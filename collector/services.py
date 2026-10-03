import hashlib
import json
import re
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import (
    CaptureMarker,
    ChatIdentity,
    ChatMessage,
    ChatSource,
    CollectorAudit,
    KnowledgeCard,
    KnowledgeCitation,
    WeeklyBatch,
)


def _event_time(value):
    if isinstance(value, (int, float)):
        return timezone.datetime.fromtimestamp(value, tz=timezone.get_current_timezone())
    parsed = parse_datetime(str(value or ""))
    if parsed is None:
        raise ValueError("timestamp_invalid")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _identity(source, external_id, display_name, sent_at):
    identity, created = ChatIdentity.objects.get_or_create(
        source=source,
        external_id=external_id,
        defaults={
            "display_name": display_name,
            "first_seen_at": sent_at,
            "last_seen_at": sent_at,
        },
    )
    changed = False
    if display_name and identity.display_name != display_name:
        identity.display_name = display_name
        changed = True
    if sent_at > identity.last_seen_at:
        identity.last_seen_at = sent_at
        changed = True
    if changed:
        identity.save(update_fields=["display_name", "last_seen_at"])
    return identity


def _save_snapshot(source, snapshot):
    if not isinstance(snapshot, dict) or not snapshot.get("message_id"):
        return None
    sent_at = _event_time(snapshot.get("timestamp"))
    sender = _identity(
        source,
        str(snapshot.get("sender_id") or "unknown"),
        str(snapshot.get("sender_name") or ""),
        sent_at,
    )
    message, _ = ChatMessage.objects.get_or_create(
        source=source,
        external_id=str(snapshot["message_id"]),
        defaults={
            "sender": sender,
            "sent_at": sent_at,
            "text": str(snapshot.get("text") or "")[:20000],
            "message_type": str(snapshot.get("message_type") or "text")[:30],
            "reply_to_external_id": str(snapshot.get("reply_to_id") or "")[:180],
        },
    )
    return message


@transaction.atomic
def ingest_event(payload):
    """接收规范化事件并识别负责人收录动作。 / Ingest one normalized event and detect capture commands."""

    required = ["platform", "group_id", "message_id", "sender_id", "timestamp"]
    if any(not payload.get(name) for name in required):
        raise ValueError("required_field_missing")
    source = ChatSource.objects.filter(
        platform=str(payload["platform"]),
        external_id=str(payload["group_id"]),
        enabled=True,
        consent_confirmed=True,
    ).first()
    if source is None:
        raise PermissionError("source_not_authorized")
    sent_at = _event_time(payload["timestamp"])
    sender_id = str(payload["sender_id"])
    sender = _identity(source, sender_id, str(payload.get("sender_name") or ""), sent_at)
    snapshot = _save_snapshot(source, payload.get("reply_snapshot"))
    reply_to_id = str(payload.get("reply_to_id") or "")[:180]
    if not reply_to_id and snapshot:
        reply_to_id = snapshot.external_id
    message, created = ChatMessage.objects.get_or_create(
        source=source,
        external_id=str(payload["message_id"]),
        defaults={
            "sender": sender,
            "sent_at": sent_at,
            "text": str(payload.get("text") or "")[:20000],
            "message_type": str(payload.get("message_type") or "text")[:30],
            "reply_to_external_id": reply_to_id,
        },
    )
    result = {"stored": created, "message_id": str(message.id), "capture": None}
    if not created:
        result["duplicate"] = True
        return result
    command_text = message.text.strip()
    capture_phrase = source.capture_phrase.strip()
    if command_text not in {capture_phrase, f"/{capture_phrase}"}:
        return result
    if sender_id not in {str(item) for item in source.manager_ids}:
        result["capture"] = "manager_required"
        return result
    if not reply_to_id:
        result["capture"] = "reply_required"
        return result
    target = ChatMessage.objects.filter(source=source, external_id=reply_to_id).first()
    if target is None:
        result["capture"] = "target_missing"
        return result
    marker, marker_created = CaptureMarker.objects.get_or_create(
        target=target,
        defaults={
            "command_message": message,
            "marked_by_external_id": sender_id,
        },
    )
    if marker_created:
        build_context(marker)
        CollectorAudit.objects.create(
            action="capture.created", object_id=str(marker.id), detail=source.display_name
        )
    result["capture"] = "created" if marker_created else "duplicate"
    result["marker_id"] = str(marker.id)
    return result


def build_context(marker, window_minutes=15, limit=80):
    target = marker.target
    start = target.sent_at - timedelta(minutes=window_minutes)
    end = target.sent_at + timedelta(minutes=window_minutes)
    query = ChatMessage.objects.filter(source=target.source, sent_at__range=(start, end)).filter(
        Q(id=target.id)
        | Q(external_id=target.reply_to_external_id)
        | Q(reply_to_external_id=target.external_id)
        | Q(sender=target.sender)
    )
    messages = list(query.order_by("sent_at", "created_at")[: limit + 1])
    marker.context_truncated = len(messages) > limit
    marker.context_message_ids = [str(item.id) for item in messages[:limit]]
    marker.save(update_fields=["context_truncated", "context_message_ids", "updated_at"])
    return messages[:limit]


def _local_title(text):
    compact = re.sub(r"\s+", " ", text).strip()
    return (compact[:46] + "…") if len(compact) > 46 else (compact or "待补充的问题")


@transaction.atomic
def process_pending(period_end=None):
    """生成明确标注的本地草稿；真实 AI 未配置时不冒充 AI。 / Build labeled local drafts without pretending AI ran."""

    markers = list(
        CaptureMarker.objects.select_related("target", "target__source", "target__sender")
        .filter(status=CaptureMarker.Status.PENDING)
        .order_by("created_at")
    )
    if not markers:
        return None
    period_end = period_end or timezone.localdate()
    period_start = period_end - timedelta(days=6)
    fingerprint = hashlib.sha256(
        "|".join(str(item.id) for item in markers).encode("utf-8")
    ).hexdigest()
    batch, created = WeeklyBatch.objects.get_or_create(
        fingerprint=fingerprint,
        defaults={
            "period_start": period_start,
            "period_end": period_end,
            "status": WeeklyBatch.Status.RUNNING,
            "marker_count": len(markers),
        },
    )
    if not created and batch.status == WeeklyBatch.Status.COMPLETE:
        return batch
    candidate_count = 0
    for marker in markers:
        messages = list(
            ChatMessage.objects.filter(id__in=marker.context_message_ids)
            .select_related("sender")
            .order_by("sent_at", "created_at")
        )
        if not messages:
            messages = build_context(marker)
        card, card_created = KnowledgeCard.objects.get_or_create(
            marker=marker,
            defaults={
                "batch": batch,
                "title": _local_title(marker.target.text),
                "problem": marker.target.text or "原消息没有可提取文本，请负责人补充。",
                "background": "\n".join(
                    f"{item.sent_at:%m-%d %H:%M} {item.sender.display_name or item.sender.external_id}：{item.text}"
                    for item in messages
                    if item.text and item.id != marker.command_message_id
                )[:6000],
                "contributors": [
                    {
                        "external_id": marker.target.sender.external_id,
                        "display_name": marker.target.sender.display_name,
                    }
                ],
                "extraction_mode": "local_draft",
            },
        )
        if card_created:
            for position, item in enumerate(messages):
                if item.text and item.id != marker.command_message_id:
                    KnowledgeCitation.objects.get_or_create(
                        card=card,
                        message=item,
                        defaults={"quote": item.text[:2000], "position": position},
                    )
            candidate_count += 1
        marker.status = CaptureMarker.Status.PROCESSED
        marker.save(update_fields=["status", "updated_at"])
    batch.status = WeeklyBatch.Status.COMPLETE
    batch.candidate_count = candidate_count
    batch.completed_at = timezone.now()
    batch.save(update_fields=["status", "candidate_count", "completed_at"])
    CollectorAudit.objects.create(
        action="batch.complete",
        object_id=str(batch.id),
        detail=json.dumps({"markers": len(markers), "cards": candidate_count}, ensure_ascii=False),
    )
    return batch


def search_cards(query):
    words = [item for item in re.split(r"\s+", query.strip()) if item]
    qs = KnowledgeCard.objects.filter(status=KnowledgeCard.Status.CONFIRMED)
    if not words:
        return qs.none()
    condition = Q()
    for word in words[:8]:
        condition |= (
            Q(title__icontains=word)
            | Q(problem__icontains=word)
            | Q(background__icontains=word)
            | Q(solution__icontains=word)
            | Q(result__icontains=word)
            | Q(pitfalls__icontains=word)
            | Q(conditions__icontains=word)
        )
    return qs.filter(condition).distinct()[:8]
