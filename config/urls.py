"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path


def _erp_admin_has_permission(request):
    user = request.user
    return bool(
        user.is_active
        and user.is_superuser
        and not getattr(user, "is_deleted", False)
        and getattr(user, "status", "active") == "active"
    )


admin.site.has_permission = _erp_admin_has_permission

urlpatterns = [
    path('', include('system.urls')),
    path('', include('accounts.urls')),
    path('masterdata/', include('masterdata.urls')),
    path('bom/', include('bom.urls')),
    path('sales/', include('sales.urls')),
    path('purchase/', include('purchase.urls')),
    path('inventory/', include('inventory.urls')),
    path('production/', include('production.urls')),
    path('finance/', include('finance.urls')),
    path('approvals/', include('approvals.urls')),
    path('notifications/', include('notifications.urls')),
    path('files/', include('files.urls')),
    path('admin/', admin.site.urls),
]

handler403 = "system.views.permission_denied_view"
handler404 = "system.views.page_not_found_view"
handler400 = "system.views.bad_request_view"
handler500 = "system.views.server_error_view"
