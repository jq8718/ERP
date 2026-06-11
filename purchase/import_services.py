from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import TextIOBase

from django.db import transaction
from django.db.models import Sum as models_sum
from django.utils import timezone

from files.models import ImportJob
from files.services import CsvImportReadError, read_csv_dict_rows
from inventory.models import InventoryBatch, WarehouseLocation
from masterdata.models import Material, MaterialSupplierPrice, Supplier
from system.services import ServiceResult, next_document_no

from .models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseReceipt,
    PurchaseReceiptItem,
    PurchaseRequest,
    PurchaseRequestItem,
    SupplierReturn,
    SupplierReturnItem,
)


PURCHASE_REQUEST_IMPORT_COLUMNS = (
    "purchase_request_no",
    "needed_date",
    "material_code",
    "request_qty",
    "suggested_supplier_no",
    "line_needed_date",
    "remark",
)

PURCHASE_REQUEST_IMPORT_TEMPLATE_ROWS = (
    PURCHASE_REQUEST_IMPORT_COLUMNS,
    ("PR-INIT-001", "2026-06-20", "RM001", "100", "S001", "2026-06-20", "示例行，导入前可删除"),
    ("PR-INIT-001", "2026-06-20", "RM002", "50", "", "2026-06-22", ""),
)

PURCHASE_ORDER_IMPORT_COLUMNS = (
    "purchase_order_no",
    "supplier_no",
    "order_date",
    "material_code",
    "order_qty",
    "unit_price",
    "needed_date",
    "remark",
)

PURCHASE_ORDER_IMPORT_TEMPLATE_ROWS = (
    PURCHASE_ORDER_IMPORT_COLUMNS,
    ("PO-INIT-001", "S001", "2026-06-20", "RM001", "100", "2.500000", "2026-06-25", "示例行，导入前可删除"),
    ("PO-INIT-001", "S001", "2026-06-20", "RM002", "50", "3.200000", "2026-06-28", ""),
)

SUPPLIER_RETURN_IMPORT_COLUMNS = (
    "supplier_return_no",
    "supplier_no",
    "purchase_receipt_no",
    "return_date",
    "purchase_order_line_no",
    "material_code",
    "return_qty",
    "unit_price",
    "batch_no",
    "location_code",
    "return_reason",
    "remark",
)

SUPPLIER_RETURN_IMPORT_TEMPLATE_ROWS = (
    SUPPLIER_RETURN_IMPORT_COLUMNS,
    ("SR-INIT-001", "S001", "GR001", "2026-06-10", "1", "RM001", "2", "2.500000", "BATCH001", "A01", "质量问题", "示例行，导入前可删除"),
    ("SR-INIT-001", "S001", "GR001", "2026-06-10", "2", "RM002", "1", "3.200000", "BATCH002", "A01", "", ""),
)

PURCHASE_RECEIPT_IMPORT_COLUMNS = (
    "purchase_receipt_no",
    "purchase_order_no",
    "receipt_date",
    "purchase_order_line_no",
    "material_code",
    "received_qty",
    "accepted_qty",
    "rejected_qty",
    "location_code",
    "remark",
)

PURCHASE_RECEIPT_IMPORT_TEMPLATE_ROWS = (
    PURCHASE_RECEIPT_IMPORT_COLUMNS,
    ("GR-INIT-001", "PO001", "2026-06-10", "1", "RM001", "100", "98", "2", "A01", "示例行，导入前可删除"),
    ("GR-INIT-001", "PO001", "2026-06-10", "2", "RM002", "50", "50", "0", "A01", ""),
)

ZERO = Decimal("0")
MONEY_QUANT = Decimal("0.01")


def import_purchase_requests_from_csv(file_obj: TextIOBase, operator_id: int | None = None) -> ServiceResult:
    job = _start_import_job("purchase_requests", operator_id)
    try:
        rows = read_csv_dict_rows(file_obj)
        errors, grouped_rows = _validate_purchase_request_rows(rows)
        if errors:
            return _validation_failed(job, "采购需求导入校验失败", errors)

        job.status = ImportJob.JobStatus.IMPORTING
        job.save(update_fields=["status"])
        created_count = 0
        with transaction.atomic():
            for group in grouped_rows.values():
                purchase_request = PurchaseRequest.objects.create(
                    purchase_request_no=group["purchase_request_no"] or next_document_no("PR"),
                    source_type=PurchaseRequest.SourceType.MANUAL,
                    status=PurchaseRequest.Status.DRAFT,
                    requested_by_id=operator_id,
                    needed_date=group["needed_date"],
                    remark=group["remark"],
                )
                for line_no, line in enumerate(group["items"], start=1):
                    PurchaseRequestItem.objects.create(
                        purchase_request=purchase_request,
                        line_no=line_no,
                        material=line["material"],
                        request_qty=line["request_qty"],
                        suggested_supplier=line["suggested_supplier"],
                        needed_date=line["needed_date"] or purchase_request.needed_date,
                        line_status=PurchaseRequestItem.LineStatus.OPEN,
                    )
                created_count += 1
        return _import_success(job, created_count, "采购需求导入完成")
    except UnicodeDecodeError:
        return _fail_import_job(job, "导入文件编码错误，请使用 UTF-8 CSV")
    except CsvImportReadError as exc:
        return _fail_import_job(job, str(exc), exc.error_code)
    except Exception as exc:
        return _fail_import_job(job, f"采购需求导入失败：{exc}", "FILE_IMPORT_FAILED")


def import_purchase_orders_from_csv(
    file_obj: TextIOBase,
    operator_id: int | None = None,
    can_import_amount: bool = False,
) -> ServiceResult:
    job = _start_import_job("purchase_orders", operator_id)
    try:
        rows = read_csv_dict_rows(file_obj)
        errors, grouped_rows = _validate_purchase_order_rows(rows, can_import_amount=can_import_amount)
        if errors:
            return _validation_failed(job, "采购单导入校验失败", errors)

        job.status = ImportJob.JobStatus.IMPORTING
        job.save(update_fields=["status"])
        created_count = 0
        with transaction.atomic():
            for group in grouped_rows.values():
                order = PurchaseOrder.objects.create(
                    purchase_order_no=group["purchase_order_no"] or next_document_no("PO"),
                    supplier=group["supplier"],
                    status=PurchaseOrder.Status.DRAFT,
                    order_date=group["order_date"],
                    created_by_id=operator_id,
                    remark=group["remark"],
                    total_amount=Decimal("0.00"),
                )
                total_amount = Decimal("0.00")
                for line_no, line in enumerate(group["items"], start=1):
                    line_amount = _money(line["order_qty"] * line["unit_price"])
                    PurchaseOrderItem.objects.create(
                        purchase_order=order,
                        line_no=line_no,
                        material=line["material"],
                        order_qty=line["order_qty"],
                        unit_price=line["unit_price"],
                        line_amount=line_amount,
                        needed_date=line["needed_date"],
                        line_status=PurchaseOrderItem.LineStatus.OPEN,
                    )
                    total_amount += line_amount
                order.total_amount = _money(total_amount)
                order.save(update_fields=["total_amount"])
                created_count += 1
        return _import_success(job, created_count, "采购单导入完成")
    except UnicodeDecodeError:
        return _fail_import_job(job, "导入文件编码错误，请使用 UTF-8 CSV")
    except CsvImportReadError as exc:
        return _fail_import_job(job, str(exc), exc.error_code)
    except Exception as exc:
        return _fail_import_job(job, f"采购单导入失败：{exc}", "FILE_IMPORT_FAILED")


def import_supplier_returns_from_csv(
    file_obj: TextIOBase,
    operator_id: int | None = None,
    can_import_amount: bool = False,
) -> ServiceResult:
    job = _start_import_job("supplier_returns", operator_id)
    try:
        rows = read_csv_dict_rows(file_obj)
        errors, grouped_rows = _validate_supplier_return_rows(rows, can_import_amount=can_import_amount)
        if errors:
            return _validation_failed(job, "供应商退货导入校验失败", errors)

        job.status = ImportJob.JobStatus.IMPORTING
        job.save(update_fields=["status"])
        created_count = 0
        with transaction.atomic():
            for group in grouped_rows.values():
                supplier_return = SupplierReturn.objects.create(
                    supplier_return_no=group["supplier_return_no"] or next_document_no("SR"),
                    supplier=group["supplier"],
                    purchase_receipt=group["purchase_receipt"],
                    return_date=group["return_date"],
                    status=SupplierReturn.Status.DRAFT,
                    created_by_id=operator_id,
                    remark=group["remark"],
                    return_amount=Decimal("0.00"),
                )
                total_amount = Decimal("0.00")
                for line in group["items"]:
                    return_amount = _money(line["return_qty"] * line["unit_price"])
                    SupplierReturnItem.objects.create(
                        supplier_return=supplier_return,
                        purchase_receipt_item=line["purchase_receipt_item"],
                        material=line["material"],
                        return_qty=line["return_qty"],
                        unit_price=line["unit_price"],
                        return_amount=return_amount,
                        batch=line["batch"],
                        location=line["location"],
                        return_reason=line["return_reason"],
                    )
                    total_amount += return_amount
                supplier_return.return_amount = _money(total_amount)
                supplier_return.save(update_fields=["return_amount"])
                created_count += 1
        return _import_success(job, created_count, "供应商退货导入完成")
    except UnicodeDecodeError:
        return _fail_import_job(job, "导入文件编码错误，请使用 UTF-8 CSV")
    except CsvImportReadError as exc:
        return _fail_import_job(job, str(exc), exc.error_code)
    except Exception as exc:
        return _fail_import_job(job, f"供应商退货导入失败：{exc}", "FILE_IMPORT_FAILED")


def import_purchase_receipts_from_csv(file_obj: TextIOBase, operator_id: int | None = None) -> ServiceResult:
    job = _start_import_job("purchase_receipts", operator_id)
    try:
        rows = read_csv_dict_rows(file_obj)
        errors, grouped_rows = _validate_purchase_receipt_rows(rows)
        if errors:
            return _validation_failed(job, "进货单导入校验失败", errors)

        job.status = ImportJob.JobStatus.IMPORTING
        job.save(update_fields=["status"])
        created_count = 0
        with transaction.atomic():
            for group in grouped_rows.values():
                receipt = PurchaseReceipt.objects.create(
                    purchase_receipt_no=group["purchase_receipt_no"] or next_document_no("GR"),
                    purchase_order=group["purchase_order"],
                    supplier=group["purchase_order"].supplier,
                    receipt_date=group["receipt_date"],
                    status=PurchaseReceipt.Status.PENDING_RECEIVE,
                    created_by_id=operator_id,
                    remark=group["remark"],
                )
                for line in group["items"]:
                    PurchaseReceiptItem.objects.create(
                        purchase_receipt=receipt,
                        purchase_order_item=line["purchase_order_item"],
                        material=line["material"],
                        received_qty=line["received_qty"],
                        accepted_qty=line["accepted_qty"],
                        rejected_qty=line["rejected_qty"],
                        unit_price=line["purchase_order_item"].unit_price,
                        location=line["location"],
                    )
                created_count += 1
        return _import_success(job, created_count, "进货单导入完成")
    except UnicodeDecodeError:
        return _fail_import_job(job, "导入文件编码错误，请使用 UTF-8 CSV")
    except CsvImportReadError as exc:
        return _fail_import_job(job, str(exc), exc.error_code)
    except Exception as exc:
        return _fail_import_job(job, f"进货单导入失败：{exc}", "FILE_IMPORT_FAILED")


def _validate_purchase_request_rows(rows: list[dict]) -> tuple[list[dict], OrderedDict]:
    errors = []
    grouped_rows = OrderedDict()
    if not rows:
        return [{"row": 0, "field": "", "message": "导入文件没有数据行"}], grouped_rows

    material_codes = {_clean(row.get("material_code")) for row in rows if _clean(row.get("material_code"))}
    supplier_nos = {_clean(row.get("suggested_supplier_no")) for row in rows if _clean(row.get("suggested_supplier_no"))}
    materials = {material.material_code: material for material in Material.objects.filter(material_code__in=material_codes)}
    suppliers = {supplier.supplier_no: supplier for supplier in Supplier.objects.filter(supplier_no__in=supplier_nos)}
    existing_request_nos = set(
        PurchaseRequest.objects.filter(
            purchase_request_no__in={_clean(row.get("purchase_request_no")) for row in rows if _clean(row.get("purchase_request_no"))}
        ).values_list("purchase_request_no", flat=True)
    )
    seen_request_material_keys = set()

    for index, row in enumerate(rows, start=2):
        request_no = _clean(row.get("purchase_request_no"))
        material_code = _clean(row.get("material_code"))
        supplier_no = _clean(row.get("suggested_supplier_no"))
        if request_no and request_no in existing_request_nos:
            errors.append({"row": index, "field": "purchase_request_no", "message": "采购需求单号已存在"})
        needed_date = _parse_date(row.get("needed_date"))
        if not needed_date:
            errors.append({"row": index, "field": "needed_date", "message": "需求日期格式错误，应为 YYYY-MM-DD"})
        line_needed_date_value = _clean(row.get("line_needed_date"))
        line_needed_date = _parse_date(line_needed_date_value) if line_needed_date_value else None
        if line_needed_date_value and not line_needed_date:
            errors.append({"row": index, "field": "line_needed_date", "message": "明细需求日期格式错误，应为 YYYY-MM-DD"})

        material = materials.get(material_code)
        if not material_code:
            errors.append({"row": index, "field": "material_code", "message": "物料编码不能为空"})
        elif not material or material.status != Material.MaterialStatus.ACTIVE:
            errors.append({"row": index, "field": "material_code", "message": "物料不存在或未启用"})

        request_qty = _parse_decimal(row.get("request_qty"))
        if request_qty is None or request_qty <= ZERO:
            errors.append({"row": index, "field": "request_qty", "message": "需求数量必须大于 0"})

        suggested_supplier = None
        if supplier_no:
            suggested_supplier = suppliers.get(supplier_no)
            if not suggested_supplier or suggested_supplier.status != Supplier.SupplierStatus.ACTIVE:
                errors.append({"row": index, "field": "suggested_supplier_no", "message": "建议供应商不存在或未启用"})

        group_key = request_no or f"__row_{index}"
        duplicate_key = (group_key, material.id if material else material_code)
        if duplicate_key in seen_request_material_keys:
            errors.append({"row": index, "field": "material_code", "message": "同一采购需求中物料不能重复"})
        seen_request_material_keys.add(duplicate_key)

        if errors and any(error["row"] == index for error in errors):
            continue

        group = grouped_rows.setdefault(
            group_key,
            {
                "purchase_request_no": request_no,
                "needed_date": needed_date,
                "remark": _clean(row.get("remark")),
                "items": [],
            },
        )
        if group["needed_date"] != needed_date:
            errors.append({"row": index, "field": "needed_date", "message": "同一采购需求单号下需求日期必须一致"})
            continue
        group["items"].append(
            {
                "material": material,
                "request_qty": request_qty,
                "suggested_supplier": suggested_supplier,
                "needed_date": line_needed_date,
            }
        )

    return errors, grouped_rows


def _validate_purchase_order_rows(rows: list[dict], can_import_amount: bool) -> tuple[list[dict], OrderedDict]:
    errors = []
    grouped_rows = OrderedDict()
    if not rows:
        return [{"row": 0, "field": "", "message": "导入文件没有数据行"}], grouped_rows

    material_codes = {_clean(row.get("material_code")) for row in rows if _clean(row.get("material_code"))}
    supplier_nos = {_clean(row.get("supplier_no")) for row in rows if _clean(row.get("supplier_no"))}
    materials = {material.material_code: material for material in Material.objects.filter(material_code__in=material_codes)}
    suppliers = {supplier.supplier_no: supplier for supplier in Supplier.objects.filter(supplier_no__in=supplier_nos)}
    existing_order_nos = set(
        PurchaseOrder.objects.filter(
            purchase_order_no__in={_clean(row.get("purchase_order_no")) for row in rows if _clean(row.get("purchase_order_no"))}
        ).values_list("purchase_order_no", flat=True)
    )
    seen_order_material_keys = set()

    for index, row in enumerate(rows, start=2):
        order_no = _clean(row.get("purchase_order_no"))
        supplier_no = _clean(row.get("supplier_no"))
        material_code = _clean(row.get("material_code"))
        if order_no and order_no in existing_order_nos:
            errors.append({"row": index, "field": "purchase_order_no", "message": "采购单号已存在"})

        supplier = suppliers.get(supplier_no)
        if not supplier_no:
            errors.append({"row": index, "field": "supplier_no", "message": "供应商编号不能为空"})
        elif not supplier or supplier.status != Supplier.SupplierStatus.ACTIVE:
            errors.append({"row": index, "field": "supplier_no", "message": "供应商不存在或未启用"})

        order_date = _parse_date(row.get("order_date"))
        if not order_date:
            errors.append({"row": index, "field": "order_date", "message": "采购日期格式错误，应为 YYYY-MM-DD"})

        needed_date_value = _clean(row.get("needed_date"))
        needed_date = _parse_date(needed_date_value) if needed_date_value else None
        if needed_date_value and not needed_date:
            errors.append({"row": index, "field": "needed_date", "message": "需求日期格式错误，应为 YYYY-MM-DD"})

        material = materials.get(material_code)
        if not material_code:
            errors.append({"row": index, "field": "material_code", "message": "物料编码不能为空"})
        elif not material or material.status != Material.MaterialStatus.ACTIVE:
            errors.append({"row": index, "field": "material_code", "message": "物料不存在或未启用"})

        order_qty = _parse_decimal(row.get("order_qty"))
        if order_qty is None or order_qty <= ZERO:
            errors.append({"row": index, "field": "order_qty", "message": "采购数量必须大于 0"})

        unit_price_text = _clean(row.get("unit_price"))
        unit_price = None
        if can_import_amount and unit_price_text:
            unit_price = _parse_decimal(unit_price_text)
            if unit_price is None or unit_price < ZERO:
                errors.append({"row": index, "field": "unit_price", "message": "采购单价不能小于 0，且必须是数字"})
        if unit_price is None and material and supplier:
            unit_price = _default_purchase_price(material, supplier)

        group_key = order_no or f"__row_{index}"
        duplicate_key = (group_key, material.id if material else material_code)
        if duplicate_key in seen_order_material_keys:
            errors.append({"row": index, "field": "material_code", "message": "同一采购单中物料不能重复"})
        seen_order_material_keys.add(duplicate_key)

        if errors and any(error["row"] == index for error in errors):
            continue

        group = grouped_rows.setdefault(
            group_key,
            {
                "purchase_order_no": order_no,
                "supplier": supplier,
                "order_date": order_date,
                "remark": _clean(row.get("remark")),
                "items": [],
            },
        )
        if group["supplier"].id != supplier.id:
            errors.append({"row": index, "field": "supplier_no", "message": "同一采购单号下供应商必须一致"})
            continue
        if group["order_date"] != order_date:
            errors.append({"row": index, "field": "order_date", "message": "同一采购单号下采购日期必须一致"})
            continue
        group["items"].append(
            {
                "material": material,
                "order_qty": order_qty,
                "unit_price": unit_price,
                "needed_date": needed_date,
            }
        )

    return errors, grouped_rows


def _validate_supplier_return_rows(rows: list[dict], can_import_amount: bool) -> tuple[list[dict], OrderedDict]:
    errors = []
    grouped_rows = OrderedDict()
    if not rows:
        return [{"row": 0, "field": "", "message": "导入文件没有数据行"}], grouped_rows

    supplier_nos = {_clean(row.get("supplier_no")) for row in rows if _clean(row.get("supplier_no"))}
    receipt_nos = {_clean(row.get("purchase_receipt_no")) for row in rows if _clean(row.get("purchase_receipt_no"))}
    material_codes = {_clean(row.get("material_code")) for row in rows if _clean(row.get("material_code"))}
    batch_nos = {_clean(row.get("batch_no")) for row in rows if _clean(row.get("batch_no"))}
    location_codes = {_clean(row.get("location_code")) for row in rows if _clean(row.get("location_code"))}
    suppliers = {supplier.supplier_no: supplier for supplier in Supplier.objects.filter(supplier_no__in=supplier_nos)}
    receipts = {
        receipt.purchase_receipt_no: receipt
        for receipt in PurchaseReceipt.objects.select_related("supplier").filter(purchase_receipt_no__in=receipt_nos)
    }
    receipt_items = {
        (item.purchase_receipt.purchase_receipt_no, item.purchase_order_item.line_no): item
        for item in PurchaseReceiptItem.objects.select_related(
            "purchase_receipt",
            "purchase_receipt__supplier",
            "purchase_order_item",
            "material",
            "batch",
            "location",
        ).filter(purchase_receipt__purchase_receipt_no__in=receipt_nos)
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
    existing_return_nos = set(
        SupplierReturn.objects.filter(
            supplier_return_no__in={_clean(row.get("supplier_return_no")) for row in rows if _clean(row.get("supplier_return_no"))}
        ).values_list("supplier_return_no", flat=True)
    )
    seen_return_item_keys = set()
    pending_qty_by_receipt_item = {}
    pending_qty_by_batch = {}
    receivable_statuses = [PurchaseReceipt.Status.PARTIAL_RECEIVED, PurchaseReceipt.Status.RECEIVED]

    for index, row in enumerate(rows, start=2):
        return_no = _clean(row.get("supplier_return_no"))
        supplier_no = _clean(row.get("supplier_no"))
        receipt_no = _clean(row.get("purchase_receipt_no"))
        line_no_text = _clean(row.get("purchase_order_line_no"))
        material_code = _clean(row.get("material_code"))
        batch_no = _clean(row.get("batch_no"))
        location_code = _clean(row.get("location_code"))

        if return_no and return_no in existing_return_nos:
            errors.append({"row": index, "field": "supplier_return_no", "message": "供应商退货单号已存在"})

        supplier = suppliers.get(supplier_no)
        if not supplier_no:
            errors.append({"row": index, "field": "supplier_no", "message": "供应商编号不能为空"})
        elif not supplier or supplier.status != Supplier.SupplierStatus.ACTIVE:
            errors.append({"row": index, "field": "supplier_no", "message": "供应商不存在或未启用"})

        purchase_receipt = None
        if receipt_no:
            purchase_receipt = receipts.get(receipt_no)
            if not purchase_receipt or purchase_receipt.status not in receivable_statuses:
                errors.append({"row": index, "field": "purchase_receipt_no", "message": "来源进货单不存在或未入库"})
            elif supplier and purchase_receipt.supplier_id != supplier.id:
                errors.append({"row": index, "field": "purchase_receipt_no", "message": "来源进货单必须属于所选供应商"})

        return_date = _parse_date(row.get("return_date"))
        if not return_date:
            errors.append({"row": index, "field": "return_date", "message": "退货日期格式错误，应为 YYYY-MM-DD"})

        purchase_receipt_item = None
        if line_no_text:
            if not receipt_no:
                errors.append({"row": index, "field": "purchase_receipt_no", "message": "填写采购订单行号时必须填写来源进货单号"})
            elif not line_no_text.isdigit():
                errors.append({"row": index, "field": "purchase_order_line_no", "message": "采购订单行号必须是正整数"})
            else:
                purchase_receipt_item = receipt_items.get((receipt_no, int(line_no_text)))
                if not purchase_receipt_item:
                    errors.append({"row": index, "field": "purchase_order_line_no", "message": "来源进货明细行不存在"})
                elif purchase_receipt_item.accepted_qty <= ZERO:
                    errors.append({"row": index, "field": "purchase_order_line_no", "message": "来源进货明细没有可退合格数量"})

        material = materials.get(material_code)
        if purchase_receipt_item:
            if material_code and material and material.id != purchase_receipt_item.material_id:
                errors.append({"row": index, "field": "material_code", "message": "退货物料必须与来源进货行物料一致"})
            material = purchase_receipt_item.material
        elif not material_code:
            errors.append({"row": index, "field": "material_code", "message": "未填写来源进货行时退货物料编码不能为空"})
        elif not material or material.status != Material.MaterialStatus.ACTIVE:
            errors.append({"row": index, "field": "material_code", "message": "退货物料不存在或未启用"})

        return_qty = _parse_decimal(row.get("return_qty"))
        if return_qty is None or return_qty <= ZERO:
            errors.append({"row": index, "field": "return_qty", "message": "退货数量必须大于 0"})

        if purchase_receipt_item and return_qty is not None and return_qty > ZERO:
            already_returned_qty = (
                SupplierReturnItem.objects.filter(purchase_receipt_item=purchase_receipt_item)
                .exclude(supplier_return__status=SupplierReturn.Status.VOIDED)
                .aggregate(total=models_sum("return_qty"))
                .get("total")
                or ZERO
            )
            pending_qty = pending_qty_by_receipt_item.get(purchase_receipt_item.id, ZERO)
            max_return_qty = purchase_receipt_item.accepted_qty - already_returned_qty - pending_qty
            if return_qty > max_return_qty:
                errors.append({"row": index, "field": "return_qty", "message": "退货数量不能超过来源进货行可退数量"})

        unit_price = _parse_decimal(row.get("unit_price"))
        if can_import_amount:
            if unit_price is None:
                unit_price = purchase_receipt_item.unit_price if purchase_receipt_item else _safe_default_purchase_price(material, supplier)
            if unit_price < ZERO:
                errors.append({"row": index, "field": "unit_price", "message": "退货单价不能小于 0"})
        else:
            unit_price = purchase_receipt_item.unit_price if purchase_receipt_item else _safe_default_purchase_price(material, supplier)

        batch = None
        if batch_no:
            batch = batches.get(batch_no)
            if not batch or batch.batch_status != InventoryBatch.BatchStatus.IN_STOCK or batch.remaining_qty <= ZERO:
                errors.append({"row": index, "field": "batch_no", "message": "批次不存在、未在库或无可用库存"})
            elif batch.inventory_type != InventoryBatch.InventoryType.AVAILABLE:
                errors.append({"row": index, "field": "batch_no", "message": "供应商退货只能选择可用库存批次"})
            elif material and batch.material_id != material.id:
                errors.append({"row": index, "field": "batch_no", "message": "批次物料必须与退货物料一致"})
            elif return_qty and return_qty > batch.remaining_qty - pending_qty_by_batch.get(batch.id, ZERO):
                errors.append({"row": index, "field": "return_qty", "message": "退货数量不能超过批次剩余数量"})
        elif purchase_receipt_item and purchase_receipt_item.batch_id:
            batch = purchase_receipt_item.batch
            if return_qty and return_qty > batch.remaining_qty - pending_qty_by_batch.get(batch.id, ZERO):
                errors.append({"row": index, "field": "return_qty", "message": "退货数量不能超过批次剩余数量"})

        location = None
        if location_code:
            location = locations.get(location_code)
            if not location or location.status != WarehouseLocation.LocationStatus.ACTIVE:
                errors.append({"row": index, "field": "location_code", "message": "库位不存在或未启用"})
        if batch:
            if location and batch.location_id != location.id:
                errors.append({"row": index, "field": "location_code", "message": "库位必须与批次库位一致"})
            location = batch.location
        elif purchase_receipt_item:
            location = purchase_receipt_item.location

        group_key = return_no or f"__row_{index}"
        duplicate_key = (
            group_key,
            purchase_receipt_item.id if purchase_receipt_item else None,
            material.id if material else material_code,
        )
        if duplicate_key in seen_return_item_keys:
            errors.append({"row": index, "field": "material_code", "message": "同一供应商退货单中相同来源行和物料不能重复"})
        seen_return_item_keys.add(duplicate_key)

        if errors and any(error["row"] == index for error in errors):
            continue

        group = grouped_rows.setdefault(
            group_key,
            {
                "supplier_return_no": return_no,
                "supplier": supplier,
                "purchase_receipt": purchase_receipt,
                "return_date": return_date,
                "remark": _clean(row.get("remark")),
                "items": [],
            },
        )
        if group["supplier"].id != supplier.id:
            errors.append({"row": index, "field": "supplier_no", "message": "同一供应商退货单号下供应商必须一致"})
            continue
        if (group["purchase_receipt"].id if group["purchase_receipt"] else None) != (purchase_receipt.id if purchase_receipt else None):
            errors.append({"row": index, "field": "purchase_receipt_no", "message": "同一供应商退货单号下来源进货单必须一致"})
            continue
        if group["return_date"] != return_date:
            errors.append({"row": index, "field": "return_date", "message": "同一供应商退货单号下退货日期必须一致"})
            continue

        group["items"].append(
            {
                "purchase_receipt_item": purchase_receipt_item,
                "material": material,
                "return_qty": return_qty,
                "unit_price": unit_price,
                "batch": batch,
                "location": location,
                "return_reason": _clean(row.get("return_reason")),
            }
        )
        if purchase_receipt_item:
            pending_qty_by_receipt_item[purchase_receipt_item.id] = (
                pending_qty_by_receipt_item.get(purchase_receipt_item.id, ZERO) + return_qty
            )
        if batch:
            pending_qty_by_batch[batch.id] = pending_qty_by_batch.get(batch.id, ZERO) + return_qty

    return errors, grouped_rows


def _validate_purchase_receipt_rows(rows: list[dict]) -> tuple[list[dict], OrderedDict]:
    errors = []
    grouped_rows = OrderedDict()
    if not rows:
        return [{"row": 0, "field": "", "message": "导入文件没有数据行"}], grouped_rows

    order_nos = {_clean(row.get("purchase_order_no")) for row in rows if _clean(row.get("purchase_order_no"))}
    material_codes = {_clean(row.get("material_code")) for row in rows if _clean(row.get("material_code"))}
    location_codes = {_clean(row.get("location_code")) for row in rows if _clean(row.get("location_code"))}
    orders = {
        order.purchase_order_no: order
        for order in PurchaseOrder.objects.select_related("supplier").filter(purchase_order_no__in=order_nos)
    }
    order_items = {
        (item.purchase_order.purchase_order_no, item.line_no): item
        for item in PurchaseOrderItem.objects.select_related("purchase_order", "material").filter(
            purchase_order__purchase_order_no__in=order_nos
        )
    }
    materials = {material.material_code: material for material in Material.objects.filter(material_code__in=material_codes)}
    locations = {
        location.location_code: location
        for location in WarehouseLocation.objects.filter(location_code__in=location_codes)
    }
    existing_receipt_nos = set(
        PurchaseReceipt.objects.filter(
            purchase_receipt_no__in={_clean(row.get("purchase_receipt_no")) for row in rows if _clean(row.get("purchase_receipt_no"))}
        ).values_list("purchase_receipt_no", flat=True)
    )
    pending_accepted_by_order_item = {
        row["purchase_order_item_id"]: row["total"] or ZERO
        for row in PurchaseReceiptItem.objects.filter(
            purchase_receipt__status__in=[
                PurchaseReceipt.Status.DRAFT,
                PurchaseReceipt.Status.PENDING_APPROVAL,
                PurchaseReceipt.Status.PENDING_RECEIVE,
                PurchaseReceipt.Status.PARTIAL_RECEIVED,
            ],
            purchase_order_item__purchase_order__purchase_order_no__in=order_nos,
        )
        .values("purchase_order_item_id")
        .annotate(total=models_sum("accepted_qty"))
    }
    csv_accepted_by_order_item: dict[int, Decimal] = {}
    seen_receipt_item_keys = set()

    for index, row in enumerate(rows, start=2):
        receipt_no = _clean(row.get("purchase_receipt_no"))
        order_no = _clean(row.get("purchase_order_no"))
        line_no_text = _clean(row.get("purchase_order_line_no"))
        material_code = _clean(row.get("material_code"))
        location_code = _clean(row.get("location_code"))

        if receipt_no and receipt_no in existing_receipt_nos:
            errors.append({"row": index, "field": "purchase_receipt_no", "message": "进货单号已存在"})

        purchase_order = orders.get(order_no)
        if not order_no:
            errors.append({"row": index, "field": "purchase_order_no", "message": "采购单号不能为空"})
        elif not purchase_order:
            errors.append({"row": index, "field": "purchase_order_no", "message": "来源采购单不存在"})
        elif purchase_order.status not in [PurchaseOrder.Status.APPROVED, PurchaseOrder.Status.PARTIAL_RECEIVED]:
            errors.append({"row": index, "field": "purchase_order_no", "message": "来源采购单状态不能生成进货单"})

        receipt_date = _parse_date(row.get("receipt_date"))
        if not receipt_date:
            errors.append({"row": index, "field": "receipt_date", "message": "进货日期格式错误，应为 YYYY-MM-DD"})

        purchase_order_item = None
        if not line_no_text:
            errors.append({"row": index, "field": "purchase_order_line_no", "message": "采购订单行号不能为空"})
        elif not line_no_text.isdigit() or int(line_no_text) <= 0:
            errors.append({"row": index, "field": "purchase_order_line_no", "message": "采购订单行号必须是正整数"})
        elif order_no:
            purchase_order_item = order_items.get((order_no, int(line_no_text)))
            if not purchase_order_item:
                errors.append({"row": index, "field": "purchase_order_line_no", "message": "来源采购订单行不存在"})
            elif purchase_order_item.line_status == PurchaseOrderItem.LineStatus.CLOSED:
                errors.append({"row": index, "field": "purchase_order_line_no", "message": "来源采购订单行已关闭"})

        material = materials.get(material_code)
        if purchase_order_item:
            if material_code and material and material.id != purchase_order_item.material_id:
                errors.append({"row": index, "field": "material_code", "message": "进货物料必须与来源采购行物料一致"})
            material = purchase_order_item.material
        elif not material_code:
            errors.append({"row": index, "field": "material_code", "message": "物料编码不能为空"})
        elif not material or material.status != Material.MaterialStatus.ACTIVE:
            errors.append({"row": index, "field": "material_code", "message": "物料不存在或未启用"})

        received_qty = _parse_decimal(row.get("received_qty"))
        if received_qty is None or received_qty <= ZERO:
            errors.append({"row": index, "field": "received_qty", "message": "到货数量必须大于 0"})
        accepted_qty = _parse_decimal(row.get("accepted_qty"))
        if accepted_qty is None:
            accepted_qty = received_qty
        if accepted_qty is None or accepted_qty < ZERO:
            errors.append({"row": index, "field": "accepted_qty", "message": "合格数量不能小于 0"})
        rejected_qty = _parse_decimal(row.get("rejected_qty"))
        if rejected_qty is None:
            rejected_qty = ZERO
        if rejected_qty < ZERO:
            errors.append({"row": index, "field": "rejected_qty", "message": "不合格数量不能小于 0"})
        if received_qty is not None and accepted_qty is not None and rejected_qty is not None and accepted_qty + rejected_qty > received_qty:
            errors.append({"row": index, "field": "accepted_qty", "message": "合格数量与不合格数量之和不能超过到货数量"})

        if purchase_order_item and accepted_qty is not None and accepted_qty >= ZERO:
            already_planned_qty = (
                pending_accepted_by_order_item.get(purchase_order_item.id, ZERO)
                + csv_accepted_by_order_item.get(purchase_order_item.id, ZERO)
            )
            remaining_qty = max(ZERO, purchase_order_item.order_qty - purchase_order_item.received_qty - already_planned_qty)
            if accepted_qty > remaining_qty:
                errors.append({"row": index, "field": "accepted_qty", "message": "合格数量不能超过采购行剩余未到货数量"})

        location = locations.get(location_code)
        if not location_code:
            errors.append({"row": index, "field": "location_code", "message": "入库库位不能为空"})
        elif not location or location.status != WarehouseLocation.LocationStatus.ACTIVE:
            errors.append({"row": index, "field": "location_code", "message": "库位不存在或未启用"})

        group_key = receipt_no or f"__row_{index}"
        duplicate_key = (group_key, purchase_order_item.id if purchase_order_item else line_no_text)
        if duplicate_key in seen_receipt_item_keys:
            errors.append({"row": index, "field": "purchase_order_line_no", "message": "同一进货单中采购订单行不能重复"})
        seen_receipt_item_keys.add(duplicate_key)

        if errors and any(error["row"] == index for error in errors):
            continue

        group = grouped_rows.setdefault(
            group_key,
            {
                "purchase_receipt_no": receipt_no,
                "purchase_order": purchase_order,
                "receipt_date": receipt_date,
                "remark": _clean(row.get("remark")),
                "items": [],
            },
        )
        if group["purchase_order"].id != purchase_order.id:
            errors.append({"row": index, "field": "purchase_order_no", "message": "同一进货单号下来源采购单必须一致"})
            continue
        if group["receipt_date"] != receipt_date:
            errors.append({"row": index, "field": "receipt_date", "message": "同一进货单号下进货日期必须一致"})
            continue

        csv_accepted_by_order_item[purchase_order_item.id] = csv_accepted_by_order_item.get(purchase_order_item.id, ZERO) + accepted_qty
        group["items"].append(
            {
                "purchase_order_item": purchase_order_item,
                "material": material,
                "received_qty": received_qty,
                "accepted_qty": accepted_qty,
                "rejected_qty": rejected_qty,
                "location": location,
            }
        )

    return errors, grouped_rows


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


def _default_purchase_price(material: Material, supplier: Supplier | None) -> Decimal:
    if supplier:
        price = (
            MaterialSupplierPrice.objects.filter(
                material=material,
                supplier=supplier,
                status=MaterialSupplierPrice.PriceStatus.ACTIVE,
            )
            .order_by("-is_default", "-effective_from", "-id")
            .first()
        )
        if price:
            return price.purchase_price
    return material.latest_purchase_price or Decimal("0")


def _safe_default_purchase_price(material: Material | None, supplier: Supplier | None) -> Decimal:
    if not material:
        return Decimal("0")
    return _default_purchase_price(material, supplier)


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(MONEY_QUANT)


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

