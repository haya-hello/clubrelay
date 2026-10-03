import json
import secrets
import uuid
from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand
from django.db import transaction
from knowledge.services import REVIEWER_GROUP, submit_entry, review_entry

class Command(BaseCommand):
    help = "创建虚构账号与示例；不重置已有密码或审核状态。 / Seed fictional demo without resetting existing state."

    def handle(self, *args, **options):
        credentials_path = settings.DATA_DIR / "demo-accounts.json"
        credentials = json.loads(credentials_path.read_text(encoding="utf-8")) if credentials_path.exists() else {}
        with transaction.atomic():
            group, _ = Group.objects.get_or_create(name=REVIEWER_GROUP)
            for username, name, reviewer in [("linxia", "林夏", False), ("zhouning", "周宁", False), ("suqing", "苏晴", True), ("chenmo", "陈默", True)]:
                user, created = User.objects.get_or_create(username=username, defaults={"first_name": name})
                if created:
                    password = secrets.token_urlsafe(15)
                    user.set_password(password)
                    user.save()
                    credentials[username] = {"name": name, "role": "负责人" if reviewer else "成员", "password": password}
                    if reviewer:
                        user.groups.add(group)
            member = User.objects.get(username="linxia")
            reviewer = User.objects.get(username="suqing")
            samples = [("a41e2920-d5f1-48c8-8b10-9d902e211111", "活动前设备检查清单", "process", "活动前一天确认投影、音响、电源和转接头。\n到场后先测试连接，再播放一段演示素材。\n准备离线备份；网络不可用时改用本地演示。", True), ("a41e2920-d5f1-48c8-8b10-9d902e222222", "报名页提交反馈检查", "case", "问题：填写后不知道是否提交成功。\n处理：成功时显示确认，失败时保留填写内容并说明原因。\n结果：在虚构测试中完成了成功与失败两条验证。", False)]
            from knowledge.models import Entry
            for token, title, category, body, approve in samples:
                if Entry.objects.filter(submit_token=token).exists():
                    continue
                entry = submit_entry(member, {"token": uuid.UUID(token), "title": title, "category": category, "body": body, "source": "虚构 AI 实践工作坊 · 演示资料", "applicability": "用于校内小型实践活动；请根据实际场地调整。"})
                if approve:
                    review_entry(reviewer, entry.pk, entry.version, "approve", "虚构演示审核：流程明确，适用边界已注明。")
            # 密码仅保存到被忽略的本机文件，不打印到日志。 / Save passwords only to ignored local file, never logs.
            credentials_path.write_text(json.dumps(credentials, ensure_ascii=False, indent=2), encoding="utf-8")
        self.stdout.write("虚构示例已就绪；账号信息保存在 var/demo-accounts.json。已有账号与状态未重置。")

