import json
import time
import uuid

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from operations.security import MANAGER_GROUP

from .bridge_security import sign_payload
from .models import CaptureMarker, ChatMessage, ChatSource, KnowledgeCard, WeeklyBatch
from .services import ingest_event, process_pending, search_cards


class CollectorWorkflowTests(TestCase):
    def setUp(self):
        group = Group.objects.create(name=MANAGER_GROUP)
        self.manager = get_user_model().objects.create_user("manager", password="strong-password-123")
        self.manager.groups.add(group)
        self.client.force_login(self.manager)
        self.source = ChatSource.objects.create(
            platform="qq_official",
            external_id="group-demo",
            display_name="测试群",
            manager_ids=["owner-1"],
            consent_confirmed=True,
        )

    def event(self, **changes):
        data = {
            "platform": "qq_official",
            "group_id": "group-demo",
            "message_id": f"m-{uuid.uuid4().hex}",
            "sender_id": "member-1",
            "sender_name": "小青",
            "timestamp": "2026-09-22T12:00:00+08:00",
            "text": "安装失败时先确认 Python 版本，3.11 已经验证可用。",
            "message_type": "text",
            "reply_to_id": "",
        }
        data.update(changes)
        return data

    def test_normal_message_is_saved_without_marker(self):
        result = ingest_event(self.event(message_id="normal-1"))
        self.assertTrue(result["stored"])
        self.assertEqual(ChatMessage.objects.count(), 1)
        self.assertEqual(CaptureMarker.objects.count(), 0)

    def test_duplicate_event_is_idempotent(self):
        payload = self.event(message_id="same-1")
        ingest_event(payload)
        result = ingest_event(payload)
        self.assertTrue(result["duplicate"])
        self.assertEqual(ChatMessage.objects.count(), 1)

    def test_manager_reply_creates_capture_and_context(self):
        ingest_event(self.event(message_id="target-1"))
        result = ingest_event(
            self.event(
                message_id="command-1",
                sender_id="owner-1",
                sender_name="负责人",
                text="收录经验",
                reply_to_id="target-1",
            )
        )
        self.assertEqual(result["capture"], "created")
        marker = CaptureMarker.objects.get()
        self.assertIn(str(marker.target_id), marker.context_message_ids)

    def test_slash_capture_phrase_is_accepted_for_official_bot_commands(self):
        ingest_event(self.event(message_id="slash-target"))
        result = ingest_event(
            self.event(
                message_id="slash-command",
                sender_id="owner-1",
                sender_name="负责人",
                text="/收录经验",
                reply_to_id="slash-target",
            )
        )
        self.assertEqual(result["capture"], "created")
        self.assertEqual(CaptureMarker.objects.count(), 1)

    def test_non_manager_cannot_create_capture(self):
        ingest_event(self.event(message_id="target-2"))
        result = ingest_event(
            self.event(message_id="command-2", text="收录经验", reply_to_id="target-2")
        )
        self.assertEqual(result["capture"], "manager_required")
        self.assertFalse(CaptureMarker.objects.exists())

    def test_capture_requires_reply(self):
        result = ingest_event(
            self.event(message_id="command-3", sender_id="owner-1", text="收录经验")
        )
        self.assertEqual(result["capture"], "reply_required")

    def test_unknown_source_is_rejected(self):
        with self.assertRaises(PermissionError):
            ingest_event(self.event(group_id="other-group"))

    def test_reply_snapshot_can_supply_missing_target(self):
        payload = self.event(
            message_id="command-4",
            sender_id="owner-1",
            text="收录经验",
            reply_to_id="snapshot-target",
            reply_snapshot={
                "message_id": "snapshot-target",
                "sender_id": "member-2",
                "sender_name": "小链",
                "timestamp": "2026-09-22T11:59:00+08:00",
                "text": "先固定依赖版本再重装。",
            },
        )
        result = ingest_event(payload)
        self.assertEqual(result["capture"], "created")
        self.assertEqual(CaptureMarker.objects.get().target.external_id, "snapshot-target")

    def test_processing_builds_labeled_candidate_with_citation(self):
        ingest_event(self.event(message_id="target-5"))
        ingest_event(
            self.event(
                message_id="command-5",
                sender_id="owner-1",
                text="收录经验",
                reply_to_id="target-5",
            )
        )
        batch = process_pending()
        self.assertEqual(batch.status, WeeklyBatch.Status.COMPLETE)
        card = KnowledgeCard.objects.get()
        self.assertEqual(card.extraction_mode, "local_draft")
        self.assertTrue(card.citations.exists())
        self.assertEqual(CaptureMarker.objects.get().status, CaptureMarker.Status.PROCESSED)

    def test_processing_is_idempotent(self):
        ingest_event(self.event(message_id="target-6"))
        ingest_event(
            self.event(
                message_id="command-6",
                sender_id="owner-1",
                text="收录经验",
                reply_to_id="target-6",
            )
        )
        first = process_pending()
        self.assertIsNone(process_pending())
        self.assertEqual(WeeklyBatch.objects.count(), 1)
        self.assertEqual(KnowledgeCard.objects.count(), 1)
        self.assertIsNotNone(first)

    def test_only_confirmed_cards_are_searchable(self):
        ingest_event(self.event(message_id="target-7"))
        ingest_event(
            self.event(
                message_id="command-7",
                sender_id="owner-1",
                text="收录经验",
                reply_to_id="target-7",
            )
        )
        process_pending()
        card = KnowledgeCard.objects.get()
        self.assertFalse(search_cards("Python").exists())
        card.status = KnowledgeCard.Status.CONFIRMED
        card.save(update_fields=["status"])
        self.assertEqual(list(search_cards("Python")), [card])

    def test_ingest_endpoint_requires_valid_hmac(self):
        body = json.dumps(self.event(message_id="api-1"), ensure_ascii=False).encode("utf-8")
        response = self.client.post(
            reverse("collector_ingest"), body, content_type="application/json"
        )
        self.assertEqual(response.status_code, 403)
        timestamp = str(int(time.time()))
        response = self.client.post(
            reverse("collector_ingest"),
            body,
            content_type="application/json",
            HTTP_X_QINGLIAN_TIMESTAMP=timestamp,
            HTTP_X_QINGLIAN_SIGNATURE=sign_payload(timestamp, body),
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_manager_pages_are_available(self):
        for name in [
            "collector_dashboard",
            "collector_sources",
            "collector_simulate",
            "collector_inbox",
            "collector_knowledge",
            "collector_assistant",
        ]:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)


class CollectorSecurityTests(TestCase):
    def test_private_pages_require_manager(self):
        self.assertEqual(self.client.get(reverse("collector_dashboard")).status_code, 302)

    def test_ordinary_user_is_forbidden(self):
        user = get_user_model().objects.create_user("ordinary", password="strong-password-123")
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("collector_dashboard")).status_code, 403)
