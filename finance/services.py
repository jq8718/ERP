from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from purchase.models import PurchaseReceipt
from sales.models import SalesOrder
from system.models import PendingEvent
from system.services import ServiceResult, enqueue_pending_event, next_document_no

from .models import (
    CustomerCreditBalance,
    CustomerCreditBalanceTransaction,
    CustomerReceipt,
    CustomerReceiptAllocation,
    CustomerReceiptReversal,
    OpeningPayable,
    OpeningReceivable,
    Reconciliation,
    SupplierCreditBalance,
    SupplierCreditBalanceTransaction,
    SupplierPayment,
    SupplierPaymentAllocation,
    SupplierPaymentReversal,
)


ZERO = Decimal("0.00")


def confirm_customer_receipt(
    receipt_id: int,
    allocations: list[dict],
    operator_id: int,
    idempotency_key: str,
) -> ServiceResult:
    if not idempotency_key:
        return ServiceResult(False, "STATE_INVALID_TRANSITION", "确认收款需要幂等键")
    try:
        with transaction.atomic():
            event_key = f"payment_confirmed:customer:{idempotency_key}"
            existing_event = PendingEvent.objects.filter(idempotency_key=event_key).first()
            if existing_event and existing_event.payload.get("confirmed"):
                return ServiceResult(False, "STATE_ALREADY_PROCESSED", "该收款确认请求已处理")
            receipt = CustomerReceipt.objects.select_for_update().get(id=receipt_id)
            if receipt.status != CustomerReceipt.Status.PENDING_APPROVAL:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "当前收款单状态不能确认")

            allocation_rows = _normalize_customer_allocations(allocations)
            allocation_total = sum((row["allocated_amount"] for row in allocation_rows), ZERO)
            if allocation_total > receipt.receipt_amount:
                return ServiceResult(False, "PAYMENT_ALLOCATION_OVER", "核销金额不能超过收款金额")

            created_allocations = []
            order_rows = sorted((row for row in allocation_rows if row["target_type"] == "sales_order"), key=lambda row: row["sales_order_id"])
            reconciliation_rows = sorted(
                (row for row in allocation_rows if row["target_type"] == "reconciliation"),
                key=lambda row: row["reconciliation_id"],
            )
            opening_rows = sorted(
                (row for row in allocation_rows if row["target_type"] == "opening_receivable"),
                key=lambda row: row["opening_receivable_id"],
            )
            for row in reconciliation_rows:
                reconciliation = Reconciliation.objects.select_for_update().get(
                    id=row["reconciliation_id"],
                    party_type=Reconciliation.PartyType.CUSTOMER,
                    customer=receipt.customer,
                    status=Reconciliation.Status.CONFIRMED,
                )
                available_amount = customer_reconciliation_available_allocation_amount(reconciliation)
                if row["allocated_amount"] > available_amount:
                    return ServiceResult(False, "PAYMENT_ALLOCATION_OVER", "核销金额超过对账单可核销余额")
                allocation = CustomerReceiptAllocation.objects.create(
                    customer_receipt=receipt,
                    reconciliation=reconciliation,
                    allocated_amount=row["allocated_amount"],
                    allocation_type=CustomerReceiptAllocation.AllocationType.RECONCILIATION,
                    created_by_id=operator_id,
                )
                created_allocations.append(
                    {
                        "allocation_id": allocation.id,
                        "target_type": "reconciliation",
                        "reconciliation_id": reconciliation.id,
                    }
                )

            for row in order_rows:
                order = SalesOrder.objects.select_for_update().get(id=row["sales_order_id"], customer=receipt.customer)
                available_amount = customer_order_available_allocation_amount(order)
                if row["allocated_amount"] > available_amount:
                    return ServiceResult(False, "PAYMENT_ALLOCATION_OVER", "核销金额超过订单可核销余额")
                allocation = CustomerReceiptAllocation.objects.create(
                    customer_receipt=receipt,
                    sales_order=order,
                    allocated_amount=row["allocated_amount"],
                    allocation_type=CustomerReceiptAllocation.AllocationType.SALES_ORDER,
                    created_by_id=operator_id,
                )
                created_allocations.append(
                    {
                        "allocation_id": allocation.id,
                        "target_type": "sales_order",
                        "sales_order_id": order.id,
                    }
                )

            for row in opening_rows:
                opening = OpeningReceivable.objects.select_for_update().get(
                    id=row["opening_receivable_id"],
                    customer=receipt.customer,
                )
                available_amount = customer_opening_receivable_available_allocation_amount(opening)
                if row["allocated_amount"] > available_amount:
                    return ServiceResult(False, "PAYMENT_ALLOCATION_OVER", "核销金额超过期初应收可核销余额")
                allocation = CustomerReceiptAllocation.objects.create(
                    customer_receipt=receipt,
                    opening_receivable=opening,
                    allocated_amount=row["allocated_amount"],
                    allocation_type=CustomerReceiptAllocation.AllocationType.OPENING_RECEIVABLE,
                    created_by_id=operator_id,
                )
                _refresh_opening_receivable(opening)
                created_allocations.append(
                    {
                        "allocation_id": allocation.id,
                        "target_type": "opening_receivable",
                        "opening_receivable_id": opening.id,
                    }
                )

            receipt.unallocated_amount = receipt.receipt_amount - allocation_total
            receipt.status = CustomerReceipt.Status.CONFIRMED
            receipt.confirmed_at = timezone.now()
            receipt.confirmed_by_id = operator_id
            receipt.save(update_fields=["unallocated_amount", "status", "confirmed_at", "confirmed_by"])
            if receipt.unallocated_amount > ZERO:
                _create_customer_credit_balance(receipt, receipt.unallocated_amount, operator_id)
            _mark_event_confirmed(event_key, "payment_confirmed", {"receipt_id": receipt.id, "party": "customer"})
    except (CustomerReceipt.DoesNotExist, SalesOrder.DoesNotExist, Reconciliation.DoesNotExist, OpeningReceivable.DoesNotExist):
        return ServiceResult(False, "DOC_NOT_FOUND", "收款单、销售订单、对账单或期初应收不存在")

    return ServiceResult(True, message="客户收款已确认", data={"receipt_id": receipt.id, "allocations": created_allocations})


def reverse_customer_receipt(
    receipt_id: int,
    reversal_amount: Decimal,
    reason: str,
    operator_id: int,
    idempotency_key: str,
) -> ServiceResult:
    if not reason:
        return ServiceResult(False, "STATE_INVALID_TRANSITION", "红冲必须填写原因")
    if not idempotency_key:
        return ServiceResult(False, "STATE_INVALID_TRANSITION", "红冲需要幂等键")
    try:
        with transaction.atomic():
            existing_reversal = CustomerReceiptReversal.objects.filter(source_receipt_id=receipt_id, idempotency_key=idempotency_key).first()
            if existing_reversal:
                return ServiceResult(
                    False,
                    "STATE_ALREADY_PROCESSED",
                    "该收款红冲请求已处理",
                    data={"reversal_id": existing_reversal.id},
                )
            receipt = CustomerReceipt.objects.select_for_update().get(id=receipt_id)
            if receipt.status not in [CustomerReceipt.Status.CONFIRMED, CustomerReceipt.Status.PART_REVERSED]:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "当前收款单状态不能红冲")
            reversible_amount = receipt.receipt_amount - _customer_reversed_amount(receipt)
            if reversal_amount <= ZERO or reversal_amount > reversible_amount:
                return ServiceResult(False, "PAYMENT_REVERSAL_OVER", "红冲金额超过可红冲金额")

            reversal = CustomerReceiptReversal.objects.create(
                reversal_no=next_document_no("RCR"),
                source_receipt=receipt,
                reversal_amount=reversal_amount,
                reason=reason,
                status=CustomerReceiptReversal.Status.CONFIRMED,
                idempotency_key=idempotency_key,
                created_by_id=operator_id,
                confirmed_at=timezone.now(),
                confirmed_by_id=operator_id,
            )
            _create_customer_reverse_allocations(receipt, reversal, reversal_amount, operator_id)
            reversed_total = _customer_reversed_amount(receipt)
            receipt.status = CustomerReceipt.Status.REVERSED if reversed_total >= receipt.receipt_amount else CustomerReceipt.Status.PART_REVERSED
            receipt.save(update_fields=["status"])
            for opening_id in receipt.allocations.filter(opening_receivable__isnull=False).values_list("opening_receivable_id", flat=True).distinct():
                _refresh_opening_receivable(OpeningReceivable.objects.select_for_update().get(id=opening_id))
            _mark_event_confirmed(
                f"payment_reversed:customer:{idempotency_key}",
                "payment_reversed",
                {"receipt_id": receipt.id, "reversal_id": reversal.id, "party": "customer"},
            )
    except CustomerReceipt.DoesNotExist:
        return ServiceResult(False, "DOC_NOT_FOUND", "收款单不存在")

    return ServiceResult(True, message="客户收款红冲已确认", data={"reversal_id": reversal.id, "receipt_status": receipt.status})


def apply_customer_credit_balance(
    credit_balance_id: int,
    action_type: str,
    amount: Decimal,
    operator_id: int,
    target_sales_order_id: int | None = None,
    reason: str = "",
    attachment_ids: list[int] | None = None,
    idempotency_key: str = "",
) -> ServiceResult:
    if not idempotency_key:
        return ServiceResult(False, "STATE_INVALID_TRANSITION", "处理客户余额需要幂等键")
    try:
        with transaction.atomic():
            balance = CustomerCreditBalance.objects.select_for_update().get(id=credit_balance_id)
            existing_transaction = CustomerCreditBalanceTransaction.objects.filter(credit_balance=balance, idempotency_key=idempotency_key).first()
            if existing_transaction:
                return ServiceResult(
                    False,
                    "STATE_ALREADY_PROCESSED",
                    "该客户余额处理请求已处理",
                    data={"transaction_id": existing_transaction.id},
                )
            if balance.status in [CustomerCreditBalance.Status.USED_UP, CustomerCreditBalance.Status.CLOSED]:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "余额状态不允许处理")
            if amount <= ZERO or amount > balance.remaining_amount:
                return ServiceResult(False, "PAYMENT_CREDIT_BALANCE_NOT_ENOUGH", "待处理余额不足")

            target_doc_type = ""
            target_doc_id = None
            target_doc_no = ""
            if action_type == CustomerCreditBalanceTransaction.ActionType.ALLOCATE_TO_ORDER:
                order = SalesOrder.objects.select_for_update().get(id=target_sales_order_id, customer=balance.customer)
                if amount > customer_order_available_allocation_amount(order):
                    return ServiceResult(False, "PAYMENT_ALLOCATION_OVER", "核销金额超过订单可核销余额")
                if balance.source_doc_type != "customer_receipt":
                    return ServiceResult(False, "STATE_INVALID_TRANSITION", "只有来源为客户收款的余额可以核销订单")
                source_receipt = CustomerReceipt.objects.select_for_update().get(id=balance.source_doc_id, customer=balance.customer)
                if amount > source_receipt.unallocated_amount:
                    return ServiceResult(False, "PAYMENT_CREDIT_BALANCE_NOT_ENOUGH", "来源收款单未分配金额不足")
                target_doc_type = "sales_order"
                target_doc_id = order.id
                target_doc_no = order.sales_order_no
                CustomerReceiptAllocation.objects.create(
                    customer_receipt=source_receipt,
                    sales_order=order,
                    allocated_amount=amount,
                    allocation_type=CustomerReceiptAllocation.AllocationType.CREDIT_BALANCE,
                    created_by_id=operator_id,
                    remark=reason,
                )
                source_receipt.unallocated_amount -= amount
                source_receipt.save(update_fields=["unallocated_amount"])
            elif action_type not in CustomerCreditBalanceTransaction.ActionType.values:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "不支持的余额处理动作")

            transaction_row = CustomerCreditBalanceTransaction.objects.create(
                transaction_no=next_document_no("CBT"),
                credit_balance=balance,
                action_type=action_type,
                amount=amount,
                target_doc_type=target_doc_type,
                target_doc_id=target_doc_id,
                target_doc_no=target_doc_no,
                idempotency_key=idempotency_key,
                reason=reason,
                created_by_id=operator_id,
            )
            balance.used_amount += amount
            balance.remaining_amount -= amount
            balance.status = _customer_balance_status_after_action(balance, action_type)
            balance.process_reason = reason
            balance.save(update_fields=["used_amount", "remaining_amount", "status", "process_reason"])
    except (CustomerCreditBalance.DoesNotExist, CustomerReceipt.DoesNotExist, SalesOrder.DoesNotExist):
        return ServiceResult(False, "DOC_NOT_FOUND", "客户余额或目标订单不存在")

    return ServiceResult(True, message="客户余额已处理", data={"transaction_id": transaction_row.id, "balance_status": balance.status})


def confirm_supplier_payment(
    payment_id: int,
    allocations: list[dict],
    operator_id: int,
    idempotency_key: str,
) -> ServiceResult:
    if not idempotency_key:
        return ServiceResult(False, "STATE_INVALID_TRANSITION", "确认付款需要幂等键")
    try:
        with transaction.atomic():
            event_key = f"payment_confirmed:supplier:{idempotency_key}"
            existing_event = PendingEvent.objects.filter(idempotency_key=event_key).first()
            if existing_event and existing_event.payload.get("confirmed"):
                return ServiceResult(False, "STATE_ALREADY_PROCESSED", "该付款确认请求已处理")
            payment = SupplierPayment.objects.select_for_update().get(id=payment_id)
            if payment.status != SupplierPayment.Status.PENDING_APPROVAL:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "当前付款单状态不能确认")

            allocation_rows = _normalize_supplier_allocations(allocations)
            allocation_total = sum((row["allocated_amount"] for row in allocation_rows), ZERO)
            if allocation_total > payment.payment_amount:
                return ServiceResult(False, "PAYMENT_ALLOCATION_OVER", "核销金额不能超过付款金额")

            created_allocations = []
            receipt_rows = sorted(
                (row for row in allocation_rows if row["target_type"] == "purchase_receipt"),
                key=lambda row: row["purchase_receipt_id"],
            )
            reconciliation_rows = sorted(
                (row for row in allocation_rows if row["target_type"] == "reconciliation"),
                key=lambda row: row["reconciliation_id"],
            )
            opening_rows = sorted(
                (row for row in allocation_rows if row["target_type"] == "opening_payable"),
                key=lambda row: row["opening_payable_id"],
            )
            for row in reconciliation_rows:
                reconciliation = Reconciliation.objects.select_for_update().get(
                    id=row["reconciliation_id"],
                    party_type=Reconciliation.PartyType.SUPPLIER,
                    supplier=payment.supplier,
                    status=Reconciliation.Status.CONFIRMED,
                )
                available_amount = supplier_reconciliation_available_allocation_amount(reconciliation)
                if row["allocated_amount"] > available_amount:
                    return ServiceResult(False, "PAYMENT_ALLOCATION_OVER", "核销金额超过对账单可核销余额")
                allocation = SupplierPaymentAllocation.objects.create(
                    supplier_payment=payment,
                    reconciliation=reconciliation,
                    allocated_amount=row["allocated_amount"],
                    allocation_type=SupplierPaymentAllocation.AllocationType.RECONCILIATION,
                    created_by_id=operator_id,
                )
                created_allocations.append(
                    {
                        "allocation_id": allocation.id,
                        "target_type": "reconciliation",
                        "reconciliation_id": reconciliation.id,
                    }
                )

            for row in receipt_rows:
                receipt = PurchaseReceipt.objects.select_for_update().get(id=row["purchase_receipt_id"], supplier=payment.supplier)
                available_amount = supplier_receipt_available_allocation_amount(receipt)
                if row["allocated_amount"] > available_amount:
                    return ServiceResult(False, "PAYMENT_ALLOCATION_OVER", "核销金额超过进货单可核销余额")
                allocation = SupplierPaymentAllocation.objects.create(
                    supplier_payment=payment,
                    purchase_receipt=receipt,
                    allocated_amount=row["allocated_amount"],
                    allocation_type=SupplierPaymentAllocation.AllocationType.PURCHASE_RECEIPT,
                    created_by_id=operator_id,
                )
                created_allocations.append(
                    {
                        "allocation_id": allocation.id,
                        "target_type": "purchase_receipt",
                        "purchase_receipt_id": receipt.id,
                    }
                )

            for row in opening_rows:
                opening = OpeningPayable.objects.select_for_update().get(
                    id=row["opening_payable_id"],
                    supplier=payment.supplier,
                )
                available_amount = supplier_opening_payable_available_allocation_amount(opening)
                if row["allocated_amount"] > available_amount:
                    return ServiceResult(False, "PAYMENT_ALLOCATION_OVER", "核销金额超过期初应付可核销余额")
                allocation = SupplierPaymentAllocation.objects.create(
                    supplier_payment=payment,
                    opening_payable=opening,
                    allocated_amount=row["allocated_amount"],
                    allocation_type=SupplierPaymentAllocation.AllocationType.OPENING_PAYABLE,
                    created_by_id=operator_id,
                )
                _refresh_opening_payable(opening)
                created_allocations.append(
                    {
                        "allocation_id": allocation.id,
                        "target_type": "opening_payable",
                        "opening_payable_id": opening.id,
                    }
                )

            payment.unallocated_amount = payment.payment_amount - allocation_total
            payment.status = SupplierPayment.Status.CONFIRMED
            payment.confirmed_at = timezone.now()
            payment.confirmed_by_id = operator_id
            payment.save(update_fields=["unallocated_amount", "status", "confirmed_at", "confirmed_by"])
            if payment.unallocated_amount > ZERO:
                _create_supplier_credit_balance(payment, payment.unallocated_amount, operator_id)
            _mark_event_confirmed(event_key, "payment_confirmed", {"payment_id": payment.id, "party": "supplier"})
    except (SupplierPayment.DoesNotExist, PurchaseReceipt.DoesNotExist, Reconciliation.DoesNotExist, OpeningPayable.DoesNotExist):
        return ServiceResult(False, "DOC_NOT_FOUND", "付款单、进货单、对账单或期初应付不存在")

    return ServiceResult(True, message="供应商付款已确认", data={"payment_id": payment.id, "allocations": created_allocations})


def reverse_supplier_payment(
    payment_id: int,
    reversal_amount: Decimal,
    reason: str,
    operator_id: int,
    idempotency_key: str,
) -> ServiceResult:
    if not reason:
        return ServiceResult(False, "STATE_INVALID_TRANSITION", "红冲必须填写原因")
    if not idempotency_key:
        return ServiceResult(False, "STATE_INVALID_TRANSITION", "红冲需要幂等键")
    try:
        with transaction.atomic():
            existing_reversal = SupplierPaymentReversal.objects.filter(source_payment_id=payment_id, idempotency_key=idempotency_key).first()
            if existing_reversal:
                return ServiceResult(
                    False,
                    "STATE_ALREADY_PROCESSED",
                    "该付款红冲请求已处理",
                    data={"reversal_id": existing_reversal.id},
                )
            payment = SupplierPayment.objects.select_for_update().get(id=payment_id)
            if payment.status not in [SupplierPayment.Status.CONFIRMED, SupplierPayment.Status.PART_REVERSED]:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "当前付款单状态不能红冲")
            reversible_amount = payment.payment_amount - _supplier_reversed_amount(payment)
            if reversal_amount <= ZERO or reversal_amount > reversible_amount:
                return ServiceResult(False, "PAYMENT_REVERSAL_OVER", "红冲金额超过可红冲金额")
            reversal = SupplierPaymentReversal.objects.create(
                reversal_no=next_document_no("RPY"),
                source_payment=payment,
                reversal_amount=reversal_amount,
                reason=reason,
                status=SupplierPaymentReversal.Status.CONFIRMED,
                idempotency_key=idempotency_key,
                created_by_id=operator_id,
                confirmed_at=timezone.now(),
                confirmed_by_id=operator_id,
            )
            _create_supplier_reverse_allocations(payment, reversal, reversal_amount, operator_id)
            reversed_total = _supplier_reversed_amount(payment)
            payment.status = SupplierPayment.Status.REVERSED if reversed_total >= payment.payment_amount else SupplierPayment.Status.PART_REVERSED
            payment.save(update_fields=["status"])
            for opening_id in payment.allocations.filter(opening_payable__isnull=False).values_list("opening_payable_id", flat=True).distinct():
                _refresh_opening_payable(OpeningPayable.objects.select_for_update().get(id=opening_id))
            _mark_event_confirmed(
                f"payment_reversed:supplier:{idempotency_key}",
                "payment_reversed",
                {"payment_id": payment.id, "reversal_id": reversal.id, "party": "supplier"},
            )
    except SupplierPayment.DoesNotExist:
        return ServiceResult(False, "DOC_NOT_FOUND", "付款单不存在")

    return ServiceResult(True, message="供应商付款红冲已确认", data={"reversal_id": reversal.id, "payment_status": payment.status})


def apply_supplier_credit_balance(
    credit_balance_id: int,
    action_type: str,
    amount: Decimal,
    operator_id: int,
    target_purchase_receipt_id: int | None = None,
    reason: str = "",
    attachment_ids: list[int] | None = None,
    idempotency_key: str = "",
) -> ServiceResult:
    if not idempotency_key:
        return ServiceResult(False, "STATE_INVALID_TRANSITION", "处理供应商余额需要幂等键")
    try:
        with transaction.atomic():
            balance = SupplierCreditBalance.objects.select_for_update().get(id=credit_balance_id)
            existing_transaction = SupplierCreditBalanceTransaction.objects.filter(credit_balance=balance, idempotency_key=idempotency_key).first()
            if existing_transaction:
                return ServiceResult(
                    False,
                    "STATE_ALREADY_PROCESSED",
                    "该供应商余额处理请求已处理",
                    data={"transaction_id": existing_transaction.id},
                )
            if balance.status in [SupplierCreditBalance.Status.USED_UP, SupplierCreditBalance.Status.CLOSED]:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "余额状态不允许处理")
            if amount <= ZERO or amount > balance.remaining_amount:
                return ServiceResult(False, "PAYMENT_CREDIT_BALANCE_NOT_ENOUGH", "待处理余额不足")

            target_doc_type = ""
            target_doc_id = None
            target_doc_no = ""
            if action_type == SupplierCreditBalanceTransaction.ActionType.ALLOCATE_TO_RECEIPT:
                receipt = PurchaseReceipt.objects.select_for_update().get(id=target_purchase_receipt_id, supplier=balance.supplier)
                if amount > supplier_receipt_available_allocation_amount(receipt):
                    return ServiceResult(False, "PAYMENT_ALLOCATION_OVER", "核销金额超过进货单可核销余额")
                if balance.source_doc_type != "supplier_payment":
                    return ServiceResult(False, "STATE_INVALID_TRANSITION", "只有来源为供应商付款的余额可以核销进货单")
                source_payment = SupplierPayment.objects.select_for_update().get(id=balance.source_doc_id, supplier=balance.supplier)
                if amount > source_payment.unallocated_amount:
                    return ServiceResult(False, "PAYMENT_CREDIT_BALANCE_NOT_ENOUGH", "来源付款单未分配金额不足")
                target_doc_type = "purchase_receipt"
                target_doc_id = receipt.id
                target_doc_no = receipt.purchase_receipt_no
                SupplierPaymentAllocation.objects.create(
                    supplier_payment=source_payment,
                    purchase_receipt=receipt,
                    allocated_amount=amount,
                    allocation_type=SupplierPaymentAllocation.AllocationType.CREDIT_BALANCE,
                    created_by_id=operator_id,
                    remark=reason,
                )
                source_payment.unallocated_amount -= amount
                source_payment.save(update_fields=["unallocated_amount"])
            elif action_type not in SupplierCreditBalanceTransaction.ActionType.values:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "不支持的余额处理动作")

            transaction_row = SupplierCreditBalanceTransaction.objects.create(
                transaction_no=next_document_no("SBT"),
                credit_balance=balance,
                action_type=action_type,
                amount=amount,
                target_doc_type=target_doc_type,
                target_doc_id=target_doc_id,
                target_doc_no=target_doc_no,
                idempotency_key=idempotency_key,
                reason=reason,
                created_by_id=operator_id,
            )
            balance.used_amount += amount
            balance.remaining_amount -= amount
            balance.status = _supplier_balance_status_after_action(balance, action_type)
            balance.process_reason = reason
            balance.save(update_fields=["used_amount", "remaining_amount", "status", "process_reason"])
    except (SupplierCreditBalance.DoesNotExist, SupplierPayment.DoesNotExist, PurchaseReceipt.DoesNotExist):
        return ServiceResult(False, "DOC_NOT_FOUND", "供应商余额或目标进货单不存在")

    return ServiceResult(True, message="供应商余额已处理", data={"transaction_id": transaction_row.id, "balance_status": balance.status})


def _normalize_customer_allocations(allocations: list[dict]) -> list[dict]:
    rows = []
    for row in allocations:
        amount = Decimal(str(row.get("allocated_amount", "0")))
        if amount <= ZERO:
            continue
        if row.get("sales_order_id"):
            rows.append(
                {
                    "target_type": "sales_order",
                    "sales_order_id": int(row["sales_order_id"]),
                    "allocated_amount": amount,
                }
            )
        elif row.get("reconciliation_id"):
            rows.append(
                {
                    "target_type": "reconciliation",
                    "reconciliation_id": int(row["reconciliation_id"]),
                    "allocated_amount": amount,
                }
            )
        elif row.get("opening_receivable_id"):
            rows.append(
                {
                    "target_type": "opening_receivable",
                    "opening_receivable_id": int(row["opening_receivable_id"]),
                    "allocated_amount": amount,
                }
            )
    return rows


def _normalize_supplier_allocations(allocations: list[dict]) -> list[dict]:
    rows = []
    for row in allocations:
        amount = Decimal(str(row.get("allocated_amount", "0")))
        if amount <= ZERO:
            continue
        if row.get("purchase_receipt_id"):
            rows.append(
                {
                    "target_type": "purchase_receipt",
                    "purchase_receipt_id": int(row["purchase_receipt_id"]),
                    "allocated_amount": amount,
                }
            )
        elif row.get("reconciliation_id"):
            rows.append(
                {
                    "target_type": "reconciliation",
                    "reconciliation_id": int(row["reconciliation_id"]),
                    "allocated_amount": amount,
                }
            )
        elif row.get("opening_payable_id"):
            rows.append(
                {
                    "target_type": "opening_payable",
                    "opening_payable_id": int(row["opening_payable_id"]),
                    "allocated_amount": amount,
                }
            )
    return rows


def customer_order_available_allocation_amount(order: SalesOrder) -> Decimal:
    available_amount = _sales_order_receivable_amount(order) - _allocated_customer_amount(order)
    if available_amount <= ZERO:
        return ZERO
    return available_amount


def customer_reconciliation_available_allocation_amount(reconciliation: Reconciliation) -> Decimal:
    available_amount = reconciliation.total_amount - _allocated_customer_reconciliation_amount(reconciliation)
    if available_amount <= ZERO:
        return ZERO
    return available_amount


def customer_opening_receivable_available_allocation_amount(opening: OpeningReceivable) -> Decimal:
    if opening.status == OpeningReceivable.Status.VOIDED:
        return ZERO
    available_amount = opening.opening_amount - _allocated_customer_opening_receivable_amount(opening)
    if available_amount <= ZERO:
        return ZERO
    return available_amount


def supplier_receipt_available_allocation_amount(receipt: PurchaseReceipt) -> Decimal:
    available_amount = _purchase_receipt_payable_amount(receipt) - _allocated_supplier_amount(receipt)
    if available_amount <= ZERO:
        return ZERO
    return available_amount


def supplier_reconciliation_available_allocation_amount(reconciliation: Reconciliation) -> Decimal:
    available_amount = reconciliation.total_amount - _allocated_supplier_reconciliation_amount(reconciliation)
    if available_amount <= ZERO:
        return ZERO
    return available_amount


def supplier_opening_payable_available_allocation_amount(opening: OpeningPayable) -> Decimal:
    if opening.status == OpeningPayable.Status.VOIDED:
        return ZERO
    available_amount = opening.opening_amount - _allocated_supplier_opening_payable_amount(opening)
    if available_amount <= ZERO:
        return ZERO
    return available_amount


def _sales_order_receivable_amount(order: SalesOrder) -> Decimal:
    return order.items.aggregate(total=Sum("line_amount"))["total"] or ZERO


def _purchase_receipt_payable_amount(receipt: PurchaseReceipt) -> Decimal:
    total = ZERO
    for item in receipt.items.all():
        total += item.accepted_qty * item.unit_price
    return total.quantize(Decimal("0.01"))


def _allocated_customer_amount(order: SalesOrder) -> Decimal:
    return CustomerReceiptAllocation.objects.filter(sales_order=order).aggregate(total=Sum("allocated_amount"))["total"] or ZERO


def _allocated_customer_reconciliation_amount(reconciliation: Reconciliation) -> Decimal:
    return CustomerReceiptAllocation.objects.filter(reconciliation=reconciliation).aggregate(total=Sum("allocated_amount"))["total"] or ZERO


def _allocated_customer_opening_receivable_amount(opening: OpeningReceivable) -> Decimal:
    return CustomerReceiptAllocation.objects.filter(opening_receivable=opening).aggregate(total=Sum("allocated_amount"))["total"] or ZERO


def _allocated_supplier_amount(receipt: PurchaseReceipt) -> Decimal:
    return SupplierPaymentAllocation.objects.filter(purchase_receipt=receipt).aggregate(total=Sum("allocated_amount"))["total"] or ZERO


def _allocated_supplier_reconciliation_amount(reconciliation: Reconciliation) -> Decimal:
    return SupplierPaymentAllocation.objects.filter(reconciliation=reconciliation).aggregate(total=Sum("allocated_amount"))["total"] or ZERO


def _allocated_supplier_opening_payable_amount(opening: OpeningPayable) -> Decimal:
    return SupplierPaymentAllocation.objects.filter(opening_payable=opening).aggregate(total=Sum("allocated_amount"))["total"] or ZERO


def _customer_reversed_amount(receipt: CustomerReceipt) -> Decimal:
    return receipt.reversals.filter(status=CustomerReceiptReversal.Status.CONFIRMED).aggregate(total=Sum("reversal_amount"))["total"] or ZERO


def _supplier_reversed_amount(payment: SupplierPayment) -> Decimal:
    return payment.reversals.filter(status=SupplierPaymentReversal.Status.CONFIRMED).aggregate(total=Sum("reversal_amount"))["total"] or ZERO


def _create_customer_reverse_allocations(receipt, reversal, reversal_amount, operator_id):
    remaining = reversal_amount
    for allocation in receipt.allocations.select_for_update().filter(allocated_amount__gt=0).order_by("id"):
        if remaining <= ZERO:
            break
        amount = min(allocation.allocated_amount, remaining)
        CustomerReceiptAllocation.objects.create(
            customer_receipt=receipt,
            sales_order=allocation.sales_order,
            reconciliation=allocation.reconciliation,
            opening_receivable=allocation.opening_receivable,
            allocated_amount=-amount,
            allocation_type=CustomerReceiptAllocation.AllocationType.REVERSAL,
            source_reversal=reversal,
            created_by_id=operator_id,
        )
        remaining -= amount


def _create_supplier_reverse_allocations(payment, reversal, reversal_amount, operator_id):
    remaining = reversal_amount
    for allocation in payment.allocations.select_for_update().filter(allocated_amount__gt=0).order_by("id"):
        if remaining <= ZERO:
            break
        amount = min(allocation.allocated_amount, remaining)
        SupplierPaymentAllocation.objects.create(
            supplier_payment=payment,
            purchase_receipt=allocation.purchase_receipt,
            reconciliation=allocation.reconciliation,
            opening_payable=allocation.opening_payable,
            allocated_amount=-amount,
            allocation_type=SupplierPaymentAllocation.AllocationType.REVERSAL,
            source_reversal=reversal,
            created_by_id=operator_id,
        )
        remaining -= amount


def _create_customer_credit_balance(receipt: CustomerReceipt, amount: Decimal, operator_id: int) -> CustomerCreditBalance:
    return CustomerCreditBalance.objects.create(
        customer=receipt.customer,
        source_doc_type="customer_receipt",
        source_doc_id=receipt.id,
        source_doc_no=receipt.receipt_no,
        balance_amount=amount,
        used_amount=ZERO,
        remaining_amount=amount,
        status=CustomerCreditBalance.Status.PENDING,
        created_by_id=operator_id,
    )


def _refresh_opening_receivable(opening: OpeningReceivable) -> None:
    settled_amount = _allocated_customer_opening_receivable_amount(opening)
    if settled_amount < ZERO:
        settled_amount = ZERO
    opening.settled_amount = settled_amount
    opening.remaining_amount = max(opening.opening_amount - settled_amount, ZERO)
    if opening.remaining_amount <= ZERO:
        opening.status = OpeningReceivable.Status.SETTLED
    elif opening.settled_amount > ZERO:
        opening.status = OpeningReceivable.Status.PART_SETTLED
    elif opening.status != OpeningReceivable.Status.VOIDED:
        opening.status = OpeningReceivable.Status.OPEN
    opening.save(update_fields=["settled_amount", "remaining_amount", "status"])


def _refresh_opening_payable(opening: OpeningPayable) -> None:
    settled_amount = _allocated_supplier_opening_payable_amount(opening)
    if settled_amount < ZERO:
        settled_amount = ZERO
    opening.settled_amount = settled_amount
    opening.remaining_amount = max(opening.opening_amount - settled_amount, ZERO)
    if opening.remaining_amount <= ZERO:
        opening.status = OpeningPayable.Status.SETTLED
    elif opening.settled_amount > ZERO:
        opening.status = OpeningPayable.Status.PART_SETTLED
    elif opening.status != OpeningPayable.Status.VOIDED:
        opening.status = OpeningPayable.Status.OPEN
    opening.save(update_fields=["settled_amount", "remaining_amount", "status"])


def _create_supplier_credit_balance(payment: SupplierPayment, amount: Decimal, operator_id: int) -> SupplierCreditBalance:
    return SupplierCreditBalance.objects.create(
        supplier=payment.supplier,
        source_doc_type="supplier_payment",
        source_doc_id=payment.id,
        source_doc_no=payment.payment_no,
        balance_amount=amount,
        used_amount=ZERO,
        remaining_amount=amount,
        status=SupplierCreditBalance.Status.PENDING,
        created_by_id=operator_id,
    )


def _customer_balance_status_after_action(balance, action_type):
    if balance.remaining_amount <= ZERO:
        if action_type == CustomerCreditBalanceTransaction.ActionType.REFUND:
            return CustomerCreditBalance.Status.REFUNDED
        if action_type == CustomerCreditBalanceTransaction.ActionType.TO_ADVANCE:
            return CustomerCreditBalance.Status.TO_ADVANCE
        if action_type == CustomerCreditBalanceTransaction.ActionType.CLOSE:
            return CustomerCreditBalance.Status.CLOSED
        return CustomerCreditBalance.Status.USED_UP
    return CustomerCreditBalance.Status.PART_USED


def _supplier_balance_status_after_action(balance, action_type):
    if balance.remaining_amount <= ZERO:
        if action_type == SupplierCreditBalanceTransaction.ActionType.REFUND:
            return SupplierCreditBalance.Status.REFUNDED
        if action_type == SupplierCreditBalanceTransaction.ActionType.TO_ADVANCE:
            return SupplierCreditBalance.Status.TO_ADVANCE
        if action_type == SupplierCreditBalanceTransaction.ActionType.CLOSE:
            return SupplierCreditBalance.Status.CLOSED
        return SupplierCreditBalance.Status.USED_UP
    return SupplierCreditBalance.Status.PART_USED


def _mark_event_confirmed(event_key: str, event_type: str, payload: dict) -> None:
    event = enqueue_pending_event(event_type, event_key, payload)
    if event.payload.get("confirmed"):
        return
    event.payload = {**payload, "confirmed": True}
    event.save(update_fields=["payload", "updated_at"])
