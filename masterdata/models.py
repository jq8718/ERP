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
        return f"{self.material_code} {self.material_name}"


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


class Customer(TimeStampedModel):
    class CustomerStatus(models.TextChoices):
        ACTIVE = "active", "启用"
        INACTIVE = "inactive", "停用"
        BLACKLIST = "blacklist", "黑名单"

    customer_no = models.CharField(max_length=80, unique=True)
    customer_name = models.CharField(max_length=200)
    short_name = models.CharField(max_length=120, blank=True)
    sales_owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    settlement_method = models.CharField(max_length=80, blank=True)
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


class Supplier(TimeStampedModel):
    class SupplierStatus(models.TextChoices):
        ACTIVE = "active", "启用"
        INACTIVE = "inactive", "停用"
        BLACKLIST = "blacklist", "黑名单"

    supplier_no = models.CharField(max_length=80, unique=True)
    supplier_name = models.CharField(max_length=200)
    contact_name = models.CharField(max_length=120, blank=True)
    contact_phone_encrypted = models.TextField(blank=True)
    supplier_type = models.CharField(max_length=80, blank=True)
    payment_method = models.CharField(max_length=80, blank=True)
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
