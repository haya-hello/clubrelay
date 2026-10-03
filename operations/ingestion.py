import hashlib
import json
import re
import uuid
from pathlib import PurePosixPath
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction, IntegrityError
from django.db.models import Sum
from django.utils import timezone
from activities.models import Activity
from .models import ImportBatch, Material, Audit, Person, Alias, Mention
from .parsers import parse_material, safe_zip_members

MAX_FILE=25*1024*1024

def require_manager(user):
    from .security import is_manager
    from django.core.exceptions import PermissionDenied
    if not is_manager(user):
        raise PermissionDenied

def material_fingerprint(event):
    rows=list(Material.objects.filter(event=event,excluded=False).order_by("id").values_list("id","sha256","parse_status","updated_at"))
    return hashlib.sha256(json.dumps([(str(a),b,c,d.isoformat()) for a,b,c,d in rows],ensure_ascii=False).encode()).hexdigest()

def invalidate(event):
    from .analysis import run_current
    from .models import AnalysisRun
    from django.db.models import Q
    # 只使真正受影响的结果失效；重复导入不触发重复模型调用。 / Invalidate changed scopes only; duplicates do not spend another model call.
    runs=AnalysisRun.objects.filter(Q(event=event)|Q(event__isnull=True),status__in=["complete","pending","running"]).select_related("event","member")
    for run in runs:
        if not run_current(run):
            AnalysisRun.objects.filter(pk=run.pk,status__in=["complete","pending","running"]).update(status="stale",error="资料或关联已变化，请重新分析。",updated_at=timezone.now())

def parse_saved(material):
    try:
        with material.file.open("rb") as stream:
            raw=stream.read(MAX_FILE+1)
        result=parse_material(material.original_name,raw)
        material.parse_status=result["status"]
        material.text=result["text"]
        material.segments=result["segments"]
        material.tables=result["tables"]
        material.note=result["error"]
    except Exception:
        material.parse_status="failed"
        material.text=""
        material.segments=[]
        material.tables=[]
        material.note="解析失败；原件已保留，可重试或更换格式。"
    material.save(update_fields=["parse_status","text","segments","tables","note","updated_at"])
    link_mentions(material)
    invalidate(material.event)
    return material

def link_mentions(material):
    # 名称命中只是候选，不自动算作参与或交付。 / Name matches are candidates, never automatic contribution claims.
    material.mentions.all().delete()
    if material.excluded or material.parse_status!="parsed":
        return
    mapping={}
    for person in Person.objects.filter(active=True).prefetch_related("aliases"):
        names={person.display_name,*[alias.value for alias in person.aliases.all()]}
        for name in names:
            if len(name.strip())>=2:
                mapping.setdefault(name.strip(),set()).add(person.pk)
    linked=set()
    for name,people in mapping.items():
        if len(people)!=1:
            continue
        pid=next(iter(people))
        if pid in linked:
            continue
        for segment in material.segments:
            pos=segment["text"].find(name)
            if pos>=0:
                Mention.objects.get_or_create(member_id=pid,material=material,defaults={"anchor":segment["anchor"][:100],"excerpt":segment["text"][max(0,pos-80):pos+220]})
                linked.add(pid)
                break

def _metadata_path(value):
    value=value.replace("\\","/")
    path=PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or ":" in value or "\x00" in value:
        raise ValidationError("资料路径不合法。")
    return str(path)[:500]

def _store(batch,filename,raw,source_path,expand_zip=True,budget=None,parent=None):
    if len(raw)>MAX_FILE:
        return {"name":filename,"status":"rejected","note":"单文件超过25MB。"}
    digest=hashlib.sha256(raw).hexdigest()
    existing=Material.objects.filter(event=batch.event,sha256=digest).first()
    if existing:
        return {"name":filename,"status":"duplicate","material_id":str(existing.pk),"note":"同活动中已有相同内容，未重复保存或统计。"}
    if budget is not None:
        if len(raw)>budget["remaining"]:
            return {"name":filename,"status":"rejected","note":"展开后存储总量超过200MB。"}
        budget["remaining"]-=len(raw)
    material=Material(event=batch.event,batch=batch,parent=parent,original_name=PurePosixPath(filename.replace("\\","/")).name[:255],source_path=source_path,sha256=digest,size=len(raw))
    stored=None
    try:
        with transaction.atomic():
            material.file.save(material.original_name,ContentFile(raw),save=False)
            stored=material.file.name
            material.save()
    except IntegrityError:
        if stored:
            material.file.storage.delete(stored)
        existing=Material.objects.filter(event=batch.event,sha256=digest).first()
        if existing:
            return {"name":filename,"status":"duplicate","material_id":str(existing.pk),"note":"重复内容"}
        raise
    except Exception:
        if stored:
            material.file.storage.delete(stored)
        raise
    if material.original_name.lower().endswith(".zip"):
        material.parse_status="unsupported"
        material.note="资料包原件已保存，展开内容另列。"
        material.save()
        if not expand_zip:
            return {"name":filename,"status":"unsupported","material_id":str(material.pk),"note":"嵌套资料包仅归档，不递归展开。"}
        try:
            members=safe_zip_members(raw)
            if budget is not None and sum(len(child["content"]) for child in members)>budget["remaining"]:
                raise ValueError("展开后总量超过本批剩余容量。")
            children=[_store(batch,child["filename"],child["content"],(source_path+"/"+child["filename"])[:500],expand_zip=False,budget=budget,parent=material) for child in members]
        except ValueError as exc:
            material.parse_status="failed"
            material.note="资料包未展开："+str(exc)[:300]
            material.save()
            return {"name":filename,"status":"failed","material_id":str(material.pk),"note":material.note}
        return {"name":filename,"status":"archived_zip","material_id":str(material.pk),"children":children,"note":"只展开一层；嵌套资料包仅归档。"}
    parse_saved(material)
    return {"name":filename,"status":material.parse_status,"material_id":str(material.pk),"note":material.note}

def archive_files(user,data,files,relative_paths=None):
    require_manager(user)
    if not files or len(files)>50 or sum(f.size for f in files)>200*1024*1024:
        raise ValidationError("请选择1至50个文件，总大小不超过200MB。")
    existing=ImportBatch.objects.filter(pk=data["token"]).first()
    if existing:
        return existing
    event=data.get("event")
    if not event:
        event,_=Activity.objects.get_or_create(token=data["token"],defaults={"creator":user,"title":data.get("title") or "待归档资料 "+timezone.localdate().isoformat(),"objective":"","audience":"","constraints":"","plan":"","scheduled_at":None,"recorded_date":data.get("recorded_date")})
    batch=ImportBatch.objects.create(id=data["token"],event=event,actor=user)
    results=[]
    budget={"remaining":200*1024*1024}
    for index,upload in enumerate(files):
        try:
            relative=_metadata_path((relative_paths or [])[index] if index<len(relative_paths or []) else upload.name)
            raw=upload.read(MAX_FILE+1)
            results.append(_store(batch,upload.name,raw,relative,budget=budget))
        except (OSError,ValidationError):
            results.append({"name":upload.name[:255],"status":"failed","note":"保存失败或路径不合法，其他文件继续处理。"})
    batch.results=results
    batch.save(update_fields=["results"])
    invalidate(event)
    Audit.objects.create(actor=user,action="import_batch",object_id=str(batch.pk),detail=f"文件数 {len(files)}")
    return batch

@transaction.atomic
def set_excluded(user,material,excluded):
    require_manager(user)
    if not excluded and material.parent_id and material.parent.excluded:
        raise ValidationError("先恢复来源资料包，再选择需要恢复的展开文件。")
    material.excluded=excluded
    material.save(update_fields=["excluded","updated_at"])
    link_mentions(material)
    if excluded:
        for child in material.children.all():
            child.excluded=True
            child.save(update_fields=["excluded","updated_at"])
            link_mentions(child)
    invalidate(material.event)
    Audit.objects.create(actor=user,action="exclude" if excluded else "restore",object_id=str(material.pk))

def retry_zip(material):
    if material.excluded or material.parent_id:
        raise ValidationError("已排除或嵌套的资料包不能展开；请先处理来源状态。")
    try:
        with material.file.open("rb") as stream:
            raw=stream.read(MAX_FILE+1)
        members=safe_zip_members(raw)
        used=Material.objects.filter(batch=material.batch).aggregate(total=Sum("size"))["total"] or 0
        budget={"remaining":max(0,200*1024*1024-used)}
        hashes=[hashlib.sha256(child["content"]).hexdigest() for child in members]
        existing=set(Material.objects.filter(event=material.event,sha256__in=hashes).values_list("sha256",flat=True))
        required=sum(len(child["content"]) for child,digest in zip(members,hashes) if digest not in existing)
        if required>budget["remaining"]:
            raise ValueError("超出原批次剩余容量。")
        results=[_store(material.batch,child["filename"],child["content"],(material.source_path+"/"+child["filename"])[:500],expand_zip=False,budget=budget,parent=material) for child in members]
        material.parse_status="unsupported"
        material.note=f"资料包已安全展开 {len(results)} 项；重复内容不重复保存。"
    except (ValueError,OSError):
        material.parse_status="failed"
        material.note="资料包仍无法安全展开，原件保留。"
    material.save(update_fields=["parse_status","note","updated_at"])
    invalidate(material.event)
