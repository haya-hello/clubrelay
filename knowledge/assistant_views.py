"""知识助手的本地闭环。 / Local knowledge assistant workflow."""
import time
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import OperationalError
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from .answers import HISTORY_KEY, collect_references, display_turn, load_history, resolve_reference, save_turn
from .forms import QuestionForm
from .models import Entry
from .services import searchable_entries

@login_required
@never_cache
def assistant(request):
    history = load_history(request.session, request.user)
    initial = {}
    scope = request.GET.get("scope")
    if scope:
        try:
            selected = searchable_entries(request.user).filter(pk=scope).first()
        except (ValidationError, ValueError):
            selected = None
        if selected:
            initial["scope"] = selected
        else:
            messages.warning(request, "这条经验暂不可用于提问，请选择当前已审核资料。")
    form = QuestionForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        previous = history[-1]["question"] if data["use_previous"] and history else ""
        selected_scope = str(data["scope"].pk) if data["scope"] else None
        try:
            references = collect_references(request.user, data["question"], scope=selected_scope, previous_question=previous)
        except (ValidationError, OperationalError) as exc:
            form.add_error(None, "检索暂时不可用，请保留问题后重试。" if isinstance(exc, OperationalError) else exc)
        else:
            save_turn(request.session, request.user, {"token": str(data["token"]), "question": data["question"], "scope_id": selected_scope, "previous_question": previous, "references": references, "created_at": time.time()})
            return redirect("assistant")
    turns = [display_turn(request.user, turn) for turn in history]
    return render(request, "assistant.html", {"nav": "assistant", "form": form, "turns": turns, "latest": turns[-1] if turns else None, "approved_count": searchable_entries(request.user).count()})

@login_required
@require_POST
@never_cache
def clear_history(request):
    request.session.pop(HISTORY_KEY, None)
    messages.success(request, "本次问答记录已清空，知识资料没有改变。")
    return redirect("assistant")

@login_required
@never_cache
def source(request, pk, version, chunk):
    evidence = resolve_reference(request.user, {"entry_id": str(pk), "version": version, "chunk": chunk})
    if evidence is None:
        return render(request, "source_unavailable.html", {"nav": "assistant"}, status=410)
    return render(request, "source.html", {"nav": "assistant", "evidence": evidence})
