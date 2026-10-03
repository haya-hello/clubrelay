from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import OperationalError
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from .forms import ActivityForm, TaskForm, ActionForm
from .models import Task
from .services import visible_activities, visible_tasks, require_lead, create_activity, publish_task, change_task

@login_required
@never_cache
def listing(request):
    activities = visible_activities(request.user).annotate(task_count=Count("tasks")).order_by("-created_at", "id")
    return render(request, "activities/list.html", {"nav":"activities", "page":Paginator(activities,12).get_page(request.GET.get("page"))})

@login_required
@never_cache
def create(request):
    require_lead(request.user)
    form = ActivityForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            activity = create_activity(request.user, form.cleaned_data)
        except (ValidationError, OperationalError) as exc:
            form.add_error(None, "保存冲突，请保留内容后重试。" if isinstance(exc, OperationalError) else exc)
        else:
            messages.success(request, "活动已创建，可以发布第一个任务。")
            return redirect("activity_detail", pk=activity.pk)
    return render(request, "activities/form.html", {"nav":"activities","form":form,"heading":"从一个想法，开始行动。","subtitle":"先写清目标、限制与参与成员。本步不自动生成方案。","button":"创建活动或项目"})

@login_required
@never_cache
def detail(request, pk):
    activity = get_object_or_404(visible_activities(request.user), pk=pk)
    tasks = activity.tasks.select_related("assignee").all()
    from knowledge.services import is_reviewer
    retros = activity.retrospectives.all() if is_reviewer(request.user) else activity.retrospectives.filter(status="confirmed")
    return render(request, "activities/detail.html", {"nav":"activities","activity":activity,"tasks":tasks,"retros":retros,"participants":activity.participants.all()})

@login_required
@never_cache
def task_create(request, pk):
    require_lead(request.user)
    activity = get_object_or_404(visible_activities(request.user), pk=pk)
    form = TaskForm(request.POST or None, activity=activity)
    if request.method == "POST" and form.is_valid():
        try:
            task = publish_task(request.user, activity, form.cleaned_data)
        except (ValidationError, OperationalError) as exc:
            form.add_error(None, "保存冲突，请保留内容后重试。" if isinstance(exc, OperationalError) else exc)
        else:
            messages.success(request, "任务已发布。指派不等于接受，请等待成员本人确认。")
            return redirect("task_detail", pk=task.pk)
    return render(request, "activities/form.html", {"nav":"activities","activity":activity,"form":form,"heading":"让任务有清楚的交付标准。","subtitle":"不指定承担人则开放认领；指定后由本人确认。","button":"发布任务"})

@login_required
@never_cache
def task_detail(request, pk):
    task = get_object_or_404(visible_tasks(request.user).select_related("activity","assignee","reference"), pk=pk)
    from knowledge.services import is_reviewer, searchable_entries
    latest = task.submissions.first() if is_reviewer(request.user) or task.assignee_id == request.user.id else None
    return render(request, "activities/task.html", {"nav":"activities","task":task,"latest_submission":latest,"can_submit":task.assignee_id == request.user.id and task.status in ["active","changes","review"] and not task.blocked_reason,"participant":task.activity.participants.filter(pk=request.user.pk).exists(),"events":task.events.select_related("actor","assignee"),"reference":task.reference if task.reference_id and searchable_entries(request.user).filter(pk=task.reference_id).exists() else None})

@login_required
@require_POST
@never_cache
def task_action(request, pk):
    task = get_object_or_404(visible_tasks(request.user), pk=pk)
    form = ActionForm(request.POST)
    if form.is_valid():
        try:
            change_task(request.user, pk, **form.cleaned_data)
        except (ValidationError, OperationalError) as exc:
            form.add_error(None, "操作冲突，请刷新后重试。" if isinstance(exc, OperationalError) else exc)
        else:
            messages.success(request, "任务状态已更新，操作已留痕。")
            return redirect("task_detail", pk=pk)
    return render(request, "activities/error.html", {"nav":"activities","task":task,"form":form}, status=409)
