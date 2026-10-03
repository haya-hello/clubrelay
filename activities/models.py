import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone

class Activity(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token = models.UUIDField(unique=True)
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_activities")
    title = models.CharField(max_length=120)
    kind = models.CharField(max_length=20, choices=[("activity", "活动"), ("project", "项目")], default="activity")
    objective = models.TextField(max_length=3000)
    audience = models.CharField(max_length=300)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    recorded_date = models.DateField(null=True, blank=True)
    description = models.TextField(blank=True, max_length=5000)
    event_status = models.CharField(max_length=20, choices=[("unknown","待确认"),("planned","计划活动"),("held","已举行")], default="unknown")
    constraints = models.TextField(max_length=3000)
    plan = models.TextField(max_length=10000, blank=True)
    stage = models.CharField(max_length=20, choices=[("planning","筹备中"),("active","执行中"),("wrapup","收尾中"),("archived","已归档")], default="planning")
    stage_version = models.PositiveIntegerField(default=1)
    participants = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="campus_activities")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

class Task(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "待认领"
        WAITING = "waiting", "待接受"
        ACTIVE = "active", "进行中"
        REVIEW = "review", "待验收"
        CHANGES = "changes", "需修改"
        DONE = "done", "已完成"
        CANCELLED = "cancelled", "已取消"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token = models.UUIDField(unique=True)
    activity = models.ForeignKey(Activity, on_delete=models.PROTECT, related_name="tasks")
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_tasks")
    title = models.CharField(max_length=120)
    objective = models.TextField(max_length=3000)
    deliverable = models.TextField(max_length=3000)
    acceptance = models.TextField(max_length=3000)
    due_at = models.DateTimeField()
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="campus_tasks")
    reference = models.ForeignKey("knowledge.Entry", null=True, blank=True, on_delete=models.PROTECT)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    blocked_reason = models.CharField(max_length=1000, blank=True)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["due_at", "created_at"]
        constraints = [
            models.CheckConstraint(condition=~models.Q(status="open") | models.Q(assignee__isnull=True), name="open_task_unassigned"),
            models.CheckConstraint(condition=~models.Q(status__in=["waiting", "active", "review", "changes", "done"]) | models.Q(assignee__isnull=False), name="accepted_task_assigned"),
        ]

    @property
    def overdue(self):
        return self.status not in [self.Status.CANCELLED, self.Status.DONE] and self.due_at < timezone.now()

class TaskEvent(models.Model):
    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=20, choices=[("publish", "发布任务"), ("claim", "主动认领"), ("accept", "接受任务"), ("decline", "暂不接受"), ("block", "报告受阻"), ("unblock", "解除受阻"), ("cancel", "取消任务"), ("submit", "提交成果"), ("approve", "验收通过"), ("return", "成果退回"), ("revoke", "撤销验收")])
    version = models.PositiveIntegerField()
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    reason = models.CharField(max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]


def delivery_path(instance, filename):
    from pathlib import PurePosixPath
    # 随机路径避免覆盖和目录穿越，原名仅作下载提示。 / Random paths prevent overwrite and traversal; original name is display-only.
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    return f"deliveries/{uuid.uuid4().hex}{suffix}"

class Submission(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待验收"
        APPROVED = "approved", "已验收"
        RETURNED = "returned", "已退回"
        SUPERSEDED = "superseded", "已由新版替代"
        REVOKED = "revoked", "验收已撤销"
        CANCELLED = "cancelled", "任务已取消"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token = models.UUIDField(unique=True)
    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="submissions")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    number = models.PositiveIntegerField()
    summary = models.TextField(max_length=3000)
    result_text = models.TextField(max_length=20000, blank=True)
    result_url = models.URLField(max_length=2000, blank=True)
    method = models.TextField(max_length=3000)
    contribution = models.TextField(max_length=3000)
    attachment = models.FileField(upload_to=delivery_path, blank=True)
    original_name = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-number"]
        constraints = [models.UniqueConstraint(fields=["task","number"], name="unique_task_submission")]

class AcceptanceEvent(models.Model):
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="reviews")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=20, choices=[("approve","验收通过"),("return","退回修改"),("revoke","撤销验收")])
    reason = models.CharField(max_length=1000)
    practice = models.BooleanField(default=False)
    contribution = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

class Achievement(models.Model):
    task = models.OneToOneField(Task, on_delete=models.PROTECT, related_name="achievement")
    submission = models.OneToOneField(Submission, on_delete=models.PROTECT)
    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    practice = models.BooleanField(default=False)
    contribution = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

class Retrospective(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        CONFIRMED = "confirmed", "已确认"
        SUPERSEDED = "superseded", "已由新版替代"
        WITHDRAWN = "withdrawn", "已撤回"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    activity = models.ForeignKey(Activity, on_delete=models.PROTECT, related_name="retrospectives")
    number = models.PositiveIntegerField()
    revision = models.PositiveIntegerField(default=1)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    facts = models.TextField(max_length=5000, blank=True)
    differences = models.TextField(max_length=5000, blank=True)
    hypotheses = models.TextField(max_length=5000, blank=True)
    improvements = models.TextField(max_length=5000, blank=True)
    applicability = models.TextField(max_length=3000, blank=True)
    snapshot = models.JSONField(default=dict)
    fingerprint = models.CharField(max_length=64)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(fields=["activity","number"], name="unique_activity_retro_number"),
            models.UniqueConstraint(fields=["activity"], condition=models.Q(status="draft"), name="one_draft_retro_per_activity"),
        ]

class RetroEvent(models.Model):
    retrospective = models.ForeignKey(Retrospective, on_delete=models.PROTECT, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=20, choices=[("create","创建草稿"),("save","保存草稿"),("refresh","刷新事实快照"),("confirm","确认复盘"),("withdraw","撤回复盘"),("export","送知识审核")])
    revision = models.PositiveIntegerField()
    reason = models.CharField(max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["-created_at","-pk"]

class KnowledgeDerivation(models.Model):
    retrospective = models.OneToOneField(Retrospective, on_delete=models.PROTECT, related_name="knowledge_card")
    entry = models.OneToOneField("knowledge.Entry", on_delete=models.PROTECT, related_name="derivation")
    created_at = models.DateTimeField(auto_now_add=True)

class ActivityStageEvent(models.Model):
    activity = models.ForeignKey(Activity, on_delete=models.PROTECT, related_name="stage_events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    before = models.CharField(max_length=20)
    after = models.CharField(max_length=20)
    reason = models.CharField(max_length=1000)
    created_at = models.DateTimeField(auto_now_add=True)
