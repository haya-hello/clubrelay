from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError
from django.db.models import Q
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from .forms import EntryForm, ReviewForm, SearchForm
from .models import Entry
from .services import is_reviewer, review_entry, resubmit_entry, submit_entry, visible_entries, searchable_entries

@login_required
@never_cache
def home(request):
    from activities.services import visible_tasks
    tasks = visible_tasks(request.user).filter(status__in=["waiting", "active", "review", "changes"])
    tasks = tasks.exclude(blocked_reason="") if is_reviewer(request.user) else tasks.filter(assignee=request.user)
    return render(request, "home.html", {"nav": "home", "result_pending_count":visible_tasks(request.user).filter(status="review").count() if is_reviewer(request.user) else 0,"next_tasks":tasks.select_related("activity","assignee")[:5], "approved_count": searchable_entries(request.user).count(), "own_count": Entry.objects.filter(owner=request.user).count(), "pending_count": Entry.objects.filter(status="pending").count() if is_reviewer(request.user) else None, "recent": visible_entries(request.user).select_related("owner")[:4]})

@login_required
@never_cache
def library(request):
    form = SearchForm(request.GET)
    entries = searchable_entries(request.user).select_related("owner")
    if form.is_valid():
        q = form.cleaned_data["q"]
        for term in q.split():
            entries = entries.filter(Q(title__icontains=term) | Q(body__icontains=term) | Q(applicability__icontains=term))
        if form.cleaned_data["category"]:
            entries = entries.filter(category=form.cleaned_data["category"])
    else:
        entries = entries.none()
    return render(request, "library.html", {"nav": "library", "form": form, "page": Paginator(entries, 12).get_page(request.GET.get("page")), "querystring": request.GET.urlencode()})

@login_required
@never_cache
def mine(request):
    return render(request, "listing.html", {"nav": "mine", "heading": "我的提交", "subtitle": "每一条经验都有状态，修改后会重新审核。", "page": Paginator(Entry.objects.filter(owner=request.user), 15).get_page(request.GET.get("page"))})

@login_required
@never_cache
def queue(request):
    if not is_reviewer(request.user):
        raise PermissionDenied
    return render(request, "listing.html", {"nav": "queue", "heading": "知识审核", "subtitle": "确认内容与适用边界。自己的经验由另一位负责人审核。", "page": Paginator(Entry.objects.filter(status="pending").select_related("owner"), 15).get_page(request.GET.get("page"))})

@login_required
@never_cache
def submit(request):
    form = EntryForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            entry = submit_entry(request.user, form.cleaned_data)
        except (ValidationError, OperationalError) as exc:
            form.add_error(None, "保存冲突，请保留内容后重试。" if isinstance(exc, OperationalError) else exc)
        else:
            messages.success(request, "经验已提交，审核通过后才会出现在经验库。")
            return redirect("detail", pk=entry.pk)
    return render(request, "entry_form.html", {"nav": "mine", "form": form, "heading": "留下一份有用的经验", "editing": False})

@login_required
@never_cache
def edit(request, pk):
    entry = get_object_or_404(visible_entries(request.user), pk=pk)
    if entry.owner_id != request.user.id:
        raise PermissionDenied
    form = EntryForm(request.POST or None, instance=entry, initial={"version": entry.version, "token": entry.submit_token})
    if request.method == "POST" and form.is_valid():
        try:
            entry = resubmit_entry(request.user, pk, form.cleaned_data)
        except (ValidationError, OperationalError) as exc:
            form.add_error(None, "保存冲突，请保留内容后重试。" if isinstance(exc, OperationalError) else exc)
        else:
            messages.success(request, "新版本已提交，旧版本不再用于检索。")
            return redirect("detail", pk=entry.pk)
    return render(request, "entry_form.html", {"nav": "mine", "form": form, "heading": "修改并重新提交", "editing": True})

@login_required
@never_cache
def detail(request, pk):
    entry = get_object_or_404(visible_entries(request.user).select_related("owner"), pk=pk)
    can_audit = is_reviewer(request.user) or entry.owner_id == request.user.id
    from activities.retros import entry_source_current
    source_valid = entry_source_current(entry)
    from activities.models import KnowledgeDerivation
    from activities.services import visible_activities
    origin = KnowledgeDerivation.objects.filter(entry=entry).select_related("retrospective").first()
    origin_retro = origin.retrospective if origin and visible_activities(request.user).filter(pk=origin.retrospective.activity_id).exists() else None
    return render(request, "detail.html", {"nav": "library", "origin_retro":origin_retro, "source_valid":source_valid, "entry": entry, "can_audit": can_audit, "can_review": is_reviewer(request.user) and entry.owner_id != request.user.id, "events": entry.events.select_related("actor").all() if can_audit else [], "revisions": entry.revisions.exclude(number=entry.version) if can_audit else [], "review_form": ReviewForm(initial={"version": entry.version})})

@login_required
@require_POST
@never_cache
def review(request, pk):
    if not is_reviewer(request.user):
        raise PermissionDenied
    entry = get_object_or_404(Entry, pk=pk)
    form = ReviewForm(request.POST)
    if form.is_valid():
        try:
            review_entry(request.user, pk, **form.cleaned_data)
        except (ValidationError, OperationalError) as exc:
            form.add_error(None, "审核冲突，请刷新后重试。" if isinstance(exc, OperationalError) else exc)
        else:
            messages.success(request, "操作已保存。经验库已同步更新。")
            return redirect("detail", pk=pk)
    return render(request, "review_error.html", {"entry": entry, "form": form, "nav": "queue"}, status=409)
