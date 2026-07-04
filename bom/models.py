from django.conf import settings
from django.db import models
from django.db.models import Q

from masterdata.models import Material


class Bom(models.Model):
    class BomStatus(models.TextChoices):
        DRAFT = "draft", "草稿"
        PENDING_APPROVAL = "pending_approval", "待审核"
        REJECTED = "rejected", "已驳回"
        ENABLED = "enabled", "已启用"
        DISABLED = "disabled", "已停用"
        VOIDED = "voided", "已作废"

    bom_no = models.CharField(max_length=100)
    finished_material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name="boms")
    bom_version = models.CharField(max_length=40)
    base_qty = models.DecimalField(max_digits=14, decimal_places=4, default=1)
    status = models.CharField(max_length=32, choices=BomStatus.choices, default=BomStatus.DRAFT)
    effective_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    is_default = models.BooleanField(default=False)
    enabled_at = models.DateTimeField(null=True, blank=True)
    disabled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    version = models.PositiveIntegerField(default=1)
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "boms"
        constraints = [
            models.UniqueConstraint(fields=["bom_no"], name="uq_bom_no"),
            models.UniqueConstraint(
                fields=["finished_material", "bom_version"],
                condition=~Q(status__in=["rejected", "voided"]),
                name="uq_active_bom_version",
            ),
        ]
        indexes = [
            models.Index(fields=["finished_material", "status"]),
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self):
        return f"{self.bom_no} {self.bom_version}"


class BomItem(models.Model):
    bom = models.ForeignKey(Bom, on_delete=models.CASCADE, related_name="items")
    line_no = models.PositiveIntegerField()
    component_material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name="used_in_bom_items")
    usage_qty = models.DecimalField(max_digits=14, decimal_places=6)
    usage_unit = models.CharField(max_length=32)
    loss_rate = models.DecimalField(max_digits=7, decimal_places=6, default=0)
    is_required = models.BooleanField(default=True)
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "bom_items"
        constraints = [
            models.UniqueConstraint(fields=["bom", "line_no"], name="uq_bom_item_line"),
        ]
        indexes = [
            models.Index(fields=["component_material"]),
        ]

    def __str__(self):
        return f"{self.bom.bom_no} 第{self.line_no}行 - {self.component_material}"
