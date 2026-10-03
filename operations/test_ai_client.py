"""所有 AI 请求仅进入内存模拟传输。 / All AI requests use in-memory mock transport only."""

import copy
import importlib.util
import json
import traceback
from unittest import TestCase
from unittest.mock import patch

import httpx

from . import ai_client
from .ai_client import AIError, MAX_RESPONSE_BYTES, analyze_sources


FICTIONAL_KEY = "fictional-key-not-a-real-credential"
SOURCES = [
    {"id": "source-a", "title": "虚构活动记录", "text": "成员完成了海报。活动现场网络中断，改用离线演示。"},
    {"id": "source-b", "title": "虚构报名表", "text": "本次收到八份报名，实际到场人数尚未记录。"},
]


def item(kind="fact", text="材料记录成员完成海报。", source_id="source-a", quote="成员完成了海报。"):
    return {"kind": kind, "text": text, "citations": [{"source_id": source_id, "quote": quote}]}


def envelope(result=None, **message_changes):
    message = {"role": "assistant", "content": json.dumps(result or {"items": [item()]}, ensure_ascii=False)}
    message.update(message_changes)
    return {"choices": [{"message": message, "finish_reason": "stop"}]}


class AIClientTests(TestCase):
    def setUp(self):
        self.requests = []
        self.options = []
        self.handler = lambda request: httpx.Response(200, json=envelope())
        actual_client = httpx.Client

        def transport_handler(request):
            self.requests.append(request)
            return self.handler(request)

        def factory(**kwargs):
            self.options.append(kwargs)
            return actual_client(transport=httpx.MockTransport(transport_handler), **kwargs)

        self.client_patch = patch("operations.ai_client.httpx.Client", side_effect=factory)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    def call(self, **changes):
        arguments = {
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "fictional-configurable-model",
            "api_key": "",
            "mode": "local",
            "sources": copy.deepcopy(SOURCES),
            "question": "活动有哪些可观察结果与问题？",
        }
        arguments.update(changes)
        return analyze_sources(**arguments)

    def assert_error(self, expected, **changes):
        with self.assertRaises(AIError) as caught:
            self.call(**changes)
        self.assertEqual(caught.exception.code, expected)
        self.assertNotIn(FICTIONAL_KEY, str(caught.exception))
        self.assertNotIn(FICTIONAL_KEY, repr(caught.exception))
        return caught.exception

    def test_valid_citations_return_structured_result(self):
        self.assertEqual(self.call(), {"items": [item()]})
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(str(self.requests[0].url), "http://127.0.0.1:11434/v1/chat/completions")

    def test_request_contains_only_selected_data_and_explicit_model(self):
        sources = copy.deepcopy(SOURCES)
        sources[0]["private_path"] = "fictional-path-not-to-send"
        self.call(sources=sources, model="a-user-configured-model")
        payload = json.loads(self.requests[0].content)
        self.assertEqual(payload["model"], "a-user-configured-model")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertIn("不可信数据", payload["messages"][0]["content"])
        self.assertNotIn("tools", payload)
        selected = json.loads(payload["messages"][1]["content"])["sources"]
        self.assertEqual(selected, SOURCES)
        self.assertNotIn("fictional-path-not-to-send", self.requests[0].content.decode())

    def test_localhost_is_pinned_to_loopback(self):
        self.call(base_url="http://localhost:12345/")
        self.assertEqual(str(self.requests[0].url), "http://127.0.0.1:12345/v1/chat/completions")

    def test_allowed_clouds_use_https_and_credential_header(self):
        for url in ("https://api.deepseek.com", "https://api.openai.com/v1/"):
            with self.subTest(url=url):
                self.call(mode="cloud", base_url=url, api_key=FICTIONAL_KEY)
                request = self.requests[-1]
                self.assertEqual(request.headers["Authorization"], f"Bearer {FICTIONAL_KEY}")
                self.assertEqual(request.url.scheme, "https")
                self.assertNotIn(FICTIONAL_KEY, request.content.decode())

    def test_transport_disables_proxy_and_redirect_with_thirty_second_timeout(self):
        self.call()
        options = self.options[0]
        self.assertFalse(options["follow_redirects"])
        self.assertFalse(options["trust_env"])
        self.assertEqual(options["timeout"].as_dict(), {"connect": 30, "read": 30, "write": 30, "pool": 30})
        self.assertEqual(self.requests[0].headers["Accept-Encoding"], "identity")

    def test_invalid_local_addresses_are_rejected_before_any_call(self):
        for url in (
            "http://192.168.1.2:11434/v1",
            "http://127.1/v1",
            "http://localhost.example.invalid/v1",
            "http://[::1]:11434/v1",
            "https://127.0.0.1/v1",
            "http://user:secret@127.0.0.1/v1",
            "http://127.0.0.1/v1?key=secret",
            "http://127.0.0.1/v1#secret",
            "http://127.0.0.1/v1/../other",
            "http://127.0.0.1:0/v1",
            "http://127.0.0.1:70000/v1",
            "http://127.0.0.1\\@example.invalid/v1",
            "http://127.0.0.1/\n",
        ):
            with self.subTest(url=url):
                self.assert_error("invalid_config", base_url=url)
        self.assertFalse(self.requests)

    def test_handoff_gateway_uses_bounded_wait_and_one_request(self):
        self.handler = lambda request: httpx.Response(200, json=envelope({"items": []}))
        self.assertEqual(self.call(purpose="handoff", api_key=FICTIONAL_KEY), {"items": []})
        self.assertEqual(self.options[0]["timeout"].read, 120)
        self.assertFalse(self.options[0]["follow_redirects"])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].headers["Authorization"], "Bearer " + FICTIONAL_KEY)

    def test_cloud_mode_rejects_private_or_nonallowlisted_origins(self):
        for url in (
            "https://127.0.0.1/v1",
            "https://localhost/v1",
            "https://192.168.1.2/v1",
            "https://169.254.169.254/v1",
            "http://api.openai.com/v1",
            "https://api.openai.com.example.invalid/v1",
            "https://api.openai.com:8443/v1",
            "https://api.openai.com@localhost/v1",
            "https://api.deepseek.com/other-path",
        ):
            with self.subTest(url=url):
                self.assert_error("invalid_config", mode="cloud", base_url=url, api_key=FICTIONAL_KEY)
        self.assertFalse(self.requests)

    def test_disabled_or_unknown_mode_never_calls(self):
        self.assert_error("invalid_config", mode="disabled")
        self.assertFalse(self.requests)

    def test_missing_cloud_key_never_calls(self):
        self.assert_error("missing_api_key", mode="cloud", base_url="https://api.openai.com")
        self.assertFalse(self.requests)

    def test_invalid_inputs_never_call(self):
        for changes, code in (
            ({"sources": []}, "invalid_sources"),
            ({"sources": [SOURCES[0], SOURCES[0]]}, "invalid_sources"),
            ({"sources": [{"id": "a", "title": "x", "text": ""}]}, "invalid_sources"),
            ({"sources": ["not-a-source"]}, "invalid_sources"),
            ({"question": ""}, "invalid_question"),
            ({"question": "a" * 6001}, "invalid_question"),
            ({"model": ""}, "invalid_config"),
            ({"api_key": FICTIONAL_KEY + "\n"}, "invalid_config"),
        ):
            with self.subTest(changes=changes):
                self.assert_error(code, **changes)
        self.assertFalse(self.requests)

    def test_oversized_request_never_calls(self):
        self.assert_error(
            "invalid_sources", sources=[{"id": "large", "title": "大资料", "text": "a" * (1024 * 1024)}]
        )
        self.assertFalse(self.requests)

    def test_redirect_is_not_followed_even_to_other_allowlisted_cloud(self):
        self.handler = lambda request: httpx.Response(307, headers={"Location": "https://api.openai.com/v1/chat/completions"})
        self.assert_error("redirect_blocked", mode="cloud", base_url="https://api.deepseek.com", api_key=FICTIONAL_KEY)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].url.host, "api.deepseek.com")

    def test_local_redirect_to_cloud_is_blocked(self):
        self.handler = lambda request: httpx.Response(302, headers={"Location": "https://example.invalid/leak"})
        self.assert_error("redirect_blocked")
        self.assertEqual(len(self.requests), 1)

    def test_authentication_error_is_safe_and_not_retried(self):
        self.handler = lambda request: httpx.Response(401, text=FICTIONAL_KEY + " secret server error")
        error = self.assert_error("authentication_failed", api_key=FICTIONAL_KEY)
        self.assertNotIn("secret server error", error.message)
        self.assertEqual(len(self.requests), 1)

    def test_error_statuses_do_not_expose_remote_body(self):
        for status, code in ((403, "authentication_failed"), (429, "rate_limited"), (500, "service_unavailable"), (400, "request_rejected")):
            with self.subTest(status=status):
                self.handler = lambda request, status=status: httpx.Response(status, text=FICTIONAL_KEY)
                self.assert_error(code)

    def test_timeout_is_safe_without_automatic_retry(self):
        def fail(request):
            raise httpx.ReadTimeout(FICTIONAL_KEY, request=request)

        self.handler = fail
        error = self.assert_error("timeout")
        self.assertNotIn(FICTIONAL_KEY, "".join(traceback.format_exception(error)))
        self.assertEqual(len(self.requests), 1)

    def test_connection_failure_is_safe(self):
        def fail(request):
            raise httpx.ConnectError(FICTIONAL_KEY, request=request)

        self.handler = fail
        self.assert_error("connection_failed")
        self.assertEqual(len(self.requests), 1)

    def test_oversized_content_length_rejected(self):
        self.handler = lambda request: httpx.Response(200, headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)}, content=b"{}")
        self.assert_error("response_too_large")

    def test_oversized_stream_without_content_length_rejected(self):
        class BigStream(httpx.SyncByteStream):
            def __iter__(self):
                for _ in range(17):
                    yield b"a" * 8192

        self.handler = lambda request: httpx.Response(200, stream=BigStream())
        self.assert_error("response_too_large")

    def test_compressed_response_is_not_decompressed(self):
        self.handler = lambda request: httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(b"not-a-gzip-payload"))
        self.assert_error("invalid_response")

    def test_invalid_json_body_is_safe(self):
        self.handler = lambda request: httpx.Response(200, text=FICTIONAL_KEY)
        self.assert_error("invalid_response")

    def test_invalid_completion_envelopes_are_rejected(self):
        invalid = [
            [],
            {},
            {"choices": []},
            envelope(content=None),
            envelope(tool_calls=[{"function": {"name": "evil"}}]),
            envelope(refusal="refused"),
            envelope(content="```json\n{}\n```"),
            {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]},
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.handler = lambda request, value=value: httpx.Response(200, json=value)
                self.assert_error("invalid_response")

    def test_bad_schema_kinds_lengths_and_extra_fields_are_rejected(self):
        invalid_items = [
            {**item(), "kind": "personality"},
            {**item(), "kind": []},
            {**item(), "text": "a" * 2001},
            {**item(), "text": " "},
            {**item(), "hidden": "not-allowed"},
            {"kind": "gap", "text": "缺少数据"},
        ]
        for value in invalid_items:
            with self.subTest(value=value):
                self.handler = lambda request, value=value: httpx.Response(200, json=envelope({"items": [value]}))
                self.assert_error("invalid_response")

    def test_item_count_is_bounded(self):
        self.handler = lambda request: httpx.Response(200, json=envelope({"items": [item()] * 31}))
        self.assert_error("invalid_response")

    def test_unknown_source_and_changed_quote_are_rejected(self):
        for value in (
            item(source_id="not-selected"),
            item(quote="所有人都完成了海报。"),
            item(source_id="source-b", quote="成员完成了海报。"),
            item(quote=" "),
            item(quote="字" * 1001),
        ):
            with self.subTest(value=value):
                self.handler = lambda request, value=value: httpx.Response(200, json=envelope({"items": [value]}))
                self.assert_error("invalid_citation")

    def test_each_nongap_kind_requires_evidence(self):
        for kind in ("fact", "hypothesis", "suggestion"):
            with self.subTest(kind=kind):
                self.handler = lambda request, kind=kind: httpx.Response(200, json=envelope({"items": [{"kind": kind, "text": "测试", "citations": []}]}))
                self.assert_error("invalid_citation")

    def test_gap_can_omit_citations_and_empty_result_is_valid(self):
        for result in ({"items": [{"kind": "gap", "text": "没有签到资料，不能确认到场人数。", "citations": []}]}, {"items": []}):
            with self.subTest(result=result):
                self.handler = lambda request, result=result: httpx.Response(200, json=envelope(result))
                self.assertEqual(self.call(), result)

    def test_duplicate_json_fields_are_rejected(self):
        self.handler = lambda request: httpx.Response(200, json=envelope(content='{"items":[],"items":[]}'))
        self.assert_error("invalid_response")

    def test_importing_client_never_creates_network_client(self):
        # 独立载入模块也不得产生请求。 / Loading this module must not initiate a request.
        with patch("httpx.Client") as constructor:
            spec = importlib.util.spec_from_file_location("_ai_client_import_probe", ai_client.__file__)
            spec.loader.exec_module(importlib.util.module_from_spec(spec))
            constructor.assert_not_called()
