"""授权资料的分析生命周期。 / Analysis lifecycle for explicitly permitted source material."""

import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from threading import RLock
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, transaction
from django.db.models import Q
from django.utils import timezone
from activities.models import Activity
from .models import ModelConfiguration, AnalysisPermission, AnalysisRun, Material, Audit, Person, Evidence
from .security import is_manager, MANAGER_GROUP
from .ai_client import analyze_sources, AIError
from .credentials import get_key, CredentialError

# 设置、许可、取消和发送前检查共用本进程锁。 / Share this process-local lock across settings, consent, cancellation, and dispatch checks.
CONFIG_LOCK = RLock()
POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qinglian-analysis")
DEFAULT_EVENT_QUESTION = "请基于所给资料总结活动的事实、结果、问题和下一次的改进。未知人数和成员贡献必须标为资料不足。"


def configuration():
    return ModelConfiguration.objects.get_or_create(pk=1)[0]


def configuration_hash(config):
    data = [config.mode, config.base_url, config.model, config.enabled, config.local_auth, config.updated_at.isoformat()]
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()


def ready(config):
    if not config.enabled or config.mode == "disabled":
        return False, "模型尚未启用，资料仅在本地归档。"
    if config.mode == "cloud" or config.local_auth:
        try:
            if not get_key():
                return False, "未配置 API Key，不能调用需要凭据的模型接口。"
        except CredentialError:
            return False, "本机凭据不可读取，请重新配置。"
    return True, ""


def _fresh_manager(user):
    actor = get_user_model().objects.filter(pk=getattr(user, "pk", None)).first()
    if actor is None or not is_manager(actor):
        raise PermissionDenied("负责人身份已失效，本次操作未执行。")
    return actor


def grant_permission(user, event, selected_ids, auto_future=False, *, expected_config_fingerprint):
    """旧配置页面不得为新服务授权。 / A stale configuration page cannot grant consent to another service."""
    with CONFIG_LOCK:
        actor = _fresh_manager(user)
        config = configuration()
        signature = configuration_hash(config)
        if not isinstance(expected_config_fingerprint, str) or expected_config_fingerprint != signature:
            raise ValidationError("模型设置已变化，请刷新后重新确认处理位置和资料范围。")
        ok, reason = ready(config)
        if not ok:
            raise ValidationError(reason)
        if not isinstance(selected_ids, (list, tuple, set)) or not selected_ids:
            raise ValidationError("请至少选择一份已解析正文。")
        try:
            ids = {uuid.UUID(str(value)) for value in selected_ids}
        except (ValueError, TypeError, AttributeError):
            raise ValidationError("资料标识无效，请刷新并重新选择。") from None
        allowed = list(event.materials.filter(pk__in=ids, excluded=False, parse_status="parsed").filter(Q(parent__isnull=True)|Q(parent__excluded=False)).values_list("pk", flat=True))
        if set(allowed) != ids:
            raise ValidationError("部分资料已变化、被排除或不属于此活动，请刷新后重新选择。")
        with transaction.atomic():
            permission, _ = AnalysisPermission.objects.update_or_create(
                event=event,
                defaults={
                    "config_fingerprint": signature,
                    "material_ids": sorted(str(value) for value in allowed),
                    "auto_future": bool(auto_future),
                    "granted_by": actor,
                },
            )
            Audit.objects.create(
                actor=actor, action="ai_permission", object_id=str(event.pk),
                detail=f"资料 {len(allowed)} 份；后续自动 {bool(auto_future)}",
            )
        return permission


def allowed_materials(config, event=None):
    query = Material.objects.filter(excluded=False, parse_status="parsed").filter(Q(parent__isnull=True)|Q(parent__excluded=False))
    if event:
        query = query.filter(event=event)
    permissions = AnalysisPermission.objects.filter(
        config_fingerprint=configuration_hash(config),
        granted_by__is_active=True,
        granted_by__groups__name=MANAGER_GROUP,
    ).distinct()
    if event:
        permissions = permissions.filter(event=event)
    allowed = []
    for permission in permissions:
        selection = Q(pk__in=permission.material_ids)
        if permission.auto_future:
            # 不把未勾选的既有资料混入“后续新增”授权。 / Future consent does not include unselected pre-existing files.
            selection |= Q(created_at__gt=permission.granted_at)
        allowed.extend(query.filter(event_id=permission.event_id).filter(selection).values_list("pk", flat=True))
    return query.filter(pk__in=allowed)


def _text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _date_range(start, end):
    values = []
    for value in (start, end):
        if value in (None, ""):
            values.append(None)
            continue
        if isinstance(value, str):
            try:
                parsed = date.fromisoformat(value)
                if value != parsed.isoformat():
                    raise ValueError
                value = parsed
            except ValueError:
                raise ValidationError("分析日期需为 YYYY-MM-DD，请重新选择。") from None
        if not isinstance(value, date) or isinstance(value, datetime):
            raise ValidationError("分析日期无效，请重新选择。")
        values.append(value)
    start, end = values
    if start and end and start > end:
        raise ValidationError("分析开始日期不能晚于结束日期。")
    return start, end


def _dated(query, field, start, end):
    # 范围按活动日期限定，未知日期不被上传时间替代。 / Filter by event dates; never substitute upload time for an unknown date.
    if start is not None or end is not None:
        query = query.filter(**{field + "__isnull": False})
    if start is not None:
        query = query.filter(**{field + "__gte": start})
    if end is not None:
        query = query.filter(**{field + "__lte": end})
    return query


def scope_fingerprint(event=None, member=None, start=None, end=None):
    """只在本地比较变更；指纹字段不因此获得外发许可。 / Compare local changes; fingerprinted fields are not authorized payloads."""
    start, end = _date_range(start, end)
    mats = Material.objects.filter(excluded=False, parse_status="parsed").filter(Q(parent__isnull=True)|Q(parent__excluded=False))
    evidence = Evidence.objects.all()
    events = Activity.objects.all()
    if event:
        mats = mats.filter(event=event)
        evidence = evidence.filter(event=event)
        events = events.filter(pk=event.pk)
    if member:
        mats = mats.filter(mentions__member=member).distinct()
        evidence = evidence.filter(member=member)
        if not event:
            events = events.filter(Q(materials__mentions__member=member) | Q(observations__member=member)).distinct()
    mats = _dated(mats, "event__recorded_date", start, end)
    evidence = _dated(evidence, "event__recorded_date", start, end)
    events = _dated(events, "recorded_date", start, end)
    values = {
        "date_scope": [start.isoformat() if start else None, end.isoformat() if end else None],
        "materials": [
            [str(mat.pk), mat.sha256, _text_hash(mat.text), str(mat.event_id), mat.original_name]
            for mat in mats.order_by("id")
        ],
        "events": [
            [str(obj.pk), obj.title, str(obj.recorded_date), obj.event_status, _text_hash(obj.description)]
            for obj in events.order_by("id")
        ],
        "evidence": [
            [str(obj.pk), obj.version, str(obj.member_id), str(obj.event_id), str(obj.material_id),
             obj.kind, obj.confidence, str(obj.occurred_on), str(obj.duplicate_of_id),
             obj.title, obj.anchor, _text_hash(obj.quote), _text_hash(obj.note)]
            for obj in evidence.order_by("id")
        ],
    }
    # 全名册的重名和别名会改变任何资料的身份归属，保守地纳入本地校验。
    # Roster-wide name/alias collisions can affect any material, so include them in the local check.
    values["identity_map"] = [
        [str(person.pk), person.external_id, person.display_name, person.role, person.interests,
         str(person.joined_on), person.active, person.is_roster,
         sorted(alias.value for alias in person.aliases.all()), _text_hash(person.notes)]
        for person in Person.objects.prefetch_related("aliases").order_by("id")
    ]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def collect_sources(config, event=None, member=None, start=None, end=None, material_ids=None):
    start, end = _date_range(start, end)
    mats = allowed_materials(config, event)
    if member:
        mats = mats.filter(mentions__member=member).distinct()
    mats = _dated(mats, "event__recorded_date", start, end)
    if material_ids is not None:
        if not isinstance(material_ids, (list, tuple, set)) or not material_ids or len(material_ids) > 40:
            raise ValidationError("请选择1至40份本活动资料。")
        try:
            selected = {uuid.UUID(str(value)) for value in material_ids}
        except (ValueError, TypeError, AttributeError):
            raise ValidationError("资料标识无效。") from None
        mats = mats.filter(pk__in=selected)
        selected_mats = list(mats.order_by("id"))
        if {mat.pk for mat in selected_mats} != selected:
            raise ValidationError("部分资料未获当前模型许可、已排除或不属于此活动。")
        if any(not mat.text.strip() or len(mat.text) > 6000 for mat in selected_mats) or sum(len(mat.text) for mat in selected_mats) > 60000:
            raise ValidationError("交接资料需有正文，每份不超过6000字、合计不超过60000字；请先缩减或整理短版。本次未发送。")
        return [{"id": str(mat.pk), "title": mat.original_name, "text": mat.text,
                 "sha256": mat.sha256, "text_hash": _text_hash(mat.text), "version": mat.version,
                 "truncated": False, "event_id": str(mat.event_id)} for mat in selected_mats]
    sources = []
    remaining = 60000
    for mat in mats.order_by("-created_at")[:40]:
        if remaining <= 0:
            break
        body = mat.text[:min(remaining, 6000)]
        if not body:
            continue
        sources.append({
            "id": str(mat.pk), "title": mat.original_name, "text": body,
            "sha256": mat.sha256, "text_hash": _text_hash(mat.text),
            "truncated": len(body) < len(mat.text), "event_id": str(mat.event_id),
        })
        remaining -= len(body)
    return sources


def run_current(run):
    try:
        start, end = _date_range(getattr(run, "start_date", None), getattr(run, "end_date", None))
        if not isinstance(run.sources, list) or not run.sources or len(run.sources) > 40:
            return False
        if run.fingerprint != scope_fingerprint(run.event, run.member, start, end):
            return False
        ids = [source["id"] for source in run.sources]
        if len(set(ids)) != len(ids):
            return False
        current_query = Material.objects.filter(pk__in=ids, excluded=False, parse_status="parsed").filter(Q(parent__isnull=True)|Q(parent__excluded=False))
        current = _dated(current_query, "event__recorded_date", start, end).in_bulk()
        for source in run.sources:
            mat = current.get(uuid.UUID(source["id"]))
            if (
                mat is None
                or mat.sha256 != source["sha256"]
                or _text_hash(mat.text) != source["text_hash"]
                or str(mat.event_id) != source["event_id"]
                or mat.original_name != source["title"]
                or not isinstance(source["text"], str)
                or not source["text"]
                or source["text"] != mat.text[:len(source["text"])]
            ):
                return False
        return True
    except (ValidationError, ValueError, TypeError, KeyError, AttributeError, Activity.DoesNotExist, Person.DoesNotExist):
        # 旧版或不完整快照按失效处理，不能造成页面500或绕过检查。 / Treat malformed snapshots as stale, never as a 500 or bypass.
        return False


def _checked_running_run(run_id):
    run = AnalysisRun.objects.select_related("event", "member", "actor").filter(pk=run_id).first()
    if run is None or run.status != "running":
        return None
    if not is_manager(run.actor):
        raise ValidationError("负责人身份已失效，本次分析已停止。")
    config = configuration()
    if not config.enabled or config.mode == "disabled":
        raise ValidationError("模型已停用，本次分析已停止。")
    if configuration_hash(config) != run.config_fingerprint or not run_current(run):
        raise ValidationError("配置、资料或身份关联已变化，本次分析已停止。")
    permitted_query = _dated(allowed_materials(config, run.event), "event__recorded_date", run.start_date, run.end_date)
    permitted = {str(value) for value in permitted_query.values_list("pk", flat=True)}
    if any(source["id"] not in permitted for source in run.sources):
        raise ValidationError("资料发送许可已变化，本次分析已停止。")
    return run, config


def _prompt(run):
    prompt = run.question
    if run.start_date is not None or run.end_date is not None:
        start_label = run.start_date.isoformat() if run.start_date else "不设开始日期"
        end_label = run.end_date.isoformat() if run.end_date else "不设结束日期"
        prompt += f"\n本次日期筛选：{start_label} 至 {end_label}（含边界）。"
        prompt += "筛选依据是资料所属活动的记录日期，未知活动日期已排除；不能据此声称正文中的每条消息或事实都发生在此时间段，具体发生时间仍须原文证据。"
    if run.member:
        target = {"id": str(run.member.pk), "display_name": run.member.display_name}
        prompt += "\n当前目标成员（仅用于指代）：" + json.dumps(target, ensure_ascii=False)
        prompt += "\n注意：所选资料中的姓名/别名关联只是候选，不代表身份或贡献已核实；资料提到的其他成员不得归给当前目标。"
    return prompt


def process_run(run_id):
    close_old_connections()
    try:
        with CONFIG_LOCK:
            if not AnalysisRun.objects.filter(pk=run_id, status="pending").update(status="running", updated_at=timezone.now()):
                return
            checked = _checked_running_run(run_id)
            if checked is None:
                return
            run, config = checked
            key = get_key() if config.mode == "cloud" or config.local_auth else ""
            if (config.mode == "cloud" or config.local_auth) and not key:
                raise ValidationError("未配置 API Key，未发送本次资料。")
            # 捕获凭据后再次检查状态与许可，避免准备阶段的取消仍发送。
            # Recheck state/consent after capturing the key so cancellations during preparation do not send.
            checked = _checked_running_run(run_id)
            if checked is None:
                return
            run, config = checked
            payload = {
                "base_url": config.base_url, "model": config.model, "api_key": key,
                "mode": config.mode, "sources": [{name: source[name] for name in ("id", "title", "text")} for source in run.sources],
                "question": _prompt(run),
            }
            if run.purpose == "handoff":
                payload["purpose"] = "handoff"
        # 到此进入发送阶段；不持锁等待网络，取消只能保证丢弃在途结果，不能撤回已发请求。
        # Dispatch starts here; release the lock during I/O. Cancellation discards in-flight results, not sent requests.
        result = analyze_sources(**payload)
        with CONFIG_LOCK:
            try:
                checked = _checked_running_run(run_id)
            except ValidationError:
                AnalysisRun.objects.filter(pk=run_id, status="running").update(
                    status="stale", error="结果返回前来源、身份、配置或许可发生变化，未采用结果。", updated_at=timezone.now()
                )
                return
            if checked is None:
                return
            AnalysisRun.objects.filter(pk=run_id, status="running").update(
                status="complete", result=result, error="", updated_at=timezone.now()
            )
    except (AIError, CredentialError, ValidationError) as exc:
        message = exc.message if isinstance(exc, (AIError, CredentialError)) else "；".join(exc.messages)
        AnalysisRun.objects.filter(pk=run_id, status__in=["pending", "running"]).update(
            status="failed", error=message[:300], updated_at=timezone.now()
        )
    except Exception:
        AnalysisRun.objects.filter(pk=run_id, status__in=["pending", "running"]).update(
            status="failed", error="分析暂时失败，原件仍在，可手动重试。", updated_at=timezone.now()
        )
    finally:
        close_old_connections()


def request_analysis(user, question, event=None, member=None, token=None, background=True, start=None, end=None, *, purpose="general", material_ids=None):
    if purpose not in {"general", "handoff"} or (purpose == "handoff" and (event is None or member is not None or material_ids is None)):
        raise ValidationError("交接请求必须指定一个活动及资料范围。")
    if not isinstance(question, str) or not question.strip() or len(question) > 1000:
        raise ValidationError("问题需为1至1000字。")
    start, end = _date_range(start, end)
    with CONFIG_LOCK:
        actor = _fresh_manager(user)
        config = configuration()
        ok, reason = ready(config)
        if not ok:
            raise ValidationError(reason)
        signature = configuration_hash(config)
        fingerprint = scope_fingerprint(event, member, start, end)
        sources = collect_sources(config, event, member, start, end, material_ids=material_ids)
        if not sources:
            raise ValidationError("没有可用且已获准发送的正文；请先解析并授权资料。")
        if token:
            try:
                token = uuid.UUID(str(token))
            except (ValueError, TypeError, AttributeError):
                raise ValidationError("请求标识无效，请刷新后重试。") from None
            existing = AnalysisRun.objects.filter(pk=token).first()
            if existing:
                if existing.purpose != purpose or (purpose == "handoff" and existing.sources != sources):
                    raise ValidationError("请求标识已用于不同资料或用途，请刷新后重试。")
                if (existing.actor_id, existing.event_id, existing.member_id, existing.question, existing.start_date, existing.end_date) != (
                    actor.pk, getattr(event, "pk", None), getattr(member, "pk", None), question, start, end
                ):
                    raise ValidationError("请求标识已用于不同分析，请刷新后重试。")
                return existing
        existing_runs = AnalysisRun.objects.filter(
            fingerprint=fingerprint, config_fingerprint=signature, question=question,
            event=event, member=member, status__in=["pending", "running", "complete"],
            start_date=start, end_date=end, purpose=purpose,
        )
        if purpose == "handoff":
            # 新的交接请求生成新版本；同一请求标识仍幂等。 / New handover tokens create new versions; identical tokens remain idempotent.
            existing_runs = existing_runs.none()
        for existing in existing_runs:
            # 许可范围变更后不能复用另一组资料的待处理请求。 / A changed consent scope cannot reuse queued work for a different selection.
            if existing.sources == sources and run_current(existing):
                return existing
        kwargs = {
            "actor": actor, "event": event, "member": member, "question": question,
            "sources": sources, "fingerprint": fingerprint, "config_fingerprint": signature,
            "start_date": start, "end_date": end,
            "purpose": purpose,
        }
        if token:
            kwargs["id"] = token
        run = AnalysisRun(**kwargs)
        if not run_current(run):
            raise ValidationError("资料在准备期间发生变化，请刷新后重新分析；本次未发送。")
        run.save(force_insert=True)
        if background:
            transaction.on_commit(lambda: POOL.submit(process_run, run.pk))
        return run


def auto_analyze(user, event):
    config = configuration()
    permission = AnalysisPermission.objects.filter(
        event=event, auto_future=True, config_fingerprint=configuration_hash(config)
    ).first()
    if permission:
        try:
            return request_analysis(
                user, DEFAULT_EVENT_QUESTION,
                event=event,
            )
        except (ValidationError, PermissionDenied):
            return None
