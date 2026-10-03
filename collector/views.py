import json
import time
import uuid

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from operations.security import manager_required

from .bridge_security import verify_payload
from .forms import ChatSourceForm, KnowledgeCardForm, SimulationForm
from .models import CaptureMarker, ChatMessage, ChatSource, CollectorAudit, KnowledgeCard, WeeklyBatch
from .services import ingest_event, process_pending, search_cards


@csrf_exempt
@require_POST
def ingest(request):
    """AstrBot 本机桥接入口；只信任带时效 HMAC 的请求。 / Local AstrBot bridge with expiring HMAC."""

    timestamp = request.headers.get("X-Qinglian-Timestamp", "")
    signature = request.headers.get("X-Qinglian-Signature", "")
    if not verify_payload(timestamp, signature, request.body):
        return JsonResponse({"ok": False, "error": "invalid_signature"}, status=403)
    try:
        payload = json.loads(request.body.decode("utf-8"))
        result = ingest_event(payload)
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)
    except PermissionError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=403)
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    return JsonResponse({"ok": True, **result})


@manager_required
def dashboard(request):
    stats = {
        "sources": ChatSource.objects.filter(enabled=True).count(),
        "messages": ChatMessage.objects.count(),
        "pending": CaptureMarker.objects.filter(status=CaptureMarker.Status.PENDING).count(),
        "candidates": KnowledgeCard.objects.filter(status=KnowledgeCard.Status.CANDIDATE).count(),
        "knowledge": KnowledgeCard.objects.filter(status=KnowledgeCard.Status.CONFIRMED).count(),
    }
    return render(
        request,
        "collector/dashboard.html",
        {
            "nav": "collector",
            "stats": stats,
            "recent_markers": CaptureMarker.objects.select_related(
                "target", "target__sender", "target__source"
            )[:8],
            "recent_batches": WeeklyBatch.objects.all()[:5],
        },
    )


@manager_required
@require_http_methods(["GET", "POST"])
def sources(request):
    form = ChatSourceForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        source = form.save()
        CollectorAudit.objects.create(
            actor=request.user,
            action="source.created",
            object_id=str(source.id),
            detail=source.display_name,
        )
        messages.success(request, "授权群已保存。接入机器人前请再次核对群标识和负责人标识。")
        return redirect("collector_sources")
    return render(
        request,
        "collector/sources.html",
        {"nav": "collector", "form": form, "sources": ChatSource.objects.all()},
    )


@manager_required
@require_http_methods(["GET", "POST"])
def simulate(request):
    form = SimulationForm(request.POST or None)
    result = None
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        payload = {
            "platform": data["source"].platform,
            "group_id": data["source"].external_id,
            "group_name": data["source"].display_name,
            "message_id": data["message_id"] or f"sim-{uuid.uuid4().hex}",
            "sender_id": data["sender_id"],
            "sender_name": data["sender_name"],
            "timestamp": timezone.now().isoformat(),
            "text": data["text"],
            "message_type": "text",
            "reply_to_id": data["reply_to_id"],
        }
        try:
            result = ingest_event(payload)
            messages.success(request, "模拟消息已进入与真实 QQ 相同的处理链路。")
        except (PermissionError, ValueError) as exc:
            messages.error(request, f"模拟失败：{exc}")
    return render(
        request,
        "collector/simulate.html",
        {"nav": "collector", "form": form, "result": result},
    )


@manager_required
def inbox(request):
    markers = CaptureMarker.objects.select_related(
        "target", "target__sender", "target__source", "command_message"
    )
    status = request.GET.get("status", "")
    if status in CaptureMarker.Status.values:
        markers = markers.filter(status=status)
    return render(
        request,
        "collector/inbox.html",
        {"nav": "collector", "markers": markers, "status": status},
    )


@manager_required
def marker(request, pk):
    item = get_object_or_404(
        CaptureMarker.objects.select_related("target", "target__sender", "target__source"),
        pk=pk,
    )
    context = ChatMessage.objects.filter(id__in=item.context_message_ids).select_related("sender")
    return render(
        request,
        "collector/marker.html",
        {"nav": "collector", "marker": item, "context_messages": context},
    )


@manager_required
@require_POST
def run_processing(request):
    batch = process_pending()
    if batch is None:
        messages.info(request, "当前没有待整理的收录消息。")
    else:
        messages.success(
            request,
            f"本批生成 {batch.candidate_count} 张本地候选卡。真实 AI 尚未启用，请人工补充并确认。",
        )
    return redirect("collector_knowledge")


@manager_required
def knowledge(request):
    status = request.GET.get("status", "")
    cards = KnowledgeCard.objects.select_related("marker", "marker__target", "marker__target__source")
    if status in KnowledgeCard.Status.values:
        cards = cards.filter(status=status)
    return render(
        request,
        "collector/knowledge.html",
        {"nav": "collector", "cards": cards, "status": status},
    )


@manager_required
@require_http_methods(["GET", "POST"])
def card(request, pk):
    item = get_object_or_404(
        KnowledgeCard.objects.select_related("marker", "marker__target", "batch"), pk=pk
    )
    form = KnowledgeCardForm(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        item = form.save(commit=False)
        action = request.POST.get("action", "save")
        if action == "confirm":
            item.status = KnowledgeCard.Status.CONFIRMED
            item.reviewed_by = request.user
            item.reviewed_at = timezone.now()
        elif action == "reject":
            item.status = KnowledgeCard.Status.REJECTED
            item.reviewed_by = request.user
            item.reviewed_at = timezone.now()
        item.version += 1
        item.save()
        CollectorAudit.objects.create(
            actor=request.user,
            action=f"card.{action}",
            object_id=str(item.id),
            detail=item.title[:200],
        )
        messages.success(request, "知识卡已保存。")
        return redirect("collector_card", pk=item.pk)
    return render(
        request,
        "collector/card.html",
        {"nav": "collector", "card": item, "form": form},
    )


@manager_required
def assistant(request):
    query = request.GET.get("q", "").strip()
    cards = search_cards(query) if query else []
    return render(
        request,
        "collector/assistant.html",
        {"nav": "collector", "query": query, "cards": cards},
    )
