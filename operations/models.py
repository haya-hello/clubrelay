import uuid
from django.conf import settings
from django.db import models

def archive_path(instance, filename):
    from pathlib import PurePosixPath
    return f"archive/{uuid.uuid4().hex}{PurePosixPath(filename).suffix.lower()[:12]}"

class ImportBatch(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey("activities.Activity", on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    results = models.JSONField(default=list)

class Material(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey("activities.Activity", on_delete=models.PROTECT, related_name="materials")
    batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT)
    parent = models.ForeignKey("self",null=True,blank=True,on_delete=models.PROTECT,related_name="children")
    file = models.FileField(upload_to=archive_path)
    original_name = models.CharField(max_length=255)
    source_path = models.CharField(max_length=500, blank=True)
    sha256 = models.CharField(max_length=64)
    size = models.PositiveBigIntegerField()
    parse_status = models.CharField(max_length=20,choices=[("pending","待解析"),("parsed","已提取正文"),("unsupported","仅归档"),("failed","解析失败")],default="pending")
    text = models.TextField(blank=True)
    segments = models.JSONField(default=list)
    tables = models.JSONField(default=list)
    note = models.TextField(blank=True)
    excluded = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["event","sha256"],name="event_material_hash_unique")]

class Person(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_id = models.CharField(max_length=120,unique=True)
    display_name = models.CharField(max_length=100)
    role = models.CharField(max_length=100,blank=True)
    interests = models.CharField(max_length=500,blank=True)
    joined_on = models.DateField(null=True,blank=True)
    notes = models.TextField(blank=True,max_length=5000)
    is_roster = models.BooleanField(default=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["display_name","external_id"]

class Alias(models.Model):
    member = models.ForeignKey(Person,on_delete=models.CASCADE,related_name="aliases")
    value = models.CharField(max_length=120)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["member","value"],name="member_alias_unique")]

class Mention(models.Model):
    member = models.ForeignKey(Person,on_delete=models.CASCADE,related_name="mentions")
    material = models.ForeignKey(Material,on_delete=models.CASCADE,related_name="mentions")
    anchor = models.CharField(max_length=100)
    excerpt = models.TextField()
    class Meta:
        constraints = [models.UniqueConstraint(fields=["member","material"],name="material_member_mention_unique")]

class Evidence(models.Model):
    class Kind(models.TextChoices):
        PARTICIPATION="participation","参与"
        DELIVERY="delivery","交付"
        HELP="help","协作帮助"
        INITIATIVE="initiative","主动推进"
        COMMUNICATION="communication","沟通记录"
    class Confidence(models.TextChoices):
        VERIFIED="verified","负责人核实"
        SELF_REPORT="self_report","材料自述"
        CANDIDATE="candidate","待核验线索"
    id = models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    member = models.ForeignKey(Person,on_delete=models.PROTECT,related_name="evidence")
    event = models.ForeignKey("activities.Activity",on_delete=models.PROTECT,related_name="observations")
    title = models.CharField(max_length=250)
    kind = models.CharField(max_length=20,choices=Kind.choices)
    confidence = models.CharField(max_length=20,choices=Confidence.choices,default=Confidence.CANDIDATE)
    occurred_on = models.DateField(null=True,blank=True)
    material = models.ForeignKey(Material,null=True,blank=True,on_delete=models.PROTECT)
    anchor = models.CharField(max_length=100,blank=True)
    quote = models.TextField(max_length=3000)
    note = models.TextField(max_length=3000,blank=True)
    dedupe_key = models.CharField(max_length=64,unique=True)
    duplicate_of = models.ForeignKey("self",null=True,blank=True,on_delete=models.PROTECT)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["-occurred_on","-created_at"]

class Audit(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    action = models.CharField(max_length=60)
    object_id = models.CharField(max_length=100)
    detail = models.CharField(max_length=500,blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

class ModelConfiguration(models.Model):
    mode = models.CharField(max_length=20,choices=[("disabled","未启用"),("local","本机模型"),("cloud","云端模型")],default="disabled")
    base_url = models.URLField(blank=True)
    model = models.CharField(max_length=100,blank=True)
    enabled = models.BooleanField(default=False)
    local_auth = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

class AnalysisPermission(models.Model):
    event = models.OneToOneField("activities.Activity",on_delete=models.PROTECT)
    config_fingerprint = models.CharField(max_length=64)
    material_ids = models.JSONField(default=list)
    auto_future = models.BooleanField(default=False)
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    granted_at = models.DateTimeField(auto_now=True)

class AnalysisRun(models.Model):
    id = models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    event = models.ForeignKey("activities.Activity",null=True,blank=True,on_delete=models.PROTECT,related_name="analyses")
    member = models.ForeignKey(Person,null=True,blank=True,on_delete=models.PROTECT,related_name="analyses")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    question = models.CharField(max_length=1000)
    purpose = models.CharField(max_length=20, choices=[("general", "普通分析"), ("handoff", "经验交接")], default="general")
    start_date = models.DateField(null=True,blank=True)
    end_date = models.DateField(null=True,blank=True)
    status = models.CharField(max_length=20,choices=[("pending","等待处理"),("running","分析中"),("complete","已完成"),("failed","分析失败"),("stale","来源已变化"),("cancelled","已取消")],default="pending")
    fingerprint = models.CharField(max_length=64)
    config_fingerprint = models.CharField(max_length=64)
    sources = models.JSONField(default=list)
    result = models.JSONField(default=dict)
    error = models.CharField(max_length=300,blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        ordering=["-created_at"]


class HandoffPack(models.Model):
    """交接版本与模型结果分离。 / Keep reviewed handovers separate from model results."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey("activities.Activity", on_delete=models.PROTECT, related_name="handoff_packs")
    source_analysis = models.ForeignKey(AnalysisRun, on_delete=models.PROTECT, related_name="handoff_packs")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="revisions")
    title = models.CharField(max_length=160)
    event_title = models.CharField(max_length=120)
    scope = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=20, choices=[("draft", "草稿"), ("confirmed", "已审阅确认")], default="draft")
    source_manifest = models.JSONField(default=list)
    config_snapshot = models.JSONField(default=dict)
    initialized_at = models.DateTimeField(null=True, blank=True)
    lock_version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_handoffs")
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="confirmed_handoffs")
    reviewer_label = models.CharField(max_length=100, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]


class HandoffItem(models.Model):
    """初稿与人工修订分别保存，引文只读。 / Preserve original drafts and edits; citations are read-only."""
    SECTION_CHOICES = [("practice", "可复用做法 / Practices"), ("pitfall", "踩坑提醒 / Pitfalls"), ("question", "待确认事项 / Open questions")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    pack = models.ForeignKey(HandoffPack, on_delete=models.CASCADE, related_name="items")
    position = models.PositiveIntegerField()
    section = models.CharField(max_length=20, choices=SECTION_CHOICES)
    title = models.CharField(max_length=160)
    record = models.TextField(blank=True, max_length=2000)
    suggestion = models.TextField(blank=True, max_length=2000)
    conditions = models.TextField(blank=True, max_length=2000)
    original = models.JSONField(default=dict)
    citations = models.JSONField(default=list)
    decision = models.CharField(max_length=20, choices=[("pending", "待审阅"), ("keep", "已审阅采用"), ("drop", "不采用")], default="pending")
    edited = models.BooleanField(default=False)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position"]
        constraints = [models.UniqueConstraint(fields=["pack", "position"], name="handoff_item_position_unique")]


class BriefingSession(models.Model):
    """负责人专属的会前准备，不修改原交接版本。 / Owner-scoped preparation without changing the handover."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    pack = models.ForeignKey(HandoffPack, on_delete=models.PROTECT, related_name="briefings")
    context = models.JSONField(default=dict)
    turns = models.JSONField(default=list)
    selected = models.JSONField(default=list)
    revision = models.PositiveIntegerField(default=1)
    pending = models.UUIDField(null=True, blank=True)
    pending_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
