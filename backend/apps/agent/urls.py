from django.urls import path
from . import views

urlpatterns = [
    path("chat/", views.chat, name="agent-chat"),
    path("frame/status/", views.frame_status, name="agent-frame-status"),
    path("frame/control/", views.frame_control, name="agent-frame-control"),
    path("list/", views.list_agents, name="agent-list"),
    path("tasks/scheduled/", views.list_scheduled_tasks, name="agent-tasks-scheduled"),
    path(
        "workflow/history/", views.list_workflow_history, name="workflow-history-list"
    ),
    path(
        "workflow/history/<str:workflow_id>/",
        views.get_workflow_history,
        name="workflow-history-detail",
    ),
]
