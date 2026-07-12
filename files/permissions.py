from __future__ import annotations

from django.apps import apps
from django.urls import NoReverseMatch, reverse
from django.db.models import Q

from accounts.permissions import PermissionCode, user_has_any_permission, user_has_permission
from system.display import code_label
from .models import Attachment


def filter_attachments_for_user(queryset, user):
    if _is_privileged(user):
        return queryset
    accessible_ids = []
    for attachment in queryset:
        if can_access_attachment_source(user, attachment):
            accessible_ids.append(attachment.id)
    return queryset.filter(id__in=accessible_ids)


def can_access_attachment(user, attachment: Attachment) -> bool:
    if not can_access_attachment_source(user, attachment):
        return False
    if attachment.is_sensitive and not user_has_permission(user, PermissionCode.ATTACHMENT_VIEW_SENSITIVE):
        return False
    return True


def can_access_attachment_source(user, attachment: Attachment) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    if _is_privileged(user):
        return True
    if attachment.uploaded_by_id == user.id:
        return True
    return can_access_source_doc(user, attachment.source_doc_type, attachment.source_doc_id)


def can_access_source_doc(user, source_doc_type: str, source_doc_id: int) -> bool:
    if not getattr(user, "is_authenticated", False) or not source_doc_type or not source_doc_id:
        return False
    if _is_privileged(user):
        return True

    checker = _SOURCE_CHECKERS.get(source_doc_type)
    if checker is None:
        return False
    return checker(user, source_doc_id)


def resolve_source_doc_no(source_doc_type: str, source_doc_id: int) -> str:
    resolver = _SOURCE_NO_RESOLVERS.get(source_doc_type)
    if resolver is None:
        return ""
    return resolver(source_doc_id) or ""


def resolve_source_doc_url(source_doc_type: str, source_doc_id: int) -> str:
    route_name = _SOURCE_DETAIL_ROUTES.get(source_doc_type)
    if route_name is None:
        return ""
    try:
        return reverse(route_name, kwargs={"pk": source_doc_id})
    except NoReverseMatch:
        return ""


def resolve_source_doc_id(source_doc_type: str, source_doc_no: str) -> int:
    if not source_doc_type or not source_doc_no:
        return 0
    resolver = _SOURCE_ID_RESOLVERS.get(source_doc_type)
    if resolver is None:
        return 0
    return resolver(source_doc_no.strip()) or 0


def _is_privileged(user) -> bool:
    return getattr(user, "is_superuser", False)


def _can_view_sales_scope(user) -> bool:
    return user_has_permission(user, PermissionCode.SALES_VIEW_ALL)


def _can_view_purchase(user) -> bool:
    return user_has_any_permission(user, (PermissionCode.PURCHASE_VIEW, PermissionCode.PURCHASE_PROCESS))


def _can_view_production(user) -> bool:
    return user_has_any_permission(user, (PermissionCode.PRODUCTION_VIEW, PermissionCode.PRODUCTION_PROCESS))


def _can_view_inventory(user) -> bool:
    return user_has_any_permission(user, (PermissionCode.INVENTORY_VIEW, PermissionCode.INVENTORY_PROCESS))


def _sales_order(user, source_doc_id: int) -> bool:
    from sales.models import SalesOrder

    queryset = SalesOrder.objects.filter(id=source_doc_id)
    if _can_view_sales_scope(user):
        return queryset.exists()
    return queryset.filter(Q(customer__sales_owner=user) | Q(created_by=user)).exists()


def _sales_order_no(source_doc_id: int) -> str:
    from sales.models import SalesOrder

    return SalesOrder.objects.filter(id=source_doc_id).values_list("sales_order_no", flat=True).first()


def _sales_shipment(user, source_doc_id: int) -> bool:
    from sales.models import SalesShipment

    queryset = SalesShipment.objects.filter(id=source_doc_id)
    if _can_view_sales_scope(user):
        return queryset.exists()
    return queryset.filter(Q(customer__sales_owner=user) | Q(sales_order__created_by=user) | Q(created_by=user)).exists()


def _sales_shipment_no(source_doc_id: int) -> str:
    from sales.models import SalesShipment

    return SalesShipment.objects.filter(id=source_doc_id).values_list("shipment_no", flat=True).first()


def _customer_return(user, source_doc_id: int) -> bool:
    from sales.models import CustomerReturn

    queryset = CustomerReturn.objects.filter(id=source_doc_id)
    if _can_view_sales_scope(user):
        return queryset.exists()
    return queryset.filter(Q(customer__sales_owner=user) | Q(sales_order__created_by=user)).exists()


def _customer_return_no(source_doc_id: int) -> str:
    from sales.models import CustomerReturn

    return CustomerReturn.objects.filter(id=source_doc_id).values_list("return_no", flat=True).first()


def _sample_loan(user, source_doc_id: int) -> bool:
    from sales.models import SampleLoan

    queryset = SampleLoan.objects.filter(id=source_doc_id)
    if _can_view_sales_scope(user):
        return queryset.exists()
    return queryset.filter(Q(customer__sales_owner=user) | Q(created_by=user)).exists()


def _sample_loan_no(source_doc_id: int) -> str:
    from sales.models import SampleLoan

    return SampleLoan.objects.filter(id=source_doc_id).values_list("sample_loan_no", flat=True).first()


def _sample_loan_return(user, source_doc_id: int) -> bool:
    from sales.models import SampleLoanReturn

    queryset = SampleLoanReturn.objects.filter(id=source_doc_id)
    if _can_view_sales_scope(user):
        return queryset.exists()
    return queryset.filter(Q(customer__sales_owner=user) | Q(sample_loan__created_by=user)).exists()


def _sample_loan_return_no(source_doc_id: int) -> str:
    from sales.models import SampleLoanReturn

    return SampleLoanReturn.objects.filter(id=source_doc_id).values_list("sample_return_no", flat=True).first()


def _purchase_request(user, source_doc_id: int) -> bool:
    from purchase.models import PurchaseRequest

    if not _can_view_purchase(user):
        return False
    return PurchaseRequest.objects.filter(id=source_doc_id).exists()


def _purchase_request_no(source_doc_id: int) -> str:
    from purchase.models import PurchaseRequest

    return PurchaseRequest.objects.filter(id=source_doc_id).values_list("purchase_request_no", flat=True).first()


def _purchase_order(user, source_doc_id: int) -> bool:
    from purchase.models import PurchaseOrder

    if not _can_view_purchase(user):
        return False
    return PurchaseOrder.objects.filter(id=source_doc_id).exists()


def _purchase_order_no(source_doc_id: int) -> str:
    from purchase.models import PurchaseOrder

    return PurchaseOrder.objects.filter(id=source_doc_id).values_list("purchase_order_no", flat=True).first()


def _purchase_receipt(user, source_doc_id: int) -> bool:
    from purchase.models import PurchaseReceipt

    if not _can_view_purchase(user):
        return False
    return PurchaseReceipt.objects.filter(id=source_doc_id).exists()


def _purchase_receipt_no(source_doc_id: int) -> str:
    from purchase.models import PurchaseReceipt

    return PurchaseReceipt.objects.filter(id=source_doc_id).values_list("purchase_receipt_no", flat=True).first()


def _supplier_return(user, source_doc_id: int) -> bool:
    from purchase.models import SupplierReturn

    if not _can_view_purchase(user):
        return False
    return SupplierReturn.objects.filter(id=source_doc_id).exists()


def _supplier_return_no(source_doc_id: int) -> str:
    from purchase.models import SupplierReturn

    return SupplierReturn.objects.filter(id=source_doc_id).values_list("supplier_return_no", flat=True).first()


def _production_order(user, source_doc_id: int) -> bool:
    from production.models import ProductionOrder

    if not _can_view_production(user):
        return False
    return ProductionOrder.objects.filter(id=source_doc_id).exists()


def _production_order_no(source_doc_id: int) -> str:
    from production.models import ProductionOrder

    return ProductionOrder.objects.filter(id=source_doc_id).values_list("production_order_no", flat=True).first()


def _production_material_requisition(user, source_doc_id: int) -> bool:
    from production.models import ProductionMaterialRequisition

    if not _can_view_production(user):
        return False
    return ProductionMaterialRequisition.objects.filter(id=source_doc_id).exists()


def _production_material_requisition_no(source_doc_id: int) -> str:
    from production.models import ProductionMaterialRequisition

    return ProductionMaterialRequisition.objects.filter(id=source_doc_id).values_list("requisition_no", flat=True).first()


def _production_receipt(user, source_doc_id: int) -> bool:
    from production.models import ProductionReceipt

    if not _can_view_production(user):
        return False
    return ProductionReceipt.objects.filter(id=source_doc_id).exists()


def _production_receipt_no(source_doc_id: int) -> str:
    from production.models import ProductionReceipt

    return ProductionReceipt.objects.filter(id=source_doc_id).values_list("production_receipt_no", flat=True).first()


def _location_transfer(user, source_doc_id: int) -> bool:
    from inventory.models import LocationTransfer

    if not _can_view_inventory(user):
        return False
    return LocationTransfer.objects.filter(id=source_doc_id).exists()


def _location_transfer_no(source_doc_id: int) -> str:
    from inventory.models import LocationTransfer

    return LocationTransfer.objects.filter(id=source_doc_id).values_list("transfer_no", flat=True).first()


def _stock_count(user, source_doc_id: int) -> bool:
    from inventory.models import StockCount

    if not _can_view_inventory(user):
        return False
    return StockCount.objects.filter(id=source_doc_id).exists()


def _stock_count_no(source_doc_id: int) -> str:
    from inventory.models import StockCount

    return StockCount.objects.filter(id=source_doc_id).values_list("stock_count_no", flat=True).first()


def _customer_receipt(user, source_doc_id: int) -> bool:
    from finance.models import CustomerReceipt

    if not user_has_permission(user, PermissionCode.FINANCE_VIEW_AMOUNT):
        return False
    return CustomerReceipt.objects.filter(id=source_doc_id).exists()


def _customer_receipt_no(source_doc_id: int) -> str:
    from finance.models import CustomerReceipt

    return CustomerReceipt.objects.filter(id=source_doc_id).values_list("receipt_no", flat=True).first()


def _customer_invoice(user, source_doc_id: int) -> bool:
    from finance.models import CustomerInvoice

    if not (
        user_has_permission(user, PermissionCode.FINANCE_VIEW_AMOUNT)
        or user_has_permission(user, PermissionCode.SALES_PROCESS)
    ):
        return False
    queryset = CustomerInvoice.objects.filter(id=source_doc_id)
    if user_has_permission(user, PermissionCode.FINANCE_VIEW_AMOUNT) or user_has_permission(user, PermissionCode.SALES_VIEW_ALL):
        return queryset.exists()
    return queryset.filter(
        Q(customer__sales_owner=user)
        | Q(customer__created_by=user)
        | Q(customer__sales_orders__created_by=user)
        | Q(created_by=user)
    ).exists()


def _customer_invoice_no(source_doc_id: int) -> str:
    from finance.models import CustomerInvoice

    return CustomerInvoice.objects.filter(id=source_doc_id).values_list("invoice_no", flat=True).first()


def _supplier_payment(user, source_doc_id: int) -> bool:
    from finance.models import SupplierPayment

    if not user_has_permission(user, PermissionCode.FINANCE_VIEW_AMOUNT):
        return False
    return SupplierPayment.objects.filter(id=source_doc_id).exists()


def _supplier_payment_no(source_doc_id: int) -> str:
    from finance.models import SupplierPayment

    return SupplierPayment.objects.filter(id=source_doc_id).values_list("payment_no", flat=True).first()


def _customer_credit_balance(user, source_doc_id: int) -> bool:
    from finance.models import CustomerCreditBalance

    if not user_has_permission(user, PermissionCode.FINANCE_VIEW_AMOUNT):
        return False
    return CustomerCreditBalance.objects.filter(id=source_doc_id).exists()


def _customer_credit_balance_no(source_doc_id: int) -> str:
    from finance.models import CustomerCreditBalance

    return CustomerCreditBalance.objects.filter(id=source_doc_id).values_list("source_doc_no", flat=True).first()


def _supplier_credit_balance(user, source_doc_id: int) -> bool:
    from finance.models import SupplierCreditBalance

    if not user_has_permission(user, PermissionCode.FINANCE_VIEW_AMOUNT):
        return False
    return SupplierCreditBalance.objects.filter(id=source_doc_id).exists()


def _supplier_credit_balance_no(source_doc_id: int) -> str:
    from finance.models import SupplierCreditBalance

    return SupplierCreditBalance.objects.filter(id=source_doc_id).values_list("source_doc_no", flat=True).first()


def _reconciliation(user, source_doc_id: int) -> bool:
    from finance.models import Reconciliation

    if not user_has_permission(user, PermissionCode.FINANCE_VIEW_AMOUNT):
        return False
    return Reconciliation.objects.filter(id=source_doc_id).exists()


def _reconciliation_no(source_doc_id: int) -> str:
    from finance.models import Reconciliation

    return Reconciliation.objects.filter(id=source_doc_id).values_list("reconciliation_no", flat=True).first()


def _expense_record(user, source_doc_id: int) -> bool:
    from finance.models import ExpenseRecord

    if not user_has_permission(user, PermissionCode.FINANCE_VIEW_AMOUNT):
        return False
    return ExpenseRecord.objects.filter(id=source_doc_id).exists()


def _expense_record_no(source_doc_id: int) -> str:
    from finance.models import ExpenseRecord

    return ExpenseRecord.objects.filter(id=source_doc_id).values_list("expense_no", flat=True).first()


def _approval(user, source_doc_id: int) -> bool:
    from approvals.models import Approval

    return Approval.objects.filter(id=source_doc_id).filter(Q(current_approver=user) | Q(submitted_by=user)).exists()


def _approval_no(source_doc_id: int) -> str:
    from approvals.models import Approval

    return Approval.objects.filter(id=source_doc_id).values_list("approval_no", flat=True).first()


_SOURCE_CHECKERS = {
    "sales_order": _sales_order,
    "sales_shipment": _sales_shipment,
    "customer_return": _customer_return,
    "sample_loan": _sample_loan,
    "sample_loan_return": _sample_loan_return,
    "purchase_request": _purchase_request,
    "purchase_order": _purchase_order,
    "purchase_receipt": _purchase_receipt,
    "supplier_return": _supplier_return,
    "production_order": _production_order,
    "production_material_requisition": _production_material_requisition,
    "production_receipt": _production_receipt,
    "location_transfer": _location_transfer,
    "stock_count": _stock_count,
    "customer_receipt": _customer_receipt,
    "customer_invoice": _customer_invoice,
    "supplier_payment": _supplier_payment,
    "customer_credit_balance": _customer_credit_balance,
    "supplier_credit_balance": _supplier_credit_balance,
    "reconciliation": _reconciliation,
    "expense_record": _expense_record,
    "approval": _approval,
}


_SOURCE_NO_RESOLVERS = {
    "sales_order": _sales_order_no,
    "sales_shipment": _sales_shipment_no,
    "customer_return": _customer_return_no,
    "sample_loan": _sample_loan_no,
    "sample_loan_return": _sample_loan_return_no,
    "purchase_request": _purchase_request_no,
    "purchase_order": _purchase_order_no,
    "purchase_receipt": _purchase_receipt_no,
    "supplier_return": _supplier_return_no,
    "production_order": _production_order_no,
    "production_material_requisition": _production_material_requisition_no,
    "production_receipt": _production_receipt_no,
    "location_transfer": _location_transfer_no,
    "stock_count": _stock_count_no,
    "customer_receipt": _customer_receipt_no,
    "customer_invoice": _customer_invoice_no,
    "supplier_payment": _supplier_payment_no,
    "customer_credit_balance": _customer_credit_balance_no,
    "supplier_credit_balance": _supplier_credit_balance_no,
    "reconciliation": _reconciliation_no,
    "expense_record": _expense_record_no,
    "approval": _approval_no,
}


def _doc_id(app_label: str, model_name: str, field_name: str, source_doc_no: str) -> int:
    model = apps.get_model(app_label, model_name)
    return model.objects.filter(**{field_name: source_doc_no}).values_list("id", flat=True).first() or 0


_SOURCE_ID_RESOLVERS = {
    "sales_order": lambda value: _doc_id("sales", "SalesOrder", "sales_order_no", value),
    "sales_shipment": lambda value: _doc_id("sales", "SalesShipment", "shipment_no", value),
    "customer_return": lambda value: _doc_id("sales", "CustomerReturn", "return_no", value),
    "sample_loan": lambda value: _doc_id("sales", "SampleLoan", "sample_loan_no", value),
    "sample_loan_return": lambda value: _doc_id("sales", "SampleLoanReturn", "sample_return_no", value),
    "purchase_request": lambda value: _doc_id("purchase", "PurchaseRequest", "purchase_request_no", value),
    "purchase_order": lambda value: _doc_id("purchase", "PurchaseOrder", "purchase_order_no", value),
    "purchase_receipt": lambda value: _doc_id("purchase", "PurchaseReceipt", "purchase_receipt_no", value),
    "supplier_return": lambda value: _doc_id("purchase", "SupplierReturn", "supplier_return_no", value),
    "production_order": lambda value: _doc_id("production", "ProductionOrder", "production_order_no", value),
    "production_material_requisition": lambda value: _doc_id("production", "ProductionMaterialRequisition", "requisition_no", value),
    "production_receipt": lambda value: _doc_id("production", "ProductionReceipt", "production_receipt_no", value),
    "location_transfer": lambda value: _doc_id("inventory", "LocationTransfer", "transfer_no", value),
    "stock_count": lambda value: _doc_id("inventory", "StockCount", "stock_count_no", value),
    "customer_receipt": lambda value: _doc_id("finance", "CustomerReceipt", "receipt_no", value),
    "customer_invoice": lambda value: _doc_id("finance", "CustomerInvoice", "invoice_no", value),
    "supplier_payment": lambda value: _doc_id("finance", "SupplierPayment", "payment_no", value),
    "customer_credit_balance": lambda value: _doc_id("finance", "CustomerCreditBalance", "source_doc_no", value),
    "supplier_credit_balance": lambda value: _doc_id("finance", "SupplierCreditBalance", "source_doc_no", value),
    "reconciliation": lambda value: _doc_id("finance", "Reconciliation", "reconciliation_no", value),
    "expense_record": lambda value: _doc_id("finance", "ExpenseRecord", "expense_no", value),
    "approval": lambda value: _doc_id("approvals", "Approval", "approval_no", value),
}


_SOURCE_DETAIL_ROUTES = {
    "sales_order": "sales:sales_order_detail",
    "sales_shipment": "sales:sales_shipment_detail",
    "customer_return": "sales:customer_return_detail",
    "sample_loan": "sales:sample_loan_detail",
    "sample_loan_return": "sales:sample_loan_return_detail",
    "purchase_request": "purchase:purchase_request_detail",
    "purchase_order": "purchase:purchase_order_detail",
    "purchase_receipt": "purchase:purchase_receipt_detail",
    "supplier_return": "purchase:supplier_return_detail",
    "production_order": "production:production_order_detail",
    "production_material_requisition": "production:material_requisition_detail",
    "production_receipt": "production:production_receipt_detail",
    "location_transfer": "inventory:location_transfer_detail",
    "stock_count": "inventory:stock_count_detail",
    "customer_receipt": "finance:customer_receipt_detail",
    "customer_invoice": "finance:customer_invoice_detail",
    "supplier_payment": "finance:supplier_payment_detail",
    "customer_credit_balance": "finance:customer_credit_balance_detail",
    "supplier_credit_balance": "finance:supplier_credit_balance_detail",
    "reconciliation": "finance:reconciliation_detail",
    "expense_record": "finance:expense_record_detail",
    "approval": "approvals:approval_detail",
}


def source_doc_type_choices_for_user(user) -> tuple[tuple[str, str], ...]:
    choices = []
    for source_doc_type in _SOURCE_CHECKERS:
        if _is_privileged(user) or _can_upload_source_type(user, source_doc_type):
            choices.append((source_doc_type, code_label(source_doc_type)))
    return tuple(choices)


def _can_upload_source_type(user, source_doc_type: str) -> bool:
    if source_doc_type in {"sales_order", "sales_shipment", "customer_return", "sample_loan", "sample_loan_return"}:
        return user_has_permission(user, PermissionCode.SALES_PROCESS) or user_has_permission(user, PermissionCode.SALES_VIEW_ALL)
    if source_doc_type in {"purchase_request", "purchase_order", "purchase_receipt", "supplier_return"}:
        return user_has_permission(user, PermissionCode.PURCHASE_PROCESS)
    if source_doc_type in {"production_order", "production_material_requisition", "production_receipt"}:
        return user_has_permission(user, PermissionCode.PRODUCTION_PROCESS)
    if source_doc_type in {"location_transfer", "stock_count"}:
        return user_has_permission(user, PermissionCode.INVENTORY_PROCESS)
    if source_doc_type in {
        "customer_receipt",
        "supplier_payment",
        "customer_credit_balance",
        "supplier_credit_balance",
        "reconciliation",
        "expense_record",
    }:
        return user_has_permission(user, PermissionCode.FINANCE_VIEW_AMOUNT)
    if source_doc_type == "customer_invoice":
        return user_has_permission(user, PermissionCode.FINANCE_VIEW_AMOUNT) or user_has_permission(user, PermissionCode.SALES_PROCESS)
    if source_doc_type == "approval":
        return True
    return False
