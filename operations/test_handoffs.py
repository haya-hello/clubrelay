"""交接闭环及边界测试，全部使用虚构资料。 / Handover workflow and boundary tests with fictional material only."""
import copy
import hashlib
import json
import uuid
from unittest.mock import patch
from django.contrib.auth.models import User, Group
from django.core.exceptions import ValidationError, PermissionDenied
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from activities.models import Activity
from . import handoffs
from .analysis import configuration_hash, grant_permission, process_run, request_analysis, run_current
from .ai_client import _validated_result, _request_payload, AIError
from .models import Material, ImportBatch, ModelConfiguration, AnalysisRun, HandoffPack, HandoffItem, Person, AnalysisPermission


class HandoffTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user("handoff-manager", password="fictional-password")
        cls.manager.groups.add(Group.objects.create(name="社团负责人"))
        cls.member = User.objects.create_user("ordinary-member", password="fictional-password")
        cls.event = Activity.objects.create(token=uuid.uuid4(), title="虚构分享会", creator=cls.manager)
        cls.batch = ImportBatch.objects.create(event=cls.event, actor=cls.manager)
        cls.a = cls.material("准备.md", "预计40人参加。签到表与二维码分开放置。")
        cls.b = cls.material("复盘.md", "实际到场23人。投影连接出现问题，开场延迟8分钟。故障原因未确认。")
        cls.c = cls.material("未选.md", "PRIVATE_UNSELECTED_MUST_NOT_BE_SENT")
        cls.config = ModelConfiguration.objects.create(pk=1, mode="local", enabled=True, base_url="http://127.0.0.1:11434", model="fictional-test-model")

    @classmethod
    def material(cls, name, text):
        return Material.objects.create(event=cls.event, batch=cls.batch, original_name=name, file="archive/fictional.txt", text=text, parse_status="parsed", sha256=hashlib.sha256(text.encode()).hexdigest(), size=len(text))

    def result(self):
        return {"items": [
            {"section": "practice", "title": "签到分流", "record": "筹备计划提出分开放置签到与二维码。", "suggestion": "下次可在适用时采用。", "conditions": "计划不代表已证明效果。", "citations": [{"source_id": str(self.a.pk), "quote": "签到表与二维码分开放置。"}]},
            {"section": "pitfall", "title": "设备连接问题", "record": "投影连接出现问题，开场延迟8分钟。", "suggestion": "下次提前检查设备。", "conditions": "故障原因未确认。", "citations": [{"source_id": str(self.b.pk), "quote": "投影连接出现问题，开场延迟8分钟。"}]},
            {"section": "question", "title": "设备借用联系人是谁？", "record": "", "suggestion": "向上一任负责人核实。", "conditions": "资料未记录，不代表没有联系人。", "citations": []},
        ]}

    def setUp(self):
        self.model_patch = patch("operations.analysis.analyze_sources", side_effect=lambda **kwargs: self.result())
        self.model = self.model_patch.start()
        self.addCleanup(self.model_patch.stop)
        self.connection_patch = patch("operations.analysis.close_old_connections")
        self.connection_patch.start()
        self.addCleanup(self.connection_patch.stop)
        self.pool_patch = patch("operations.analysis.POOL.submit")
        self.pool = self.pool_patch.start()
        self.addCleanup(self.pool_patch.stop)
        self.client.force_login(self.manager)
        self.grant()

    def grant(self):
        self.config.refresh_from_db()
        grant_permission(self.manager, self.event, [str(self.a.pk), str(self.b.pk)], expected_config_fingerprint=configuration_hash(self.config))

    def make(self, *, initialize=True, process=True, token=None, parent=None):
        pack = handoffs.create_pack(self.manager, self.event, "活动交接", "仅用于虚构测试", [str(self.a.pk), str(self.b.pk)], token or uuid.uuid4(), parent=parent, background=False)
        if process:
            process_run(pack.source_analysis_id)
        if initialize:
            handoffs.initialize_pack(self.manager, pack.pk, pack.lock_version)
        pack.refresh_from_db()
        return pack

    def review(self, pack):
        for item in pack.items.all():
            pack.refresh_from_db()
            handoffs.edit_item(self.manager, pack.pk, item.pk, pack.lock_version, {k: getattr(item, k) for k in handoffs.EDIT_FIELDS}, "keep", True)
        pack.refresh_from_db()

    def confirm(self, pack):
        self.review(pack)
        handoffs.confirm_pack(self.manager, pack.pk, pack.lock_version, "演示负责人")
        pack.refresh_from_db()
        return pack

    def test_selection_sends_only_a_b(self):
        pack = self.make()
        payload = self.model.call_args.kwargs
        self.assertEqual(payload["purpose"], "handoff")
        self.assertEqual({s["id"] for s in payload["sources"]}, {str(self.a.pk), str(self.b.pk)})
        self.assertNotIn("PRIVATE_UNSELECTED", json.dumps(payload))
        self.assertEqual(pack.items.count(), 3)

    def test_local_credentials_are_opt_in(self):
        with patch("operations.analysis.get_key", return_value="fictional-key") as secret:
            self.make()
            secret.assert_not_called()
            self.assertEqual(self.model.call_args.kwargs["api_key"], "")

    def test_authorized_local_gateway_receives_its_key(self):
        self.config.local_auth = True
        self.config.save()
        with patch("operations.analysis.get_key", return_value="fictional-key"):
            self.grant()
            self.make()
        self.assertEqual(self.model.call_args.kwargs["api_key"], "fictional-key")

    def test_local_auth_change_invalidates_processing_permission(self):
        self.config.local_auth = True
        self.config.save()
        with patch("operations.analysis.get_key", return_value="fictional-key"):
            with self.assertRaises(ValidationError):
                self.make()
        self.model.assert_not_called()

    def test_unpermitted_material_is_rejected_before_call(self):
        with self.assertRaises(ValidationError):
            handoffs.create_pack(self.manager, self.event, "x", "", [str(self.c.pk)], uuid.uuid4(), background=False)
        self.model.assert_not_called()

    def test_capacity_does_not_truncate(self):
        Material.objects.filter(pk=self.a.pk).update(text="字" * 6001)
        with self.assertRaises(ValidationError):
            self.make()
        self.assertEqual(HandoffPack.objects.count(), 0)
        self.model.assert_not_called()

    def test_excluded_source_cannot_generate(self):
        Material.objects.filter(pk=self.b.pk).update(excluded=True)
        with self.assertRaises(ValidationError):
            self.make()

    def test_invalid_citation_and_more_than_twelve_items(self):
        payload = self.result()
        payload["items"][0]["citations"][0]["quote"] = "不存在的引文"
        from .handoff_ai import validate_handoff_result
        with self.assertRaises(AIError):
            validate_handoff_result(payload, {str(self.a.pk): self.a.text, str(self.b.pk): self.b.text})
        payload = {"items": [self.result()["items"][2]] * 13}
        with self.assertRaises(AIError):
            validate_handoff_result(payload, {})

    def test_response_envelope_uses_dedicated_validator(self):
        content = self.result()
        envelope = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(content)}}]}
        sources = {str(self.a.pk): self.a.text, str(self.b.pk): self.b.text}
        self.assertEqual(_validated_result(envelope, sources, "handoff"), content)
        with self.assertRaises(AIError):
            _validated_result(envelope, sources)
        raw, _ = _request_payload("test-model", [{"id": str(self.a.pk), "title": "A", "text": self.a.text}], "交接", purpose="handoff")
        self.assertIn("最多12条", raw.decode())

    def test_bad_json_and_fabricated_source(self):
        envelope = {"choices": [{"message": {"content": "not-json"}}]}
        with self.assertRaises(AIError):
            _validated_result(envelope, {}, "handoff")
        content = self.result()
        content["items"][0]["citations"][0]["source_id"] = str(self.c.pk)
        envelope["choices"][0]["message"]["content"] = json.dumps(content)
        with self.assertRaises(AIError):
            _validated_result(envelope, {str(self.a.pk): self.a.text, str(self.b.pk): self.b.text}, "handoff")

    def test_duplicate_create_and_initialize_are_idempotent(self):
        token = uuid.uuid4()
        first = self.make(token=token)
        second = handoffs.create_pack(self.manager, self.event, "活动交接", "仅用于虚构测试", [str(self.a.pk), str(self.b.pk)], token, background=False)
        handoffs.initialize_pack(self.manager, second.pk, 1)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(HandoffItem.objects.count(), 3)
        self.assertEqual(AnalysisRun.objects.count(), 1)
        self.assertEqual(self.model.call_count, 1)

    def test_repeated_token_cannot_change_selection(self):
        pack = self.make()
        with self.assertRaises(handoffs.Conflict):
            handoffs.create_pack(self.manager, self.event, "活动交接", "仅用于虚构测试", [str(self.a.pk)], pack.pk, background=False)

    def test_regenerate_creates_new_pack_and_run(self):
        first = self.confirm(self.make())
        second = self.make(parent=first)
        self.assertNotEqual(first.source_analysis_id, second.source_analysis_id)
        self.assertEqual(second.parent_id, first.pk)
        first.refresh_from_db()
        self.assertEqual(first.status, "confirmed")

    def test_edit_resets_review_and_retains_original(self):
        pack = self.make()
        self.review(pack)
        item = pack.items.first()
        original = copy.deepcopy(item.original)
        values = {k: getattr(item, k) for k in handoffs.EDIT_FIELDS}
        values["record"] = "负责人对照材料修订：这是计划。"
        handoffs.edit_item(self.manager, pack.pk, item.pk, pack.lock_version, values, "keep", True)
        item.refresh_from_db()
        self.assertEqual(item.decision, "pending")
        self.assertEqual(item.original, original)
        self.assertTrue(item.edited)

    def test_pending_or_all_dropped_cannot_confirm(self):
        pack = self.make()
        with self.assertRaises(ValidationError):
            handoffs.confirm_pack(self.manager, pack.pk, pack.lock_version, "测试")
        pack.items.update(decision="drop")
        pack.refresh_from_db()
        with self.assertRaises(ValidationError):
            handoffs.confirm_pack(self.manager, pack.pk, pack.lock_version, "测试")

    def test_keep_requires_acknowledgement(self):
        pack = self.make()
        item = pack.items.first()
        with self.assertRaises(ValidationError):
            handoffs.edit_item(self.manager, pack.pk, item.pk, pack.lock_version, {k: getattr(item, k) for k in handoffs.EDIT_FIELDS}, "keep")

    def test_source_free_item_cannot_become_a_fact(self):
        pack = self.make()
        item = pack.items.get(section="question")
        values = {k: getattr(item, k) for k in handoffs.EDIT_FIELDS}
        values["record"] = "没有来源却写成事实"
        with self.assertRaises(ValidationError):
            handoffs.edit_item(self.manager, pack.pk, item.pk, pack.lock_version, values, "save")

    def test_confirmed_readonly_copy_resets_review(self):
        pack = self.confirm(self.make())
        item = pack.items.first()
        with self.assertRaises(handoffs.Conflict):
            handoffs.edit_item(self.manager, pack.pk, item.pk, pack.lock_version, {k: getattr(item, k) for k in handoffs.EDIT_FIELDS}, "drop")
        token = uuid.uuid4()
        copied = handoffs.copy_pack(self.manager, pack.pk, token)
        self.assertEqual(copied.items.filter(decision="pending").count(), 3)
        self.assertIsNone(copied.confirmed_at)
        self.assertEqual(handoffs.copy_pack(self.manager, pack.pk, token).pk, copied.pk)
        pack.refresh_from_db()
        self.assertEqual(pack.status, "confirmed")

    def test_stale_edit_returns_conflict_without_overwrite(self):
        pack = self.make()
        item = pack.items.first()
        version = pack.lock_version
        values = {k: getattr(item, k) for k in handoffs.EDIT_FIELDS}
        handoffs.edit_item(self.manager, pack.pk, item.pk, version, values, "keep", True)
        with self.assertRaises(handoffs.Conflict):
            handoffs.edit_item(self.manager, pack.pk, item.pk, version, {**values, "title": "过时覆盖"}, "save")
        item.refresh_from_db()
        self.assertNotEqual(item.title, "过时覆盖")

    def test_material_changes_block_export_without_version_increment(self):
        pack = self.confirm(self.make())
        for change in ({"text": "新正文"}, {"sha256": "b" * 64}, {"original_name": "改名.md"}, {"excluded": True}, {"parse_status": "failed"}):
            with self.subTest(change=change):
                Material.objects.filter(pk=self.a.pk).update(**change)
                self.assertFalse(handoffs.sources_current(pack))
                with self.assertRaises(ValidationError):
                    handoffs.export_data(pack)
                Material.objects.filter(pk=self.a.pk).update(text=self.a.text, sha256=self.a.sha256, original_name=self.a.original_name, excluded=False, parse_status="parsed")

    def test_zip_parent_exclusion_and_move_invalidate(self):
        pack = self.confirm(self.make())
        Material.objects.filter(pk=self.a.pk).update(parent=self.c)
        Material.objects.filter(pk=self.c.pk).update(excluded=True)
        self.assertFalse(handoffs.sources_current(pack))
        Material.objects.filter(pk=self.a.pk).update(parent=None)
        other = Activity.objects.create(token=uuid.uuid4(), title="其他活动", creator=self.manager)
        Material.objects.filter(pk=self.a.pk).update(event=other)
        self.assertFalse(handoffs.sources_current(pack))

    def test_unrelated_roster_and_new_material_leave_prepared_pack_valid(self):
        pack = self.confirm(self.make())
        Person.objects.create(external_id="new-person", display_name="虚构新成员")
        self.material("新增资料.md", "未选择的新材料")
        self.assertFalse(run_current(pack.source_analysis))
        self.assertTrue(handoffs.sources_current(pack))
        self.assertEqual(len(handoffs.export_data(pack)["entries"]), 3)

    def test_revoke_and_model_change_do_not_delete_confirmed_pack(self):
        pack = self.confirm(self.make())
        AnalysisPermission.objects.all().delete()
        ModelConfiguration.objects.filter(pk=1).update(enabled=False)
        self.assertTrue(handoffs.sources_current(pack))
        self.assertEqual(handoffs.export_data(pack)["reviewer"], "演示负责人")

    def test_cancel_and_restart_never_resend(self):
        pack = self.make(process=False, initialize=False)
        AnalysisRun.objects.filter(pk=pack.source_analysis_id).update(status="cancelled")
        process_run(pack.source_analysis_id)
        self.model.assert_not_called()
        another = self.make(process=False, initialize=False)
        call_command("recover_analysis", verbosity=0)
        another.source_analysis.refresh_from_db()
        self.assertEqual(another.source_analysis.status, "failed")
        self.model.assert_not_called()

    def test_timeout_retains_previous_confirmed_pack(self):
        pack = self.confirm(self.make())
        self.model.side_effect = AIError("timeout")
        failed = self.make(initialize=False, parent=pack)
        failed.source_analysis.refresh_from_db()
        self.assertEqual(failed.source_analysis.status, "failed")
        pack.refresh_from_db()
        self.assertEqual(pack.status, "confirmed")

    def test_empty_output_is_not_a_complete_handover(self):
        self.model.side_effect = lambda **kwargs: {"items": []}
        pack = self.make()
        self.assertEqual(pack.items.count(), 0)
        with self.assertRaises(ValidationError):
            handoffs.confirm_pack(self.manager, pack.pk, pack.lock_version, "测试")

    def test_export_matches_reviewed_content_and_no_private_metadata(self):
        pack = self.make()
        self.review(pack)
        pack.items.filter(section="practice").update(decision="drop")
        handoffs.confirm_pack(self.manager, pack.pk, pack.lock_version, "公开显示名")
        pack.refresh_from_db()
        data = handoffs.export_data(pack)
        text = handoffs.export_markdown(data)
        self.assertEqual(len(data["entries"]), 2)
        self.assertNotIn("签到分流", text)
        for secret in ("fictional-test-model", "127.0.0.1", "handoff-manager", "PRIVATE_UNSELECTED", "archive/fictional.txt"):
            self.assertNotIn(secret, text)
        self.assertIn("待确认", text)

    def test_injection_is_escaped_in_preview_and_markdown(self):
        pack = self.make()
        item = pack.items.first()
        item.title = '<script>alert("x")</script>'
        item.save()
        pack = self.confirm(pack)
        response = self.client.get(reverse("handoff_export", args=[pack.pk, "preview"]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '<script>alert("x")</script>')
        self.assertIn("&lt;script&gt;", handoffs.export_markdown(handoffs.export_data(pack)))

    def test_permissions_cover_every_new_endpoint(self):
        pack = self.make()
        item = pack.items.first()
        self.client.force_login(self.member)
        gets = [("handoff_start", [self.event.pk]), ("handoff_permission", [self.event.pk]), ("handoff_detail", [pack.pk]), ("handoff_export", [pack.pk, "preview"])]
        posts = [("handoff_prepare", [pack.pk]), ("handoff_item_save", [pack.pk, item.pk]), ("handoff_confirm", [pack.pk]), ("handoff_copy", [pack.pk]), ("handoff_cancel", [pack.pk]), ("handoff_export", [pack.pk, "markdown"])]
        for name, args in gets:
            self.assertIn(self.client.get(reverse(name, args=args)).status_code, (302, 403))
        for name, args in posts:
            self.assertIn(self.client.post(reverse(name, args=args), {}).status_code, (302, 403))

    def test_service_rechecks_revoked_manager(self):
        pack = self.make()
        self.manager.groups.clear()
        with self.assertRaises(PermissionDenied):
            handoffs.initialize_pack(self.manager, pack.pk, pack.lock_version)

    def test_get_pages_have_no_generation_side_effect(self):
        pack = self.make(process=False, initialize=False)
        before = HandoffItem.objects.count()
        for name, args in (("handoff_start", [self.event.pk]), ("handoff_permission", [self.event.pk]), ("handoff_detail", [pack.pk])):
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200)
        self.assertEqual(HandoffItem.objects.count(), before)
        self.model.assert_not_called()

    def test_permission_save_does_not_generate(self):
        response = self.client.post(reverse("handoff_permission", args=[self.event.pk]), {"materials": [str(self.a.pk)], "consent": "on", "config_fingerprint": configuration_hash(self.config)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AnalysisRun.objects.count(), 0)
        self.pool.assert_not_called()

    def test_http_review_confirm_download_and_general_redirect(self):
        pack = self.make()
        self.assertEqual(self.client.get(reverse("ops_analysis", args=[pack.source_analysis_id])).status_code, 302)
        response = self.client.get(reverse("ops_assistant"))
        self.assertNotContains(response, str(pack.source_analysis_id))
        for item in pack.items.all():
            pack.refresh_from_db()
            response = self.client.post(reverse("handoff_item_save", args=[pack.pk, item.pk]), {**{k: getattr(item, k) for k in handoffs.EDIT_FIELDS}, "version": pack.lock_version, "action": "keep", "acknowledge": "on"})
            self.assertEqual(response.status_code, 302)
        pack.refresh_from_db()
        response = self.client.post(reverse("handoff_confirm", args=[pack.pk]), {"version": pack.lock_version, "reviewer_label": "试用负责人", "confirm_ack": "on"})
        self.assertEqual(response.status_code, 302)
        pack.refresh_from_db()
        self.assertEqual(self.client.get(reverse("handoff_export", args=[pack.pk, "markdown"])).status_code, 405)
        download = self.client.post(reverse("handoff_export", args=[pack.pk, "markdown"]), {"version": pack.lock_version, "export_ack": "on"})
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download["Content-Disposition"])

    def test_http_invalid_form_keeps_input_and_conflict_is_409(self):
        pack = self.make()
        item = pack.items.first()
        fields = {k: getattr(item, k) for k in handoffs.EDIT_FIELDS}
        response = self.client.post(reverse("handoff_item_save", args=[pack.pk, item.pk]), {**fields, "title": "", "suggestion": "尚未保存的意见", "version": pack.lock_version, "action": "save"})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "尚未保存的意见", status_code=400)
        response = self.client.post(reverse("handoff_item_save", args=[pack.pk, item.pk]), {**fields, "version": 0, "action": "drop"})
        self.assertEqual(response.status_code, 409)

    def test_stale_preview_is_blocked(self):
        pack = self.confirm(self.make())
        Material.objects.filter(pk=self.b.pk).update(excluded=True)
        response = self.client.get(reverse("handoff_export", args=[pack.pk, "preview"]))
        self.assertEqual(response.status_code, 409)

    def test_refresh_preserves_confirmed_review_and_original_result(self):
        pack = self.confirm(self.make())
        reread = HandoffPack.objects.get(pk=pack.pk)
        self.assertEqual(reread.items.filter(decision="keep").count(), 3)
        self.assertEqual(reread.source_analysis.result, self.result())
        self.assertEqual(reread.status, "confirmed")
