from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import TextIOBase

from django.db import transaction
from django.utils import timezone

from files.models import ImportJob
from files.services import CsvImportReadError, csv_import_header_row, read_csv_dict_rows
from masterdata.models import Customer, Supplier
from system.services import ServiceResult, next_document_no

from .models import CustomerReceipt, SupplierPayment


CUSTOMER_RECEIPT_IMPORT_COLUMNS = (
    "receipt_no",
    "customer_no",
    "receipt_date",
    "receipt_amount",
    "receipt_method",
    "remark",
)

CUSTOMER_RECEIPT_IMPORT_TEMPLATE_ROWS = (
    csv_import_header_row(CUSTOMER_RECEIPT_IMPORT_COLUMNS),
    ("RC-INIT-001", "C001", "2026-06-10", "1000.00", "transfer", "示例行，导入前可删除"),
    ("", "C002", "2026-06-11", "500.00", "cash", ""),
)

SUPPLIER_PAYMENT_IMPORT_COLUMNS = (
    "payment_no",
    "supplier_no",
    "payment_date",
    "payment_amount",
    "payment_method",
    "remark",
)

SUPPLIER_PAYMENT_IMPORT_TEMPLATE_ROWS = (
    csv_import_header_row(SUPPLIER_PAYMENT_IMPORT_COLUMNS),
    ("PY-INIT-001", "S001", "2026-06-10", "800.00", "transfer", "示例行，导入前可删除"),
    ("", "S002", "2026-06-11", "300.00", "cash", ""),
)

ZERO = Decimal("0.00")


def import_customer_receipts_from_csv(file_obj: TextIOBase, operator_id: int | None = None) -> ServiceResult:
    job = _start_import_job("customer_receipts", operator_id)
    try:
        rows = read_csv_dict_rows(file_obj)
        errors, validated_rows = _validate_customer_receipt_rows(rows)
        if errors:
            return _validation_failed(job, "客户收款导入校验失败", errors)

        job.status = ImportJob.JobStatus.IMPORTING
        job.save(update_fields=["status"])
        with transaction.atomic():
            for row in validated_rows:
                CustomerReceipt.objects.create(
                    receipt_no=row["receipt_no"] or next_document_no("RC"),
                    customer=row["customer"],
                    receipt_date=row["receipt_date"],
                    receipt_amount=row["receipt_amount"],
                    unallocated_amount=row["receipt_amount"],
                    receipt_method=row["receipt_method"],
                    status=CustomerReceipt.Status.PENDING_APPROVAL,
                    handled_by_id=operator_id,
                    created_by_id=operator_id,
                    remark=row["remark"],
                )
        return _import_success(job, len(validated_rows), "客户收款导入完成")
    except UnicodeDecodeError:
        return _fail_import_job(job, "导入文件编码错误，请使用 UTF-8 CSV")
    except CsvImportReadError as exc:
        return _fail_import_job(job, str(exc), exc.error_code)
    except Exception as exc:
        return _fail_import_job(job, f"客户收款导入失败：{exc}", "FILE_IMPORT_FAILED")


def import_supplier_payments_from_csv(file_obj: TextIOBase, operator_id: int | None = None) -> ServiceResult:
    job = _start_import_job("supplier_payments", operator_id)
    try:
        rows = read_csv_dict_rows(file_obj)
        errors, validated_rows = _validate_supplier_payment_rows(rows)
        if errors:
            return _validation_failed(job, "供应商付款导入校验失败", errors)

        job.status = ImportJob.JobStatus.IMPORTING
        job.save(update_fields=["status"])
        with transaction.atomic():
            for row in validated_rows:
                SupplierPayment.objects.create(
                    payment_no=row["payment_no"] or next_document_no("PY"),
                    supplier=row["supplier"],
                    payment_date=row["payment_date"],
                    payment_amount=row["payment_amount"],
                    unallocated_amount=row["payment_amount"],
                    payment_method=row["payment_method"],
                    status=SupplierPayment.Status.PENDING_APPROVAL,
                    handled_by_id=operator_id,
                    created_by_id=operator_id,
                    remark=row["remark"],
                )
        return _import_success(job, len(validated_rows), "供应商付款导入完成")
    except UnicodeDecodeError:
        return _fail_import_job(job, "导入文件编码错误，请使用 UTF-8 CSV")
    except CsvImportReadError as exc:
        return _fail_import_job(job, str(exc), exc.error_code)
    except Exception as exc:
        return _fail_import_job(job, f"供应商付款导入失败：{exc}", "FILE_IMPORT_FAILED")


def _validate_customer_receipt_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    errors = []
    validated_rows = []
    if not rows:
        return [{"row": 0, "field": "", "message": "导入文件没有数据行"}], validated_rows

    receipt_nos = {_clean(row.get("receipt_no")) for row in rows if _clean(row.get("receipt_no"))}
    customer_nos = {_clean(row.get("customer_no")) for row in rows if _clean(row.get("customer_no"))}
    existing_receipt_nos = set(CustomerReceipt.objects.filter(receipt_no__in=receipt_nos).values_list("receipt_no", flat=True))
    customers = {customer.customer_no: customer for customer in Customer.objects.filter(customer_no__in=customer_nos)}
    seen_receipt_nos = set()

    for index, row in enumerate(rows, start=2):
        receipt_no = _clean(row.get("receipt_no"))
        customer_no = _clean(row.get("customer_no"))
        if receipt_no:
            if receipt_no in existing_receipt_nos:
                errors.append({"row": index, "field": "receipt_no", "message": "客户收款单号已存在"})
            if receipt_no in seen_receipt_nos:
                errors.append({"row": index, "field": "receipt_no", "message": "导入文件中客户收款单号不能重复"})
            seen_receipt_nos.add(receipt_no)

        customer = customers.get(customer_no)
        if not customer_no:
            errors.append({"row": index, "field": "customer_no", "message": "客户编号不能为空"})
        elif not customer or customer.status != Customer.CustomerStatus.ACTIVE:
            errors.append({"row": index, "field": "customer_no", "message": "客户不存在或未启用"})

        receipt_date = _parse_date(row.get("receipt_date"))
        if not receipt_date:
            errors.append({"row": index, "field": "receipt_date", "message": "收款日期格式错误，应为 YYYY-MM-DD"})

        receipt_amount = _parse_money(row.get("receipt_amount"))
        if receipt_amount is None or receipt_amount <= ZERO:
            errors.append({"row": index, "field": "receipt_amount", "message": "收款金额必须大于 0"})

        receipt_method = _clean(row.get("receipt_method")) or CustomerReceipt.ReceiptMethod.TRANSFER
        if receipt_method not in CustomerReceipt.ReceiptMethod.values:
            errors.append({"row": index, "field": "receipt_method", "message": "收款方式必须是 cash、transfer、check 或 other"})

        if errors and any(error["row"] == index for error in errors):
            continue
        validated_rows.append(
            {
                "receipt_no": receipt_no,
                "customer": customer,
                "receipt_date": receipt_date,
                "receipt_amount": receipt_amount,
                "receipt_method": receipt_method,
                "remark": _clean(row.get("remark")),
            }
        )

    return errors, validated_rows


def _validate_supplier_payment_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    errors = []
    validated_rows = []
    if not rows:
        return [{"row": 0, "field": "", "message": "导入文件没有数据行"}], validated_rows

    payment_nos = {_clean(row.get("payment_no")) for row in rows if _clean(row.get("payment_no"))}
    supplier_nos = {_clean(row.get("supplier_no")) for row in rows if _clean(row.get("supplier_no"))}
    existing_payment_nos = set(SupplierPayment.objects.filter(payment_no__in=payment_nos).values_list("payment_no", flat=True))
    suppliers = {supplier.supplier_no: supplier for supplier in Supplier.objects.filter(supplier_no__in=supplier_nos)}
    seen_payment_nos = set()

    for index, row in enumerate(rows, start=2):
        payment_no = _clean(row.get("payment_no"))
        supplier_no = _clean(row.get("supplier_no"))
        if payment_no:
            if payment_no in existing_payment_nos:
                errors.append({"row": index, "field": "payment_no", "message": "供应商付款单号已存在"})
            if payment_no in seen_payment_nos:
                errors.append({"row": index, "field": "payment_no", "message": "导入文件中供应商付款单号不能重复"})
            seen_payment_nos.add(payment_no)

        supplier = suppliers.get(supplier_no)
        if not supplier_no:
            errors.append({"row": index, "field": "supplier_no", "message": "供应商编号不能为空"})
        elif not supplier or supplier.status != Supplier.SupplierStatus.ACTIVE:
            errors.append({"row": index, "field": "supplier_no", "message": "供应商不存在或未启用"})

        payment_date = _parse_date(row.get("payment_date"))
        if not payment_date:
            errors.append({"row": index, "field": "payment_date", "message": "付款日期格式错误，应为 YYYY-MM-DD"})

        payment_amount = _parse_money(row.get("payment_amount"))
        if payment_amount is None or payment_amount <= ZERO:
            errors.append({"row": index, "field": "payment_amount", "message": "付款金额必须大于 0"})

        payment_method = _clean(row.get("payment_method")) or SupplierPayment.PaymentMethod.TRANSFER
        if payment_method not in SupplierPayment.PaymentMethod.values:
            errors.append({"row": index, "field": "payment_method", "message": "付款方式必须是 cash、transfer、check 或 other"})

        if errors and any(error["row"] == index for error in errors):
            continue
        validated_rows.append(
            {
                "payment_no": payment_no,
                "supplier": supplier,
                "payment_date": payment_date,
                "payment_amount": payment_amount,
                "payment_method": payment_method,
                "remark": _clean(row.get("remark")),
            }
        )

    return errors, validated_rows


def _parse_date(value):
    value = _clean(value)
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_money(value):
    value = _clean(value)
    if not value:
        return None
    try:
        return Decimal(value).quantize(Decimal("0.01"))
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

