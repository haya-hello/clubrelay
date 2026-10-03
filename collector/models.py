import uuid

from django.conf import settings
from django.db import models


class ChatSource(models.Model):
    """授权的聊天来源。 / An authorized chat source."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    platform = models.CharField(max_length=30, default="qq_official")
    external_id = models.CharField(max_length=160)
    display_name = models.CharField(max_length=120)
    manager_ids = models.JSONField(default=list)
    capture_phrase = models.CharField(max_length=40, default="收录经验")
    consent_confirmed = models.BooleanField(default=False)
    enabled = models.BooleanField(default=True)
    retention_days = models.PositiveIntegerField(default=90)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["platform", "external_id"], name="collector_source_unique"
            )
        ]


class ChatIdentity(models.Model):
    """群内身份与本地成员分离。 / Keep chat identity separate from local people."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(ChatSource, on_delete=models.PROTECT, related_name="identities")
    external_id = models.CharField(max_length=160)
    display_name = models.CharField(max_length=120, blank=True)
    person = models.ForeignKey(
        "operations.Person", null=True, blank=True, on_delete=models.PROTECT
    )
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()

    class Meta:
        ordering = ["display_name", "external_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id"], name="collector_identity_unique"
            )
        ]


class ChatMessage(models.Model):
    """只保存业务需要的规范化消息。 / Store only normalized fields needed by the product."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(ChatSource, on_delete=models.PROTECT, related_name="messages")
    external_id = models.CharField(max_length=180)
    sender = models.ForeignKey(ChatIdentity, on_delete=models.PROTECT, related_name="messages")
    sent_at = models.DateTimeField()
    text = models.TextField(blank=True, max_length=20000)
    message_type = models.CharField(max_length=30, default="text")
    reply_to_external_id = models.CharField(max_length=180, blank=True)
    is_recalled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sent_at", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id"], name="collector_message_unique"
            )
        ]
        indexes = [
            models.Index(fields=["source", "sent_at"], name="collector_msg_time"),
            models.Index(
                fields=["source", "reply_to_external_id"], name="collector_msg_reply"
            ),
        ]


class CaptureMarker(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待整理"
        PROCESSED = "processed", "已生成候选"
        SKIPPED = "skipped", "已排除"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    target = models.OneToOneField(
        ChatMessage, on_delete=models.PROTECT, related_name="capture_marker"
    )
    command_message = models.ForeignKey(
        ChatMessage,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="issued_markers",
    )
    marked_by_external_id = models.CharField(max_length=160)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    context_message_ids = models.JSONField(default=list)
    context_truncated = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]


class WeeklyBatch(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "处理中"
        COMPLETE = "complete", "已完成"
        FAILED = "failed", "失败"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    fingerprint = models.CharField(max_length=64, unique=True)
    period_start = models.DateField()
    period_end = models.DateField()
    status = models.CharField(max_length=20, choices=Status.choices)
    marker_count = models.PositiveIntegerField(default=0)
    candidate_count = models.PositiveIntegerField(default=0)
    extraction_mode = models.CharField(max_length=30, default="local_draft")
    error = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class KnowledgeCard(models.Model):
    class Status(models.TextChoices):
        CANDIDATE = "candidate", "待审核"
        CONFIRMED = "confirmed", "已确认"
        REJECTED = "rejected", "已拒绝"
        RETIRED = "retired", "已下架"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    marker = models.ForeignKey(
        CaptureMarker, on_delete=models.PROTECT, related_name="cards"
    )
    batch = models.ForeignKey(
        WeeklyBatch, null=True, blank=True, on_delete=models.PROTECT, related_name="cards"
    )
    title = models.CharField(max_length=240)
    problem = models.TextField(max_length=4000)
    background = models.TextField(max_length=6000, blank=True)
    solution = models.TextField(max_length=6000, blank=True)
    result = models.TextField(max_length=4000, blank=True)
    pitfalls = models.TextField(max_length=4000, blank=True)
    conditions = models.TextField(max_length=4000, blank=True)
    contributors = models.JSONField(default=list)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.CANDIDATE
    )
    extraction_mode = models.CharField(max_length=30, default="local_draft")
    version = models.PositiveIntegerField(default=1)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reviewed_collector_cards",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]


class KnowledgeCitation(models.Model):
    card = models.ForeignKey(KnowledgeCard, on_delete=models.CASCADE, related_name="citations")
    message = models.ForeignKey(ChatMessage, on_delete=models.PROTECT, related_name="citations")
    quote = models.TextField(max_length=2000)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["card", "message"], name="collector_card_message_unique"
            )
        ]


class CollectorAudit(models.Model):
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT
    )
    action = models.CharField(max_length=60)
    object_id = models.CharField(max_length=180)
    detail = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
