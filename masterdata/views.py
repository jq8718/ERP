import csv
from io import StringIO

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import redirect
from django.views import View
from django.views.generic import TemplateView
from django.views.generic import DetailView
from django.views.generic.edit import CreateView, UpdateView

from accounts.permissions import PermissionCode, can_view_amount, can_view_personal_info, require_any_erp_permission, require_erp_permission, user_has_permission
from files.services import csv_upload_validation_error, export_queryset_to_csv, uploaded_csv_text_file
from files.view_helpers import export_file_response
from system.display import set_form_labels
from system.services import record_audit_log_from_request
from system.view_helpers import ErpListView, optional_post_reason

from .import_services import (
    CUSTOMER_IMPORT_TEMPLATE_ROWS,
    CUSTOMER_ADDRESS_IMPORT_TEMPLATE_ROWS,
    CUSTOMER_PRODUCT_IMPORT_TEMPLATE_ROWS,
    MATERIAL_IMPORT_TEMPLATE_ROWS,
    MATERIAL_SUPPLIER_PRICE_IMPORT_TEMPLATE_ROWS,
    MATERIAL_UNIT_CONVERSION_IMPORT_TEMPLATE_ROWS,
    SUPPLIER_IMPORT_TEMPLATE_ROWS,
    import_customer_addresses_from_csv,
    import_customer_products_from_csv,
    import_customers_from_csv,
    import_materials_from_csv,
    import_material_supplier_prices_from_csv,
    import_material_unit_conversions_from_csv,
    import_suppliers_from_csv,
)
from .models import Customer, CustomerAddress, CustomerProduct, Material, MaterialSupplierPrice, MaterialUnitConversion, Supplier


class MaterialListView(ErpListView):
    model = Material
    page_title = "物料"
    view_permission_required = (
        PermissionCode.BOM_VIEW,
        PermissionCode.BOM_PROCESS,
        PermissionCode.PURCHASE_VIEW,
        PermissionCode.PURCHASE_PROCESS,
        PermissionCode.INVENTORY_VIEW,
        PermissionCode.INVENTORY_PROCESS,
        PermissionCode.PRODUCTION_VIEW,
        PermissionCode.PRODUCTION_PROCESS,
        PermissionCode.FINANCE_VIEW_AMOUNT,
    )
    permission_denied_message = "缺少物料查看权限"
    create_url_name = "masterdata:material_create"
    detail_url_name = "masterdata:material_detail"
    columns = (
        ("编码", "material_code"),
        ("名称", "material_name"),
        ("类型", "get_material_type_display"),
        ("单位", "base_unit"),
        ("状态", "get_status_display"),
    )
    ordering = ["material_code"]
    search_fields = ("material_code", "material_name", "spec")
    status_filter_field = "status"
    sortable_fields = {
        "material_code": "material_code",
        "material_name": "material_name",
        "material_type": "material_type",
        "base_unit": "base_unit",
        "get_status_display": "status",
    }
    page_actions = (
        ("导出CSV", "masterdata:material_export", ""),
        ("下载导入模板", "masterdata:material_import_template", ""),
        ("导入CSV", "masterdata:material_import", "primary"),
        ("下载单位换算模板", "masterdata:material_unit_conversion_import_template", ""),
        ("导入单位换算", "masterdata:material_unit_conversion_import", ""),
        ("下载供应商价格模板", "masterdata:material_supplier_price_import_template", ""),
        ("导入供应商价格", "masterdata:material_supplier_price_import", ""),
    )
    page_action_permissions = {
        "masterdata:material_import_template": PermissionCode.FINANCE_VIEW_AMOUNT,
        "masterdata:material_import": PermissionCode.FINANCE_VIEW_AMOUNT,
        "masterdata:material_supplier_price_import_template": PermissionCode.FINANCE_VIEW_AMOUNT,
        "masterdata:material_supplier_price_import": PermissionCode.FINANCE_VIEW_AMOUNT,
    }


class MasterdataCsvExportView(LoginRequiredMixin, View):
    module = ""
    list_view_class = None
    ordering = ()
    select_related = ()

    def dispatch(self, request, *args, **kwargs):
        required_permissions = getattr(self.list_view_class, "view_permission_required", ())
        if request.user.is_authenticated and required_permissions:
            require_any_erp_permission(request.user, required_permissions, "缺少基础资料查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        list_view = self.list_view_class()
        list_view.request = self.request
        queryset = self.list_view_class.model.objects.all()
        queryset = list_view.apply_search(queryset)
        queryset = list_view.apply_status_filter(queryset)
        queryset = list_view.apply_extra_filters(queryset)
        if self.select_related:
            queryset = queryset.select_related(*self.select_related)
        queryset = queryset.order_by(*self.get_ordering(list_view))
        return queryset

    def get_ordering(self, list_view):
        return list_view.current_ordering() or self.ordering

    def get_mask_fields(self):
        return ()

    def get_filter_json(self):
        list_view = self.list_view_class()
        list_view.request = self.request
        return {"ordering": ",".join(self.get_ordering(list_view)), "query": self.request.GET.dict()}

    def get(self, request):
        result = export_queryset_to_csv(
            self.module,
            self.get_queryset(),
            self.list_view_class.columns,
            request.user.id,
            filter_json=self.get_filter_json(),
            mask_fields=self.get_mask_fields(),
        )
        return export_file_response(result)


class MaterialExportView(MasterdataCsvExportView):
    module = "materials"
    list_view_class = MaterialListView
    ordering = ("material_code",)


class CsvImportTemplateView(LoginRequiredMixin, View):
    template_rows = ()
    filename = "import_template.csv"
    permission_required = ""
    permission_denied_message = "无权限执行此操作"

    def dispatch(self, request, *args, **kwargs):
        if self.permission_required:
            require_erp_permission(request.user, self.permission_required, self.permission_denied_message)
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        output = StringIO()
        writer = csv.writer(output)
        writer.writerows(self.template_rows)
        response = HttpResponse("\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{self.filename}"'
        return response


class CsvImportView(LoginRequiredMixin, TemplateView):
    template_name = "masterdata/csv_import.html"
    page_title = "导入CSV"
    list_url_name = ""
    template_url_name = ""
    import_service = None
    permission_required = ""
    permission_denied_message = "无权限执行此操作"

    def dispatch(self, request, *args, **kwargs):
        if self.permission_required:
            require_erp_permission(request.user, self.permission_required, self.permission_denied_message)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = self.page_title
        context["list_url_name"] = self.list_url_name
        context["template_url_name"] = self.template_url_name
        return context

    def post(self, request):
        upload = request.FILES.get("import_file")
        validation_error = csv_upload_validation_error(upload)
        if validation_error:
            messages.error(request, validation_error)
            return redirect(self.import_url_name)
        text_file = uploaded_csv_text_file(upload)
        result = self.import_service(text_file, request.user.id)
        if result.success:
            messages.success(request, f"{result.message}，成功 {result.data['success_count']} 行")
            return redirect(self.list_url_name)
        return self.render_to_response(
            self.get_context_data(
                errors=result.data.get("errors", []),
                import_job_id=result.data.get("import_job_id"),
            )
        )


class MaterialImportTemplateView(CsvImportTemplateView):
    template_rows = MATERIAL_IMPORT_TEMPLATE_ROWS
    filename = "material_import_template.csv"
    permission_required = PermissionCode.FINANCE_VIEW_AMOUNT
    permission_denied_message = "缺少财务金额查看权限"


class MaterialImportView(CsvImportView):
    page_title = "导入物料"
    list_url_name = "masterdata:material_list"
    template_url_name = "masterdata:material_import_template"
    import_url_name = "masterdata:material_import"
    import_service = staticmethod(import_materials_from_csv)
    permission_required = PermissionCode.FINANCE_VIEW_AMOUNT
    permission_denied_message = "缺少财务金额查看权限"


class MaterialUnitConversionImportTemplateView(CsvImportTemplateView):
    template_rows = MATERIAL_UNIT_CONVERSION_IMPORT_TEMPLATE_ROWS
    filename = "material_unit_conversion_import_template.csv"


class MaterialUnitConversionImportView(CsvImportView):
    page_title = "导入物料单位换算"
    list_url_name = "masterdata:material_list"
    template_url_name = "masterdata:material_unit_conversion_import_template"
    import_url_name = "masterdata:material_unit_conversion_import"
    import_service = staticmethod(import_material_unit_conversions_from_csv)


class MaterialSupplierPriceImportTemplateView(CsvImportTemplateView):
    template_rows = MATERIAL_SUPPLIER_PRICE_IMPORT_TEMPLATE_ROWS
    filename = "material_supplier_price_import_template.csv"
    permission_required = PermissionCode.FINANCE_VIEW_AMOUNT
    permission_denied_message = "缺少财务金额查看权限"


class MaterialSupplierPriceImportView(CsvImportView):
    page_title = "导入物料供应商价格"
    list_url_name = "masterdata:material_list"
    template_url_name = "masterdata:material_supplier_price_import_template"
    import_url_name = "masterdata:material_supplier_price_import"
    import_service = staticmethod(import_material_supplier_prices_from_csv)
    permission_required = PermissionCode.FINANCE_VIEW_AMOUNT
    permission_denied_message = "缺少财务金额查看权限"


class MaterialCreateView(LoginRequiredMixin, CreateView):
    model = Material
    template_name = "masterdata/material_form.html"
    fields = [
        "material_code",
        "material_name",
        "material_type",
        "spec",
        "base_unit",
        "qty_precision",
        "min_stock_qty",
        "latest_purchase_price",
        "status",
        "remark",
    ]

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        set_form_labels(form)
        if not can_view_amount(self.request.user):
            form.fields.pop("latest_purchase_price", None)
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "新建物料"
        return context

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        form.instance.updated_by = self.request.user
        messages.success(self.request, "物料已创建")
        return super().form_valid(form)

    def get_success_url(self):
        return f"/masterdata/materials/{self.object.pk}/"


class MaterialUpdateView(LoginRequiredMixin, UpdateView):
    model = Material
    template_name = "masterdata/material_form.html"
    fields = MaterialCreateView.fields

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        set_form_labels(form)
        if not can_view_amount(self.request.user):
            form.fields.pop("latest_purchase_price", None)
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"编辑物料 {self.object.material_code}"
        context["material"] = self.object
        context["is_edit"] = True
        return context

    def form_valid(self, form):
        before_snapshot = _material_snapshot(Material.objects.get(pk=self.object.pk))
        form.instance.updated_by = self.request.user
        form.instance.version += 1
        response = super().form_valid(form)
        record_audit_log_from_request(
            self.request,
            "material_update",
            "material",
            self.object.id,
            self.object.material_code,
            before_snapshot=before_snapshot,
            after_snapshot={
                **_material_snapshot(self.object),
                "operation_reason": optional_post_reason(self.request, default="页面编辑物料"),
            },
        )
        messages.success(self.request, "物料已更新")
        return response

    def get_success_url(self):
        return f"/masterdata/materials/{self.object.pk}/"


class MaterialDetailView(LoginRequiredMixin, DetailView):
    model = Material
    template_name = "masterdata/material_detail.html"
    context_object_name = "material"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, MaterialListView.view_permission_required, "缺少物料查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return super().get_queryset().prefetch_related("unit_conversions", "supplier_prices__supplier")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"物料 {self.object.material_code}"
        context["can_view_amount"] = can_view_amount(self.request.user)
        return context


class MaterialUnitConversionCreateView(LoginRequiredMixin, CreateView):
    model = MaterialUnitConversion
    template_name = "masterdata/material_unit_conversion_form.html"
    fields = ["source_unit", "target_unit", "ratio", "status"]

    def dispatch(self, request, *args, **kwargs):
        self.material = Material.objects.get(pk=kwargs["material_pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        set_form_labels(form)
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"新增单位换算 {self.material.material_code}"
        context["material"] = self.material
        return context

    def form_valid(self, form):
        form.instance.material = self.material
        form.instance.created_by = self.request.user
        form.instance.updated_by = self.request.user
        messages.success(self.request, "单位换算已新增")
        return super().form_valid(form)

    def get_success_url(self):
        return f"/masterdata/materials/{self.material.pk}/"


class MaterialUnitConversionUpdateView(LoginRequiredMixin, UpdateView):
    model = MaterialUnitConversion
    template_name = "masterdata/material_unit_conversion_form.html"
    fields = MaterialUnitConversionCreateView.fields

    def get_queryset(self):
        return super().get_queryset().select_related("material")

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        set_form_labels(form)
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"编辑单位换算 {self.object.material.material_code}"
        context["material"] = self.object.material
        context["is_edit"] = True
        return context

    def form_valid(self, form):
        before_snapshot = _material_unit_conversion_snapshot(
            MaterialUnitConversion.objects.select_related("material").get(pk=self.object.pk)
        )
        form.instance.updated_by = self.request.user
        form.instance.version += 1
        response = super().form_valid(form)
        record_audit_log_from_request(
            self.request,
            "material_unit_conversion_update",
            "material_unit_conversion",
            self.object.id,
            f"{self.object.material.material_code}:{self.object.source_unit}->{self.object.target_unit}",
            before_snapshot=before_snapshot,
            after_snapshot={
                **_material_unit_conversion_snapshot(self.object),
                "operation_reason": optional_post_reason(self.request, default="页面编辑单位换算"),
            },
        )
        messages.success(self.request, "单位换算已更新")
        return response

    def get_success_url(self):
        return f"/masterdata/materials/{self.object.material.pk}/"


class MaterialSupplierPriceCreateView(LoginRequiredMixin, CreateView):
    model = MaterialSupplierPrice
    template_name = "masterdata/material_supplier_price_form.html"
    fields = ["supplier", "purchase_price", "currency", "effective_from", "effective_to", "is_default", "status"]

    def dispatch(self, request, *args, **kwargs):
        if not can_view_amount(request.user):
            raise PermissionDenied("缺少财务金额查看权限")
        self.material = Material.objects.get(pk=kwargs["material_pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        set_form_labels(form)
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"新增供应商价格 {self.material.material_code}"
        context["material"] = self.material
        return context

    def form_valid(self, form):
        form.instance.material = self.material
        form.instance.created_by = self.request.user
        form.instance.updated_by = self.request.user
        if form.instance.is_default:
            MaterialSupplierPrice.objects.filter(material=self.material, status=MaterialSupplierPrice.PriceStatus.ACTIVE).update(is_default=False)
        messages.success(self.request, "供应商价格已新增")
        return super().form_valid(form)

    def get_success_url(self):
        return f"/masterdata/materials/{self.material.pk}/"


class MaterialSupplierPriceUpdateView(LoginRequiredMixin, UpdateView):
    model = MaterialSupplierPrice
    template_name = "masterdata/material_supplier_price_form.html"
    fields = MaterialSupplierPriceCreateView.fields

    def dispatch(self, request, *args, **kwargs):
        if not can_view_amount(request.user):
            raise PermissionDenied("缺少财务金额查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return super().get_queryset().select_related("material", "supplier")

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        set_form_labels(form)
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"编辑供应商价格 {self.object.material.material_code}"
        context["material"] = self.object.material
        context["is_edit"] = True
        return context

    def form_valid(self, form):
        before_snapshot = _material_supplier_price_snapshot(
            MaterialSupplierPrice.objects.select_related("material", "supplier").get(pk=self.object.pk)
        )
        form.instance.updated_by = self.request.user
        form.instance.version += 1
        if form.instance.is_default:
            MaterialSupplierPrice.objects.filter(
                material=form.instance.material,
                status=MaterialSupplierPrice.PriceStatus.ACTIVE,
            ).exclude(pk=form.instance.pk).update(is_default=False)
        response = super().form_valid(form)
        record_audit_log_from_request(
            self.request,
            "material_supplier_price_update",
            "material_supplier_price",
            self.object.id,
            f"{self.object.material.material_code}:{self.object.supplier.supplier_no}",
            before_snapshot=before_snapshot,
            after_snapshot={
                **_material_supplier_price_snapshot(self.object),
                "operation_reason": optional_post_reason(self.request, default="页面编辑供应商价格"),
            },
        )
        messages.success(self.request, "供应商价格已更新")
        return response

    def get_success_url(self):
        return f"/masterdata/materials/{self.object.material.pk}/"


class CustomerListView(ErpListView):
    model = Customer
    page_title = "客户"
    view_permission_required = (
        PermissionCode.SALES_VIEW,
        PermissionCode.SALES_PROCESS,
        PermissionCode.SALES_VIEW_ALL,
        PermissionCode.FINANCE_VIEW_AMOUNT,
        PermissionCode.FINANCE_PAYMENT_PROCESS,
    )
    permission_denied_message = "缺少客户查看权限"
    create_url_name = "masterdata:customer_create"
    detail_url_name = "masterdata:customer_detail"
    columns = (
        ("客户编号", "customer_no"),
        ("客户名称", "customer_name"),
        ("简称", "short_name"),
        ("状态", "get_status_display"),
    )
    ordering = ["customer_no"]
    search_fields = ("customer_no", "customer_name", "short_name")
    status_filter_field = "status"
    page_actions = (
        ("导出CSV", "masterdata:customer_export", ""),
        ("下载导入模板", "masterdata:customer_import_template", ""),
        ("导入CSV", "masterdata:customer_import", "primary"),
        ("下载地址模板", "masterdata:customer_address_import_template", ""),
        ("导入客户地址", "masterdata:customer_address_import", ""),
    )
    page_action_permissions = {
        "masterdata:customer_import_template": PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO,
        "masterdata:customer_import": PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO,
        "masterdata:customer_address_import_template": PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO,
        "masterdata:customer_address_import": PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO,
    }

    def get_queryset(self):
        return _filter_customer_queryset_for_user(super().get_queryset(), self.request.user).select_related("sales_owner")


class CustomerExportView(MasterdataCsvExportView):
    module = "customers"
    list_view_class = CustomerListView
    ordering = ("customer_no",)
    select_related = ("sales_owner",)

    def get_queryset(self):
        return _filter_customer_queryset_for_user(super().get_queryset(), self.request.user)


class CustomerImportTemplateView(CsvImportTemplateView):
    template_rows = CUSTOMER_IMPORT_TEMPLATE_ROWS
    filename = "customer_import_template.csv"
    permission_required = PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO
    permission_denied_message = "缺少个人信息查看权限"


class CustomerImportView(CsvImportView):
    page_title = "导入客户"
    list_url_name = "masterdata:customer_list"
    template_url_name = "masterdata:customer_import_template"
    import_url_name = "masterdata:customer_import"
    import_service = staticmethod(import_customers_from_csv)
    permission_required = PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO
    permission_denied_message = "缺少个人信息查看权限"


class CustomerAddressImportTemplateView(CsvImportTemplateView):
    template_rows = CUSTOMER_ADDRESS_IMPORT_TEMPLATE_ROWS
    filename = "customer_address_import_template.csv"
    permission_required = PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO
    permission_denied_message = "缺少个人信息查看权限"


class CustomerAddressImportView(CsvImportView):
    page_title = "导入客户地址"
    list_url_name = "masterdata:customer_list"
    template_url_name = "masterdata:customer_address_import_template"
    import_url_name = "masterdata:customer_address_import"
    import_service = staticmethod(import_customer_addresses_from_csv)
    permission_required = PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO
    permission_denied_message = "缺少个人信息查看权限"


class CustomerCreateView(LoginRequiredMixin, CreateView):
    model = Customer
    template_name = "masterdata/customer_form.html"
    fields = [
        "customer_no",
        "customer_name",
        "short_name",
        "sales_owner",
        "settlement_method",
        "contact_phone_encrypted",
        "status",
        "remark",
    ]

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        set_form_labels(form)
        if not can_view_personal_info(self.request.user):
            form.fields.pop("contact_phone_encrypted", None)
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "新建客户"
        return context

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        form.instance.updated_by = self.request.user
        messages.success(self.request, "客户已创建")
        return super().form_valid(form)

    def get_success_url(self):
        return f"/masterdata/customers/{self.object.pk}/"


class CustomerUpdateView(LoginRequiredMixin, UpdateView):
    model = Customer
    template_name = "masterdata/customer_form.html"
    fields = CustomerCreateView.fields

    def get_queryset(self):
        return _filter_customer_queryset_for_user(super().get_queryset(), self.request.user)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        set_form_labels(form)
        if not can_view_personal_info(self.request.user):
            form.fields.pop("contact_phone_encrypted", None)
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"编辑客户 {self.object.customer_name}"
        context["customer"] = self.object
        context["is_edit"] = True
        return context

    def form_valid(self, form):
        before_snapshot = _customer_snapshot(Customer.objects.get(pk=self.object.pk))
        form.instance.updated_by = self.request.user
        form.instance.version += 1
        response = super().form_valid(form)
        record_audit_log_from_request(
            self.request,
            "customer_update",
            "customer",
            self.object.id,
            self.object.customer_no,
            before_snapshot=before_snapshot,
            after_snapshot={
                **_customer_snapshot(self.object),
                "operation_reason": optional_post_reason(self.request, default="页面编辑客户"),
            },
        )
        messages.success(self.request, "客户已更新")
        return response

    def get_success_url(self):
        return f"/masterdata/customers/{self.object.pk}/"


class CustomerDetailView(LoginRequiredMixin, DetailView):
    model = Customer
    template_name = "masterdata/customer_detail.html"
    context_object_name = "customer"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, CustomerListView.view_permission_required, "缺少客户查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return (
            _filter_customer_queryset_for_user(super().get_queryset(), self.request.user)
            .select_related("sales_owner")
            .prefetch_related("products__finished_material", "addresses")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"客户 {self.object.customer_name}"
        context["can_view_amount"] = can_view_amount(self.request.user)
        context["can_view_personal_info"] = can_view_personal_info(self.request.user)
        return context


class CustomerProductCreateView(LoginRequiredMixin, CreateView):
    model = CustomerProduct
    template_name = "masterdata/customer_product_form.html"
    fields = [
        "customer_product_no",
        "customer_product_name",
        "finished_material",
        "default_sale_price",
        "label_requirements",
        "packaging_requirements",
        "status",
    ]

    def dispatch(self, request, *args, **kwargs):
        self.customer = _filter_customer_queryset_for_user(Customer.objects.all(), request.user).get(pk=kwargs["customer_pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        set_form_labels(form)
        if not can_view_amount(self.request.user):
            form.fields.pop("default_sale_price", None)
        form.fields["finished_material"].queryset = Material.objects.filter(
            material_type=Material.MaterialType.FINISHED,
            status=Material.MaterialStatus.ACTIVE,
        ).order_by("material_code")
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"新增客户产品 {self.customer.customer_name}"
        context["customer"] = self.customer
        return context

    def form_valid(self, form):
        form.instance.customer = self.customer
        form.instance.created_by = self.request.user
        form.instance.updated_by = self.request.user
        if not can_view_amount(self.request.user):
            form.instance.default_sale_price = None
        messages.success(self.request, "客户产品已新增")
        return super().form_valid(form)

    def get_success_url(self):
        return f"/masterdata/customer-products/{self.object.pk}/"


class CustomerProductDetailView(LoginRequiredMixin, DetailView):
    model = CustomerProduct
    template_name = "masterdata/customer_product_detail.html"
    context_object_name = "product"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, CustomerProductListView.view_permission_required, "缺少客户产品查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return _filter_customer_product_queryset_for_user(
            super().get_queryset(),
            self.request.user,
        ).select_related("customer", "finished_material", "created_by", "updated_by")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"客户产品 {self.object.customer_product_no}"
        context["can_view_amount"] = can_view_amount(self.request.user)
        return context


class CustomerProductUpdateView(LoginRequiredMixin, UpdateView):
    model = CustomerProduct
    template_name = "masterdata/customer_product_form.html"
    fields = CustomerProductCreateView.fields

    def get_queryset(self):
        return _filter_customer_product_queryset_for_user(
            super().get_queryset(),
            self.request.user,
        ).select_related("customer")

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        set_form_labels(form)
        if not can_view_amount(self.request.user):
            form.fields.pop("default_sale_price", None)
        form.fields["finished_material"].queryset = Material.objects.filter(
            material_type=Material.MaterialType.FINISHED,
            status=Material.MaterialStatus.ACTIVE,
        ).order_by("material_code")
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"编辑客户产品 {self.object.customer_product_no}"
        context["customer"] = self.object.customer
        context["product"] = self.object
        context["is_edit"] = True
        return context

    def form_valid(self, form):
        before_snapshot = _customer_product_snapshot(CustomerProduct.objects.select_related("customer").get(pk=self.object.pk))
        form.instance.updated_by = self.request.user
        form.instance.version += 1
        response = super().form_valid(form)
        record_audit_log_from_request(
            self.request,
            "customer_product_update",
            "customer_product",
            self.object.id,
            self.object.customer_product_no,
            before_snapshot=before_snapshot,
            after_snapshot={
                **_customer_product_snapshot(self.object),
                "operation_reason": optional_post_reason(self.request, default="页面编辑客户产品"),
            },
        )
        messages.success(self.request, "客户产品已更新")
        return response

    def get_success_url(self):
        return f"/masterdata/customer-products/{self.object.pk}/"


class CustomerAddressCreateView(LoginRequiredMixin, CreateView):
    model = CustomerAddress
    template_name = "masterdata/customer_address_form.html"
    fields = ["address_type", "receiver_name", "receiver_phone_encrypted", "address_encrypted", "is_default", "status"]

    def dispatch(self, request, *args, **kwargs):
        if not can_view_personal_info(request.user):
            raise PermissionDenied("缺少个人信息查看权限")
        self.customer = _filter_customer_queryset_for_user(Customer.objects.all(), request.user).get(pk=kwargs["customer_pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        set_form_labels(form)
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"新增客户地址 {self.customer.customer_name}"
        context["customer"] = self.customer
        return context

    def form_valid(self, form):
        form.instance.customer = self.customer
        form.instance.created_by = self.request.user
        form.instance.updated_by = self.request.user
        if form.instance.is_default:
            CustomerAddress.objects.filter(customer=self.customer, address_type=form.instance.address_type).update(is_default=False)
        messages.success(self.request, "客户地址已新增")
        return super().form_valid(form)

    def get_success_url(self):
        return f"/masterdata/customers/{self.customer.pk}/"


class CustomerAddressUpdateView(LoginRequiredMixin, UpdateView):
    model = CustomerAddress
    template_name = "masterdata/customer_address_form.html"
    fields = CustomerAddressCreateView.fields

    def dispatch(self, request, *args, **kwargs):
        if not can_view_personal_info(request.user):
            raise PermissionDenied("缺少个人信息查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return CustomerAddress.objects.filter(
            customer__in=_filter_customer_queryset_for_user(Customer.objects.all(), self.request.user)
        ).select_related("customer")

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        set_form_labels(form)
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"编辑客户地址 {self.object.customer.customer_name}"
        context["customer"] = self.object.customer
        context["address"] = self.object
        context["is_edit"] = True
        return context

    def form_valid(self, form):
        before_snapshot = _customer_address_snapshot(CustomerAddress.objects.select_related("customer").get(pk=self.object.pk))
        form.instance.updated_by = self.request.user
        form.instance.version += 1
        if form.instance.is_default:
            CustomerAddress.objects.filter(
                customer=form.instance.customer,
                address_type=form.instance.address_type,
            ).exclude(pk=form.instance.pk).update(is_default=False)
        response = super().form_valid(form)
        record_audit_log_from_request(
            self.request,
            "customer_address_update",
            "customer_address",
            self.object.id,
            self.object.customer.customer_no,
            before_snapshot=before_snapshot,
            after_snapshot={
                **_customer_address_snapshot(self.object),
                "operation_reason": optional_post_reason(self.request, default="页面编辑客户地址"),
            },
        )
        messages.success(self.request, "客户地址已更新")
        return response

    def get_success_url(self):
        return f"/masterdata/customers/{self.object.customer.pk}/"


class CustomerProductListView(ErpListView):
    model = CustomerProduct
    page_title = "客户产品"
    view_permission_required = (
        PermissionCode.SALES_VIEW,
        PermissionCode.SALES_PROCESS,
        PermissionCode.SALES_VIEW_ALL,
        PermissionCode.FINANCE_VIEW_AMOUNT,
    )
    permission_denied_message = "缺少客户产品查看权限"
    detail_url_name = "masterdata:customer_product_detail"
    columns = (
        ("客户", "customer.customer_name"),
        ("客户产品编号", "customer_product_no"),
        ("客户产品名称", "customer_product_name"),
        ("关联成品", "finished_material.material_code"),
        ("状态", "get_status_display"),
    )
    ordering = ["customer_id", "customer_product_no"]
    search_fields = (
        "customer__customer_name",
        "customer_product_no",
        "customer_product_name",
        "finished_material__material_code",
        "finished_material__material_name",
    )
    status_filter_field = "status"
    page_actions = (
        ("导出CSV", "masterdata:customer_product_export", ""),
        ("下载导入模板", "masterdata:customer_product_import_template", ""),
        ("导入CSV", "masterdata:customer_product_import", "primary"),
    )
    page_action_permissions = {
        "masterdata:customer_product_import_template": PermissionCode.FINANCE_VIEW_AMOUNT,
        "masterdata:customer_product_import": PermissionCode.FINANCE_VIEW_AMOUNT,
    }

    def get_queryset(self):
        return _filter_customer_product_queryset_for_user(super().get_queryset(), self.request.user).select_related("customer", "finished_material")


class CustomerProductExportView(MasterdataCsvExportView):
    module = "customer_products"
    list_view_class = CustomerProductListView
    ordering = ("customer_id", "customer_product_no")
    select_related = ("customer", "finished_material")

    def get_queryset(self):
        return _filter_customer_product_queryset_for_user(super().get_queryset(), self.request.user)


class CustomerProductImportTemplateView(CsvImportTemplateView):
    template_rows = CUSTOMER_PRODUCT_IMPORT_TEMPLATE_ROWS
    filename = "customer_product_import_template.csv"
    permission_required = PermissionCode.FINANCE_VIEW_AMOUNT
    permission_denied_message = "缺少财务金额查看权限"


class CustomerProductImportView(CsvImportView):
    page_title = "导入客户产品"
    list_url_name = "masterdata:customer_product_list"
    template_url_name = "masterdata:customer_product_import_template"
    import_url_name = "masterdata:customer_product_import"
    import_service = staticmethod(import_customer_products_from_csv)
    permission_required = PermissionCode.FINANCE_VIEW_AMOUNT
    permission_denied_message = "缺少财务金额查看权限"


class SupplierListView(ErpListView):
    model = Supplier
    page_title = "供应商"
    view_permission_required = (
        PermissionCode.PURCHASE_VIEW,
        PermissionCode.PURCHASE_PROCESS,
        PermissionCode.FINANCE_VIEW_AMOUNT,
        PermissionCode.FINANCE_PAYMENT_PROCESS,
    )
    permission_denied_message = "缺少供应商查看权限"
    create_url_name = "masterdata:supplier_create"
    detail_url_name = "masterdata:supplier_detail"
    columns = (
        ("供应商编号", "supplier_no"),
        ("供应商名称", "supplier_name"),
        ("联系人", "contact_name"),
        ("状态", "get_status_display"),
    )
    sensitive_columns = ("contact_name",)
    ordering = ["supplier_no"]
    search_fields = ("supplier_no", "supplier_name", "contact_name", "supplier_type")
    status_filter_field = "status"
    page_actions = (
        ("导出CSV", "masterdata:supplier_export", ""),
        ("下载导入模板", "masterdata:supplier_import_template", ""),
        ("导入CSV", "masterdata:supplier_import", "primary"),
    )
    page_action_permissions = {
        "masterdata:supplier_import_template": PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO,
        "masterdata:supplier_import": PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO,
    }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["mask_sensitive_columns"] = not can_view_personal_info(self.request.user)
        return context


class SupplierExportView(MasterdataCsvExportView):
    module = "suppliers"
    list_view_class = SupplierListView
    ordering = ("supplier_no",)

    def get_mask_fields(self):
        if can_view_personal_info(self.request.user):
            return ()
        return SupplierListView.sensitive_columns


class SupplierImportTemplateView(CsvImportTemplateView):
    template_rows = SUPPLIER_IMPORT_TEMPLATE_ROWS
    filename = "supplier_import_template.csv"
    permission_required = PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO
    permission_denied_message = "缺少个人信息查看权限"


class SupplierImportView(CsvImportView):
    page_title = "导入供应商"
    list_url_name = "masterdata:supplier_list"
    template_url_name = "masterdata:supplier_import_template"
    import_url_name = "masterdata:supplier_import"
    import_service = staticmethod(import_suppliers_from_csv)
    permission_required = PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO
    permission_denied_message = "缺少个人信息查看权限"


class SupplierCreateView(LoginRequiredMixin, CreateView):
    model = Supplier
    template_name = "masterdata/supplier_form.html"
    fields = [
        "supplier_no",
        "supplier_name",
        "contact_name",
        "contact_phone_encrypted",
        "supplier_type",
        "payment_method",
        "status",
        "remark",
    ]

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        set_form_labels(form)
        if not can_view_personal_info(self.request.user):
            form.fields.pop("contact_name", None)
            form.fields.pop("contact_phone_encrypted", None)
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "新建供应商"
        return context

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        form.instance.updated_by = self.request.user
        messages.success(self.request, "供应商已创建")
        return super().form_valid(form)

    def get_success_url(self):
        return f"/masterdata/suppliers/{self.object.pk}/"


class SupplierUpdateView(LoginRequiredMixin, UpdateView):
    model = Supplier
    template_name = "masterdata/supplier_form.html"
    fields = SupplierCreateView.fields

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        set_form_labels(form)
        if not can_view_personal_info(self.request.user):
            form.fields.pop("contact_name", None)
            form.fields.pop("contact_phone_encrypted", None)
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"编辑供应商 {self.object.supplier_name}"
        context["supplier"] = self.object
        context["is_edit"] = True
        return context

    def form_valid(self, form):
        before_snapshot = _supplier_snapshot(Supplier.objects.get(pk=self.object.pk))
        form.instance.updated_by = self.request.user
        form.instance.version += 1
        response = super().form_valid(form)
        record_audit_log_from_request(
            self.request,
            "supplier_update",
            "supplier",
            self.object.id,
            self.object.supplier_no,
            before_snapshot=before_snapshot,
            after_snapshot={
                **_supplier_snapshot(self.object),
                "operation_reason": optional_post_reason(self.request, default="页面编辑供应商"),
            },
        )
        messages.success(self.request, "供应商已更新")
        return response

    def get_success_url(self):
        return f"/masterdata/suppliers/{self.object.pk}/"


class SupplierDetailView(LoginRequiredMixin, DetailView):
    model = Supplier
    template_name = "masterdata/supplier_detail.html"
    context_object_name = "supplier"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, SupplierListView.view_permission_required, "缺少供应商查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return super().get_queryset().prefetch_related("material_prices__material")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"供应商 {self.object.supplier_name}"
        context["can_view_amount"] = can_view_amount(self.request.user)
        context["can_view_personal_info"] = can_view_personal_info(self.request.user)
        return context


def _can_view_all_sales(user) -> bool:
    return user_has_permission(user, PermissionCode.SALES_VIEW_ALL)


def _filter_customer_queryset_for_user(queryset, user):
    if _can_view_all_sales(user):
        return queryset
    return queryset.filter(Q(sales_owner=user) | Q(created_by=user)).distinct()


def _filter_customer_product_queryset_for_user(queryset, user):
    if _can_view_all_sales(user):
        return queryset
    return queryset.filter(Q(customer__sales_owner=user) | Q(customer__created_by=user)).distinct()


def _material_snapshot(material: Material) -> dict:
    material.refresh_from_db()
    return {
        "material_code": material.material_code,
        "material_name": material.material_name,
        "material_type": material.material_type,
        "spec": material.spec,
        "base_unit": material.base_unit,
        "qty_precision": material.qty_precision,
        "min_stock_qty": str(material.min_stock_qty),
        "latest_purchase_price": str(material.latest_purchase_price) if material.latest_purchase_price is not None else None,
        "status": material.status,
        "remark": material.remark,
        "version": material.version,
    }


def _material_unit_conversion_snapshot(conversion: MaterialUnitConversion) -> dict:
    conversion.refresh_from_db()
    return {
        "material_id": conversion.material_id,
        "material_code": conversion.material.material_code,
        "source_unit": conversion.source_unit,
        "target_unit": conversion.target_unit,
        "ratio": str(conversion.ratio),
        "status": conversion.status,
        "version": conversion.version,
    }


def _material_supplier_price_snapshot(price: MaterialSupplierPrice) -> dict:
    price.refresh_from_db()
    return {
        "material_id": price.material_id,
        "material_code": price.material.material_code,
        "supplier_id": price.supplier_id,
        "supplier_no": price.supplier.supplier_no,
        "purchase_price": str(price.purchase_price),
        "currency": price.currency,
        "effective_from": price.effective_from.isoformat() if price.effective_from else None,
        "effective_to": price.effective_to.isoformat() if price.effective_to else None,
        "is_default": price.is_default,
        "status": price.status,
        "version": price.version,
    }


def _customer_snapshot(customer: Customer) -> dict:
    customer.refresh_from_db()
    return {
        "customer_no": customer.customer_no,
        "customer_name": customer.customer_name,
        "short_name": customer.short_name,
        "sales_owner_id": customer.sales_owner_id,
        "settlement_method": customer.settlement_method,
        "contact_phone_encrypted": customer.contact_phone_encrypted,
        "status": customer.status,
        "remark": customer.remark,
        "version": customer.version,
    }


def _customer_product_snapshot(product: CustomerProduct) -> dict:
    product.refresh_from_db()
    return {
        "customer_id": product.customer_id,
        "customer_no": product.customer.customer_no,
        "customer_product_no": product.customer_product_no,
        "customer_product_name": product.customer_product_name,
        "finished_material_id": product.finished_material_id,
        "default_sale_price": str(product.default_sale_price) if product.default_sale_price is not None else None,
        "label_requirements": product.label_requirements,
        "packaging_requirements": product.packaging_requirements,
        "status": product.status,
        "version": product.version,
    }


def _customer_address_snapshot(address: CustomerAddress) -> dict:
    address.refresh_from_db()
    return {
        "customer_id": address.customer_id,
        "customer_no": address.customer.customer_no,
        "address_type": address.address_type,
        "receiver_name": address.receiver_name,
        "receiver_phone_encrypted": address.receiver_phone_encrypted,
        "address_encrypted": address.address_encrypted,
        "is_default": address.is_default,
        "status": address.status,
        "version": address.version,
    }


def _supplier_snapshot(supplier: Supplier) -> dict:
    supplier.refresh_from_db()
    return {
        "supplier_no": supplier.supplier_no,
        "supplier_name": supplier.supplier_name,
        "contact_name": supplier.contact_name,
        "contact_phone_encrypted": supplier.contact_phone_encrypted,
        "supplier_type": supplier.supplier_type,
        "payment_method": supplier.payment_method,
        "status": supplier.status,
        "remark": supplier.remark,
        "version": supplier.version,
    }
