import calendar
import hashlib
import json
import uuid
from datetime import date
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction, IntegrityError, OperationalError
from django.db.models import Q, Count
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from activities.models import Activity
from .security import manager_required
from .models import Material, ImportBatch, Person, Alias, Evidence, Audit, AnalysisRun, AnalysisPermission
from .forms import FilterForm, UploadForm, EventForm, PersonForm, EvidenceForm, ConfigurationForm, AskForm
from .ingestion import archive_files, parse_saved, set_excluded, invalidate, link_mentions, require_manager
from .people import active_evidence, import_roster, save_evidence, merge_evidence
from .analysis import configuration, ready, configuration_hash, grant_permission, request_analysis, auto_analyze, run_current, DEFAULT_EVENT_QUESTION
from .credentials import save_key, CredentialError

def filters(request):
    form=FilterForm(request.GET)
    if form.is_valid():
        return form,form.cleaned_data
    return form,None

def dated(query,field,data):
    if data is None:
        return query.none()
    if data.get("start"):
        query=query.filter(**{field+"__gte":data["start"]})
    if data.get("end"):
        query=query.filter(**{field+"__lte":data["end"]})
    return query

def filtered_evidence(request):
    form,data=filters(request)
    return form,data,dated(active_evidence(),"occurred_on",data)

@manager_required
def home(request):
    form,data,evidence=filtered_evidence(request)
    events=dated(Activity.objects.all(),"recorded_date",data)
    materials=Material.objects.filter(event__in=events,excluded=False)
    active=Person.objects.all()
    observed=evidence.values("member_id").distinct().count()
    covered=set()
    for run in AnalysisRun.objects.filter(status="complete",event__in=events,purpose="general"):
        if run_current(run):
            covered.update(s["id"] for s in run.sources)
    stats={"events":events.count(),"materials":Material.objects.filter(event__in=events).count(),"excluded":Material.objects.filter(event__in=events,excluded=True).count(),"parsed":materials.filter(parse_status="parsed").count(),"roster":active.filter(is_roster=True).count(),"observed":observed,"verified":evidence.filter(kind="delivery",confidence="verified").count(),"pending_analysis":materials.filter(parse_status="parsed").exclude(pk__in=covered).count()}
    issues=[]
    unknown=Activity.objects.filter(recorded_date__isnull=True).count()
    if unknown:
        issues.append(f"{unknown} 个档案日期待确认；按日期筛选时不计入范围。")
    failed=materials.filter(parse_status__in=["failed","unsupported"]).count()
    if failed:
        issues.append(f"{failed} 份资料仅归档或解析失败，未纳入正文分析。")
    if not active.filter(is_roster=True).exists():
        issues.append("尚未导入完整名册，不能将现有记录称作全体成员覆盖。")
    config=configuration()
    ok,reason=ready(config)
    if not ok:
        issues.append(reason)
    today=timezone.localdate()
    rows=[]
    for shift in range(5,-1,-1):
        month_num=today.year*12+today.month-1-shift
        y,m=divmod(month_num,12)
        m+=1
        rows.append({"label":f"{y}-{m:02d}","count":events.filter(recorded_date__year=y,recorded_date__month=m).count()})
    distribution=list(evidence.filter(kind="delivery",confidence="verified").values("member_id","member__display_name").annotate(count=Count("id")).order_by("-count","member__display_name")[:8])
    return render(request,"ops/dashboard.html",{"nav":"home","stats":stats,"filter_form":form,"contribution_rows":distribution,"max_contribution":max([item["count"] for item in distribution],default=1),"recent_events":events.order_by("-recorded_date","-created_at")[:6],"recent_evidence":evidence.select_related("member","event")[:8],"issues":issues,"activity_rows":rows})

@manager_required
def events(request):
    form,data=filters(request)
    rows=dated(Activity.objects.all(),"recorded_date",data)
    q=request.GET.get("q","").strip()[:120]
    if q:
        rows=rows.filter(Q(title__icontains=q)|Q(description__icontains=q))
    return render(request,"ops/events.html",{"nav":"events","filter_form":form,"q":q,"page":Paginator(rows.order_by("-recorded_date","-created_at"),12).get_page(request.GET.get("page"))})

@manager_required
def event(request,pk):
    obj=get_object_or_404(Activity,pk=pk)
    form=EventForm(request.POST or None,instance=obj)
    if request.method=="POST" and form.is_valid():
        form.save()
        invalidate(obj)
        Audit.objects.create(actor=request.user,action="event_correct",object_id=str(obj.pk))
        messages.success(request,"活动信息已保存；受影响分析标记待更新。")
        return redirect("ops_event",pk=pk)
    runs=obj.analyses.filter(purpose="general")[:8]
    latest=None
    for run in runs:
        if run.status=="complete" and run_current(run):
            latest=run
            break
    return render(request,"ops/event.html",{"nav":"events","event":obj,"form":form,"materials":obj.materials.all(),"latest":latest,"runs":runs,"evidence":active_evidence().filter(event=obj).select_related("member"),"model_ready":ready(configuration())[0]})

@manager_required
def upload(request):
    initial={"event":request.GET.get("event")}
    form=UploadForm(request.POST or None,initial=initial)
    if request.method=="POST":
        uploads=request.FILES.getlist("files")
        if getattr(request,"upload_failure",None):
            form.add_error(None,request.upload_failure)
        if form.is_valid():
            try:
                paths=json.loads(request.POST.get("relative_paths","[]"))
                if not isinstance(paths,list) or not all(isinstance(x,str) for x in paths):
                    raise ValueError
                batch=archive_files(request.user,form.cleaned_data,uploads,paths)
                auto_analyze(request.user,batch.event)
            except (ValidationError,ValueError,OperationalError,OSError) as exc:
                form.add_error(None,exc if isinstance(exc,ValidationError) else "导入暂未完成，请核对文件并重试；已归档原件不会被覆盖。")
            else:
                messages.success(request,"资料已本地归档，查看每份文件的解析和分析状态。")
                return redirect("ops_batch",pk=batch.pk)
    return render(request,"ops/upload.html",{"nav":"events","form":form})

@manager_required
def batch(request,pk):
    batch=get_object_or_404(ImportBatch.objects.select_related("event"),pk=pk)
    labels={"parsed":"已解析，可供分析","unsupported":"仅归档，未提取正文","failed":"处理失败","rejected":"超出限制，未归档","duplicate":"重复内容，未重复保存","archived_zip":"已归档并展开资料包"}
    for item in batch.results:
        item["status_label"]=labels.get(item.get("status"),item.get("status","待确认"))
        for child in item.get("children",[]):
            child["status_label"]=labels.get(child.get("status"),child.get("status","待确认"))
    return render(request,"ops/batch.html",{"nav":"events","batch":batch})

@manager_required
def material(request,pk):
    obj=get_object_or_404(Material.objects.select_related("event"),pk=pk)
    return render(request,"ops/material.html",{"nav":"events","material":obj,"events":Activity.objects.exclude(pk=obj.event_id),"image_preview":obj.original_name.lower().endswith((".jpg",".jpeg",".png")),"mentions":obj.mentions.select_related("member"),"segments":obj.segments[:100]})

@manager_required
@require_POST
def move_material(request,pk):
    material=get_object_or_404(Material,pk=pk)
    try:
        target_id=uuid.UUID(request.POST.get("event",""))
        expected=int(request.POST.get("version","0"))
    except (ValueError,TypeError):
        return HttpResponse("目标活动或版本不合法。",status=400)
    target=get_object_or_404(Activity,pk=target_id)
    if Material.objects.filter(event=target,sha256=material.sha256).exclude(pk=material.pk).exists():
        messages.error(request,"目标活动已有相同内容，请使用已有资料或排除本条，不能覆盖原件。")
        return redirect("ops_material",pk=pk)
    old_event=material.event
    with transaction.atomic():
        changed=Material.objects.filter(pk=pk,version=expected).update(event=target,version=expected+1,updated_at=timezone.now())
        if not changed:
            messages.error(request,"资料归属已变化，请刷新后重新确认。")
            return redirect("ops_material",pk=pk)
        invalidate(old_event)
        invalidate(target)
        Audit.objects.create(actor=request.user,action="move_material",object_id=str(pk),detail=f"活动 {old_event.pk} → {target.pk}")
    messages.success(request,"归属已更正。原件未变，旧活动下的相关成员证据需重新核对。")
    return redirect("ops_material",pk=pk)

@manager_required
def preview(request,pk):
    from PIL import Image, UnidentifiedImageError
    from io import BytesIO
    obj=get_object_or_404(Material,pk=pk)
    if not obj.original_name.lower().endswith((".png",".jpg",".jpeg")):
        raise Http404
    try:
        with obj.file.open("rb") as stream:
            with Image.open(stream) as img:
                if img.width*img.height>20000000 or img.format not in ("PNG","JPEG"):
                    raise Http404
                img.thumbnail((900,700))
                out=BytesIO()
                img.convert("RGB").save(out,format="JPEG",quality=85)
                response=HttpResponse(out.getvalue(),content_type="image/jpeg")
                response["X-Content-Type-Options"]="nosniff"
                return response
    except (OSError,UnidentifiedImageError,Image.DecompressionBombError):
        raise Http404

@manager_required
def download(request,pk):
    obj=get_object_or_404(Material,pk=pk)
    try:
        stream=obj.file.open("rb")
    except OSError:
        raise Http404
    response=FileResponse(stream,as_attachment=True,filename=obj.original_name,content_type="application/octet-stream")
    response["Content-Security-Policy"]="sandbox; default-src 'none'"
    return response

@manager_required
@require_POST
def material_action(request,pk):
    obj=get_object_or_404(Material,pk=pk)
    action=request.POST.get("action")
    try:
        if action in ("exclude","restore"):
            set_excluded(request.user,obj,action=="exclude")
        elif action=="retry":
            if obj.excluded:
                raise ValidationError("资料已排除，请先明确恢复后再重试。")
            if obj.original_name.lower().endswith(".zip"):
                from .ingestion import retry_zip
                retry_zip(obj)
            else:
                parse_saved(obj)
            auto_analyze(request.user,obj.event)
        else:
            return HttpResponse("无效操作",status=400)
    except ValidationError as exc:
        messages.error(request,"；".join(exc.messages))
    return redirect("ops_material",pk=pk)

@manager_required
def members(request):
    form,data,evidence=filtered_evidence(request)
    rows=Person.objects.all()
    q=request.GET.get("q","").strip()[:100]
    if q:
        rows=rows.filter(Q(display_name__icontains=q)|Q(external_id__icontains=q)|Q(role__icontains=q)|Q(interests__icontains=q))
    if request.GET.get("has_evidence")=="1":
        rows=rows.filter(pk__in=evidence.values("member_id"))
    if request.GET.get("verified")=="1":
        rows=rows.filter(pk__in=evidence.filter(kind="delivery",confidence="verified").values("member_id"))
    page=Paginator(rows,30).get_page(request.GET.get("page"))
    counts={item["member_id"]:item["n"] for item in evidence.values("member_id").annotate(n=Count("id"))}
    table=[{"member":p,"count":counts.get(p.pk,0),"verified":evidence.filter(member=p,kind="delivery",confidence="verified").count()} for p in page]
    return render(request,"ops/members.html",{"nav":"members","filter_form":form,"page":page,"rows":table,"q":q,"roster_count":Person.objects.filter(is_roster=True).count()})

@manager_required
def member(request,pk):
    obj=get_object_or_404(Person,pk=pk)
    form,data,evidence=filtered_evidence(request)
    evidence=evidence.filter(member=obj).select_related("event","material")
    mentions=obj.mentions.filter(material__excluded=False,material__parse_status="parsed").select_related("material__event")
    if data is not None:
        mentions=dated(mentions,"material__event__recorded_date",data)
    else:
        mentions=mentions.none()
    counts=[{"label":label,"count":evidence.filter(kind=key).count()} for key,label in Evidence.Kind.choices]
    page=Paginator(evidence,20).get_page(request.GET.get("page"))
    for item in page:
        item.merge_targets=active_evidence().filter(member=obj,kind=item.kind).exclude(pk=item.pk)[:100]
    analyses=[run for run in obj.analyses.filter(status="complete")[:5] if run_current(run)]
    return render(request,"ops/member.html",{"nav":"members","member":obj,"filter_form":form,"evidence":page,"page":page,"mentions":mentions[:30],"counts":counts,"analyses":analyses})

@manager_required
def person_edit(request,pk=None):
    obj=get_object_or_404(Person,pk=pk) if pk else None
    aliases="，".join(obj.aliases.values_list("value",flat=True)) if obj else ""
    form=PersonForm(request.POST or None,instance=obj,initial={"aliases":aliases})
    if request.method=="POST" and form.is_valid():
        with transaction.atomic():
            person=form.save()
            names=[n.strip() for n in form.cleaned_data["aliases"].replace("，",",").split(",") if n.strip()]
            person.aliases.exclude(value__in=names).delete()
            for name in names:
                if len(name)<=120:
                    Alias.objects.get_or_create(member=person,value=name)
            AnalysisRun.objects.filter(status__in=["complete","pending","running"]).update(status="stale",error="成员身份关联已更正，请重新分析。")
        for mat in Material.objects.filter(excluded=False,parse_status="parsed"):
            link_mentions(mat)
        return redirect("ops_member",pk=person.pk)
    return render(request,"ops/form.html",{"nav":"members","form":form,"heading":"维护成员资料","button":"保存成员"})

@manager_required
def compare(request):
    ids=request.GET.getlist("ids")
    if len(ids)>4:
        return HttpResponse("每次最多比较4名成员。",status=400)
    try:
        ids=[uuid.UUID(value) for value in ids]
    except (ValueError,TypeError):
        return HttpResponse("成员编号不合法。",status=400)
    people=Person.objects.filter(pk__in=ids) if ids else Person.objects.none()
    form,data,evidence=filtered_evidence(request)
    rows=[{"member":p,"counts":[evidence.filter(member=p,kind=kind).count() for kind,label in Evidence.Kind.choices],"verified":evidence.filter(member=p,confidence="verified",kind="delivery").count()} for p in people]
    return render(request,"ops/compare.html",{"nav":"members","rows":rows,"kinds":[x[1] for x in Evidence.Kind.choices],"filter_form":form})

@manager_required
def roster(request):
    material_id=request.POST.get("material") or request.GET.get("material")
    try:
        material_id=uuid.UUID(material_id) if material_id else None
    except (ValueError,TypeError):
        return HttpResponse("资料编号不合法。",status=400)
    obj=get_object_or_404(Material,pk=material_id,excluded=False,parse_status="parsed") if material_id else None
    index_value=request.POST.get("table_index") or request.GET.get("table_index","0")
    try:
        table_index=int(index_value)
    except (ValueError,TypeError):
        return HttpResponse("工作表编号不合法。",status=400)
    report=None
    if obj and request.method=="POST":
        mapping={k:request.POST.get(k,"") for k in ["id","name","role","interests","aliases","joined_on"]}
        try:
            report=import_roster(request.user,obj,mapping,table_index)
            for mat in Material.objects.filter(excluded=False,parse_status="parsed"):
                link_mentions(mat)
        except ValidationError as exc:
            report={"created":0,"updated":0,"issues":exc.messages}
    table=obj.tables[table_index] if obj and 0<=table_index<len(obj.tables) else None
    return render(request,"ops/roster.html",{"nav":"members","material":obj,"table":table,"table_index":table_index,"report":report,"mapping_fields":[("id","稳定成员编号（必选）"),("name","姓名（必选）"),("role","角色"),("interests","兴趣方向"),("aliases","别名"),("joined_on","加入日期")],"materials":Material.objects.filter(parse_status="parsed",excluded=False).exclude(tables=[])})

@manager_required
def evidence_edit(request,pk=None):
    obj=get_object_or_404(Evidence,pk=pk) if pk else None
    initial={"member":request.GET.get("member"),"event":request.GET.get("event"),"material":request.GET.get("material")}
    if not obj and initial["member"] and initial["material"]:
        from .models import Mention
        try:
            mention=Mention.objects.filter(member_id=initial["member"],material_id=initial["material"],material__excluded=False).first()
        except (ValidationError,ValueError):
            mention=None
        if mention:
            initial.update(quote=mention.excerpt,anchor=mention.anchor,confidence="candidate")
    form=EvidenceForm(request.POST or None,instance=obj,initial=initial)
    if request.method=="POST" and form.is_valid():
        try:
            data=dict(form.cleaned_data)
            if obj:
                data["version"]=int(request.POST.get("version","0"))
            record=save_evidence(request.user,data,instance=obj)
        except (ValidationError,ValueError,OperationalError) as exc:
            form.add_error(None,exc if isinstance(exc,ValidationError) else "记录已变化，请刷新后重试。")
        else:
            return redirect("ops_member",pk=record.member_id)
    return render(request,"ops/form.html",{"nav":"members","form":form,"heading":"核对一条成员证据","button":"保存证据","version":obj.version if obj else None})

@manager_required
@require_POST
def merge(request,pk):
    source=get_object_or_404(Evidence,pk=pk)
    try:
        target_raw,target_version=request.POST.get("target","").split(":",1)
        target_id=uuid.UUID(target_raw)
        source_version=int(request.POST.get("version","0"))
        target_version=int(target_version)
    except (ValueError,TypeError):
        return HttpResponse("请选择正确的目标证据。",status=400)
    target=get_object_or_404(Evidence,pk=target_id)
    if source.version!=source_version or target.version!=target_version:
        messages.error(request,"证据已变化，请刷新后重新选择合并目标。")
        return redirect("ops_member",pk=source.member_id)
    try:
        merge_evidence(request.user,source,target)
    except ValidationError as exc:
        messages.error(request,"；".join(exc.messages))
    return redirect("ops_member",pk=source.member_id)

@manager_required
def settings_view(request):
    obj=configuration()
    form=ConfigurationForm(request.POST or None,instance=obj,initial={"expected_fingerprint":configuration_hash(obj)})
    if request.method=="POST" and form.is_valid():
        try:
            from .analysis import CONFIG_LOCK
            with CONFIG_LOCK:
                if form.cleaned_data["expected_fingerprint"]!=configuration_hash(configuration()):
                    raise ValidationError("设置已由其他页面修改，请刷新后重试。")
                AnalysisRun.objects.filter(status__in=["pending","running"]).update(status="cancelled",error="模型配置已变更，本次未采用结果。")
                desired=form.cleaned_data["enabled"]
                config=form.save(commit=False)
                config.enabled=False
                config.save()
                if form.cleaned_data["api_key"]:
                    save_key(form.cleaned_data["api_key"])
                config.enabled=desired
                config.save()
            messages.success(request,"设置已保存。资料仍需按活动明确授权，才会发送给该模型。")
            return redirect("ops_settings")
        except CredentialError as exc:
            form.add_error("api_key",exc.message)
        except ValidationError as exc:
            form.add_error(None,exc)
    return render(request,"ops/settings.html",{"nav":"settings","form":form,"model_ready":ready(obj)[0],"model_reason":ready(obj)[1]})

@manager_required
def permission(request,pk):
    obj=get_object_or_404(Activity,pk=pk)
    mats=obj.materials.filter(excluded=False,parse_status="parsed")
    if request.method=="POST":
        if request.POST.get("ack")!="on":
            messages.error(request,"请先确认处理位置和资料范围。")
        else:
            try:
                grant_permission(request.user,obj,request.POST.getlist("materials"),request.POST.get("auto_future")=="on",expected_config_fingerprint=request.POST.get("config_fingerprint",""))
                run=request_analysis(request.user,DEFAULT_EVENT_QUESTION,event=obj)
            except ValidationError as exc:
                messages.error(request,"；".join(exc.messages))
            else:
                return redirect("ops_analysis",pk=run.pk)
    config=configuration()
    return render(request,"ops/permission.html",{"nav":"events","event":obj,"materials":mats,"config":config,"config_fingerprint":configuration_hash(config),"model_ready":ready(config)[0],"reason":ready(config)[1]})

@manager_required
@require_POST
def revoke_permission(request,pk):
    obj=get_object_or_404(Activity,pk=pk)
    from .analysis import CONFIG_LOCK
    with CONFIG_LOCK:
        AnalysisPermission.objects.filter(event=obj).delete()
        AnalysisRun.objects.filter(status__in=["pending","running"]).update(status="cancelled",error="资料处理许可已变化。")
    return redirect("ops_event",pk=pk)

@manager_required
def assistant(request):
    form=AskForm(request.POST or None,initial={"event":request.GET.get("event"),"member":request.GET.get("member"),"start":request.GET.get("start"),"end":request.GET.get("end")})
    if request.method=="POST" and form.is_valid():
        if request.POST.get("send_ack")!="on":
            form.add_error(None,"请确认本次问题和已授权正文的处理范围。")
        else:
            try:
                run=request_analysis(request.user,form.cleaned_data["question"],event=form.cleaned_data["event"],member=form.cleaned_data["member"],start=form.cleaned_data["start"],end=form.cleaned_data["end"])
            except ValidationError as exc:
                form.add_error(None,exc)
            else:
                return redirect("ops_analysis",pk=run.pk)
    return render(request,"ops/assistant.html",{"nav":"assistant","form":form,"config":configuration(),"runs":AnalysisRun.objects.filter(purpose="general")[:10],"ready":ready(configuration())[0]})

@manager_required
def analysis_detail(request,pk):
    run=get_object_or_404(AnalysisRun.objects.select_related("event","member"),pk=pk)
    if run.purpose == "handoff":
        pack = run.handoff_packs.order_by("created_at").first()
        if pack:
            return redirect("handoff_detail", pk=pack.pk)
        raise Http404
    current=run_current(run)
    items=run.result.get("items",[]) if current and run.status=="complete" else []
    for item in items:
        item["kind_label"]={"fact":"资料事实","hypothesis":"原因假设","suggestion":"建议","gap":"资料不足"}.get(item["kind"],item["kind"])
    return render(request,"ops/analysis.html",{"nav":"assistant","run":run,"current":current,"items":items})

@manager_required
@require_POST
def cancel_analysis(request,pk):
    from .analysis import CONFIG_LOCK
    with CONFIG_LOCK:
        AnalysisRun.objects.filter(pk=pk,status__in=["pending","running"]).update(status="cancelled",error="已取消；已发出的请求无法撤回，返回结果不会采用。")
    return redirect("ops_analysis",pk=pk)

@manager_required
def citation(request,pk,item,reference):
    run=get_object_or_404(AnalysisRun,pk=pk,status="complete")
    if not run_current(run):
        return HttpResponse("资料已变化，旧引用已失效。",status=410)
    try:
        ref=run.result["items"][item]["citations"][reference]
        material=get_object_or_404(Material,pk=ref["source_id"],excluded=False,parse_status="parsed")
        if ref["quote"] not in material.text:
            raise IndexError
    except (KeyError,IndexError):
        return HttpResponse("出处不可用。",status=410)
    return render(request,"ops/citation.html",{"nav":"assistant","material":material,"quote":ref["quote"],"run":run})

@manager_required
def roster_template(request):
    response=HttpResponse("\ufeffmember_id,name,role,interests,aliases,joined_on\nDEMO001,示例成员,普通成员,AI视频,示例昵称,2026-09-01\n",content_type="text/csv; charset=utf-8")
    response["Content-Disposition"]='attachment; filename="roster-template.csv"'
    return response

@manager_required
def import_records(request,pk):
    from .records_import import import_observations
    material=get_object_or_404(Material,pk=pk,excluded=False,parse_status="parsed")
    try:
        table_index=int(request.POST.get("table_index") or request.GET.get("table_index","0"))
    except (ValueError,TypeError):
        return HttpResponse("工作表编号不合法。",status=400)
    table=material.tables[table_index] if 0<=table_index<len(material.tables) else None
    report=None
    if request.method=="POST":
        mapping={key:request.POST.get(key,"") for key in ["person_id","title","occurred_on","quote"]}
        try:
            report=import_observations(request.user,material,mapping,request.POST.get("kind","participation"),confidence=request.POST.get("confidence","self_report"),table_index=table_index,verified_ack=request.POST.get("verified_ack")=="on")
        except ValidationError as exc:
            report={"created":0,"duplicates":0,"issues":exc.messages}
    return render(request,"ops/import_records.html",{"nav":"members","material":material,"table":table,"table_index":table_index,"report":report,"mapping_fields":[("person_id","稳定成员编号（必选）"),("title","事项或作品标题（必选）"),("quote","原文依据列（必选）"),("occurred_on","发生日期（可不选）")],"kinds":Evidence.Kind.choices,"confidences":Evidence.Confidence.choices})
