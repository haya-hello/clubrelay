import io
import json
import tempfile
import uuid
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import patch
from django.contrib.auth.models import User, Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from activities.models import Activity
from .models import Material, Person, Alias, Evidence, ImportBatch, AnalysisRun
from .ingestion import archive_files, parse_saved, set_excluded, retry_zip, link_mentions
from .people import import_roster, save_evidence, active_evidence

class WorkspaceWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager=User.objects.create_user("workspace_manager",password="fictional-test-only",first_name="负责人")
        cls.member_user=User.objects.create_user("workspace_member",password="fictional-test-only")
        cls.manager.groups.add(Group.objects.create(name="社团负责人"))
        cls.event=Activity.objects.create(creator=cls.manager,token=uuid.uuid4(),title="虚构活动",objective="",audience="",constraints="",scheduled_at=None,recorded_date=date(2026,9,20))
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.override=override_settings(DATA_DIR=Path(self.temp.name),MEDIA_ROOT=Path(self.temp.name)/"uploads",FILE_UPLOAD_TEMP_DIR=self.temp.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.client.force_login(self.manager)
    def ingest(self,name="notes.txt",content="林夏完成了演示网页。"):
        file=SimpleUploadedFile(name,content.encode() if isinstance(content,str) else content)
        batch=archive_files(self.manager,{"token":uuid.uuid4(),"event":self.event},[file])
        return Material.objects.get(pk=batch.results[0]["material_id"])
    def person(self,id="P1",name="林夏"):
        return Person.objects.create(external_id=id,display_name=name)
    def test_new_pages_are_real_and_render(self):
        for path in ["/","/events/","/import/","/members/","/roster/","/analysis/","/settings/"]:
            self.assertEqual(self.client.get(path).status_code,200,path)
    def test_date_edit_input_preserves_iso_value(self):
        response=self.client.get(reverse("ops_event",args=[self.event.pk]))
        self.assertContains(response,'value="2026-09-20"')
    def test_move_material_preserves_file_and_invalidates_old_association(self):
        mat=self.ingest()
        path=mat.file.name
        target=Activity.objects.create(token=uuid.uuid4(),creator=self.manager,title="正确活动")
        response=self.client.post(reverse("ops_move_material",args=[mat.pk]),{"event":target.pk,"version":1})
        self.assertEqual(response.status_code,302)
        mat.refresh_from_db()
        self.assertEqual(mat.event_id,target.pk)
        self.assertEqual(mat.file.name,path)
        self.assertEqual(mat.version,2)
    def test_old_members_denied_all_old_and_new_business_routes(self):
        self.client.force_login(self.member_user)
        for path in ["/","/events/","/import/","/members/","/analysis/","/activities/","/growth/","/assistant/","/knowledge/","/review/","/submissions/new/","/acceptance/"]:
            self.assertEqual(self.client.get(path).status_code,403,path)
    def test_old_member_cannot_log_in(self):
        self.client.logout()
        response=self.client.post("/login/",{"username":"workspace_member","password":"fictional-test-only"})
        self.assertEqual(response.status_code,200)
        self.assertContains(response,"仅负责人")
    def test_manager_login_redirects_to_new_dashboard(self):
        self.client.logout()
        response=self.client.post("/login/",{"username":"workspace_manager","password":"fictional-test-only"})
        self.assertRedirects(response,"/")
    def test_batch_stores_original_and_extracts_text(self):
        mat=self.ingest()
        self.assertEqual(mat.parse_status,"parsed")
        self.assertIn("林夏",mat.text)
        with mat.file.open("rb") as raw:
            self.assertIn("林夏",raw.read().decode())
    def test_http_multiple_files(self):
        response=self.client.post("/import/",{"token":uuid.uuid4(),"event":self.event.pk,"files":[SimpleUploadedFile("a.txt",b"alpha"),SimpleUploadedFile("b.md",b"beta")],"relative_paths":json.dumps(["folder/a.txt","folder/b.md"])})
        self.assertEqual(response.status_code,302)
        self.assertEqual(Material.objects.count(),2)
    def test_duplicate_and_same_name_new_content(self):
        first=self.ingest()
        self.ingest()
        second=self.ingest(content="另一版正文")
        self.assertEqual(Material.objects.count(),2)
        self.assertNotEqual(first.file.name,second.file.name)
    def test_duplicate_import_reuses_existing_auto_analysis(self):
        from .analysis import configuration_hash,grant_permission,request_analysis,auto_analyze,DEFAULT_EVENT_QUESTION
        from .models import ModelConfiguration
        mat=self.ingest()
        config=ModelConfiguration.objects.create(pk=1,mode="local",base_url="http://127.0.0.1:1",model="test-only",enabled=True)
        grant_permission(self.manager,self.event,[mat.pk],True,expected_config_fingerprint=configuration_hash(config))
        run=request_analysis(self.manager,DEFAULT_EVENT_QUESTION,event=self.event,background=False)
        run.status="complete"
        run.result={"items":[]}
        run.save()
        self.ingest()
        run.refresh_from_db()
        self.assertEqual(run.status,"complete")
        with patch("operations.analysis.POOL.submit") as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                result=auto_analyze(self.manager,self.event)
            dispatch.assert_not_called()
        self.assertEqual(result.pk,run.pk)
    def test_unknown_event_date_not_upload_date(self):
        batch=archive_files(self.manager,{"token":uuid.uuid4(),"title":"","recorded_date":None,"event":None},[SimpleUploadedFile("a.txt",b"hello")])
        self.assertIsNone(batch.event.recorded_date)
        self.assertIsNone(batch.event.scheduled_at)
    def test_folder_traversal_metadata_rejected(self):
        batch=archive_files(self.manager,{"token":uuid.uuid4(),"event":self.event},[SimpleUploadedFile("a.txt",b"x")],["../../outside.txt"])
        self.assertEqual(batch.results[0]["status"],"failed")
        self.assertEqual(Material.objects.count(),0)
    def test_mixed_failure_keeps_other_materials(self):
        batch=archive_files(self.manager,{"token":uuid.uuid4(),"event":self.event},[SimpleUploadedFile("bad.pdf",b"invalid"),SimpleUploadedFile("ok.txt",b"good")])
        self.assertEqual(Material.objects.count(),2)
        self.assertTrue(Material.objects.filter(parse_status="parsed").exists())
        self.assertTrue(Material.objects.filter(parse_status="failed").exists())
    def test_zip_single_level_and_exclusion_propagation(self):
        nested=io.BytesIO()
        with zipfile.ZipFile(nested,"w") as z:z.writestr("deep.txt","deep")
        raw=io.BytesIO()
        with zipfile.ZipFile(raw,"w") as z:
            z.writestr("notes.txt","notes")
            z.writestr("nested.zip",nested.getvalue())
        parent=self.ingest("bundle.zip",raw.getvalue())
        self.assertEqual(parent.children.count(),2)
        nested_mat=parent.children.get(original_name="nested.zip")
        self.assertEqual(nested_mat.children.count(),0)
        with self.assertRaises(Exception):
            retry_zip(nested_mat)
        set_excluded(self.manager,parent,True)
        self.assertFalse(parent.children.filter(excluded=False).exists())
        with self.assertRaises(Exception):
            retry_zip(parent)
    def test_zip_rejects_traversal_but_preserves_parent(self):
        raw=io.BytesIO()
        with zipfile.ZipFile(raw,"w") as z:z.writestr("../bad.txt","bad")
        mat=self.ingest("unsafe.zip",raw.getvalue())
        self.assertEqual(mat.parse_status,"failed")
        self.assertEqual(mat.children.count(),0)
        self.assertTrue(mat.file.storage.exists(mat.file.name))
    def test_download_auth_attachment_and_no_media_route(self):
        mat=self.ingest("demo.html","<script>no()</script>")
        response=self.client.get(reverse("ops_download",args=[mat.pk]))
        self.assertEqual(response.status_code,200)
        self.assertIn("attachment",response.headers["Content-Disposition"])
        self.assertIn("no-store",response.headers["Cache-Control"])
        response.close()
        self.client.force_login(self.member_user)
        self.assertEqual(self.client.get(reverse("ops_download",args=[mat.pk])).status_code,403)
    def test_image_preview_is_private_raster(self):
        from PIL import Image
        b=io.BytesIO()
        Image.new("RGB",(30,20),"green").save(b,format="PNG")
        mat=self.ingest("picture.png",b.getvalue())
        response=self.client.get(reverse("ops_preview",args=[mat.pk]))
        self.assertEqual(response.headers["Content-Type"],"image/jpeg")
        self.assertEqual(mat.parse_status,"unsupported")
    def test_roster_import_and_silent_member_visible(self):
        mat=self.ingest("roster.csv","编号,姓名,方向\nP1,林夏,网页\nP2,周宁,视频\n")
        report=import_roster(self.manager,mat,{"id":"编号","name":"姓名","interests":"方向"})
        self.assertEqual(report["created"],2)
        response=self.client.get("/members/")
        self.assertContains(response,"周宁")
        self.assertContains(response,"不等于无贡献")
    def test_excel_date_cell_can_be_imported_as_join_date(self):
        from datetime import datetime
        from openpyxl import Workbook
        wb=Workbook()
        ws=wb.active
        ws.append(["id","name","joined"])
        ws.append(["P1","林夏",datetime(2026,9,1)])
        buffer=io.BytesIO()
        wb.save(buffer)
        mat=self.ingest("roster.xlsx",buffer.getvalue())
        report=import_roster(self.manager,mat,{"id":"id","name":"name","joined_on":"joined"})
        self.assertEqual(report["created"],1)
        self.assertEqual(Person.objects.get(external_id="P1").joined_on,date(2026,9,1))
    def test_ambiguous_alias_not_auto_assigned(self):
        a=self.person()
        b=self.person("P2","另一位")
        Alias.objects.create(member=a,value="共同昵称")
        Alias.objects.create(member=b,value="共同昵称")
        mat=self.ingest(content="共同昵称分享了材料。")
        self.assertEqual(mat.mentions.count(),0)
    def test_unique_name_is_only_candidate(self):
        p=self.person()
        mat=self.ingest()
        self.assertEqual(mat.mentions.count(),1)
        self.assertEqual(Evidence.objects.count(),0)
    def test_removed_alias_no_longer_matches(self):
        p=self.person()
        Alias.objects.create(member=p,value="旧昵称")
        mat=self.ingest(content="旧昵称发言。")
        self.assertEqual(mat.mentions.count(),1)
        response=self.client.post(reverse("ops_person_edit",args=[p.pk]),{"external_id":"P1","display_name":"林夏","role":"","interests":"","notes":"","aliases":"","active":"on"})
        self.assertEqual(response.status_code,302)
        self.assertEqual(p.aliases.count(),0)
        self.assertEqual(mat.mentions.count(),0)
    def test_stats_and_date_filters_match_evidence(self):
        p=self.person()
        mat=self.ingest()
        save_evidence(self.manager,{"member":p,"event":self.event,"title":"网页作品","kind":"delivery","confidence":"verified","occurred_on":date(2026,9,20),"material":mat,"anchor":"第1段","quote":"林夏完成了演示网页。","note":""})
        response=self.client.get("/",{"start":"2026-09-01","end":"2026-09-30"})
        self.assertEqual(response.context["stats"]["verified"],1)
        self.assertEqual(response.context["stats"]["observed"],1)
        response=self.client.get("/",{"start":"2026-08-01","end":"2026-08-31"})
        self.assertEqual(response.context["stats"]["verified"],0)
        self.assertEqual(response.context["stats"]["roster"],1)
    def test_excluded_source_leaves_roster_but_not_evidence_count(self):
        p=self.person()
        mat=self.ingest()
        save_evidence(self.manager,{"member":p,"event":self.event,"title":"网页","kind":"delivery","confidence":"candidate","occurred_on":None,"material":mat,"anchor":"段","quote":"林夏完成了演示网页。","note":""})
        set_excluded(self.manager,mat,True)
        self.assertEqual(active_evidence().count(),0)
        self.assertEqual(Person.objects.count(),1)
    def test_roster_mapping_get_and_post(self):
        mat=self.ingest("roster.csv","id,name\nM1,示例成员\n")
        response=self.client.get("/roster/",{"material":mat.pk})
        self.assertContains(response,"稳定成员编号")
        response=self.client.post("/roster/",{"material":mat.pk,"table_index":"0","id":"id","name":"name"})
        self.assertEqual(response.context["report"]["created"],1)
    def test_invalid_raw_parameters_are_controlled(self):
        for path in ["/members/compare/?ids=oops","/roster/?material=oops","/roster/?table_index=%C2%B2"]:
            self.assertEqual(self.client.get(path).status_code,400,path)
    def test_disabled_model_blocks_analysis_without_network(self):
        with patch("operations.analysis.analyze_sources",side_effect=AssertionError("must not call")):
            response=self.client.post("/analysis/",{"question":"总结活动","send_ack":"on"})
        self.assertEqual(response.status_code,200)
        self.assertContains(response,"尚未启用")
        self.assertEqual(AnalysisRun.objects.count(),0)
    def test_csrf_upload_and_member_form(self):
        c=Client(enforce_csrf_checks=True)
        c.force_login(self.manager)
        self.assertEqual(c.post("/import/",{"token":uuid.uuid4()}).status_code,403)
        self.assertEqual(c.post("/members/new/",{}).status_code,403)
    def test_oversize_batch_fails_without_partial_material(self):
        response=self.client.post("/import/",{"token":uuid.uuid4(),"event":self.event.pk,"files":SimpleUploadedFile("large.txt",b"x"*(25*1024*1024+1))})
        self.assertTrue(response.context["form"].errors)
        self.assertEqual(Material.objects.count(),0)
    def test_xss_is_not_executed(self):
        mat=self.ingest(content="<script>alert(1)</script>")
        self.assertContains(self.client.get(reverse("ops_material",args=[mat.pk])),"&lt;script&gt;")
    def test_table_retry_preserves_raw_file(self):
        mat=self.ingest("bad.json","not json")
        name=mat.file.name
        parse_saved(mat)
        self.assertEqual(mat.file.name,name)
        self.assertEqual(mat.parse_status,"failed")
    def test_settings_requires_stale_page_protection_and_never_echoes_key(self):
        from .analysis import configuration,configuration_hash
        config=configuration()
        response=self.client.post("/settings/",{"mode":"disabled","model":"","base_url":"","expected_fingerprint":"0"*64,"api_key":"fake-secret-value"})
        self.assertEqual(response.status_code,200)
        self.assertContains(response,"设置已由其他页面修改")
        self.assertNotContains(response,"fake-secret-value")
