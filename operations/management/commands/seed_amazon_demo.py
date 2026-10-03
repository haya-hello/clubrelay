"""准备明确标注的英文虚构交接样例，不调用模型。 / Seed a labelled English fictional handover without calling a model."""
import uuid
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.utils import timezone
from operations.ingestion import archive_files
from operations.security import MANAGER_GROUP
from operations.analysis import _text_hash
from operations.models import AnalysisRun, HandoffPack, HandoffItem


class Command(BaseCommand):
    help = "Seed fictional Amazon demo without enabling a model / 创建虚构演示，保持模型关闭"

    @transaction.atomic
    def handle(self, *args, **options):
        if settings.DATA_DIR.resolve() == (settings.BASE_DIR / "var").resolve() or "clubrelay" not in str(settings.DATA_DIR).lower():
            raise CommandError("Use an isolated QINGLIAN_DATA_DIR containing clubrelay / 请使用含clubrelay的隔离目录")
        call_command("seed_manager")
        actor=User.objects.filter(is_active=True,groups__name=MANAGER_GROUP).first()
        root=settings.BASE_DIR / "examples" / "amazon"
        files=[SimpleUploadedFile(p.name,p.read_bytes(),content_type="text/markdown") for p in sorted(root.glob("0*.md"))]
        token=uuid.uuid5(uuid.NAMESPACE_URL,"clubrelay-amazon-fixture-v1")
        batch=archive_files(actor,{"token":token,"event":None,"title":"Fictional campus maker meetup","recorded_date":None},files,[])
        pack_id=uuid.uuid5(uuid.NAMESPACE_URL,"clubrelay-amazon-reviewed-fixture-v1")
        if not HandoffPack.objects.filter(pk=pack_id).exists():
            materials=list(batch.event.materials.order_by("original_name"))
            manifest=[{"id":str(m.pk),"event_id":str(m.event_id),"title":m.original_name,"text":m.text,"sha256":m.sha256,"text_hash":_text_hash(m.text)} for m in materials]
            run=AnalysisRun.objects.create(id=pack_id,event=batch.event,actor=actor,question="Manually authored test fixture; NOT a model result.",purpose="handoff",status="complete",fingerprint="manual-fixture",config_fingerprint="manual-fixture",sources=manifest,result={"items":[]})
            pack=HandoffPack.objects.create(id=pack_id,event=batch.event,source_analysis=run,title="Maker meetup · Reviewed fictional handover",event_title=batch.event.title,scope="Three fictional files; technical demonstration only, pending real organizer acceptance.",status="confirmed",source_manifest=manifest,config_snapshot={"mode":"fixture","model":"manually-authored-not-AI"},created_by=actor,confirmed_by=actor,reviewer_label="Authored demo fixture — not a real member review",confirmed_at=timezone.now(),initialized_at=timezone.now())
            def citation(index,quote):return {"source_id":str(materials[index].pk),"quote":quote}
            rows=[
                ("practice","Separate the entrance tasks","The event recorded 23 attendees, not the 40 expected in the plan.","Place check-in and the resource QR code separately; assign a volunteer to each needed task.","No obvious congestion was recorded for 23 attendees. The same setup is not proven for 80.",[citation(0,"The plan expected 40 attendees in room A101."),citation(2,"The event took place in B203 with 23 recorded attendees."),citation(2,"Volunteers separated check-in from the resource QR code; no obvious entrance congestion was recorded.")]),
                ("pitfall","Check projection before opening","A connection issue delayed the opening by eight minutes.","Reserve setup time to check the projector, sound and cables.","The fault cause was not verified; checking earlier is a suggestion, not a proven fix.",[citation(2,"A projector connection issue delayed the opening by eight minutes."),citation(2,"The connection worked after a cable was replaced, but the technical cause was not verified.")]),
                ("practice","Confirm the venue update reached people","The venue changed from A101 to B203.","For a future venue change, update the announcement and confirm receipt with participants.","The existing record does not prove everyone saw the notice; do not send an expired event reminder.",[citation(1,"The venue changed from A101 to B203; the planned start time did not change."),citation(1,"The record does not say whether every registered person saw the update.")]),
                ("question","Find the equipment contact","","Ask the previous organizer who lends the equipment and where the spare cable is kept.","The lender, spare-cable location and future borrowing process remain unknown.",[]),
            ]
            for position,row in enumerate(rows,1):
                section,title,record,suggestion,conditions,citations=row
                HandoffItem.objects.create(pack=pack,position=position,section=section,title=title,record=record,suggestion=suggestion,conditions=conditions,citations=citations,original={"fixture":True},decision="keep",reviewed_by=actor,reviewed_at=timezone.now())
        self.stdout.write(f"Fictional fixture ready: /handoffs/{pack_id}/briefing/?lang=en. No model called or enabled.")
