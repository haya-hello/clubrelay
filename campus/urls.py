from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path, include
from operations.security import ManagerAuthenticationForm
from operations import views as ops_views
from knowledge import views
from knowledge import assistant_views
from activities import views as activity_views
from activities import delivery_views
from activities import retro_views

urlpatterns = [
    path("login/", LoginView.as_view(template_name="ops/login.html",authentication_form=ManagerAuthenticationForm), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("", include("collector.urls")),
    path("", include("operations.urls")),
    path("", ops_views.home, name="home"),
    path("legacy/home/", views.home, name="legacy_home"),
    path("activities/", activity_views.listing, name="activities"),
    path("activities/new/", activity_views.create, name="activity_create"),
    path("activities/<uuid:pk>/", activity_views.detail, name="activity_detail"),
    path("activities/<uuid:pk>/stage/", retro_views.stage, name="activity_stage"),
    path("activities/<uuid:pk>/retros/new/", retro_views.create, name="retro_create"),
    path("retros/<uuid:pk>/", retro_views.detail, name="retro_detail"),
    path("retros/<uuid:pk>/save/", retro_views.save, name="retro_save"),
    path("retros/<uuid:pk>/action/", retro_views.action, name="retro_action"),
    path("retros/<uuid:pk>/export/", retro_views.export, name="retro_export"),
    path("activities/<uuid:pk>/tasks/new/", activity_views.task_create, name="task_create"),
    path("tasks/<uuid:pk>/", activity_views.task_detail, name="task_detail"),
    path("tasks/<uuid:pk>/action/", activity_views.task_action, name="task_action"),
    path("tasks/<uuid:pk>/submit/", delivery_views.submit, name="submit_result"),
    path("results/<uuid:pk>/", delivery_views.detail, name="submission_detail"),
    path("results/<uuid:pk>/review/", delivery_views.review, name="review_result"),
    path("results/<uuid:pk>/download/", delivery_views.download, name="download_result"),
    path("acceptance/", delivery_views.queue, name="acceptance"),
    path("growth/", delivery_views.growth, name="growth"),
    path("knowledge/", views.library, name="library"),
    path("assistant/", assistant_views.assistant, name="assistant"),
    path("assistant/clear/", assistant_views.clear_history, name="clear_answers"),
    path("assistant/sources/<uuid:pk>/<int:version>/<int:chunk>/", assistant_views.source, name="answer_source"),
    path("submissions/", views.mine, name="mine"),
    path("submissions/new/", views.submit, name="submit"),
    path("review/", views.queue, name="queue"),
    path("knowledge/<uuid:pk>/", views.detail, name="detail"),
    path("knowledge/<uuid:pk>/edit/", views.edit, name="edit"),
    path("knowledge/<uuid:pk>/review/", views.review, name="review"),
]
