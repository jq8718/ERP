from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Permission, Role, User, UserSession


@admin.register(User)
class ErpUserAdmin(UserAdmin):
    list_display = ("username", "display_name", "department", "security_level", "status", "is_staff")
    list_filter = ("status", "security_level", "department", "is_staff")
    search_fields = ("username", "display_name", "email", "department")
    fieldsets = UserAdmin.fieldsets + (
        ("ERP", {"fields": ("display_name", "department", "position", "security_level", "status", "is_deleted", "roles")}),
    )
    filter_horizontal = UserAdmin.filter_horizontal + ("roles",)


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("role_code", "role_name", "status")
    list_filter = ("status",)
    search_fields = ("role_code", "role_name")
    filter_horizontal = ("permissions",)


@admin.register(Permission)
class PermissionAdmin(admin.ModelAdmin):
    list_display = ("permission_code", "permission_name", "permission_type")
    list_filter = ("permission_type",)
    search_fields = ("permission_code", "permission_name")


@admin.register(UserSession)
class UserSessionAdmin(admin.ModelAdmin):
    list_display = ("user", "session_key", "status", "ip_address", "created_at", "last_seen_at")
    list_filter = ("status",)
    search_fields = ("user__username", "session_key", "ip_address")
