import json
import secrets
from django.conf import settings
from django.contrib.auth.models import User, Group
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from operations.security import MANAGER_GROUP

class Command(BaseCommand):
    help="仅在空库创建初始负责人，不恢复被撤销权限。 / Create the initial owner only in an empty database."
    @transaction.atomic
    def handle(self,*args,**options):
        group,_=Group.objects.get_or_create(name=MANAGER_GROUP)
        if User.objects.filter(is_active=True,groups=group).exists():
            self.stdout.write("现有负责人账号保持不变。")
            return
        if User.objects.exists():
            raise CommandError("已有账号但无有效负责人；为避免恢复已撤销权限，请由管理员明确处理。")
        password=secrets.token_urlsafe(18)
        user=User.objects.create_user(username="manager",first_name="负责人",password=password)
        user.groups.add(group)
        path=settings.DATA_DIR/"demo-accounts.json"
        path.write_text(json.dumps({"manager":{"name":"负责人","role":"负责人","password":password}},ensure_ascii=False,indent=2),encoding="utf-8")
        self.stdout.write("初始负责人已创建；随机密码仅保存在 var/demo-accounts.json。")

