from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import TextIOBase

from django.db import transaction
from django.db.models import Sum as models_sum
from django.utils import timezone

from bom.models import Bom
from files.models import ImportJob
from files.services import CsvImportReadError, read_csv_dict_rows
from inventory.models import InventoryBatch, WarehouseLocation
from masterdata.models import Material
from sales.models import SalesOrder, SalesOrderItem
from system.services import ServiceResult, next_document_no

from .models import (
    ProductionMaterialRequisition,
    ProductionMaterialRequisitionItem,
    ProductionOrder,
    ProductionReceipt,
    ProductionReceiptItem,
)


PRODUCTION_ORDER_IMPORT_COLUMNS = (
    "production_order_no",
    "sales_order_no",
    "sales_order_line_no",
    "material_code",
    "production_qty",
    "bom_no",
    "bom_version",
    "planned_start_date",
    "planned_finish_date",
    "remark",
)

PRODUCTION_ORDER_IMPORT_TEMPLATE_ROWS = (
    PRODUCTION_ORDER_IMPORT_COLUMNS,
    ("MO-INIT-001", "SO001", "1", "FG001", "10", "", "", "2026-06-10", "2026-06-15", "示例行，导入前可删除"),
    ("MO-INIT-002", "", "", "FG002", "5", "BOM-FG002", "V1", "2026-06-12", "2026-06-18", ""),
)

MATERIAL_REQUISITION_IMPORT_COLUMNS = (
    "requisition_no",
    "production_order_no",
    "requisition_date",
    "material_code",
    "required_qty",
    "issued_qty",
    "batch_no",
    "location_code",
    "adjust_reason",
    "remark",
)

MATERIAL_REQUISITION_IMPORT_TEMPLATE_ROWS = (
    MATERIAL_REQUISITION_IMPORT_COLUMNS,
    ("MR-INIT-001", "MO001", "2026-06-10", "RM001", "20", "20", "BATCH001", "A01", "", "示例行，导入前可删除"),
    ("MR-INIT-001", "MO001", "2026-06-10", "RM002", "5", "4", "BATCH002", "A01", "少领 1", ""),
)

PRODUCTION_RECEIPT_IMPORT_COLUMNS = (
    "production_receipt_no",
    "production_order_no",
    "receipt_date",
    "receipt_qty",
    "location_code",
    "batch_no",
    "quality_status",
    "remark",
)

PRODUCTION_RECEIPT_IMPORT_TEMPLATE_ROWS = (
    PRODUCTION_RECEIPT_IMPORT_COLUMNS,
    ("PI-INIT-001", "MO001", "2026-06-10", "10", "A01", "FG-BATCH-001", "qualified", "示例行，导入前可删除"),
    ("PI-INIT-002", "MO002", "2026-06-11", "5", "A01", "", "pending", ""),
)

ZERO = Decimal("0")


def import_production_orders_from_csv(file_obj: TextIOBase, operator_id: int | None = None) -> ServiceResult:
    job = _start_import_job("production_orders", operator_id)
    try:
        rows = read_csv_dict_rows(file_obj)
        errors, validated_rows = _validate_production_order_rows(rows)
        if errors:
            return _validation_failed(job, "生产指令导入校验失败", errors)

        job.status = ImportJob.JobStatus.IMPORTING
        job.save(update_fields=["status"])
        created_count = 0
        with transaction.atomic():
            for row in validated_rows:
                ProductionOrder.objects.create(
                    production_order_no=row["production_order_no"] or next_document_no("MO"),
                    sales_order_item=row["sales_order_item"],
                    finished_material=row["finished_material"],
                    production_qty=row["production_qty"],
                    locked_bom=row["locked_bom"],
                    locked_bom_version=row["locked_bom"].bom_version,
                    status=ProductionOrder.Status.PENDING,
                    planned_start_date=row["planned_start_date"] or timezone.localdate(),
                    planned_finish_date=row["planned_finish_date"],
                    created_by_id=operator_id,
                    updated_by_id=operator_id,
                    remark=row["remark"],
                )
                created_count += 1
        return _import_success(job, created_count, "生产指令导入完成")
    except UnicodeDecodeError:
        return _fail_import_job(job, "导入文件编码错误，请使用 UTF-8 CSV")
    except CsvImportReadError as exc:
        return _fail_import_job(job, str(exc), exc.error_code)
    except Exception as exc:
        return _fail_import_job(job, f"生产指令导入失败：{exc}", "FILE_IMPORT_FAILED")


def import_material_requisitions_from_csv(file_obj: TextIOBase, operator_id: int | None = None) -> ServiceResult:
    job = _start_import_job("production_material_requisitions", operator_id)
    try:
        rows = read_csv_dict_rows(file_obj)
        errors, grouped_rows = _validate_material_requisition_rows(rows)
        if errors:
            return _validation_failed(job, "生产领料导入校验失败", errors)

        job.status = ImportJob.JobStatus.IMPORTING
        job.save(update_fields=["status"])
        created_count = 0
        with transaction.atomic():
            for group in grouped_rows.values():
                requisition = ProductionMaterialRequisition.objects.create(
                    requisition_no=group["requisition_no"] or next_document_no("MR"),
                    production_order=group["production_order"],
                    requisition_date=group["requisition_date"],
                    status=ProductionMaterialRequisition.Status.PENDING_CONFIRM,
                    created_by_id=operator_id,
                    remark=group["remark"],
                )
                for line_no, line in enumerate(group["items"], start=1):
                    ProductionMaterialRequisitionItem.objects.create(
                        requisition=requisition,
                        production_order=group["production_order"],
                        line_no=line_no,
                        material=line["material"],
                        required_qty=line["required_qty"],
                        issued_qty=line["issued_qty"],
                        batch=line["batch"],
                        location=line["batch"].location,
                        adjust_reason=line["adjust_reason"],
                    )
                created_count += 1
        return _import_success(job, created_count, "生产领料导入完成")
    except UnicodeDecodeError:
        return _fail_import_job(job, "导入文件编码错误，请使用 UTF-8 CSV")
    except CsvImportReadError as exc:
        return _fail_import_job(job, str(exc), exc.error_code)
    except Exception as exc:
        return _fail_import_job(job, f"生产领料导入失败：{exc}", "FILE_IMPORT_FAILED")


def import_production_receipts_from_csv(file_obj: TextIOBase, operator_id: int | None = None) -> ServiceResult:
    job = _start_import_job("production_receipts", operator_id)
    try:
        rows = read_csv_dict_rows(file_obj)
        errors, grouped_rows = _validate_production_receipt_rows(rows)
        if errors:
            return _validation_failed(job, "生产入库导入校验失败", errors)

        job.status = ImportJob.JobStatus.IMPORTING
        job.save(update_fields=["status"])
        created_count = 0
        with transaction.atomic():
            for group in grouped_rows.values():
                receipt = ProductionReceipt.objects.create(
                    production_receipt_no=group["production_receipt_no"] or next_document_no("PI"),
                    production_order=group["production_order"],
                    receipt_date=group["receipt_date"],
                    status=ProductionReceipt.Status.PENDING_CONFIRM,
                    created_by_id=operator_id,
                    remark=group["remark"],
                )
                for line_no, line in enumerate(group["items"], start=1):
                    ProductionReceiptItem.objects.create(
                        production_receipt=receipt,
                        production_order=group["production_order"],
                        line_no=line_no,
                        finished_material=group["production_order"].finished_material,
                        receipt_qty=line["receipt_qty"],
                        location=line["location"],
                        batch_no=line["batch_no"],
                        quality_status=line["quality_status"],
                    )
                created_count += 1
        return _import_success(job, created_count, "生产入库导入完成")
    except UnicodeDecodeError:
        return _fail_import_job(job, "导入文件编码错误，请使用 UTF-8 CSV")
    except CsvImportReadError as exc:
        return _fail_import_job(job, str(exc), exc.error_code)
    except Exception as exc:
        return _fail_import_job(job, f"生产入库导入失败：{exc}", "FILE_IMPORT_FAILED")


def _validate_production_order_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    errors = []
    validated_rows = []
    if not rows:
        return [{"row": 0, "field": "", "message": "导入文件没有数据行"}], validated_rows

    production_order_nos = {_clean(row.get("production_order_no")) for row in rows if _clean(row.get("production_order_no"))}
    material_codes = {_clean(row.get("material_code")) for row in rows if _clean(row.get("material_code"))}
    sales_order_nos = {_clean(row.get("sales_order_no")) for row in rows if _clean(row.get("sales_order_no"))}
    bom_nos = {_clean(row.get("bom_no")) for row in rows if _clean(row.get("bom_no"))}
    bom_versions = {_clean(row.get("bom_version")) for row in rows if _clean(row.get("bom_version"))}

    existing_order_nos = set(
        ProductionOrder.objects.filter(production_order_no__in=production_order_nos).values_list("production_order_no", flat=True)
    )
    materials = {material.material_code: material for material in Material.objects.filter(material_code__in=material_codes)}
    sales_orders = {
        order.sales_order_no: order
        for order in SalesOrder.objects.filter(sales_order_no__in=sales_order_nos)
    }
    sales_items = {
        (item.sales_order.sales_order_no, item.line_no): item
        for item in SalesOrderItem.objects.select_related("sales_order", "finished_material", "locked_bom").filter(
            sales_order__sales_order_no__in=sales_order_nos
        )
    }
    boms = _candidate_boms(bom_nos, bom_versions, material_codes)
    seen_order_nos = set()
    pending_qty_by_sales_item = {}

    for index, row in enumerate(rows, start=2):
        order_no = _clean(row.get("production_order_no"))
        sales_order_no = _clean(row.get("sales_order_no"))
        sales_order_line_no = _clean(row.get("sales_order_line_no"))
        material_code = _clean(row.get("material_code"))
        bom_no = _clean(row.get("bom_no"))
        bom_version = _clean(row.get("bom_version"))

        if order_no:
            if order_no in existing_order_nos:
                errors.append({"row": index, "field": "production_order_no", "message": "生产指令单号已存在"})
            if order_no in seen_order_nos:
                errors.append({"row": index, "field": "production_order_no", "message": "导入文件中生产指令单号不能重复"})
            seen_order_nos.add(order_no)

        sales_order_item = None
        if sales_order_no or sales_order_line_no:
            sales_order = sales_orders.get(sales_order_no)
            if not sales_order_no:
                errors.append({"row": index, "field": "sales_order_no", "message": "填写销售订单行号时必须填写销售订单号"})
            elif not sales_order or sales_order.status not in [SalesOrder.Status.CONFIRMED, SalesOrder.Status.IN_PRODUCTION]:
                errors.append({"row": index, "field": "sales_order_no", "message": "来源销售订单不存在或不允许生成生产指令"})
            if not sales_order_line_no:
                errors.append({"row": index, "field": "sales_order_line_no", "message": "关联销售订单时必须填写销售订单行号"})
            elif not sales_order_line_no.isdigit():
                errors.append({"row": index, "field": "sales_order_line_no", "message": "销售订单行号必须是正整数"})
            elif sales_order_no:
                sales_order_item = sales_items.get((sales_order_no, int(sales_order_line_no)))
                if not sales_order_item:
                    errors.append({"row": index, "field": "sales_order_line_no", "message": "来源销售订单行不存在"})
                elif sales_order_item.line_status not in [
                    SalesOrderItem.LineStatus.CONFIRMED,
                    SalesOrderItem.LineStatus.IN_PRODUCTION,
                ]:
                    errors.append({"row": index, "field": "sales_order_line_no", "message": "来源销售订单行状态不允许生成生产指令"})
                elif not sales_order_item.locked_bom_id:
                    errors.append({"row": index, "field": "sales_order_line_no", "message": "来源销售订单行未锁定 BOM，不能导入生产指令"})

        finished_material = materials.get(material_code)
        if sales_order_item:
            if material_code and finished_material and finished_material.id != sales_order_item.finished_material_id:
                errors.append({"row": index, "field": "material_code", "message": "生产成品必须与来源销售订单行一致"})
            finished_material = sales_order_item.finished_material
        elif not material_code:
            errors.append({"row": index, "field": "material_code", "message": "未关联销售订单行时成品编码不能为空"})
        elif not finished_material or finished_material.status != Material.MaterialStatus.ACTIVE:
            errors.append({"row": index, "field": "material_code", "message": "成品物料不存在或未启用"})
        elif finished_material.material_type != Material.MaterialType.FINISHED:
            errors.append({"row": index, "field": "material_code", "message": "生产物料必须是成品"})

        production_qty = _parse_decimal(row.get("production_qty"))
        if production_qty is None or production_qty <= ZERO:
            errors.append({"row": index, "field": "production_qty", "message": "生产数量必须大于 0"})
        if sales_order_item and production_qty is not None and production_qty > ZERO:
            existing_qty = (
                ProductionOrder.objects.filter(sales_order_item=sales_order_item)
                .exclude(status=ProductionOrder.Status.CANCELLED)
                .aggregate(total=models_sum("production_qty"))
                .get("total")
                or ZERO
            )
            pending_qty = pending_qty_by_sales_item.get(sales_order_item.id, ZERO)
            remaining_qty = sales_order_item.order_qty - existing_qty - pending_qty
            if production_qty > remaining_qty:
                errors.append({"row": index, "field": "production_qty", "message": "生产数量不能超过来源销售订单行剩余未排产数量"})

        planned_start_date_text = _clean(row.get("planned_start_date"))
        planned_start_date = _parse_date(planned_start_date_text) if planned_start_date_text else None
        if planned_start_date_text and not planned_start_date:
            errors.append({"row": index, "field": "planned_start_date", "message": "计划开始日期格式错误，应为 YYYY-MM-DD"})
        planned_finish_date_text = _clean(row.get("planned_finish_date"))
        planned_finish_date = _parse_date(planned_finish_date_text) if planned_finish_date_text else None
        if planned_finish_date_text and not planned_finish_date:
            errors.append({"row": index, "field": "planned_finish_date", "message": "计划完成日期格式错误，应为 YYYY-MM-DD"})
        if planned_start_date and planned_finish_date and planned_finish_date < planned_start_date:
            errors.append({"row": index, "field": "planned_finish_date", "message": "计划完成日期不能早于计划开始日期"})

        locked_bom = None
        if sales_order_item and sales_order_item.locked_bom_id:
            locked_bom = sales_order_item.locked_bom
            if bom_no and bom_no != locked_bom.bom_no:
                errors.append({"row": index, "field": "bom_no", "message": "关联销售订单行时必须使用该行锁定 BOM"})
            if bom_version and bom_version != locked_bom.bom_version:
                errors.append({"row": index, "field": "bom_version", "message": "关联销售订单行时必须使用该行锁定 BOM 版本"})
        elif finished_material:
            locked_bom = _resolve_manual_bom(finished_material, bom_no, bom_version, boms)
            if not locked_bom:
                errors.append({"row": index, "field": "bom_no", "message": "未找到该成品可用的启用 BOM"})

        if errors and any(error["row"] == index for error in errors):
            continue

        validated_rows.append(
            {
                "production_order_no": order_no,
                "sales_order_item": sales_order_item,
                "finished_material": finished_material,
                "production_qty": production_qty,
                "locked_bom": locked_bom,
                "planned_start_date": planned_start_date,
                "planned_finish_date": planned_finish_date,
                "remark": _clean(row.get("remark")),
            }
        )
        if sales_order_item:
            pending_qty_by_sales_item[sales_order_item.id] = pending_qty_by_sales_item.get(sales_order_item.id, ZERO) + production_qty

    return errors, validated_rows


def _validate_material_requisition_rows(rows: list[dict]) -> tuple[list[dict], OrderedDict]:
    errors = []
    grouped_rows = OrderedDict()
    if not rows:
        return [{"row": 0, "field": "", "message": "导入文件没有数据行"}], grouped_rows

    requisition_nos = {_clean(row.get("requisition_no")) for row in rows if _clean(row.get("requisition_no"))}
    production_order_nos = {_clean(row.get("production_order_no")) for row in rows if _clean(row.get("production_order_no"))}
    material_codes = {_clean(row.get("material_code")) for row in rows if _clean(row.get("material_code"))}
    batch_nos = {_clean(row.get("batch_no")) for row in rows if _clean(row.get("batch_no"))}
    location_codes = {_clean(row.get("location_code")) for row in rows if _clean(row.get("location_code"))}

    existing_requisition_nos = set(
        ProductionMaterialRequisition.objects.filter(requisition_no__in=requisition_nos).values_list("requisition_no", flat=True)
    )
    production_orders = {
        order.production_order_no: order
        for order in ProductionOrder.objects.select_related("locked_bom").filter(production_order_no__in=production_order_nos)
    }
    existing_requisition_order_ids = set(
        ProductionMaterialRequisition.objects.exclude(status=ProductionMaterialRequisition.Status.VOIDED).filter(
            production_order__production_order_no__in=production_order_nos
        ).values_list("production_order_id", flat=True)
    )
    bom_material_ids_by_order = {
        order.id: set(
            order.locked_bom.items.filter(component_material__material_code__in=material_codes).values_list("component_material_id", flat=True)
        )
        for order in production_orders.values()
    }
    materials = {material.material_code: material for material in Material.objects.filter(material_code__in=material_codes)}
    batches = {
        batch.batch_no: batch
        for batch in InventoryBatch.objects.select_related("material", "location").filter(batch_no__in=batch_nos)
    }
    locations = {
        location.location_code: location
        for location in WarehouseLocation.objects.filter(location_code__in=location_codes)
    }
    csv_qty_by_batch: dict[int, Decimal] = {}
    group_key_by_order_id: dict[int, str] = {}
    seen_requisition_batch_keys = set()

    for index, row in enumerate(rows, start=2):
        requisition_no = _clean(row.get("requisition_no"))
        production_order_no = _clean(row.get("production_order_no"))
        material_code = _clean(row.get("material_code"))
        batch_no = _clean(row.get("batch_no"))
        location_code = _clean(row.get("location_code"))

        if requisition_no and requisition_no in existing_requisition_nos:
            errors.append({"row": index, "field": "requisition_no", "message": "生产领料单号已存在"})

        production_order = production_orders.get(production_order_no)
        if not production_order_no:
            errors.append({"row": index, "field": "production_order_no", "message": "生产指令单号不能为空"})
        elif not production_order:
            errors.append({"row": index, "field": "production_order_no", "message": "生产指令不存在"})
        elif production_order.status not in [ProductionOrder.Status.PENDING, ProductionOrder.Status.IN_PROGRESS]:
            errors.append({"row": index, "field": "production_order_no", "message": "生产指令状态不能生成领料单"})
        elif production_order.id in existing_requisition_order_ids:
            errors.append({"row": index, "field": "production_order_no", "message": "该生产指令已存在未作废领料单"})

        requisition_date = _parse_date(row.get("requisition_date"))
        if not requisition_date:
            errors.append({"row": index, "field": "requisition_date", "message": "领料日期格式错误，应为 YYYY-MM-DD"})

        material = materials.get(material_code)
        if not material_code:
            errors.append({"row": index, "field": "material_code", "message": "领料物料编码不能为空"})
        elif not material or material.status != Material.MaterialStatus.ACTIVE:
            errors.append({"row": index, "field": "material_code", "message": "领料物料不存在或未启用"})
        elif production_order and material.id not in bom_material_ids_by_order.get(production_order.id, set()):
            errors.append({"row": index, "field": "material_code", "message": "领料物料不在生产指令锁定 BOM 子件中"})

        required_qty = _parse_decimal(row.get("required_qty"))
        if required_qty is None or required_qty <= ZERO:
            errors.append({"row": index, "field": "required_qty", "message": "需求数量必须大于 0"})
        issued_qty = _parse_decimal(row.get("issued_qty"))
        if issued_qty is None:
            issued_qty = required_qty
        if issued_qty is None or issued_qty < ZERO:
            errors.append({"row": index, "field": "issued_qty", "message": "实领数量不能小于 0"})
        if required_qty is not None and issued_qty is not None and issued_qty > required_qty:
            errors.append({"row": index, "field": "issued_qty", "message": "实领数量不能超过需求数量"})

        batch = batches.get(batch_no)
        if not batch_no:
            errors.append({"row": index, "field": "batch_no", "message": "批次号不能为空"})
        elif not batch or batch.inventory_type != InventoryBatch.InventoryType.AVAILABLE or batch.batch_status != InventoryBatch.BatchStatus.IN_STOCK:
            errors.append({"row": index, "field": "batch_no", "message": "批次不存在、不是可用库存或未在库"})
        elif material and batch.material_id != material.id:
            errors.append({"row": index, "field": "batch_no", "message": "批次物料必须与领料物料一致"})

        location = None
        if location_code:
            location = locations.get(location_code)
            if not location or location.status != WarehouseLocation.LocationStatus.ACTIVE:
                errors.append({"row": index, "field": "location_code", "message": "库位不存在或未启用"})
        if batch:
            if location and batch.location_id != location.id:
                errors.append({"row": index, "field": "location_code", "message": "库位必须与批次库位一致"})
            location = batch.location
            if issued_qty is not None and issued_qty > batch.remaining_qty - csv_qty_by_batch.get(batch.id, ZERO):
                errors.append({"row": index, "field": "issued_qty", "message": "实领数量不能超过批次可用剩余数量"})

        group_key = requisition_no or f"__row_{index}"
        duplicate_key = (group_key, batch.id if batch else batch_no, material.id if material else material_code)
        if duplicate_key in seen_requisition_batch_keys:
            errors.append({"row": index, "field": "batch_no", "message": "同一领料单中相同物料和批次不能重复"})
        seen_requisition_batch_keys.add(duplicate_key)

        if errors and any(error["row"] == index for error in errors):
            continue

        if production_order.id in group_key_by_order_id and group_key_by_order_id[production_order.id] != group_key:
            errors.append({"row": index, "field": "production_order_no", "message": "同一生产指令不能生成多张领料单"})
            continue
        group_key_by_order_id[production_order.id] = group_key

        group = grouped_rows.setdefault(
            group_key,
            {
                "requisition_no": requisition_no,
                "production_order": production_order,
                "requisition_date": requisition_date,
                "remark": _clean(row.get("remark")),
                "items": [],
            },
        )
        if group["production_order"].id != production_order.id:
            errors.append({"row": index, "field": "production_order_no", "message": "同一领料单号下生产指令必须一致"})
            continue
        if group["requisition_date"] != requisition_date:
            errors.append({"row": index, "field": "requisition_date", "message": "同一领料单号下领料日期必须一致"})
            continue

        csv_qty_by_batch[batch.id] = csv_qty_by_batch.get(batch.id, ZERO) + issued_qty
        group["items"].append(
            {
                "material": material,
                "required_qty": required_qty,
                "issued_qty": issued_qty,
                "batch": batch,
                "location": location,
                "adjust_reason": _clean(row.get("adjust_reason")),
            }
        )

    return errors, grouped_rows


def _validate_production_receipt_rows(rows: list[dict]) -> tuple[list[dict], OrderedDict]:
    errors = []
    grouped_rows = OrderedDict()
    if not rows:
        return [{"row": 0, "field": "", "message": "导入文件没有数据行"}], grouped_rows

    receipt_nos = {_clean(row.get("production_receipt_no")) for row in rows if _clean(row.get("production_receipt_no"))}
    production_order_nos = {_clean(row.get("production_order_no")) for row in rows if _clean(row.get("production_order_no"))}
    location_codes = {_clean(row.get("location_code")) for row in rows if _clean(row.get("location_code"))}
    batch_nos = {_clean(row.get("batch_no")) for row in rows if _clean(row.get("batch_no"))}

    existing_receipt_nos = set(
        ProductionReceipt.objects.filter(production_receipt_no__in=receipt_nos).values_list("production_receipt_no", flat=True)
    )
    existing_batch_nos = set(InventoryBatch.objects.filter(batch_no__in=batch_nos).values_list("batch_no", flat=True))
    production_orders = {
        order.production_order_no: order
        for order in ProductionOrder.objects.select_related("finished_material").filter(production_order_no__in=production_order_nos)
    }
    pending_qty_by_order = {
        row["production_order"]: row["total"] or ZERO
        for row in ProductionReceiptItem.objects.filter(
            production_order__production_order_no__in=production_order_nos,
            production_receipt__status__in=[ProductionReceipt.Status.DRAFT, ProductionReceipt.Status.PENDING_CONFIRM],
        )
        .values("production_order")
        .annotate(total=models_sum("receipt_qty"))
    }
    locations = {
        location.location_code: location
        for location in WarehouseLocation.objects.filter(location_code__in=location_codes)
    }
    csv_qty_by_order: dict[int, Decimal] = {}
    seen_receipt_batch_nos = set()

    for index, row in enumerate(rows, start=2):
        receipt_no = _clean(row.get("production_receipt_no"))
        production_order_no = _clean(row.get("production_order_no"))
        location_code = _clean(row.get("location_code"))
        batch_no = _clean(row.get("batch_no"))

        if receipt_no and receipt_no in existing_receipt_nos:
            errors.append({"row": index, "field": "production_receipt_no", "message": "生产入库单号已存在"})

        production_order = production_orders.get(production_order_no)
        if not production_order_no:
            errors.append({"row": index, "field": "production_order_no", "message": "生产指令单号不能为空"})
        elif not production_order:
            errors.append({"row": index, "field": "production_order_no", "message": "生产指令不存在"})
        elif production_order.status not in [ProductionOrder.Status.PENDING, ProductionOrder.Status.IN_PROGRESS]:
            errors.append({"row": index, "field": "production_order_no", "message": "生产指令状态不能生成入库单"})

        receipt_date = _parse_date(row.get("receipt_date"))
        if not receipt_date:
            errors.append({"row": index, "field": "receipt_date", "message": "入库日期格式错误，应为 YYYY-MM-DD"})

        receipt_qty = _parse_decimal(row.get("receipt_qty"))
        if receipt_qty is None or receipt_qty <= ZERO:
            errors.append({"row": index, "field": "receipt_qty", "message": "入库数量必须大于 0"})
        elif production_order:
            pending_qty = pending_qty_by_order.get(production_order.id, ZERO)
            csv_qty = csv_qty_by_order.get(production_order.id, ZERO)
            remaining_qty = production_order.production_qty - production_order.received_qty - pending_qty - csv_qty
            if receipt_qty > remaining_qty:
                errors.append({"row": index, "field": "receipt_qty", "message": "入库数量不能超过生产指令剩余未入库数量"})

        location = None
        if not location_code:
            errors.append({"row": index, "field": "location_code", "message": "入库库位不能为空"})
        else:
            location = locations.get(location_code)
            if not location or location.status != WarehouseLocation.LocationStatus.ACTIVE:
                errors.append({"row": index, "field": "location_code", "message": "库位不存在或未启用"})

        if batch_no:
            if batch_no in existing_batch_nos:
                errors.append({"row": index, "field": "batch_no", "message": "批次号已存在，生产入库确认时不能重复生成"})
            if batch_no in seen_receipt_batch_nos:
                errors.append({"row": index, "field": "batch_no", "message": "导入文件中批次号不能重复"})
            seen_receipt_batch_nos.add(batch_no)

        quality_status = _clean(row.get("quality_status")) or ProductionReceiptItem.QualityStatus.QUALIFIED
        if quality_status not in ProductionReceiptItem.QualityStatus.values:
            errors.append({"row": index, "field": "quality_status", "message": "质量状态必须是 qualified、pending 或 defective"})

        group_key = receipt_no or f"__row_{index}"
        if errors and any(error["row"] == index for error in errors):
            continue

        group = grouped_rows.setdefault(
            group_key,
            {
                "production_receipt_no": receipt_no,
                "production_order": production_order,
                "receipt_date": receipt_date,
                "remark": _clean(row.get("remark")),
                "items": [],
            },
        )
        if group["production_order"].id != production_order.id:
            errors.append({"row": index, "field": "production_order_no", "message": "同一入库单号下生产指令必须一致"})
            continue
        if group["receipt_date"] != receipt_date:
            errors.append({"row": index, "field": "receipt_date", "message": "同一入库单号下入库日期必须一致"})
            continue

        csv_qty_by_order[production_order.id] = csv_qty_by_order.get(production_order.id, ZERO) + receipt_qty
        group["items"].append(
            {
                "receipt_qty": receipt_qty,
                "location": location,
                "batch_no": batch_no,
                "quality_status": quality_status,
            }
        )

    return errors, grouped_rows


def _candidate_boms(bom_nos: set[str], bom_versions: set[str], material_codes: set[str]) -> list[Bom]:
    return list(
        Bom.objects.select_related("finished_material")
        .filter(status=Bom.BomStatus.ENABLED)
        .filter(
            models_filter_for_boms(bom_nos, bom_versions, material_codes)
        )
        .order_by("finished_material_id", "-is_default", "-enabled_at", "-id")
    )


def models_filter_for_boms(bom_nos: set[str], bom_versions: set[str], material_codes: set[str]):
    from django.db.models import Q

    query = Q(finished_material__material_code__in=material_codes)
    if bom_nos:
        query |= Q(bom_no__in=bom_nos)
    if bom_versions and material_codes:
        query |= Q(finished_material__material_code__in=material_codes, bom_version__in=bom_versions)
    return query


def _resolve_manual_bom(finished_material: Material, bom_no: str, bom_version: str, boms: list[Bom]) -> Bom | None:
    choices = [bom for bom in boms if bom.finished_material_id == finished_material.id]
    if bom_no:
        choices = [bom for bom in choices if bom.bom_no == bom_no]
    if bom_version:
        choices = [bom for bom in choices if bom.bom_version == bom_version]
    if choices:
        return choices[0]
    return (
        Bom.objects.filter(finished_material=finished_material, status=Bom.BomStatus.ENABLED)
        .order_by("-is_default", "-enabled_at", "-id")
        .first()
    )


def _parse_date(value):
    value = _clean(value)
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_decimal(value):
    value = _clean(value)
    if not value:
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def _clean(value) -> str:
    return (value or "").strip()


def _start_import_job(template_type: str, operator_id: int | None) -> ImportJob:
    return ImportJob.objects.create(
        job_no=next_document_no("IMP"),
        template_type=template_type,
        template_version="v1",
        status=ImportJob.JobStatus.VALIDATING,
        started_at=timezone.now(),
        created_by_id=operator_id,
    )


def _validation_failed(job: ImportJob, message: str, errors: list[dict]) -> ServiceResult:
    job.status = ImportJob.JobStatus.FAILED
    job.failed_count = len(errors)
    job.error_summary = {"errors": errors[:50]}
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "failed_count", "error_summary", "finished_at"])
    return ServiceResult(False, "FILE_IMPORT_VALIDATION_FAILED", message, data={"import_job_id": job.id, "errors": errors})


def _import_success(job: ImportJob, success_count: int, message: str) -> ServiceResult:
    job.status = ImportJob.JobStatus.SUCCESS
    job.success_count = success_count
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "success_count", "finished_at"])
    return ServiceResult(True, message=message, data={"import_job_id": job.id, "success_count": success_count, "failed_count": 0})


def _fail_import_job(job: ImportJob, message: str, error_code: str = "FILE_IMPORT_VALIDATION_FAILED") -> ServiceResult:
    job.status = ImportJob.JobStatus.FAILED
    job.failed_count = 1
    job.error_summary = {"errors": [{"row": 0, "field": "", "message": message}]}
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "failed_count", "error_summary", "finished_at"])
    return ServiceResult(False, error_code, message, data={"import_job_id": job.id, "errors": job.error_summary["errors"]})

