from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect
from django.db.models import Q
from urllib.parse import urlencode
from django.views.generic import ListView

from system.display import code_label
from accounts.permissions import require_any_erp_permission

from .models import SavedFilter


class ErpListView(LoginRequiredMixin, ListView):
    template_name = "common/list.html"
    paginate_by = 25
    page_title = ""
    columns: tuple[tuple[str, str], ...] = ()
    create_url_name = ""
    create_permission_required = ""
    detail_url_name = ""
    view_permission_required = ""
    permission_denied_message = "无权限访问此页面"
    page_actions: tuple[tuple[str, str, str], ...] = ()
    page_action_permissions = {}
    sensitive_columns: tuple[str, ...] = ()
    mask_sensitive_columns = False
    search_fields: tuple[str, ...] = ()
    status_filter_field = ""
    filter_fields: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = ()
    sortable_fields: dict[str, str] = {}
    saved_filter_module = ""

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and self.view_permission_required:
            require_any_erp_permission(request.user, self.view_permission_required, self.permission_denied_message)
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        if request.user.is_authenticated and not request.GET:
            default_filter = (
                SavedFilter.objects.filter(
                    user=request.user,
                    module=self._saved_filter_module(),
                    is_default=True,
                )
                .order_by("id")
                .first()
            )
            if default_filter:
                query_string = saved_filter_query_string(default_filter.filter_json)
                if query_string:
                    return redirect(f"{request.path}?{query_string}")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = self.page_title or self.model._meta.verbose_name_plural or self.model.__name__
        context["columns"] = self.columns
        context["create_url_name"] = self.get_create_url_name()
        context["detail_url_name"] = self.detail_url_name
        context["page_actions"] = self.get_page_actions()
        context["sensitive_columns"] = self.sensitive_columns
        context["mask_sensitive_columns"] = self.mask_sensitive_columns
        context["has_search"] = bool(self.search_fields)
        context["search_query"] = self.request.GET.get("q", "").strip()
        context["status_value"] = self.request.GET.get("status", "").strip()
        context["status_choices"] = self._status_choices()
        context["extra_filters"] = self._extra_filter_context()
        active_query_string = self._active_query_string()
        context["active_query_string"] = active_query_string
        context["sort_links"] = self._sort_link_context()
        context["is_filtered_list"] = bool(active_query_string)
        context["empty_message"] = self.get_empty_message(bool(active_query_string))
        context["empty_clear_url"] = self.request.path if active_query_string else ""
        context["saved_filter_module"] = self._saved_filter_module()
        context["saved_filters"] = self._saved_filter_context()
        return context

    def get_empty_message(self, is_filtered: bool) -> str:
        if is_filtered:
            return "没有符合当前筛选条件的数据，请调整条件后重试。"
        title = self.page_title or self.model._meta.verbose_name_plural or self.model.__name__
        return f"暂无{title}数据。"

    def get_create_url_name(self) -> str:
        if self._has_required_permissions(self.create_permission_required):
            return self.create_url_name
        return ""

    def get_page_actions(self) -> tuple[tuple[str, str, str], ...]:
        actions = []
        for label, url_name, style in self.page_actions:
            if self._has_required_permissions(self.page_action_permissions.get(url_name, "")):
                actions.append((label, url_name, style))
        return tuple(actions)

    def _has_required_permissions(self, required_permissions) -> bool:
        if not required_permissions:
            return True
        from accounts.permissions import user_has_permission

        if isinstance(required_permissions, str):
            required_permissions = (required_permissions,)
        return all(user_has_permission(self.request.user, permission) for permission in required_permissions)

    def get_queryset(self):
        queryset = super().get_queryset()
        queryset = self.apply_search(queryset)
        queryset = self.apply_status_filter(queryset)
        queryset = self.apply_extra_filters(queryset)
        queryset = self.apply_sorting(queryset)
        return queryset

    def apply_search(self, queryset):
        search_query = self.request.GET.get("q", "").strip()
        if not search_query or not self.search_fields:
            return queryset
        condition = Q()
        for field in self.search_fields:
            condition |= Q(**{f"{field}__icontains": search_query})
        return queryset.filter(condition)

    def apply_status_filter(self, queryset):
        status_value = self.request.GET.get("status", "").strip()
        if not status_value or not self.status_filter_field:
            return queryset
        return queryset.filter(**{self.status_filter_field: status_value})

    def apply_extra_filters(self, queryset):
        for _label, param_name, _choices in self.filter_fields:
            value = self.request.GET.get(param_name, "").strip()
            if value:
                queryset = queryset.filter(**{param_name: value})
        return queryset

    def apply_sorting(self, queryset):
        ordering = self.current_ordering()
        if not ordering:
            return queryset
        return queryset.order_by(*ordering)

    def get_sortable_fields(self) -> dict[str, str]:
        return dict(self.sortable_fields)

    def current_ordering(self) -> tuple[str, ...]:
        sortable_fields = self.get_sortable_fields()
        sort_key = self.request.GET.get("sort", "").strip()
        sort_field = sortable_fields.get(sort_key)
        if not sort_field:
            return ()
        direction = self.request.GET.get("dir", "asc").strip().lower()
        prefix = "-" if direction == "desc" else ""
        return (f"{prefix}{sort_field}", "pk")

    def _status_choices(self):
        if not self.status_filter_field:
            return ()
        try:
            field = self.model._meta.get_field(self.status_filter_field)
        except Exception:
            return ()
        return field.choices or ()

    def _extra_filter_context(self):
        filters = []
        for label, param_name, choices in self.filter_fields:
            filters.append(
                {
                    "label": label,
                    "param_name": param_name,
                    "choices": choices,
                    "value": self.request.GET.get(param_name, "").strip(),
                }
            )
        return filters

    def _active_query_string(self):
        params = self.request.GET.copy()
        params.pop("page", None)
        return params.urlencode()

    def _sort_link_context(self) -> dict:
        sortable_fields = self.get_sortable_fields()
        current_sort = self.request.GET.get("sort", "").strip()
        current_dir = self.request.GET.get("dir", "asc").strip().lower()
        links = {}
        for _label, field in self.columns:
            if field not in sortable_fields:
                continue
            next_dir = "desc" if current_sort == field and current_dir != "desc" else "asc"
            params = self.request.GET.copy()
            params.pop("page", None)
            params["sort"] = field
            params["dir"] = next_dir
            links[field] = {
                "query_string": params.urlencode(),
                "active": current_sort == field,
                "direction": "desc" if current_dir == "desc" else "asc",
            }
        return links

    def _saved_filter_module(self) -> str:
        if self.saved_filter_module:
            return self.saved_filter_module
        return f"{self.model._meta.app_label}.{self.model._meta.model_name}"

    def _saved_filter_context(self) -> list[dict]:
        rows = []
        for saved_filter in SavedFilter.objects.filter(
            user=self.request.user,
            module=self._saved_filter_module(),
        ).order_by("-is_default", "filter_name", "id"):
            rows.append(
                {
                    "id": saved_filter.id,
                    "filter_name": saved_filter.filter_name,
                    "is_default": saved_filter.is_default,
                    "query_string": saved_filter_query_string(saved_filter.filter_json),
                }
            )
        return rows


def row_value(obj, attr_path: str):
    value = obj
    for attr in attr_path.split("."):
        value = getattr(value, attr, "")
        if value is None:
            return ""
        if callable(value):
            value = value()
    if attr_path.split(".")[-1] in {
        "action",
        "backup_type",
        "event_type",
        "job_type",
        "trigger_type",
        "module",
        "approval_type",
        "doc_type",
        "source_doc_type",
        "source_type",
        "target_doc_type",
        "template_type",
        "scope_type",
    }:
        return code_label(value)
    return value


def filter_json_from_query_string(query_string: str) -> dict:
    from django.http import QueryDict

    query = QueryDict(query_string, mutable=False)
    data = {}
    for key, values in query.lists():
        if key == "page":
            continue
        if len(values) == 1:
            data[key] = values[0]
        elif values:
            data[key] = values
    return {"query": data}


def saved_filter_query_string(filter_json: dict) -> str:
    query = filter_json.get("query", filter_json) if isinstance(filter_json, dict) else {}
    items = []
    for key, value in query.items():
        if key == "page" or value in [None, ""]:
            continue
        if isinstance(value, list):
            for item in value:
                if item not in [None, ""]:
                    items.append((key, item))
        else:
            items.append((key, value))
    return urlencode(items, doseq=True)


def require_second_verify(request, redirect_name: str, pk: int | None = None, **redirect_kwargs):
    current_password = request.POST.get("current_password", "")
    if current_password and request.user.check_password(current_password):
        return None

    messages.error(request, "二次验证失败，请输入当前登录密码后再执行该操作")
    if pk is not None:
        redirect_kwargs.setdefault("pk", pk)
    return redirect(redirect_name, **redirect_kwargs)


def require_post_reason(
    request,
    redirect_name: str,
    pk: int | None = None,
    field_names: tuple[str, ...] = ("reason",),
    message: str = "请填写操作原因",
    **redirect_kwargs,
):
    for field_name in field_names:
        reason = request.POST.get(field_name, "").strip()
        if reason:
            return reason, None

    messages.error(request, message)
    if pk is not None:
        redirect_kwargs.setdefault("pk", pk)
    return "", redirect(redirect_name, **redirect_kwargs)


def optional_post_reason(
    request,
    field_names: tuple[str, ...] = ("operation_reason", "change_reason", "reason"),
    default: str = "页面编辑",
) -> str:
    for field_name in field_names:
        reason = request.POST.get(field_name, "").strip()
        if reason:
            return reason
    return default
