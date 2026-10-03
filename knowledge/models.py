import uuid
from django.conf import settings
from django.db import models

class Entry(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待审核"
        APPROVED = "approved", "已审核"
        REJECTED = "rejected", "已退回"
        WITHDRAWN = "withdrawn", "已下架"

    class Category(models.TextChoices):
        PROCESS = "process", "正式流程"
        TUTORIAL = "tutorial", "实践教程"
        CASE = "case", "成败案例"
        HANDOVER = "handover", "岗位经验"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submit_token = models.UUIDField(unique=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    title = models.CharField(max_length=120)
    category = models.CharField(max_length=20, choices=Category.choices)
    body = models.TextField()
    source = models.CharField(max_length=300)
    applicability = models.CharField(max_length=500)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    version = models.PositiveIntegerField(default=1)
    review_reason = models.CharField(max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

class Revision(models.Model):
    entry = models.ForeignKey(Entry, on_delete=models.PROTECT, related_name="revisions")
    number = models.PositiveIntegerField()
    title = models.CharField(max_length=120)
    category = models.CharField(max_length=20)
    body = models.TextField()
    source = models.CharField(max_length=300)
    applicability = models.CharField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-number"]
        constraints = [models.UniqueConstraint(fields=["entry", "number"], name="unique_entry_revision")]

class ReviewEvent(models.Model):
    entry = models.ForeignKey(Entry, on_delete=models.PROTECT, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    version = models.PositiveIntegerField()
    action = models.CharField(max_length=20, choices=[("submit", "提交审核"), ("resubmit", "修改重提"), ("approve", "审核通过"), ("reject", "退回修改"), ("withdraw", "下架")])
    reason = models.CharField(max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

