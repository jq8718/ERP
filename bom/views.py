from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.validators import MinValueValidator
from django.db import IntegrityError, transaction
from django.forms import BaseInlineFormSet, inlineformset_factory
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
COMPONENT_MATERIAL_TYPES = (
    Material.MaterialType.RAW,
    Material.MaterialType.PART,
    Material.MaterialType.PACKAGING,
    Material.MaterialType.OTHER,
)


class BomForm(forms.ModelForm):
    finished_material_code = forms.CharField(max_length=80)
    finished_material_name = forms.CharField(max_length=200)
    finished_material_spec = forms.CharField(max_length=200, required=False)
    finished_material_base_unit = forms.CharField(max_length=32)
    finished_material_qty_precision = forms.IntegerField(min_value=0, initial=0)

    class Meta:
        model = Bom
        fields = [
            "bom_no",
            "bom_version",
            "base_qty",
            "effective_date",
            "expiry_date",
            "remark",
        ]
        widgets = {
            "effective_date": forms.DateInput(attrs={"type": "date"}),
            "expiry_date": forms.DateInput(attrs={"type": "date"}),
            "remark": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and self.instance.finished_material_id:
            material = self.instance.finished_material
            self.fields["finished_material_code"].initial = material.material_code
            self.fields["finished_material_name"].initial = material.material_name
            self.fields["finished_material_spec"].initial = material.spec
            self.fields["finished_material_base_unit"].initial = material.base_unit
            self.fields["finished_material_qty_precision"].initial = material.qty_precision

    def clean(self):
        cleaned = super().clean()
        effective_date = cleaned.get("effective_date")
        expiry_date = cleaned.get("expiry_date")
        if effective_date and expiry_date and expiry_date < effective_date:
            self.add_error("expiry_date", "失效日期不能早于生效日期")
        material_code = (cleaned.get("finished_material_code") or "").strip()
        existing_material = Material.objects.filter(material_code=material_code).first() if material_code else None
        if existing_material and existing_material.material_type != Material.MaterialType.FINISHED:
            self.add_error("finished_material_code", "该物料号已存在，但不是成品物料")
        self._finished_material = existing_material
        return cleaned

    def save(self, commit=True):
        bom = super().save(commit=False)
        bom.finished_material = self._save_finished_material()
        if commit:
            bom.save()
            self.save_m2m()
        return bom

    def _save_finished_material(self):
        material = getattr(self, "_finished_material", None)
        material_code = self.cleaned_data["finished_material_code"].strip()
        updates = {
            "material_name": self.cleaned_data["finished_material_name"].strip(),
            "material_type": Material.MaterialType.FINISHED,
            "spec": (self.cleaned_data.get("finished_material_spec") or "").strip(),
            "base_unit": self.cleaned_data["finished_material_base_unit"].strip(),
            "qty_precision": self.cleaned_data.get("finished_material_qty_precision") or 0,
            "status": Material.MaterialStatus.ACTIVE,
        }
        if material is None:
            material = Material(material_code=material_code, **updates)
            if self.user and self.user.is_authenticated:
                material.created_by = self.user
                material.updated_by = self.user
            material.save()
            return material

        for field_name, value in updates.items():
            setattr(material, field_name, value)
        if self.user and self.user.is_authenticated:
            material.updated_by = self.user
        material.save()
        return material


class BomItemForm(forms.ModelForm):
    class Meta:
        model = BomItem
        fields = ["line_no", "component_material", "usage_qty", "usage_unit", "loss_rate", "is_required", "remark"]
        widgets = {"remark": forms.TextInput()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        self.fields["component_material"].queryset = _component_material_queryset()
        self.fields["loss_rate"].initial = self.fields["loss_rate"].initial or 0
        self.fields["is_required"].initial = True

    def clean(self):
        cleaned = super().clean()
        usage_qty = cleaned.get("usage_qty")
        loss_rate = cleaned.get("loss_rate")
        if usage_qty is not None and usage_qty <= 0:
            self.add_error("usage_qty", "用量必须大于 0")
        if loss_rate is not None and (loss_rate < 0 or loss_rate > 1):
            self.add_error("loss_rate", "损耗率必须在 0 到 1 之间")
        return cleaned


class BaseBomItemFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        seen_line_numbers = set()
        for form in self.forms:
            if not form.cleaned_data or form.cleaned_data.get("DELETE") or not form.cleaned_data.get("component_material"):
                continue
            line_no = form.cleaned_data.get("line_no")
            if line_no in seen_line_numbers:
                form.add_error("line_no", "同一个组成清单下行号不能重复")
            seen_line_numbers.add(line_no)


BomItemFormSet = inlineformset_factory(
    Bom,
    BomItem,
    form=BomItemForm,
    formset=BaseBomItemFormSet,
    fields=["line_no", "component_material", "usage_qty", "usage_unit", "loss_rate", "is_required", "remark"],
    extra=3,
    can_delete=True,
)


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
    field_filters = (
        {"label": "清单编号", "param": "bom_no", "field": "bom_no", "placeholder": "BOM/清单编号"},
        {"label": "成品编码", "param": "material_code", "field": "finished_material__material_code", "placeholder": "成品编码"},
        {"label": "成品名称", "param": "material_name", "field": "finished_material__material_name", "placeholder": "成品名称"},
        {"label": "型号", "param": "material_spec", "field": "finished_material__spec", "placeholder": "规格型号"},
        {"label": "版本", "param": "bom_version", "field": "bom_version", "placeholder": "版本"},
    )

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
        queryset = list_view.apply_field_filters(queryset)
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
    form_class = BomForm

    def dispatch(self, request, *args, **kwargs):
        require_erp_permission(request.user, PermissionCode.BOM_PROCESS, "缺少产品组成清单维护权限")
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        return _prepare_bom_header_form(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "新建产品组成清单"
        if "item_formset" not in context:
            if self.request.POST and "items-TOTAL_FORMS" in self.request.POST:
                context["item_formset"] = BomItemFormSet(self.request.POST, instance=self.object)
            else:
                context["item_formset"] = BomItemFormSet(instance=self.object)
        return context

    def form_valid(self, form):
        if "items-TOTAL_FORMS" in self.request.POST:
            item_formset = BomItemFormSet(self.request.POST, instance=self.object)
            if not item_formset.is_valid():
                return self.render_to_response(self.get_context_data(form=form, item_formset=item_formset))
        else:
            item_formset = None

        operation_reason = optional_post_reason(self.request, default="页面创建产品组成清单")
        with transaction.atomic():
            self.object = form.save(commit=False)
            self.object.status = Bom.BomStatus.DRAFT
            self.object.is_default = False
            self.object.created_by = self.request.user
            self.object.updated_by = self.request.user
            self.object.save()
            if item_formset is not None:
                item_formset.instance = self.object
                item_formset.save()
            record_audit_log_from_request(
                self.request,
                "bom_create",
                "bom",
                self.object.id,
                self.object.bom_no,
                after_snapshot=_bom_snapshot(self.object, operation_reason, include_items=True),
            )
        messages.success(self.request, "产品组成清单已创建")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return f"/bom/{self.object.pk}/"


class BomUpdateView(LoginRequiredMixin, UpdateView):
    model = Bom
    template_name = "bom/bom_form.html"
    form_class = BomForm

    def dispatch(self, request, *args, **kwargs):
        require_erp_permission(request.user, PermissionCode.BOM_PROCESS, "缺少产品组成清单维护权限")
        self.object = self.get_object()
        if not _can_edit_bom_items(self.object):
            messages.error(request, "当前组成清单状态不允许直接编辑，请复制为新版本后修改")
            return redirect("bom:bom_detail", pk=self.object.pk)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

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
        context["component_materials"] = _component_materials_for_bom(self.object)
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
    form.fields["base_qty"].validators.append(MinValueValidator(Decimal("0.0001"), message="BOM 基准数量必须大于 0"))
    form.fields["base_qty"].error_messages["min_value"] = "BOM 基准数量必须大于 0"
    form.fields["base_qty"].help_text = "例如：1 表示下面明细用量按生产 1 个成品计算。"
    return form


def _component_material_queryset():
    return Material.objects.filter(
        material_type__in=COMPONENT_MATERIAL_TYPES,
        status=Material.MaterialStatus.ACTIVE,
    ).order_by("material_type", "material_code")


def _component_materials_for_bom(bom: Bom):
    return _component_material_queryset().exclude(id=bom.finished_material_id)


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
        material_type__in=COMPONENT_MATERIAL_TYPES,
        status=Material.MaterialStatus.ACTIVE,
    ).first()
    if component is None or component.id == bom.finished_material_id:
        return {"success": False, "message": "子件物料必须是启用状态的原料、配件或包装材料，且不能等于成品本身"}

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
