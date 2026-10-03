import uuid
from datetime import timedelta
from django.contrib.auth.models import User, Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from knowledge.models import Entry
from knowledge.services import REVIEWER_GROUP, review_entry, searchable_entries
from knowledge.answers import collect_references, resolve_reference
from .models import Retrospective, KnowledgeDerivation
from .services import create_activity, publish_task, change_task
from .deliveries import submit_result, review_result
from .retros import create_retro, save_retro, confirm_retro, withdraw_retro, change_stage, export_knowledge, activity_snapshot, source_current

class RetroWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.lead=User.objects.create_user("retro_lead",first_name="负责人甲")
        cls.lead2=User.objects.create_user("retro_lead2",first_name="负责人乙")
        cls.member=User.objects.create_user("retro_member",first_name="成员")
        cls.outside=User.objects.create_user("retro_outside",first_name="非参与成员")
        group=Group.objects.create(name=REVIEWER_GROUP)
        cls.lead.groups.add(group)
        cls.lead2.groups.add(group)
        cls.activity=create_activity(cls.lead,{"token":uuid.uuid4(),"title":"虚构复盘活动","kind":"activity","objective":"学会做一个原型","audience":"入门成员","scheduled_at":timezone.now()+timedelta(days=3),"constraints":"场地待确认","plan":"先演示，再实践","participants":[cls.member]})
        task=publish_task(cls.lead,cls.activity,{"token":uuid.uuid4(),"title":"投影检查","objective":"验证现场展示","deliverable":"清单","acceptance":"记录成功与失败","due_at":timezone.now()+timedelta(days=2),"assignee":cls.member,"reference":None})
        cls.task=change_task(cls.member,task.pk,1,"accept")

    def notes(self,**changes):
        data={"facts":"现场完成了演示验证。","differences":"准备比预计更久。","hypotheses":"可能没有提前试连，尚待核实。","improvements":"投影故障经验：活动前一天检查接口并准备离线演示。","applicability":"仅适用于小型演示活动。"}
        data.update(changes)
        return data

    def draft(self):
        return create_retro(self.lead,self.activity.pk)

    def confirm(self,retro=None):
        retro=retro or self.draft()
        retro=save_retro(self.lead,retro.pk,retro.revision,self.notes())
        self.activity.refresh_from_db()
        if self.activity.stage in ("planning","active"):
            change_stage(self.lead,self.activity.pk,self.activity.stage_version,"wrapup","开始收尾")
        return confirm_retro(self.lead,retro.pk,retro.revision,True)

    def card(self,retro=None,approve=False):
        retro=retro or self.confirm()
        entry=export_knowledge(self.lead,retro.pk,retro.revision,{"title":"投影故障经验","body":retro.improvements,"applicability":"小型演示","share_ack":True})
        if approve:
            entry=review_entry(self.lead2,entry.pk,entry.version,"approve","来源和边界已核对")
        return retro,entry

    def accepted_result(self):
        self.task.refresh_from_db()
        result=submit_result(self.member,self.task.pk,{"token":uuid.uuid4(),"version":self.task.version,"summary":"SECRET_SUMMARY","result_text":"SECRET_BODY","result_url":"","method":"SECRET_METHOD","contribution":"SECRET_PERSONAL_WORK","attachment":None})
        self.task.refresh_from_db()
        return review_result(self.lead,result.pk,self.task.version,"approve","SECRET_REVIEW",True,True)

    def test_draft_creation_is_idempotent(self):
        first=self.draft()
        self.assertEqual(self.draft().pk,first.pk)
        self.assertEqual(Retrospective.objects.count(),1)

    def test_member_cannot_create_or_read_draft(self):
        with self.assertRaises(PermissionDenied):
            create_retro(self.member,self.activity.pk)
        retro=self.draft()
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("retro_detail",args=[retro.pk])).status_code,404)

    def test_draft_counts_do_not_call_active_task_complete(self):
        snap=activity_snapshot(self.activity)
        self.assertEqual(snap["counts"]["verified_done"],0)
        self.assertEqual(snap["counts"]["unfinished"],1)

    def test_snapshot_excludes_private_work_and_feedback(self):
        self.accepted_result()
        snap=activity_snapshot(self.activity)
        self.assertEqual(snap["counts"]["verified_done"],1)
        self.assertNotIn("SECRET",str(snap))
        self.assertNotIn("method",str(snap))

    def test_stale_draft_save_rejected_and_old_confirmed_immutable(self):
        retro=self.draft()
        newer=save_retro(self.lead,retro.pk,1,self.notes())
        with self.assertRaises(ValidationError):
            save_retro(self.lead,retro.pk,1,self.notes(facts="不应覆盖"))
        confirmed=self.confirm(newer)
        with self.assertRaises(ValidationError):
            save_retro(self.lead,confirmed.pk,confirmed.revision,self.notes(facts="改写"))

    def test_confirm_requires_actual_result_and_ack(self):
        retro=self.draft()
        change_stage(self.lead,self.activity.pk,1,"wrapup","进入收尾")
        with self.assertRaises(ValidationError):
            confirm_retro(self.lead,retro.pk,1,True)
        retro=save_retro(self.lead,retro.pk,1,self.notes())
        with self.assertRaises(ValidationError):
            confirm_retro(self.lead,retro.pk,retro.revision,False)

    def test_confirm_requires_wrapup(self):
        retro=self.draft()
        retro=save_retro(self.lead,retro.pk,1,self.notes())
        with self.assertRaises(ValidationError):
            confirm_retro(self.lead,retro.pk,retro.revision,True)

    def test_changed_task_requires_refresh_before_confirmation(self):
        retro=self.draft()
        retro=save_retro(self.lead,retro.pk,1,self.notes())
        change_task(self.member,self.task.pk,2,"block","需要转接头")
        change_stage(self.lead,self.activity.pk,1,"wrapup","收尾")
        with self.assertRaises(ValidationError):
            confirm_retro(self.lead,retro.pk,retro.revision,True)
        retro=save_retro(self.lead,retro.pk,retro.revision,self.notes(),refresh=True)
        confirmed=confirm_retro(self.lead,retro.pk,retro.revision,True)
        self.assertTrue(source_current(confirmed))

    def test_confirmed_visible_only_to_activity_members(self):
        retro=self.confirm()
        self.client.force_login(self.member)
        self.assertContains(self.client.get(reverse("retro_detail",args=[retro.pk])),retro.facts)
        self.client.force_login(self.outside)
        self.assertEqual(self.client.get(reverse("retro_detail",args=[retro.pk])).status_code,404)

    def test_archive_does_not_complete_tasks_or_invalidate_retro(self):
        retro=self.confirm()
        self.activity.refresh_from_db()
        change_stage(self.lead,self.activity.pk,self.activity.stage_version,"archived","存档但继续跟进未完成事项")
        self.task.refresh_from_db()
        self.assertEqual(self.task.status,"active")
        retro.refresh_from_db()
        self.assertTrue(source_current(retro))

    def test_phase_changes_require_role_version_and_reason(self):
        with self.assertRaises(PermissionDenied):
            change_stage(self.member,self.activity.pk,1,"wrapup","越权")
        with self.assertRaises(ValidationError):
            change_stage(self.lead,self.activity.pk,1,"archived","不能跳过收尾")
        with self.assertRaises(ValidationError):
            change_stage(self.lead,self.activity.pk,1,"wrapup","")
        change_stage(self.lead,self.activity.pk,1,"active","开始")
        with self.assertRaises(ValidationError):
            change_stage(self.lead,self.activity.pk,1,"wrapup","旧页面")

    def test_draft_cannot_export(self):
        retro=self.draft()
        with self.assertRaises(ValidationError):
            export_knowledge(self.lead,retro.pk,1,{"title":"经验","body":"内容","applicability":"小型","share_ack":True})
        self.assertEqual(KnowledgeDerivation.objects.count(),0)

    def test_export_requires_explicit_club_sharing_ack(self):
        retro=self.confirm()
        with self.assertRaises(ValidationError):
            export_knowledge(self.lead,retro.pk,retro.revision,{"title":"经验","body":"内容","applicability":"小型","share_ack":False})

    def test_export_creates_pending_and_requires_other_reviewer(self):
        retro,entry=self.card()
        self.assertEqual(entry.status,"pending")
        self.assertFalse(searchable_entries(self.member).filter(pk=entry.pk).exists())
        with self.assertRaises(PermissionDenied):
            review_entry(self.lead,entry.pk,1,"approve","自审")
        review_entry(self.lead2,entry.pk,1,"approve","核对通过")
        self.assertTrue(searchable_entries(self.outside).filter(pk=entry.pk).exists())

    def test_repeated_export_does_not_overwrite_card(self):
        retro,entry=self.card()
        again=export_knowledge(self.lead,retro.pk,retro.revision,{"title":"不覆盖","body":"不覆盖","applicability":"小型","share_ack":True})
        self.assertEqual(again.pk,entry.pk)
        self.assertEqual(Entry.objects.count(),1)
        self.assertEqual(KnowledgeDerivation.objects.count(),1)
        self.assertEqual(again.title,"投影故障经验")

    def test_approved_derived_card_can_be_retrieved_with_original_quote(self):
        retro,entry=self.card(approve=True)
        refs=collect_references(self.outside,"投影故障")
        self.assertEqual(len(refs),1)
        self.assertEqual(resolve_reference(self.outside,refs[0])["excerpt"],retro.improvements)

    def test_withdraw_excludes_library_qa_source_and_public_detail(self):
        retro,entry=self.card(approve=True)
        ref=collect_references(self.member,"投影故障")[0]
        retro.refresh_from_db()
        withdraw_retro(self.lead,retro.pk,retro.revision,"重新核对")
        self.assertIsNone(resolve_reference(self.member,ref))
        self.assertFalse(searchable_entries(self.member).filter(pk=entry.pk).exists())
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("detail",args=[entry.pk])).status_code,404)
        self.assertNotContains(self.client.get("/knowledge/?q=投影故障"),entry.title)
        self.assertEqual(self.client.get(reverse("answer_source",args=[entry.pk,1,0])).status_code,410)

    def test_revoked_acceptance_invalidates_confirmed_retro_and_knowledge(self):
        result=self.accepted_result()
        retro,entry=self.card(approve=True)
        self.task.refresh_from_db()
        review_result(self.lead,result.pk,self.task.version,"revoke","重新验收")
        self.assertFalse(source_current(retro))
        self.assertFalse(searchable_entries(self.member).filter(pk=entry.pk).exists())
        self.client.force_login(self.member)
        response=self.client.get(reverse("retro_detail",args=[retro.pk]))
        self.assertNotContains(response,retro.facts)
        self.assertContains(response,"旧内容已隐藏")

    def test_new_task_invalidates_snapshot(self):
        retro,entry=self.card(approve=True)
        publish_task(self.lead,self.activity,{"token":uuid.uuid4(),"title":"新增任务","objective":"补充","deliverable":"清单","acceptance":"有依据","due_at":timezone.now(),"assignee":None,"reference":None})
        self.assertFalse(source_current(retro))
        self.assertFalse(searchable_entries(self.member).filter(pk=entry.pk).exists())

    def test_invalid_source_cannot_be_reapproved_or_assigned_as_reference(self):
        retro,entry=self.card()
        retro.refresh_from_db()
        withdraw_retro(self.lead,retro.pk,retro.revision,"来源撤回")
        with self.assertRaises(ValidationError):
            review_entry(self.lead2,entry.pk,1,"approve","不能绕过")
        entry.status="approved"
        entry.save()
        with self.assertRaises(ValidationError):
            publish_task(self.lead,self.activity,{"token":uuid.uuid4(),"title":"新任务","objective":"测试","deliverable":"清单","acceptance":"完成","due_at":timezone.now(),"assignee":None,"reference":entry})

    def test_new_confirmed_retro_supersedes_old_derived_card(self):
        old,entry=self.card(approve=True)
        new=self.confirm(self.draft())
        old.refresh_from_db()
        self.assertEqual(old.status,"superseded")
        self.assertTrue(source_current(new))
        self.assertFalse(searchable_entries(self.member).filter(pk=entry.pk).exists())

    def test_existing_unrelated_knowledge_is_unchanged(self):
        from knowledge.services import submit_entry
        normal=submit_entry(self.member,{"token":uuid.uuid4(),"title":"普通独立经验","category":"case","body":"正常内容","source":"独立来源","applicability":"本机"})
        review_entry(self.lead,normal.pk,1,"approve","独立审核")
        retro,entry=self.card(approve=True)
        change_task(self.member,self.task.pk,2,"block","任务变更")
        self.assertTrue(searchable_entries(self.member).filter(pk=normal.pk).exists())
        self.assertFalse(searchable_entries(self.member).filter(pk=entry.pk).exists())

    def test_post_csrf_and_member_mutation(self):
        retro=self.draft()
        c=Client(enforce_csrf_checks=True)
        c.force_login(self.lead)
        self.assertEqual(c.post(reverse("retro_save",args=[retro.pk]),{"revision":1}).status_code,403)
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(reverse("retro_save",args=[retro.pk]),{"revision":1}).status_code,403)

    def test_http_create_save_confirm_and_export(self):
        self.client.force_login(self.lead)
        response=self.client.post(reverse("retro_create",args=[self.activity.pk]))
        self.assertEqual(response.status_code,302)
        retro=Retrospective.objects.get()
        response=self.client.post(reverse("retro_save",args=[retro.pk]),{"revision":1,**self.notes()})
        self.assertEqual(response.status_code,302)
        change_stage(self.lead,self.activity.pk,1,"wrapup","收尾")
        self.assertEqual(self.client.post(reverse("retro_action",args=[retro.pk]),{"revision":2,"action":"confirm","acknowledged":"on"}).status_code,302)
        response=self.client.post(reverse("retro_export",args=[retro.pk]),{"revision":3,"title":"提炼经验","body":"公开的改进措施","applicability":"本机","share_ack":"on"})
        self.assertEqual(response.status_code,302)

    def test_invalid_save_preserves_input(self):
        retro=self.draft()
        self.client.force_login(self.lead)
        response=self.client.post(reverse("retro_save",args=[retro.pk]),{"revision":99,**self.notes(facts="需要保留的输入")})
        self.assertEqual(response.status_code,409)
        self.assertContains(response,"需要保留的输入",status_code=409)

    def test_stale_scope_hidden_from_question_choices(self):
        retro,entry=self.card(approve=True)
        change_task(self.member,self.task.pk,2,"block","任务变化")
        self.client.force_login(self.member)
        response=self.client.get("/assistant/")
        self.assertNotContains(response,entry.title)
        response=self.client.post("/assistant/",{"token":uuid.uuid4(),"question":"问题","scope":str(entry.pk)})
        self.assertTrue(response.context["form"].errors)

    def test_untrusted_retro_notes_are_escaped(self):
        retro=self.draft()
        retro=save_retro(self.lead,retro.pk,1,self.notes(facts="<script>unsafe()</script>"))
        change_stage(self.lead,self.activity.pk,1,"wrapup","收尾")
        retro=confirm_retro(self.lead,retro.pk,retro.revision,True)
        self.client.force_login(self.member)
        self.assertContains(self.client.get(reverse("retro_detail",args=[retro.pk])),"&lt;script&gt;")

