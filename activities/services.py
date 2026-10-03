from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from knowledge.services import is_reviewer, require_active, searchable_entries
from knowledge.models import Entry
from .models import Activity, Task, TaskEvent

def require_lead(user):
    require_active(user)
    if not is_reviewer(user):
        raise PermissionDenied("仅负责人可以创建活动和发布任务。")

def visible_activities(user):
    require_active(user)
    if is_reviewer(user):
        return Activity.objects.all()
    return Activity.objects.filter(participants=user)

def visible_tasks(user):
    return Task.objects.filter(activity__in=visible_activities(user))

@transaction.atomic
def create_activity(user, data):
    require_lead(user)
    existing = Activity.objects.filter(token=data["token"]).first()
    if existing:
        if existing.creator_id != user.id:
            raise PermissionDenied("不能重复使用其他人的创建标识。")
        return existing
    activity = Activity(creator=user, **{key:data[key] for key in ["token","title","kind","objective","audience","scheduled_at","constraints","plan"]})
    activity.full_clean()
    activity.save()
    selected = list(data["participants"])
    if any(not person.is_active for person in selected):
        raise ValidationError("包含已停用成员，请重新选择。")
    activity.participants.set(selected)
    activity.participants.add(user)
    return activity

@transaction.atomic
def publish_task(user, activity, data):
    require_lead(user)
    existing = Task.objects.filter(token=data["token"]).first()
    if existing:
        if existing.creator_id != user.id or existing.activity_id != activity.pk:
            raise PermissionDenied("不能重复使用其他任务标识。")
        return existing
    assignee = data["assignee"]
    if assignee and not activity.participants.filter(pk=assignee.pk, is_active=True).exists():
        raise ValidationError("承担人必须是本活动中有效的成员。")
    reference = data["reference"]
    if reference and not searchable_entries(user).filter(pk=reference.pk).exists():
        raise ValidationError("参考经验已失效，请重新选择。")
    task = Task(activity=activity, creator=user, status=Task.Status.WAITING if assignee else Task.Status.OPEN, **{key:data[key] for key in ["token","title","objective","deliverable","acceptance","due_at","assignee","reference"]})
    task.full_clean()
    task.save()
    TaskEvent.objects.create(task=task, actor=user, action="publish", version=1, assignee=assignee, reason="等待本人接受" if assignee else "开放给活动成员认领")
    return task

@transaction.atomic
def change_task(user, pk, version, action, reason=""):
    require_active(user)
    task = visible_tasks(user).get(pk=pk)
    values = {}
    reason = reason.strip()
    if len(reason) > 1000:
        raise ValidationError("说明不能超过 1,000 字。")
    if action in ("decline", "block", "cancel") and not reason:
        raise ValidationError("请填写原因或需要的支持。")
    if action == "claim":
        if not task.activity.participants.filter(pk=user.pk).exists():
            raise PermissionDenied("只有本活动成员可以认领。")
        if task.status != Task.Status.OPEN:
            raise ValidationError("任务已被认领或不再开放。")
        values = {"assignee":user, "status":Task.Status.ACTIVE}
    elif action in ("accept", "decline"):
        if task.assignee_id != user.id:
            raise PermissionDenied("必须由被指派成员本人确认，负责人也不能代替。")
        if task.status != Task.Status.WAITING:
            raise ValidationError("任务已不处于待接受状态。")
        values = {"status":Task.Status.ACTIVE} if action == "accept" else {"status":Task.Status.OPEN, "assignee":None}
    elif action in ("block", "unblock"):
        if task.assignee_id != user.id:
            raise PermissionDenied("只有承担人可以报告或解除受阻。")
        if task.status != Task.Status.ACTIVE:
            raise ValidationError("只有进行中的任务可以更新受阻情况。")
        if action == "unblock" and not task.blocked_reason:
            raise ValidationError("任务目前没有受阻记录。")
        values = {"blocked_reason":reason if action == "block" else ""}
    elif action == "cancel":
        require_lead(user)
        if task.status == Task.Status.CANCELLED:
            raise ValidationError("任务已取消。")
        if task.status == Task.Status.DONE:
            raise ValidationError("任务已验收，请先在成果页撤销验收，不能直接取消。")
        values = {"status":Task.Status.CANCELLED, "blocked_reason":""}
    else:
        raise ValidationError("不支持的操作。")
    # 条件写入保证旧页面不能覆盖任务的最新归属。 / Conditional writes prevent stale pages from changing current ownership.
    updated = Task.objects.filter(pk=pk, version=version, status=task.status).update(**values, version=task.version+1, updated_at=timezone.now())
    if not updated:
        raise ValidationError("任务已发生变化，请刷新后再操作。")
    task.refresh_from_db()
    if action == "cancel":
        task.submissions.filter(status="pending").update(status="cancelled")
    TaskEvent.objects.create(task=task, actor=user, action=action, version=task.version, assignee=task.assignee, reason=reason)
    return task
