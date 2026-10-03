from django.urls import path

from . import views


urlpatterns = [
    path("collector/", views.dashboard, name="collector_dashboard"),
    path("collector/sources/", views.sources, name="collector_sources"),
    path("collector/simulate/", views.simulate, name="collector_simulate"),
    path("collector/inbox/", views.inbox, name="collector_inbox"),
    path("collector/inbox/<uuid:pk>/", views.marker, name="collector_marker"),
    path("collector/process/", views.run_processing, name="collector_process"),
    path("collector/knowledge/", views.knowledge, name="collector_knowledge"),
    path("collector/knowledge/<uuid:pk>/", views.card, name="collector_card"),
    path("collector/ask/", views.assistant, name="collector_assistant"),
    path("integrations/qq/events/", views.ingest, name="collector_ingest"),
]
