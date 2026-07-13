from django.conf import settings
from django.db import models


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    version = models.PositiveIntegerField(default=1)

    class Meta:
        abstract = True


class SettlementMethod(models.TextChoices):
    CASH = "现结", "现结"
    MONTHLY = "月结", "月结"
    MONTHLY_30 = "月结30天", "月结30天"
    MONTHLY_60 = "月结60天", "月结60天"
    PREPAID = "预付", "预付"
    PAYMENT_BEFORE_SHIPMENT = "款到发货", "款到发货"
    CASH_ON_DELIVERY = "货到付款", "货到付款"
    AFTER_RECONCILIATION = "对账后付款", "对账后付款"


class SupplierType(models.TextChoices):
    RAW = "原料", "原料"
    AUXILIARY = "辅料", "辅料"
    PART = "配件", "配件"
    PACKAGING = "包装", "包装"
    OUTSOURCING = "外协加工", "外协加工"
    TRANSPORT = "运输", "运输"
    EQUIPMENT = "设备", "设备"
    SERVICE = "服务", "服务"
    OTHER = "其他", "其他"


class SupplierPaymentMethod(models.TextChoices):
    CASH = "现金", "现金"
    TRANSFER = "转账", "转账"
    CHECK = "支票", "支票"
    PAY_NOW = "现付", "现付"
    MONTHLY = "月结", "月结"
    MONTHLY_30 = "月结30天", "月结30天"
    MONTHLY_60 = "月结60天", "月结60天"
    PREPAID = "预付", "预付"
    CASH_ON_DELIVERY = "货到付款", "货到付款"
    AFTER_RECONCILIATION = "对账后付款", "对账后付款"
    OTHER = "其他", "其他"


class Material(TimeStampedModel):
    class MaterialType(models.TextChoices):
        FINISHED = "finished", "成品"
        RAW = "raw", "原料"
        PART = "part", "配件"
        PACKAGING = "packaging", "包装材料"
        OTHER = "other", "其他"

    class MaterialStatus(models.TextChoices):
        ACTIVE = "active", "启用"
        INACTIVE = "inactive", "停用"

    material_code = models.CharField(max_length=80, unique=True)
    material_name = models.CharField(max_length=200)
    material_type = models.CharField(max_length=24, choices=MaterialType.choices)
    spec = models.CharField(max_length=200, blank=True)
    base_unit = models.CharField(max_length=32)
    qty_precision = models.PositiveSmallIntegerField(default=0)
    min_stock_qty = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    latest_purchase_price = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)
    status = models.CharField(max_length=16, choices=MaterialStatus.choices, default=MaterialStatus.ACTIVE)
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "materials"
        indexes = [
            models.Index(fields=["material_type", "status"]),
            models.Index(fields=["material_name"]),
        ]

    def __str__(self):
        parts = [self.material_code, self.material_name]
        if self.spec:
            parts.append(f"规格型号：{self.spec}")
        if self.base_unit:
            parts.append(f"单位：{self.base_unit}")
        return "｜".join(part for part in parts if part)


class MaterialUnitConversion(TimeStampedModel):
    class ConversionStatus(models.TextChoices):
        ACTIVE = "active", "启用"
        INACTIVE = "inactive", "停用"

    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name="unit_conversions")
    source_unit = models.CharField(max_length=32)
    target_unit = models.CharField(max_length=32)
    ratio = models.DecimalField(max_digits=18, decimal_places=8)
    status = models.CharField(max_length=16, choices=ConversionStatus.choices, default=ConversionStatus.ACTIVE)

    class Meta:
        db_table = "material_unit_conversions"
        constraints = [
            models.UniqueConstraint(fields=["material", "source_unit", "target_unit"], name="uq_material_unit_conversion"),
        ]

    def __str__(self):
        return f"{self.material} - {self.source_unit}->{self.target_unit} = {self.ratio}"


class Customer(TimeStampedModel):
    class CustomerStatus(models.TextChoices):
        ACTIVE = "active", "启用"
        INACTIVE = "inactive", "停用"
        BLACKLIST = "blacklist", "黑名单"

    customer_no = models.CharField(max_length=80, unique=True)
    customer_name = models.CharField(max_length=200)
    short_name = models.CharField(max_length=120, blank=True)
    sales_owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    settlement_method = models.CharField(max_length=80, choices=SettlementMethod.choices, blank=True)
    contact_phone_encrypted = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=CustomerStatus.choices, default=CustomerStatus.ACTIVE)
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "customers"
        indexes = [
            models.Index(fields=["customer_name"]),
            models.Index(fields=["sales_owner", "status"]),
        ]

    def __str__(self):
        return self.customer_name


class CustomerProduct(TimeStampedModel):
    class ProductStatus(models.TextChoices):
        ACTIVE = "active", "启用"
        INACTIVE = "inactive", "停用"

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="products")
    customer_product_no = models.CharField(max_length=80)
    customer_product_name = models.CharField(max_length=200)
    finished_material = models.ForeignKey(Material, null=True, blank=True, on_delete=models.PROTECT)
    default_sale_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    label_requirements = models.JSONField(default=dict, blank=True)
    packaging_requirements = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=ProductStatus.choices, default=ProductStatus.ACTIVE)

    class Meta:
        db_table = "customer_products"
        constraints = [
            models.UniqueConstraint(fields=["customer", "customer_product_no"], name="uq_customer_product_no"),
        ]

    @property
    def label_requirements_display(self):
        return _requirements_display(self.label_requirements)

    @property
    def packaging_requirements_display(self):
        return _requirements_display(self.packaging_requirements)

    def __str__(self):
        parts = [
            self.customer.customer_name if self.customer_id else "",
            self.customer_product_no,
            self.customer_product_name,
        ]
        if self.finished_material_id:
            parts.append(f"成品:{self.finished_material}")
        return " - ".join(part for part in parts if part)


def _requirements_display(value):
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if set(value.keys()) == {"说明"}:
            return value["说明"]
        return "\n".join(f"{key}: {item}" for key, item in value.items())
    if isinstance(value, list):
        return "\n".join(str(item) for item in value)
    return str(value)


class CustomerAddress(TimeStampedModel):
    class AddressType(models.TextChoices):
        SHIPPING = "shipping", "收货地址"
        RETURN = "return", "退货地址"
        SAMPLE = "sample", "样品地址"

    class AddressStatus(models.TextChoices):
        ACTIVE = "active", "启用"
        INACTIVE = "inactive", "停用"

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="addresses")
    address_type = models.CharField(max_length=24, choices=AddressType.choices, default=AddressType.SHIPPING)
    receiver_name = models.CharField(max_length=120)
    receiver_phone_encrypted = models.TextField(blank=True)
    address_encrypted = models.TextField(blank=True)
    is_default = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=AddressStatus.choices, default=AddressStatus.ACTIVE)

    class Meta:
        db_table = "customer_addresses"
        indexes = [
            models.Index(fields=["customer", "status"]),
            models.Index(fields=["customer", "address_type", "status"]),
        ]

    def __str__(self):
        parts = [
            self.customer.customer_name if self.customer_id else "",
            self.get_address_type_display(),
            self.receiver_name,
            self.address_encrypted,
        ]
        label = " - ".join(part for part in parts if part)
        if self.is_default:
            label = f"{label}（默认）"
        return label


class Supplier(TimeStampedModel):
    class SupplierStatus(models.TextChoices):
        ACTIVE = "active", "启用"
        INACTIVE = "inactive", "停用"
        BLACKLIST = "blacklist", "黑名单"

    supplier_no = models.CharField(max_length=80, unique=True)
    supplier_name = models.CharField(max_length=200)
    contact_name = models.CharField(max_length=120, blank=True)
    contact_phone_encrypted = models.TextField(blank=True)
    supplier_type = models.CharField(max_length=80, choices=SupplierType.choices, blank=True)
    payment_method = models.CharField(max_length=80, choices=SupplierPaymentMethod.choices, blank=True)
    status = models.CharField(max_length=16, choices=SupplierStatus.choices, default=SupplierStatus.ACTIVE)
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "suppliers"
        indexes = [
            models.Index(fields=["supplier_name"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return self.supplier_name


class MaterialSupplierPrice(TimeStampedModel):
    class PriceStatus(models.TextChoices):
        ACTIVE = "active", "启用"
        INACTIVE = "inactive", "停用"

    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name="supplier_prices")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="material_prices")
    purchase_price = models.DecimalField(max_digits=14, decimal_places=6)
    currency = models.CharField(max_length=12, default="CNY")
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)
    is_default = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=PriceStatus.choices, default=PriceStatus.ACTIVE)

    class Meta:
        db_table = "material_supplier_prices"
        indexes = [
            models.Index(fields=["material", "supplier", "status"]),
            models.Index(fields=["is_default", "status"]),
        ]

    def __str__(self):
        return f"{self.material} - {self.supplier} - {self.purchase_price}"
