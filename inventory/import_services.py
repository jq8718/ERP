from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from io import TextIOBase

from django.db import transaction
from django.utils import timezone

from files.models import ImportJob, InitializationJob
from files.services import CsvImportReadError, read_csv_dict_rows
from masterdata.models import Material
from system.services import ServiceResult, next_document_no

from .models import Inventory, InventoryBatch, InventoryTransaction, WarehouseLocation


INITIAL_INVENTORY_IMPORT_COLUMNS = (
    "material_code",
    "location_code",
    "batch_no",
    "inventory_type",
    "initial_qty",
    "cost_price",
    "received_at",
)

INITIAL_INVENTORY_IMPORT_TEMPLATE_ROWS = (
    INITIAL_INVENTORY_IMPORT_COLUMNS,
    ("RM001", "A01", "OPEN-RM001-A01-001", "available", "100.0000", "12.345600", "2026-06-09"),
    ("FG001", "A01", "", "available", "20.0000", "", "2026-06-09"),
)

WAREHOUSE_LOCATION_IMPORT_COLUMNS = (
    "location_code",
    "location_name",
    "status",
    "remark",
)

WAREHOUSE_LOCATION_IMPORT_TEMPLATE_ROWS = (
    WAREHOUSE_LOCATION_IMPORT_COLUMNS,
    ("A01", "原料库 A01", "active", "示例行，导入前可删除"),
    ("B01", "成品库 B01", "active", ""),
)

ZERO = Decimal("0")


def import_warehouse_locations_from_csv(file_obj: TextIOBase, operator_id: int | None = None) -> ServiceResult:
    job = _start_import_job("warehouse_locations", operator_id)
    try:
        rows = read_csv_dict_rows(file_obj)
        errors = _validate_warehouse_location_rows(rows)
        if errors:
            return _validation_failed_import(job, "库位导入校验失败", errors)

        job.status = ImportJob.JobStatus.IMPORTING
        job.save(update_fields=["status"])
        with transaction.atomic():
            locations = [_build_warehouse_location(row) for row in rows]
            WarehouseLocation.objects.bulk_create(locations)
        return _import_success(job, len(rows), "库位导入完成")
    except UnicodeDecodeError:
        return _fail_import_job(job, "导入文件编码错误，请使用 UTF-8 CSV")
    except CsvImportReadError as exc:
        return _fail_import_job(job, str(exc), exc.error_code)
    except Exception as exc:
        return _fail_import_job(job, f"库位导入失败：{exc}", "FILE_IMPORT_FAILED")


def preview_initial_inventory_from_csv(file_obj: TextIOBase, operator_id: int | None = None) -> ServiceResult:
    job = InitializationJob.objects.create(
        job_no=next_document_no("INI"),
        template_type="initial_inventory",
        status=InitializationJob.JobStatus.VALIDATING,
        created_by_id=operator_id,
    )
    try:
        rows = read_csv_dict_rows(file_obj)
        errors, material_map, location_map = _validate_initial_inventory_rows(rows)
        if errors:
            return _validation_failed(job, "期初库存导入校验失败", errors)

        preview_rows = _initial_inventory_preview_rows(rows, material_map, location_map)
        job.status = InitializationJob.JobStatus.PENDING_CONFIRM
        job.success_count = len(preview_rows)
        job.error_summary = {"preview_rows": preview_rows}
        job.save(update_fields=["status", "success_count", "error_summary"])
        return ServiceResult(
            True,
            message="期初库存校验通过，请确认后入账",
            data={
                "initialization_job_id": job.id,
                "success_count": len(preview_rows),
                "failed_count": 0,
                "preview_rows": preview_rows,
            },
        )
    except UnicodeDecodeError:
        return _fail_initialization_job(job, "导入文件编码错误，请使用 UTF-8 CSV")
    except CsvImportReadError as exc:
        return _fail_initialization_job(job, str(exc), exc.error_code)
    except Exception as exc:
        return _fail_initialization_job(job, f"期初库存导入失败：{exc}", "FILE_IMPORT_FAILED")


def confirm_initial_inventory_import(job_id: int, operator_id: int | None = None) -> ServiceResult:
    try:
        with transaction.atomic():
            job = InitializationJob.objects.select_for_update().get(id=job_id, template_type="initial_inventory")
            if job.status != InitializationJob.JobStatus.PENDING_CONFIRM:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "只有待确认的期初库存任务可以确认")
            preview_rows = job.error_summary.get("preview_rows", []) if isinstance(job.error_summary, dict) else []
            errors, material_map, location_map = _validate_initial_inventory_rows(preview_rows)
            if errors:
                job.status = InitializationJob.JobStatus.FAILED
                job.failed_count = len(errors)
                job.error_summary = {"errors": errors[:50], "preview_rows": preview_rows}
                job.save(update_fields=["status", "failed_count", "error_summary"])
                return ServiceResult(
                    False,
                    "FILE_IMPORT_VALIDATION_FAILED",
                    "期初库存确认前校验失败",
                    data={"initialization_job_id": job.id, "errors": errors},
                )

            job.status = InitializationJob.JobStatus.IMPORTING
            job.save(update_fields=["status"])
            imported_rows = _create_initial_inventory_rows(preview_rows, material_map, location_map, job, operator_id)
            job.status = InitializationJob.JobStatus.SUCCESS
            job.success_count = imported_rows
            job.failed_count = 0
            job.confirmed_by_id = operator_id
            job.confirmed_at = timezone.now()
            job.save(update_fields=["status", "success_count", "failed_count", "confirmed_by", "confirmed_at"])
        return ServiceResult(
            True,
            message="期初库存导入完成",
            data={"initialization_job_id": job.id, "success_count": imported_rows, "failed_count": 0},
        )
    except InitializationJob.DoesNotExist:
        return ServiceResult(False, "FILE_IMPORT_JOB_NOT_FOUND", "期初库存任务不存在")
    except Exception as exc:
        return ServiceResult(False, "FILE_IMPORT_FAILED", f"期初库存确认失败：{exc}")


def cancel_initial_inventory_import(job_id: int, operator_id: int | None = None) -> ServiceResult:
    try:
        with transaction.atomic():
            job = InitializationJob.objects.select_for_update().get(id=job_id, template_type="initial_inventory")
            if job.status == InitializationJob.JobStatus.PENDING_CONFIRM:
                job.status = InitializationJob.JobStatus.CANCELLED
                job.confirmed_by_id = operator_id
                job.confirmed_at = timezone.now()
                job.save(update_fields=["status", "confirmed_by", "confirmed_at"])
                return ServiceResult(True, message="期初库存导入任务已取消", data={"initialization_job_id": job.id})
            if job.status != InitializationJob.JobStatus.SUCCESS:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "只有待确认或已成功的期初库存任务可以撤销")

            transactions = list(
                InventoryTransaction.objects.select_for_update()
                .select_related("batch")
                .filter(
                    transaction_type=InventoryTransaction.TransactionType.INITIAL_STOCK,
                    source_doc_type="initial_inventory_import",
                    source_doc_id=job.id,
                )
                .order_by("material_id", "location_id", "batch_id")
            )
            if not transactions:
                return ServiceResult(False, "FILE_IMPORT_NOT_REVERSIBLE", "未找到可撤销的期初库存流水")
            for transaction_row in transactions:
                batch = InventoryBatch.objects.select_for_update().get(id=transaction_row.batch_id)
                if batch.remaining_qty != transaction_row.qty_delta or batch.batch_status != InventoryBatch.BatchStatus.IN_STOCK:
                    return ServiceResult(False, "FILE_IMPORT_NOT_REVERSIBLE", "期初库存批次已被使用，不能撤销")

            reversed_rows = 0
            for transaction_row in transactions:
                batch = InventoryBatch.objects.select_for_update().get(id=transaction_row.batch_id)
                inventory = _inventory_for_update(batch.material_id, batch.location_id, batch.inventory_type)
                if inventory.qty < batch.remaining_qty:
                    return ServiceResult(False, "INVENTORY_QTY_NEGATIVE", "库存汇总数量不足，不能撤销")
                inventory.qty -= batch.remaining_qty
                inventory.save(update_fields=["qty", "updated_at"])
                InventoryTransaction.objects.create(
                    transaction_no=next_document_no("IT"),
                    transaction_type=InventoryTransaction.TransactionType.STOCK_ADJUSTMENT,
                    material=batch.material,
                    batch=batch,
                    location=batch.location,
                    qty_delta=-batch.remaining_qty,
                    source_doc_type="initial_inventory_cancel",
                    source_doc_id=job.id,
                    source_doc_no=job.job_no,
                    created_by_id=operator_id,
                )
                batch.remaining_qty = ZERO
                batch.batch_status = InventoryBatch.BatchStatus.VOIDED
                batch.save(update_fields=["remaining_qty", "batch_status"])
                reversed_rows += 1

            job.status = InitializationJob.JobStatus.CANCELLED
            job.confirmed_by_id = operator_id
            job.confirmed_at = timezone.now()
            job.save(update_fields=["status", "confirmed_by", "confirmed_at"])
        return ServiceResult(
            True,
            message="期初库存导入已撤销",
            data={"initialization_job_id": job.id, "reversed_rows": reversed_rows},
        )
    except InitializationJob.DoesNotExist:
        return ServiceResult(False, "FILE_IMPORT_JOB_NOT_FOUND", "期初库存任务不存在")


def import_initial_inventory_from_csv(file_obj: TextIOBase, operator_id: int | None = None) -> ServiceResult:
    preview_result = preview_initial_inventory_from_csv(file_obj, operator_id)
    if not preview_result.success:
        return preview_result
    return confirm_initial_inventory_import(preview_result.data["initialization_job_id"], operator_id)


def _legacy_import_initial_inventory_from_csv(file_obj: TextIOBase, operator_id: int | None = None) -> ServiceResult:
    job = InitializationJob.objects.create(
        job_no=next_document_no("INI"),
        template_type="initial_inventory",
        status=InitializationJob.JobStatus.VALIDATING,
        created_by_id=operator_id,
    )
    try:
        rows = read_csv_dict_rows(file_obj)
        errors, material_map, location_map = _validate_initial_inventory_rows(rows)
        if errors:
            return _validation_failed(job, "期初库存导入校验失败", errors)

        job.status = InitializationJob.JobStatus.IMPORTING
        job.save(update_fields=["status"])
        with transaction.atomic():
            imported_rows = _create_initial_inventory_rows(rows, material_map, location_map, job, operator_id)

        job.status = InitializationJob.JobStatus.SUCCESS
        job.success_count = imported_rows
        job.confirmed_by_id = operator_id
        job.confirmed_at = timezone.now()
        job.save(update_fields=["status", "success_count", "confirmed_by", "confirmed_at"])
        return ServiceResult(
            True,
            message="期初库存导入完成",
            data={"initialization_job_id": job.id, "success_count": imported_rows, "failed_count": 0},
        )
    except UnicodeDecodeError:
        return _fail_initialization_job(job, "导入文件编码错误，请使用 UTF-8 CSV")
    except CsvImportReadError as exc:
        return _fail_initialization_job(job, str(exc), exc.error_code)
    except Exception as exc:
        return _fail_initialization_job(job, f"期初库存导入失败：{exc}", "FILE_IMPORT_FAILED")


def _validate_warehouse_location_rows(rows: list[dict[str, str]]) -> list[dict]:
    errors = []
    if not rows:
        return [{"row": 0, "field": "", "message": "导入文件没有数据行"}]

    seen_codes = set()
    existing_codes = set(
        WarehouseLocation.objects.filter(
            location_code__in=[_clean(row.get("location_code")) for row in rows]
        ).values_list("location_code", flat=True)
    )
    valid_statuses = set(WarehouseLocation.LocationStatus.values)

    for row_no, row in enumerate(rows, start=2):
        for field in ("location_code", "location_name"):
            if not _clean(row.get(field)):
                errors.append({"row": row_no, "field": field, "message": "必填字段不能为空"})

        location_code = _clean(row.get("location_code"))
        if location_code:
            if location_code in seen_codes:
                errors.append({"row": row_no, "field": "location_code", "message": "导入文件中库位编码重复"})
            if location_code in existing_codes:
                errors.append({"row": row_no, "field": "location_code", "message": "库位编码已存在"})
            seen_codes.add(location_code)

        status = _clean(row.get("status")) or WarehouseLocation.LocationStatus.ACTIVE
        if status not in valid_statuses:
            errors.append({"row": row_no, "field": "status", "message": "状态不合法"})

    return errors


def _build_warehouse_location(row: dict[str, str]) -> WarehouseLocation:
    return WarehouseLocation(
        location_code=_clean(row.get("location_code")),
        location_name=_clean(row.get("location_name")),
        status=_clean(row.get("status")) or WarehouseLocation.LocationStatus.ACTIVE,
        remark=_clean(row.get("remark")),
    )


def _initial_inventory_preview_rows(
    rows: list[dict[str, str]],
    material_map: dict[str, Material],
    location_map: dict[str, WarehouseLocation],
) -> list[dict[str, str]]:
    preview_rows = []
    for row in rows:
        material_code = _clean(row.get("material_code"))
        location_code = _clean(row.get("location_code"))
        preview_rows.append(
            {
                "material_code": material_code,
                "material_name": material_map[material_code].material_name,
                "location_code": location_code,
                "location_name": location_map[location_code].location_name,
                "batch_no": _clean(row.get("batch_no")),
                "inventory_type": _clean(row.get("inventory_type")) or InventoryBatch.InventoryType.AVAILABLE,
                "initial_qty": str(_parse_decimal(_clean(row.get("initial_qty"))) or ZERO),
                "cost_price": str(_parse_decimal(_clean(row.get("cost_price"))) or "") if _clean(row.get("cost_price")) else "",
                "received_at": _clean(row.get("received_at")),
            }
        )
    return preview_rows


def _validate_initial_inventory_rows(rows: list[dict[str, str]]) -> tuple[list[dict], dict[str, Material], dict[str, WarehouseLocation]]:
    errors = []
    if not rows:
        return [{"row": 0, "field": "", "message": "导入文件没有数据行"}], {}, {}

    material_codes = {_clean(row.get("material_code")) for row in rows if _clean(row.get("material_code"))}
    location_codes = {_clean(row.get("location_code")) for row in rows if _clean(row.get("location_code"))}
    material_map = {material.material_code: material for material in Material.objects.filter(material_code__in=material_codes)}
    location_map = {
        location.location_code: location
        for location in WarehouseLocation.objects.filter(location_code__in=location_codes)
    }
    existing_batch_nos = set(
        InventoryBatch.objects.filter(batch_no__in=[_clean(row.get("batch_no")) for row in rows if _clean(row.get("batch_no"))]).values_list(
            "batch_no",
            flat=True,
        )
    )
    seen_batch_nos = set()
    valid_inventory_types = set(InventoryBatch.InventoryType.values)

    for row_no, row in enumerate(rows, start=2):
        for field in ("material_code", "location_code", "initial_qty"):
            if not _clean(row.get(field)):
                errors.append({"row": row_no, "field": field, "message": "必填字段不能为空"})

        material_code = _clean(row.get("material_code"))
        if material_code and material_code not in material_map:
            errors.append({"row": row_no, "field": "material_code", "message": "物料编码不存在"})

        location_code = _clean(row.get("location_code"))
        location = location_map.get(location_code)
        if location_code and location is None:
            errors.append({"row": row_no, "field": "location_code", "message": "库位编码不存在"})
        elif location and location.status != WarehouseLocation.LocationStatus.ACTIVE:
            errors.append({"row": row_no, "field": "location_code", "message": "库位未启用"})

        batch_no = _clean(row.get("batch_no"))
        if batch_no:
            if batch_no in seen_batch_nos:
                errors.append({"row": row_no, "field": "batch_no", "message": "导入文件中批次号重复"})
            if batch_no in existing_batch_nos:
                errors.append({"row": row_no, "field": "batch_no", "message": "批次号已存在"})
            seen_batch_nos.add(batch_no)

        inventory_type = _clean(row.get("inventory_type")) or InventoryBatch.InventoryType.AVAILABLE
        if inventory_type not in valid_inventory_types:
            errors.append({"row": row_no, "field": "inventory_type", "message": "库存类型不合法"})

        initial_qty = _parse_decimal(_clean(row.get("initial_qty")))
        if initial_qty is None:
            errors.append({"row": row_no, "field": "initial_qty", "message": "数量格式不合法"})
        elif initial_qty <= ZERO:
            errors.append({"row": row_no, "field": "initial_qty", "message": "期初数量必须大于 0"})

        cost_price = _clean(row.get("cost_price"))
        parsed_cost_price = _parse_decimal(cost_price) if cost_price else None
        if cost_price and parsed_cost_price is None:
            errors.append({"row": row_no, "field": "cost_price", "message": "成本单价格式不合法"})
        elif parsed_cost_price is not None and parsed_cost_price < ZERO:
            errors.append({"row": row_no, "field": "cost_price", "message": "成本单价不能小于 0"})

        received_at = _clean(row.get("received_at"))
        if received_at and _parse_received_at(received_at) is None:
            errors.append({"row": row_no, "field": "received_at", "message": "入库日期格式不合法"})

    return errors, material_map, location_map


def _create_initial_inventory_rows(
    rows: list[dict[str, str]],
    material_map: dict[str, Material],
    location_map: dict[str, WarehouseLocation],
    job: InitializationJob,
    operator_id: int | None,
) -> int:
    sorted_rows = sorted(
        rows,
        key=lambda row: (
            _clean(row.get("material_code")),
            _clean(row.get("location_code")),
            _clean(row.get("inventory_type")) or InventoryBatch.InventoryType.AVAILABLE,
            _clean(row.get("batch_no")),
        ),
    )
    imported_rows = 0
    for row in sorted_rows:
        material = material_map[_clean(row.get("material_code"))]
        location = location_map[_clean(row.get("location_code"))]
        inventory_type = _clean(row.get("inventory_type")) or InventoryBatch.InventoryType.AVAILABLE
        initial_qty = _parse_decimal(_clean(row.get("initial_qty"))) or ZERO
        cost_price = _parse_decimal(_clean(row.get("cost_price")))
        received_at = _parse_received_at(_clean(row.get("received_at"))) or timezone.now()

        batch = InventoryBatch.objects.create(
            batch_no=_clean(row.get("batch_no")) or next_document_no("IB"),
            material=material,
            location=location,
            inventory_type=inventory_type,
            received_at=received_at,
            initial_qty=initial_qty,
            remaining_qty=initial_qty,
            cost_price=cost_price,
            batch_status=InventoryBatch.BatchStatus.IN_STOCK,
        )
        inventory = _inventory_for_update(material.id, location.id, inventory_type)
        inventory.qty += initial_qty
        inventory.save(update_fields=["qty", "updated_at"])
        InventoryTransaction.objects.create(
            transaction_no=next_document_no("IT"),
            transaction_type=InventoryTransaction.TransactionType.INITIAL_STOCK,
            material=material,
            batch=batch,
            location=location,
            qty_delta=initial_qty,
            source_doc_type="initial_inventory_import",
            source_doc_id=job.id,
            source_doc_no=job.job_no,
            created_by_id=operator_id,
        )
        imported_rows += 1
    return imported_rows


def _inventory_for_update(material_id: int, location_id: int, inventory_type: str) -> Inventory:
    inventory, _ = (
        Inventory.objects.select_for_update()
        .get_or_create(
            material_id=material_id,
            location_id=location_id,
            inventory_type=inventory_type,
            defaults={"qty": ZERO},
        )
    )
    return inventory


def _parse_decimal(value: str) -> Decimal | None:
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _parse_received_at(value: str):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            parsed_date = date.fromisoformat(value)
        except ValueError:
            return None
        parsed = datetime.combine(parsed_date, time.min)
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _clean(value) -> str:
    return (value or "").strip()


def _validation_failed(job: InitializationJob, message: str, errors: list[dict]) -> ServiceResult:
    job.status = InitializationJob.JobStatus.FAILED
    job.failed_count = len(errors)
    job.error_summary = {"errors": errors[:50]}
    job.save(update_fields=["status", "failed_count", "error_summary"])
    return ServiceResult(
        False,
        "FILE_IMPORT_VALIDATION_FAILED",
        message,
        data={"initialization_job_id": job.id, "errors": errors},
    )


def _start_import_job(template_type: str, operator_id: int | None) -> ImportJob:
    return ImportJob.objects.create(
        job_no=next_document_no("IMP"),
        template_type=template_type,
        template_version="v1",
        status=ImportJob.JobStatus.VALIDATING,
        started_at=timezone.now(),
        created_by_id=operator_id,
    )


def _validation_failed_import(job: ImportJob, message: str, errors: list[dict]) -> ServiceResult:
    job.status = ImportJob.JobStatus.FAILED
    job.failed_count = len(errors)
    job.error_summary = {"errors": errors[:50]}
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "failed_count", "error_summary", "finished_at"])
    return ServiceResult(
        False,
        "FILE_IMPORT_VALIDATION_FAILED",
        message,
        data={"import_job_id": job.id, "errors": errors},
    )


def _import_success(job: ImportJob, success_count: int, message: str) -> ServiceResult:
    job.status = ImportJob.JobStatus.SUCCESS
    job.success_count = success_count
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "success_count", "finished_at"])
    return ServiceResult(
        True,
        message=message,
        data={"import_job_id": job.id, "success_count": success_count, "failed_count": 0},
    )


def _fail_import_job(job: ImportJob, message: str, error_code: str = "FILE_IMPORT_VALIDATION_FAILED") -> ServiceResult:
    job.status = ImportJob.JobStatus.FAILED
    job.failed_count = 1
    job.error_summary = {"errors": [{"row": 0, "field": "", "message": message}]}
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "failed_count", "error_summary", "finished_at"])
    return ServiceResult(False, error_code, message, data={"import_job_id": job.id, "errors": job.error_summary["errors"]})


def _fail_initialization_job(job: InitializationJob, message: str, error_code: str = "FILE_IMPORT_VALIDATION_FAILED") -> ServiceResult:
    job.status = InitializationJob.JobStatus.FAILED
    job.failed_count = 1
    job.error_summary = {"errors": [{"row": 0, "field": "", "message": message}]}
    job.save(update_fields=["status", "failed_count", "error_summary"])
    return ServiceResult(False, error_code, message, data={"initialization_job_id": job.id, "errors": job.error_summary["errors"]})

