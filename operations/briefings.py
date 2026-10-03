"""有版本、出处和幂等保护的活动准备。 / Versioned, evidence-bound, idempotent event preparation."""
import json
import uuid
from datetime import timedelta
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from .analysis import CONFIG_LOCK, _fresh_manager, configuration, configuration_hash, allowed_materials, ready
from .ai_client import analyze_sources, AIError
from .credentials import get_key, CredentialError
from .handoffs import export_data, Conflict, _markdown_text
from .models import BriefingSession, HandoffPack


def guard(user, pack_id, *, sending=False):
    _fresh_manager(user)
    pack = HandoffPack.objects.select_related("event").get(pk=pack_id)
    try:
        data = export_data(pack)
    except ValidationError:
        raise ValidationError("交接版本或来源已失效，请重新确认。 / Handover or sources changed. Review a new version.") from None
    config = configuration()
    permitted = {str(v) for v in allowed_materials(config, pack.event).values_list("pk", flat=True)}
    if not {s["id"] for s in pack.source_manifest}.issubset(permitted):
        raise ValidationError("资料处理许可已变化，请重新确认。 / Processing permission changed; review consent.")
    if sending:
        ok, reason = ready(config)
        if not ok:
            raise ValidationError(reason + " / Model unavailable; check settings.")
    entries = []
    for item, row in zip(pack.items.filter(decision="keep"), data["entries"]):
        entries.append({**row, "id": str(item.pk), "evidence": [data["references"][i - 1] for i in row["references"]], "has_evidence": bool(row["references"])})
    return pack, config, data, entries


def checked_context(value):
    if not isinstance(value, dict) or set(value) != {"goal", "expected_attendees", "constraints", "language"}:
        raise ValidationError("活动信息格式无效。 / Invalid event context.")
    for field, limit in (("goal", 300), ("constraints", 500)):
        if not isinstance(value[field], str) or len(value[field]) > limit:
            raise ValidationError("活动描述过长或格式无效。 / Invalid event description.")
    if not value["goal"].strip() or value["language"] not in ("zh", "en"):
        raise ValidationError("请填写活动目标和语言。 / Enter a goal and supported language.")
    count = value["expected_attendees"]
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100000:
        raise ValidationError("预计人数需为1至100000的整数。 / Expected attendance must be 1–100000.")
    return value


def create(user, pack_id, context, request_id):
    context = checked_context(context)
    with CONFIG_LOCK, transaction.atomic():
        guard(user, pack_id)
        session, created = BriefingSession.objects.get_or_create(pk=request_id, defaults={"owner": user, "pack_id": pack_id, "context": context})
        if session.owner_id != user.pk or session.pack_id != pack_id or session.context != context:
            raise Conflict("请求标识已被使用，请刷新。 / Request identifier already used; reload.")
        return session


def owned(user, pack_id, session_id):
    _fresh_manager(user)
    return BriefingSession.objects.get(pk=session_id, owner=user, pack_id=pack_id)


def _claim(session, revision, **updates):
    if type(revision) is not int or not BriefingSession.objects.filter(pk=session.pk, revision=revision).update(revision=F("revision") + 1, updated_at=timezone.now(), **updates):
        raise Conflict("另一个页面已更新，请刷新后继续。 / Another page changed this preparation; reload.")
    session.refresh_from_db()


def ask(user, pack_id, session_id, question, request_id, revision):
    if not isinstance(question, str) or not question.strip() or len(question) > 1000:
        raise ValidationError("问题需为1至1000字。 / Question must contain 1–1000 characters.")
    question = question.strip()
    token = str(request_id)
    with CONFIG_LOCK, transaction.atomic():
        session = owned(user, pack_id, session_id)
        pack, config, data, entries = guard(user, pack_id, sending=True)
        for turn in session.turns:
            if turn["request_id"] == token:
                if turn["question"] != question:
                    raise Conflict("请求标识与问题不一致。 / Request identifier belongs to another question.")
                if turn.get("error"):
                    raise AIError(turn["error"])
                return session
        if session.pending and session.pending_at > timezone.now() - timedelta(minutes=3):
            raise Conflict("已有问题正在处理，请稍后刷新。 / A request is running; refresh shortly.")
        if len(session.turns) >= 40:
            raise ValidationError("本轮已达40次，请新建准备会话。 / Start a new preparation after 40 turns.")
        _claim(session, revision, pending=request_id, pending_at=timezone.now())
        fingerprint = configuration_hash(config)
        history = [{"question": t["question"], "response": t["response"]} for t in session.turns if t.get("response")][-5:]
        query = json.dumps({"event_context": session.context, "history": history, "question": question}, ensure_ascii=False)
        if len(query) > 6000:
            # 限制历史总量，当前问题与活动条件不截断。 / Drop oldest history, never truncate the current question/context.
            while history and len(query) > 6000:
                history.pop(0)
                query = json.dumps({"event_context": session.context, "history": history, "question": question}, ensure_ascii=False)
        sources = [{"id": e["id"], "title": e["title"], "text": json.dumps({k: e[k] for k in ("record", "suggestion", "conditions", "evidence", "has_evidence")}, ensure_ascii=False)} for e in entries]
        try:
            key = get_key() if config.mode == "cloud" or config.local_auth else ""
        except CredentialError:
            raise ValidationError("模型凭据不可读取。 / Model credentials unavailable.") from None
    error, response = "", None
    try:
        response = analyze_sources(base_url=config.base_url, model=config.model, mode=config.mode, api_key=key, sources=sources, question=query, purpose="briefing")
    except AIError as exc:
        error = exc.code
    with CONFIG_LOCK, transaction.atomic():
        session = owned(user, pack_id, session_id)
        if str(session.pending) != token:
            raise Conflict("会话已重置，本次结果未采用。 / Preparation was reset; response discarded.")
        try:
            _, current, _, _ = guard(user, pack_id)
            if fingerprint != configuration_hash(current):
                raise ValidationError("模型设置已变化，本次结果未采用。 / Model settings changed; response discarded.")
        except ValidationError:
            BriefingSession.objects.filter(pk=session.pk, pending=request_id).update(pending=None, pending_at=None)
            # 让清理提交后再报告失效。 / Commit pending cleanup before reporting stale state.
            stale = True
        else:
            stale = False
            session.turns.append({"request_id": token, "question": question, "response": response, "error": error})
            _claim(session, session.revision, turns=session.turns, pending=None, pending_at=None)
    if stale:
        raise ValidationError("来源、许可或模型已变化，本次结果未采用。 / Sources, consent or model changed; response discarded.")
    if error:
        raise AIError(error)
    return session


def checklist_options(entries, session):
    """从已保存回答重建建议，客户端只能提交标识。 / Rebuild saved suggestions; clients may submit identifiers only."""
    options = [{**entry, "origin": "handover"} for entry in entries]
    if not session or not entries:
        return options
    by_id = {entry["id"]: entry for entry in entries}
    en = session.context["language"] == "en"
    for turn in session.turns:
        for index, answer in enumerate((turn.get("response") or {}).get("answers", [])):
            if answer["kind"] != "suggestion" or not answer["entry_ids"] or any(key not in by_id for key in answer["entry_ids"]):
                continue
            evidence = []
            for key in answer["entry_ids"]:
                for ref in by_id[key]["evidence"]:
                    if ref not in evidence:
                        evidence.append(ref)
            options.append({"id": f"answer:{turn['request_id']}:{index}", "origin": "conversation", "section": "practice",
                            "title": answer["text"][:90], "suggestion": answer["text"],
                            "record": "Generated advice, not a recorded fact." if en else "本次生成的建议，不是材料事实。",
                            "conditions": "Check applicability to the saved event context." if en else "执行前核对是否适用于已保存的活动条件。",
                            "evidence": evidence, "has_evidence": bool(evidence)})
    return options


def update(user, pack_id, session_id, revision, selected=None, reset=False):
    with CONFIG_LOCK, transaction.atomic():
        session = owned(user, pack_id, session_id)
        _, _, _, entries = guard(user, pack_id)
        if reset:
            _claim(session, revision, turns=[], selected=[], pending=None, pending_at=None)
        else:
            valid_ids = {e["id"] for e in checklist_options(entries, session)}
            if not isinstance(selected, list) or len(selected) > len(valid_ids) or any(not isinstance(v, str) or v not in valid_ids for v in selected) or len(selected) != len(set(selected)):
                raise ValidationError("清单条目不属于此次准备。 / Checklist item is outside this preparation.")
            if session.pending:
                raise Conflict("请等待当前回答完成再保存。 / Wait for the current answer before saving.")
            _claim(session, revision, selected=selected)
        return session


def public_state(user, pack_id, session):
    error = ""
    try:
        pack, _, data, entries = guard(user, pack_id)
    except ValidationError as exc:
        pack, entries = HandoffPack.objects.get(pk=pack_id), []
        error = " / ".join(exc.messages)
    state = {"pack_id": str(pack.pk), "title": pack.title, "entries": entries, "options": checklist_options(entries, session), "error": error, "session": None}
    if session:
        state["session"] = {"id": str(session.pk), "context": session.context, "turns": session.turns, "selected": session.selected, "revision": session.revision, "pending": bool(session.pending)}
    return state


def export_context(user, pack_id, session_id):
    session = owned(user, pack_id, session_id)
    pack, _, data, entries = guard(user, pack_id)
    if not session.selected:
        raise ValidationError("请先选择并保存准备事项。 / Select and save preparation items first.")
    gaps = list(dict.fromkeys([e["conditions"] or e["title"] for e in entries if e["section"] == "question"] + [g for t in session.turns if t.get("response") for g in t["response"]["unknowns"]]))
    return {"context": session.context, "version": str(pack.pk), "entries": [e for e in checklist_options(entries, session) if e["id"] in session.selected], "gaps": gaps, "english": session.context["language"] == "en"}


def export(user, pack_id, session_id):
    data = export_context(user, pack_id, session_id)
    context, entries, gaps = data["context"], data["entries"], data["gaps"]
    clean = _markdown_text
    en = data["english"]
    lines = ["# " + ("Event preparation checklist" if en else "本次活动准备清单"), "", clean(context["goal"]), "",
             ("Expected attendees (plan, not actual): " if en else "预计人数（计划，非实际到场）：") + str(context["expected_attendees"]),
             ("Constraints: " if en else "限制条件：") + clean(context["constraints"]), "",
             "Handover version: " + data["version"], "",
             "Selected for preparation, not completed. / 纳入准备，不代表任务完成。", ""]
    for entry in entries:
        lines.extend(["## " + clean(entry["title"]), "", clean(entry["suggestion"]), "", "Conditions / 适用条件：" + clean(entry["conditions"])])
        for ref in entry["evidence"]:
            lines.extend(["", "Source / 来源：" + clean(ref["title"]), "> " + clean(ref["quote"])])
    lines.extend(["", "## Open questions / 未解决事项", ""])
    lines.extend("- " + clean(g) for g in gaps)
    lines.extend(["", "Snapshot of this preparation; verify applicability before use. / 本次准备快照，执行前请核对适用条件。"])
    return "\n".join(lines)
