from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import OperationalError
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, render, redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from knowledge.services import is_reviewer
from .models import Task
from .services import visible_tasks, require_lead
from .deliveries import visible_submissions, current_achievements, submit_result, review_result
from .delivery_forms import SubmissionForm, AcceptanceForm

@login_required
@never_cache
def submit(request, pk):
    task = get_object_or_404(visible_tasks(request.user),pk=pk)
    if task.assignee_id != request.user.id:
        raise PermissionDenied
    latest = task.submissions.first()
    initial = {"version":task.version}
    if latest:
        initial.update({key:getattr(latest,key) for key in ["summary","result_text","result_url","method","contribution"]})
    form = SubmissionForm(request.POST or None,request.FILES or None,initial=initial)
    if getattr(request, "upload_failure", None):
        form.add_error("attachment", request.upload_failure)
    if request.method=="POST" and form.is_valid():
        try:
            result = submit_result(request.user,task.pk,form.cleaned_data)
        except (ValidationError,OperationalError,OSError) as exc:
            form.add_error(None,exc if isinstance(exc,ValidationError) else "保存暂时失败，文字已保留，请重新选择附件再试。")
        else:
            messages.success(request,"成果已提交，待人工验收；尚未计入确认记录。")
            return redirect("submission_detail",pk=result.pk)
    return render(request,"deliveries/form.html",{"nav":"activities","form":form,"task":task})

@login_required
@never_cache
def detail(request, pk):
    result = get_object_or_404(visible_submissions(request.user).select_related("task","author"),pk=pk)
    task=result.task
    latest=task.submissions.first()
    actionable=latest.pk==result.pk and ((task.status=="review" and result.status=="pending") or (task.status=="done" and result.status=="approved"))
    return render(request,"deliveries/detail.html",{"nav":"growth","result":result,"task":task,"can_review":is_reviewer(request.user) and result.author_id!=request.user.id and actionable,"events":result.reviews.select_related("actor"),"history":visible_submissions(request.user).filter(task=task),"review_form":AcceptanceForm(initial={"version":task.version})})

@login_required
@require_POST
@never_cache
def review(request,pk):
    require_lead(request.user)
    result=get_object_or_404(visible_submissions(request.user),pk=pk)
    form=AcceptanceForm(request.POST)
    if form.is_valid():
        try:
            review_result(request.user,result.pk,**form.cleaned_data)
        except (ValidationError,OperationalError) as exc:
            form.add_error(None,exc if isinstance(exc,ValidationError) else "验收冲突，请刷新后重试。")
        else:
            messages.success(request,"验收操作已保存，任务与成长记录已同步。")
            return redirect("submission_detail",pk=pk)
    return render(request,"deliveries/error.html",{"nav":"growth","result":result,"form":form},status=409)

@login_required
@never_cache
def download(request,pk):
    result=get_object_or_404(visible_submissions(request.user),pk=pk)
    if not result.attachment:
        raise Http404
    try:
        stream=result.attachment.open("rb")
    except (FileNotFoundError,OSError):
        raise Http404
    response=FileResponse(stream,as_attachment=True,filename=result.original_name,content_type="application/octet-stream")
    response["X-Content-Type-Options"]="nosniff"
    response["Content-Security-Policy"]="sandbox; default-src 'none'"
    return response

@login_required
@never_cache
def queue(request):
    require_lead(request.user)
    rows=visible_submissions(request.user).filter(status="pending",task__status="review").select_related("task","author").order_by("created_at","id")
    return render(request,"deliveries/queue.html",{"nav":"acceptance","page":Paginator(rows,15).get_page(request.GET.get("page"))})

@login_required
@never_cache
def growth(request):
    credits=current_achievements(request.user)
    tasks=visible_tasks(request.user).filter(submissions__author=request.user).distinct().order_by("-updated_at","id")
    page=Paginator(tasks,12).get_page(request.GET.get("page"))
    rows=[{"task":task,"submission":task.submissions.filter(author=request.user).first()} for task in page]
    return render(request,"deliveries/growth.html",{"nav":"growth","page":page,"rows":rows,"practice_count":credits.filter(practice=True).count(),"contribution_count":credits.filter(contribution=True).count(),"unique_count":credits.count()})
