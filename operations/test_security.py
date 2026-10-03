"""仅用虚构账号验证负责人边界。 / Verify the manager boundary with fictional accounts only."""

from unittest.mock import Mock

from django.contrib.auth.models import AnonymousUser, Group, User
from django.contrib.auth.views import LoginView, LogoutView
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings
from django.urls import path

from .security import (
    MANAGER_GROUP,
    ManagerAuthenticationForm,
    ManagerOnlyMiddleware,
    is_manager,
    manager_required,
)


PRIVATE_CONTENT = "仅负责人可见的虚构资料"
TEST_PASSWORD = "fictional-manager-boundary-only"


def private_view(request, **kwargs):
    return HttpResponse(PRIVATE_CONTENT)


def static_view(request):
    return HttpResponse("body { color: #123; }", content_type="text/css")


urlpatterns = [
    path("", private_view, name="home"),
    path(
        "entry/",
        LoginView.as_view(
            authentication_form=ManagerAuthenticationForm,
            template_name="security_login.html",
        ),
        name="login",
    ),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("static/app.css", static_view, name="static_test"),
    path("activities/", private_view, name="activities"),
    path("knowledge/", private_view, name="knowledge_test"),
    path("assistant/", private_view, name="assistant"),
    path("assistant/sources/<path:source>/", private_view, name="source_test"),
    path("results/<str:pk>/download/", private_view, name="download_test"),
    path("tasks/<str:pk>/action/", private_view, name="action_test"),
    path("growth/", private_view, name="growth"),
    path("api/summary/", private_view, name="api_test"),
    path("private-uploads-not-served/<path:file>", private_view, name="private_file_test"),
    path("login/secret/", private_view, name="not_login_test"),
    path("static-lookalike/file", private_view, name="not_static_test"),
]


SECURITY_TEST_SETTINGS = {
    "ROOT_URLCONF": __name__,
    "LOGIN_URL": "login",
    "LOGIN_REDIRECT_URL": "home",
    "LOGOUT_REDIRECT_URL": "login",
    "MIDDLEWARE": [
        "django.middleware.security.SecurityMiddleware",
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.middleware.common.CommonMiddleware",
        "django.middleware.csrf.CsrfViewMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "operations.security.ManagerOnlyMiddleware",
        "django.contrib.messages.middleware.MessageMiddleware",
        "django.middleware.clickjacking.XFrameOptionsMiddleware",
    ],
    "TEMPLATES": [
        {
            "BACKEND": "django.template.backends.django.DjangoTemplates",
            "APP_DIRS": False,
            "OPTIONS": {
                "loaders": [
                    (
                        "django.template.loaders.locmem.Loader",
                        {"security_login.html": "负责人登录 {{ form.errors }}"},
                    )
                ]
            },
        }
    ],
}


@override_settings(**SECURITY_TEST_SETTINGS)
class ManagerBoundaryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.group = Group.objects.create(name=MANAGER_GROUP)
        cls.member = User.objects.create_user("boundary_member", password=TEST_PASSWORD)
        cls.manager = User.objects.create_user("boundary_manager", password=TEST_PASSWORD)
        cls.manager.groups.add(cls.group)
        cls.staff = User.objects.create_user("boundary_staff", password=TEST_PASSWORD, is_staff=True)
        cls.superuser = User.objects.create_superuser(
            "boundary_superuser", password=TEST_PASSWORD, email=""
        )
        cls.inactive = User.objects.create_user(
            "boundary_inactive", password=TEST_PASSWORD, is_active=False
        )
        cls.inactive.groups.add(cls.group)

    def assert_private(self, response):
        for value in ("private", "no-store", "no-cache", "max-age=0"):
            self.assertIn(value, response["Cache-Control"])
        self.assertIn("Cookie", response["Vary"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["X-Frame-Options"], "DENY")

    def test_anonymous_business_access_redirects_with_return_path(self):
        response = self.client.get("/activities/?q=local")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/entry/?next=/activities/%3Fq%3Dlocal")
        self.assert_private(response)

    def test_anonymous_json_accept_is_401(self):
        response = self.client.get("/api/summary/", HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "authentication_required")
        self.assert_private(response)

    def test_json_post_is_401_without_executing_view(self):
        response = self.client.post("/api/summary/", "{}", content_type="application/json")
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(PRIVATE_CONTENT, response.content.decode())

    def test_member_is_blocked_on_all_legacy_routes_and_downloads(self):
        self.client.force_login(self.member)
        for target in (
            "/",
            "/activities/",
            "/knowledge/",
            "/assistant/",
            "/assistant/sources/fake/1/0/",
            "/results/fake/download/",
            "/tasks/fake/action/",
            "/growth/",
            "/api/summary/",
            "/private-uploads-not-served/file.txt",
            "/login/secret/",
        ):
            with self.subTest(target=target):
                response = self.client.get(target)
                self.assertEqual(response.status_code, 403)
                self.assertNotIn(PRIVATE_CONTENT, response.content.decode())
                self.assert_private(response)

    def test_member_post_is_blocked(self):
        self.client.force_login(self.member)
        response = self.client.post("/tasks/fake/action/", {"action": "accept"})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(PRIVATE_CONTENT, response.content.decode())

    def test_member_json_access_returns_403(self):
        self.client.force_login(self.member)
        response = self.client.get("/api/summary/", HTTP_ACCEPT="application/problem+json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "manager_required")

    def test_staff_and_superuser_do_not_bypass_explicit_group(self):
        for user in (self.staff, self.superuser):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.get("/").status_code, 403)
                self.assertFalse(is_manager(user))

    def test_manager_can_use_existing_routes(self):
        self.client.force_login(self.manager)
        for target in ("/", "/activities/", "/assistant/", "/results/fake/download/"):
            with self.subTest(target=target):
                response = self.client.get(target)
                self.assertContains(response, PRIVATE_CONTENT)
                self.assert_private(response)

    def test_removing_manager_group_blocks_existing_session(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.manager.groups.remove(self.group)
        response = self.client.get("/assistant/")
        self.assertEqual(response.status_code, 403)
        self.assert_private(response)

    def test_disabled_authenticated_user_is_forbidden(self):
        request = RequestFactory().get("/activities/")
        request.user = self.inactive
        next_handler = Mock(return_value=HttpResponse(PRIVATE_CONTENT))
        response = ManagerOnlyMiddleware(next_handler)(request)
        self.assertEqual(response.status_code, 403)
        next_handler.assert_not_called()

    def test_disabling_user_revokes_existing_session_access(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get("/").status_code, 200)
        User.objects.filter(pk=self.manager.pk).update(is_active=False)
        response = self.client.get("/results/fake/download/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/entry/?next="))
        self.assertNotIn(PRIVATE_CONTENT, response.content.decode())

    def test_named_login_is_public_even_when_path_is_not_login(self):
        response = self.client.get("/entry/")
        self.assertContains(response, "负责人登录")
        self.assert_private(response)

    def test_login_form_rejects_correct_member_password(self):
        response = self.client.post(
            "/entry/", {"username": self.member.username, "password": TEST_PASSWORD}
        )
        self.assertContains(response, "仅负责人可以登录")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_form_rejects_superuser_without_manager_group(self):
        form = ManagerAuthenticationForm(
            data={"username": self.superuser.username, "password": TEST_PASSWORD}
        )
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors.as_data()["__all__"][0].code, "manager_required")

    def test_login_form_keeps_disabled_accounts_out(self):
        form = ManagerAuthenticationForm(
            data={"username": self.inactive.username, "password": TEST_PASSWORD}
        )
        self.assertFalse(form.is_valid())

    def test_valid_manager_login_is_accepted(self):
        response = self.client.post(
            "/entry/", {"username": self.manager.username, "password": TEST_PASSWORD}
        )
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.manager.pk)
        self.assert_private(response)

    def test_login_does_not_redirect_to_external_next(self):
        response = self.client.post(
            "/entry/?next=https://example.invalid/",
            {"username": self.manager.username, "password": TEST_PASSWORD},
        )
        self.assertRedirects(response, "/", fetch_redirect_response=False)

    def test_member_can_logout_from_an_old_session(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.get("/logout/").status_code, 405)
        response = self.client.post("/logout/")
        self.assertRedirects(response, "/entry/", fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assert_private(response)

    def test_static_css_is_public_but_private_files_are_not(self):
        response = self.client.get("/static/app.css")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/css")
        self.assertEqual(self.client.get("/static/not-a-public-file.txt").status_code, 404)
        self.assertEqual(self.client.get("/private-uploads-not-served/file.txt").status_code, 302)

    def test_static_lookalikes_do_not_bypass_manager_check(self):
        self.client.force_login(self.member)
        for target in (
            "/static-lookalike/file",
            "/static/../private-uploads-not-served/file.txt",
            "/static/%2e%2e/private-uploads-not-served/file.txt",
            "/static/%252e%252e/private-uploads-not-served/file.txt",
            "/static/..\\private-file.txt",
        ):
            with self.subTest(target=target):
                self.assertEqual(self.client.get(target).status_code, 403)

    def test_unknown_business_routes_do_not_reveal_content_to_member(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.get("/not-a-route/").status_code, 403)
        self.client.force_login(self.manager)
        response = self.client.get("/not-a-route/")
        self.assertEqual(response.status_code, 404)
        self.assert_private(response)

    def test_no_store_replaces_public_cache_directives(self):
        request = RequestFactory().get("/activities/")
        request.user = self.manager
        cached = HttpResponse(PRIVATE_CONTENT)
        cached["Cache-Control"] = "public, max-age=3600"
        response = ManagerOnlyMiddleware(lambda request: cached)(request)
        self.assert_private(response)
        self.assertNotIn("public", response["Cache-Control"])

    def test_decorator_alone_rejects_member_without_running_view(self):
        request = RequestFactory().post("/api/summary/", content_type="application/json")
        request.user = self.member
        view = Mock(return_value=HttpResponse(PRIVATE_CONTENT))
        response = manager_required(view)(request)
        self.assertEqual(response.status_code, 403)
        view.assert_not_called()
        self.assert_private(response)

    def test_decorator_alone_redirects_anonymous(self):
        request = RequestFactory().get("/activities/")
        request.user = AnonymousUser()
        response = manager_required(private_view)(request)
        self.assertEqual(response.status_code, 302)
        self.assert_private(response)

    def test_decorator_allows_manager_and_keeps_private_cache(self):
        request = RequestFactory().get("/activities/")
        request.user = self.manager
        response = manager_required(private_view)(request)
        self.assertContains(response, PRIVATE_CONTENT)
        self.assert_private(response)
