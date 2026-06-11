from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import TextIOBase

from django.db import transaction
from django.db.models import Q, Sum as models_sum
from django.utils import timezone

from files.models import ImportJob
from files.services import CsvImportReadError, read_csv_dict_rows
from inventory.models import InventoryBatch, WarehouseLocation
from masterdata.models import Customer, CustomerAddress, CustomerProduct, Material
from system.services import ServiceResult, next_document_no

from .models import (
    CustomerReturn,
    CustomerReturnItem,
    SampleLoan,
    SampleLoanItem,
    SalesOrder,
    SalesOrderItem,
    SalesShipment,
    SalesShipmentItem,
)


SALES_ORDER_IMPORT_COLUMNS = (
    "sales_order_no",
    "customer_no",
    "customer_address_id",
    "order_date",
    "delivery_date",
    "customer_product_no",
    "order_qty",
    "unit_price",
    "remark",
)

SALES_ORDER_IMPORT_TEMPLATE_ROWS = (
    SALES_ORDER_IMPORT_COLUMNS,
    ("SO-INIT-001", "C001", "", "2026-06-10", "2026-06-20", "CP001", "10", "88.0000", "示例行，导入前可删除"),
    ("SO-INIT-001", "C001", "", "2026-06-10", "2026-06-20", "CP002", "5", "66.0000", ""),
)

SAMPLE_LOAN_IMPORT_COLUMNS = (
    "sample_loan_no",
    "customer_no",
    "loan_date",
    "expected_return_date",
    "material_code",
    "loan_qty",
    "batch_no",
    "location_code",
    "line_expected_return_date",
    "remark",
)

SAMPLE_LOAN_IMPORT_TEMPLATE_ROWS = (
    SAMPLE_LOAN_IMPORT_COLUMNS,
    ("SL-INIT-001", "C001", "2026-06-10", "2026-06-20", "FG001", "2", "BATCH001", "A01", "2026-06-20", "示例行，导入前可删除"),
    ("SL-INIT-001", "C001", "2026-06-10", "2026-06-20", "FG002", "1", "", "", "2026-06-22", ""),
)

CUSTOMER_RETURN_IMPORT_COLUMNS = (
    "return_no",
    "customer_no",
    "sales_order_no",
    "return_date",
    "sales_order_line_no",
    "material_code",
    "return_qty",
    "unit_price",
    "location_code",
    "inventory_type",
    "return_reason",
    "remark",
)

CUSTOMER_RETURN_IMPORT_TEMPLATE_ROWS = (
    CUSTOMER_RETURN_IMPORT_COLUMNS,
    ("RT-INIT-001", "C001", "SO001", "2026-06-10", "1", "FG001", "1", "88.0000", "A01", "available", "客户退回", "示例行，导入前可删除"),
    ("RT-INIT-001", "C001", "SO001", "2026-06-10", "2", "FG002", "1", "66.0000", "A01", "available", "", ""),
)

SALES_SHIPMENT_IMPORT_COLUMNS = (
    "shipment_no",
    "sales_order_no",
    "shipment_date",
    "sales_order_line_no",
    "material_code",
    "shipment_qty",
    "batch_no",
    "location_code",
    "remark",
)

SALES_SHIPMENT_IMPORT_TEMPLATE_ROWS = (
    SALES_SHIPMENT_IMPORT_COLUMNS,
    ("SS-INIT-001", "SO001", "2026-06-10", "1", "FG001", "5", "BATCH001", "A01", "示例行，导入前可删除"),
    ("SS-INIT-001", "SO001", "2026-06-10", "2", "FG002", "3", "BATCH002", "A01", ""),
)

ZERO = Decimal("0")
MONEY_QUANT = Decimal("0.01")


def import_sales_orders_from_csv(file_obj: TextIOBase, operator_id: int | None = None, can_import_amount: bool = False) -> ServiceResult:
    job = _start_import_job("sales_orders", operator_id)
    try:
        rows = read_csv_dict_rows(file_obj)
        errors, grouped_rows = _validate_sales_order_rows(rows, can_import_amount=can_import_amount)
        if errors:
            return _validation_failed(job, "销售订单导入校验失败", errors)

        job.status = ImportJob.JobStatus.IMPORTING
        job.save(update_fields=["status"])
        created_count = 0
        with transaction.atomic():
            for group in grouped_rows.values():
                order = SalesOrder.objects.create(
                    sales_order_no=group["sales_order_no"] or next_document_no("SO"),
                    customer=group["customer"],
                    customer_address=group["customer_address"],
                    order_date=group["order_date"],
                    delivery_date=group["delivery_date"],
                    status=SalesOrder.Status.DRAFT,
                    created_by_id=operator_id,
                    updated_by_id=operator_id,
                    remark=group["remark"],
                )
                total_amount = Decimal("0.00")
                for line_no, line in enumerate(group["items"], start=1):
                    unit_price = line["unit_price"] if can_import_amount else (line["customer_product"].default_sale_price or ZERO)
                    line_amount = _money(line["order_qty"] * unit_price)
                    SalesOrderItem.objects.create(
                        sales_order=order,
                        line_no=line_no,
                        customer_product=line["customer_product"],
                        finished_material=line["customer_product"].finished_material,
                        order_qty=line["order_qty"],
                        unit_price=unit_price,
                        line_amount=line_amount,
                        line_status=SalesOrderItem.LineStatus.DRAFT,
                    )
                    total_amount += line_amount
                order.total_amount = _money(total_amount)
                order.save(update_fields=["total_amount"])
                created_count += 1
        return _import_success(job, created_count, "销售订单导入完成")
    except UnicodeDecodeError:
        return _fail_import_job(job, "导入文件编码错误，请使用 UTF-8 CSV")
    except CsvImportReadError as exc:
        return _fail_import_job(job, str(exc), exc.error_code)
    except Exception as exc:
        return _fail_import_job(job, f"销售订单导入失败：{exc}", "FILE_IMPORT_FAILED")


def import_sample_loans_from_csv(file_obj: TextIOBase, operator_id: int | None = None) -> ServiceResult:
    job = _start_import_job("sample_loans", operator_id)
    try:
        rows = read_csv_dict_rows(file_obj)
        errors, grouped_rows = _validate_sample_loan_rows(rows)
        if errors:
            return _validation_failed(job, "借样单导入校验失败", errors)

        job.status = ImportJob.JobStatus.IMPORTING
        job.save(update_fields=["status"])
        created_count = 0
        with transaction.atomic():
            for group in grouped_rows.values():
                loan = SampleLoan.objects.create(
                    sample_loan_no=group["sample_loan_no"] or next_document_no("SL"),
                    customer=group["customer"],
                    loan_date=group["loan_date"],
                    expected_return_date=group["expected_return_date"],
                    status=SampleLoan.Status.PENDING_APPROVAL,
                    created_by_id=operator_id,
                    remark=group["remark"],
                )
                for line_no, line in enumerate(group["items"], start=1):
                    SampleLoanItem.objects.create(
                        sample_loan=loan,
                        line_no=line_no,
                        material=line["material"],
                        loan_qty=line["loan_qty"],
                        expected_return_date=line["expected_return_date"] or loan.expected_return_date,
                        batch=line["batch"],
                        location=line["location"],
                        line_status=SampleLoanItem.LineStatus.OUT,
                    )
                created_count += 1
        return _import_success(job, created_count, "借样单导入完成")
    except UnicodeDecodeError:
        return _fail_import_job(job, "导入文件编码错误，请使用 UTF-8 CSV")
    except CsvImportReadError as exc:
        return _fail_import_job(job, str(exc), exc.error_code)
    except Exception as exc:
        return _fail_import_job(job, f"借样单导入失败：{exc}", "FILE_IMPORT_FAILED")


def import_customer_returns_from_csv(
    file_obj: TextIOBase,
    operator_id: int | None = None,
    can_import_amount: bool = False,
) -> ServiceResult:
    job = _start_import_job("customer_returns", operator_id)
    try:
        rows = read_csv_dict_rows(file_obj)
        errors, grouped_rows = _validate_customer_return_rows(rows, can_import_amount=can_import_amount)
        if errors:
            return _validation_failed(job, "客户退货导入校验失败", errors)

        job.status = ImportJob.JobStatus.IMPORTING
        job.save(update_fields=["status"])
        created_count = 0
        with transaction.atomic():
            for group in grouped_rows.values():
                customer_return = CustomerReturn.objects.create(
                    return_no=group["return_no"] or next_document_no("RT"),
                    customer=group["customer"],
                    sales_order=group["sales_order"],
                    return_date=group["return_date"],
                    status=CustomerReturn.Status.DRAFT,
                    remark=group["remark"],
                    return_amount=Decimal("0.00"),
                )
                total_amount = Decimal("0.00")
                for line in group["items"]:
                    return_amount = _money(line["return_qty"] * line["unit_price"])
                    CustomerReturnItem.objects.create(
                        customer_return=customer_return,
                        sales_order_item=line["sales_order_item"],
                        material=line["material"],
                        return_qty=line["return_qty"],
                        unit_price=line["unit_price"],
                        return_amount=return_amount,
                        location=line["location"],
                        inventory_type=line["inventory_type"],
                        return_reason=line["return_reason"],
                    )
                    total_amount += return_amount
                customer_return.return_amount = _money(total_amount)
                customer_return.save(update_fields=["return_amount"])
                created_count += 1
        return _import_success(job, created_count, "客户退货导入完成")
    except UnicodeDecodeError:
        return _fail_import_job(job, "导入文件编码错误，请使用 UTF-8 CSV")
    except CsvImportReadError as exc:
        return _fail_import_job(job, str(exc), exc.error_code)
    except Exception as exc:
        return _fail_import_job(job, f"客户退货导入失败：{exc}", "FILE_IMPORT_FAILED")


def import_sales_shipments_from_csv(
    file_obj: TextIOBase,
    operator_id: int | None = None,
    can_view_all: bool = False,
) -> ServiceResult:
    job = _start_import_job("sales_shipments", operator_id)
    try:
        rows = read_csv_dict_rows(file_obj)
        errors, grouped_rows = _validate_sales_shipment_rows(rows, operator_id=operator_id, can_view_all=can_view_all)
        if errors:
            return _validation_failed(job, "销售出库导入校验失败", errors)

        job.status = ImportJob.JobStatus.IMPORTING
        job.save(update_fields=["status"])
        created_count = 0
        with transaction.atomic():
            for group in grouped_rows.values():
                shipment = SalesShipment.objects.create(
                    shipment_no=group["shipment_no"] or next_document_no("SS"),
                    sales_order=group["sales_order"],
                    customer=group["sales_order"].customer,
                    shipment_date=group["shipment_date"],
                    status=SalesShipment.Status.PENDING_CONFIRM,
                    created_by_id=operator_id,
                    remark=group["remark"],
                )
                for line in group["items"]:
                    SalesShipmentItem.objects.create(
                        shipment=shipment,
                        sales_order_item=line["sales_order_item"],
                        material=line["material"],
                        shipment_qty=line["shipment_qty"],
                        batch=line["batch"],
                        location=line["batch"].location,
                        cost_price=line["batch"].cost_price,
                    )
                created_count += 1
        return _import_success(job, created_count, "销售出库导入完成")
    except UnicodeDecodeError:
        return _fail_import_job(job, "导入文件编码错误，请使用 UTF-8 CSV")
    except CsvImportReadError as exc:
        return _fail_import_job(job, str(exc), exc.error_code)
    except Exception as exc:
        return _fail_import_job(job, f"销售出库导入失败：{exc}", "FILE_IMPORT_FAILED")


def _validate_sales_order_rows(rows: list[dict], can_import_amount: bool) -> tuple[list[dict], OrderedDict]:
    errors = []
    grouped_rows = OrderedDict()
    if not rows:
        return [{"row": 0, "field": "", "message": "导入文件没有数据行"}], grouped_rows

    customer_nos = {_clean(row.get("customer_no")) for row in rows if _clean(row.get("customer_no"))}
    customers = {customer.customer_no: customer for customer in Customer.objects.filter(customer_no__in=customer_nos)}
    product_keys = {
        (_clean(row.get("customer_no")), _clean(row.get("customer_product_no")))
        for row in rows
        if _clean(row.get("customer_no")) and _clean(row.get("customer_product_no"))
    }
    customer_products = {
        (product.customer.customer_no, product.customer_product_no): product
        for product in CustomerProduct.objects.select_related("customer", "finished_material").filter(
            customer__customer_no__in={customer_no for customer_no, _product_no in product_keys},
            customer_product_no__in={product_no for _customer_no, product_no in product_keys},
        )
    }
    existing_order_nos = set(
        SalesOrder.objects.filter(
            sales_order_no__in={_clean(row.get("sales_order_no")) for row in rows if _clean(row.get("sales_order_no"))}
        ).values_list("sales_order_no", flat=True)
    )
    seen_order_product_keys = set()

    for index, row in enumerate(rows, start=2):
        order_no = _clean(row.get("sales_order_no"))
        customer_no = _clean(row.get("customer_no"))
        product_no = _clean(row.get("customer_product_no"))
        if order_no and order_no in existing_order_nos:
            errors.append({"row": index, "field": "sales_order_no", "message": "销售订单号已存在"})
        if not customer_no:
            errors.append({"row": index, "field": "customer_no", "message": "客户编号不能为空"})
            continue
        customer = customers.get(customer_no)
        if not customer or customer.status != Customer.CustomerStatus.ACTIVE:
            errors.append({"row": index, "field": "customer_no", "message": "客户不存在或未启用"})
            continue
        if not product_no:
            errors.append({"row": index, "field": "customer_product_no", "message": "客户产品编号不能为空"})
            continue
        product = customer_products.get((customer_no, product_no))
        if not product or product.status != CustomerProduct.ProductStatus.ACTIVE:
            errors.append({"row": index, "field": "customer_product_no", "message": "客户产品不存在或未启用"})
            continue
        if not product.finished_material_id:
            errors.append({"row": index, "field": "customer_product_no", "message": "客户产品未关联成品编码"})
            continue

        order_date = _parse_date(row.get("order_date"))
        if not order_date:
            errors.append({"row": index, "field": "order_date", "message": "订单日期格式错误，应为 YYYY-MM-DD"})
        delivery_date_value = _clean(row.get("delivery_date"))
        delivery_date = _parse_date(delivery_date_value) if delivery_date_value else None
        if delivery_date_value and not delivery_date:
            errors.append({"row": index, "field": "delivery_date", "message": "交期格式错误，应为 YYYY-MM-DD"})
        if order_date and delivery_date and delivery_date < order_date:
            errors.append({"row": index, "field": "delivery_date", "message": "交期不能早于订单日期"})

        order_qty = _parse_decimal(row.get("order_qty"))
        if order_qty is None or order_qty <= ZERO:
            errors.append({"row": index, "field": "order_qty", "message": "订单数量必须大于 0"})

        unit_price = _parse_decimal(row.get("unit_price"))
        if can_import_amount:
            if unit_price is None:
                unit_price = product.default_sale_price or ZERO
            if unit_price < ZERO:
                errors.append({"row": index, "field": "unit_price", "message": "单价不能小于 0"})
        else:
            unit_price = product.default_sale_price or ZERO

        customer_address = _address_from_row(row, customer, index, errors)
        group_key = order_no or f"__row_{index}"
        duplicate_key = (group_key, product.id)
        if duplicate_key in seen_order_product_keys:
            errors.append({"row": index, "field": "customer_product_no", "message": "同一销售订单中客户产品不能重复"})
        seen_order_product_keys.add(duplicate_key)

        if errors and any(error["row"] == index for error in errors):
            continue

        group = grouped_rows.setdefault(
            group_key,
            {
                "sales_order_no": order_no,
                "customer": customer,
                "customer_address": customer_address,
                "order_date": order_date,
                "delivery_date": delivery_date,
                "remark": _clean(row.get("remark")),
                "items": [],
            },
        )
        _validate_group_header(group, row, index, customer, customer_address, order_date, delivery_date, errors)
        if errors and any(error["row"] == index for error in errors):
            continue
        group["items"].append(
            {
                "customer_product": product,
                "order_qty": order_qty,
                "unit_price": unit_price,
            }
        )

    return errors, grouped_rows


def _validate_sample_loan_rows(rows: list[dict]) -> tuple[list[dict], OrderedDict]:
    errors = []
    grouped_rows = OrderedDict()
    if not rows:
        return [{"row": 0, "field": "", "message": "导入文件没有数据行"}], grouped_rows

    customer_nos = {_clean(row.get("customer_no")) for row in rows if _clean(row.get("customer_no"))}
    material_codes = {_clean(row.get("material_code")) for row in rows if _clean(row.get("material_code"))}
    batch_nos = {_clean(row.get("batch_no")) for row in rows if _clean(row.get("batch_no"))}
    location_codes = {_clean(row.get("location_code")) for row in rows if _clean(row.get("location_code"))}
    customers = {customer.customer_no: customer for customer in Customer.objects.filter(customer_no__in=customer_nos)}
    materials = {material.material_code: material for material in Material.objects.filter(material_code__in=material_codes)}
    batches = {
        batch.batch_no: batch
        for batch in InventoryBatch.objects.select_related("material", "location").filter(batch_no__in=batch_nos)
    }
    locations = {
        location.location_code: location
        for location in WarehouseLocation.objects.filter(location_code__in=location_codes)
    }
    existing_loan_nos = set(
        SampleLoan.objects.filter(
            sample_loan_no__in={_clean(row.get("sample_loan_no")) for row in rows if _clean(row.get("sample_loan_no"))}
        ).values_list("sample_loan_no", flat=True)
    )
    seen_loan_material_keys = set()

    for index, row in enumerate(rows, start=2):
        loan_no = _clean(row.get("sample_loan_no"))
        customer_no = _clean(row.get("customer_no"))
        material_code = _clean(row.get("material_code"))
        batch_no = _clean(row.get("batch_no"))
        location_code = _clean(row.get("location_code"))

        if loan_no and loan_no in existing_loan_nos:
            errors.append({"row": index, "field": "sample_loan_no", "message": "借样单号已存在"})

        customer = customers.get(customer_no)
        if not customer_no:
            errors.append({"row": index, "field": "customer_no", "message": "客户编号不能为空"})
        elif not customer or customer.status != Customer.CustomerStatus.ACTIVE:
            errors.append({"row": index, "field": "customer_no", "message": "客户不存在或未启用"})

        loan_date = _parse_date(row.get("loan_date"))
        if not loan_date:
            errors.append({"row": index, "field": "loan_date", "message": "借出日期格式错误，应为 YYYY-MM-DD"})
        expected_return_date = _parse_date(row.get("expected_return_date"))
        if not expected_return_date:
            errors.append({"row": index, "field": "expected_return_date", "message": "预计归还日期格式错误，应为 YYYY-MM-DD"})
        if loan_date and expected_return_date and expected_return_date < loan_date:
            errors.append({"row": index, "field": "expected_return_date", "message": "预计归还日期不能早于借出日期"})

        line_expected_return_date_value = _clean(row.get("line_expected_return_date"))
        line_expected_return_date = _parse_date(line_expected_return_date_value) if line_expected_return_date_value else None
        if line_expected_return_date_value and not line_expected_return_date:
            errors.append({"row": index, "field": "line_expected_return_date", "message": "明细预计归还日期格式错误，应为 YYYY-MM-DD"})
        if loan_date and line_expected_return_date and line_expected_return_date < loan_date:
            errors.append({"row": index, "field": "line_expected_return_date", "message": "明细预计归还日期不能早于借出日期"})

        material = materials.get(material_code)
        if not material_code:
            errors.append({"row": index, "field": "material_code", "message": "样品物料编码不能为空"})
        elif not material or material.status != Material.MaterialStatus.ACTIVE or material.material_type != Material.MaterialType.FINISHED:
            errors.append({"row": index, "field": "material_code", "message": "样品物料不存在、未启用或不是成品"})

        loan_qty = _parse_decimal(row.get("loan_qty"))
        if loan_qty is None or loan_qty <= ZERO:
            errors.append({"row": index, "field": "loan_qty", "message": "借出数量必须大于 0"})

        batch = None
        if batch_no:
            batch = batches.get(batch_no)
            if not batch or batch.batch_status != InventoryBatch.BatchStatus.IN_STOCK or batch.remaining_qty <= ZERO:
                errors.append({"row": index, "field": "batch_no", "message": "批次不存在、未在库或无可用库存"})
            elif material and batch.material_id != material.id:
                errors.append({"row": index, "field": "batch_no", "message": "批次物料必须与样品物料一致"})
            elif loan_qty and batch.remaining_qty < loan_qty:
                errors.append({"row": index, "field": "loan_qty", "message": "借样数量不能超过批次剩余数量"})

        location = None
        if location_code:
            location = locations.get(location_code)
            if not location or location.status != WarehouseLocation.LocationStatus.ACTIVE:
                errors.append({"row": index, "field": "location_code", "message": "库位不存在或未启用"})
        if batch:
            if location and batch.location_id != location.id:
                errors.append({"row": index, "field": "location_code", "message": "库位必须与批次库位一致"})
            location = batch.location

        group_key = loan_no or f"__row_{index}"
        duplicate_key = (group_key, material.id if material else material_code)
        if duplicate_key in seen_loan_material_keys:
            errors.append({"row": index, "field": "material_code", "message": "同一借样单中样品物料不能重复"})
        seen_loan_material_keys.add(duplicate_key)

        if errors and any(error["row"] == index for error in errors):
            continue

        group = grouped_rows.setdefault(
            group_key,
            {
                "sample_loan_no": loan_no,
                "customer": customer,
                "loan_date": loan_date,
                "expected_return_date": expected_return_date,
                "remark": _clean(row.get("remark")),
                "items": [],
            },
        )
        if group["customer"].id != customer.id:
            errors.append({"row": index, "field": "customer_no", "message": "同一借样单号下客户必须一致"})
            continue
        if group["loan_date"] != loan_date:
            errors.append({"row": index, "field": "loan_date", "message": "同一借样单号下借出日期必须一致"})
            continue
        if group["expected_return_date"] != expected_return_date:
            errors.append({"row": index, "field": "expected_return_date", "message": "同一借样单号下预计归还日期必须一致"})
            continue
        group["items"].append(
            {
                "material": material,
                "loan_qty": loan_qty,
                "expected_return_date": line_expected_return_date,
                "batch": batch,
                "location": location,
            }
        )

    return errors, grouped_rows


def _validate_customer_return_rows(rows: list[dict], can_import_amount: bool) -> tuple[list[dict], OrderedDict]:
    errors = []
    grouped_rows = OrderedDict()
    if not rows:
        return [{"row": 0, "field": "", "message": "导入文件没有数据行"}], grouped_rows

    customer_nos = {_clean(row.get("customer_no")) for row in rows if _clean(row.get("customer_no"))}
    order_nos = {_clean(row.get("sales_order_no")) for row in rows if _clean(row.get("sales_order_no"))}
    material_codes = {_clean(row.get("material_code")) for row in rows if _clean(row.get("material_code"))}
    location_codes = {_clean(row.get("location_code")) for row in rows if _clean(row.get("location_code"))}
    customers = {customer.customer_no: customer for customer in Customer.objects.filter(customer_no__in=customer_nos)}
    orders = {
        order.sales_order_no: order
        for order in SalesOrder.objects.select_related("customer").filter(sales_order_no__in=order_nos)
    }
    order_items = {
        (item.sales_order.sales_order_no, item.line_no): item
        for item in SalesOrderItem.objects.select_related("sales_order", "finished_material").filter(
            sales_order__sales_order_no__in=order_nos
        )
    }
    materials = {material.material_code: material for material in Material.objects.filter(material_code__in=material_codes)}
    locations = {
        location.location_code: location
        for location in WarehouseLocation.objects.filter(location_code__in=location_codes)
    }
    existing_return_nos = set(
        CustomerReturn.objects.filter(
            return_no__in={_clean(row.get("return_no")) for row in rows if _clean(row.get("return_no"))}
        ).values_list("return_no", flat=True)
    )
    valid_inventory_types = {choice[0] for choice in InventoryBatch.InventoryType.choices}
    seen_return_item_keys = set()

    for index, row in enumerate(rows, start=2):
        return_no = _clean(row.get("return_no"))
        customer_no = _clean(row.get("customer_no"))
        order_no = _clean(row.get("sales_order_no"))
        line_no_text = _clean(row.get("sales_order_line_no"))
        material_code = _clean(row.get("material_code"))
        location_code = _clean(row.get("location_code"))

        if return_no and return_no in existing_return_nos:
            errors.append({"row": index, "field": "return_no", "message": "客户退货单号已存在"})

        customer = customers.get(customer_no)
        if not customer_no:
            errors.append({"row": index, "field": "customer_no", "message": "客户编号不能为空"})
        elif not customer or customer.status != Customer.CustomerStatus.ACTIVE:
            errors.append({"row": index, "field": "customer_no", "message": "客户不存在或未启用"})

        sales_order = None
        if order_no:
            sales_order = orders.get(order_no)
            if not sales_order or sales_order.status not in [SalesOrder.Status.SHIPPED, SalesOrder.Status.COMPLETED]:
                errors.append({"row": index, "field": "sales_order_no", "message": "来源销售订单不存在或未发货完成"})
            elif customer and sales_order.customer_id != customer.id:
                errors.append({"row": index, "field": "sales_order_no", "message": "来源销售订单必须属于所选客户"})

        return_date = _parse_date(row.get("return_date"))
        if not return_date:
            errors.append({"row": index, "field": "return_date", "message": "退货日期格式错误，应为 YYYY-MM-DD"})

        sales_order_item = None
        if line_no_text:
            if not order_no:
                errors.append({"row": index, "field": "sales_order_no", "message": "填写销售订单行号时必须填写来源销售订单号"})
            elif not line_no_text.isdigit():
                errors.append({"row": index, "field": "sales_order_line_no", "message": "销售订单行号必须是正整数"})
            else:
                sales_order_item = order_items.get((order_no, int(line_no_text)))
                if not sales_order_item:
                    errors.append({"row": index, "field": "sales_order_line_no", "message": "来源销售订单行不存在"})
                elif sales_order_item.shipped_qty <= ZERO:
                    errors.append({"row": index, "field": "sales_order_line_no", "message": "来源销售订单行没有可退已发货数量"})

        material = materials.get(material_code)
        if sales_order_item:
            if material_code and material and material.id != sales_order_item.finished_material_id:
                errors.append({"row": index, "field": "material_code", "message": "退货物料必须与来源销售行成品一致"})
            material = sales_order_item.finished_material
        elif not material_code:
            errors.append({"row": index, "field": "material_code", "message": "未填写来源销售行时退货物料编码不能为空"})
        elif not material or material.status != Material.MaterialStatus.ACTIVE or material.material_type != Material.MaterialType.FINISHED:
            errors.append({"row": index, "field": "material_code", "message": "退货物料不存在、未启用或不是成品"})

        return_qty = _parse_decimal(row.get("return_qty"))
        if return_qty is None or return_qty <= ZERO:
            errors.append({"row": index, "field": "return_qty", "message": "退货数量必须大于 0"})

        if sales_order_item and return_qty is not None and return_qty > ZERO:
            returned_qty = (
                CustomerReturnItem.objects.filter(sales_order_item=sales_order_item)
                .exclude(customer_return__status=CustomerReturn.Status.VOIDED)
                .aggregate(total=models_sum("return_qty"))
                .get("total")
                or ZERO
            )
            if return_qty > sales_order_item.shipped_qty - returned_qty:
                errors.append({"row": index, "field": "return_qty", "message": "退货数量不能超过来源销售行可退数量"})

        unit_price = _parse_decimal(row.get("unit_price"))
        if can_import_amount:
            if unit_price is None:
                unit_price = sales_order_item.unit_price if sales_order_item else ZERO
            if unit_price < ZERO:
                errors.append({"row": index, "field": "unit_price", "message": "退货单价不能小于 0"})
        else:
            unit_price = sales_order_item.unit_price if sales_order_item else ZERO

        location = None
        if location_code:
            location = locations.get(location_code)
            if not location or location.status != WarehouseLocation.LocationStatus.ACTIVE:
                errors.append({"row": index, "field": "location_code", "message": "库位不存在或未启用"})

        inventory_type = _clean(row.get("inventory_type")) or InventoryBatch.InventoryType.AVAILABLE
        if inventory_type not in valid_inventory_types:
            errors.append({"row": index, "field": "inventory_type", "message": "库存类型不在允许范围内"})

        group_key = return_no or f"__row_{index}"
        duplicate_key = (
            group_key,
            sales_order_item.id if sales_order_item else None,
            material.id if material else material_code,
        )
        if duplicate_key in seen_return_item_keys:
            errors.append({"row": index, "field": "material_code", "message": "同一退货单中相同来源行和物料不能重复"})
        seen_return_item_keys.add(duplicate_key)

        if errors and any(error["row"] == index for error in errors):
            continue

        group = grouped_rows.setdefault(
            group_key,
            {
                "return_no": return_no,
                "customer": customer,
                "sales_order": sales_order,
                "return_date": return_date,
                "remark": _clean(row.get("remark")),
                "items": [],
            },
        )
        if group["customer"].id != customer.id:
            errors.append({"row": index, "field": "customer_no", "message": "同一退货单号下客户必须一致"})
            continue
        if (group["sales_order"].id if group["sales_order"] else None) != (sales_order.id if sales_order else None):
            errors.append({"row": index, "field": "sales_order_no", "message": "同一退货单号下来源销售订单必须一致"})
            continue
        if group["return_date"] != return_date:
            errors.append({"row": index, "field": "return_date", "message": "同一退货单号下退货日期必须一致"})
            continue
        group["items"].append(
            {
                "sales_order_item": sales_order_item,
                "material": material,
                "return_qty": return_qty,
                "unit_price": unit_price,
                "location": location,
                "inventory_type": inventory_type,
                "return_reason": _clean(row.get("return_reason")),
            }
        )

    return errors, grouped_rows


def _validate_sales_shipment_rows(rows: list[dict], operator_id: int | None, can_view_all: bool) -> tuple[list[dict], OrderedDict]:
    errors = []
    grouped_rows = OrderedDict()
    if not rows:
        return [{"row": 0, "field": "", "message": "导入文件没有数据行"}], grouped_rows

    order_nos = {_clean(row.get("sales_order_no")) for row in rows if _clean(row.get("sales_order_no"))}
    material_codes = {_clean(row.get("material_code")) for row in rows if _clean(row.get("material_code"))}
    batch_nos = {_clean(row.get("batch_no")) for row in rows if _clean(row.get("batch_no"))}
    location_codes = {_clean(row.get("location_code")) for row in rows if _clean(row.get("location_code"))}

    orders_queryset = SalesOrder.objects.select_related("customer").filter(sales_order_no__in=order_nos)
    if not can_view_all:
        orders_queryset = orders_queryset.filter(Q(customer__sales_owner_id=operator_id) | Q(created_by_id=operator_id))
    orders = {order.sales_order_no: order for order in orders_queryset}
    existing_order_nos = set(SalesOrder.objects.filter(sales_order_no__in=order_nos).values_list("sales_order_no", flat=True))
    order_items = {
        (item.sales_order.sales_order_no, item.line_no): item
        for item in SalesOrderItem.objects.select_related("sales_order", "finished_material").filter(
            sales_order__sales_order_no__in=order_nos
        )
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
    existing_shipment_nos = set(
        SalesShipment.objects.filter(
            shipment_no__in={_clean(row.get("shipment_no")) for row in rows if _clean(row.get("shipment_no"))}
        ).values_list("shipment_no", flat=True)
    )
    pending_by_sales_item = {
        row["sales_order_item_id"]: row["total"] or ZERO
        for row in SalesShipmentItem.objects.filter(
            shipment__status__in=[SalesShipment.Status.DRAFT, SalesShipment.Status.PENDING_CONFIRM],
            sales_order_item__sales_order__sales_order_no__in=order_nos,
        )
        .values("sales_order_item_id")
        .annotate(total=models_sum("shipment_qty"))
    }
    pending_by_batch = {
        row["batch_id"]: row["total"] or ZERO
        for row in SalesShipmentItem.objects.filter(
            shipment__status__in=[SalesShipment.Status.DRAFT, SalesShipment.Status.PENDING_CONFIRM],
            batch__batch_no__in=batch_nos,
        )
        .values("batch_id")
        .annotate(total=models_sum("shipment_qty"))
    }
    csv_qty_by_sales_item: dict[int, Decimal] = {}
    csv_qty_by_batch: dict[int, Decimal] = {}
    seen_shipment_item_keys = set()

    for index, row in enumerate(rows, start=2):
        shipment_no = _clean(row.get("shipment_no"))
        order_no = _clean(row.get("sales_order_no"))
        line_no_text = _clean(row.get("sales_order_line_no"))
        material_code = _clean(row.get("material_code"))
        batch_no = _clean(row.get("batch_no"))
        location_code = _clean(row.get("location_code"))

        if shipment_no and shipment_no in existing_shipment_nos:
            errors.append({"row": index, "field": "shipment_no", "message": "销售出库单号已存在"})

        sales_order = orders.get(order_no)
        if not order_no:
            errors.append({"row": index, "field": "sales_order_no", "message": "销售订单号不能为空"})
        elif order_no not in existing_order_nos:
            errors.append({"row": index, "field": "sales_order_no", "message": "来源销售订单不存在"})
        elif not sales_order:
            errors.append({"row": index, "field": "sales_order_no", "message": "来源销售订单不在当前用户数据范围内"})
        elif sales_order.status not in [SalesOrder.Status.CONFIRMED, SalesOrder.Status.IN_PRODUCTION, SalesOrder.Status.SHIPPED]:
            errors.append({"row": index, "field": "sales_order_no", "message": "来源销售订单状态不能生成出库单"})

        shipment_date = _parse_date(row.get("shipment_date"))
        if not shipment_date:
            errors.append({"row": index, "field": "shipment_date", "message": "出库日期格式错误，应为 YYYY-MM-DD"})

        sales_order_item = None
        if not line_no_text:
            errors.append({"row": index, "field": "sales_order_line_no", "message": "销售订单行号不能为空"})
        elif not line_no_text.isdigit() or int(line_no_text) <= 0:
            errors.append({"row": index, "field": "sales_order_line_no", "message": "销售订单行号必须是正整数"})
        elif order_no:
            sales_order_item = order_items.get((order_no, int(line_no_text)))
            if not sales_order_item:
                errors.append({"row": index, "field": "sales_order_line_no", "message": "来源销售订单行不存在"})
            elif sales_order_item.line_status not in [
                SalesOrderItem.LineStatus.CONFIRMED,
                SalesOrderItem.LineStatus.IN_PRODUCTION,
                SalesOrderItem.LineStatus.SHIPPED,
            ]:
                errors.append({"row": index, "field": "sales_order_line_no", "message": "来源销售订单行状态不能出库"})

        material = materials.get(material_code)
        if sales_order_item:
            if material_code and material and material.id != sales_order_item.finished_material_id:
                errors.append({"row": index, "field": "material_code", "message": "出库物料必须与来源销售行成品一致"})
            material = sales_order_item.finished_material
        elif not material_code:
            errors.append({"row": index, "field": "material_code", "message": "出库物料编码不能为空"})
        elif not material or material.status != Material.MaterialStatus.ACTIVE or material.material_type != Material.MaterialType.FINISHED:
            errors.append({"row": index, "field": "material_code", "message": "出库物料不存在、未启用或不是成品"})

        shipment_qty = _parse_decimal(row.get("shipment_qty"))
        if shipment_qty is None or shipment_qty <= ZERO:
            errors.append({"row": index, "field": "shipment_qty", "message": "出库数量必须大于 0"})

        batch = batches.get(batch_no)
        if not batch_no:
            errors.append({"row": index, "field": "batch_no", "message": "批次号不能为空"})
        elif not batch or batch.inventory_type != InventoryBatch.InventoryType.AVAILABLE or batch.batch_status != InventoryBatch.BatchStatus.IN_STOCK:
            errors.append({"row": index, "field": "batch_no", "message": "批次不存在、不是可用库存或未在库"})
        elif material and batch.material_id != material.id:
            errors.append({"row": index, "field": "batch_no", "message": "批次物料必须与出库物料一致"})

        location = None
        if location_code:
            location = locations.get(location_code)
            if not location or location.status != WarehouseLocation.LocationStatus.ACTIVE:
                errors.append({"row": index, "field": "location_code", "message": "库位不存在或未启用"})
        if batch:
            if location and batch.location_id != location.id:
                errors.append({"row": index, "field": "location_code", "message": "库位必须与批次库位一致"})
            location = batch.location

        if sales_order_item and shipment_qty and shipment_qty > ZERO:
            already_planned_qty = pending_by_sales_item.get(sales_order_item.id, ZERO) + csv_qty_by_sales_item.get(sales_order_item.id, ZERO)
            available_to_ship = max(ZERO, sales_order_item.order_qty - sales_order_item.shipped_qty - already_planned_qty)
            if shipment_qty > available_to_ship:
                errors.append({"row": index, "field": "shipment_qty", "message": "出库数量不能超过销售订单行未发货数量"})

        if batch and shipment_qty and shipment_qty > ZERO:
            already_planned_qty = pending_by_batch.get(batch.id, ZERO) + csv_qty_by_batch.get(batch.id, ZERO)
            available_batch_qty = max(ZERO, batch.remaining_qty - already_planned_qty)
            if shipment_qty > available_batch_qty:
                errors.append({"row": index, "field": "shipment_qty", "message": "出库数量不能超过批次可用剩余数量"})

        group_key = shipment_no or f"__row_{index}"
        duplicate_key = (group_key, sales_order_item.id if sales_order_item else line_no_text, batch.id if batch else batch_no)
        if duplicate_key in seen_shipment_item_keys:
            errors.append({"row": index, "field": "batch_no", "message": "同一出库单中相同销售行和批次不能重复"})
        seen_shipment_item_keys.add(duplicate_key)

        if errors and any(error["row"] == index for error in errors):
            continue

        group = grouped_rows.setdefault(
            group_key,
            {
                "shipment_no": shipment_no,
                "sales_order": sales_order,
                "shipment_date": shipment_date,
                "remark": _clean(row.get("remark")),
                "items": [],
            },
        )
        if group["sales_order"].id != sales_order.id:
            errors.append({"row": index, "field": "sales_order_no", "message": "同一出库单号下来源销售订单必须一致"})
            continue
        if group["shipment_date"] != shipment_date:
            errors.append({"row": index, "field": "shipment_date", "message": "同一出库单号下出库日期必须一致"})
            continue

        csv_qty_by_sales_item[sales_order_item.id] = csv_qty_by_sales_item.get(sales_order_item.id, ZERO) + shipment_qty
        csv_qty_by_batch[batch.id] = csv_qty_by_batch.get(batch.id, ZERO) + shipment_qty
        group["items"].append(
            {
                "sales_order_item": sales_order_item,
                "material": material,
                "shipment_qty": shipment_qty,
                "batch": batch,
            }
        )

    return errors, grouped_rows


def _validate_group_header(group, row, index, customer, customer_address, order_date, delivery_date, errors):
    if group["customer"].id != customer.id:
        errors.append({"row": index, "field": "customer_no", "message": "同一销售订单号下客户必须一致"})
    if (group["customer_address"].id if group["customer_address"] else None) != (customer_address.id if customer_address else None):
        errors.append({"row": index, "field": "customer_address_id", "message": "同一销售订单号下客户地址必须一致"})
    if group["order_date"] != order_date:
        errors.append({"row": index, "field": "order_date", "message": "同一销售订单号下订单日期必须一致"})
    if group["delivery_date"] != delivery_date:
        errors.append({"row": index, "field": "delivery_date", "message": "同一销售订单号下交期必须一致"})


def _address_from_row(row, customer, row_no, errors):
    address_id = _clean(row.get("customer_address_id"))
    if address_id:
        if not address_id.isdigit():
            errors.append({"row": row_no, "field": "customer_address_id", "message": "客户地址 ID 必须是数字"})
            return None
        address = CustomerAddress.objects.filter(id=int(address_id), customer=customer, status=CustomerAddress.AddressStatus.ACTIVE).first()
        if not address:
            errors.append({"row": row_no, "field": "customer_address_id", "message": "客户地址不存在、未启用或不属于该客户"})
            return None
        return address
    return customer.addresses.filter(address_type=CustomerAddress.AddressType.SHIPPING, status=CustomerAddress.AddressStatus.ACTIVE, is_default=True).first()


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


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(MONEY_QUANT)


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

