from pathlib import PurePosixPath
from urllib.parse import urlsplit
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from knowledge.services import require_active, is_reviewer
from .models import Task, TaskEvent, Submission, AcceptanceEvent, Achievement
from .services import visible_tasks, require_lead
from .delivery_forms import validate_file

FIELDS = ["summary","result_text","result_url","method","contribution"]

def visible_submissions(user):
    require_active(user)
    rows = Submission.objects.filter(task__in=visible_tasks(user))
    return rows if is_reviewer(user) else rows.filter(author=user)

def current_achievements(user):
    return Achievement.objects.filter(member=user, active=True, task__in=visible_tasks(user), task__status="done", submission__status="approved")

def submit_result(user, task_id, data):
    require_active(user)
    upload = data.get("attachment")
    validate_file(upload)
    url = data.get("result_url","")
    if url and (urlsplit(url).scheme not in ("http","https") or urlsplit(url).username or urlsplit(url).password):
        raise ValidationError("成果链接仅支持不含账号密码的 HTTP/HTTPS。")
    if not (data.get("result_text") or url or upload):
        raise ValidationError("必须提供实际成果内容、链接或附件。")
    saved_file = None
    storage = None
    try:
        with transaction.atomic():
            task = visible_tasks(user).get(pk=task_id)
            if task.assignee_id != user.id:
                raise PermissionDenied("只有任务承担人可以提交成果。")
            duplicate = Submission.objects.filter(token=data["token"]).first()
            if duplicate:
                if duplicate.author_id != user.id or duplicate.task_id != task.pk:
                    raise PermissionDenied("提交标识不属于本任务。")
                return duplicate
            if task.status not in [Task.Status.ACTIVE, Task.Status.CHANGES, Task.Status.REVIEW]:
                raise ValidationError("请先接受任务；已完成或取消的任务不能直接提交。")
            if task.blocked_reason:
                raise ValidationError("请先解除受阻并确认任务可以交付。")
            changed = Task.objects.filter(pk=task.pk, version=data["version"], status=task.status).update(status=Task.Status.REVIEW, version=task.version+1, updated_at=timezone.now())
            if not changed:
                raise ValidationError("任务版本已变化，请刷新后提交。")
            number = (task.submissions.aggregate(value=Max("number"))["value"] or 0) + 1
            result = Submission(task=task,author=user,token=data["token"],number=number,**{key:data.get(key,"") for key in FIELDS})
            result.full_clean(exclude=["attachment"])
            # 附件仅写随机私有路径；数据库失败时移除本次新建副本。 / Store privately; remove only the new copy if the transaction fails.
            if upload:
                result.original_name = PurePosixPath(upload.name.replace("\\","/")).name[:255]
                result.attachment.save(result.original_name, upload, save=False)
                saved_file, storage = result.attachment.name, result.attachment.storage
            result.save()
            task.submissions.exclude(pk=result.pk).filter(status="pending").update(status="superseded")
            TaskEvent.objects.create(task=task,actor=user,action="submit",version=task.version+1,assignee=user)
            return result
    except Exception:
        if saved_file and storage:
            storage.delete(saved_file)
        raise

@transaction.atomic
def review_result(user, submission_id, version, action, reason, practice=False, contribution=False):
    require_lead(user)
    result = visible_submissions(user).select_related("task").get(pk=submission_id)
    task = result.task
    if result.author_id == user.id:
        raise PermissionDenied("不能验收自己的成果，请由另一位负责人处理。")
    if not reason.strip() or len(reason) > 1000:
        raise ValidationError("请填写 1 至 1,000 字验收意见。")
    if action not in ("approve","return","revoke"):
        raise ValidationError("不支持的验收操作。")
    if action == "approve" and not (practice or contribution):
        raise ValidationError("至少确认一种成果用途。")
    latest = task.submissions.order_by("-number").values_list("pk",flat=True).first()
    expected_task, expected_result = ("done","approved") if action == "revoke" else ("review","pending")
    if latest != result.pk or task.status != expected_task or result.status != expected_result:
        raise ValidationError("该成果已不是可处理的当前版本，请刷新。")
    changed = Task.objects.filter(pk=task.pk,version=version,status=expected_task).update(status="done" if action=="approve" else "changes", version=task.version+1, updated_at=timezone.now())
    if not changed:
        raise ValidationError("任务版本已变化，验收未生效，请刷新。")
    result.status = {"approve":"approved","return":"returned","revoke":"revoked"}[action]
    result.save(update_fields=["status"])
    if action == "approve":
        Achievement.objects.update_or_create(task=task,defaults={"submission":result,"member":result.author,"practice":practice,"contribution":contribution,"active":True})
    else:
        Achievement.objects.filter(task=task).update(active=False)
    AcceptanceEvent.objects.create(submission=result,actor=user,action=action,reason=reason.strip(),practice=practice if action=="approve" else False,contribution=contribution if action=="approve" else False)
    # 私密验收意见不进入全体活动成员可见的任务记录。 / Keep private review feedback out of shared task events.
    TaskEvent.objects.create(task=task,actor=user,action=action,version=task.version+1,assignee=task.assignee)
    return result

