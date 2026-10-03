"""交接包本机工作区。 / Local handover workspace."""
import uuid
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction, OperationalError
from django.db.models import Q
from django.http import HttpResponse, Http404
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST
from activities.models import Activity
from .analysis import configuration_hash, ready, allowed_materials, grant_permission, CONFIG_LOCK
from .models import HandoffPack, HandoffItem, ModelConfiguration, AnalysisPermission, Material, AnalysisRun
from .security import manager_required
from . import handoffs


class StartForm(forms.Form):
    title = forms.CharField(label="交接包标题 / Title", max_length=160)
    scope = forms.CharField(label="适用范围 / Scope", max_length=500, required=False, widget=forms.Textarea(attrs={"rows": 2}))
    materials = forms.ModelMultipleChoiceField(label="本次使用的资料 / Materials", queryset=Material.objects.none(), widget=forms.CheckboxSelectMultiple)
    token = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["materials"].queryset = event.materials.filter(excluded=False, parse_status="parsed").exclude(text="").filter(Q(parent__isnull=True) | Q(parent__excluded=False))
        self.fields["materials"].label_from_instance = lambda mat: f"{mat.original_name} · {len(mat.text)} 字"


class ItemForm(forms.ModelForm):
    acknowledge = forms.BooleanField(label="我已对照原文核对当前表述及适用条件 / Reviewed", required=False)
    class Meta:
        model = HandoffItem
        fields = ["section", "title", "record", "suggestion", "conditions"]
        labels = {"section": "分类 / Section", "title": "标题 / Title", "record": "材料记录 / Records", "suggestion": "下次建议 / Suggested next step", "conditions": "适用条件与未知项 / Conditions and unknowns"}
        widgets = {key: forms.Textarea(attrs={"rows": 3}) for key in ("record", "suggestion", "conditions")}


def config_context(event):
    config = ModelConfiguration.objects.filter(pk=1).first()
    ok, reason = ready(config) if config else (False, "模型尚未配置。")
    allowed = {str(value) for value in allowed_materials(config, event).values_list("pk", flat=True)} if config else set()
    return config, ok, reason, allowed


@manager_required
def start(request, event_id):
    event = get_object_or_404(Activity, pk=event_id)
    parent = get_object_or_404(HandoffPack, pk=request.GET["parent"], event=event) if request.GET.get("parent") else None
    form = StartForm(request.POST or None, event=event, initial={"title": f"{event.title} · 经验交接"[:160], "token": uuid.uuid4(), "scope": "供下一次类似活动筹备与负责人交接使用。"})
    status = 200
    if request.method == "POST" and form.is_valid():
        try:
            pack = handoffs.create_pack(request.user, event, form.cleaned_data["title"], form.cleaned_data["scope"],
                [str(mat.pk) for mat in form.cleaned_data["materials"]], form.cleaned_data["token"], parent=parent)
        except (ValidationError, OperationalError) as exc:
            form.add_error(None, exc if isinstance(exc, ValidationError) else "正在保存另一项操作，请稍后重试；已有内容未覆盖。")
            status = 409 if isinstance(exc, (handoffs.Conflict, OperationalError)) else 400
        else:
            return redirect("handoff_detail", pk=pack.pk)
    config, ok, reason, allowed = config_context(event)
    rows = [{"material": mat, "allowed": str(mat.pk) in allowed} for mat in event.materials.all()]
    return render(request, "ops/handoffs/start.html", {"nav": "events", "event": event, "form": form, "config": config,
        "model_ready": ok, "reason": reason, "materials": rows, "parent": parent}, status=status)


@manager_required
def permission(request, event_id):
    event = get_object_or_404(Activity, pk=event_id)
    config, ok, reason, allowed = config_context(event)
    current = AnalysisPermission.objects.filter(event=event).first()
    error, status = "", 200
    selected = set(request.POST.getlist("materials")) if request.method == "POST" else allowed
    auto_future = (request.POST.get("auto_future") == "on") if request.method == "POST" else bool(current and current.auto_future)
    if request.method == "POST":
        try:
            if request.POST.get("consent") != "on":
                raise ValidationError("请确认当前处理位置和完整资料许可范围。")
            grant_permission(request.user, event, list(selected), auto_future=auto_future, expected_config_fingerprint=request.POST.get("config_fingerprint", ""))
        except ValidationError as exc:
            error, status = "；".join(exc.messages), 400
        else:
            messages.success(request, "资料许可已保存，尚未调用模型。请选取资料生成交接草稿。")
            return redirect("handoff_start", event_id=event.pk)
    mats = event.materials.filter(excluded=False, parse_status="parsed").exclude(text="").filter(Q(parent__isnull=True) | Q(parent__excluded=False))
    return render(request, "ops/handoffs/permission.html", {"nav": "events", "event": event, "config": config, "model_ready": ok, "reason": reason,
        "fingerprint": configuration_hash(config) if config else "", "materials": [{"material": mat, "selected": str(mat.pk) in selected} for mat in mats],
        "auto_future": auto_future, "error": error}, status=status)


def detail_context(pack, request, active_item=None, bound_form=None):
    items = list(pack.items.all())
    if active_item is None:
        active_item = next((i for i in items if str(i.pk) == request.GET.get("item")), None) or next((i for i in items if i.decision == "pending"), None) or (items[0] if items else None)
    current = handoffs.sources_current(pack)
    source_map = {s["id"]: s for s in pack.source_manifest}
    refs = []
    if current and active_item:
        for ref in active_item.citations:
            source = source_map.get(ref["source_id"])
            if source and ref["quote"] in source["text"]:
                offset = source["text"].find(ref["quote"])
                refs.append({"id": source["id"], "title": source["title"], "quote": ref["quote"], "context": source["text"][max(0, offset-160):offset+len(ref["quote"])+160]})
    pending = sum(i.decision == "pending" for i in items)
    kept = sum(i.decision == "keep" for i in items)
    return {"nav": "events", "pack": pack, "run": pack.source_analysis, "items": items, "active_item": active_item,
        "form": bound_form or (ItemForm(instance=active_item) if active_item else None), "references": refs, "current": current,
        "pending": pending, "kept": kept, "dropped": sum(i.decision == "drop" for i in items),
        "questions": sum(i.section == "question" and i.decision == "keep" for i in items),
        "can_confirm": bool(pack.initialized_at and current and not pending and kept and pack.status == "draft"),
        "copy_token": uuid.uuid4(), "versions": pack.event.handoff_packs.all()[:10],
        "new_materials": pack.event.materials.filter(excluded=False, parse_status="parsed").exclude(pk__in=list(source_map)).exists()}


def error_page(request, pack, exc, status=400, item=None, form=None):
    pack.refresh_from_db()
    context = detail_context(pack, request, item, form)
    context["error"] = "；".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
    return render(request, "ops/handoffs/detail.html", context, status=409 if isinstance(exc, handoffs.Conflict) else status)


@manager_required
def detail(request, pk):
    pack = get_object_or_404(HandoffPack.objects.select_related("event", "source_analysis", "parent"), pk=pk)
    return render(request, "ops/handoffs/detail.html", detail_context(pack, request))


@manager_required
@require_POST
def prepare(request, pk):
    pack = get_object_or_404(HandoffPack, pk=pk)
    try:
        handoffs.initialize_pack(request.user, pk, request.POST.get("version"))
    except ValidationError as exc:
        return error_page(request, pack, exc)
    return redirect("handoff_detail", pk=pk)


@manager_required
@require_POST
def item_save(request, pk, item_id):
    pack = get_object_or_404(HandoffPack, pk=pk)
    item = get_object_or_404(HandoffItem, pk=item_id, pack=pack)
    form = ItemForm(request.POST, instance=item)
    if not form.is_valid():
        return error_page(request, pack, "请检查输入，文字尚未丢失。", item=item, form=form)
    try:
        handoffs.edit_item(request.user, pk, item_id, request.POST.get("version"), {k: form.cleaned_data[k] for k in handoffs.EDIT_FIELDS}, request.POST.get("action"), form.cleaned_data["acknowledge"])
    except ValidationError as exc:
        return error_page(request, pack, exc, item=item, form=form)
    messages.success(request, "审阅操作已保存；修改过的条目需再次核对并采用。")
    return redirect("handoff_detail", pk=pk)


@manager_required
@require_POST
def confirm(request, pk):
    pack = get_object_or_404(HandoffPack, pk=pk)
    try:
        if request.POST.get("confirm_ack") != "on":
            raise ValidationError("请确认已检查交付内容与待确认事项。")
        handoffs.confirm_pack(request.user, pk, request.POST.get("version"), request.POST.get("reviewer_label", ""))
    except ValidationError as exc:
        return error_page(request, pack, exc)
    return redirect("handoff_detail", pk=pk)


@manager_required
@require_POST
def duplicate(request, pk):
    pack = get_object_or_404(HandoffPack, pk=pk)
    try:
        new = handoffs.copy_pack(request.user, pk, request.POST.get("token"))
    except (ValidationError, ValueError, TypeError) as exc:
        return error_page(request, pack, exc if isinstance(exc, ValidationError) else "复制标识无效，请刷新。")
    return redirect("handoff_detail", pk=new.pk)


@manager_required
@require_POST
def cancel(request, pk):
    pack = get_object_or_404(HandoffPack, pk=pk)
    with CONFIG_LOCK:
        AnalysisRun.objects.filter(pk=pack.source_analysis_id, status__in=["pending", "running"]).update(status="cancelled", error="已取消采用；已发送的请求无法撤回。")
    return redirect("handoff_detail", pk=pk)


@manager_required
def export(request, pk, format):
    if format not in ("preview", "markdown"):
        raise Http404
    if format == "markdown" and request.method != "POST":
        return HttpResponse("下载请从导出预览页发起。", status=405)
    pack = get_object_or_404(HandoffPack, pk=pk)
    try:
        with CONFIG_LOCK, transaction.atomic():
            data = handoffs.export_data(pack)
            if format == "markdown" and (request.POST.get("version") != str(pack.lock_version) or request.POST.get("export_ack") != "on"):
                raise ValidationError("请在预览页确认当前版本及分享范围。")
    except ValidationError as exc:
        return error_page(request, pack, exc, status=409)
    if format == "markdown":
        response = HttpResponse(handoffs.export_markdown(data), content_type="text/markdown; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="clubrelay-{pack.pk}.md"'
        response["Content-Security-Policy"] = "sandbox; default-src 'none'"
        return response
    return render(request, "ops/handoffs/export.html", {"data": data, "pack": pack})
