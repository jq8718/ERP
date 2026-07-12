from django.conf import settings
from django.db import models

from inventory.models import InventoryBatch, WarehouseLocation
from masterdata.models import Material, Supplier
from sales.models import SalesOrderItem, ShortageAlert


class PurchaseRequest(models.Model):
    class SourceType(models.TextChoices):
        MANUAL = "manual", "人工"
        SHORTAGE = "shortage", "欠料"
        LOW_STOCK = "low_stock", "低库存"

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PENDING_APPROVAL = "pending_approval", "待审核"
        REJECTED = "rejected", "已驳回"
        APPROVED = "approved", "已通过"
        CLOSED = "closed", "已关闭"
        VOIDED = "voided", "已作废"

    purchase_request_no = models.CharField(max_length=100, unique=True)
    source_type = models.CharField(max_length=24, choices=SourceType.choices, default=SourceType.MANUAL)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.DRAFT)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    needed_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "purchase_requests"

    def __str__(self):
        return f"{self.purchase_request_no} - {self.get_status_display()}"


class PurchaseRequestItem(models.Model):
    class LineStatus(models.TextChoices):
        OPEN = "open", "未下单"
        ORDERED = "ordered", "已下单"
        PARTIAL_ORDERED = "partial_ordered", "部分下单"
        CLOSED = "closed", "已关闭"

    purchase_request = models.ForeignKey(PurchaseRequest, on_delete=models.CASCADE, related_name="items")
    line_no = models.PositiveIntegerField()
    material = models.ForeignKey(Material, on_delete=models.PROTECT)
    request_qty = models.DecimalField(max_digits=14, decimal_places=4)
    suggested_supplier = models.ForeignKey(Supplier, null=True, blank=True, on_delete=models.PROTECT)
    needed_date = models.DateField(null=True, blank=True)
    source_shortage_alert = models.ForeignKey(ShortageAlert, null=True, blank=True, on_delete=models.PROTECT)
    source_sales_order_item = models.ForeignKey(SalesOrderItem, null=True, blank=True, on_delete=models.PROTECT)
    line_status = models.CharField(max_length=32, choices=LineStatus.choices, default=LineStatus.OPEN)

    class Meta:
        db_table = "purchase_request_items"
        constraints = [
            models.UniqueConstraint(fields=["purchase_request", "line_no"], name="uq_purchase_request_line"),
        ]
        indexes = [
            models.Index(fields=["material", "line_status"]),
            models.Index(fields=["source_shortage_alert"]),
        ]

    def __str__(self):
        return f"{self.purchase_request.purchase_request_no} 第{self.line_no}行 - {self.material}"


class PurchaseOrder(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PENDING_APPROVAL = "pending_approval", "待审核"
        REJECTED = "rejected", "已驳回"
        APPROVED = "approved", "已通过"
        PARTIAL_RECEIVED = "partial_received", "部分到货"
        RECEIVED = "received", "已到货"
        CLOSED = "closed", "已关闭"
        VOIDED = "voided", "已作废"

    purchase_order_no = models.CharField(max_length=100, unique=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchase_orders")
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.DRAFT)
    order_date = models.DateField()
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    purchase_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="owned_purchase_orders",
    )
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "purchase_orders"
        indexes = [
            models.Index(fields=["supplier", "status"]),
            models.Index(fields=["order_date"]),
            models.Index(fields=["purchase_owner", "status"]),
        ]

    def __str__(self):
        return f"{self.purchase_order_no} - {self.supplier} - {self.get_status_display()}"


class PurchaseOrderItem(models.Model):
    class LineStatus(models.TextChoices):
        OPEN = "open", "未到货"
        PARTIAL_RECEIVED = "partial_received", "部分到货"
        RECEIVED = "received", "已到货"
        CLOSED = "closed", "已关闭"

    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="items")
    purchase_request_item = models.ForeignKey(PurchaseRequestItem, null=True, blank=True, on_delete=models.PROTECT)
    line_no = models.PositiveIntegerField()
    material = models.ForeignKey(Material, on_delete=models.PROTECT)
    order_qty = models.DecimalField(max_digits=14, decimal_places=4)
    received_qty = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    unit_price = models.DecimalField(max_digits=14, decimal_places=6)
    line_amount = models.DecimalField(max_digits=14, decimal_places=2)
    needed_date = models.DateField(null=True, blank=True)
    line_status = models.CharField(max_length=32, choices=LineStatus.choices, default=LineStatus.OPEN)

    class Meta:
        db_table = "purchase_order_items"
        constraints = [
            models.UniqueConstraint(fields=["purchase_order", "line_no"], name="uq_purchase_order_line"),
        ]
        indexes = [
            models.Index(fields=["purchase_order", "line_status"]),
            models.Index(fields=["material"]),
        ]

    def __str__(self):
        return f"{self.purchase_order.purchase_order_no} 第{self.line_no}行 - {self.material}"


class PurchaseReceipt(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PENDING_APPROVAL = "pending_approval", "待审核"
        PENDING_RECEIVE = "pending_receive", "待入库"
        PARTIAL_RECEIVED = "partial_received", "部分入库"
        RECEIVED = "received", "已入库"
        VOIDED = "voided", "已作废"

    purchase_receipt_no = models.CharField(max_length=100, unique=True)
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="receipts")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT)
    receipt_date = models.DateField()
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "purchase_receipts"
        indexes = [
            models.Index(fields=["supplier", "status"]),
            models.Index(fields=["receipt_date"]),
        ]

    def __str__(self):
        return f"{self.purchase_receipt_no} - {self.supplier} - {self.get_status_display()}"


class PurchaseReceiptItem(models.Model):
    purchase_receipt = models.ForeignKey(PurchaseReceipt, on_delete=models.CASCADE, related_name="items")
    purchase_order_item = models.ForeignKey(PurchaseOrderItem, on_delete=models.PROTECT)
    material = models.ForeignKey(Material, on_delete=models.PROTECT)
    received_qty = models.DecimalField(max_digits=14, decimal_places=4)
    accepted_qty = models.DecimalField(max_digits=14, decimal_places=4)
    rejected_qty = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    unit_price = models.DecimalField(max_digits=14, decimal_places=6)
    location = models.ForeignKey(WarehouseLocation, on_delete=models.PROTECT)
    batch = models.ForeignKey(InventoryBatch, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        db_table = "purchase_receipt_items"
        constraints = [
            models.UniqueConstraint(
                fields=["purchase_receipt", "purchase_order_item", "material"],
                name="uq_purchase_receipt_item",
            ),
        ]

    def __str__(self):
        return f"{self.purchase_receipt.purchase_receipt_no} - {self.material} - 合格:{self.accepted_qty}"


class SupplierReturn(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PENDING_APPROVAL = "pending_approval", "待审核"
        REJECTED = "rejected", "已驳回"
        CONFIRMED = "confirmed", "已确认"
        SHIPPED = "shipped", "已退货出库"
        VOIDED = "voided", "已作废"

    supplier_return_no = models.CharField(max_length=100, unique=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT)
    purchase_receipt = models.ForeignKey(PurchaseReceipt, null=True, blank=True, on_delete=models.PROTECT)
    return_date = models.DateField()
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.DRAFT)
    return_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "supplier_returns"

    def __str__(self):
        return f"{self.supplier_return_no} - {self.supplier} - {self.get_status_display()}"


class SupplierReturnItem(models.Model):
    supplier_return = models.ForeignKey(SupplierReturn, on_delete=models.CASCADE, related_name="items")
    purchase_receipt_item = models.ForeignKey(PurchaseReceiptItem, null=True, blank=True, on_delete=models.PROTECT)
    material = models.ForeignKey(Material, on_delete=models.PROTECT)
    return_qty = models.DecimalField(max_digits=14, decimal_places=4)
    unit_price = models.DecimalField(max_digits=14, decimal_places=6)
    return_amount = models.DecimalField(max_digits=14, decimal_places=2)
    batch = models.ForeignKey(InventoryBatch, null=True, blank=True, on_delete=models.PROTECT)
    location = models.ForeignKey(WarehouseLocation, null=True, blank=True, on_delete=models.PROTECT)
    return_reason = models.TextField(blank=True)

    class Meta:
        db_table = "supplier_return_items"
        constraints = [
            models.UniqueConstraint(
                fields=["supplier_return", "material", "purchase_receipt_item"],
                name="uq_supplier_return_item",
            ),
        ]

    def __str__(self):
        return f"{self.supplier_return.supplier_return_no} - {self.material} - {self.return_qty}"
