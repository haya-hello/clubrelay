import uuid
from datetime import timedelta
from django.contrib.auth.models import User, Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from django.core.management import call_command
from django.test import override_settings
import tempfile
from pathlib import Path
from knowledge.models import Entry
from knowledge.services import REVIEWER_GROUP, submit_entry
from .models import Activity, Task, TaskEvent
from .services import create_activity, publish_task, change_task

class ActivityWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.lead = User.objects.create_user("task_lead", password="fictional-test-only", first_name="负责人")
        cls.member = User.objects.create_user("task_member", password="fictional-test-only", first_name="成员甲")
        cls.second = User.objects.create_user("task_second", password="fictional-test-only", first_name="成员乙")
        cls.outside = User.objects.create_user("task_outside", password="fictional-test-only", first_name="未加入")
        cls.lead.groups.add(Group.objects.create(name=REVIEWER_GROUP))

    def data(self, **changes):
        value = {"token":uuid.uuid4(),"title":"虚构实践工作坊","kind":"activity","objective":"做出可演示作品","audience":"入门成员","scheduled_at":timezone.now()+timedelta(days=10),"constraints":"场地待确认","plan":"先演示再实践","participants":[self.member,self.second]}
        value.update(changes)
        return value

    def activity(self, **changes):
        return create_activity(self.lead,self.data(**changes))

    def task_data(self, **changes):
        value = {"token":uuid.uuid4(),"title":"制作报名原型","objective":"验证报名流程","deliverable":"原型和测试说明","acceptance":"字段可填且提交反馈清楚","due_at":timezone.now()+timedelta(days=7),"assignee":None,"reference":None}
        value.update(changes)
        return value

    def task(self, **changes):
        return publish_task(self.lead,self.activity(),self.task_data(**changes))

    def test_login_required(self):
        self.assertEqual(self.client.get("/activities/").status_code,302)

    def test_member_cannot_create_activity(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.get("/activities/new/").status_code,403)
        with self.assertRaises(PermissionDenied):
            create_activity(self.member,self.data())

    def test_create_includes_creator_and_selected_members_only(self):
        activity = self.activity()
        self.assertSetEqual(set(activity.participants.values_list("id",flat=True)),{self.lead.id,self.member.id,self.second.id})
        self.client.force_login(self.outside)
        self.assertNotContains(self.client.get("/activities/"),activity.title)
        self.assertEqual(self.client.get(reverse("activity_detail",args=[activity.pk])).status_code,404)

    def test_activity_creation_http_and_duplicate_token(self):
        self.client.force_login(self.lead)
        data = self.data(participants=[self.member.pk], scheduled_at="2026-12-01T14:00")
        for _ in range(2):
            self.assertEqual(self.client.post("/activities/new/",data).status_code,302)
        self.assertEqual(Activity.objects.count(),1)

    def test_activity_validation_keeps_input(self):
        self.client.force_login(self.lead)
        data = self.data(participants=[],objective="",scheduled_at="not-time")
        response = self.client.post("/activities/new/",data)
        self.assertEqual(response.status_code,200)
        self.assertContains(response,data["title"])
        self.assertEqual(Activity.objects.count(),0)

    def test_task_publication_open_vs_assigned(self):
        activity = self.activity()
        open_task = publish_task(self.lead,activity,self.task_data())
        assigned = publish_task(self.lead,activity,self.task_data(assignee=self.member))
        self.assertEqual(open_task.status,"open")
        self.assertIsNone(open_task.assignee_id)
        self.assertEqual(assigned.status,"waiting")
        self.assertEqual(assigned.assignee_id,self.member.id)

    def test_task_http_validation_and_publish(self):
        activity = self.activity()
        self.client.force_login(self.lead)
        data = self.task_data(assignee="",reference="",due_at="2026-12-01T12:00")
        url = reverse("task_create",args=[activity.pk])
        self.assertEqual(self.client.post(url,data).status_code,302)
        self.assertEqual(self.client.post(url,data).status_code,302)
        self.assertEqual(activity.tasks.count(),1)
        self.assertEqual(TaskEvent.objects.count(),1)
        data = self.task_data(acceptance="",assignee="",reference="",due_at="2026-12-01T12:00")
        response = self.client.post(url,data)
        self.assertTrue(response.context["form"].errors)
        self.assertContains(response,data["title"])

    def test_member_cannot_publish_task(self):
        activity = self.activity()
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("task_create",args=[activity.pk])).status_code,403)

    def test_cannot_assign_non_participant(self):
        with self.assertRaises(ValidationError):
            publish_task(self.lead,self.activity(),self.task_data(assignee=self.outside))
        self.assertEqual(Task.objects.count(),0)

    def test_deactivated_assignee_not_valid(self):
        activity = self.activity()
        self.member.is_active=False
        self.member.save()
        with self.assertRaises(ValidationError):
            publish_task(self.lead,activity,self.task_data(assignee=self.member))

    def test_claim_assigns_current_user_not_posted_identity(self):
        task = self.task()
        self.client.force_login(self.member)
        response = self.client.post(reverse("task_action",args=[task.pk]),{"version":1,"action":"claim","assignee":self.second.pk})
        self.assertEqual(response.status_code,302)
        task.refresh_from_db()
        self.assertEqual(task.assignee_id,self.member.id)
        self.assertEqual(task.status,"active")

    def test_duplicate_claim_only_one_owner_and_event(self):
        task = self.task()
        change_task(self.member,task.pk,1,"claim")
        with self.assertRaises(ValidationError):
            change_task(self.second,task.pk,1,"claim")
        task.refresh_from_db()
        self.assertEqual(task.assignee_id,self.member.id)
        self.assertEqual(task.events.filter(action="claim").count(),1)

    def test_non_participant_cannot_view_or_claim(self):
        task = self.task()
        self.client.force_login(self.outside)
        for suffix in ("task_detail","task_action"):
            response = self.client.get(reverse(suffix,args=[task.pk])) if suffix=="task_detail" else self.client.post(reverse(suffix,args=[task.pk]),{"version":1,"action":"claim"})
            self.assertEqual(response.status_code,404)

    def test_assignment_requires_personal_acceptance(self):
        task = self.task(assignee=self.member)
        for other in [self.second,self.lead]:
            with self.assertRaises(PermissionDenied):
                change_task(other,task.pk,1,"accept")
        task.refresh_from_db()
        self.assertEqual(task.status,"waiting")
        change_task(self.member,task.pk,1,"accept")
        task.refresh_from_db()
        self.assertEqual(task.status,"active")

    def test_decline_requires_reason_and_reopens(self):
        task = self.task(assignee=self.member)
        with self.assertRaises(ValidationError):
            change_task(self.member,task.pk,1,"decline")
        change_task(self.member,task.pk,1,"decline","本周没有时间")
        task.refresh_from_db()
        self.assertEqual(task.status,"open")
        self.assertIsNone(task.assignee)
        change_task(self.second,task.pk,2,"claim")
        task.refresh_from_db()
        self.assertEqual(task.assignee_id,self.second.id)

    def test_stale_accept_after_decline_is_denied(self):
        task = self.task(assignee=self.member)
        change_task(self.member,task.pk,1,"decline","时间冲突")
        with self.assertRaises(PermissionDenied):
            change_task(self.member,task.pk,1,"accept")

    def test_block_and_unblock_only_active_assignee(self):
        task = self.task(assignee=self.member)
        with self.assertRaises(ValidationError):
            change_task(self.member,task.pk,1,"block","缺少设备")
        change_task(self.member,task.pk,1,"accept")
        with self.assertRaises(PermissionDenied):
            change_task(self.second,task.pk,2,"block","不应代报")
        with self.assertRaises(ValidationError):
            change_task(self.member,task.pk,2,"block","")
        change_task(self.member,task.pk,2,"block","缺少转接头")
        task.refresh_from_db()
        self.assertEqual(task.blocked_reason,"缺少转接头")
        self.assertEqual(task.status,"active")
        self.client.force_login(self.lead)
        self.assertContains(self.client.get("/"),"缺少转接头")
        change_task(self.member,task.pk,3,"unblock","已借到")
        task.refresh_from_db()
        self.assertEqual(task.blocked_reason,"")

    def test_stale_block_does_not_replace_current_note(self):
        task = self.task()
        change_task(self.member,task.pk,1,"claim")
        change_task(self.member,task.pk,2,"block","当前原因")
        with self.assertRaises(ValidationError):
            change_task(self.member,task.pk,2,"block","过时原因")
        task.refresh_from_db()
        self.assertEqual(task.blocked_reason,"当前原因")

    def test_cancel_requires_lead_and_reason(self):
        task = self.task()
        with self.assertRaises(PermissionDenied):
            change_task(self.member,task.pk,1,"cancel","取消")
        with self.assertRaises(ValidationError):
            change_task(self.lead,task.pk,1,"cancel","")
        change_task(self.lead,task.pk,1,"cancel","安排变化")
        task.refresh_from_db()
        self.assertEqual(task.status,"cancelled")
        with self.assertRaises(ValidationError):
            change_task(self.member,task.pk,2,"claim")

    def test_no_completion_action_available(self):
        task=self.task()
        with self.assertRaises(ValidationError):
            change_task(self.member,task.pk,1,"complete","")

    def test_csrf_and_post_only(self):
        task=self.task()
        client=Client(enforce_csrf_checks=True)
        client.force_login(self.member)
        self.assertEqual(client.post(reverse("task_action",args=[task.pk]),{"action":"claim","version":1}).status_code,403)
        self.assertEqual(client.get(reverse("task_action",args=[task.pk])).status_code,405)

    def test_pending_reference_denied_and_changed_reference_hidden(self):
        entry=submit_entry(self.member,{"token":uuid.uuid4(),"title":"隐藏经验标题","category":"case","body":"不应展示的正文","source":"私有来源","applicability":"待验证"})
        with self.assertRaises(ValidationError):
            publish_task(self.lead,self.activity(),self.task_data(reference=entry))
        entry.status="approved"
        entry.save()
        task=self.task(reference=entry)
        self.client.force_login(self.second)
        self.assertContains(self.client.get(reverse("task_detail",args=[task.pk])),entry.title)
        entry.status="withdrawn"
        entry.save()
        response=self.client.get(reverse("task_detail",args=[task.pk]))
        self.assertNotContains(response,entry.title)
        self.assertContains(response,"关联经验已失效")

    def test_member_home_shows_assigned_task_not_others(self):
        first=self.task(assignee=self.member,title="我的指派")
        second=self.task(assignee=self.second,title="别人的指派")
        self.client.force_login(self.member)
        response=self.client.get("/")
        self.assertContains(response,first.title)
        self.assertNotContains(response,second.title)

    def test_invalid_action_keeps_reason(self):
        task=self.task()
        self.client.force_login(self.member)
        response=self.client.post(reverse("task_action",args=[task.pk]),{"version":99,"action":"claim","reason":"保留输入"})
        self.assertEqual(response.status_code,409)
        self.assertContains(response,"保留输入",status_code=409)

    def test_html_escaped_and_deactivated_member_denied(self):
        task=self.task(title="<script>unsafe()</script>")
        self.client.force_login(self.member)
        response=self.client.get(reverse("task_detail",args=[task.pk]))
        self.assertNotContains(response,"<script>unsafe")
        self.assertContains(response,"&lt;script&gt;")
        self.member.is_active=False
        self.member.save()
        with self.assertRaises(PermissionDenied):
            change_task(self.member,task.pk,1,"claim")

    def test_other_reviewer_can_view_but_cannot_claim_without_membership(self):
        lead=User.objects.create_user("other_task_lead")
        lead.groups.add(Group.objects.get(name=REVIEWER_GROUP))
        task=self.task()
        self.client.force_login(lead)
        self.assertEqual(self.client.get(reverse("task_detail",args=[task.pk])).status_code,200)
        with self.assertRaises(PermissionDenied):
            change_task(lead,task.pk,1,"claim")

    def test_submission_tokens_cannot_cross_activities(self):
        data=self.task_data()
        first=publish_task(self.lead,self.activity(),data)
        with self.assertRaises(PermissionDenied):
            publish_task(self.lead,self.activity(title="另一个活动"),data)
        self.assertEqual(Task.objects.count(),1)

    def test_overdue_does_not_mean_completed(self):
        task=self.task(due_at=timezone.now()-timedelta(days=1))
        self.assertTrue(task.overdue)
        self.assertEqual(task.status,"open")

    def test_activity_demo_does_not_reset_task_state(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(DATA_DIR=Path(folder)):
            call_command("seed_demo", verbosity=0)
            call_command("seed_activity_demo", verbosity=0)
            task = Task.objects.get(token="9367f755-c7cb-4153-8e22-000000000002")
            member = User.objects.get(username="linxia")
            change_task(member,task.pk,1,"accept")
            call_command("seed_activity_demo", verbosity=0)
            task.refresh_from_db()
            self.assertEqual(task.status,"active")
            self.assertEqual(Activity.objects.count(),1)
            self.assertEqual(Task.objects.count(),2)
            member.is_active=False
            member.save()
            call_command("seed_activity_demo", verbosity=0)
            member.refresh_from_db()
            self.assertFalse(member.is_active)
