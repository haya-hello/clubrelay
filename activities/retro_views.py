from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError
from django.shortcuts import get_object_or_404, render, redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from knowledge.services import is_reviewer
from .models import Retrospective, KnowledgeDerivation
from .services import visible_activities, require_lead
from .retros import RETRO_FIELDS, activity_snapshot, fingerprint, source_current, create_retro, save_retro, confirm_retro, withdraw_retro, export_knowledge, change_stage
from .retro_forms import RetroForm, RetroActionForm, KnowledgeExportForm, StageForm

def allowed_retros(user):
    rows = Retrospective.objects.filter(activity__in=visible_activities(user)).select_related("activity")
    return rows if is_reviewer(user) else rows.filter(status="confirmed")

@login_required
@require_POST
@never_cache
def create(request, pk):
    require_lead(request.user)
    activity = get_object_or_404(visible_activities(request.user),pk=pk)
    try:
        retro = create_retro(request.user,activity.pk)
    except (IntegrityError,OperationalError):
        messages.error(request,"另一个负责人可能正在创建草稿，请刷新活动页再试。")
        return redirect("activity_detail",pk=pk)
    return redirect("retro_detail",pk=retro.pk)

@login_required
@never_cache
def detail(request, pk):
    retro = get_object_or_404(allowed_retros(request.user),pk=pk)
    fresh = retro.fingerprint == fingerprint(activity_snapshot(retro.activity))
    initial = {key:getattr(retro,key) for key in RETRO_FIELDS}
    initial["revision"] = retro.revision
    form = RetroForm(initial=initial)
    link = KnowledgeDerivation.objects.filter(retrospective=retro).first() if is_reviewer(request.user) else None
    return render(request,"retros/detail.html",{"nav":"activities","retro":retro,"fresh":fresh,"show_content":is_reviewer(request.user) or source_current(retro),"form":form,"link":link,"events":retro.events.select_related("actor") if is_reviewer(request.user) else []})

@login_required
@require_POST
@never_cache
def save(request,pk):
    require_lead(request.user)
    retro = get_object_or_404(allowed_retros(request.user),pk=pk)
    form = RetroForm(request.POST)
    if form.is_valid():
        try:
            data = dict(form.cleaned_data)
            save_retro(request.user,pk,data.pop("revision"),data,refresh=request.POST.get("save_action")=="refresh")
        except (ValidationError,OperationalError) as exc:
            form.add_error(None,exc if isinstance(exc,ValidationError) else "保存冲突，请保留输入并刷新。")
        else:
            messages.success(request,"草稿已保存，请核对事实快照后再确认。")
            return redirect("retro_detail",pk=pk)
    return render(request,"retros/edit_error.html",{"nav":"activities","retro":retro,"form":form},status=409)

@login_required
@require_POST
@never_cache
def action(request,pk):
    require_lead(request.user)
    retro = get_object_or_404(allowed_retros(request.user),pk=pk)
    form = RetroActionForm(request.POST)
    if form.is_valid():
        data = form.cleaned_data
        try:
            if data["action"]=="confirm":
                confirm_retro(request.user,pk,data["revision"],data["acknowledged"])
            else:
                withdraw_retro(request.user,pk,data["revision"],data["reason"])
        except (ValidationError,OperationalError) as exc:
            form.add_error(None,exc if isinstance(exc,ValidationError) else "状态发生变化，请刷新后重试。")
        else:
            messages.success(request,"复盘状态已更新。确认不等于知识审核通过。")
            return redirect("retro_detail",pk=pk)
    return render(request,"retros/error.html",{"nav":"activities","retro":retro,"form":form},status=409)

@login_required
@never_cache
def export(request,pk):
    require_lead(request.user)
    retro = get_object_or_404(allowed_retros(request.user),pk=pk)
    form = KnowledgeExportForm(request.POST or None,initial={"revision":retro.revision,"title":(retro.activity.title+"：复盘经验")[:120],"body":retro.improvements,"applicability":retro.applicability})
    if request.method=="POST" and form.is_valid():
        data = dict(form.cleaned_data)
        try:
            entry = export_knowledge(request.user,pk,data.pop("revision"),data)
        except (ValidationError,OperationalError,IntegrityError) as exc:
            form.add_error(None,exc if isinstance(exc,ValidationError) else "提交冲突，请保留内容后刷新。")
        else:
            messages.success(request,"经验已送知识审核，由另一位负责人审核通过后才能检索。")
            return redirect("detail",pk=entry.pk)
    return render(request,"retros/export.html",{"nav":"activities","retro":retro,"form":form})

@login_required
@require_POST
@never_cache
def stage(request,pk):
    require_lead(request.user)
    activity = get_object_or_404(visible_activities(request.user),pk=pk)
    form = StageForm(request.POST)
    if form.is_valid():
        try:
            change_stage(request.user,pk,**form.cleaned_data)
        except (ValidationError,OperationalError) as exc:
            form.add_error(None,exc if isinstance(exc,ValidationError) else "阶段更新冲突，请刷新。")
        else:
            messages.success(request,"阶段已更新，不会自动完成或取消任何任务。")
            return redirect("activity_detail",pk=pk)
    return render(request,"retros/stage_error.html",{"activity":activity,"form":form},status=409)

