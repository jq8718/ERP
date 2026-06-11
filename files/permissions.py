from __future__ import annotations

from django.db.models import Q

from accounts.permissions import PermissionCode, user_has_permission
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


def _is_privileged(user) -> bool:
    return getattr(user, "is_superuser", False)


def _can_view_sales_scope(user) -> bool:
    return user_has_permission(user, PermissionCode.SALES_VIEW_ALL)


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

    if not user_has_permission(user, PermissionCode.PURCHASE_PROCESS):
        return False
    return PurchaseRequest.objects.filter(id=source_doc_id).exists()


def _purchase_request_no(source_doc_id: int) -> str:
    from purchase.models import PurchaseRequest

    return PurchaseRequest.objects.filter(id=source_doc_id).values_list("purchase_request_no", flat=True).first()


def _purchase_order(user, source_doc_id: int) -> bool:
    from purchase.models import PurchaseOrder

    if not user_has_permission(user, PermissionCode.PURCHASE_PROCESS):
        return False
    return PurchaseOrder.objects.filter(id=source_doc_id).exists()


def _purchase_order_no(source_doc_id: int) -> str:
    from purchase.models import PurchaseOrder

    return PurchaseOrder.objects.filter(id=source_doc_id).values_list("purchase_order_no", flat=True).first()


def _purchase_receipt(user, source_doc_id: int) -> bool:
    from purchase.models import PurchaseReceipt

    if not user_has_permission(user, PermissionCode.PURCHASE_PROCESS):
        return False
    return PurchaseReceipt.objects.filter(id=source_doc_id).exists()


def _purchase_receipt_no(source_doc_id: int) -> str:
    from purchase.models import PurchaseReceipt

    return PurchaseReceipt.objects.filter(id=source_doc_id).values_list("purchase_receipt_no", flat=True).first()


def _supplier_return(user, source_doc_id: int) -> bool:
    from purchase.models import SupplierReturn

    if not user_has_permission(user, PermissionCode.PURCHASE_PROCESS):
        return False
    return SupplierReturn.objects.filter(id=source_doc_id).exists()


def _supplier_return_no(source_doc_id: int) -> str:
    from purchase.models import SupplierReturn

    return SupplierReturn.objects.filter(id=source_doc_id).values_list("supplier_return_no", flat=True).first()


def _production_order(user, source_doc_id: int) -> bool:
    from production.models import ProductionOrder

    if not user_has_permission(user, PermissionCode.PRODUCTION_PROCESS):
        return False
    return ProductionOrder.objects.filter(id=source_doc_id).exists()


def _production_order_no(source_doc_id: int) -> str:
    from production.models import ProductionOrder

    return ProductionOrder.objects.filter(id=source_doc_id).values_list("production_order_no", flat=True).first()


def _production_material_requisition(user, source_doc_id: int) -> bool:
    from production.models import ProductionMaterialRequisition

    if not user_has_permission(user, PermissionCode.PRODUCTION_PROCESS):
        return False
    return ProductionMaterialRequisition.objects.filter(id=source_doc_id).exists()


def _production_material_requisition_no(source_doc_id: int) -> str:
    from production.models import ProductionMaterialRequisition

    return ProductionMaterialRequisition.objects.filter(id=source_doc_id).values_list("requisition_no", flat=True).first()


def _production_receipt(user, source_doc_id: int) -> bool:
    from production.models import ProductionReceipt

    if not user_has_permission(user, PermissionCode.PRODUCTION_PROCESS):
        return False
    return ProductionReceipt.objects.filter(id=source_doc_id).exists()


def _production_receipt_no(source_doc_id: int) -> str:
    from production.models import ProductionReceipt

    return ProductionReceipt.objects.filter(id=source_doc_id).values_list("production_receipt_no", flat=True).first()


def _location_transfer(user, source_doc_id: int) -> bool:
    from inventory.models import LocationTransfer

    if not user_has_permission(user, PermissionCode.INVENTORY_PROCESS):
        return False
    return LocationTransfer.objects.filter(id=source_doc_id).exists()


def _location_transfer_no(source_doc_id: int) -> str:
    from inventory.models import LocationTransfer

    return LocationTransfer.objects.filter(id=source_doc_id).values_list("transfer_no", flat=True).first()


def _stock_count(user, source_doc_id: int) -> bool:
    from inventory.models import StockCount

    if not user_has_permission(user, PermissionCode.INVENTORY_PROCESS):
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
    "supplier_payment": _supplier_payment,
    "customer_credit_balance": _customer_credit_balance,
    "supplier_credit_balance": _supplier_credit_balance,
    "reconciliation": _reconciliation,
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
    "supplier_payment": _supplier_payment_no,
    "customer_credit_balance": _customer_credit_balance_no,
    "supplier_credit_balance": _supplier_credit_balance_no,
    "reconciliation": _reconciliation_no,
    "approval": _approval_no,
}
