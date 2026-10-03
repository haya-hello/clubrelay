"""复盘事实快照与知识派生，不读取私密成果正文。 / Snapshot retrospectives without copying private deliverable content."""
import hashlib
import json
import uuid
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from .models import Activity, Task, Achievement, Retrospective, RetroEvent, KnowledgeDerivation, ActivityStageEvent
from .services import require_lead, visible_activities

RETRO_FIELDS = ["facts","differences","hypotheses","improvements","applicability"]

def activity_snapshot(activity):
    credits = {credit.task_id:credit for credit in Achievement.objects.filter(task__activity=activity,active=True,task__status="done",submission__status="approved")}
    tasks = []
    counts = {"total":0,"verified_done":0,"unfinished":0,"cancelled":0,"blocked":0}
    for task in activity.tasks.order_by("id"):
        credit = credits.get(task.pk)
        verified = task.status == "done" and credit is not None
        tasks.append({"id":str(task.pk),"title":task.title,"status":task.status,"status_label":task.get_status_display(),"version":task.version,"verified_done":verified,"blocked":bool(task.blocked_reason),"submission_id":str(credit.submission_id) if credit else None,"practice":credit.practice if credit else False,"contribution":credit.contribution if credit else False})
        counts["total"] += 1
        counts["verified_done"] += int(verified)
        counts["unfinished"] += int(not verified and task.status != "cancelled")
        counts["cancelled"] += int(task.status=="cancelled")
        counts["blocked"] += int(bool(task.blocked_reason))
    return {"activity":{"id":str(activity.pk),"title":activity.title,"objective":activity.objective,"plan":activity.plan,"constraints":activity.constraints,"scheduled_at":activity.scheduled_at.isoformat() if activity.scheduled_at else None},"counts":counts,"tasks":tasks}

def fingerprint(snapshot):
    return hashlib.sha256(json.dumps(snapshot,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()

def source_current(retro):
    return retro.status == Retrospective.Status.CONFIRMED and retro.fingerprint == fingerprint(activity_snapshot(retro.activity))

def stale_knowledge_ids():
    # 只检查派生关系与公开任务元数据，不修改 GET 请求中的业务状态。 / Check derivations and shared task metadata without mutating GET requests.
    return [link.entry_id for link in KnowledgeDerivation.objects.filter(entry__status="approved").select_related("retrospective__activity") if not source_current(link.retrospective)]

def entry_source_current(entry):
    link = KnowledgeDerivation.objects.filter(entry=entry).select_related("retrospective__activity").first()
    return not link or source_current(link.retrospective)

@transaction.atomic
def change_stage(user, activity_id, version, stage, reason):
    require_lead(user)
    activity = visible_activities(user).get(pk=activity_id)
    allowed = {"planning":["active","wrapup"],"active":["wrapup"],"wrapup":["archived"],"archived":["wrapup"]}
    if stage not in allowed.get(activity.stage,[]) or not reason.strip():
        raise ValidationError("请选择允许的下一阶段并说明原因。")
    if len(reason)>1000:
        raise ValidationError("原因不超过 1,000 字。")
    changed = Activity.objects.filter(pk=activity.pk,stage_version=version,stage=activity.stage).update(stage=stage,stage_version=activity.stage_version+1)
    if not changed:
        raise ValidationError("阶段已发生变化，请刷新。")
    ActivityStageEvent.objects.create(activity=activity,actor=user,before=activity.stage,after=stage,reason=reason.strip())
    return Activity.objects.get(pk=activity.pk)

@transaction.atomic
def create_retro(user, activity_id):
    require_lead(user)
    activity = visible_activities(user).get(pk=activity_id)
    existing = activity.retrospectives.filter(status="draft").first()
    if existing:
        return existing
    number = (activity.retrospectives.aggregate(value=Max("number"))["value"] or 0)+1
    snapshot = activity_snapshot(activity)
    retro = Retrospective.objects.create(activity=activity,author=user,number=number,snapshot=snapshot,fingerprint=fingerprint(snapshot))
    RetroEvent.objects.create(retrospective=retro,actor=user,action="create",revision=1)
    return retro

@transaction.atomic
def save_retro(user, pk, revision, data, refresh=False):
    require_lead(user)
    retro = Retrospective.objects.select_related("activity").get(pk=pk)
    values = {field:data.get(field,"") for field in RETRO_FIELDS}
    for field in RETRO_FIELDS:
        if len(values[field]) > (3000 if field=="applicability" else 5000):
            raise ValidationError("复盘字段超出长度限制。")
    if refresh:
        snapshot = activity_snapshot(retro.activity)
        values.update(snapshot=snapshot,fingerprint=fingerprint(snapshot))
    changed = Retrospective.objects.filter(pk=pk,revision=revision,status="draft").update(**values,revision=retro.revision+1)
    if not changed:
        raise ValidationError("草稿版本或状态已变化，请刷新；当前输入尚未保存。")
    RetroEvent.objects.create(retrospective=retro,actor=user,action="refresh" if refresh else "save",revision=retro.revision+1)
    return Retrospective.objects.get(pk=pk)

@transaction.atomic
def confirm_retro(user, pk, revision, acknowledged=False):
    require_lead(user)
    retro = Retrospective.objects.select_related("activity").get(pk=pk)
    if not acknowledged:
        raise ValidationError("请确认已核对事实和成员可见内容。")
    if retro.activity.stage not in ("wrapup","archived"):
        raise ValidationError("请先由负责人将活动标记为收尾，再确认复盘。")
    if not all(getattr(retro,key).strip() for key in ("facts","improvements","applicability")):
        raise ValidationError("确认前须填写实际结果、改进措施和适用边界。")
    if retro.fingerprint != fingerprint(activity_snapshot(retro.activity)):
        raise ValidationError("活动任务或验收已变化，请刷新事实快照并核对。")
    changed = Retrospective.objects.filter(pk=pk,revision=revision,status="draft").update(status="confirmed",confirmed_by=user,confirmed_at=timezone.now(),revision=retro.revision+1)
    if not changed:
        raise ValidationError("复盘已变化或已确认，请刷新。")
    # 新确认版本替代旧版，旧派生经验停止检索。 / A new confirmed version supersedes the old one and its derived search eligibility.
    Retrospective.objects.filter(activity=retro.activity,status="confirmed").exclude(pk=pk).update(status="superseded")
    RetroEvent.objects.create(retrospective=retro,actor=user,action="confirm",revision=retro.revision+1)
    return Retrospective.objects.get(pk=pk)

@transaction.atomic
def withdraw_retro(user, pk, revision, reason):
    require_lead(user)
    if not reason.strip() or len(reason)>1000:
        raise ValidationError("撤回必须填写原因，不超过 1,000 字。")
    retro = Retrospective.objects.get(pk=pk)
    if not Retrospective.objects.filter(pk=pk,revision=revision,status="confirmed").update(status="withdrawn",revision=retro.revision+1):
        raise ValidationError("仅可撤回当前已确认且未变化的版本。")
    RetroEvent.objects.create(retrospective=retro,actor=user,action="withdraw",revision=retro.revision+1,reason=reason.strip())

@transaction.atomic
def export_knowledge(user, pk, revision, data):
    require_lead(user)
    retro = Retrospective.objects.select_related("activity").get(pk=pk)
    if not source_current(retro):
        raise ValidationError("复盘尚未确认或来源已变化，不能送知识审核。")
    if not data.get("share_ack"):
        raise ValidationError("请确认提炼内容可供全社团成员查看。")
    existing = KnowledgeDerivation.objects.filter(retrospective=retro).first()
    if existing:
        return existing.entry
    if not Retrospective.objects.filter(pk=pk,revision=revision,status="confirmed").update(revision=retro.revision+1):
        raise ValidationError("复盘版本已变化，请刷新。")
    from knowledge.services import submit_entry
    entry = submit_entry(user,{"token":uuid.uuid4(),"title":data["title"],"category":"case","body":data["body"],"source":f"活动复盘：{retro.activity.title} / v{retro.number}","applicability":data["applicability"]})
    KnowledgeDerivation.objects.create(retrospective=retro,entry=entry)
    RetroEvent.objects.create(retrospective=retro,actor=user,action="export",revision=retro.revision+1)
    return entry
