from django.urls import path

from .views import (
    ApprovalActionView,
    ApprovalDetailView,
    ApprovalListView,
    ApprovalRuleCreateView,
    ApprovalRuleDetailView,
    ApprovalRuleListView,
)

app_name = "approvals"

urlpatterns = [
    path("", ApprovalListView.as_view(), name="approval_list"),
    path("rules/", ApprovalRuleListView.as_view(), name="approval_rule_list"),
    path("rules/new/", ApprovalRuleCreateView.as_view(), name="approval_rule_create"),
    path("rules/<int:pk>/", ApprovalRuleDetailView.as_view(), name="approval_rule_detail"),
    path("<int:pk>/", ApprovalDetailView.as_view(), name="approval_detail"),
    path("<int:pk>/<str:action>/", ApprovalActionView.as_view(), name="approval_action"),
]
