from django.urls import path

from .views import (
    AttachmentAccessLogListView,
    AttachmentDeleteView,
    AttachmentDetailView,
    AttachmentDownloadView,
    AttachmentListView,
    AttachmentUploadView,
    ExportLogDownloadView,
    ExportLogDetailView,
    ExportLogListView,
    ImportJobDetailView,
    ImportJobListView,
    InitializationJobDetailView,
    InitializationJobListView,
    PrintLogDetailView,
    PrintLogListView,
)

app_name = "files"

urlpatterns = [
    path("", AttachmentListView.as_view(), name="attachment_list"),
    path("access-logs/", AttachmentAccessLogListView.as_view(), name="attachment_access_log_list"),
    path("import-jobs/", ImportJobListView.as_view(), name="import_job_list"),
    path("initialization-jobs/", InitializationJobListView.as_view(), name="initialization_job_list"),
    path("import-jobs/<int:pk>/", ImportJobDetailView.as_view(), name="import_job_detail"),
    path("initialization-jobs/<int:pk>/", InitializationJobDetailView.as_view(), name="initialization_job_detail"),
    path("export-logs/", ExportLogListView.as_view(), name="export_log_list"),
    path("export-logs/<int:pk>/", ExportLogDetailView.as_view(), name="export_log_detail"),
    path("export-logs/<int:pk>/download/", ExportLogDownloadView.as_view(), name="export_log_download"),
    path("print-logs/", PrintLogListView.as_view(), name="print_log_list"),
    path("print-logs/<int:pk>/", PrintLogDetailView.as_view(), name="print_log_detail"),
    path("upload/", AttachmentUploadView.as_view(), name="attachment_upload"),
    path("<int:pk>/", AttachmentDetailView.as_view(), name="attachment_detail"),
    path("<int:pk>/download/", AttachmentDownloadView.as_view(), name="attachment_download"),
    path("<int:pk>/delete/", AttachmentDeleteView.as_view(), name="attachment_delete"),
]
