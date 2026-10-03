"""名册与有出处的成员观察服务。 / Roster and source-backed member observation services."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from activities.models import Activity
from .models import Alias, AnalysisRun, Audit, Evidence, Material, Person
from .security import is_manager


_ROSTER_FIELDS = ("id", "name", "role", "interests", "aliases", "joined_on")
_EVIDENCE_FIELDS = ("member", "event", "title", "kind", "confidence", "occurred_on", "material", "anchor", "quote", "note")


def _require_manager(user):
    if not is_manager(user):
        raise PermissionDenied("仅有效的社团负责人可以修改名册或成员证据。")


def _identifier(value):
    return value.pk if hasattr(value, "pk") else value


def _text(value):
    return "" if value is None else str(value).strip()


def _iso_date(value, label):
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    value = _text(value)
    # Excel 日期单元格常被表示为午夜，不猜测其他含糊日期。 / Excel date cells may serialize as midnight; do not guess ambiguous dates.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]00:00:00(?:\.0+)?", value):
        value = value[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValidationError(f"{label}须为 YYYY-MM-DD，不会自动猜测日期。")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{label}不是有效日期。") from exc


def _aliases(value):
    names = list(dict.fromkeys(part.strip() for part in re.split("[,，]", _text(value)) if part.strip()))
    if any(len(name) > 120 for name in names):
        raise ValidationError("单个别名不得超过 120 字。")
    return names


def _invalidate_analysis(event_ids=(), member_ids=()):
    # 包含整体分析；成员与活动范围可能交叉，宁可明确待更新也不继续显示旧结论。
    # Include whole-club runs; crossed event/member scopes must not retain old conclusions.
    relevant = Q(event__isnull=True, member__isnull=True)
    if event_ids:
        relevant |= Q(event_id__in=set(event_ids))
    if member_ids:
        relevant |= Q(member_id__in=set(member_ids))
    AnalysisRun.objects.filter(relevant, status__in=["complete", "pending", "running"]).update(
        status="stale", error="成员证据或名册已变化，请重新分析。", updated_at=timezone.now()
    )


@transaction.atomic
def import_roster(user, material, mapping, table_index=0):
    """仅消费负责人已确认映射的本地表格。 / Import only locally parsed tables after mapping confirmation."""
    _require_manager(user)
    try:
        material = Material.objects.select_for_update().get(pk=_identifier(material))
    except (Material.DoesNotExist, ValueError, TypeError) as exc:
        raise ValidationError("名册来源不存在，请重新选择资料。") from exc
    if material.excluded or material.parse_status != "parsed":
        raise ValidationError("只能从已解析且未排除的资料导入名册。")
    if not isinstance(mapping, dict) or not mapping.get("id") or not mapping.get("name"):
        raise ValidationError("请先映射稳定成员编号和姓名列，不使用昵称猜测身份。")
    if not isinstance(material.tables, list):
        raise ValidationError("资料没有可用表格，请重新解析。")
    if not isinstance(table_index, int) or isinstance(table_index, bool) or not 0 <= table_index < len(material.tables):
        raise ValidationError("所选表格不存在。")
    table = material.tables[table_index]
    if not isinstance(table, dict):
        raise ValidationError("资料表格结构不完整，请重新解析。")
    columns, rows = table.get("columns"), table.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise ValidationError("资料表格结构不完整，请重新解析。")
    indexes = {}
    for field in _ROSTER_FIELDS:
        selected = mapping.get(field)
        if not selected:
            continue
        if selected not in columns:
            raise ValidationError(f"映射列“{selected}”不存在，请重新确认。")
        if columns.count(selected) != 1:
            raise ValidationError(f"映射列“{selected}”重名，请先整理表头。")
        indexes[field] = columns.index(selected)

    result = {"created": 0, "updated": 0, "issues": []}
    groups = defaultdict(list)
    for row_number, row in enumerate(rows, start=2):
        if not isinstance(row, (list, tuple)):
            result["issues"].append(f"第 {row_number} 行：结构不受支持，未导入。")
            continue
        values = {field: _text(row[index]) if index < len(row) else "" for field, index in indexes.items()}
        if not any(values.values()):
            continue
        if not values["id"]:
            result["issues"].append(f"第 {row_number} 行：缺少稳定成员编号，未按姓名合并。")
            continue
        groups[values["id"]].append((row_number, values))

    changed_members = []
    for external_id, group in groups.items():
        # 同批冲突先全部发现，再决定是否写入，避免第一行抢先污染人员身份。
        # Detect all same-batch conflicts before writing any row for that identity.
        signatures = {json.dumps(values, sort_keys=True, ensure_ascii=False) for _, values in group}
        if len(signatures) > 1:
            for row_number, _ in group:
                result["issues"].append(f"第 {row_number} 行：编号“{external_id}”在本批存在冲突，各冲突行均未导入。")
            continue
        row_number, values = group[0]
        for duplicate_number, _ in group[1:]:
            result["issues"].append(f"第 {duplicate_number} 行：与第 {row_number} 行内容重复，未重复导入。")
        try:
            with transaction.atomic():
                if not values["name"]:
                    raise ValidationError("缺少姓名，未导入。")
                aliases = _aliases(values.get("aliases", ""))
                joined_on = _iso_date(values.get("joined_on"), "加入日期") if "joined_on" in indexes else None
                person = Person.objects.select_for_update().filter(external_id=external_id).first()
                created = person is None
                if person is not None and person.display_name != values["name"]:
                    raise ValidationError("现有编号对应的姓名不同，请人工核对，未覆盖现有成员。")
                if person is None:
                    person = Person(external_id=external_id, display_name=values["name"], is_roster=True)
                before = {field: getattr(person, field) for field in ("role", "interests", "joined_on", "is_roster")}
                for field in ("role", "interests"):
                    if field in indexes:
                        setattr(person, field, values[field])
                if "joined_on" in indexes:
                    person.joined_on = joined_on
                person.is_roster = True
                person.full_clean()
                changed = created or any(getattr(person, field) != value for field, value in before.items())
                if changed:
                    person.save()
                added_alias = False
                for alias in aliases:
                    _, added = Alias.objects.get_or_create(member=person, value=alias)
                    added_alias = added_alias or added
                if created:
                    result["created"] += 1
                elif changed or added_alias:
                    result["updated"] += 1
                if changed or added_alias:
                    changed_members.append(person.pk)
        except ValidationError as exc:
            result["issues"].append(f"第 {row_number} 行：{'；'.join(exc.messages)}")
        except IntegrityError:
            result["issues"].append(f"第 {row_number} 行：该编号在处理期间发生变化，请重新导入并核对。")

    if changed_members:
        # 名册及别名可改变所有资料的身份关联，因此让旧分析统一待更新。
        # Roster/alias changes can affect identity matching across every material.
        AnalysisRun.objects.filter(status__in=["complete", "pending", "running"]).update(
            status="stale", error="名册或别名已更新，请重新核对关联并分析。", updated_at=timezone.now()
        )
    Audit.objects.create(
        actor=user, action="import_roster", object_id=str(material.pk),
        detail=f"新增 {result['created']}，更新 {result['updated']}，待核对 {len(result['issues'])}。",
    )
    return result


def active_evidence():
    """只有主记录和有效来源参与统计；人工补充不依赖文件。 / Count root evidence backed by valid sources, including manual entries."""
    return Evidence.objects.filter(duplicate_of__isnull=True).filter(
        Q(material__isnull=True)
        | (Q(material__excluded=False, material__parse_status="parsed", material__event_id=F("event_id")) & (Q(material__parent__isnull=True)|Q(material__parent__excluded=False)))
    )


def _related(model, value, label, *, optional=False):
    if optional and value in (None, ""):
        return None
    try:
        return model.objects.get(pk=_identifier(value))
    except (model.DoesNotExist, ValueError, TypeError, ValidationError) as exc:
        raise ValidationError(f"{label}不存在，请重新选择。") from exc


def _evidence_values(data, current=None):
    def provided(field, default=""):
        if field in data:
            return data[field]
        return getattr(current, field) if current is not None else default

    material = _related(Material, provided("material", None), "来源资料", optional=True)
    event = _related(Activity, provided("event"), "活动")
    values = {
        "member": _related(Person, provided("member"), "成员"),
        "event": event,
        "material": material,
        "title": _text(provided("title")),
        "kind": _text(provided("kind")),
        "confidence": _text(provided("confidence", Evidence.Confidence.CANDIDATE)),
        "occurred_on": _iso_date(provided("occurred_on", None), "证据日期"),
        "anchor": _text(provided("anchor")),
        "quote": _text(provided("quote")),
        "note": _text(provided("note")),
    }
    if material is not None:
        if material.event_id != event.pk:
            raise ValidationError("来源资料不属于所选活动。")
        if material.excluded or material.parse_status != "parsed":
            raise ValidationError("来源资料已排除或尚未解析，不能作为当前证据。")
        if not values["quote"] or values["quote"] not in material.text:
            raise ValidationError("引用必须逐字存在于所选资料正文中，不能用改写或推断代替。")
    elif not values["quote"]:
        raise ValidationError("人工补充须写明线下依据，不能只填写判断。")
    return values


def _dedupe_key(values):
    # 消息数量、字数、可信度和措辞补充均不制造新的贡献事件。
    # Message count, text length, confidence, and notes never create extra contribution events.
    title = unicodedata.normalize("NFKC", " ".join(values["title"].split())).casefold()
    payload = [
        title, str(values["member"].pk), str(values["event"].pk), values["kind"],
        values["occurred_on"].isoformat() if values["occurred_on"] else None,
        str(values["material"].pk) if values["material"] else "manual",
    ]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _current_root(evidence):
    # 合并后的旧去重键仍有效，但返回主记录，绝不重新激活被合并项。
    # A merged key remains reserved; return its root without reactivating the duplicate.
    return evidence.duplicate_of if evidence.duplicate_of_id else evidence


def _expected_version(value):
    if isinstance(value, bool):
        raise ValidationError("请刷新页面后使用当前记录版本。")
    try:
        version = int(value)
    except (ValueError, TypeError) as exc:
        raise ValidationError("修改证据必须携带当前版本，请刷新页面。") from exc
    if version < 1 or str(version) != str(value):
        raise ValidationError("记录版本不合法，请刷新页面。")
    return version


@transaction.atomic
def save_evidence(user, data, instance=None):
    _require_manager(user)
    current = None
    expected_version = None
    if instance is not None:
        expected_version = _expected_version(data.get("version"))
        current = _related(Evidence, instance, "证据")
        if current.version != expected_version:
            raise ValidationError("记录版本已变化，请刷新；未覆盖他人的修改。")
        if current.duplicate_of_id:
            raise ValidationError("该记录已合并，请修改对应主记录。")
    values = _evidence_values(data, current)
    dedupe_key = _dedupe_key(values)
    duplicate = Evidence.objects.filter(dedupe_key=dedupe_key).exclude(pk=current.pk if current else None).select_related("duplicate_of").first()
    if duplicate is not None:
        if current is not None:
            raise ValidationError("修改后将与已有证据重复，请使用明确的合并操作，原记录未改动。")
        return _current_root(duplicate)

    candidate = Evidence(**values, dedupe_key=dedupe_key)
    if current:
        candidate.pk = current.pk
        if current.member_id != values["member"].pk and Evidence.objects.filter(duplicate_of=current).exists():
            raise ValidationError("该主记录仍有合并来源，不能直接改为其他成员。")
    candidate.full_clean(validate_unique=False)
    if current is None:
        try:
            with transaction.atomic():
                candidate.save(force_insert=True)
        except IntegrityError:
            existing = Evidence.objects.filter(dedupe_key=dedupe_key).select_related("duplicate_of").first()
            if existing is None:
                raise
            return _current_root(existing)
        Audit.objects.create(actor=user, action="evidence_create", object_id=str(candidate.pk), detail="建立独立事项证据；未按发言数量计分。")
        _invalidate_analysis([candidate.event_id], [candidate.member_id])
        return candidate

    differences = any(
        getattr(current, f"{field}_id") != getattr(candidate, f"{field}_id") if field in {"member", "event", "material"}
        else getattr(current, field) != getattr(candidate, field)
        for field in _EVIDENCE_FIELDS
    )
    if not differences:
        return current
    try:
        with transaction.atomic():
            changed = Evidence.objects.filter(pk=current.pk, version=expected_version, duplicate_of__isnull=True).update(
                **values, dedupe_key=dedupe_key, version=F("version") + 1
            )
    except IntegrityError as exc:
        raise ValidationError("同一事项在处理期间已有新记录，请刷新并核对是否需要合并。") from exc
    if not changed:
        raise ValidationError("记录版本已变化，请刷新；本次修改未生效。")
    _invalidate_analysis([current.event_id, candidate.event_id], [current.member_id, candidate.member_id])
    Audit.objects.create(
        actor=user, action="evidence_update", object_id=str(current.pk),
        detail=f"版本 {expected_version} → {expected_version + 1}；相关分析已标待更新。",
    )
    return Evidence.objects.get(pk=current.pk)


@transaction.atomic
def merge_evidence(user, source, target):
    _require_manager(user)
    source_id, target_id = _identifier(source), _identifier(target)
    if source_id == target_id:
        raise ValidationError("不能把证据合并到自身。")
    source_version = _expected_version(getattr(source, "version", None))
    target_version = _expected_version(getattr(target, "version", None))
    locked = {
        str(record.pk): record
        for record in Evidence.objects.select_for_update().filter(pk__in=[source_id, target_id]).order_by("pk")
    }
    source, target = locked.get(str(source_id)), locked.get(str(target_id))
    if source is None or target is None:
        raise ValidationError("待合并记录不存在，请刷新。")
    if source.version != source_version or target.version != target_version:
        raise ValidationError("待合并记录的版本已变化，请刷新后核对。")
    if source.member_id != target.member_id:
        raise ValidationError("不同成员的记录不能合并，不自动分配团队成果。")
    if target.duplicate_of_id:
        raise ValidationError("合并目标必须是未被合并的主记录。")
    if source.duplicate_of_id:
        if source.duplicate_of_id == target.pk:
            return target
        raise ValidationError("来源已合并到另一主记录，请重新核对。")
    children = list(Evidence.objects.select_for_update().filter(duplicate_of=source))
    if any(child.member_id != target.member_id for child in children):
        raise ValidationError("已有合并关系包含身份冲突，需先人工核对。")
    changed = Evidence.objects.filter(pk=source.pk, version=source_version, duplicate_of__isnull=True).update(
        duplicate_of=target, version=F("version") + 1
    )
    if not changed:
        raise ValidationError("来源版本已变化，合并未执行。")
    if children:
        Evidence.objects.filter(pk__in=[child.pk for child in children]).update(duplicate_of=target, version=F("version") + 1)
    # 主记录不自动继承更高可信度；合并只是去重而不是事实核验。
    # The target never inherits higher confidence: merging is deduplication, not verification.
    target_changed = Evidence.objects.filter(pk=target.pk, version=target_version, duplicate_of__isnull=True).update(version=F("version") + 1)
    if not target_changed:
        raise ValidationError("目标记录在处理期间变化，合并未执行。")
    _invalidate_analysis([source.event_id, target.event_id, *(child.event_id for child in children)], [target.member_id])
    Audit.objects.create(
        actor=user, action="evidence_merge", object_id=str(source.pk),
        detail=f"合并至 {target.pk}；保留来源原记录，连带来源 {len(children)} 条；未升级可信度。",
    )
    return Evidence.objects.get(pk=target.pk)
