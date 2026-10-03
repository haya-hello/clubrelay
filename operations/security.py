"""负责人私有工作台的访问边界。 / Access boundary for the private manager workspace."""

from functools import wraps
from urllib.parse import unquote

from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import ValidationError
from django.http import HttpResponseForbidden, JsonResponse
from django.urls import Resolver404, resolve
from django.utils.cache import add_never_cache_headers, patch_vary_headers


MANAGER_GROUP = "社团负责人"


def is_manager(user):
    """每次核对有效身份和负责人组。 / Check active identity and group on every request."""
    return bool(
        user.is_authenticated
        and user.is_active
        and user.groups.filter(name=MANAGER_GROUP).exists()
    )


def _wants_json(request):
    accepted = {
        value.split(";", 1)[0].strip().lower()
        for value in request.headers.get("Accept", "").split(",")
    }
    return (
        "application/json" in accepted
        or any(value.endswith("+json") for value in accepted)
        or request.content_type == "application/json"
    )


def _private_response(response):
    """业务结果不留浏览器缓存。 / Keep business responses out of browser caches."""
    add_never_cache_headers(response)
    patch_vary_headers(response, ("Cookie",))
    response.setdefault("X-Content-Type-Options", "nosniff")
    response.setdefault("X-Frame-Options", "DENY")
    response.setdefault("Referrer-Policy", "same-origin")
    return response


def _access_denial(request):
    if not request.user.is_authenticated:
        if _wants_json(request):
            return JsonResponse(
                {"error": "authentication_required", "message": "请先使用负责人账号登录。"},
                status=401,
            )
        return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
    if not is_manager(request.user):
        message = "只有有效的社团负责人账号可以访问此私有运营分析台。"
        if _wants_json(request):
            return JsonResponse({"error": "manager_required", "message": message}, status=403)
        return HttpResponseForbidden(message, content_type="text/plain; charset=utf-8")
    return None


def _public_static_path(path):
    # 静态资源例外不能被编码的路径穿越利用。 / Encoded traversal must not inherit the static exception.
    decoded = path
    for _ in range(3):
        expanded = unquote(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    if not decoded.startswith("/static/"):
        return False
    if any(part in {".", ".."} for part in decoded.split("/")):
        return False
    return not any(char == "\\" or ord(char) < 32 for char in decoded)


def _public_auth_route(request):
    # 按解析后的路由名放行，不能凭字符串前缀绕过。 / Exempt resolved auth names, never path prefixes.
    try:
        match = resolve(request.path_info, urlconf=getattr(request, "urlconf", None))
    except Resolver404:
        return False
    # QQ 桥接接口由自身的时效 HMAC 保护。 / The QQ bridge has its own expiring HMAC boundary.
    return match.view_name in {"login", "logout", "collector_ingest"}


class ManagerOnlyMiddleware:
    """放在 AuthenticationMiddleware 之后。 / Install immediately after AuthenticationMiddleware."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        static_request = _public_static_path(request.path_info)
        if not static_request and not _public_auth_route(request):
            denied = _access_denial(request)
            if denied is not None:
                return _private_response(denied)
        response = self.get_response(request)
        if static_request:
            return response
        return _private_response(response)


class ManagerAuthenticationForm(AuthenticationForm):
    """登录时也拒绝普通成员。 / Reject ordinary members during login as well."""

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not is_manager(user):
            raise ValidationError("仅负责人可以登录此私有运营分析台。", code="manager_required")


def manager_required(view_func):
    """为业务视图再加一道明确边界。 / Add an explicit boundary to individual business views."""

    @wraps(view_func)
    def guarded(request, *args, **kwargs):
        denied = _access_denial(request)
        if denied is not None:
            return _private_response(denied)
        return _private_response(view_func(request, *args, **kwargs))

    return guarded
