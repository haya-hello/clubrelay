import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from django.contrib.auth.models import User, Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone
from knowledge.services import REVIEWER_GROUP
from .models import Submission, Achievement, Task
from .services import create_activity, publish_task, change_task
from .deliveries import submit_result, review_result, current_achievements

class DeliveryWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.lead=User.objects.create_user("delivery_lead",first_name="负责人甲")
        cls.lead2=User.objects.create_user("delivery_lead2",first_name="负责人乙")
        cls.member=User.objects.create_user("delivery_member",first_name="成员甲")
        cls.other=User.objects.create_user("delivery_other",first_name="成员乙")
        group=Group.objects.create(name=REVIEWER_GROUP)
        cls.lead.groups.add(group)
        cls.lead2.groups.add(group)
        cls.activity=create_activity(cls.lead,{"token":uuid.uuid4(),"title":"成果验收测试","kind":"activity","objective":"验证实际交付","audience":"测试成员","scheduled_at":timezone.now()+timedelta(days=10),"constraints":"虚构资料","plan":"","participants":[cls.member,cls.other]})

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        settings=override_settings(MEDIA_ROOT=Path(self.temp.name))
        settings.enable()
        self.addCleanup(settings.disable)
        self.client.force_login(self.member)

    def task(self, author=None):
        author=author or self.member
        task=publish_task(self.lead,self.activity,{"token":uuid.uuid4(),"title":"提交网页原型","objective":"完成表单反馈","deliverable":"可复核原型","acceptance":"流程可验证","due_at":timezone.now()+timedelta(days=5),"assignee":author,"reference":None})
        return change_task(author,task.pk,1,"accept")

    def data(self,task,**changes):
        task.refresh_from_db()
        data={"token":uuid.uuid4(),"version":task.version,"summary":"已完成原型和测试","result_text":"表单包含姓名字段，提交成功显示确认。","result_url":"","method":"使用AI辅助编码，自己验证两条流程。","contribution":"本人负责表单和错误提示。","attachment":None}
        data.update(changes)
        return data

    def result(self,task=None,author=None,**changes):
        task=task or self.task(author)
        return submit_result(author or self.member,task.pk,self.data(task,**changes))

    def approve(self,result,**changes):
        result.task.refresh_from_db()
        data={"version":result.task.version,"action":"approve","reason":"逐项验证通过","practice":True,"contribution":True}
        data.update(changes)
        return review_result(self.lead,result.pk,**data)

    def test_submit_sets_pending_not_credit(self):
        result=self.result()
        result.task.refresh_from_db()
        self.assertEqual(result.status,"pending")
        self.assertEqual(result.task.status,"review")
        self.assertEqual(Achievement.objects.count(),0)

    def test_double_use_and_single_credit(self):
        result=self.approve(self.result())
        self.assertEqual(Achievement.objects.count(),1)
        self.assertEqual(current_achievements(self.member).filter(practice=True).count(),1)
        self.assertEqual(current_achievements(self.member).filter(contribution=True).count(),1)

    def test_duplicate_acceptance_not_counted_twice(self):
        result=self.result()
        self.approve(result)
        with self.assertRaises(ValidationError):
            self.approve(result)
        self.assertEqual(Achievement.objects.count(),1)
        self.assertEqual(result.reviews.count(),1)

    def test_return_then_new_version_preserves_history(self):
        task=self.task()
        first=self.result(task)
        review_result(self.lead,first.pk,3,"return","补充失败流程")
        task.refresh_from_db()
        self.assertEqual(task.status,"changes")
        second=self.result(task,result_text="第二版包含失败流程。")
        self.assertEqual(second.number,2)
        first.refresh_from_db()
        self.assertEqual(first.status,"returned")
        self.approve(second)
        self.assertEqual(Achievement.objects.get().submission_id,second.pk)

    def test_pending_resubmission_blocks_stale_review(self):
        task=self.task()
        first=self.result(task)
        old_version=3
        second=self.result(task)
        first.refresh_from_db()
        self.assertEqual(first.status,"superseded")
        with self.assertRaises(ValidationError):
            review_result(self.lead,first.pk,old_version,"approve","旧页审核",True,True)
        self.approve(second)
        self.assertEqual(Achievement.objects.count(),1)

    def test_revoke_removes_both_counts_and_reapproval_reuses_record(self):
        task=self.task()
        first=self.approve(self.result(task))
        task.refresh_from_db()
        review_result(self.lead,first.pk,task.version,"revoke","漏检重要项")
        self.assertEqual(current_achievements(self.member).count(),0)
        task.refresh_from_db()
        self.assertEqual(task.status,"changes")
        second=self.approve(self.result(task))
        self.assertEqual(Achievement.objects.count(),1)
        self.assertEqual(Achievement.objects.get().submission_id,second.pk)
        self.assertEqual(first.reviews.count(),2)

    def test_cannot_review_own_work_but_other_lead_can(self):
        result=self.result(author=self.lead)
        result.task.refresh_from_db()
        with self.assertRaises(PermissionDenied):
            review_result(self.lead,result.pk,result.task.version,"approve","自审",True,False)
        review_result(self.lead2,result.pk,result.task.version,"approve","他人验收",True,False)

    def test_wrong_member_cannot_submit_or_review(self):
        task=self.task()
        with self.assertRaises(PermissionDenied):
            submit_result(self.other,task.pk,self.data(task))
        result=self.result(task)
        with self.assertRaises(PermissionDenied):
            review_result(self.other,result.pk,3,"approve","越权",True,True)

    def test_blocked_unaccepted_cancelled_cannot_submit(self):
        task=self.task()
        change_task(self.member,task.pk,2,"block","缺少材料")
        with self.assertRaises(ValidationError):
            self.result(task)
        change_task(self.lead,task.pk,3,"cancel","取消")
        with self.assertRaises(ValidationError):
            self.result(task)

    def test_done_task_cannot_resubmit_or_cancel_directly(self):
        result=self.approve(self.result())
        with self.assertRaises(ValidationError):
            self.result(result.task)
        result.task.refresh_from_db()
        with self.assertRaises(ValidationError):
            change_task(self.lead,result.task_id,result.task.version,"cancel","不应绕过撤销")

    def test_cancel_pending_removes_acceptance_queue(self):
        result=self.result()
        change_task(self.lead,result.task_id,3,"cancel","任务取消")
        result.refresh_from_db()
        self.assertEqual(result.status,"cancelled")
        self.client.force_login(self.lead)
        self.assertNotContains(self.client.get("/acceptance/"),result.task.title)

    def test_stale_task_version_rejected(self):
        task=self.task()
        data=self.data(task)
        change_task(self.member,task.pk,2,"block","受阻")
        change_task(self.member,task.pk,3,"unblock","已解决")
        with self.assertRaises(ValidationError):
            submit_result(self.member,task.pk,data)
        self.assertEqual(Submission.objects.count(),0)

    def test_duplicate_submission_token_idempotent(self):
        task=self.task()
        data=self.data(task)
        a=submit_result(self.member,task.pk,data)
        b=submit_result(self.member,task.pk,data)
        self.assertEqual(a.pk,b.pk)
        self.assertEqual(Submission.objects.count(),1)

    def test_reason_and_credit_flags_required(self):
        result=self.result()
        for reason,practice,contribution in [("",True,True),("无用途",False,False)]:
            with self.assertRaises(ValidationError):
                review_result(self.lead,result.pk,3,"approve",reason,practice,contribution)

    def test_no_evidence_rejected(self):
        task=self.task()
        with self.assertRaises(ValidationError):
            self.result(task,result_text="",result_url="",attachment=None)

    def test_file_only_submission_private_download_and_nosniff(self):
        task=self.task()
        upload=SimpleUploadedFile("demo.html",b"<script>example()</script>","text/html")
        result=self.result(task,result_text="",attachment=upload)
        self.assertTrue(result.attachment.name.startswith("deliveries/"))
        response=self.client.get(reverse("download_result",args=[result.pk]))
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.headers["Content-Disposition"].startswith("attachment"))
        self.assertEqual(response.headers["Content-Type"],"application/octet-stream")
        self.assertEqual(response.headers["X-Content-Type-Options"],"nosniff")
        self.assertIn("no-store",response.headers["Cache-Control"])
        response.close()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("download_result",args=[result.pk])).status_code,404)
        self.assertEqual(self.client.get("/private-uploads-not-served/"+result.attachment.name).status_code,404)

    def test_invalid_empty_or_oversize_files(self):
        for name,content in [("run.exe",b"x"),("empty.txt",b""),("big.txt",b"x"*(5*1024*1024+1))]:
            task=self.task()
            with self.assertRaises(ValidationError):
                self.result(task,attachment=SimpleUploadedFile(name,content))
        self.assertEqual(list(Path(self.temp.name).rglob("*")),[])

    def test_http_streaming_limit_rejects_even_if_text_present(self):
        task=self.task()
        data=self.data(task)
        data["attachment"]=SimpleUploadedFile("big.txt",b"x"*(5*1024*1024+1))
        response=self.client.post(reverse("submit_result",args=[task.pk]),data)
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.context["form"].errors)
        self.assertEqual(Submission.objects.count(),0)

    def test_http_rejects_multiple_files(self):
        task=self.task()
        data=self.data(task)
        data["attachment"]=[SimpleUploadedFile("a.txt",b"a"),SimpleUploadedFile("b.txt",b"b")]
        response=self.client.post(reverse("submit_result",args=[task.pk]),data)
        self.assertTrue(response.context["form"].errors)
        self.assertEqual(Submission.objects.count(),0)

    def test_file_cleaned_if_database_save_fails(self):
        task=self.task()
        data=self.data(task,attachment=SimpleUploadedFile("copy.txt",b"temporary"))
        with patch("activities.models.Submission.save",side_effect=IntegrityError("test")):
            with self.assertRaises(IntegrityError):
                submit_result(self.member,task.pk,data)
        self.assertFalse(any(p.is_file() for p in Path(self.temp.name).rglob("*")))
        task.refresh_from_db()
        self.assertEqual(task.status,"active")

    def test_private_review_not_in_shared_task_events(self):
        result=self.result()
        self.approve(result,reason="仅限本人查看的详细反馈")
        self.client.force_login(self.other)
        response=self.client.get(reverse("task_detail",args=[result.task_id]))
        self.assertNotContains(response,"仅限本人查看")
        self.assertEqual(self.client.get(reverse("submission_detail",args=[result.pk])).status_code,404)

    def test_growth_only_counts_own_current_accepted_results(self):
        self.approve(self.result())
        self.result()
        response=self.client.get("/growth/")
        self.assertEqual(response.context["unique_count"],1)
        self.assertEqual(response.context["practice_count"],1)
        self.assertEqual(response.context["contribution_count"],1)
        self.client.force_login(self.other)
        response=self.client.get("/growth/")
        self.assertEqual(response.context["unique_count"],0)

    def test_xss_and_invalid_links(self):
        result=self.result(result_text="<script>unsafe()</script>")
        self.assertContains(self.client.get(reverse("submission_detail",args=[result.pk])),"&lt;script&gt;")
        task=self.task()
        for url in ["javascript:alert(1)","https://user:password@example.com"]:
            with self.assertRaises(ValidationError):
                self.result(task,result_url=url)

    def test_http_form_submission_and_error_retains_text(self):
        task=self.task()
        data=self.data(task)
        data.pop("attachment")
        response=self.client.post(reverse("submit_result",args=[task.pk]),data)
        self.assertEqual(response.status_code,302)
        result=Submission.objects.get()
        self.client.force_login(self.lead)
        response=self.client.post(reverse("review_result",args=[result.pk]),{"version":99,"action":"approve","reason":"保留意见","practice":"on"})
        self.assertEqual(response.status_code,409)
        self.assertContains(response,"保留意见",status_code=409)

    def test_csrf_and_review_queue_permissions(self):
        task=self.task()
        client=Client(enforce_csrf_checks=True)
        client.force_login(self.member)
        data=self.data(task)
        data.pop("attachment")
        self.assertEqual(client.post(reverse("submit_result",args=[task.pk]),data).status_code,403)
        self.assertEqual(self.client.get("/acceptance/").status_code,403)

    def test_credit_purpose_can_be_corrected_via_revoke_and_reaccept(self):
        task=self.task()
        first=self.approve(self.result(task),practice=True,contribution=False)
        task.refresh_from_db()
        review_result(self.lead,first.pk,task.version,"revoke","更正用途，需重新确认")
        second=self.approve(self.result(task),practice=False,contribution=True)
        credit=Achievement.objects.get()
        self.assertFalse(credit.practice)
        self.assertTrue(credit.contribution)
        self.assertEqual(credit.submission_id,second.pk)

