from django.urls import path

from .views import (
    AccountUserCreateView,
    AccountUserDetailView,
    AccountUserListView,
    AccountUserPasswordResetView,
    AccountUserUpdateView,
    ErpLoginView,
    ErpLogoutView,
    ErpPasswordChangeView,
    PermissionListView,
    RoleCreateView,
    RoleDetailView,
    RoleListView,
    RoleUpdateView,
    UserSessionDetailView,
    UserSessionListView,
    UserSessionRevokeView,
)

urlpatterns = [
    path("login/", ErpLoginView.as_view(), name="login"),
    path("logout/", ErpLogoutView.as_view(), name="logout"),
    path("password/change/", ErpPasswordChangeView.as_view(), name="password_change"),
    path("users/", AccountUserListView.as_view(), name="account_user_list"),
    path("users/create/", AccountUserCreateView.as_view(), name="account_user_create"),
    path("users/<int:pk>/", AccountUserDetailView.as_view(), name="account_user_detail"),
    path("users/<int:pk>/edit/", AccountUserUpdateView.as_view(), name="account_user_edit"),
    path("users/<int:pk>/password-reset/", AccountUserPasswordResetView.as_view(), name="account_user_password_reset"),
    path("roles/", RoleListView.as_view(), name="role_list"),
    path("roles/create/", RoleCreateView.as_view(), name="role_create"),
    path("roles/<int:pk>/", RoleDetailView.as_view(), name="role_detail"),
    path("roles/<int:pk>/edit/", RoleUpdateView.as_view(), name="role_edit"),
    path("permissions/", PermissionListView.as_view(), name="permission_list"),
    path("user-sessions/", UserSessionListView.as_view(), name="user_session_list"),
    path("user-sessions/<int:pk>/", UserSessionDetailView.as_view(), name="user_session_detail"),
    path("user-sessions/<int:pk>/revoke/", UserSessionRevokeView.as_view(), name="user_session_revoke"),
]
