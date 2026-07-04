from django.conf import settings
from django.db import models

from bom.models import Bom
from inventory.models import InventoryBatch, WarehouseLocation
from masterdata.models import Material
from sales.models import SalesOrderItem


class ProductionOrder(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待生产"
        IN_PROGRESS = "in_progress", "生产中"
        COMPLETED = "completed", "已完成"
        CANCELLED = "cancelled", "已取消"

    production_order_no = models.CharField(max_length=100, unique=True)
    sales_order_item = models.ForeignKey(
        SalesOrderItem,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="production_orders",
    )
    finished_material = models.ForeignKey(Material, on_delete=models.PROTECT)
    production_qty = models.DecimalField(max_digits=14, decimal_places=4)
    received_qty = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    locked_bom = models.ForeignKey(Bom, on_delete=models.PROTECT)
    locked_bom_version = models.CharField(max_length=40)
    label_requirements = models.JSONField(default=dict, blank=True)
    packaging_requirements = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.PENDING)
    planned_start_date = models.DateField(null=True, blank=True)
    planned_finish_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    version = models.PositiveIntegerField(default=1)
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "production_orders"
        indexes = [
            models.Index(fields=["sales_order_item", "status"]),
            models.Index(fields=["finished_material", "status"]),
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self):
        return f"{self.production_order_no} - {self.finished_material} - {self.get_status_display()}"


class ProductionMaterialRequisition(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PENDING_CONFIRM = "pending_confirm", "待确认"
        ISSUED = "issued", "已出库"
        VOIDED = "voided", "已作废"

    requisition_no = models.CharField(max_length=100, unique=True)
    production_order = models.ForeignKey(
        ProductionOrder,
        on_delete=models.PROTECT,
        related_name="material_requisitions",
    )
    requisition_date = models.DateField()
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "production_material_requisitions"
        indexes = [
            models.Index(fields=["production_order", "status"]),
            models.Index(fields=["requisition_date"]),
        ]

    def __str__(self):
        return f"{self.requisition_no} - {self.production_order.production_order_no} - {self.get_status_display()}"


class ProductionMaterialRequisitionItem(models.Model):
    requisition = models.ForeignKey(ProductionMaterialRequisition, on_delete=models.CASCADE, related_name="items")
    production_order = models.ForeignKey(ProductionOrder, on_delete=models.PROTECT)
    line_no = models.PositiveIntegerField()
    material = models.ForeignKey(Material, on_delete=models.PROTECT)
    required_qty = models.DecimalField(max_digits=14, decimal_places=4)
    issued_qty = models.DecimalField(max_digits=14, decimal_places=4)
    batch = models.ForeignKey(InventoryBatch, on_delete=models.PROTECT)
    location = models.ForeignKey(WarehouseLocation, on_delete=models.PROTECT)
    adjust_reason = models.TextField(blank=True)

    class Meta:
        db_table = "production_material_requisition_items"
        constraints = [
            models.UniqueConstraint(fields=["requisition", "line_no"], name="uq_production_requisition_line"),
        ]
        indexes = [
            models.Index(fields=["production_order", "material"]),
            models.Index(fields=["batch", "location"]),
        ]

    def __str__(self):
        return f"{self.requisition.requisition_no} 第{self.line_no}行 - {self.material}"


class ProductionReceipt(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PENDING_CONFIRM = "pending_confirm", "待确认"
        RECEIVED = "received", "已入库"
        VOIDED = "voided", "已作废"

    production_receipt_no = models.CharField(max_length=100, unique=True)
    production_order = models.ForeignKey(ProductionOrder, on_delete=models.PROTECT, related_name="receipts")
    receipt_date = models.DateField()
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "production_receipts"
        indexes = [
            models.Index(fields=["production_order", "status"]),
            models.Index(fields=["receipt_date"]),
        ]

    def __str__(self):
        return f"{self.production_receipt_no} - {self.production_order.production_order_no} - {self.get_status_display()}"


class ProductionReceiptItem(models.Model):
    class QualityStatus(models.TextChoices):
        QUALIFIED = "qualified", "合格"
        PENDING = "pending", "待检"
        DEFECTIVE = "defective", "不良"

    production_receipt = models.ForeignKey(ProductionReceipt, on_delete=models.CASCADE, related_name="items")
    production_order = models.ForeignKey(ProductionOrder, on_delete=models.PROTECT)
    line_no = models.PositiveIntegerField()
    finished_material = models.ForeignKey(Material, on_delete=models.PROTECT)
    receipt_qty = models.DecimalField(max_digits=14, decimal_places=4)
    location = models.ForeignKey(WarehouseLocation, on_delete=models.PROTECT)
    batch = models.ForeignKey(InventoryBatch, null=True, blank=True, on_delete=models.PROTECT)
    batch_no = models.CharField(max_length=100, blank=True)
    quality_status = models.CharField(
        max_length=24,
        choices=QualityStatus.choices,
        default=QualityStatus.QUALIFIED,
    )

    class Meta:
        db_table = "production_receipt_items"
        constraints = [
            models.UniqueConstraint(fields=["production_receipt", "line_no"], name="uq_production_receipt_line"),
        ]
        indexes = [
            models.Index(fields=["production_order", "finished_material"]),
            models.Index(fields=["batch"]),
        ]

    def __str__(self):
        return f"{self.production_receipt.production_receipt_no} 第{self.line_no}行 - {self.finished_material}"
