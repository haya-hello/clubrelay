"""在独立测试数据库验证人员身份和证据口径。 / Verify identity and evidence semantics in isolated test databases."""

import uuid
from datetime import date

from django.contrib.auth.models import AnonymousUser, Group, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from activities.models import Activity
from .models import Alias, AnalysisRun, Audit, Evidence, ImportBatch, Material, Person
from .people import active_evidence, import_roster, merge_evidence, save_evidence
from .security import MANAGER_GROUP


class PeopleFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user("synthetic_roster_manager", password="local-test-only")
        cls.ordinary = User.objects.create_user("synthetic_roster_member", password="local-test-only")
        cls.manager.groups.add(Group.objects.get_or_create(name=MANAGER_GROUP)[0])
        cls.event = Activity.objects.create(token=uuid.uuid4(), creator=cls.manager, title="虚构工作坊", objective="", audience="", constraints="")
        cls.other_event = Activity.objects.create(token=uuid.uuid4(), creator=cls.manager, title="虚构分享会", objective="", audience="", constraints="")
        cls.batch = ImportBatch.objects.create(event=cls.event, actor=cls.manager)
        cls.material = Material.objects.create(
            event=cls.event, batch=cls.batch, file="synthetic/not-written.txt", original_name="虚构资料.txt",
            sha256="0" * 64, size=30, parse_status="parsed", text="虚构甲完成了海报。虚构乙协助整理设备。",
            segments=[{"anchor": "第 1 行", "text": "虚构甲完成了海报。虚构乙协助整理设备。"}],
            tables=[{"name": "虚构名册", "columns": ["member_id", "member_name", "岗位", "兴趣", "昵称", "入社日期"], "rows": []}],
        )
        cls.person = Person.objects.create(external_id="synthetic-001", display_name="虚构甲")
        cls.other_person = Person.objects.create(external_id="synthetic-002", display_name="虚构乙")

    def roster(self, rows, **changes):
        self.material.tables[0]["rows"] = rows
        for key, value in changes.items():
            setattr(self.material, key, value)
        self.material.save()
        return self.material

    def mapping(self, **changes):
        result = {"id": "member_id", "name": "member_name", "role": "岗位", "interests": "兴趣", "aliases": "昵称", "joined_on": "入社日期"}
        result.update(changes)
        return result

    def evidence(self, **changes):
        data = {
            "member": self.person, "event": self.event, "title": "完成海报", "kind": "delivery",
            "confidence": "candidate", "occurred_on": date(2026, 9, 1), "material": self.material,
            "anchor": "第 1 行", "quote": "虚构甲完成了海报。", "note": "负责人观察待核验",
        }
        data.update(changes)
        return save_evidence(self.manager, data)

    def analysis_run(self, **changes):
        data = {"actor": self.manager, "question": "虚构分析", "fingerprint": "0" * 64, "config_fingerprint": "1" * 64, "status": "complete"}
        data.update(changes)
        return AnalysisRun.objects.create(**data)


class RosterImportTests(PeopleFixture):
    def test_import_requires_manager(self):
        for user in (self.ordinary, AnonymousUser()):
            with self.subTest(user=user), self.assertRaises(PermissionDenied):
                import_roster(user, self.material, self.mapping())

    def test_import_rechecks_source_state_from_database(self):
        Material.objects.filter(pk=self.material.pk).update(excluded=True)
        with self.assertRaises(ValidationError):
            import_roster(self.manager, self.material, self.mapping())

    def test_unparsed_source_is_not_roster(self):
        self.roster([], parse_status="unsupported")
        with self.assertRaises(ValidationError):
            import_roster(self.manager, self.material, self.mapping())

    def test_id_and_name_mapping_are_required(self):
        for mapping in ({"name": "member_name"}, {"id": "member_id"}, {"id": "missing", "name": "member_name"}):
            with self.subTest(mapping=mapping), self.assertRaises(ValidationError):
                import_roster(self.manager, self.material, mapping)

    def test_chinese_mapping_is_not_hardcoded(self):
        self.material.tables = [{"name": "名册", "columns": ["稳定编号", "姓名"], "rows": [["new-03", "虚构丙"]]}]
        self.material.save()
        result = import_roster(self.manager, self.material, {"id": "稳定编号", "name": "姓名"})
        self.assertEqual(result, {"created": 1, "updated": 0, "issues": []})
        self.assertEqual(Person.objects.get(external_id="new-03").display_name, "虚构丙")

    def test_import_all_members_even_without_messages(self):
        self.roster([["new-03", "虚构丙", "", "", "", ""], ["new-04", "虚构丁", "", "", "", ""]])
        result = import_roster(self.manager, self.material, self.mapping())
        self.assertEqual(result["created"], 2)
        self.assertFalse(Person.objects.get(external_id="new-04").evidence.exists())

    def test_reimport_is_idempotent_and_preserves_original(self):
        self.roster([["new-03", "虚构丙", "设计", "视觉", "小丙，设计丙,小丙", "2026-09-01"]])
        original = (self.material.text, self.material.tables, self.material.file.name, self.material.sha256)
        first = import_roster(self.manager, self.material, self.mapping())
        second = import_roster(self.manager, self.material, self.mapping())
        self.assertEqual((first["created"], second["created"], second["updated"]), (1, 0, 0))
        self.assertEqual(Alias.objects.filter(member__external_id="new-03").count(), 2)
        self.material.refresh_from_db()
        self.assertEqual((self.material.text, self.material.tables, self.material.file.name, self.material.sha256), original)

    def test_existing_member_updates_only_mapped_fields(self):
        self.person.role = "原岗位"
        self.person.interests = "原兴趣"
        self.person.active = False
        self.person.save()
        self.roster([[self.person.external_id, self.person.display_name, "新岗位"]])
        result = import_roster(self.manager, self.material, {"id": "member_id", "name": "member_name", "role": "岗位"})
        self.person.refresh_from_db()
        self.assertEqual(result["updated"], 1)
        self.assertEqual(self.person.role, "新岗位")
        self.assertEqual(self.person.interests, "原兴趣")
        self.assertFalse(self.person.active)

    def test_same_id_different_name_never_overwrites(self):
        self.roster([[self.person.external_id, "另一个同编号的人", "", "", "", ""]])
        result = import_roster(self.manager, self.material, self.mapping())
        self.person.refresh_from_db()
        self.assertEqual(self.person.display_name, "虚构甲")
        self.assertEqual((result["created"], result["updated"]), (0, 0))
        self.assertIn("姓名不同", result["issues"][0])

    def test_conflicting_batch_ids_block_every_conflicting_row(self):
        self.roster([["new-03", "虚构丙", "设计"], ["new-03", "虚构丙", "技术"], ["new-04", "虚构丁", ""]])
        result = import_roster(self.manager, self.material, self.mapping())
        self.assertFalse(Person.objects.filter(external_id="new-03").exists())
        self.assertEqual(result["created"], 1)
        self.assertEqual(len(result["issues"]), 2)

    def test_exact_duplicate_rows_only_import_once(self):
        self.roster([["new-03", "虚构丙"], ["new-03", "虚构丙"]])
        result = import_roster(self.manager, self.material, self.mapping())
        self.assertEqual(result["created"], 1)
        self.assertIn("重复", result["issues"][0])

    def test_missing_id_and_bad_date_are_row_issues(self):
        self.roster([["", "未分配编号"], ["new-03", "虚构丙", "", "", "", "09/01/2026"], ["new-04", "虚构丁", "", "", "", "2026-02-30"]])
        result = import_roster(self.manager, self.material, self.mapping())
        self.assertEqual(result["created"], 0)
        self.assertEqual(len(result["issues"]), 3)

    def test_aliases_can_belong_to_different_people(self):
        self.roster([["new-03", "虚构丙", "", "", "同名昵称", ""], ["new-04", "虚构丁", "", "", "同名昵称", ""]])
        result = import_roster(self.manager, self.material, self.mapping())
        self.assertEqual(result["created"], 2)
        self.assertEqual(Alias.objects.filter(value="同名昵称").count(), 2)

    def test_alias_import_is_additive(self):
        Alias.objects.create(member=self.person, value="旧昵称")
        self.roster([[self.person.external_id, self.person.display_name, "", "", "新昵称", ""]])
        import_roster(self.manager, self.material, self.mapping())
        self.assertSetEqual(set(self.person.aliases.values_list("value", flat=True)), {"旧昵称", "新昵称"})

    def test_import_invalidates_analysis_and_records_audit(self):
        run = self.analysis_run(event=self.other_event)
        self.roster([["new-03", "虚构丙"]])
        import_roster(self.manager, self.material, self.mapping())
        run.refresh_from_db()
        self.assertEqual(run.status, "stale")
        self.assertTrue(Audit.objects.filter(action="import_roster", object_id=str(self.material.pk)).exists())

    def test_table_index_and_duplicate_headers_are_rejected(self):
        with self.assertRaises(ValidationError):
            import_roster(self.manager, self.material, self.mapping(), table_index=-1)
        self.material.tables[0]["columns"].append("member_name")
        self.material.save()
        with self.assertRaises(ValidationError):
            import_roster(self.manager, self.material, self.mapping())


class EvidenceServiceTests(PeopleFixture):
    def test_mutations_require_active_manager(self):
        with self.assertRaises(PermissionDenied):
            save_evidence(self.ordinary, {})
        self.manager.is_active = False
        self.manager.save()
        with self.assertRaises(PermissionDenied):
            save_evidence(self.manager, {})

    def test_source_event_and_quote_are_rechecked(self):
        for changes in ({"event": self.other_event}, {"quote": "凭空猜测已经完成"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.evidence(**changes)

    def test_excluded_or_failed_source_is_rejected(self):
        for state in ({"excluded": True}, {"excluded": False, "parse_status": "failed"}):
            Material.objects.filter(pk=self.material.pk).update(**state)
            with self.subTest(state=state), self.assertRaises(ValidationError):
                self.evidence()

    def test_repeated_quote_or_confidence_does_not_duplicate_event(self):
        original = self.evidence()
        duplicate = self.evidence(confidence="verified", quote="完成了海报", note="再次提及")
        self.assertEqual(original.pk, duplicate.pk)
        duplicate.refresh_from_db()
        self.assertEqual(duplicate.confidence, "candidate")
        self.assertEqual(Evidence.objects.count(), 1)

    def test_title_normalization_keeps_one_record(self):
        one = self.evidence(title="制作 AI 海报")
        two = self.evidence(title=" 制作   ai 海报 ")
        self.assertEqual(one.pk, two.pk)

    def test_manual_evidence_requires_basis_and_survives_material_exclusion(self):
        record = self.evidence(material=None, quote="负责人现场核对了海报交付。")
        Material.objects.filter(pk=self.material.pk).update(excluded=True)
        self.assertIn(record, active_evidence())
        with self.assertRaises(ValidationError):
            self.evidence(material=None, quote="", title="没有依据")

    def test_active_evidence_excludes_invalid_sources_and_duplicates(self):
        source = self.evidence(title="原文件海报")
        manual = self.evidence(material=None, quote="负责人观察", title="人工记录")
        merge_evidence(self.manager, source, manual)
        self.assertEqual(list(active_evidence()), [Evidence.objects.get(pk=manual.pk)])
        other = self.evidence(title="另一份文件记录")
        Material.objects.filter(pk=self.material.pk).update(excluded=True)
        self.assertNotIn(other, active_evidence())
        Material.objects.filter(pk=self.material.pk).update(excluded=False, parse_status="failed")
        self.assertNotIn(other, active_evidence())

    def test_edit_requires_current_version(self):
        record = self.evidence()
        for version in (None, 0, "bogus", True):
            with self.subTest(version=version), self.assertRaises(ValidationError):
                save_evidence(self.manager, {"version": version, "note": "修改"}, instance=record)
        updated = save_evidence(self.manager, {"version": 1, "note": "核对后补充"}, instance=record)
        self.assertEqual(updated.version, 2)
        with self.assertRaises(ValidationError):
            save_evidence(self.manager, {"version": 1, "note": "旧页面"}, instance=record)
        updated.refresh_from_db()
        self.assertEqual(updated.note, "核对后补充")

    def test_explicit_edit_can_verify_but_repeat_creation_cannot(self):
        record = self.evidence()
        updated = save_evidence(self.manager, {"version": 1, "confidence": "verified"}, instance=record)
        self.assertEqual(updated.confidence, "verified")
        self.assertEqual(updated.version, 2)

    def test_edit_collision_requires_explicit_merge(self):
        one = self.evidence(title="海报一")
        two = self.evidence(title="海报二")
        with self.assertRaises(ValidationError):
            save_evidence(self.manager, {"version": 1, "title": "海报一"}, instance=two)
        two.refresh_from_db()
        self.assertEqual(two.title, "海报二")
        self.assertEqual(Evidence.objects.count(), 2)

    def test_kind_and_confidence_choices_are_validated(self):
        for changes in ({"kind": "moral_score"}, {"confidence": "ai_certified"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.evidence(**changes)

    def test_mutation_invalidates_relevant_runs_not_unrelated_runs(self):
        record = self.evidence()
        related = [self.analysis_run(event=self.event), self.analysis_run(member=self.person), self.analysis_run(), self.analysis_run(event=self.event, status="pending")]
        unrelated = self.analysis_run(event=self.other_event, member=self.other_person)
        failed = self.analysis_run(event=self.event, status="failed")
        save_evidence(self.manager, {"version": record.version, "note": "修正观察"}, instance=record)
        for run in related:
            run.refresh_from_db()
            self.assertEqual(run.status, "stale")
        unrelated.refresh_from_db()
        failed.refresh_from_db()
        self.assertEqual(unrelated.status, "complete")
        self.assertEqual(failed.status, "failed")

    def test_message_kind_is_not_turned_into_delivery(self):
        communication = self.evidence(kind="communication", title="沟通记录")
        self.assertEqual(communication.kind, "communication")
        self.assertEqual(active_evidence().filter(kind="delivery").count(), 0)


class EvidenceMergeTests(PeopleFixture):
    def test_merge_keeps_sources_without_confidence_upgrade(self):
        source = self.evidence(title="来源记录", confidence="verified")
        target = self.evidence(title="主记录", material=None, quote="线下观察", confidence="candidate")
        target = merge_evidence(self.manager, source, target)
        source.refresh_from_db()
        self.assertEqual(source.duplicate_of_id, target.pk)
        self.assertEqual(source.material_id, self.material.pk)
        self.assertEqual(source.quote, "虚构甲完成了海报。")
        self.assertEqual(target.confidence, "candidate")
        self.assertEqual(Evidence.objects.count(), 2)
        self.assertEqual(active_evidence().count(), 1)
        self.assertEqual((source.version, target.version), (2, 2))

    def test_merge_rejects_other_member_self_and_non_root_target(self):
        source = self.evidence(title="来源记录")
        target = self.evidence(title="主记录")
        outsider = self.evidence(member=self.other_person, title="另一位成员")
        for pair in ((source, source), (source, outsider)):
            with self.subTest(pair=pair), self.assertRaises(ValidationError):
                merge_evidence(self.manager, *pair)
        target = merge_evidence(self.manager, source, target)
        source.refresh_from_db()
        with self.assertRaises(ValidationError):
            merge_evidence(self.manager, target, source)

    def test_merge_requires_manager_and_fresh_versions(self):
        source = self.evidence(title="来源记录")
        target = self.evidence(title="主记录")
        with self.assertRaises(PermissionDenied):
            merge_evidence(self.ordinary, source, target)
        save_evidence(self.manager, {"version": 1, "note": "已变化"}, instance=source)
        with self.assertRaises(ValidationError):
            merge_evidence(self.manager, source, target)
        source.refresh_from_db()
        self.assertIsNone(source.duplicate_of_id)

    def test_merging_root_flattens_existing_sources(self):
        leaf = self.evidence(title="最早来源")
        source = self.evidence(title="中间主记录")
        target = self.evidence(title="最终主记录")
        source = merge_evidence(self.manager, leaf, source)
        target = merge_evidence(self.manager, source, target)
        leaf.refresh_from_db()
        source.refresh_from_db()
        self.assertEqual(leaf.duplicate_of_id, target.pk)
        self.assertEqual(source.duplicate_of_id, target.pk)
        self.assertEqual(active_evidence().count(), 1)

    def test_reimporting_merged_key_returns_root(self):
        source = self.evidence(title="来源记录")
        target = self.evidence(title="主记录")
        target = merge_evidence(self.manager, source, target)
        repeated = self.evidence(title="来源记录", confidence="verified")
        self.assertEqual(repeated.pk, target.pk)
        self.assertEqual(repeated.confidence, "candidate")
        self.assertEqual(active_evidence().count(), 1)

    def test_merged_source_and_root_identity_cannot_be_rewritten(self):
        source = self.evidence(title="来源记录")
        target = self.evidence(title="主记录")
        target = merge_evidence(self.manager, source, target)
        source.refresh_from_db()
        with self.assertRaises(ValidationError):
            save_evidence(self.manager, {"version": source.version, "title": "修改已合并项"}, instance=source)
        with self.assertRaises(ValidationError):
            save_evidence(self.manager, {"version": target.version, "member": self.other_person}, instance=target)

    def test_merge_invalidates_both_events_and_member(self):
        source = self.evidence(title="来源记录")
        target = self.evidence(title="主记录", event=self.other_event, material=None, quote="负责人核对")
        runs = [self.analysis_run(event=self.event), self.analysis_run(event=self.other_event), self.analysis_run(member=self.person), self.analysis_run()]
        merge_evidence(self.manager, source, target)
        for run in runs:
            run.refresh_from_db()
            self.assertEqual(run.status, "stale")

    def test_repeated_merge_with_current_objects_is_idempotent(self):
        source = self.evidence(title="来源记录")
        target = self.evidence(title="主记录")
        target = merge_evidence(self.manager, source, target)
        source.refresh_from_db()
        repeated = merge_evidence(self.manager, source, target)
        self.assertEqual(repeated.version, target.version)
        self.assertEqual(Audit.objects.filter(action="evidence_merge").count(), 1)
