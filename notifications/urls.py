from django.urls import path

from .views import (
    SystemMessageBulkActionView,
    SystemMessageCloseView,
    SystemMessageDetailView,
    SystemMessageListView,
    SystemMessageProcessView,
    SystemMessageSnoozeView,
)

app_name = "notifications"

urlpatterns = [
    path("", SystemMessageListView.as_view(), name="message_list"),
    path("bulk-action/", SystemMessageBulkActionView.as_view(), name="message_bulk_action"),
    path("<int:pk>/", SystemMessageDetailView.as_view(), name="message_detail"),
    path("<int:pk>/process/", SystemMessageProcessView.as_view(), name="message_process"),
    path("<int:pk>/close/", SystemMessageCloseView.as_view(), name="message_close"),
    path("<int:pk>/snooze/", SystemMessageSnoozeView.as_view(), name="message_snooze"),
]
