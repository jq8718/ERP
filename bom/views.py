from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.validators import MinValueValidator
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.views import View
from django.views.generic import DetailView
from django.views.generic.edit import CreateView, UpdateView

from accounts.permissions import PermissionCode, require_any_erp_permission, require_erp_permission, user_has_permission
from files.services import export_queryset_to_csv
from files.view_helpers import export_file_response
from masterdata.models import Material
from system.display import set_form_labels
from system.services import record_audit_log_from_request
from system.view_helpers import ErpListView, optional_post_reason, require_post_reason, require_second_verify

from .models import Bom, BomItem
from .services import disable_bom, enable_bom


EDITABLE_BOM_STATUSES = {
    Bom.BomStatus.DRAFT,
    Bom.BomStatus.PENDING_APPROVAL,
    Bom.BomStatus.REJECTED,
}

class BomListView(ErpListView):
    model = Bom
    page_title = "产品组成清单"
    create_url_name = "bom:bom_create"
    create_permission_required = PermissionCode.BOM_PROCESS
    view_permission_required = (PermissionCode.BOM_VIEW, PermissionCode.BOM_PROCESS)
    permission_denied_message = "缺少产品组成清单查看权限"
    detail_url_name = "bom:bom_detail"
    columns = (
        ("清单编号", "bom_no"),
        ("成品", "finished_material.material_code"),
        ("版本", "bom_version"),
        ("状态", "get_status_display"),
        ("默认", "is_default"),
    )
    ordering = ["-created_at"]
    page_actions = (("导出CSV", "bom:bom_export", ""),)
    search_fields = ("bom_no", "finished_material__material_code", "finished_material__material_name", "bom_version")
    status_filter_field = "status"

    def get_queryset(self):
        return super().get_queryset().select_related("finished_material")


class BomExportView(LoginRequiredMixin, View):
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, (PermissionCode.BOM_VIEW, PermissionCode.BOM_PROCESS), "缺少产品组成清单查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        list_view = BomListView()
        list_view.request = self.request
        queryset = Bom.objects.all()
        queryset = list_view.apply_search(queryset)
        queryset = list_view.apply_status_filter(queryset)
        queryset = list_view.apply_extra_filters(queryset)
        return queryset.select_related("finished_material").order_by("-created_at")

    def get(self, request):
        result = export_queryset_to_csv(
            "boms",
            self.get_queryset(),
            BomListView.columns,
            request.user.id,
            filter_json={"ordering": "-created_at", "query": request.GET.dict()},
        )
        return export_file_response(result)


class BomCreateView(LoginRequiredMixin, CreateView):
    model = Bom
    template_name = "bom/bom_form.html"
    fields = [
        "bom_no",
        "finished_material",
        "bom_version",
        "base_qty",
        "effective_date",
        "expiry_date",
        "remark",
    ]

    def dispatch(self, request, *args, **kwargs):
        require_erp_permission(request.user, PermissionCode.BOM_PROCESS, "缺少产品组成清单维护权限")
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        return _prepare_bom_header_form(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "新建产品组成清单"
        return context

    def form_valid(self, form):
        form.instance.status = Bom.BomStatus.DRAFT
        form.instance.is_default = False
        form.instance.created_by = self.request.user
        form.instance.updated_by = self.request.user
        operation_reason = optional_post_reason(self.request, default="页面创建产品组成清单")
        response = super().form_valid(form)
        record_audit_log_from_request(
            self.request,
            "bom_create",
            "bom",
            self.object.id,
            self.object.bom_no,
            after_snapshot=_bom_snapshot(self.object, operation_reason),
        )
        messages.success(self.request, "产品组成清单已创建")
        return response

    def get_success_url(self):
        return f"/bom/{self.object.pk}/"


class BomUpdateView(LoginRequiredMixin, UpdateView):
    model = Bom
    template_name = "bom/bom_form.html"
    fields = [
        "bom_no",
        "finished_material",
        "bom_version",
        "base_qty",
        "effective_date",
        "expiry_date",
        "remark",
    ]

    def dispatch(self, request, *args, **kwargs):
        require_erp_permission(request.user, PermissionCode.BOM_PROCESS, "缺少产品组成清单维护权限")
        self.object = self.get_object()
        if not _can_edit_bom_items(self.object):
            messages.error(request, "当前组成清单状态不允许直接编辑，请复制为新版本后修改")
            return redirect("bom:bom_detail", pk=self.object.pk)
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        return _prepare_bom_header_form(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"编辑产品组成清单 {self.object.bom_no}"
        context["bom"] = self.object
        return context

    def form_valid(self, form):
        before_snapshot = _bom_snapshot(Bom.objects.select_related("finished_material").get(pk=self.object.pk))
        operation_reason = optional_post_reason(self.request, default="页面编辑产品组成清单")
        form.instance.updated_by = self.request.user
        form.instance.version += 1
        response = super().form_valid(form)
        record_audit_log_from_request(
            self.request,
            "bom_update",
            "bom",
            self.object.id,
            self.object.bom_no,
            before_snapshot=before_snapshot,
            after_snapshot=_bom_snapshot(self.object, operation_reason),
        )
        messages.success(self.request, "产品组成清单已更新")
        return response

    def get_success_url(self):
        return f"/bom/{self.object.pk}/"


class BomDetailView(LoginRequiredMixin, DetailView):
    model = Bom
    template_name = "bom/bom_detail.html"
    context_object_name = "bom"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, (PermissionCode.BOM_VIEW, PermissionCode.BOM_PROCESS), "缺少产品组成清单查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return super().get_queryset().select_related("finished_material", "created_by", "updated_by", "approved_by").prefetch_related("items__component_material")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"产品组成清单 {self.object.bom_no}"
        context["component_materials"] = (
            Material.objects.exclude(id=self.object.finished_material_id)
            .filter(status=Material.MaterialStatus.ACTIVE)
            .order_by("material_type", "material_code")
        )
        can_process_bom = _can_process_bom(self.request.user)
        context["can_process_bom"] = can_process_bom
        context["can_edit_bom"] = can_process_bom and _can_edit_bom_items(self.object)
        context["copy_bom_no_placeholder"] = f"{self.object.bom_no}-NEW"
        context["copy_bom_version_placeholder"] = f"{self.object.bom_version}-NEW"
        return context


class BomItemCreateView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.BOM_PROCESS, "缺少产品组成清单维护权限")
        try:
            bom = Bom.objects.get(pk=pk)
        except Bom.DoesNotExist:
            messages.error(request, "产品组成清单不存在")
            return redirect("bom:bom_list")

        if bom.status in [Bom.BomStatus.ENABLED, Bom.BomStatus.DISABLED, Bom.BomStatus.VOIDED]:
            messages.error(request, "已启用、已停用或已作废组成清单不能直接新增明细，请复制为新版本后修改")
            return redirect("bom:bom_detail", pk=pk)

        parsed = _parse_item_post(request, bom)
        if not parsed["success"]:
            messages.error(request, parsed["message"])
            return redirect("bom:bom_detail", pk=pk)

        operation_reason = optional_post_reason(request, default="页面新增组成明细")
        try:
            item = BomItem.objects.create(
                bom=bom,
                line_no=parsed["line_no"],
                component_material=parsed["component"],
                usage_qty=parsed["usage_qty"],
                usage_unit=parsed["usage_unit"],
                loss_rate=parsed["loss_rate"],
                is_required=parsed["is_required"],
                remark=parsed["remark"],
            )
        except IntegrityError:
            messages.error(request, "组成明细保存失败，请检查行号是否重复")
            return redirect("bom:bom_detail", pk=pk)
        _touch_bom(bom, request.user)
        record_audit_log_from_request(
            request,
            "bom_item_create",
            "bom",
            bom.id,
            bom.bom_no,
            after_snapshot=_bom_item_snapshot(item, operation_reason),
        )
        messages.success(request, "组成明细已新增")
        return redirect("bom:bom_detail", pk=pk)


class BomItemEditView(LoginRequiredMixin, View):
    template_name = "bom/bom_item_form.html"

    def get(self, request, pk, item_pk):
        require_erp_permission(request.user, PermissionCode.BOM_PROCESS, "缺少产品组成清单维护权限")
        item = _get_bom_item(pk, item_pk)
        if item is None:
            messages.error(request, "组成明细不存在")
            return redirect("bom:bom_detail", pk=pk)
        if not _can_edit_bom_items(item.bom):
            messages.error(request, "当前组成清单状态不允许编辑明细")
            return redirect("bom:bom_detail", pk=pk)
        return render(request, self.template_name, self._context(item))

    def post(self, request, pk, item_pk):
        require_erp_permission(request.user, PermissionCode.BOM_PROCESS, "缺少产品组成清单维护权限")
        item = _get_bom_item(pk, item_pk)
        if item is None:
            messages.error(request, "组成明细不存在")
            return redirect("bom:bom_detail", pk=pk)
        bom = item.bom
        if not _can_edit_bom_items(bom):
            messages.error(request, "当前组成清单状态不允许编辑明细")
            return redirect("bom:bom_detail", pk=pk)

        parsed = _parse_item_post(request, bom, exclude_item_id=item.id)
        if not parsed["success"]:
            messages.error(request, parsed["message"])
            return redirect("bom:bom_item_edit", pk=pk, item_pk=item_pk)

        before_snapshot = _bom_item_snapshot(item)
        operation_reason = optional_post_reason(request, default="页面编辑组成明细")

        item.line_no = parsed["line_no"]
        item.component_material = parsed["component"]
        item.usage_qty = parsed["usage_qty"]
        item.usage_unit = parsed["usage_unit"]
        item.loss_rate = parsed["loss_rate"]
        item.is_required = parsed["is_required"]
        item.remark = parsed["remark"]
        try:
            item.save()
        except IntegrityError:
            messages.error(request, "组成明细保存失败，请检查行号是否重复")
            return redirect("bom:bom_item_edit", pk=pk, item_pk=item_pk)
        _touch_bom(bom, request.user)
        record_audit_log_from_request(
            request,
            "bom_item_update",
            "bom",
            bom.id,
            bom.bom_no,
            before_snapshot=before_snapshot,
            after_snapshot=_bom_item_snapshot(item, operation_reason),
        )
        messages.success(request, "组成明细已更新")
        return redirect("bom:bom_detail", pk=pk)

    def _context(self, item):
        return {
            "page_title": f"编辑组成明细 {item.line_no}",
            "bom": item.bom,
            "item": item,
            "component_materials": _component_materials_for_bom(item.bom),
        }


class BomItemDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk, item_pk):
        require_erp_permission(request.user, PermissionCode.BOM_PROCESS, "缺少产品组成清单维护权限")
        verification_response = require_second_verify(request, "bom:bom_detail", pk)
        if verification_response:
            return verification_response
        reason, reason_response = require_post_reason(request, "bom:bom_detail", pk)
        if reason_response:
            return reason_response
        item = _get_bom_item(pk, item_pk)
        if item is None:
            messages.error(request, "组成明细不存在")
            return redirect("bom:bom_detail", pk=pk)
        bom = item.bom
        if not _can_edit_bom_items(bom):
            messages.error(request, "当前组成清单状态不允许删除明细")
            return redirect("bom:bom_detail", pk=pk)

        before_snapshot = _bom_item_snapshot(item, reason)
        item.delete()
        _touch_bom(bom, request.user)
        record_audit_log_from_request(
            request,
            "bom_item_delete",
            "bom",
            bom.id,
            bom.bom_no,
            before_snapshot=before_snapshot,
            after_snapshot={"deleted": True, "reason": reason},
        )
        messages.success(request, "组成明细已删除")
        return redirect("bom:bom_detail", pk=pk)


class BomCopyVersionView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.BOM_PROCESS, "缺少产品组成清单维护权限")
        new_bom_no = request.POST.get("new_bom_no", "").strip()
        new_bom_version = request.POST.get("new_bom_version", "").strip()
        operation_reason = optional_post_reason(request, default="复制产品组成清单新版本")
        if not new_bom_no or not new_bom_version:
            messages.error(request, "新清单编号和新版本必须填写")
            return redirect("bom:bom_detail", pk=pk)

        try:
            with transaction.atomic():
                source = Bom.objects.select_for_update().prefetch_related("items").get(pk=pk)
                before_snapshot = _bom_snapshot(source, operation_reason, include_items=True)
                if source.status == Bom.BomStatus.VOIDED:
                    messages.error(request, "已作废组成清单不能复制")
                    return redirect("bom:bom_detail", pk=pk)
                copied = Bom.objects.create(
                    bom_no=new_bom_no,
                    finished_material=source.finished_material,
                    bom_version=new_bom_version,
                    base_qty=source.base_qty,
                    status=Bom.BomStatus.DRAFT,
                    effective_date=source.effective_date,
                    expiry_date=source.expiry_date,
                    is_default=False,
                    created_by=request.user,
                    updated_by=request.user,
                    remark=source.remark,
                )
                BomItem.objects.bulk_create(
                    [
                        BomItem(
                            bom=copied,
                            line_no=item.line_no,
                            component_material_id=item.component_material_id,
                            usage_qty=item.usage_qty,
                            usage_unit=item.usage_unit,
                            loss_rate=item.loss_rate,
                            is_required=item.is_required,
                            remark=item.remark,
                        )
                        for item in source.items.all()
                    ]
                )
        except Bom.DoesNotExist:
            messages.error(request, "产品组成清单不存在")
            return redirect("bom:bom_list")
        except IntegrityError:
            messages.error(request, "复制失败，请检查新清单编号或新版本是否重复")
            return redirect("bom:bom_detail", pk=pk)

        record_audit_log_from_request(
            request,
            "bom_copy_version",
            "bom",
            copied.id,
            copied.bom_no,
            before_snapshot=before_snapshot,
            after_snapshot=_bom_snapshot(copied, operation_reason, include_items=True),
        )
        messages.success(request, "产品组成清单已复制为新版本")
        return redirect("bom:bom_detail", pk=copied.pk)


class BomEnableView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.BOM_PROCESS, "缺少产品组成清单维护权限")
        verification_response = require_second_verify(request, "bom:bom_detail", pk)
        if verification_response:
            return verification_response
        reason, reason_response = require_post_reason(request, "bom:bom_detail", pk)
        if reason_response:
            return reason_response
        before_snapshot = _bom_snapshot(Bom.objects.filter(pk=pk).select_related("finished_material").first(), reason)
        result = enable_bom(pk, request.user.id, make_default=True)
        if result.success:
            bom = Bom.objects.select_related("finished_material").get(pk=pk)
            record_audit_log_from_request(
                request,
                "bom_enable",
                "bom",
                bom.id,
                bom.bom_no,
                before_snapshot=before_snapshot,
                after_snapshot=_bom_snapshot(bom, reason),
            )
            messages.success(request, result.message)
        else:
            messages.error(request, result.message or result.error_code)
        return redirect("bom:bom_detail", pk=pk)


class BomDisableView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.BOM_PROCESS, "缺少产品组成清单维护权限")
        verification_response = require_second_verify(request, "bom:bom_detail", pk)
        if verification_response:
            return verification_response
        reason, reason_response = require_post_reason(request, "bom:bom_detail", pk)
        if reason_response:
            return reason_response
        before_snapshot = _bom_snapshot(Bom.objects.filter(pk=pk).select_related("finished_material").first(), reason)
        result = disable_bom(pk, request.user.id)
        if result.success:
            bom = Bom.objects.select_related("finished_material").get(pk=pk)
            record_audit_log_from_request(
                request,
                "bom_disable",
                "bom",
                bom.id,
                bom.bom_no,
                before_snapshot=before_snapshot,
                after_snapshot=_bom_snapshot(bom, reason),
            )
            messages.success(request, result.message)
        else:
            messages.error(request, result.message or result.error_code)
        return redirect("bom:bom_detail", pk=pk)


def _decimal_from_post(request, field_name: str, default=None):
    value = request.POST.get(field_name, "")
    if value == "" and default is not None:
        return default
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError):
        return None


def _int_from_post(request, field_name: str):
    try:
        return int(request.POST.get(field_name, ""))
    except (TypeError, ValueError):
        return None


def _can_edit_bom_items(bom: Bom) -> bool:
    return bom.status in EDITABLE_BOM_STATUSES


def _can_process_bom(user) -> bool:
    return user_has_permission(user, PermissionCode.BOM_PROCESS)


def _prepare_bom_header_form(form):
    set_form_labels(form)
    form.fields["finished_material"].queryset = Material.objects.filter(material_type=Material.MaterialType.FINISHED).order_by("material_code")
    form.fields["base_qty"].validators.append(MinValueValidator(Decimal("0.0001"), message="BOM 基准数量必须大于 0"))
    form.fields["base_qty"].error_messages["min_value"] = "BOM 基准数量必须大于 0"
    form.fields["base_qty"].help_text = "例如：1 表示下面明细用量按生产 1 个成品计算。"
    return form


def _component_materials_for_bom(bom: Bom):
    return (
        Material.objects.exclude(id=bom.finished_material_id)
        .filter(status=Material.MaterialStatus.ACTIVE)
        .order_by("material_type", "material_code")
    )


def _bom_item_snapshot(item: BomItem, reason: str = "") -> dict:
    return {
        "item_id": item.id,
        "bom_id": item.bom_id,
        "line_no": item.line_no,
        "component_material_id": item.component_material_id,
        "component_material_code": item.component_material.material_code,
        "usage_qty": str(item.usage_qty),
        "usage_unit": item.usage_unit,
        "loss_rate": str(item.loss_rate),
        "is_required": item.is_required,
        "remark": item.remark,
        "reason": reason,
    }


def _bom_snapshot(bom: Bom | None, reason: str = "", include_items: bool = False) -> dict:
    if bom is None:
        return {"missing": True, "reason": reason}

    snapshot = {
        "bom_id": bom.id,
        "bom_no": bom.bom_no,
        "finished_material_id": bom.finished_material_id,
        "finished_material_code": bom.finished_material.material_code if getattr(bom, "finished_material", None) else "",
        "bom_version": bom.bom_version,
        "base_qty": str(bom.base_qty),
        "status": bom.status,
        "effective_date": bom.effective_date.isoformat() if bom.effective_date else "",
        "expiry_date": bom.expiry_date.isoformat() if bom.expiry_date else "",
        "is_default": bom.is_default,
        "enabled_at": bom.enabled_at.isoformat() if bom.enabled_at else "",
        "disabled_at": bom.disabled_at.isoformat() if bom.disabled_at else "",
        "version": bom.version,
        "remark": bom.remark,
        "reason": reason,
    }
    if include_items:
        snapshot["items"] = [
            _bom_item_snapshot(item)
            for item in bom.items.select_related("component_material").order_by("line_no", "id")
        ]
    return snapshot


def _get_bom_item(pk, item_pk):
    return BomItem.objects.select_related("bom", "component_material", "bom__finished_material").filter(
        pk=item_pk,
        bom_id=pk,
    ).first()


def _parse_item_post(request, bom: Bom, exclude_item_id=None):
    line_no = _int_from_post(request, "line_no")
    component_material_id = request.POST.get("component_material")
    usage_qty = _decimal_from_post(request, "usage_qty")
    loss_rate = _decimal_from_post(request, "loss_rate", default=Decimal("0"))
    usage_unit = request.POST.get("usage_unit", "").strip()
    is_required = request.POST.get("is_required") == "on"
    remark = request.POST.get("remark", "").strip()

    if not line_no or not component_material_id or usage_qty is None or usage_qty <= 0 or not usage_unit:
        return {"success": False, "message": "行号、子件物料、用量和单位必须正确填写"}
    if loss_rate is None or loss_rate < 0 or loss_rate > 1:
        return {"success": False, "message": "损耗率必须在 0 到 1 之间"}
    duplicate_query = BomItem.objects.filter(bom=bom, line_no=line_no)
    if exclude_item_id:
        duplicate_query = duplicate_query.exclude(id=exclude_item_id)
    if duplicate_query.exists():
        return {"success": False, "message": "同一个组成清单下行号不能重复"}

    component = Material.objects.filter(
        id=component_material_id,
        status=Material.MaterialStatus.ACTIVE,
    ).first()
    if component is None or component.id == bom.finished_material_id:
        return {"success": False, "message": "子件物料必须是启用状态，且不能等于成品本身"}

    return {
        "success": True,
        "line_no": line_no,
        "component": component,
        "usage_qty": usage_qty,
        "usage_unit": usage_unit,
        "loss_rate": loss_rate,
        "is_required": is_required,
        "remark": remark,
    }


def _touch_bom(bom: Bom, user):
    bom.updated_by = user
    bom.version += 1
    bom.save(update_fields=["updated_by", "updated_at", "version"])
