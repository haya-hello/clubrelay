import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("operations", "0004_material_version"),
    ]
    operations = [
        migrations.CreateModel(
            name="ChatSource",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("platform", models.CharField(default="qq_official", max_length=30)),
                ("external_id", models.CharField(max_length=160)),
                ("display_name", models.CharField(max_length=120)),
                ("manager_ids", models.JSONField(default=list)),
                ("capture_phrase", models.CharField(default="收录经验", max_length=40)),
                ("consent_confirmed", models.BooleanField(default=False)),
                ("enabled", models.BooleanField(default=True)),
                ("retention_days", models.PositiveIntegerField(default=90)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["display_name"]},
        ),
        migrations.AddConstraint(
            model_name="chatsource",
            constraint=models.UniqueConstraint(fields=("platform", "external_id"), name="collector_source_unique"),
        ),
        migrations.CreateModel(
            name="ChatIdentity",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("external_id", models.CharField(max_length=160)),
                ("display_name", models.CharField(blank=True, max_length=120)),
                ("first_seen_at", models.DateTimeField()),
                ("last_seen_at", models.DateTimeField()),
                ("person", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to="operations.person")),
                ("source", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="identities", to="collector.chatsource")),
            ],
            options={"ordering": ["display_name", "external_id"]},
        ),
        migrations.AddConstraint(
            model_name="chatidentity",
            constraint=models.UniqueConstraint(fields=("source", "external_id"), name="collector_identity_unique"),
        ),
        migrations.CreateModel(
            name="ChatMessage",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("external_id", models.CharField(max_length=180)),
                ("sent_at", models.DateTimeField()),
                ("text", models.TextField(blank=True, max_length=20000)),
                ("message_type", models.CharField(default="text", max_length=30)),
                ("reply_to_external_id", models.CharField(blank=True, max_length=180)),
                ("is_recalled", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("sender", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="messages", to="collector.chatidentity")),
                ("source", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="messages", to="collector.chatsource")),
            ],
            options={"ordering": ["sent_at", "created_at"]},
        ),
        migrations.AddConstraint(
            model_name="chatmessage",
            constraint=models.UniqueConstraint(fields=("source", "external_id"), name="collector_message_unique"),
        ),
        migrations.AddIndex(model_name="chatmessage", index=models.Index(fields=["source", "sent_at"], name="collector_msg_time")),
        migrations.AddIndex(model_name="chatmessage", index=models.Index(fields=["source", "reply_to_external_id"], name="collector_msg_reply")),
        migrations.CreateModel(
            name="CaptureMarker",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("marked_by_external_id", models.CharField(max_length=160)),
                ("status", models.CharField(choices=[("pending", "待整理"), ("processed", "已生成候选"), ("skipped", "已排除")], default="pending", max_length=20)),
                ("context_message_ids", models.JSONField(default=list)),
                ("context_truncated", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("command_message", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="issued_markers", to="collector.chatmessage")),
                ("target", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="capture_marker", to="collector.chatmessage")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="WeeklyBatch",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("fingerprint", models.CharField(max_length=64, unique=True)),
                ("period_start", models.DateField()),
                ("period_end", models.DateField()),
                ("status", models.CharField(choices=[("running", "处理中"), ("complete", "已完成"), ("failed", "失败")], max_length=20)),
                ("marker_count", models.PositiveIntegerField(default=0)),
                ("candidate_count", models.PositiveIntegerField(default=0)),
                ("extraction_mode", models.CharField(default="local_draft", max_length=30)),
                ("error", models.CharField(blank=True, max_length=300)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="KnowledgeCard",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("title", models.CharField(max_length=240)),
                ("problem", models.TextField(max_length=4000)),
                ("background", models.TextField(blank=True, max_length=6000)),
                ("solution", models.TextField(blank=True, max_length=6000)),
                ("result", models.TextField(blank=True, max_length=4000)),
                ("pitfalls", models.TextField(blank=True, max_length=4000)),
                ("conditions", models.TextField(blank=True, max_length=4000)),
                ("contributors", models.JSONField(default=list)),
                ("status", models.CharField(choices=[("candidate", "待审核"), ("confirmed", "已确认"), ("rejected", "已拒绝"), ("retired", "已下架")], default="candidate", max_length=20)),
                ("extraction_mode", models.CharField(default="local_draft", max_length=30)),
                ("version", models.PositiveIntegerField(default=1)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("batch", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="cards", to="collector.weeklybatch")),
                ("marker", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="cards", to="collector.capturemarker")),
                ("reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="reviewed_collector_cards", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="KnowledgeCitation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quote", models.TextField(max_length=2000)),
                ("position", models.PositiveIntegerField(default=0)),
                ("card", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="citations", to="collector.knowledgecard")),
                ("message", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="citations", to="collector.chatmessage")),
            ],
            options={"ordering": ["position", "id"]},
        ),
        migrations.AddConstraint(
            model_name="knowledgecitation",
            constraint=models.UniqueConstraint(fields=("card", "message"), name="collector_card_message_unique"),
        ),
        migrations.CreateModel(
            name="CollectorAudit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action", models.CharField(max_length=60)),
                ("object_id", models.CharField(max_length=180)),
                ("detail", models.CharField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
