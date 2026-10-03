"""分析许可与状态的隔离测试，不访问模型。 / Isolated consent/state tests without any model network calls."""

import copy
import hashlib
import json
import uuid
from datetime import date, datetime, timedelta
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from activities.models import Activity
from . import analysis
from .analysis import (
    CONFIG_LOCK, allowed_materials, collect_sources, configuration_hash,
    grant_permission, process_run, request_analysis, run_current, scope_fingerprint,
)
from .models import Alias, AnalysisPermission, AnalysisRun, Evidence, ImportBatch, Material, Mention, ModelConfiguration, Person


class AnalysisLifecycleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.group = Group.objects.create(name="社团负责人")
        cls.manager = User.objects.create_user("analysis_test_manager", password="fictional-password")
        cls.manager.groups.add(cls.group)
        cls.ordinary = User.objects.create_user("analysis_test_member", password="fictional-password")
        cls.event = Activity.objects.create(token=uuid.uuid4(), creator=cls.manager, title="虚构分析活动")
        cls.other_event = Activity.objects.create(token=uuid.uuid4(), creator=cls.manager, title="其他虚构活动")
        cls.batch = ImportBatch.objects.create(event=cls.event, actor=cls.manager)
        cls.person = Person.objects.create(
            external_id="FICTIONAL-MEMBER", display_name="测试同学", notes="PRIVATE_PERSON_NOTE_NOT_AUTHORIZED"
        )
        cls.selected = Material.objects.create(
            event=cls.event, batch=cls.batch, original_name="selected.txt", file="archive/fictional-selected.txt",
            sha256=hashlib.sha256(b"selected").hexdigest(), size=12, parse_status="parsed",
            text="测试同学提交了海报。另一位同学负责签到。",
        )
        cls.unselected = Material.objects.create(
            event=cls.event, batch=cls.batch, original_name="unselected.txt", file="archive/fictional-unselected.txt",
            sha256=hashlib.sha256(b"unselected").hexdigest(), size=10, parse_status="parsed",
            text="未选资料不能发送。",
        )
        Mention.objects.create(member=cls.person, material=cls.selected, anchor="第1行", excerpt="测试同学提交了海报。")
        cls.config = ModelConfiguration.objects.create(
            pk=1, enabled=True, mode="local", base_url="http://127.0.0.1:11434/v1", model="fictional-local-model"
        )

    def setUp(self):
        self.key_patch = patch("operations.analysis.get_key", return_value="fictional-api-credential")
        self.key = self.key_patch.start()
        self.addCleanup(self.key_patch.stop)
        self.client_patch = patch("operations.analysis.analyze_sources", return_value={"items": [{
            "kind": "fact", "text": "资料记录测试同学提交海报。",
            "citations": [{"source_id": str(self.selected.pk), "quote": "测试同学提交了海报。"}],
        }]})
        self.client = self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.pool_patch = patch("operations.analysis.POOL.submit")
        self.pool = self.pool_patch.start()
        self.addCleanup(self.pool_patch.stop)
        self.connection_patch = patch("operations.analysis.close_old_connections")
        self.connection_patch.start()
        self.addCleanup(self.connection_patch.stop)

    def grant(self, ids=None, auto_future=False):
        self.config.refresh_from_db()
        return grant_permission(
            self.manager, self.event, ids if ids is not None else [str(self.selected.pk)], auto_future,
            expected_config_fingerprint=configuration_hash(self.config),
        )

    def prepare(self, *, member=None, background=False, question="总结这些资料。"):
        self.grant()
        return request_analysis(self.manager, question, event=self.event, member=member, background=background)

    def cloud(self):
        self.config.mode = "cloud"
        self.config.base_url = "https://api.deepseek.com/v1"
        self.config.model = "fictional-cloud-model"
        self.config.save()

    def add_evidence(self):
        return Evidence.objects.create(
            member=self.person, event=self.event, material=self.selected, title="虚构海报交付",
            kind="delivery", confidence="candidate", quote="测试同学提交了海报。",
            note="PRIVATE_EVIDENCE_NOTE_NOT_AUTHORIZED", dedupe_key=uuid.uuid4().hex,
        )

    def test_permission_requires_page_configuration_fingerprint(self):
        with self.assertRaises(TypeError):
            grant_permission(self.manager, self.event, [str(self.selected.pk)])
        for value in (None, "", "wrong-fingerprint"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                grant_permission(self.manager, self.event, [str(self.selected.pk)], expected_config_fingerprint=value)
        self.assertFalse(AnalysisPermission.objects.exists())

    def test_stale_permission_page_cannot_authorize_changed_service(self):
        original = configuration_hash(self.config)
        self.cloud()
        with self.assertRaises(ValidationError):
            grant_permission(self.manager, self.event, [str(self.selected.pk)], expected_config_fingerprint=original)
        self.assertFalse(AnalysisPermission.objects.exists())
        self.client.assert_not_called()

    def test_permission_rechecks_manager_role(self):
        self.manager.groups.remove(self.group)
        with self.assertRaises(PermissionDenied):
            self.grant()
        self.assertFalse(AnalysisPermission.objects.exists())

    def test_invalid_excluded_or_other_event_selection_is_rejected(self):
        for ids in (["not-a-uuid"], [str(uuid.uuid4())], [str(self.selected.pk), str(uuid.uuid4())]):
            with self.subTest(ids=ids), self.assertRaises(ValidationError):
                self.grant(ids)
        Material.objects.filter(pk=self.selected.pk).update(excluded=True)
        with self.assertRaises(ValidationError):
            self.grant()

    def test_auto_future_never_grants_unselected_existing_material(self):
        permission = self.grant(auto_future=True)
        actual = set(allowed_materials(self.config, self.event).values_list("pk", flat=True))
        self.assertEqual(actual, {self.selected.pk})
        future = Material.objects.create(
            event=self.event, batch=self.batch, original_name="future.txt", file="archive/fictional-future.txt",
            sha256=hashlib.sha256(b"future").hexdigest(), size=5, parse_status="parsed", text="后续新增资料。",
        )
        Material.objects.filter(pk=future.pk).update(created_at=permission.granted_at + timedelta(seconds=1))
        actual = set(allowed_materials(self.config, self.event).values_list("pk", flat=True))
        self.assertEqual(actual, {self.selected.pk, future.pk})
        self.assertNotIn(self.unselected.pk, actual)

    def test_future_boundary_is_strict_and_exclusions_still_apply(self):
        permission = self.grant(auto_future=True)
        Material.objects.filter(pk=self.unselected.pk).update(created_at=permission.granted_at)
        self.assertNotIn(self.unselected.pk, allowed_materials(self.config, self.event).values_list("pk", flat=True))
        Material.objects.filter(pk=self.unselected.pk).update(created_at=permission.granted_at + timedelta(seconds=1), excluded=True)
        self.assertNotIn(self.unselected.pk, allowed_materials(self.config, self.event).values_list("pk", flat=True))

    def test_revoked_grantor_role_removes_material_permission(self):
        self.grant()
        self.manager.groups.remove(self.group)
        self.assertFalse(allowed_materials(self.config, self.event).exists())

    def test_source_collection_contains_only_authorized_bounded_text(self):
        Material.objects.filter(pk=self.selected.pk).update(text="测" * 7000)
        self.grant()
        sources = collect_sources(self.config, self.event)
        self.assertEqual([source["id"] for source in sources], [str(self.selected.pk)])
        self.assertEqual(len(sources[0]["text"]), 6000)
        self.assertTrue(sources[0]["truncated"])

    def test_successful_local_run_never_reads_or_sends_cloud_key(self):
        run = self.prepare()
        self.key.reset_mock()
        process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "complete")
        self.assertTrue(run_current(run))
        self.key.assert_not_called()
        self.assertEqual(self.client.call_args.kwargs["api_key"], "")
        self.assertEqual(self.client.call_count, 1)

    def test_member_prompt_names_target_without_private_notes(self):
        self.add_evidence()
        run = self.prepare(member=self.person, question="这个成员做了什么？")
        process_run(run.pk)
        payload = self.client.call_args.kwargs
        self.assertIn(self.person.display_name, payload["question"])
        self.assertIn(str(self.person.pk), payload["question"])
        self.assertIn("候选", payload["question"])
        sent = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("PRIVATE_PERSON_NOTE_NOT_AUTHORIZED", sent)
        self.assertNotIn("PRIVATE_EVIDENCE_NOTE_NOT_AUTHORIZED", sent)
        self.assertEqual(set(payload["sources"][0]), {"id", "title", "text"})

    def test_cancelled_pending_run_never_calls_client(self):
        run = self.prepare()
        AnalysisRun.objects.filter(pk=run.pk).update(status="cancelled")
        process_run(run.pk)
        self.client.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, "cancelled")

    def test_cancellation_during_key_capture_prevents_dispatch(self):
        self.cloud()
        run = self.prepare()

        def cancel_while_preparing():
            with CONFIG_LOCK:
                AnalysisRun.objects.filter(pk=run.pk).update(status="cancelled")
            return "fictional-api-credential"

        self.key.side_effect = cancel_while_preparing
        process_run(run.pk)
        self.client.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, "cancelled")

    def test_permission_revoked_during_key_capture_prevents_dispatch(self):
        self.cloud()
        run = self.prepare()

        def revoke_while_preparing():
            with CONFIG_LOCK:
                AnalysisPermission.objects.filter(event=self.event).delete()
            return "fictional-api-credential"

        self.key.side_effect = revoke_while_preparing
        process_run(run.pk)
        self.client.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")

    def test_config_change_during_key_capture_prevents_dispatch(self):
        self.cloud()
        run = self.prepare()

        def reconfigure_while_preparing():
            with CONFIG_LOCK:
                self.config.model = "changed-fictional-model"
                self.config.save()
            return "fictional-api-credential"

        self.key.side_effect = reconfigure_while_preparing
        process_run(run.pk)
        self.client.assert_not_called()

    def test_revoked_actor_or_disabled_actor_never_dispatches(self):
        for revoke in ("group", "active"):
            with self.subTest(revoke=revoke):
                self.manager.is_active = True
                self.manager.save()
                self.manager.groups.add(self.group)
                run = self.prepare(question=f"检查 {revoke}")
                if revoke == "group":
                    self.manager.groups.remove(self.group)
                else:
                    User.objects.filter(pk=self.manager.pk).update(is_active=False)
                process_run(run.pk)
                run.refresh_from_db()
                self.assertEqual(run.status, "failed")
        self.client.assert_not_called()

    def test_inflight_cancellation_preserves_cancelled_status(self):
        run = self.prepare()

        def cancel_inflight(**kwargs):
            with CONFIG_LOCK:
                AnalysisRun.objects.filter(pk=run.pk).update(status="cancelled")
            return {"items": []}

        self.client.side_effect = cancel_inflight
        process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(self.client.call_count, 1)
        self.assertEqual(run.status, "cancelled")
        self.assertEqual(run.result, {})

    def test_inflight_permission_revocation_discards_result(self):
        run = self.prepare()

        def revoke_inflight(**kwargs):
            with CONFIG_LOCK:
                AnalysisPermission.objects.filter(event=self.event).delete()
            return {"items": []}

        self.client.side_effect = revoke_inflight
        process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "stale")
        self.assertEqual(run.result, {})

    def test_inflight_actor_revocation_discards_result(self):
        run = self.prepare()

        def revoke_actor(**kwargs):
            self.manager.groups.remove(self.group)
            return {"items": []}

        self.client.side_effect = revoke_actor
        process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "stale")

    def test_inflight_evidence_correction_discards_result(self):
        evidence = self.add_evidence()
        run = self.prepare()

        def correct_evidence(**kwargs):
            Evidence.objects.filter(pk=evidence.pk).update(version=2, confidence="verified")
            return {"items": []}

        self.client.side_effect = correct_evidence
        process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "stale")
        self.assertEqual(run.result, {})

    def test_inflight_identity_change_discards_result(self):
        run = self.prepare(member=self.person)

        def correct_identity(**kwargs):
            Alias.objects.create(member=self.person, value="新核对昵称")
            return {"items": []}

        self.client.side_effect = correct_identity
        process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "stale")

    def test_inflight_exclusion_discards_result(self):
        run = self.prepare()

        def exclude_material(**kwargs):
            Material.objects.filter(pk=self.selected.pk).update(excluded=True)
            return {"items": []}

        self.client.side_effect = exclude_material
        process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "stale")

    def test_mismatched_source_text_hash_cannot_borrow_new_scope_fingerprint(self):
        run = self.prepare()
        Material.objects.filter(pk=self.selected.pk).update(text="新的解析正文，与旧版不同。")
        run.fingerprint = scope_fingerprint(self.event)
        self.assertFalse(run_current(run))

    def test_source_event_id_and_exact_sent_excerpt_are_checked(self):
        run = self.prepare()
        original = copy.deepcopy(run.sources)
        run.sources[0]["event_id"] = str(self.other_event.pk)
        self.assertFalse(run_current(run))
        run.sources = copy.deepcopy(original)
        run.sources[0]["text"] = "并不存在的正文"
        self.assertFalse(run_current(run))

    def test_malformed_snapshot_fails_closed_without_500(self):
        run = self.prepare()
        for sources in (None, [], [{}], [{"id": "bad-uuid"}], [run.sources[0], run.sources[0]]):
            with self.subTest(sources=sources):
                run.sources = sources
                self.assertFalse(run_current(run))

    def test_private_notes_change_fingerprint_but_are_not_collected(self):
        run = self.prepare(member=self.person)
        Person.objects.filter(pk=self.person.pk).update(notes="PRIVATE_NEW_NOTE", active=False)
        self.assertFalse(run_current(run))
        self.assertNotIn("PRIVATE_NEW_NOTE", json.dumps(collect_sources(self.config, self.event)))

    def test_request_rejects_source_change_during_collection(self):
        self.grant()
        real_collect = collect_sources

        def change_during_collection(*args, **kwargs):
            snapshots = real_collect(*args, **kwargs)
            Material.objects.filter(pk=self.selected.pk).update(text="准备期间的新正文。")
            return snapshots

        with patch("operations.analysis.collect_sources", side_effect=change_during_collection):
            with self.assertRaises(ValidationError):
                request_analysis(self.manager, "测试一致性", event=self.event, background=False)
        self.assertFalse(AnalysisRun.objects.exists())
        self.client.assert_not_called()

    def test_request_deduplicates_pending_work(self):
        first = self.prepare()
        second = request_analysis(self.manager, first.question, event=self.event, background=False)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(AnalysisRun.objects.count(), 1)

    def test_changed_selection_does_not_reuse_old_pending_work(self):
        first = self.prepare()
        self.grant(ids=[str(self.unselected.pk)])
        second = request_analysis(self.manager, first.question, event=self.event, background=False)
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual([source["id"] for source in second.sources], [str(self.unselected.pk)])
        process_run(first.pk)
        self.client.assert_not_called()
        first.refresh_from_db()
        self.assertEqual(first.status, "failed")

    def test_background_submission_waits_for_commit(self):
        with self.captureOnCommitCallbacks(execute=True):
            run = self.prepare(background=True)
            self.pool.assert_not_called()
        self.pool.assert_called_once_with(process_run, run.pk)

    def test_cloud_key_is_captured_once_at_dispatch(self):
        self.cloud()
        run = self.prepare()
        self.key.reset_mock()
        process_run(run.pk)
        self.key.assert_called_once_with()
        self.assertEqual(self.client.call_args.kwargs["api_key"], "fictional-api-credential")

    def test_model_error_is_safe_and_not_retried(self):
        run = self.prepare()
        self.client.side_effect = analysis.AIError("timeout")
        process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertIn("超时", run.error)
        self.assertEqual(self.client.call_count, 1)

    def test_unexpected_failure_does_not_expose_private_detail(self):
        run = self.prepare()
        self.client.side_effect = RuntimeError("PRIVATE_RAW_EXCEPTION")
        process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertNotIn("PRIVATE_RAW_EXCEPTION", run.error)

    def test_request_requires_current_manager_and_valid_question(self):
        self.grant()
        with self.assertRaises(PermissionDenied):
            request_analysis(self.ordinary, "查看资料", event=self.event, background=False)
        for value in (None, "", "a" * 1001):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValidationError):
                request_analysis(self.manager, value, event=self.event, background=False)
        self.client.assert_not_called()

    def dated_material(self, recorded_date, title):
        event = Activity.objects.create(
            token=uuid.uuid4(), creator=self.manager, title=title, recorded_date=recorded_date
        )
        batch = ImportBatch.objects.create(event=event, actor=self.manager)
        material = Material.objects.create(
            event=event, batch=batch, original_name=f"{title}.txt", file="archive/fictional-date-source.txt",
            sha256=hashlib.sha256(title.encode()).hexdigest(), size=5, parse_status="parsed",
            text=f"日期范围测试：{title}。",
        )
        grant_permission(
            self.manager, event, [str(material.pk)],
            expected_config_fingerprint=configuration_hash(self.config),
        )
        return material

    def prepare_dated(self, *, start=date(2026, 9, 1), end=date(2026, 9, 30), token=None):
        self.event.recorded_date = date(2026, 9, 15)
        self.event.save(update_fields=["recorded_date"])
        self.grant()
        return request_analysis(
            self.manager, "看看本期活动。", event=self.event, start=start, end=end,
            token=token, background=False,
        )

    def test_date_scope_excludes_unknown_and_outside_events_from_payload(self):
        self.event.recorded_date = date(2026, 9, 15)
        self.event.save(update_fields=["recorded_date"])
        self.grant()
        unknown = self.dated_material(None, "日期未知资料")
        older = self.dated_material(date(2026, 8, 31), "范围之外资料")
        run = request_analysis(
            self.manager, "本月有哪些活动？", start=date(2026, 9, 1), end=date(2026, 9, 30), background=False
        )
        self.assertEqual([source["id"] for source in run.sources], [str(self.selected.pk)])
        process_run(run.pk)
        payload = self.client.call_args.kwargs
        self.assertEqual([source["id"] for source in payload["sources"]], [str(self.selected.pk)])
        self.assertNotIn(str(unknown.pk), json.dumps(payload))
        self.assertNotIn(str(older.pk), json.dumps(payload))
        self.assertIn("资料所属活动的记录日期", payload["question"])
        self.assertIn("不能据此声称", payload["question"])
        self.assertIn("2026-09-01", payload["question"])
        run.refresh_from_db()
        self.assertEqual(run.status, "complete")
        self.assertEqual(run.start_date, date(2026, 9, 1))
        self.assertEqual(run.end_date, date(2026, 9, 30))

    def test_one_sided_date_bounds_exclude_unknowns_and_include_boundary(self):
        september = self.dated_material(date(2026, 9, 1), "九月一日资料")
        august = self.dated_material(date(2026, 8, 31), "八月末资料")
        self.dated_material(None, "没有日期资料")
        after = collect_sources(self.config, start=date(2026, 9, 1))
        before = collect_sources(self.config, end=date(2026, 8, 31))
        self.assertEqual({source["id"] for source in after}, {str(september.pk)})
        self.assertEqual({source["id"] for source in before}, {str(august.pk)})

    def test_specific_event_outside_range_or_undated_cannot_be_sent(self):
        self.grant()
        for actual_date in (None, date(2026, 8, 31)):
            with self.subTest(actual_date=actual_date):
                Activity.objects.filter(pk=self.event.pk).update(recorded_date=actual_date)
                with self.assertRaises(ValidationError):
                    request_analysis(
                        self.manager, "本期分析", event=self.event,
                        start=date(2026, 9, 1), end=date(2026, 9, 30), background=False,
                    )
        self.client.assert_not_called()
        self.assertFalse(AnalysisRun.objects.exists())

    def test_same_date_range_reuses_complete_result_but_changed_range_does_not(self):
        first = self.prepare_dated()
        process_run(first.pk)
        identical = request_analysis(
            self.manager, first.question, event=self.event,
            start=date(2026, 9, 1), end=date(2026, 9, 30), background=False,
        )
        changed = request_analysis(
            self.manager, first.question, event=self.event,
            start=date(2026, 9, 10), end=date(2026, 9, 20), background=False,
        )
        self.assertEqual(first.pk, identical.pk)
        self.assertNotEqual(first.pk, changed.pk)
        self.assertNotEqual(first.fingerprint, changed.fingerprint)
        self.assertEqual(first.sources, changed.sources)

    def test_explicit_token_cannot_be_reused_for_another_date_range(self):
        token = uuid.uuid4()
        first = self.prepare_dated(token=token)
        with self.assertRaises(ValidationError):
            request_analysis(
                self.manager, first.question, event=self.event, token=token,
                start=date(2026, 9, 10), end=date(2026, 9, 20), background=False,
            )

    def test_date_correction_invalidates_completed_analysis(self):
        run = self.prepare_dated()
        process_run(run.pk)
        run.refresh_from_db()
        self.assertTrue(run_current(run))
        Activity.objects.filter(pk=self.event.pk).update(recorded_date=date(2026, 10, 1))
        self.assertFalse(run_current(run))

    def test_date_correction_before_dispatch_prevents_call(self):
        run = self.prepare_dated()
        Activity.objects.filter(pk=self.event.pk).update(recorded_date=None)
        process_run(run.pk)
        self.client.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")

    def test_date_correction_inflight_discards_result(self):
        run = self.prepare_dated()

        def correct_date(**kwargs):
            Activity.objects.filter(pk=self.event.pk).update(recorded_date=date(2026, 8, 31))
            return {"items": []}

        self.client.side_effect = correct_date
        process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "stale")
        self.assertEqual(run.result, {})

    def test_formerly_unknown_date_entering_scope_invalidates_old_result(self):
        self.event.recorded_date = date(2026, 9, 15)
        self.event.save(update_fields=["recorded_date"])
        self.grant()
        unknown = self.dated_material(None, "后来确认日期资料")
        run = request_analysis(
            self.manager, "本期整体分析", start=date(2026, 9, 1), end=date(2026, 9, 30), background=False
        )
        self.assertTrue(run_current(run))
        Activity.objects.filter(pk=unknown.event_id).update(recorded_date=date(2026, 9, 20))
        self.assertFalse(run_current(run))

    def test_individual_sources_must_still_belong_to_current_date_scope(self):
        run = self.prepare_dated()
        Activity.objects.filter(pk=self.event.pk).update(recorded_date=date(2026, 8, 1))
        run.fingerprint = scope_fingerprint(self.event, start=run.start_date, end=run.end_date)
        self.assertFalse(run_current(run))

    def test_invalid_or_reversed_date_range_is_rejected_before_call(self):
        self.grant()
        for start, end in (
            (date(2026, 10, 1), date(2026, 9, 1)),
            ("not-a-date", None),
            ("20260901", None),
            (datetime(2026, 9, 1, 1, 0), None),
            (None, 20260930),
        ):
            with self.subTest(start=start, end=end), self.assertRaises(ValidationError):
                request_analysis(self.manager, "本期分析", start=start, end=end, background=False)
        self.client.assert_not_called()

    def test_iso_date_inputs_are_normalized_without_changing_scope(self):
        run = self.prepare_dated(start="2026-09-01", end="2026-09-30")
        self.assertEqual(run.start_date, date(2026, 9, 1))
        self.assertEqual(run.end_date, date(2026, 9, 30))
        self.assertTrue(run_current(run))
