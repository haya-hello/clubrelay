import json
import tempfile
import uuid
from pathlib import Path
from django.contrib.auth.models import User, Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from .models import Entry, Revision, ReviewEvent
from .services import submit_entry, review_entry, resubmit_entry, REVIEWER_GROUP

class KnowledgeWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.member = User.objects.create_user("test_member", password="fictional-test-only", first_name="测试成员")
        cls.other = User.objects.create_user("test_other", password="fictional-test-only", first_name="其他成员")
        cls.lead = User.objects.create_user("test_lead", password="fictional-test-only", first_name="测试负责人")
        cls.lead.groups.add(Group.objects.create(name=REVIEWER_GROUP))

    def data(self, **changes):
        result = {"token": uuid.uuid4(), "title": "设备检查演练", "category": "process", "body": "先测试投影，再准备离线备份。", "source": "虚构测试资料", "applicability": "仅用于小型活动演练"}
        result.update(changes)
        return result

    def entry(self, **changes):
        return submit_entry(self.member, self.data(**changes))

    def approve(self, entry):
        return review_entry(self.lead, entry.pk, entry.version, "approve", "测试审核通过")

    def test_login_requires_credentials_and_logout_is_post(self):
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertFalse(self.client.login(username="test_member", password="wrong"))
        self.assertTrue(self.client.login(username="test_member", password="fictional-test-only"))
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/logout/").status_code, 405)
        self.assertEqual(self.client.post("/logout/").status_code, 302)

    def test_pending_is_not_searchable_or_visible_to_other(self):
        entry = self.entry()
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get("/knowledge/?q=设备"), entry.title)
        self.assertEqual(self.client.get(reverse("detail", args=[entry.pk])).status_code, 404)

    def test_owner_can_see_pending_and_lead_can_review(self):
        entry = self.entry()
        for user in (self.member, self.lead):
            self.client.force_login(user)
            self.assertContains(self.client.get(reverse("detail", args=[entry.pk])), entry.title)

    def test_approved_search_and_source(self):
        entry = self.approve(self.entry())
        self.client.force_login(self.other)
        self.assertContains(self.client.get("/knowledge/?q=离线"), entry.title)
        detail = self.client.get(reverse("detail", args=[entry.pk]))
        self.assertContains(detail, entry.source)
        self.assertContains(detail, entry.body)
        self.assertNotContains(detail, "测试审核通过")

    def test_unrelated_query_and_category_filter(self):
        entry = self.approve(self.entry())
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get("/knowledge/?q=不存在的词"), entry.title)
        self.assertNotContains(self.client.get("/knowledge/?category=case"), entry.title)
        self.assertContains(self.client.get("/knowledge/?category=process&q=投影"), entry.title)

    def test_invalid_search_is_not_silently_all_results(self):
        entry = self.approve(self.entry())
        self.client.force_login(self.other)
        response = self.client.get("/knowledge/", {"q": "长" * 121})
        self.assertNotContains(response, entry.title)
        self.assertTrue(response.context["form"].errors)

    def test_submit_http_and_double_post_idempotency(self):
        self.client.force_login(self.member)
        data = self.data()
        for _ in range(2):
            self.assertEqual(self.client.post("/submissions/new/", data).status_code, 302)
        self.assertEqual(Entry.objects.count(), 1)
        self.assertEqual(Revision.objects.count(), 1)
        self.assertEqual(ReviewEvent.objects.count(), 1)

    def test_forged_owner_ignored(self):
        self.client.force_login(self.member)
        data = self.data(owner=self.lead.pk, status="approved")
        self.client.post("/submissions/new/", data)
        entry = Entry.objects.get()
        self.assertEqual(entry.owner_id, self.member.id)
        self.assertEqual(entry.status, "pending")

    def test_other_user_cannot_reuse_submission_token(self):
        data = self.data()
        submit_entry(self.member, data)
        with self.assertRaises(PermissionDenied):
            submit_entry(self.other, data)

    def test_empty_and_oversized_body_rejected(self):
        self.client.force_login(self.member)
        for body in ("   ", "文" * 50001):
            response = self.client.post("/submissions/new/", self.data(body=body))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["form"].errors)
        self.assertEqual(Entry.objects.count(), 0)

    def test_missing_metadata_keeps_user_input(self):
        self.client.force_login(self.member)
        response = self.client.post("/submissions/new/", self.data(source=""))
        self.assertContains(response, "设备检查演练")
        self.assertTrue(response.context["form"].errors)
        self.assertEqual(Entry.objects.count(), 0)

    def test_members_cannot_review_or_view_queue(self):
        entry = self.entry()
        self.client.force_login(self.member)
        self.assertEqual(self.client.get("/review/").status_code, 403)
        self.assertEqual(self.client.post(reverse("review", args=[entry.pk]), {"version": 1, "action": "approve", "reason": "越权"}).status_code, 403)
        entry.refresh_from_db()
        self.assertEqual(entry.status, "pending")

    def test_own_knowledge_cannot_be_self_approved(self):
        entry = submit_entry(self.lead, self.data())
        with self.assertRaises(PermissionDenied):
            review_entry(self.lead, entry.pk, 1, "approve", "自己审核")

    def test_reject_requires_reason_and_retains_opinion(self):
        entry = self.entry()
        self.client.force_login(self.lead)
        url = reverse("review", args=[entry.pk])
        self.assertEqual(self.client.post(url, {"version": 1, "action": "reject", "reason": ""}).status_code, 409)
        self.assertEqual(self.client.post(url, {"version": 1, "action": "reject", "reason": "请补充边界"}).status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.status, "rejected")
        self.assertEqual(entry.review_reason, "请补充边界")

    def test_resubmit_preserves_old_body_and_removes_from_search(self):
        entry = self.approve(self.entry())
        data = self.data(version=1, body="新版本：检查电源。")
        updated = resubmit_entry(self.member, entry.pk, data)
        self.assertEqual(updated.version, 2)
        self.assertEqual(updated.status, "pending")
        self.assertIn("离线", updated.revisions.get(number=1).body)
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get("/knowledge/?q=设备"), updated.title)
        self.assertEqual(self.client.get(reverse("detail", args=[entry.pk])).status_code, 404)

    def test_edit_http_resubmits(self):
        entry = self.approve(self.entry())
        self.client.force_login(self.member)
        response = self.client.post(reverse("edit", args=[entry.pk]), self.data(token=entry.submit_token, version=1, body="修改后正文"))
        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.version, 2)
        self.assertEqual(entry.body, "修改后正文")

    def test_old_edit_rejected(self):
        entry = self.entry()
        resubmit_entry(self.member, entry.pk, self.data(version=1))
        with self.assertRaises(ValidationError):
            resubmit_entry(self.member, entry.pk, self.data(version=1, body="不应覆盖"))

    def test_non_owner_cannot_edit(self):
        entry = self.approve(self.entry())
        self.client.force_login(self.lead)
        self.assertEqual(self.client.get(reverse("edit", args=[entry.pk])).status_code, 403)
        with self.assertRaises(PermissionDenied):
            resubmit_entry(self.other, entry.pk, self.data(version=1))

    def test_stale_or_double_review_not_applied(self):
        entry = self.entry()
        resubmit_entry(self.member, entry.pk, self.data(version=1))
        with self.assertRaises(ValidationError):
            review_entry(self.lead, entry.pk, 1, "approve", "过时审核")
        review_entry(self.lead, entry.pk, 2, "approve", "通过")
        with self.assertRaises(ValidationError):
            review_entry(self.lead, entry.pk, 2, "approve", "重复")
        self.assertEqual(entry.events.filter(action="approve").count(), 1)

    def test_withdraw_excludes_search_and_other_user_detail(self):
        entry = self.approve(self.entry())
        review_entry(self.lead, entry.pk, 1, "withdraw", "流程需要核验")
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get("/knowledge/?q=设备"), entry.title)
        self.assertEqual(self.client.get(reverse("detail", args=[entry.pk])).status_code, 404)

    def test_illegal_transition_denied(self):
        entry = self.entry()
        with self.assertRaises(ValidationError):
            review_entry(self.lead, entry.pk, 1, "withdraw", "未审核不可下架")
        with self.assertRaises(ValidationError):
            review_entry(self.lead, entry.pk, 1, "delete", "不提供删除")

    def test_text_is_escaped_not_executed(self):
        entry = self.approve(self.entry(body="<script>alert('x')</script>"))
        self.client.force_login(self.other)
        response = self.client.get(reverse("detail", args=[entry.pk]))
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>alert")

    def test_csrf_required_for_submission(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.member)
        self.assertEqual(client.post("/submissions/new/", self.data()).status_code, 403)

    def test_inactive_account_denied(self):
        self.member.is_active = False
        self.member.save()
        self.assertFalse(self.client.login(username="test_member", password="fictional-test-only"))
        with self.assertRaises(PermissionDenied):
            submit_entry(self.member, self.data())

    def test_private_audit_and_revision_only_visible_to_owner_and_lead(self):
        entry = self.entry()
        resubmit_entry(self.member, entry.pk, self.data(version=1, body="新的已批准正文"))
        entry.refresh_from_db()
        self.approve(entry)
        self.client.force_login(self.other)
        response = self.client.get(reverse("detail", args=[entry.pk]))
        self.assertNotContains(response, "历史版本")
        self.assertNotContains(response, "流转记录")
        self.assertNotContains(response, "离线备份")
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_invalid_review_retains_reason(self):
        entry = self.entry()
        self.client.force_login(self.lead)
        response = self.client.post(reverse("review", args=[entry.pk]), {"version": 99, "action": "approve", "reason": "保留这条意见"})
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "保留这条意见", status_code=409)

    def test_seed_is_idempotent_does_not_reset_password_or_status(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(DATA_DIR=Path(folder)):
            call_command("seed_demo", verbosity=0)
            first = User.objects.get(username="linxia").password
            entry = Entry.objects.get(submit_token="a41e2920-d5f1-48c8-8b10-9d902e211111")
            entry.status = "withdrawn"
            entry.save()
            call_command("seed_demo", verbosity=0)
            self.assertEqual(User.objects.get(username="linxia").password, first)
            entry.refresh_from_db()
            self.assertEqual(entry.status, "withdrawn")
            credentials = json.loads((Path(folder) / "demo-accounts.json").read_text(encoding="utf-8"))
            self.assertEqual(len(credentials), 4)

