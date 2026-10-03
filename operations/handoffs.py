"""有来源的人审交接版本。 / Source-bound, human-reviewed handover versions."""

import copy
import html
import re
import uuid
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, F
from django.utils import timezone
from .analysis import CONFIG_LOCK, _fresh_manager, _text_hash, configuration, request_analysis, run_current
from .handoff_ai import validate_handoff_result
from .models import HandoffPack, HandoffItem, Material, Audit

QUESTION = "请整理这次活动的可复用做法、踩坑提醒与待确认事项。区分材料记录和下次建议，保留适用条件与逐字出处。"
EDIT_FIELDS = ("section", "title", "record", "suggestion", "conditions")


class Conflict(ValidationError):
    pass


def sources_current(pack):
    """仅检查真正使用的来源，不依赖名册或模型设置。 / Check used sources only, independent of roster and model configuration."""
    try:
        sources = pack.source_manifest
        if not isinstance(sources, list) or not 1 <= len(sources) <= 40:
            return False
        ids = [uuid.UUID(s["id"]) for s in sources]
        if len(ids) != len(set(ids)):
            return False
        mats = Material.objects.filter(pk__in=ids, excluded=False, parse_status="parsed").filter(Q(parent__isnull=True) | Q(parent__excluded=False)).in_bulk()
        for source, sid in zip(sources, ids):
            mat = mats.get(sid)
            if mat is None or mat.event_id != pack.event_id or str(mat.event_id) != source["event_id"]:
                return False
            if mat.original_name != source["title"] or mat.sha256 != source["sha256"] or _text_hash(mat.text) != source["text_hash"]:
                return False
            if not source["text"] or mat.text != source["text"]:
                return False
        return True
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def create_pack(user, event, title, scope, material_ids, token, parent=None, background=True):
    with CONFIG_LOCK, transaction.atomic():
        actor = _fresh_manager(user)
        token = uuid.UUID(str(token))
        if parent and parent.event_id != event.pk:
            raise ValidationError("父版本不属于此活动。")
        existing = HandoffPack.objects.filter(pk=token).first()
        if existing:
            selected = {str(uuid.UUID(str(value))) for value in material_ids}
            if existing.created_by_id != actor.pk or existing.event_id != event.pk or existing.title != title or existing.scope != scope or existing.parent_id != getattr(parent, "pk", None) or selected != {s["id"] for s in existing.source_manifest}:
                raise Conflict("请求已用于另一份交接包，请刷新后重试。")
            return existing
        run = request_analysis(actor, QUESTION, event=event, token=token, background=background, purpose="handoff", material_ids=material_ids)
        config = configuration()
        pack = HandoffPack.objects.create(
            id=token, event=event, source_analysis=run, parent=parent, title=title, scope=scope,
            event_title=event.title, source_manifest=copy.deepcopy(run.sources), created_by=actor,
            config_snapshot={"mode": config.mode, "service": config.base_url, "model": config.model, "fingerprint": run.config_fingerprint},
        )
        Audit.objects.create(actor=actor, action="handoff_create", object_id=str(pack.pk))
        return pack


def _lock_pack(user, pack_id, expected, require_current=True):
    actor = _fresh_manager(user)
    try:
        expected = int(expected)
    except (TypeError, ValueError):
        raise Conflict("页面版本无效，请重新打开交接包。") from None
    # 条件更新提供SQLite上的乐观并发保护。 / Conditional updates provide optimistic concurrency on SQLite.
    changed = HandoffPack.objects.filter(pk=pack_id, lock_version=expected, status="draft").update(lock_version=F("lock_version") + 1, updated_at=timezone.now())
    if not changed:
        raise Conflict("此版本已确认或被其他页面修改，请刷新后核对。")
    pack = HandoffPack.objects.select_related("source_analysis", "event").get(pk=pack_id)
    if require_current and not sources_current(pack):
        raise ValidationError("来源已变化，不能继续确认或导出，请用当前材料重新生成。")
    return actor, pack


def initialize_pack(user, pack_id, expected):
    with CONFIG_LOCK, transaction.atomic():
        _fresh_manager(user)
        current = HandoffPack.objects.get(pk=pack_id)
        if current.initialized_at:
            return current
        actor, pack = _lock_pack(user, pack_id, expected)
        run = pack.source_analysis
        if run.status != "complete" or not run_current(run):
            raise ValidationError("生成尚未成功，或生成阶段来源已变化，请重新生成。")
        result = validate_handoff_result(run.result, {s["id"]: s["text"] for s in pack.source_manifest})
        for position, item in enumerate(result["items"], 1):
            HandoffItem.objects.create(pack=pack, position=position, original=copy.deepcopy(item), citations=copy.deepcopy(item["citations"]), **{key: item[key] for key in EDIT_FIELDS})
        pack.initialized_at = timezone.now()
        pack.save(update_fields=["initialized_at"])
        Audit.objects.create(actor=actor, action="handoff_prepare", object_id=str(pack.pk))
        return pack


def edit_item(user, pack_id, item_id, expected, values, action, acknowledge=False):
    if action not in ("save", "keep", "drop"):
        raise ValidationError("不支持的审阅操作。")
    with CONFIG_LOCK, transaction.atomic():
        actor, pack = _lock_pack(user, pack_id, expected)
        if not pack.initialized_at:
            raise ValidationError("请先准备审阅草稿。")
        item = HandoffItem.objects.get(pk=item_id, pack=pack)
        changed = any(getattr(item, key) != values[key] for key in EDIT_FIELDS)
        for key in EDIT_FIELDS:
            setattr(item, key, values[key])
        if not item.citations and (item.section != "question" or item.record.strip()):
            raise ValidationError("无文件依据的条目只能作为待确认事项，材料记录需留空。")
        if action == "keep" and not changed and not acknowledge:
            raise ValidationError("请先核对当前表述与原文，再勾选审阅确认。")
        item.edited = any(values[key] != item.original[key] for key in EDIT_FIELDS)
        if changed or action == "save":
            item.decision, item.reviewed_by, item.reviewed_at = "pending", None, None
        else:
            item.decision = action
            item.reviewed_by, item.reviewed_at = actor, timezone.now()
        item.save()
        Audit.objects.create(actor=actor, action="handoff_item", object_id=str(item.pk), detail=f"{item.decision}; revised={item.edited}")
        return pack


def confirm_pack(user, pack_id, expected, reviewer_label):
    if not isinstance(reviewer_label, str) or not reviewer_label.strip() or len(reviewer_label) > 100:
        raise ValidationError("请填写用于交接文件的确认人显示名。")
    with CONFIG_LOCK, transaction.atomic():
        actor, pack = _lock_pack(user, pack_id, expected)
        items = list(pack.items.all())
        if not pack.initialized_at or not items or any(item.decision == "pending" for item in items):
            raise ValidationError("请先将每条内容明确采用或排除。")
        if not any(item.decision == "keep" for item in items):
            raise ValidationError("至少采用一条内容，不能确认空交接包。")
        source_texts = {s["id"]: s["text"] for s in pack.source_manifest}
        validate_handoff_result({"items": [{**{k: getattr(item, k) for k in EDIT_FIELDS}, "citations": item.citations} for item in items if item.decision == "keep"]}, source_texts)
        pack.status = "confirmed"
        pack.confirmed_by, pack.confirmed_at = actor, timezone.now()
        pack.reviewer_label = reviewer_label.strip()
        pack.save(update_fields=["status", "confirmed_by", "confirmed_at", "reviewer_label"])
        Audit.objects.create(actor=actor, action="handoff_confirm", object_id=str(pack.pk))
        return pack


def copy_pack(user, pack_id, token):
    with CONFIG_LOCK, transaction.atomic():
        actor = _fresh_manager(user)
        source = HandoffPack.objects.get(pk=pack_id)
        if source.status != "confirmed" or not sources_current(source):
            raise ValidationError("仅可复制来源仍有效的确认版；来源变化时请重新生成。")
        token = uuid.UUID(str(token))
        existing = HandoffPack.objects.filter(pk=token).first()
        if existing:
            if existing.parent_id != source.pk or existing.created_by_id != actor.pk:
                raise Conflict("复制请求标识已被使用。")
            return existing
        pack = HandoffPack.objects.create(id=token, event=source.event, source_analysis=source.source_analysis, parent=source,
            title=source.title, event_title=source.event_title, scope=source.scope, created_by=actor,
            source_manifest=copy.deepcopy(source.source_manifest), config_snapshot=copy.deepcopy(source.config_snapshot), initialized_at=timezone.now())
        for item in source.items.all():
            HandoffItem.objects.create(pack=pack, position=item.position, original=copy.deepcopy(item.original), citations=copy.deepcopy(item.citations), edited=item.edited, **{k: getattr(item, k) for k in EDIT_FIELDS})
        Audit.objects.create(actor=actor, action="handoff_copy", object_id=str(pack.pk))
        return pack


def export_data(pack):
    if pack.status != "confirmed" or not sources_current(pack):
        raise ValidationError("仅能导出来源有效的已确认版本。")
    sources = {s["id"]: s for s in pack.source_manifest}
    entries, refs = [], []
    for item in pack.items.filter(decision="keep"):
        numbers = []
        for citation in item.citations:
            ref = {"title": sources[citation["source_id"]]["title"], "quote": citation["quote"]}
            if ref not in refs:
                refs.append(ref)
            numbers.append(refs.index(ref) + 1)
        entries.append({**{key: getattr(item, key) for key in EDIT_FIELDS}, "edited": item.edited, "references": numbers})
    return {"title": pack.title, "event": pack.event_title, "scope": pack.scope,
            "version": str(pack.pk), "confirmed_at": timezone.localtime(pack.confirmed_at).strftime("%Y-%m-%d %H:%M %Z"),
            "reviewer": pack.reviewer_label, "entries": entries, "references": refs,
            "sources": [s["title"] for s in pack.source_manifest], "sections": HandoffItem.SECTION_CHOICES}


def _markdown_text(value):
    # 导出文本不能成为Markdown脚本或外链语法。 / Escape exported text so it cannot become scripts or link syntax.
    text = html.escape(str(value), quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", text)


def export_markdown(data):
    clean = _markdown_text
    lines = [f"# {clean(data['title'])}", "", f"活动：{clean(data['event'])}", f"适用范围：{clean(data['scope']) or '仅限本次活动资料范围'}",
             f"版本：{data['version']}", f"确认时间：{data['confirmed_at']}", f"确认人：{clean(data['reviewer'])}",
             "", "已审阅表示负责人决定交付；不代表系统自动验证事实。", ""]
    for section, label in data["sections"]:
        lines.extend([f"## {label}", ""])
        rows = [item for item in data["entries"] if item["section"] == section]
        if not rows:
            lines.extend(["本版没有采用的内容。", ""])
        for item in rows:
            lines.extend([f"### {clean(item['title'])}", ""])
            for key, label in (("record", "材料记录"), ("suggestion", "下次建议"), ("conditions", "适用条件与未知项")):
                if item[key]:
                    lines.extend([f"**{label}：** {clean(item[key])}", ""])
            lines.extend(["来源：" + ("、".join(f"[{n}]" for n in item["references"]) or "资料不足，作为待确认事项"),
                          "负责人修订：" + ("是" if item["edited"] else "否"), ""])
    lines.extend(["## 来源摘录", ""])
    for number, ref in enumerate(data["references"], 1):
        lines.extend([f"### [{number}] {clean(ref['title'])}", "", clean(ref["quote"]), ""])
    lines.extend(["## 资料覆盖与局限", "", "本包仅基于以下资料，不代表其他活动或全部社团经验："])
    lines.extend(f"- {clean(title)}" for title in data["sources"])
    lines.extend(["", "下载文件是确认时的快照，不会随原资料自动更新。待确认事项仍需核实。公开分享前请检查文字与引用是否适合公开。", ""])
    return "\n".join(lines)
