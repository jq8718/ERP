from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction

from masterdata.models import Customer, Supplier
from purchase.models import PurchaseReceipt
from sales.models import SalesOrder


class Reconciliation(models.Model):
    class PartyType(models.TextChoices):
        CUSTOMER = "customer", "客户"
        SUPPLIER = "supplier", "供应商"

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        CONFIRMED = "confirmed", "已确认"
        VOIDED = "voided", "已作废"

    reconciliation_no = models.CharField(max_length=100, unique=True)
    party_type = models.CharField(max_length=24, choices=PartyType.choices)
    customer = models.ForeignKey(Customer, null=True, blank=True, on_delete=models.PROTECT)
    supplier = models.ForeignKey(Supplier, null=True, blank=True, on_delete=models.PROTECT)
    period_start = models.DateField()
    period_end = models.DateField()
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "reconciliations"
        indexes = [
            models.Index(fields=["party_type", "status"]),
            models.Index(fields=["customer", "status"]),
            models.Index(fields=["supplier", "status"]),
            models.Index(fields=["period_start", "period_end"]),
        ]


class ReconciliationItem(models.Model):
    class SourceType(models.TextChoices):
        SALES_ORDER = "sales_order", "销售订单"
        PURCHASE_RECEIPT = "purchase_receipt", "进货单"

    reconciliation = models.ForeignKey(Reconciliation, on_delete=models.CASCADE, related_name="items")
    line_no = models.PositiveIntegerField()
    source_type = models.CharField(max_length=32, choices=SourceType.choices)
    source_doc_id = models.PositiveBigIntegerField()
    source_no = models.CharField(max_length=100)
    source_date = models.DateField()
    gross_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    adjust_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    allocated_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    open_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "reconciliation_items"
        constraints = [
            models.UniqueConstraint(fields=["reconciliation", "line_no"], name="uq_reconciliation_item_line"),
            models.UniqueConstraint(fields=["reconciliation", "source_type", "source_doc_id"], name="uq_reconciliation_item_source"),
        ]
        indexes = [
            models.Index(fields=["reconciliation", "line_no"]),
            models.Index(fields=["source_type", "source_doc_id"]),
        ]


class CustomerReceipt(models.Model):
    class ReceiptMethod(models.TextChoices):
        CASH = "cash", "现金"
        TRANSFER = "transfer", "转账"
        CHECK = "check", "支票"
        OTHER = "other", "其他"

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PENDING_APPROVAL = "pending_approval", "待审核"
        CONFIRMED = "confirmed", "已确认"
        VOIDED = "voided", "已作废"
        REVERSED = "reversed", "已红冲"
        PART_REVERSED = "part_reversed", "部分红冲"

    receipt_no = models.CharField(max_length=100, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="receipts")
    receipt_date = models.DateField()
    receipt_amount = models.DecimalField(max_digits=14, decimal_places=2)
    unallocated_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    receipt_method = models.CharField(max_length=24, choices=ReceiptMethod.choices, default=ReceiptMethod.TRANSFER)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.DRAFT)
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="handled_customer_receipts",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "customer_receipts"
        indexes = [
            models.Index(fields=["customer", "status", "receipt_date"]),
            models.Index(fields=["status", "created_at"]),
        ]


class OpeningReceivable(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "未结清"
        PART_SETTLED = "part_settled", "部分结清"
        SETTLED = "settled", "已结清"
        VOIDED = "voided", "已作废"

    opening_no = models.CharField(max_length=100, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="opening_receivables")
    source_doc_no = models.CharField(max_length=100, blank=True)
    opening_date = models.DateField()
    due_date = models.DateField(null=True, blank=True)
    opening_amount = models.DecimalField(max_digits=14, decimal_places=2)
    settled_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    remaining_amount = models.DecimalField(max_digits=14, decimal_places=2)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "opening_receivables"
        indexes = [
            models.Index(fields=["customer", "status", "opening_date"]),
            models.Index(fields=["status", "remaining_amount"]),
        ]


class CustomerReceiptAllocation(models.Model):
    class AllocationType(models.TextChoices):
        SALES_ORDER = "sales_order", "销售订单核销"
        RECONCILIATION = "reconciliation", "客户对账单核销"
        OPENING_RECEIVABLE = "opening_receivable", "期初应收核销"
        REVERSAL = "reversal", "红冲反向核销"
        CREDIT_BALANCE = "credit_balance", "余额核销"

    customer_receipt = models.ForeignKey(CustomerReceipt, on_delete=models.CASCADE, related_name="allocations")
    sales_order = models.ForeignKey(SalesOrder, null=True, blank=True, on_delete=models.PROTECT)
    reconciliation = models.ForeignKey(Reconciliation, null=True, blank=True, on_delete=models.PROTECT)
    opening_receivable = models.ForeignKey(OpeningReceivable, null=True, blank=True, on_delete=models.PROTECT)
    allocated_amount = models.DecimalField(max_digits=14, decimal_places=2)
    allocation_type = models.CharField(max_length=32, choices=AllocationType.choices, default=AllocationType.SALES_ORDER)
    source_reversal = models.ForeignKey(
        "finance.CustomerReceiptReversal",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reverse_allocations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "customer_receipt_allocations"
        indexes = [
            models.Index(fields=["customer_receipt", "allocation_type"]),
            models.Index(fields=["sales_order"]),
            models.Index(fields=["reconciliation"]),
            models.Index(fields=["opening_receivable"]),
        ]

    def save(self, *args, **kwargs):
        if not transaction.get_connection().in_atomic_block:
            with transaction.atomic():
                self._validate_available_amount()
                return super().save(*args, **kwargs)
        self._validate_available_amount()
        return super().save(*args, **kwargs)

    def _validate_available_amount(self):
        if self.allocated_amount <= 0:
            return
        current_id = self.pk
        receipt = CustomerReceipt.objects.select_for_update().get(pk=self.customer_receipt_id)
        receipt_allocated = (
            CustomerReceiptAllocation.objects.filter(customer_receipt_id=self.customer_receipt_id)
            .exclude(pk=current_id)
            .aggregate(total=models.Sum("allocated_amount"))["total"]
            or 0
        )
        if self.allocated_amount > receipt.receipt_amount - receipt_allocated:
            raise ValidationError("核销金额不能超过收款单金额")
        if self.sales_order_id:
            order = SalesOrder.objects.select_for_update().get(pk=self.sales_order_id)
            if order.customer_id != receipt.customer_id:
                raise ValidationError("收款单客户与销售订单客户不一致")
            receivable = order.items.aggregate(total=models.Sum("line_amount"))["total"] or 0
            allocated = (
                CustomerReceiptAllocation.objects.filter(sales_order_id=self.sales_order_id)
                .exclude(pk=current_id)
                .aggregate(total=models.Sum("allocated_amount"))["total"]
                or 0
            )
            if self.allocated_amount > receivable - allocated:
                raise ValidationError("核销金额超过订单可核销余额")
        if self.reconciliation_id:
            reconciliation = Reconciliation.objects.select_for_update().get(pk=self.reconciliation_id)
            if (
                reconciliation.party_type != Reconciliation.PartyType.CUSTOMER
                or reconciliation.customer_id != receipt.customer_id
            ):
                raise ValidationError("收款单客户与对账单客户不一致")
            allocated = (
                CustomerReceiptAllocation.objects.filter(reconciliation_id=self.reconciliation_id)
                .exclude(pk=current_id)
                .aggregate(total=models.Sum("allocated_amount"))["total"]
                or 0
            )
            if self.allocated_amount > reconciliation.total_amount - allocated:
                raise ValidationError("核销金额超过对账单可核销余额")
        if self.opening_receivable_id:
            opening = OpeningReceivable.objects.select_for_update().get(pk=self.opening_receivable_id)
            if opening.customer_id != receipt.customer_id:
                raise ValidationError("收款单客户与期初应收客户不一致")
            if opening.status == OpeningReceivable.Status.VOIDED:
                raise ValidationError("已作废期初应收不能核销")
            allocated = (
                CustomerReceiptAllocation.objects.filter(opening_receivable_id=self.opening_receivable_id)
                .exclude(pk=current_id)
                .aggregate(total=models.Sum("allocated_amount"))["total"]
                or 0
            )
            if self.allocated_amount > opening.opening_amount - allocated:
                raise ValidationError("核销金额超过期初应收可核销余额")


class CustomerReceiptReversal(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PENDING_APPROVAL = "pending_approval", "待审核"
        CONFIRMED = "confirmed", "已确认"
        VOIDED = "voided", "已作废"

    reversal_no = models.CharField(max_length=100, unique=True)
    source_receipt = models.ForeignKey(CustomerReceipt, on_delete=models.PROTECT, related_name="reversals")
    reversal_amount = models.DecimalField(max_digits=14, decimal_places=2)
    reason = models.TextField()
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.DRAFT)
    idempotency_key = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta:
        db_table = "customer_receipt_reversals"
        constraints = [
            models.UniqueConstraint(fields=["source_receipt", "idempotency_key"], name="uq_customer_receipt_reversal_idem"),
        ]
        indexes = [
            models.Index(fields=["source_receipt", "status"]),
        ]


class CustomerCreditBalance(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待处理"
        TO_ADVANCE = "to_advance", "已转预收"
        PART_USED = "part_used", "部分使用"
        USED_UP = "used_up", "已用完"
        REFUNDED = "refunded", "已退款"
        CLOSED = "closed", "已关闭"

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="credit_balances")
    source_doc_type = models.CharField(max_length=80)
    source_doc_id = models.PositiveBigIntegerField()
    source_doc_no = models.CharField(max_length=100, blank=True)
    balance_amount = models.DecimalField(max_digits=14, decimal_places=2)
    used_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    remaining_amount = models.DecimalField(max_digits=14, decimal_places=2)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PENDING)
    process_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        db_table = "customer_credit_balances"
        indexes = [
            models.Index(fields=["customer", "status", "remaining_amount"]),
            models.Index(fields=["source_doc_type", "source_doc_id"]),
        ]


class CustomerCreditBalanceTransaction(models.Model):
    class ActionType(models.TextChoices):
        TO_ADVANCE = "to_advance", "转预收款"
        ALLOCATE_TO_ORDER = "allocate_to_order", "核销其他订单"
        REFUND = "refund", "登记退款"
        CLOSE = "close", "关闭"

    transaction_no = models.CharField(max_length=100, unique=True)
    credit_balance = models.ForeignKey(CustomerCreditBalance, on_delete=models.PROTECT, related_name="transactions")
    action_type = models.CharField(max_length=32, choices=ActionType.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    target_doc_type = models.CharField(max_length=80, blank=True)
    target_doc_id = models.PositiveBigIntegerField(null=True, blank=True)
    target_doc_no = models.CharField(max_length=100, blank=True)
    idempotency_key = models.CharField(max_length=200)
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        db_table = "customer_credit_balance_transactions"
        constraints = [
            models.UniqueConstraint(fields=["credit_balance", "idempotency_key"], name="uq_customer_credit_txn_idem"),
        ]
        indexes = [
            models.Index(fields=["credit_balance", "action_type"]),
            models.Index(fields=["target_doc_type", "target_doc_id"]),
        ]


class SupplierPayment(models.Model):
    class PaymentMethod(models.TextChoices):
        CASH = "cash", "现金"
        TRANSFER = "transfer", "转账"
        CHECK = "check", "支票"
        OTHER = "other", "其他"

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PENDING_APPROVAL = "pending_approval", "待审核"
        CONFIRMED = "confirmed", "已确认"
        VOIDED = "voided", "已作废"
        REVERSED = "reversed", "已红冲"
        PART_REVERSED = "part_reversed", "部分红冲"

    payment_no = models.CharField(max_length=100, unique=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="payments")
    payment_date = models.DateField()
    payment_amount = models.DecimalField(max_digits=14, decimal_places=2)
    unallocated_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    payment_method = models.CharField(max_length=24, choices=PaymentMethod.choices, default=PaymentMethod.TRANSFER)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.DRAFT)
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="handled_supplier_payments",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "supplier_payments"
        indexes = [
            models.Index(fields=["supplier", "status", "payment_date"]),
            models.Index(fields=["status", "created_at"]),
        ]


class OpeningPayable(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "未结清"
        PART_SETTLED = "part_settled", "部分结清"
        SETTLED = "settled", "已结清"
        VOIDED = "voided", "已作废"

    opening_no = models.CharField(max_length=100, unique=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="opening_payables")
    source_doc_no = models.CharField(max_length=100, blank=True)
    opening_date = models.DateField()
    due_date = models.DateField(null=True, blank=True)
    opening_amount = models.DecimalField(max_digits=14, decimal_places=2)
    settled_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    remaining_amount = models.DecimalField(max_digits=14, decimal_places=2)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "opening_payables"
        indexes = [
            models.Index(fields=["supplier", "status", "opening_date"]),
            models.Index(fields=["status", "remaining_amount"]),
        ]


class SupplierPaymentAllocation(models.Model):
    class AllocationType(models.TextChoices):
        PURCHASE_RECEIPT = "purchase_receipt", "进货单核销"
        RECONCILIATION = "reconciliation", "供应商对账单核销"
        OPENING_PAYABLE = "opening_payable", "期初应付核销"
        REVERSAL = "reversal", "红冲反向核销"
        CREDIT_BALANCE = "credit_balance", "余额核销"

    supplier_payment = models.ForeignKey(SupplierPayment, on_delete=models.CASCADE, related_name="allocations")
    purchase_receipt = models.ForeignKey(PurchaseReceipt, null=True, blank=True, on_delete=models.PROTECT)
    reconciliation = models.ForeignKey(Reconciliation, null=True, blank=True, on_delete=models.PROTECT)
    opening_payable = models.ForeignKey(OpeningPayable, null=True, blank=True, on_delete=models.PROTECT)
    allocated_amount = models.DecimalField(max_digits=14, decimal_places=2)
    allocation_type = models.CharField(
        max_length=32,
        choices=AllocationType.choices,
        default=AllocationType.PURCHASE_RECEIPT,
    )
    source_reversal = models.ForeignKey(
        "finance.SupplierPaymentReversal",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reverse_allocations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "supplier_payment_allocations"
        indexes = [
            models.Index(fields=["supplier_payment", "allocation_type"]),
            models.Index(fields=["purchase_receipt"]),
            models.Index(fields=["reconciliation"]),
            models.Index(fields=["opening_payable"]),
        ]

    def save(self, *args, **kwargs):
        if not transaction.get_connection().in_atomic_block:
            with transaction.atomic():
                self._validate_available_amount()
                return super().save(*args, **kwargs)
        self._validate_available_amount()
        return super().save(*args, **kwargs)

    def _validate_available_amount(self):
        if self.allocated_amount <= 0:
            return
        current_id = self.pk
        payment = SupplierPayment.objects.select_for_update().get(pk=self.supplier_payment_id)
        payment_allocated = (
            SupplierPaymentAllocation.objects.filter(supplier_payment_id=self.supplier_payment_id)
            .exclude(pk=current_id)
            .aggregate(total=models.Sum("allocated_amount"))["total"]
            or 0
        )
        if self.allocated_amount > payment.payment_amount - payment_allocated:
            raise ValidationError("核销金额不能超过付款单金额")
        if self.purchase_receipt_id:
            receipt = PurchaseReceipt.objects.select_for_update().get(pk=self.purchase_receipt_id)
            if receipt.supplier_id != payment.supplier_id:
                raise ValidationError("付款单供应商与进货单供应商不一致")
            payable = sum((item.accepted_qty * item.unit_price for item in receipt.items.all()), start=0)
            allocated = (
                SupplierPaymentAllocation.objects.filter(purchase_receipt_id=self.purchase_receipt_id)
                .exclude(pk=current_id)
                .aggregate(total=models.Sum("allocated_amount"))["total"]
                or 0
            )
            if self.allocated_amount > payable - allocated:
                raise ValidationError("核销金额超过进货单可核销余额")
        if self.reconciliation_id:
            reconciliation = Reconciliation.objects.select_for_update().get(pk=self.reconciliation_id)
            if (
                reconciliation.party_type != Reconciliation.PartyType.SUPPLIER
                or reconciliation.supplier_id != payment.supplier_id
            ):
                raise ValidationError("付款单供应商与对账单供应商不一致")
            allocated = (
                SupplierPaymentAllocation.objects.filter(reconciliation_id=self.reconciliation_id)
                .exclude(pk=current_id)
                .aggregate(total=models.Sum("allocated_amount"))["total"]
                or 0
            )
            if self.allocated_amount > reconciliation.total_amount - allocated:
                raise ValidationError("核销金额超过对账单可核销余额")
        if self.opening_payable_id:
            opening = OpeningPayable.objects.select_for_update().get(pk=self.opening_payable_id)
            if opening.supplier_id != payment.supplier_id:
                raise ValidationError("付款单供应商与期初应付供应商不一致")
            if opening.status == OpeningPayable.Status.VOIDED:
                raise ValidationError("已作废期初应付不能核销")
            allocated = (
                SupplierPaymentAllocation.objects.filter(opening_payable_id=self.opening_payable_id)
                .exclude(pk=current_id)
                .aggregate(total=models.Sum("allocated_amount"))["total"]
                or 0
            )
            if self.allocated_amount > opening.opening_amount - allocated:
                raise ValidationError("核销金额超过期初应付可核销余额")


class ExpenseRecord(models.Model):
    class ExpenseCategory(models.TextChoices):
        AUXILIARY = "auxiliary", "采购辅料"
        FREIGHT = "freight", "运费"
        ELECTRICITY = "electricity", "电费"
        RENT = "rent", "房租"
        EQUIPMENT = "equipment", "设备采购"
        MEAL = "meal", "餐费"
        BUSINESS = "business", "业务费用"
        GIFT = "gift", "礼品"
        OFFICE = "office", "办公费用"
        OTHER = "other", "其他"

    class PaymentMethod(models.TextChoices):
        CASH = "cash", "现金"
        TRANSFER = "transfer", "转账"
        CHECK = "check", "支票"
        OTHER = "other", "其他"

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        CONFIRMED = "confirmed", "已确认"
        VOIDED = "voided", "已作废"

    expense_no = models.CharField(max_length=100, unique=True)
    expense_date = models.DateField()
    category = models.CharField(max_length=32, choices=ExpenseCategory.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    payment_method = models.CharField(max_length=24, choices=PaymentMethod.choices, default=PaymentMethod.TRANSFER)
    payee = models.CharField(max_length=160, blank=True)
    invoice_no = models.CharField(max_length=100, blank=True)
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="handled_expense_records",
    )
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    confirmed_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "expense_records"
        indexes = [
            models.Index(fields=["expense_date", "category"]),
            models.Index(fields=["status", "created_at"]),
        ]


class SupplierPaymentReversal(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PENDING_APPROVAL = "pending_approval", "待审核"
        CONFIRMED = "confirmed", "已确认"
        VOIDED = "voided", "已作废"

    reversal_no = models.CharField(max_length=100, unique=True)
    source_payment = models.ForeignKey(SupplierPayment, on_delete=models.PROTECT, related_name="reversals")
    reversal_amount = models.DecimalField(max_digits=14, decimal_places=2)
    reason = models.TextField()
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.DRAFT)
    idempotency_key = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta:
        db_table = "supplier_payment_reversals"
        constraints = [
            models.UniqueConstraint(fields=["source_payment", "idempotency_key"], name="uq_supplier_payment_reversal_idem"),
        ]
        indexes = [
            models.Index(fields=["source_payment", "status"]),
        ]


class SupplierCreditBalance(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待处理"
        TO_ADVANCE = "to_advance", "已转预付"
        PART_USED = "part_used", "部分使用"
        USED_UP = "used_up", "已用完"
        REFUNDED = "refunded", "已退款"
        CLOSED = "closed", "已关闭"

    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="credit_balances")
    source_doc_type = models.CharField(max_length=80)
    source_doc_id = models.PositiveBigIntegerField()
    source_doc_no = models.CharField(max_length=100, blank=True)
    balance_amount = models.DecimalField(max_digits=14, decimal_places=2)
    used_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    remaining_amount = models.DecimalField(max_digits=14, decimal_places=2)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PENDING)
    process_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        db_table = "supplier_credit_balances"
        indexes = [
            models.Index(fields=["supplier", "status", "remaining_amount"]),
            models.Index(fields=["source_doc_type", "source_doc_id"]),
        ]


class SupplierCreditBalanceTransaction(models.Model):
    class ActionType(models.TextChoices):
        TO_ADVANCE = "to_advance", "转预付款"
        ALLOCATE_TO_RECEIPT = "allocate_to_receipt", "核销其他进货单"
        REFUND = "refund", "登记供应商退款"
        CLOSE = "close", "关闭"

    transaction_no = models.CharField(max_length=100, unique=True)
    credit_balance = models.ForeignKey(SupplierCreditBalance, on_delete=models.PROTECT, related_name="transactions")
    action_type = models.CharField(max_length=32, choices=ActionType.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    target_doc_type = models.CharField(max_length=80, blank=True)
    target_doc_id = models.PositiveBigIntegerField(null=True, blank=True)
    target_doc_no = models.CharField(max_length=100, blank=True)
    idempotency_key = models.CharField(max_length=200)
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        db_table = "supplier_credit_balance_transactions"
        constraints = [
            models.UniqueConstraint(fields=["credit_balance", "idempotency_key"], name="uq_supplier_credit_txn_idem"),
        ]
        indexes = [
            models.Index(fields=["credit_balance", "action_type"]),
            models.Index(fields=["target_doc_type", "target_doc_id"]),
        ]
