from django.core.management.base import BaseCommand

from collector.services import process_pending


class Command(BaseCommand):
    help = "处理本周已标记的 QQ 经验消息 / Process pending QQ experience markers"

    def handle(self, *args, **options):
        batch = process_pending()
        if batch is None:
            self.stdout.write("No pending capture markers.")
            return
        self.stdout.write(
            self.style.SUCCESS(
                f"Batch {batch.id}: markers={batch.marker_count}, candidates={batch.candidate_count}"
            )
        )
