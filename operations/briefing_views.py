"""准备页面及同源接口。 / Preparation page and same-origin endpoints."""
import json
import uuid
from functools import wraps
from django.core.exceptions import ValidationError
from django.db import OperationalError
from django.http import JsonResponse, HttpResponse, Http404
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST, require_GET
from .models import HandoffPack, BriefingSession
from .security import manager_required
from .ai_client import AIError
from .handoffs import Conflict
from . import briefings


def api_errors(fn):
    @wraps(fn)
    def wrapped(request, *args, **kwargs):
        try:
            if len(request.body) > 16000:
                raise ValidationError("请求过大。 / Request too large.")
            return fn(request, *args, **kwargs)
        except (BriefingSession.DoesNotExist, HandoffPack.DoesNotExist):
            raise Http404
        except AIError as exc:
            return JsonResponse({"error": exc.message + " / Model request failed: " + exc.code, "code": exc.code}, status=503)
        except ValidationError as exc:
            return JsonResponse({"error": " / ".join(exc.messages)}, status=409 if isinstance(exc, Conflict) else 400)
        except (ValueError, TypeError, KeyError, UnicodeError):
            return JsonResponse({"error": "请求格式无效。 / Invalid request."}, status=400)
        except OperationalError:
            return JsonResponse({"error": "正在保存，请稍后刷新。 / Storage busy; reload shortly."}, status=409)
    return wrapped


def payload(request):
    value = json.loads(request.body)
    if not isinstance(value, dict):
        raise ValueError
    return value


@manager_required
@require_GET
def page(request, pk):
    pack = get_object_or_404(HandoffPack, pk=pk)
    session = BriefingSession.objects.filter(owner=request.user, pack=pack).first()
    state = briefings.public_state(request.user, pk, session)
    en = request.GET.get("lang") == "en" or ("lang" not in request.GET and session and session.context["language"] == "en")
    return render(request, "ops/briefing.html", {"pack": pack, "state": state, "english": bool(en)})


@manager_required
@require_POST
@api_errors
def start(request, pk):
    data = payload(request)
    session = briefings.create(request.user, pk, data["context"], uuid.UUID(data["request_id"]))
    return JsonResponse(briefings.public_state(request.user, pk, session))


@manager_required
@require_GET
@api_errors
def state(request, pk, sid):
    session = briefings.owned(request.user, pk, sid)
    return JsonResponse(briefings.public_state(request.user, pk, session))


@manager_required
@require_POST
@api_errors
def turn(request, pk, sid):
    data = payload(request)
    session = briefings.ask(request.user, pk, sid, data["question"], uuid.UUID(data["request_id"]), data["revision"])
    return JsonResponse(briefings.public_state(request.user, pk, session))


@manager_required
@require_POST
@api_errors
def selection(request, pk, sid):
    data = payload(request)
    session = briefings.update(request.user, pk, sid, data["revision"], selected=data["selected"])
    return JsonResponse(briefings.public_state(request.user, pk, session))


@manager_required
@require_POST
@api_errors
def reset(request, pk, sid):
    data = payload(request)
    session = briefings.update(request.user, pk, sid, data["revision"], reset=True)
    return JsonResponse(briefings.public_state(request.user, pk, session))


@manager_required
@require_GET
@api_errors
def export(request, pk, sid, format):
    text = briefings.export(request.user, pk, sid)
    if format == "md":
        response = HttpResponse(text, content_type="text/markdown; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="clubrelay-preparation.md"'
        return response
    if format == "print":
        return render(request, "ops/briefing_print.html", briefings.export_context(request.user, pk, sid))
    raise Http404
