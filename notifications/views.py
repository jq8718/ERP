from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from datetime import datetime, time, timedelta
from django.utils import timezone
from django.shortcuts import redirect
from django.views import View
from django.views.generic import DetailView

from system.view_helpers import ErpListView

from .models import SystemMessage
from .services import close_message, mark_message_processed, mark_message_read, refresh_due_snoozed_messages, snooze_message


class SystemMessageListView(ErpListView):
    model = SystemMessage
    page_title = "系统消息"
    detail_url_name = "notifications:message_detail"
    columns = (
        ("标题", "title"),
        ("级别", "get_level_display"),
        ("状态", "get_status_display"),
        ("来源", "source_doc_type"),
        ("来源单号", "source_doc_no"),
        ("创建时间", "created_at"),
    )
    ordering = ["-created_at"]
    search_fields = ("title", "source_doc_no", "source_doc_type")
    status_filter_field = "status"
    filter_fields = (("级别", "level", SystemMessage.Level.choices),)
    field_filters = (
        {"label": "标题", "param": "title", "field": "title", "placeholder": "消息标题"},
        {"label": "来源类型", "param": "source_doc_type", "field": "source_doc_type", "placeholder": "来源类型"},
        {"label": "来源单号", "param": "source_doc_no", "field": "source_doc_no", "placeholder": "来源单号"},
    )

    def get_queryset(self):
        refresh_due_snoozed_messages(self.request.user.id)
        queryset = super().get_queryset().filter(receiver=self.request.user)
        if self.request.GET.get("status", "").strip() != SystemMessage.Status.SNOOZED:
            queryset = queryset.exclude(
                status=SystemMessage.Status.SNOOZED,
                snoozed_until__gt=timezone.now(),
            )
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["bulk_action_url_name"] = "notifications:message_bulk_action"
        context["bulk_select_name"] = "message_ids"
        context["bulk_actions"] = (
            ("process", "标记已处理"),
            ("close", "关闭消息"),
            ("snooze_one_hour", "1小时后提醒"),
            ("snooze_tomorrow", "明天9点提醒"),
        )
        return context


class SystemMessageBulkActionView(LoginRequiredMixin, View):
    def post(self, request):
        action = request.POST.get("action", "")
        message_ids = [int(value) for value in request.POST.getlist("message_ids") if value.isdigit()]
        if not message_ids:
            messages.error(request, "请选择要处理的消息")
            return redirect("notifications:message_list")

        queryset = SystemMessage.objects.filter(receiver=request.user, id__in=message_ids)
        if action == "process":
            updated = queryset.exclude(status__in=[SystemMessage.Status.PROCESSED, SystemMessage.Status.CLOSED]).update(
                status=SystemMessage.Status.PROCESSED,
                processed_at=timezone.now(),
                snoozed_until=None,
            )
            messages.success(request, f"已处理 {updated} 条消息")
        elif action == "close":
            updated = queryset.exclude(status__in=[SystemMessage.Status.PROCESSED, SystemMessage.Status.CLOSED]).update(
                status=SystemMessage.Status.CLOSED,
                snoozed_until=None,
            )
            messages.success(request, f"已关闭 {updated} 条消息")
        elif action in ["snooze_one_hour", "snooze_tomorrow"]:
            snoozed_until = _snooze_until(action)
            updated = queryset.exclude(status__in=[SystemMessage.Status.PROCESSED, SystemMessage.Status.CLOSED]).update(
                status=SystemMessage.Status.SNOOZED,
                snoozed_until=snoozed_until,
                read_at=timezone.now(),
            )
            messages.success(request, f"已设置 {updated} 条消息稍后提醒")
        else:
            messages.error(request, "未知批量操作")
        return redirect("notifications:message_list")


class SystemMessageDetailView(LoginRequiredMixin, DetailView):
    model = SystemMessage
    template_name = "notifications/message_detail.html"
    context_object_name = "message_obj"

    def get_queryset(self):
        return super().get_queryset().filter(receiver=self.request.user)

    def get_object(self, queryset=None):
        refresh_due_snoozed_messages(self.request.user.id)
        message = super().get_object(queryset)
        if message.status == SystemMessage.Status.UNREAD:
            mark_message_read(message.id, self.request.user.id)
            message.refresh_from_db()
        return message

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"消息 {self.object.message_no}"
        context["can_process"] = self.object.status not in [SystemMessage.Status.PROCESSED, SystemMessage.Status.CLOSED]
        context["can_close"] = self.object.status not in [SystemMessage.Status.PROCESSED, SystemMessage.Status.CLOSED]
        context["can_snooze"] = self.object.status not in [SystemMessage.Status.PROCESSED, SystemMessage.Status.CLOSED]
        return context


class SystemMessageProcessView(LoginRequiredMixin, View):
    def post(self, request, pk):
        result = mark_message_processed(pk, request.user.id)
        if result.success:
            messages.success(request, result.message)
        else:
            messages.error(request, result.message or result.error_code or "消息处理失败")
        return redirect("notifications:message_detail", pk=pk)


class SystemMessageCloseView(LoginRequiredMixin, View):
    def post(self, request, pk):
        result = close_message(pk, request.user.id)
        if result.success:
            messages.success(request, result.message)
        else:
            messages.error(request, result.message or result.error_code or "消息关闭失败")
        return redirect("notifications:message_detail", pk=pk)


class SystemMessageSnoozeView(LoginRequiredMixin, View):
    def post(self, request, pk):
        option = request.POST.get("option", "snooze_one_hour")
        result = snooze_message(pk, request.user.id, _snooze_until(option))
        if result.success:
            messages.success(request, result.message)
        else:
            messages.error(request, result.message or result.error_code or "稍后提醒设置失败")
        return redirect("notifications:message_detail", pk=pk)


def _snooze_until(option: str):
    if option == "snooze_tomorrow":
        local_now = timezone.localtime()
        target_date = local_now.date() + timedelta(days=1)
        return timezone.make_aware(datetime.combine(target_date, time(hour=9)), timezone.get_current_timezone())
    return timezone.now() + timedelta(hours=1)
