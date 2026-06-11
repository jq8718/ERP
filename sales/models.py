from django.conf import settings
from django.db import models

from bom.models import Bom
from inventory.models import InventoryBatch, WarehouseLocation
from masterdata.models import Customer, CustomerAddress, CustomerProduct, Material


class SalesOrder(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PENDING_APPROVAL = "pending_approval", "待审核"
        REJECTED = "rejected", "已驳回"
        PENDING_BOM = "pending_bom", "待 BOM 处理"
        CONFIRMED = "confirmed", "已确认"
        IN_PRODUCTION = "in_production", "生产中"
        SHIPPED = "shipped", "已发货"
        COMPLETED = "completed", "已完成"
        VOIDED = "voided", "已作废"

    sales_order_no = models.CharField(max_length=100, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="sales_orders")
    customer_address = models.ForeignKey(CustomerAddress, null=True, blank=True, on_delete=models.PROTECT)
    order_date = models.DateField()
    delivery_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.DRAFT)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    contract_attachment_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    version = models.PositiveIntegerField(default=1)
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "sales_orders"
        indexes = [
            models.Index(fields=["customer", "status"]),
            models.Index(fields=["delivery_date"]),
            models.Index(fields=["created_at"]),
        ]


class SalesOrderItem(models.Model):
    class LineStatus(models.TextChoices):
        DRAFT = "draft", "草稿"
        PENDING_APPROVAL = "pending_approval", "待审核"
        CONFIRMED = "confirmed", "已确认"
        IN_PRODUCTION = "in_production", "生产中"
        SHIPPED = "shipped", "已发货"
        COMPLETED = "completed", "已完成"

    class InventoryCheckStatus(models.TextChoices):
        UNCHECKED = "unchecked", "未检查"
        SUFFICIENT = "sufficient", "库存充足"
        PENDING_BOM = "pending_bom", "待 BOM 处理"
        SHORTAGE = "shortage", "欠料"
        KITTED = "kitted", "已齐套"

    sales_order = models.ForeignKey(SalesOrder, on_delete=models.CASCADE, related_name="items")
    line_no = models.PositiveIntegerField()
    customer_product = models.ForeignKey(CustomerProduct, on_delete=models.PROTECT)
    finished_material = models.ForeignKey(Material, on_delete=models.PROTECT)
    order_qty = models.DecimalField(max_digits=14, decimal_places=4)
    shipped_qty = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    unit_price = models.DecimalField(max_digits=14, decimal_places=4)
    line_amount = models.DecimalField(max_digits=14, decimal_places=2)
    locked_bom = models.ForeignKey(Bom, null=True, blank=True, on_delete=models.PROTECT)
    locked_bom_version = models.CharField(max_length=40, blank=True)
    line_status = models.CharField(max_length=32, choices=LineStatus.choices, default=LineStatus.DRAFT)
    inventory_check_status = models.CharField(
        max_length=32,
        choices=InventoryCheckStatus.choices,
        default=InventoryCheckStatus.UNCHECKED,
    )

    class Meta:
        db_table = "sales_order_items"
        constraints = [
            models.UniqueConstraint(fields=["sales_order", "customer_product"], name="uq_sales_order_customer_product"),
            models.UniqueConstraint(fields=["sales_order", "line_no"], name="uq_sales_order_line"),
        ]
        indexes = [
            models.Index(fields=["sales_order", "line_status"]),
            models.Index(fields=["finished_material"]),
        ]


class SalesOrderChangeLog(models.Model):
    sales_order = models.ForeignKey(SalesOrder, on_delete=models.CASCADE, related_name="change_logs")
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    changed_at = models.DateTimeField(auto_now_add=True)
    change_reason = models.TextField()
    before_snapshot = models.JSONField(default=dict)
    after_snapshot = models.JSONField(default=dict)
    approval_id = models.PositiveBigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "sales_order_change_logs"
        indexes = [models.Index(fields=["sales_order", "changed_at"])]


class CustomerReturn(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PENDING_APPROVAL = "pending_approval", "待审核"
        REJECTED = "rejected", "已驳回"
        CONFIRMED = "confirmed", "已确认"
        RECEIVED = "received", "已收货"
        VOIDED = "voided", "已作废"

    return_no = models.CharField(max_length=100, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    sales_order = models.ForeignKey(SalesOrder, null=True, blank=True, on_delete=models.PROTECT)
    return_date = models.DateField()
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.DRAFT)
    return_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "customer_returns"


class CustomerReturnItem(models.Model):
    customer_return = models.ForeignKey(CustomerReturn, on_delete=models.CASCADE, related_name="items")
    sales_order_item = models.ForeignKey(SalesOrderItem, null=True, blank=True, on_delete=models.PROTECT)
    material = models.ForeignKey(Material, on_delete=models.PROTECT)
    return_qty = models.DecimalField(max_digits=14, decimal_places=4)
    unit_price = models.DecimalField(max_digits=14, decimal_places=4)
    return_amount = models.DecimalField(max_digits=14, decimal_places=2)
    location = models.ForeignKey(WarehouseLocation, null=True, blank=True, on_delete=models.PROTECT)
    inventory_type = models.CharField(max_length=24, default="available")
    return_reason = models.TextField(blank=True)

    class Meta:
        db_table = "customer_return_items"
        constraints = [
            models.UniqueConstraint(fields=["customer_return", "sales_order_item", "material"], name="uq_customer_return_item"),
        ]


class SampleLoan(models.Model):
    class Status(models.TextChoices):
        PENDING_APPROVAL = "pending_approval", "待审核"
        OUT = "out", "已出库"
        PART_RETURNED = "part_returned", "部分归还"
        RETURNED = "returned", "已归还"
        PART_SOLD = "part_sold", "部分转销售"
        SOLD = "sold", "已转销售"
        VOIDED = "voided", "已作废"

    class OverdueStatus(models.TextChoices):
        NONE = "none", "未逾期"
        DUE_SOON = "due_soon", "即将到期"
        OVERDUE = "overdue", "已逾期"
        CLOSED = "closed", "已关闭"

    sample_loan_no = models.CharField(max_length=100, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    loan_date = models.DateField()
    expected_return_date = models.DateField()
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.PENDING_APPROVAL)
    is_overdue = models.BooleanField(default=False)
    overdue_days = models.PositiveIntegerField(default=0)
    overdue_status = models.CharField(max_length=24, choices=OverdueStatus.choices, default=OverdueStatus.NONE)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "sample_loans"
        indexes = [
            models.Index(fields=["customer", "status"]),
            models.Index(fields=["overdue_status", "expected_return_date"]),
        ]


class SampleLoanItem(models.Model):
    class LineStatus(models.TextChoices):
        OUT = "out", "已出库"
        PART_RETURNED = "part_returned", "部分归还"
        RETURNED = "returned", "已归还"
        PART_SOLD = "part_sold", "部分转销售"
        SOLD = "sold", "已转销售"
        VOIDED = "voided", "已作废"

    sample_loan = models.ForeignKey(SampleLoan, on_delete=models.CASCADE, related_name="items")
    line_no = models.PositiveIntegerField()
    material = models.ForeignKey(Material, on_delete=models.PROTECT)
    loan_qty = models.DecimalField(max_digits=14, decimal_places=4)
    returned_qty = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    sold_qty = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    expected_return_date = models.DateField(null=True, blank=True)
    batch = models.ForeignKey(InventoryBatch, null=True, blank=True, on_delete=models.PROTECT)
    location = models.ForeignKey(WarehouseLocation, null=True, blank=True, on_delete=models.PROTECT)
    line_status = models.CharField(max_length=32, choices=LineStatus.choices, default=LineStatus.OUT)

    class Meta:
        db_table = "sample_loan_items"
        constraints = [
            models.UniqueConstraint(fields=["sample_loan", "line_no"], name="uq_sample_loan_line"),
        ]


class SampleLoanReturn(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PENDING_CONFIRM = "pending_confirm", "待确认"
        RECEIVED = "received", "已入库"
        VOIDED = "voided", "已作废"

    sample_return_no = models.CharField(max_length=100, unique=True)
    sample_loan = models.ForeignKey(SampleLoan, on_delete=models.PROTECT, related_name="returns")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    return_date = models.DateField()
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.DRAFT)
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "sample_loan_returns"


class SampleLoanReturnItem(models.Model):
    class SampleCondition(models.TextChoices):
        GOOD = "good", "完好"
        DAMAGED = "damaged", "损坏"
        PENDING_CHECK = "pending_check", "待检"
        MISSING_PART = "missing_part", "缺件"

    sample_return = models.ForeignKey(SampleLoanReturn, on_delete=models.CASCADE, related_name="items")
    sample_loan = models.ForeignKey(SampleLoan, on_delete=models.PROTECT)
    sample_loan_item = models.ForeignKey(SampleLoanItem, on_delete=models.PROTECT)
    material = models.ForeignKey(Material, on_delete=models.PROTECT)
    return_qty = models.DecimalField(max_digits=14, decimal_places=4)
    location = models.ForeignKey(WarehouseLocation, on_delete=models.PROTECT)
    inventory_type = models.CharField(max_length=24, default="available")
    sample_condition = models.CharField(max_length=32, choices=SampleCondition.choices, default=SampleCondition.GOOD)
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "sample_loan_return_items"
        constraints = [
            models.UniqueConstraint(fields=["sample_return", "sample_loan_item", "location"], name="uq_sample_return_item"),
        ]


class SalesShipment(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PENDING_CONFIRM = "pending_confirm", "待确认"
        SHIPPED = "shipped", "已出库"
        VOIDED = "voided", "已作废"

    shipment_no = models.CharField(max_length=100, unique=True)
    sales_order = models.ForeignKey(SalesOrder, on_delete=models.PROTECT, related_name="shipments")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    shipment_date = models.DateField()
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "sales_shipments"


class SalesShipmentItem(models.Model):
    shipment = models.ForeignKey(SalesShipment, on_delete=models.CASCADE, related_name="items")
    sales_order_item = models.ForeignKey(SalesOrderItem, on_delete=models.PROTECT)
    material = models.ForeignKey(Material, on_delete=models.PROTECT)
    shipment_qty = models.DecimalField(max_digits=14, decimal_places=4)
    batch = models.ForeignKey(InventoryBatch, on_delete=models.PROTECT)
    location = models.ForeignKey(WarehouseLocation, on_delete=models.PROTECT)
    cost_price = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)

    class Meta:
        db_table = "sales_shipment_items"
        constraints = [
            models.UniqueConstraint(fields=["shipment", "sales_order_item", "batch"], name="uq_sales_shipment_item"),
        ]


class ShortageAlert(models.Model):
    class Status(models.TextChoices):
        UNPROCESSED = "unprocessed", "未处理"
        PURCHASE_REQUESTED = "purchase_requested", "已生成采购需求"
        PARTIAL_RECEIVED = "partial_received", "部分到货"
        KITTED = "kitted", "已齐套"
        CLOSED = "closed", "已关闭"

    shortage_no = models.CharField(max_length=100, unique=True)
    sales_order = models.ForeignKey(SalesOrder, on_delete=models.CASCADE, related_name="shortage_alerts")
    sales_order_item = models.ForeignKey(SalesOrderItem, on_delete=models.CASCADE, related_name="shortage_alerts")
    material = models.ForeignKey(Material, on_delete=models.PROTECT)
    required_qty = models.DecimalField(max_digits=14, decimal_places=4)
    available_qty = models.DecimalField(max_digits=14, decimal_places=4)
    shortage_qty = models.DecimalField(max_digits=14, decimal_places=4)
    is_required = models.BooleanField(default=True)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.UNPROCESSED)
    purchase_request = models.ForeignKey(
        "purchase.PurchaseRequest",
        null=True,
        blank=True,
        db_column="purchase_request_id",
        on_delete=models.SET_NULL,
        related_name="shortage_alerts",
    )
    closed_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "shortage_alerts"
        indexes = [
            models.Index(fields=["status", "material"]),
            models.Index(fields=["sales_order_item", "status"]),
        ]
