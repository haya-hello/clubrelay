# Django生成的交接包增量迁移。 / Django-generated additive ClubRelay migration.

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('activities', '0004_activity_description_activity_event_status_and_more'),
        ('operations', '0004_material_version'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='analysisrun',
            name='purpose',
            field=models.CharField(choices=[('general', '普通分析'), ('handoff', '经验交接')], default='general', max_length=20),
        ),
        migrations.CreateModel(
            name='HandoffPack',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('title', models.CharField(max_length=160)),
                ('event_title', models.CharField(max_length=120)),
                ('scope', models.CharField(blank=True, max_length=500)),
                ('status', models.CharField(choices=[('draft', '草稿'), ('confirmed', '已审阅确认')], default='draft', max_length=20)),
                ('source_manifest', models.JSONField(default=list)),
                ('config_snapshot', models.JSONField(default=dict)),
                ('initialized_at', models.DateTimeField(blank=True, null=True)),
                ('lock_version', models.PositiveIntegerField(default=1)),
                ('reviewer_label', models.CharField(blank=True, max_length=100)),
                ('confirmed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('confirmed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='confirmed_handoffs', to=settings.AUTH_USER_MODEL)),
                ('created_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='created_handoffs', to=settings.AUTH_USER_MODEL)),
                ('event', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='handoff_packs', to='activities.activity')),
                ('parent', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='revisions', to='operations.handoffpack')),
                ('source_analysis', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='handoff_packs', to='operations.analysisrun')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='HandoffItem',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('position', models.PositiveIntegerField()),
                ('section', models.CharField(choices=[('practice', '可复用做法 / Practices'), ('pitfall', '踩坑提醒 / Pitfalls'), ('question', '待确认事项 / Open questions')], max_length=20)),
                ('title', models.CharField(max_length=160)),
                ('record', models.TextField(blank=True, max_length=2000)),
                ('suggestion', models.TextField(blank=True, max_length=2000)),
                ('conditions', models.TextField(blank=True, max_length=2000)),
                ('original', models.JSONField(default=dict)),
                ('citations', models.JSONField(default=list)),
                ('decision', models.CharField(choices=[('pending', '待审阅'), ('keep', '已审阅采用'), ('drop', '不采用')], default='pending', max_length=20)),
                ('edited', models.BooleanField(default=False)),
                ('reviewed_at', models.DateTimeField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('reviewed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
                ('pack', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='items', to='operations.handoffpack')),
            ],
            options={
                'ordering': ['position'],
                'constraints': [models.UniqueConstraint(fields=('pack', 'position'), name='handoff_item_position_unique')],
            },
        ),
    ]
