from django.urls import path

from .views import (
    AuditLogListView,
    BackgroundJobListView,
    BackupListView,
    DashboardView,
    HealthCheckView,
    ReleaseRecordListView,
    SavedFilterDeleteView,
    SavedFilterSaveView,
    SavedFilterSetDefaultView,
)

app_name = "system"

urlpatterns = [
    path("", DashboardView.as_view(), name="dashboard"),
    path("health/", HealthCheckView.as_view(), name="health_check"),
    path("backups/", BackupListView.as_view(), name="backup_list"),
    path("background-jobs/", BackgroundJobListView.as_view(), name="background_job_list"),
    path("release-records/", ReleaseRecordListView.as_view(), name="release_record_list"),
    path("audit-logs/", AuditLogListView.as_view(), name="audit_log_list"),
    path("saved-filters/save/", SavedFilterSaveView.as_view(), name="saved_filter_save"),
    path("saved-filters/<int:pk>/delete/", SavedFilterDeleteView.as_view(), name="saved_filter_delete"),
    path("saved-filters/<int:pk>/default/", SavedFilterSetDefaultView.as_view(), name="saved_filter_default"),
]
