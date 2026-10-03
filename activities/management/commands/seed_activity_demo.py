import uuid
from datetime import datetime
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from knowledge.models import Entry
from activities.models import Activity, Task
from activities.services import create_activity, publish_task
from knowledge.services import is_reviewer

class Command(BaseCommand):
    help = "只新增缺失的虚构活动样例，不重置任务状态。 / Add missing fictional examples without resetting tasks."

    @transaction.atomic
    def handle(self, *args, **options):
        token = uuid.UUID("9367f755-c7cb-4153-8e22-000000000001")
        task_tokens = ["9367f755-c7cb-4153-8e22-000000000002", "9367f755-c7cb-4153-8e22-000000000003"]
        if Activity.objects.filter(token=token).exists() and Task.objects.filter(token__in=task_tokens).count() == 2:
            self.stdout.write("S3 样例已存在，所有账号和任务状态保持不变。")
            return
        try:
            lead = User.objects.get(username="suqing", is_active=True)
            member = User.objects.get(username="linxia", is_active=True)
            second = User.objects.get(username="zhouning", is_active=True)
        except User.DoesNotExist:
            self.stdout.write("原演示账号缺失或停用，跳过活动样例，不恢复账号；可手动创建活动。")
            return
        if not is_reviewer(lead):
            self.stdout.write("原演示负责人权限已变化，跳过活动样例，不恢复权限。")
            return
        activity = Activity.objects.filter(token=token).first()
        if activity is None:
            activity = create_activity(lead, {"token":token,"title":"AI 实践工作坊（S3 演示）","kind":"activity","objective":"用 AI 完成一个可展示的小作品，并记录自己的实践过程。","audience":"青禾 AI 社团入门成员","scheduled_at":timezone.make_aware(datetime(2026,9,26,14,0)),"constraints":"虚构演示活动；场地和预算待确认，不对外发布。","plan":"先说明目标，再分别准备报名原型和现场设备。","participants":[member,second]})
        reference = Entry.objects.filter(submit_token="a41e2920-d5f1-48c8-8b10-9d902e211111",status="approved").first()
        for suffix,title,assignee,deliverable in [("2","报名网页原型",member,"一个可填写并展示提交反馈的网页原型及方法说明"),("3","现场设备检查",None,"投影、电源、转接头与离线备份的检查清单")]:
            task_token = uuid.UUID("9367f755-c7cb-4153-8e22-00000000000"+suffix)
            if Task.objects.filter(token=task_token).exists():
                continue
            publish_task(lead,activity,{"token":task_token,"title":title,"objective":"为虚构工作坊做好执行准备。","deliverable":deliverable,"acceptance":"每项检查结果有记录，未解决问题有明确说明。","due_at":timezone.make_aware(datetime(2026,9,20,18,0)),"assignee":assignee,"reference":reference if assignee is None else None})
        self.stdout.write("S3 虚构样例已就绪；已有活动、认领及接受状态未重置。")
