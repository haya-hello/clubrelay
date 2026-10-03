"""本地证据检索，不调用模型或网络。 / Local evidence retrieval without model or network calls."""
import re
import time
import unicodedata
from django.core.exceptions import ValidationError
from .models import Entry
from .services import require_active, searchable_entries

HISTORY_KEY = "knowledge_assistant_v1"
MAX_TURNS = 5
TURN_TTL = 1800
CHUNK_SIZE = 600

def chunks(body):
    """按非空行切片，返回稳定的原文片段。 / Slice nonempty lines into stable original-text chunks."""
    result = []
    for line in body.splitlines():
        line = line.strip()
        if line:
            result.extend(line[i:i + CHUNK_SIZE] for i in range(0, len(line), CHUNK_SIZE))
    return result

def query_terms(question):
    text = unicodedata.normalize("NFKC", question).lower()
    # 去除问句套语，但不添加同义词或推断新事实。 / Remove question fillers without inventing synonyms or facts.
    text = re.sub(r"请问|帮我|我们|如何|怎么|什么|哪些|是否|需要|应该|有没有|一下|还有呢", " ", text)
    terms = set(re.findall(r"[a-z][a-z0-9_-]{1,30}", text))
    for phrase in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        phrase = phrase.strip("的了吗呢")
        if len(phrase) >= 2:
            terms.add(phrase)
            terms.update(phrase[i:i + 2] for i in range(len(phrase) - 1))
    return sorted(terms, key=lambda value: (-len(value), value))[:120]

def collect_references(user, question, scope=None, previous_question=""):
    require_active(user)
    if not question.strip() or len(question) > 600:
        raise ValidationError("请输入 1 至 600 字问题。")
    entries = searchable_entries(user)
    if scope:
        entries = entries.filter(pk=scope)
        if not entries.exists():
            raise ValidationError("限定经验已失效，请重新选择。")
    primary = query_terms(question)
    previous = query_terms(previous_question) if previous_question else []
    ranked = []
    for entry in entries.iterator(chunk_size=100):
        best = None
        title = unicodedata.normalize("NFKC", entry.title).lower()
        for index, excerpt in enumerate(chunks(entry.body)):
            text = unicodedata.normalize("NFKC", excerpt).lower()
            current_hits = sum(term in text for term in primary)
            title_hits = sum(term in title for term in primary)
            prior_hits = sum(term in text or term in title for term in previous)
            score = current_hits * 3 + title_hits * 2 + prior_hits
            if score and (best is None or score > best[0]):
                best = (score, index)
        if best:
            ranked.append((best[0], str(entry.pk), entry.version, best[1]))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [{"entry_id": pk, "version": version, "chunk": index} for _, pk, version, index in ranked[:3]]

def resolve_reference(user, reference):
    """每次显示都重新检查当前状态；不保存正文缓存。 / Recheck current state on every render; never cache source text."""
    require_active(user)
    try:
        entry = searchable_entries(user).filter(pk=reference["entry_id"], version=reference["version"]).first()
        index = reference["chunk"]
        if entry is None or not isinstance(index, int) or index < 0:
            return None
        parts = chunks(entry.body)
        if index >= len(parts):
            return None
    except (KeyError, TypeError, ValueError, ValidationError):
        return None
    return {"entry_id": str(entry.pk), "version": entry.version, "chunk": index, "part_number": index + 1, "title": entry.title, "excerpt": parts[index], "source": entry.source, "applicability": entry.applicability}

def load_history(session, user):
    require_active(user)
    history = session.get(HISTORY_KEY, {})
    if history.get("user_id") != user.id:
        if HISTORY_KEY in session:
            session.pop(HISTORY_KEY)
        return []
    turns = [turn for turn in history.get("turns", []) if time.time() - turn.get("created_at", 0) < TURN_TTL][-MAX_TURNS:]
    if turns != history.get("turns", []):
        session[HISTORY_KEY] = {"user_id": user.id, "turns": turns}
    return turns

def save_turn(session, user, turn):
    history = load_history(session, user)
    if not any(old["token"] == turn["token"] for old in history):
        history.append(turn)
    session[HISTORY_KEY] = {"user_id": user.id, "turns": history[-MAX_TURNS:]}

def display_turn(user, turn):
    references = [resolve_reference(user, reference) for reference in turn["references"]]
    evidence = [item for item in references if item is not None]
    return {**turn, "evidence": evidence, "stale_count": len(references) - len(evidence), "had_evidence": bool(turn["references"])}
