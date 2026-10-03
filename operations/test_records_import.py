"""验证结构化批量证据导入，全部使用虚构资料。 / Test structured evidence imports with synthetic records only."""

import uuid
from datetime import date
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser, Group, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from activities.models import Activity
from .models import AnalysisRun, Audit, Evidence, ImportBatch, Material, Person
from .people import merge_evidence, save_evidence
from .records_import import import_observations
from .security import MANAGER_GROUP


class ObservationImportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user("synthetic_records_manager", password="local-test-only")
        cls.member_user = User.objects.create_user("synthetic_records_ordinary", password="local-test-only")
        cls.manager.groups.add(Group.objects.get_or_create(name=MANAGER_GROUP)[0])
        cls.event = Activity.objects.create(token=uuid.uuid4(), creator=cls.manager, title="虚构工作坊", objective="", audience="", constraints="")
        cls.batch = ImportBatch.objects.create(event=cls.event, actor=cls.manager)
        cls.person = Person.objects.create(external_id="roster-a", display_name="虚构甲")
        cls.second = Person.objects.create(external_id="roster-b", display_name="虚构乙")
        cls.material = Material.objects.create(
            event=cls.event, batch=cls.batch, file="synthetic/not-written.csv", original_name="虚构贡献记录.csv",
            sha256="a" * 64, size=120, parse_status="parsed", text="甲完成海报。乙整理设备。甲帮助同伴检查作品。",
            segments=[{"anchor": "CSV · 第 2 行", "text": "甲完成海报。"}, {"anchor": "CSV · 第 3 行", "text": "乙整理设备。"}],
            tables=[{"name": "虚构记录", "columns": ["稳定编号", "事项", "日期", "引用"], "rows": [["roster-a", "海报交付", "2026-09-01", "甲完成海报。"]]}],
        )

    def mapping(self, **changes):
        value = {"person_id": "稳定编号", "title": "事项", "occurred_on": "日期", "quote": "引用"}
        value.update(changes)
        return value

    def rows(self, rows):
        self.material.tables[0]["rows"] = rows
        self.material.save(update_fields=["tables"])
        return self.material

    def call(self, **changes):
        arguments = {"user": self.manager, "material": self.material, "mapping": self.mapping(), "kind": "delivery"}
        arguments.update(changes)
        return import_observations(**arguments)

    def test_defaults_to_self_report_and_keeps_source(self):
        result = self.call()
        self.assertEqual(result, {"created": 1, "duplicates": 0, "issues": []})
        evidence = Evidence.objects.get()
        self.assertEqual(evidence.member, self.person)
        self.assertEqual(evidence.event, self.event)
        self.assertEqual(evidence.material, self.material)
        self.assertEqual(evidence.confidence, "self_report")
        self.assertEqual(evidence.occurred_on, date(2026, 9, 1))
        self.assertEqual(evidence.anchor, "CSV · 第 2 行")
        self.assertIn("不是 AI", evidence.note)

    def test_only_active_manager_can_import(self):
        for user in (self.member_user, AnonymousUser()):
            with self.subTest(user=user), self.assertRaises(PermissionDenied):
                self.call(user=user)
        self.manager.is_active = False
        self.manager.save()
        with self.assertRaises(PermissionDenied):
            self.call()

    def test_source_status_is_reloaded_not_trusted_from_argument(self):
        Material.objects.filter(pk=self.material.pk).update(excluded=True)
        with self.assertRaises(ValidationError):
            self.call()
        Material.objects.filter(pk=self.material.pk).update(excluded=False, parse_status="failed")
        with self.assertRaises(ValidationError):
            self.call()
        self.assertEqual(Evidence.objects.count(), 0)

    def test_required_mapping_columns_and_unique_headers(self):
        for field in ("person_id", "title", "quote"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.call(mapping=self.mapping(**{field: ""}))
        with self.assertRaises(ValidationError):
            self.call(mapping=self.mapping(title="missing"))
        self.material.tables[0]["columns"].append("事项")
        self.material.save()
        with self.assertRaises(ValidationError):
            self.call()

    def test_invalid_kind_and_confidence_are_rejected_before_writes(self):
        for values in ({"kind": "message_score"}, {"confidence": "ai_verified"}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.call(**values)
        self.assertFalse(Evidence.objects.exists())

    def test_verified_requires_explicit_boolean_acknowledgement(self):
        for ack in (False, None, "yes", 1):
            with self.subTest(ack=ack), self.assertRaises(ValidationError):
                self.call(confidence="verified", verified_ack=ack)
        result = self.call(confidence="verified", verified_ack=True)
        self.assertEqual(result["created"], 1)
        self.assertEqual(Evidence.objects.get().confidence, "verified")

    def test_unknown_id_is_not_guessed_or_created(self):
        self.rows([["unknown-id", "海报交付", "2026-09-01", "甲完成海报。"]])
        result = self.call()
        self.assertEqual(result["created"], 0)
        self.assertEqual(Person.objects.count(), 2)
        self.assertIn("未自动创建", result["issues"][0])

    def test_same_name_is_not_used_as_external_id(self):
        self.rows([["虚构甲", "海报交付", "2026-09-01", "甲完成海报。"]])
        self.assertEqual(self.call()["created"], 0)

    def test_invalid_rows_do_not_undo_valid_rows(self):
        self.rows([
            ["roster-a", "海报交付", "2026-09-01", "甲完成海报。"],
            ["missing", "未知成员", "2026-09-01", "甲完成海报。"],
            ["roster-b", "设备整理", "09/01/2026", "乙整理设备。"],
            ["roster-b", "设备整理", "2026-09-01", "乙整理设备。"],
        ])
        result = self.call()
        self.assertEqual((result["created"], len(result["issues"])), (2, 2))
        self.assertEqual(Evidence.objects.count(), 2)

    def test_date_mapping_can_be_omitted_without_using_upload_date(self):
        result = self.call(mapping=self.mapping(occurred_on=""))
        self.assertEqual(result["created"], 1)
        self.assertIsNone(Evidence.objects.get().occurred_on)

    def test_blank_date_stays_unknown(self):
        self.rows([["roster-a", "海报交付", "", "甲完成海报。"]])
        self.call()
        self.assertIsNone(Evidence.objects.get().occurred_on)

    def test_invalid_iso_date_reports_issue(self):
        self.rows([["roster-a", "海报交付", "2026-02-30", "甲完成海报。"]])
        result = self.call()
        self.assertEqual(result["created"], 0)
        self.assertIn("有效日期", result["issues"][0])

    def test_quote_must_be_verbatim(self):
        self.rows([["roster-a", "海报交付", "2026-09-01", "甲一定特别有能力"]])
        result = self.call()
        self.assertEqual(result["created"], 0)
        self.assertIn("逐字", result["issues"][0])

    def test_title_and_quote_are_required_and_have_model_limits(self):
        long_quote = "引" * 3001
        self.material.text += long_quote
        self.material.save(update_fields=["text"])
        self.rows([
            ["roster-a", "", "", "甲完成海报。"],
            ["roster-a", "标题" * 126, "", "甲完成海报。"],
            ["roster-a", "无引用", "", ""],
            ["roster-a", "超长引用", "", long_quote],
        ])
        result = self.call()
        self.assertEqual(result["created"], 0)
        self.assertEqual(len(result["issues"]), 4)

    def test_reimport_does_not_duplicate_or_upgrade_confidence(self):
        first = self.call(confidence="candidate")
        second = self.call(confidence="verified", verified_ack=True)
        self.assertEqual((first["created"], second["created"], second["duplicates"]), (1, 0, 1))
        self.assertEqual(Evidence.objects.get().confidence, "candidate")
        self.assertEqual(Evidence.objects.count(), 1)

    def test_same_batch_repetitions_are_counted_as_duplicates(self):
        row = ["roster-a", "海报交付", "2026-09-01", "甲完成海报。"]
        self.rows([row, row, row])
        result = self.call()
        self.assertEqual(result, {"created": 1, "duplicates": 2, "issues": []})

    def test_duplicate_with_normalized_title_uses_shared_service(self):
        self.rows([["roster-a", "制作 AI 海报", "", "甲完成海报。"], ["roster-a", "制作   ai 海报", "", "甲完成海报。"]])
        result = self.call()
        self.assertEqual((result["created"], result["duplicates"]), (1, 1))

    def test_merged_source_reimport_keeps_one_root(self):
        self.call()
        source = Evidence.objects.get()
        target = save_evidence(self.manager, {
            "member": self.person, "event": self.event, "material": None,
            "title": "已归并的海报工作", "kind": "delivery", "confidence": "candidate",
            "quote": "负责人线下观察", "occurred_on": None,
        })
        merge_evidence(self.manager, source, target)
        result = self.call(confidence="verified", verified_ack=True)
        self.assertEqual((result["created"], result["duplicates"]), (0, 1))
        self.assertEqual(Evidence.objects.filter(duplicate_of__isnull=True).count(), 1)
        target.refresh_from_db()
        self.assertEqual(target.confidence, "candidate")

    def test_communication_dimension_never_becomes_delivery(self):
        self.call(kind="communication")
        self.assertEqual(Evidence.objects.get().kind, "communication")
        self.assertFalse(Evidence.objects.filter(kind="delivery").exists())

    def test_changes_invalidate_running_analysis_through_service(self):
        run = AnalysisRun.objects.create(actor=self.manager, event=self.event, question="虚构问题", status="running", fingerprint="a" * 64, config_fingerprint="b" * 64)
        self.call()
        run.refresh_from_db()
        self.assertEqual(run.status, "stale")

    def test_no_source_changes_or_network_calls(self):
        original = (self.material.file.name, self.material.sha256, self.material.text, self.material.tables)
        with patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden")):
            self.call()
        self.material.refresh_from_db()
        self.assertEqual((self.material.file.name, self.material.sha256, self.material.text, self.material.tables), original)
        audit = Audit.objects.get(action="import_observations")
        self.assertIn("非 AI", audit.detail)

    def test_table_selection_and_blank_rows(self):
        self.material.tables.append({"name": "第二张", "columns": ["稳定编号", "事项", "日期", "引用"], "rows": [[], ["", "", "", ""], ["roster-b", "设备整理", "", "乙整理设备。"]]})
        self.material.save()
        result = self.call(table_index=1)
        self.assertEqual(result, {"created": 1, "duplicates": 0, "issues": []})
        self.assertEqual(Evidence.objects.get().member, self.second)
        with self.assertRaises(ValidationError):
            self.call(table_index=-1)

    def test_malformed_rows_are_reported(self):
        self.rows([{"id": "roster-a"}, ["", "海报交付", "", "甲完成海报。"]])
        result = self.call()
        self.assertEqual(result["created"], 0)
        self.assertEqual(len(result["issues"]), 2)
