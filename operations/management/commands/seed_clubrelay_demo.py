"""仅导入隔离演示材料，不启用模型。 / Import isolated fictional examples without enabling a model."""
import uuid
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.core.files.uploadedfile import SimpleUploadedFile
from operations.ingestion import archive_files
from operations.security import MANAGER_GROUP


class Command(BaseCommand):
    help = "准备ClubRelay虚构演示；仅允许明确隔离的数据目录。 / Prepare fictional ClubRelay data in an isolated directory."

    def handle(self, *args, **options):
        if settings.DATA_DIR.resolve() == (settings.BASE_DIR / "var").resolve() or "clubrelay" not in str(settings.DATA_DIR).lower():
            raise CommandError("请先设置带clubrelay标识的独立QINGLIAN_DATA_DIR，不能在原工作库执行。")
        call_command("seed_manager")
        actor = User.objects.filter(is_active=True, groups__name=MANAGER_GROUP).first()
        root = settings.BASE_DIR / "examples" / "clubrelay"
        files = [SimpleUploadedFile(path.name, path.read_bytes(), content_type="text/markdown") for path in sorted(root.glob("0*.md"))]
        token = uuid.uuid5(uuid.NAMESPACE_URL, "clubrelay-fictional-demo-v1")
        batch = archive_files(actor, {"token": token, "event": None, "title": "虚构样例 · 校园创客分享会", "recorded_date": None}, files, [])
        self.stdout.write(f"虚构活动已准备：/events/{batch.event_id}/；本次初始化未启用模型或发送资料，已有设置保持原样。")
