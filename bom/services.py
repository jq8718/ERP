from decimal import Decimal, ROUND_CEILING

from django.db import transaction
from django.utils import timezone

from masterdata.models import Material, MaterialUnitConversion
from system.services import ServiceResult

from .models import Bom, BomItem


class UnitConversionMissing(Exception):
    pass


def enable_bom(bom_id: int, operator_id: int, make_default: bool = True) -> ServiceResult:
    try:
        with transaction.atomic():
            bom = Bom.objects.select_for_update().prefetch_related("items").get(id=bom_id)
            if bom.status == Bom.BomStatus.ENABLED:
                return ServiceResult(False, "STATE_ALREADY_PROCESSED", "BOM 已经启用")
            if bom.status == Bom.BomStatus.VOIDED:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "已作废 BOM 不能启用")
            if not bom.items.exists():
                return ServiceResult(False, "DOC_NOT_FOUND", "BOM 启用前必须至少有一条明细")

            should_be_default = make_default or not Bom.objects.filter(
                finished_material=bom.finished_material,
                status=Bom.BomStatus.ENABLED,
                is_default=True,
            ).exclude(id=bom.id).exists()
            if should_be_default:
                Bom.objects.select_for_update().filter(
                    finished_material=bom.finished_material,
                    status=Bom.BomStatus.ENABLED,
                    is_default=True,
                ).exclude(id=bom.id).update(is_default=False, updated_by_id=operator_id)

            now = timezone.now()
            bom.status = Bom.BomStatus.ENABLED
            bom.is_default = should_be_default
            bom.enabled_at = now
            bom.disabled_at = None
            bom.approved_by_id = operator_id
            bom.approved_at = now
            bom.updated_by_id = operator_id
            bom.version += 1
            bom.save(
                update_fields=[
                    "status",
                    "is_default",
                    "enabled_at",
                    "disabled_at",
                    "approved_by",
                    "approved_at",
                    "updated_by",
                    "updated_at",
                    "version",
                ]
            )
    except Bom.DoesNotExist:
        return ServiceResult(False, "DOC_NOT_FOUND", "BOM 不存在")

    return ServiceResult(True, message="BOM 已启用", data={"bom_id": bom_id}, next_action="view_detail")


def disable_bom(bom_id: int, operator_id: int) -> ServiceResult:
    try:
        with transaction.atomic():
            bom = Bom.objects.select_for_update().get(id=bom_id)
            if bom.status != Bom.BomStatus.ENABLED:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "只有已启用 BOM 可以停用")

            bom.status = Bom.BomStatus.DISABLED
            bom.is_default = False
            bom.disabled_at = timezone.now()
            bom.updated_by_id = operator_id
            bom.version += 1
            bom.save(update_fields=["status", "is_default", "disabled_at", "updated_by", "updated_at", "version"])
    except Bom.DoesNotExist:
        return ServiceResult(False, "DOC_NOT_FOUND", "BOM 不存在")

    return ServiceResult(True, message="BOM 已停用", data={"bom_id": bom_id}, next_action="view_detail")


def required_component_qty_base(bom_item: BomItem, production_qty: Decimal) -> Decimal:
    material = bom_item.component_material
    base_qty = bom_item.bom.base_qty or Decimal("1")
    if base_qty <= 0:
        raise ValueError("BOM 基准数量必须大于 0")
    required_in_bom_unit = (bom_item.usage_qty / base_qty) * production_qty * (Decimal("1") + bom_item.loss_rate)
    required_in_base_unit = convert_qty(required_in_bom_unit, material, bom_item.usage_unit, material.base_unit)
    return round_qty(required_in_base_unit, material.qty_precision)


def convert_qty(qty: Decimal, material: Material, source_unit: str, target_unit: str) -> Decimal:
    if source_unit == target_unit:
        return qty
    conversion = MaterialUnitConversion.objects.filter(
        material=material,
        source_unit=source_unit,
        target_unit=target_unit,
        status=MaterialUnitConversion.ConversionStatus.ACTIVE,
    ).first()
    if conversion is None:
        raise UnitConversionMissing(f"{material.material_code}: {source_unit} -> {target_unit}")
    return qty * conversion.ratio


def round_qty(qty: Decimal, precision: int) -> Decimal:
    quantum = Decimal("1").scaleb(-precision)
    return qty.quantize(quantum, rounding=ROUND_CEILING)
