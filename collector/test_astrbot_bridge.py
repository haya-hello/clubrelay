import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase

from collector.bridge_security import sign_payload as server_sign_payload


PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "integrations" / "astrbot_qinglian_collector"


def _load_bridge():
    spec = importlib.util.spec_from_file_location("qinglian_bridge", PLUGIN_ROOT / "bridge.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


bridge = _load_bridge()


class AstrBotBridgeHelperTests(TestCase):
    def test_signature_matches_server(self):
        body = bridge.encode_payload({"text": "中文经验", "message_id": "m1"})
        self.assertEqual(
            bridge.sign_payload("1700000000", body, "shared-secret"),
            server_sign_payload("1700000000", body, "shared-secret"),
        )

    def test_capture_reply_only_for_capture_result(self):
        self.assertIsNone(bridge.capture_reply({"ok": True, "stored": True}))
        self.assertIn("已加入", bridge.capture_reply({"capture": "created"}))
        self.assertIn("负责人", bridge.capture_reply({"capture": "manager_required"}))

    def test_post_event_uses_signed_stable_body(self):
        captured = {}

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"ok": True, "capture": "created"}

        class FakeClient:
            def __init__(self, **kwargs):
                captured["client"] = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, url, content, headers):
                captured.update(url=url, content=content, headers=headers)
                return FakeResponse()

        original = bridge.httpx.AsyncClient
        bridge.httpx.AsyncClient = FakeClient
        try:
            result = asyncio.run(
                bridge.post_event("http://127.0.0.1:8021/events", "secret", {"a": "甲"})
            )
        finally:
            bridge.httpx.AsyncClient = original
        self.assertTrue(result["ok"])
        self.assertEqual(json.loads(captured["content"]), {"a": "甲"})
        timestamp = captured["headers"]["X-Qinglian-Timestamp"]
        self.assertEqual(
            captured["headers"]["X-Qinglian-Signature"],
            bridge.sign_payload(timestamp, captured["content"], "secret"),
        )
        self.assertFalse(captured["client"]["trust_env"])
