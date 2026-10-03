from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from .models import Entry, Revision, ReviewEvent

REVIEWER_GROUP = "社团负责人"
CONTENT_FIELDS = ("title", "category", "body", "source", "applicability")

def is_reviewer(user):
    return bool(user.is_authenticated and user.is_active and user.groups.filter(name=REVIEWER_GROUP).exists())

def require_active(user):
    if not user.is_authenticated or not user.is_active:
        raise PermissionDenied("请使用有效账号登录。")

def visible_entries(user):
    require_active(user)
    if is_reviewer(user):
        return Entry.objects.all()
    return Entry.objects.filter(Q(pk__in=searchable_entries(user).values("pk")) | Q(owner=user))

def searchable_entries(user=None):
    if user is not None:
        require_active(user)
    from activities.retros import stale_knowledge_ids
    return Entry.objects.filter(status=Entry.Status.APPROVED).exclude(pk__in=stale_knowledge_ids())

def snapshot(entry):
    Revision.objects.create(entry=entry, number=entry.version, **{name: getattr(entry, name) for name in CONTENT_FIELDS})

@transaction.atomic
def submit_entry(user, data):
    require_active(user)
    existing = Entry.objects.filter(submit_token=data["token"]).first()
    if existing:
        if existing.owner_id != user.id:
            raise PermissionDenied("无权使用该提交标识。")
        return existing
    entry = Entry(owner=user, submit_token=data["token"], **{name: data[name] for name in CONTENT_FIELDS})
    entry.full_clean()
    entry.save()
    snapshot(entry)
    ReviewEvent.objects.create(entry=entry, actor=user, version=1, action="submit")
    return entry

@transaction.atomic
def resubmit_entry(user, pk, data):
    require_active(user)
    entry = Entry.objects.get(pk=pk)
    if entry.owner_id != user.id:
        raise PermissionDenied("只能修改自己的经验。")
    # 条件更新防止旧页面覆盖新版；旧快照保留。 / Compare-and-swap prevents stale writes; snapshots stay intact.
    values = {name: data[name] for name in CONTENT_FIELDS}
    candidate = Entry(**values)
    candidate.clean_fields(exclude=["owner", "submit_token"])
    changed = Entry.objects.filter(pk=pk, version=data.get("version")).update(**values, version=entry.version + 1, status=Entry.Status.PENDING, review_reason="", updated_at=timezone.now())
    if not changed:
        raise ValidationError("内容版本已变化，请刷新后再提交。")
    entry.refresh_from_db()
    snapshot(entry)
    ReviewEvent.objects.create(entry=entry, actor=user, version=entry.version, action="resubmit")
    return entry

@transaction.atomic
def review_entry(user, pk, version, action, reason):
    require_active(user)
    if not is_reviewer(user):
        raise PermissionDenied("只有负责人可以审核。")
    entry = Entry.objects.get(pk=pk)
    if entry.owner_id == user.id:
        raise PermissionDenied("请由另一位负责人审核自己的经验。")
    if not reason.strip() or len(reason) > 1000:
        raise ValidationError("请填写 1 至 1,000 字审核意见。")
    targets = {"approve": Entry.Status.APPROVED, "reject": Entry.Status.REJECTED, "withdraw": Entry.Status.WITHDRAWN}
    if action not in targets:
        raise ValidationError("无效审核操作。")
    if action == "approve":
        from activities.retros import entry_source_current
        if not entry_source_current(entry):
            raise ValidationError("来源复盘已失效或事实已变化，不能批准这条派生经验。")
    expected = Entry.Status.APPROVED if action == "withdraw" else Entry.Status.PENDING
    changed = Entry.objects.filter(pk=pk, version=version, status=expected).update(status=targets[action], review_reason=reason.strip(), updated_at=timezone.now())
    if not changed:
        raise ValidationError("版本或状态已变化，请刷新。重复审核不会再次生效。")
    ReviewEvent.objects.create(entry=entry, actor=user, version=version, action=action, reason=reason.strip())
    return Entry.objects.get(pk=pk)
