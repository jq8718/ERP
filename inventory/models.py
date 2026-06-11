from django.conf import settings
from django.db import models

from masterdata.models import Material


class WarehouseLocation(models.Model):
    class LocationStatus(models.TextChoices):
        ACTIVE = "active", "启用"
        INACTIVE = "inactive", "停用"

    location_code = models.CharField(max_length=80, unique=True)
    location_name = models.CharField(max_length=160)
    status = models.CharField(max_length=16, choices=LocationStatus.choices, default=LocationStatus.ACTIVE)
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "warehouse_locations"

    def __str__(self):
        return self.location_code


class InventoryBatch(models.Model):
    class InventoryType(models.TextChoices):
        AVAILABLE = "available", "可用"
        DEFECTIVE = "defective", "不良"
        PENDING = "pending", "待处理"
        SAMPLE = "sample", "样品"

    class BatchStatus(models.TextChoices):
        IN_STOCK = "in_stock", "在库"
        FROZEN = "frozen", "冻结"
        VOIDED = "voided", "作废"
        USED_UP = "used_up", "已用完"

    batch_no = models.CharField(max_length=100, unique=True)
    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name="inventory_batches")
    location = models.ForeignKey(WarehouseLocation, on_delete=models.PROTECT)
    inventory_type = models.CharField(max_length=24, choices=InventoryType.choices, default=InventoryType.AVAILABLE)
    received_at = models.DateTimeField()
    initial_qty = models.DecimalField(max_digits=14, decimal_places=4)
    remaining_qty = models.DecimalField(max_digits=14, decimal_places=4)
    cost_price = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)
    batch_status = models.CharField(max_length=24, choices=BatchStatus.choices, default=BatchStatus.IN_STOCK)

    class Meta:
        db_table = "inventory_batches"
        indexes = [
            models.Index(fields=["material", "location", "received_at", "batch_no"]),
            models.Index(fields=["batch_status", "inventory_type"]),
        ]


class Inventory(models.Model):
    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name="inventory_summaries")
    location = models.ForeignKey(WarehouseLocation, on_delete=models.PROTECT)
    inventory_type = models.CharField(max_length=24, choices=InventoryBatch.InventoryType.choices)
    qty = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "inventory"
        constraints = [
            models.UniqueConstraint(fields=["material", "location", "inventory_type"], name="uq_inventory_material_location_type"),
        ]


class InventoryTransaction(models.Model):
    class TransactionType(models.TextChoices):
        PURCHASE_IN = "purchase_in", "采购入库"
        SALES_OUT = "sales_out", "销售出库"
        PRODUCTION_ISSUE = "production_issue", "生产领料"
        PRODUCTION_RECEIPT = "production_receipt", "生产入库"
        SAMPLE_OUT = "sample_out", "借样出库"
        SAMPLE_RETURN_IN = "sample_return_in", "借样归还入库"
        CUSTOMER_RETURN_IN = "customer_return_in", "客户退货入库"
        SUPPLIER_RETURN_OUT = "supplier_return_out", "供应商退货出库"
        SAMPLE_TO_SALES = "sample_to_sales", "借样转销售"
        LOCATION_TRANSFER = "location_transfer", "库位移库"
        STOCK_ADJUSTMENT = "stock_adjustment", "盘点调整"
        INITIAL_STOCK = "initial_stock", "期初库存"

    transaction_no = models.CharField(max_length=100, unique=True)
    transaction_type = models.CharField(max_length=40, choices=TransactionType.choices)
    material = models.ForeignKey(Material, on_delete=models.PROTECT)
    batch = models.ForeignKey(InventoryBatch, null=True, blank=True, on_delete=models.PROTECT)
    location = models.ForeignKey(WarehouseLocation, on_delete=models.PROTECT)
    qty_delta = models.DecimalField(max_digits=14, decimal_places=4)
    source_doc_type = models.CharField(max_length=80)
    source_doc_id = models.PositiveBigIntegerField()
    source_doc_no = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        db_table = "inventory_transactions"
        indexes = [
            models.Index(fields=["material", "created_at"]),
            models.Index(fields=["source_doc_type", "source_doc_id"]),
        ]


class LocationTransfer(models.Model):
    class TransferStatus(models.TextChoices):
        DRAFT = "draft", "草稿"
        CONFIRMED = "confirmed", "已确认"
        VOIDED = "voided", "已作废"

    transfer_no = models.CharField(max_length=100, unique=True)
    material = models.ForeignKey(Material, on_delete=models.PROTECT)
    batch = models.ForeignKey(InventoryBatch, on_delete=models.PROTECT)
    from_location = models.ForeignKey(WarehouseLocation, on_delete=models.PROTECT, related_name="transfers_out")
    to_location = models.ForeignKey(WarehouseLocation, on_delete=models.PROTECT, related_name="transfers_in")
    transfer_qty = models.DecimalField(max_digits=14, decimal_places=4)
    status = models.CharField(max_length=24, choices=TransferStatus.choices, default=TransferStatus.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "location_transfers"


class StockCount(models.Model):
    class CountStatus(models.TextChoices):
        DRAFT = "draft", "草稿"
        COUNTING = "counting", "盘点中"
        PENDING_APPROVAL = "pending_approval", "待审核"
        APPROVED_PENDING_ADJUSTMENT = "approved_pending_adjustment", "待调整"
        ADJUSTED = "adjusted", "已调整"
        VOIDED = "voided", "已作废"

    stock_count_no = models.CharField(max_length=100, unique=True)
    scope_type = models.CharField(max_length=40)
    scope_value = models.CharField(max_length=200, blank=True)
    snapshot_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=40, choices=CountStatus.choices, default=CountStatus.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        db_table = "stock_counts"


class StockCountItem(models.Model):
    stock_count = models.ForeignKey(StockCount, on_delete=models.CASCADE, related_name="items")
    material = models.ForeignKey(Material, on_delete=models.PROTECT)
    batch = models.ForeignKey(InventoryBatch, null=True, blank=True, on_delete=models.PROTECT)
    location = models.ForeignKey(WarehouseLocation, on_delete=models.PROTECT)
    book_qty = models.DecimalField(max_digits=14, decimal_places=4)
    counted_qty = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    difference_qty = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    difference_reason = models.TextField(blank=True)

    class Meta:
        db_table = "stock_count_items"
        constraints = [
            models.UniqueConstraint(fields=["stock_count", "material", "batch", "location"], name="uq_stock_count_item_scope"),
        ]
