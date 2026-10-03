from django.core.management.base import BaseCommand
from django.utils import timezone
from operations.models import AnalysisRun
class Command(BaseCommand):
    help="启动前标记被中断请求，不自动重新外发。 / Mark interrupted requests without resending data."
    def handle(self,*args,**options):
        count=AnalysisRun.objects.filter(status__in=["pending","running"]).update(status="failed",error="服务重启中断了请求，原件仍在；请确认后重新分析。",updated_at=timezone.now())
        self.stdout.write(f"已标记 {count} 个中断请求；未重发任何资料。")

