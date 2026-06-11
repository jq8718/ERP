from django.urls import path

from .views import (
    BomCopyVersionView,
    BomCreateView,
    BomDetailView,
    BomDisableView,
    BomEnableView,
    BomExportView,
    BomItemCreateView,
    BomItemDeleteView,
    BomItemEditView,
    BomListView,
    BomUpdateView,
)

app_name = "bom"

urlpatterns = [
    path("", BomListView.as_view(), name="bom_list"),
    path("export/", BomExportView.as_view(), name="bom_export"),
    path("new/", BomCreateView.as_view(), name="bom_create"),
    path("<int:pk>/", BomDetailView.as_view(), name="bom_detail"),
    path("<int:pk>/edit/", BomUpdateView.as_view(), name="bom_edit"),
    path("<int:pk>/copy-version/", BomCopyVersionView.as_view(), name="bom_copy_version"),
    path("<int:pk>/items/new/", BomItemCreateView.as_view(), name="bom_item_create"),
    path("<int:pk>/items/<int:item_pk>/edit/", BomItemEditView.as_view(), name="bom_item_edit"),
    path("<int:pk>/items/<int:item_pk>/delete/", BomItemDeleteView.as_view(), name="bom_item_delete"),
    path("<int:pk>/enable/", BomEnableView.as_view(), name="bom_enable"),
    path("<int:pk>/disable/", BomDisableView.as_view(), name="bom_disable"),
]
