from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from io import TextIOBase

from django.db import transaction
from django.contrib.auth import get_user_model
from django.utils import timezone

from files.models import ImportJob
from files.services import CsvImportReadError, csv_import_header_row, read_csv_dict_rows
from system.services import ServiceResult, next_document_no

from .models import Customer, CustomerAddress, CustomerProduct, Material, MaterialSupplierPrice, MaterialUnitConversion, Supplier


MATERIAL_IMPORT_COLUMNS = (
    "material_code",
    "material_name",
    "material_type",
    "base_unit",
    "spec",
    "qty_precision",
    "min_stock_qty",
    "latest_purchase_price",
    "status",
    "remark",
)


MATERIAL_IMPORT_TEMPLATE_ROWS = (
    csv_import_header_row(MATERIAL_IMPORT_COLUMNS),
    ("RM001", "示例原料", "raw", "kg", "通用规格", "3", "0", "12.345600", "active", "示例行，导入前可删除"),
    ("FG001", "示例成品", "finished", "pcs", "成品规格", "0", "0", "", "active", ""),
)

CUSTOMER_IMPORT_COLUMNS = (
    "customer_no",
    "customer_name",
    "short_name",
    "sales_owner_username",
    "settlement_method",
    "contact_phone",
    "status",
    "remark",
)

CUSTOMER_IMPORT_TEMPLATE_ROWS = (
    csv_import_header_row(CUSTOMER_IMPORT_COLUMNS),
    ("C001", "示例客户", "示例", "", "月结", "13800000000", "active", "示例行，导入前可删除"),
)

SUPPLIER_IMPORT_COLUMNS = (
    "supplier_no",
    "supplier_name",
    "contact_name",
    "contact_phone",
    "supplier_type",
    "payment_method",
    "status",
    "remark",
)

SUPPLIER_IMPORT_TEMPLATE_ROWS = (
    csv_import_header_row(SUPPLIER_IMPORT_COLUMNS),
    ("S001", "示例供应商", "李四", "13900000000", "原料", "月结", "active", "示例行，导入前可删除"),
)

CUSTOMER_PRODUCT_IMPORT_COLUMNS = (
    "customer_no",
    "customer_product_no",
    "customer_product_name",
    "finished_material_code",
    "default_sale_price",
    "status",
)

CUSTOMER_PRODUCT_IMPORT_TEMPLATE_ROWS = (
    csv_import_header_row(CUSTOMER_PRODUCT_IMPORT_COLUMNS),
    ("C001", "CP001", "示例客户产品", "FG001", "88.0000", "active"),
)

MATERIAL_UNIT_CONVERSION_IMPORT_COLUMNS = (
    "material_code",
    "source_unit",
    "target_unit",
    "ratio",
    "status",
)

MATERIAL_UNIT_CONVERSION_IMPORT_TEMPLATE_ROWS = (
    csv_import_header_row(MATERIAL_UNIT_CONVERSION_IMPORT_COLUMNS),
    ("RM001", "g", "kg", "0.00100000", "active"),
)

CUSTOMER_ADDRESS_IMPORT_COLUMNS = (
    "customer_no",
    "address_type",
    "receiver_name",
    "receiver_phone",
    "address",
    "is_default",
    "status",
)

CUSTOMER_ADDRESS_IMPORT_TEMPLATE_ROWS = (
    csv_import_header_row(CUSTOMER_ADDRESS_IMPORT_COLUMNS),
    ("C001", "shipping", "王五", "13800000000", "深圳市示例路 1 号", "true", "active"),
)

MATERIAL_SUPPLIER_PRICE_IMPORT_COLUMNS = (
    "material_code",
    "supplier_no",
    "purchase_price",
    "currency",
    "effective_from",
    "effective_to",
    "is_default",
    "status",
)

MATERIAL_SUPPLIER_PRICE_IMPORT_TEMPLATE_ROWS = (
    csv_import_header_row(MATERIAL_SUPPLIER_PRICE_IMPORT_COLUMNS),
    ("RM001", "S001", "12.345600", "CNY", "2026-06-09", "", "true", "active"),
)


def import_materials_from_csv(file_obj: TextIOBase, operator_id: int | None = None) -> ServiceResult:
    job = ImportJob.objects.create(
        job_no=next_document_no("IMP"),
        template_type="materials",
        template_version="v1",
        status=ImportJob.JobStatus.VALIDATING,
        started_at=timezone.now(),
        created_by_id=operator_id,
    )
    try:
        rows = read_csv_dict_rows(file_obj)
        errors = _validate_material_rows(rows)
        if errors:
            job.status = ImportJob.JobStatus.FAILED
            job.failed_count = len(errors)
            job.error_summary = {"errors": errors[:50]}
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "failed_count", "error_summary", "finished_at"])
            return ServiceResult(
                False,
                "FILE_IMPORT_VALIDATION_FAILED",
                "物料导入校验失败",
                data={"import_job_id": job.id, "errors": errors},
            )

        job.status = ImportJob.JobStatus.IMPORTING
        job.save(update_fields=["status"])
        with transaction.atomic():
            materials = [_build_material(row, operator_id) for row in rows]
            Material.objects.bulk_create(materials)

        job.status = ImportJob.JobStatus.SUCCESS
        job.success_count = len(rows)
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "success_count", "finished_at"])
        return ServiceResult(
            True,
            message="物料导入完成",
            data={"import_job_id": job.id, "success_count": len(rows), "failed_count": 0},
        )
    except UnicodeDecodeError:
        return _fail_import_job(job, "导入文件编码错误，请使用 UTF-8 CSV")
    except CsvImportReadError as exc:
        return _fail_import_job(job, str(exc), exc.error_code)
    except Exception as exc:
        return _fail_import_job(job, f"物料导入失败：{exc}", "FILE_IMPORT_FAILED")


def import_customers_from_csv(file_obj: TextIOBase, operator_id: int | None = None) -> ServiceResult:
    job = _start_import_job("customers", operator_id)
    try:
        rows = read_csv_dict_rows(file_obj)
        errors, owner_map = _validate_customer_rows(rows)
        if errors:
            return _validation_failed(job, "客户导入校验失败", errors)

        job.status = ImportJob.JobStatus.IMPORTING
        job.save(update_fields=["status"])
        with transaction.atomic():
            customers = [_build_customer(row, owner_map, operator_id) for row in rows]
            Customer.objects.bulk_create(customers)
        return _import_success(job, len(rows), "客户导入完成")
    except UnicodeDecodeError:
        return _fail_import_job(job, "导入文件编码错误，请使用 UTF-8 CSV")
    except CsvImportReadError as exc:
        return _fail_import_job(job, str(exc), exc.error_code)
    except Exception as exc:
        return _fail_import_job(job, f"客户导入失败：{exc}", "FILE_IMPORT_FAILED")


def import_suppliers_from_csv(file_obj: TextIOBase, operator_id: int | None = None) -> ServiceResult:
    job = _start_import_job("suppliers", operator_id)
    try:
        rows = read_csv_dict_rows(file_obj)
        errors = _validate_supplier_rows(rows)
        if errors:
            return _validation_failed(job, "供应商导入校验失败", errors)

        job.status = ImportJob.JobStatus.IMPORTING
        job.save(update_fields=["status"])
        with transaction.atomic():
            suppliers = [_build_supplier(row, operator_id) for row in rows]
            Supplier.objects.bulk_create(suppliers)
        return _import_success(job, len(rows), "供应商导入完成")
    except UnicodeDecodeError:
        return _fail_import_job(job, "导入文件编码错误，请使用 UTF-8 CSV")
    except CsvImportReadError as exc:
        return _fail_import_job(job, str(exc), exc.error_code)
    except Exception as exc:
        return _fail_import_job(job, f"供应商导入失败：{exc}", "FILE_IMPORT_FAILED")


def import_customer_products_from_csv(file_obj: TextIOBase, operator_id: int | None = None) -> ServiceResult:
    job = _start_import_job("customer_products", operator_id)
    try:
        rows = read_csv_dict_rows(file_obj)
        errors, customer_map, material_map = _validate_customer_product_rows(rows)
        if errors:
            return _validation_failed(job, "客户产品导入校验失败", errors)

        job.status = ImportJob.JobStatus.IMPORTING
        job.save(update_fields=["status"])
        with transaction.atomic():
            products = [_build_customer_product(row, customer_map, material_map, operator_id) for row in rows]
            CustomerProduct.objects.bulk_create(products)
        return _import_success(job, len(rows), "客户产品导入完成")
    except UnicodeDecodeError:
        return _fail_import_job(job, "导入文件编码错误，请使用 UTF-8 CSV")
    except CsvImportReadError as exc:
        return _fail_import_job(job, str(exc), exc.error_code)
    except Exception as exc:
        return _fail_import_job(job, f"客户产品导入失败：{exc}", "FILE_IMPORT_FAILED")


def import_material_unit_conversions_from_csv(file_obj: TextIOBase, operator_id: int | None = None) -> ServiceResult:
    job = _start_import_job("material_unit_conversions", operator_id)
    try:
        rows = read_csv_dict_rows(file_obj)
        errors, material_map = _validate_material_unit_conversion_rows(rows)
        if errors:
            return _validation_failed(job, "物料单位换算导入校验失败", errors)

        job.status = ImportJob.JobStatus.IMPORTING
        job.save(update_fields=["status"])
        with transaction.atomic():
            conversions = [_build_material_unit_conversion(row, material_map, operator_id) for row in rows]
            MaterialUnitConversion.objects.bulk_create(conversions)
        return _import_success(job, len(rows), "物料单位换算导入完成")
    except UnicodeDecodeError:
        return _fail_import_job(job, "导入文件编码错误，请使用 UTF-8 CSV")
    except CsvImportReadError as exc:
        return _fail_import_job(job, str(exc), exc.error_code)
    except Exception as exc:
        return _fail_import_job(job, f"物料单位换算导入失败：{exc}", "FILE_IMPORT_FAILED")


def import_customer_addresses_from_csv(file_obj: TextIOBase, operator_id: int | None = None) -> ServiceResult:
    job = _start_import_job("customer_addresses", operator_id)
    try:
        rows = read_csv_dict_rows(file_obj)
        errors, customer_map = _validate_customer_address_rows(rows)
        if errors:
            return _validation_failed(job, "客户地址导入校验失败", errors)

        job.status = ImportJob.JobStatus.IMPORTING
        job.save(update_fields=["status"])
        with transaction.atomic():
            for row in rows:
                _create_customer_address(row, customer_map, operator_id)
        return _import_success(job, len(rows), "客户地址导入完成")
    except UnicodeDecodeError:
        return _fail_import_job(job, "导入文件编码错误，请使用 UTF-8 CSV")
    except CsvImportReadError as exc:
        return _fail_import_job(job, str(exc), exc.error_code)
    except Exception as exc:
        return _fail_import_job(job, f"客户地址导入失败：{exc}", "FILE_IMPORT_FAILED")


def import_material_supplier_prices_from_csv(file_obj: TextIOBase, operator_id: int | None = None) -> ServiceResult:
    job = _start_import_job("material_supplier_prices", operator_id)
    try:
        rows = read_csv_dict_rows(file_obj)
        errors, material_map, supplier_map = _validate_material_supplier_price_rows(rows)
        if errors:
            return _validation_failed(job, "物料供应商价格导入校验失败", errors)

        job.status = ImportJob.JobStatus.IMPORTING
        job.save(update_fields=["status"])
        with transaction.atomic():
            for row in rows:
                _create_material_supplier_price(row, material_map, supplier_map, operator_id)
        return _import_success(job, len(rows), "物料供应商价格导入完成")
    except UnicodeDecodeError:
        return _fail_import_job(job, "导入文件编码错误，请使用 UTF-8 CSV")
    except CsvImportReadError as exc:
        return _fail_import_job(job, str(exc), exc.error_code)
    except Exception as exc:
        return _fail_import_job(job, f"物料供应商价格导入失败：{exc}", "FILE_IMPORT_FAILED")


def _validate_material_rows(rows: list[dict[str, str]]) -> list[dict]:
    errors = []
    if not rows:
        return [{"row": 0, "field": "", "message": "导入文件没有数据行"}]

    seen_codes = set()
    existing_codes = set(Material.objects.filter(material_code__in=[_clean(row.get("material_code")) for row in rows]).values_list("material_code", flat=True))
    valid_types = set(Material.MaterialType.values)
    valid_statuses = set(Material.MaterialStatus.values)
    required_fields = ("material_code", "material_name", "material_type", "base_unit")

    for row_no, row in enumerate(rows, start=2):
        for field in required_fields:
            if not _clean(row.get(field)):
                errors.append({"row": row_no, "field": field, "message": "必填字段不能为空"})

        material_code = _clean(row.get("material_code"))
        if material_code:
            if material_code in seen_codes:
                errors.append({"row": row_no, "field": "material_code", "message": "导入文件中物料编码重复"})
            if material_code in existing_codes:
                errors.append({"row": row_no, "field": "material_code", "message": "物料编码已存在"})
            seen_codes.add(material_code)

        material_type = _clean(row.get("material_type"))
        if material_type and material_type not in valid_types:
            errors.append({"row": row_no, "field": "material_type", "message": "物料类型不合法"})

        status = _clean(row.get("status")) or Material.MaterialStatus.ACTIVE
        if status not in valid_statuses:
            errors.append({"row": row_no, "field": "status", "message": "状态不合法"})

        qty_precision = _clean(row.get("qty_precision")) or "0"
        if not qty_precision.isdigit():
            errors.append({"row": row_no, "field": "qty_precision", "message": "数量精度必须是非负整数"})

        for decimal_field in ("min_stock_qty", "latest_purchase_price"):
            value = _clean(row.get(decimal_field))
            if value and _parse_decimal(value) is None:
                errors.append({"row": row_no, "field": decimal_field, "message": "数值格式不合法"})

    return errors


def _validate_customer_rows(rows: list[dict[str, str]]) -> tuple[list[dict], dict[str, object]]:
    errors = []
    if not rows:
        return [{"row": 0, "field": "", "message": "导入文件没有数据行"}], {}

    seen_nos = set()
    existing_nos = set(Customer.objects.filter(customer_no__in=[_clean(row.get("customer_no")) for row in rows]).values_list("customer_no", flat=True))
    valid_statuses = set(Customer.CustomerStatus.values)
    usernames = {_clean(row.get("sales_owner_username")) for row in rows if _clean(row.get("sales_owner_username"))}
    User = get_user_model()
    owner_map = {user.username: user for user in User.objects.filter(username__in=usernames)}

    for row_no, row in enumerate(rows, start=2):
        for field in ("customer_no", "customer_name"):
            if not _clean(row.get(field)):
                errors.append({"row": row_no, "field": field, "message": "必填字段不能为空"})

        customer_no = _clean(row.get("customer_no"))
        if customer_no:
            if customer_no in seen_nos:
                errors.append({"row": row_no, "field": "customer_no", "message": "导入文件中客户编号重复"})
            if customer_no in existing_nos:
                errors.append({"row": row_no, "field": "customer_no", "message": "客户编号已存在"})
            seen_nos.add(customer_no)

        status = _clean(row.get("status")) or Customer.CustomerStatus.ACTIVE
        if status not in valid_statuses:
            errors.append({"row": row_no, "field": "status", "message": "状态不合法"})

        username = _clean(row.get("sales_owner_username"))
        if username and username not in owner_map:
            errors.append({"row": row_no, "field": "sales_owner_username", "message": "销售负责人账号不存在"})

    return errors, owner_map


def _validate_supplier_rows(rows: list[dict[str, str]]) -> list[dict]:
    errors = []
    if not rows:
        return [{"row": 0, "field": "", "message": "导入文件没有数据行"}]

    seen_nos = set()
    existing_nos = set(Supplier.objects.filter(supplier_no__in=[_clean(row.get("supplier_no")) for row in rows]).values_list("supplier_no", flat=True))
    valid_statuses = set(Supplier.SupplierStatus.values)

    for row_no, row in enumerate(rows, start=2):
        for field in ("supplier_no", "supplier_name"):
            if not _clean(row.get(field)):
                errors.append({"row": row_no, "field": field, "message": "必填字段不能为空"})

        supplier_no = _clean(row.get("supplier_no"))
        if supplier_no:
            if supplier_no in seen_nos:
                errors.append({"row": row_no, "field": "supplier_no", "message": "导入文件中供应商编号重复"})
            if supplier_no in existing_nos:
                errors.append({"row": row_no, "field": "supplier_no", "message": "供应商编号已存在"})
            seen_nos.add(supplier_no)

        status = _clean(row.get("status")) or Supplier.SupplierStatus.ACTIVE
        if status not in valid_statuses:
            errors.append({"row": row_no, "field": "status", "message": "状态不合法"})

    return errors


def _validate_customer_product_rows(rows: list[dict[str, str]]) -> tuple[list[dict], dict[str, Customer], dict[str, Material]]:
    errors = []
    if not rows:
        return [{"row": 0, "field": "", "message": "导入文件没有数据行"}], {}, {}

    customer_nos = {_clean(row.get("customer_no")) for row in rows if _clean(row.get("customer_no"))}
    material_codes = {_clean(row.get("finished_material_code")) for row in rows if _clean(row.get("finished_material_code"))}
    customer_map = {customer.customer_no: customer for customer in Customer.objects.filter(customer_no__in=customer_nos)}
    material_map = {material.material_code: material for material in Material.objects.filter(material_code__in=material_codes)}
    valid_statuses = set(CustomerProduct.ProductStatus.values)
    existing_pairs = set(
        CustomerProduct.objects.filter(customer__customer_no__in=customer_nos).values_list(
            "customer__customer_no",
            "customer_product_no",
        )
    )
    seen_pairs = set()

    for row_no, row in enumerate(rows, start=2):
        for field in ("customer_no", "customer_product_no", "customer_product_name"):
            if not _clean(row.get(field)):
                errors.append({"row": row_no, "field": field, "message": "必填字段不能为空"})

        customer_no = _clean(row.get("customer_no"))
        product_no = _clean(row.get("customer_product_no"))
        pair = (customer_no, product_no)
        if customer_no and customer_no not in customer_map:
            errors.append({"row": row_no, "field": "customer_no", "message": "客户编号不存在"})
        if product_no:
            if pair in seen_pairs:
                errors.append({"row": row_no, "field": "customer_product_no", "message": "导入文件中客户产品编号重复"})
            if pair in existing_pairs:
                errors.append({"row": row_no, "field": "customer_product_no", "message": "客户产品编号已存在"})
            seen_pairs.add(pair)

        material_code = _clean(row.get("finished_material_code"))
        if material_code and material_code not in material_map:
            errors.append({"row": row_no, "field": "finished_material_code", "message": "关联成品编码不存在"})

        price = _clean(row.get("default_sale_price"))
        if price and _parse_decimal(price) is None:
            errors.append({"row": row_no, "field": "default_sale_price", "message": "数值格式不合法"})

        status = _clean(row.get("status")) or CustomerProduct.ProductStatus.ACTIVE
        if status not in valid_statuses:
            errors.append({"row": row_no, "field": "status", "message": "状态不合法"})

    return errors, customer_map, material_map


def _validate_material_unit_conversion_rows(rows: list[dict[str, str]]) -> tuple[list[dict], dict[str, Material]]:
    errors = []
    if not rows:
        return [{"row": 0, "field": "", "message": "导入文件没有数据行"}], {}

    material_codes = {_clean(row.get("material_code")) for row in rows if _clean(row.get("material_code"))}
    material_map = {material.material_code: material for material in Material.objects.filter(material_code__in=material_codes)}
    existing_pairs = set(
        MaterialUnitConversion.objects.filter(material__material_code__in=material_codes).values_list(
            "material__material_code",
            "source_unit",
            "target_unit",
        )
    )
    valid_statuses = set(MaterialUnitConversion.ConversionStatus.values)
    seen_pairs = set()

    for row_no, row in enumerate(rows, start=2):
        for field in ("material_code", "source_unit", "target_unit", "ratio"):
            if not _clean(row.get(field)):
                errors.append({"row": row_no, "field": field, "message": "必填字段不能为空"})

        material_code = _clean(row.get("material_code"))
        source_unit = _clean(row.get("source_unit"))
        target_unit = _clean(row.get("target_unit"))
        pair = (material_code, source_unit, target_unit)
        if material_code and material_code not in material_map:
            errors.append({"row": row_no, "field": "material_code", "message": "物料编码不存在"})
        if source_unit and target_unit and source_unit == target_unit:
            errors.append({"row": row_no, "field": "target_unit", "message": "源单位和目标单位不能相同"})
        if source_unit and target_unit:
            if pair in seen_pairs:
                errors.append({"row": row_no, "field": "source_unit", "message": "导入文件中单位换算重复"})
            if pair in existing_pairs:
                errors.append({"row": row_no, "field": "source_unit", "message": "单位换算已存在"})
            seen_pairs.add(pair)

        ratio = _parse_decimal(_clean(row.get("ratio")))
        if ratio is None:
            errors.append({"row": row_no, "field": "ratio", "message": "换算比例格式不合法"})
        elif ratio <= Decimal("0"):
            errors.append({"row": row_no, "field": "ratio", "message": "换算比例必须大于 0"})

        status = _clean(row.get("status")) or MaterialUnitConversion.ConversionStatus.ACTIVE
        if status not in valid_statuses:
            errors.append({"row": row_no, "field": "status", "message": "状态不合法"})

    return errors, material_map


def _validate_customer_address_rows(rows: list[dict[str, str]]) -> tuple[list[dict], dict[str, Customer]]:
    errors = []
    if not rows:
        return [{"row": 0, "field": "", "message": "导入文件没有数据行"}], {}

    customer_nos = {_clean(row.get("customer_no")) for row in rows if _clean(row.get("customer_no"))}
    customer_map = {customer.customer_no: customer for customer in Customer.objects.filter(customer_no__in=customer_nos)}
    valid_address_types = set(CustomerAddress.AddressType.values)
    valid_statuses = set(CustomerAddress.AddressStatus.values)
    default_keys = set()

    for row_no, row in enumerate(rows, start=2):
        for field in ("customer_no", "address_type", "receiver_name", "address"):
            if not _clean(row.get(field)):
                errors.append({"row": row_no, "field": field, "message": "必填字段不能为空"})

        customer_no = _clean(row.get("customer_no"))
        address_type = _clean(row.get("address_type")) or CustomerAddress.AddressType.SHIPPING
        if customer_no and customer_no not in customer_map:
            errors.append({"row": row_no, "field": "customer_no", "message": "客户编号不存在"})
        if address_type not in valid_address_types:
            errors.append({"row": row_no, "field": "address_type", "message": "地址类型不合法"})

        parsed_default = _parse_bool(_clean(row.get("is_default")))
        if parsed_default is None:
            errors.append({"row": row_no, "field": "is_default", "message": "是否默认格式不合法"})
        elif parsed_default:
            key = (customer_no, address_type)
            if key in default_keys:
                errors.append({"row": row_no, "field": "is_default", "message": "同一客户同一地址类型只能有一个默认地址"})
            default_keys.add(key)

        status = _clean(row.get("status")) or CustomerAddress.AddressStatus.ACTIVE
        if status not in valid_statuses:
            errors.append({"row": row_no, "field": "status", "message": "状态不合法"})

    return errors, customer_map


def _validate_material_supplier_price_rows(
    rows: list[dict[str, str]],
) -> tuple[list[dict], dict[str, Material], dict[str, Supplier]]:
    errors = []
    if not rows:
        return [{"row": 0, "field": "", "message": "导入文件没有数据行"}], {}, {}

    material_codes = {_clean(row.get("material_code")) for row in rows if _clean(row.get("material_code"))}
    supplier_nos = {_clean(row.get("supplier_no")) for row in rows if _clean(row.get("supplier_no"))}
    material_map = {material.material_code: material for material in Material.objects.filter(material_code__in=material_codes)}
    supplier_map = {supplier.supplier_no: supplier for supplier in Supplier.objects.filter(supplier_no__in=supplier_nos)}
    valid_statuses = set(MaterialSupplierPrice.PriceStatus.values)
    seen_default_materials = set()
    seen_price_keys = set()

    for row_no, row in enumerate(rows, start=2):
        for field in ("material_code", "supplier_no", "purchase_price"):
            if not _clean(row.get(field)):
                errors.append({"row": row_no, "field": field, "message": "必填字段不能为空"})

        material_code = _clean(row.get("material_code"))
        supplier_no = _clean(row.get("supplier_no"))
        if material_code and material_code not in material_map:
            errors.append({"row": row_no, "field": "material_code", "message": "物料编码不存在"})
        if supplier_no and supplier_no not in supplier_map:
            errors.append({"row": row_no, "field": "supplier_no", "message": "供应商编号不存在"})

        purchase_price = _parse_decimal(_clean(row.get("purchase_price")))
        if purchase_price is None:
            errors.append({"row": row_no, "field": "purchase_price", "message": "采购价格格式不合法"})
        elif purchase_price < Decimal("0"):
            errors.append({"row": row_no, "field": "purchase_price", "message": "采购价格不能小于 0"})

        effective_from = _parse_date(_clean(row.get("effective_from")))
        effective_to = _parse_date(_clean(row.get("effective_to")))
        if _clean(row.get("effective_from")) and effective_from is None:
            errors.append({"row": row_no, "field": "effective_from", "message": "生效日期格式不合法"})
        if _clean(row.get("effective_to")) and effective_to is None:
            errors.append({"row": row_no, "field": "effective_to", "message": "失效日期格式不合法"})
        if effective_from and effective_to and effective_to < effective_from:
            errors.append({"row": row_no, "field": "effective_to", "message": "失效日期不能早于生效日期"})

        price_key = (material_code, supplier_no, _clean(row.get("effective_from")), _clean(row.get("effective_to")))
        if material_code and supplier_no:
            if price_key in seen_price_keys:
                errors.append({"row": row_no, "field": "supplier_no", "message": "导入文件中供应商价格重复"})
            seen_price_keys.add(price_key)

        parsed_default = _parse_bool(_clean(row.get("is_default")))
        if parsed_default is None:
            errors.append({"row": row_no, "field": "is_default", "message": "是否默认格式不合法"})
        elif parsed_default:
            if material_code in seen_default_materials:
                errors.append({"row": row_no, "field": "is_default", "message": "同一物料只能导入一个默认供应商价格"})
            seen_default_materials.add(material_code)

        status = _clean(row.get("status")) or MaterialSupplierPrice.PriceStatus.ACTIVE
        if status not in valid_statuses:
            errors.append({"row": row_no, "field": "status", "message": "状态不合法"})

    return errors, material_map, supplier_map


def _build_material(row: dict[str, str], operator_id: int | None) -> Material:
    return Material(
        material_code=_clean(row.get("material_code")),
        material_name=_clean(row.get("material_name")),
        material_type=_clean(row.get("material_type")),
        spec=_clean(row.get("spec")),
        base_unit=_clean(row.get("base_unit")),
        qty_precision=int(_clean(row.get("qty_precision")) or "0"),
        min_stock_qty=_parse_decimal(_clean(row.get("min_stock_qty"))) or Decimal("0"),
        latest_purchase_price=_parse_decimal(_clean(row.get("latest_purchase_price"))),
        status=_clean(row.get("status")) or Material.MaterialStatus.ACTIVE,
        remark=_clean(row.get("remark")),
        created_by_id=operator_id,
        updated_by_id=operator_id,
    )


def _build_customer(row: dict[str, str], owner_map: dict[str, object], operator_id: int | None) -> Customer:
    username = _clean(row.get("sales_owner_username"))
    return Customer(
        customer_no=_clean(row.get("customer_no")),
        customer_name=_clean(row.get("customer_name")),
        short_name=_clean(row.get("short_name")),
        sales_owner=owner_map.get(username) if username else None,
        settlement_method=_clean(row.get("settlement_method")),
        contact_phone_encrypted=_clean(row.get("contact_phone")),
        status=_clean(row.get("status")) or Customer.CustomerStatus.ACTIVE,
        remark=_clean(row.get("remark")),
        created_by_id=operator_id,
        updated_by_id=operator_id,
    )


def _build_supplier(row: dict[str, str], operator_id: int | None) -> Supplier:
    return Supplier(
        supplier_no=_clean(row.get("supplier_no")),
        supplier_name=_clean(row.get("supplier_name")),
        contact_name=_clean(row.get("contact_name")),
        contact_phone_encrypted=_clean(row.get("contact_phone")),
        supplier_type=_clean(row.get("supplier_type")),
        payment_method=_clean(row.get("payment_method")),
        status=_clean(row.get("status")) or Supplier.SupplierStatus.ACTIVE,
        remark=_clean(row.get("remark")),
        created_by_id=operator_id,
        updated_by_id=operator_id,
    )


def _build_customer_product(
    row: dict[str, str],
    customer_map: dict[str, Customer],
    material_map: dict[str, Material],
    operator_id: int | None,
) -> CustomerProduct:
    material_code = _clean(row.get("finished_material_code"))
    return CustomerProduct(
        customer=customer_map[_clean(row.get("customer_no"))],
        customer_product_no=_clean(row.get("customer_product_no")),
        customer_product_name=_clean(row.get("customer_product_name")),
        finished_material=material_map.get(material_code) if material_code else None,
        default_sale_price=_parse_decimal(_clean(row.get("default_sale_price"))),
        status=_clean(row.get("status")) or CustomerProduct.ProductStatus.ACTIVE,
        created_by_id=operator_id,
        updated_by_id=operator_id,
    )


def _build_material_unit_conversion(
    row: dict[str, str],
    material_map: dict[str, Material],
    operator_id: int | None,
) -> MaterialUnitConversion:
    return MaterialUnitConversion(
        material=material_map[_clean(row.get("material_code"))],
        source_unit=_clean(row.get("source_unit")),
        target_unit=_clean(row.get("target_unit")),
        ratio=_parse_decimal(_clean(row.get("ratio"))) or Decimal("0"),
        status=_clean(row.get("status")) or MaterialUnitConversion.ConversionStatus.ACTIVE,
        created_by_id=operator_id,
        updated_by_id=operator_id,
    )


def _create_customer_address(
    row: dict[str, str],
    customer_map: dict[str, Customer],
    operator_id: int | None,
) -> CustomerAddress:
    customer = customer_map[_clean(row.get("customer_no"))]
    address_type = _clean(row.get("address_type")) or CustomerAddress.AddressType.SHIPPING
    is_default = bool(_parse_bool(_clean(row.get("is_default"))))
    if is_default:
        CustomerAddress.objects.filter(customer=customer, address_type=address_type).update(is_default=False)
    return CustomerAddress.objects.create(
        customer=customer,
        address_type=address_type,
        receiver_name=_clean(row.get("receiver_name")),
        receiver_phone_encrypted=_clean(row.get("receiver_phone")),
        address_encrypted=_clean(row.get("address")),
        is_default=is_default,
        status=_clean(row.get("status")) or CustomerAddress.AddressStatus.ACTIVE,
        created_by_id=operator_id,
        updated_by_id=operator_id,
    )


def _create_material_supplier_price(
    row: dict[str, str],
    material_map: dict[str, Material],
    supplier_map: dict[str, Supplier],
    operator_id: int | None,
) -> MaterialSupplierPrice:
    material = material_map[_clean(row.get("material_code"))]
    is_default = bool(_parse_bool(_clean(row.get("is_default"))))
    if is_default:
        MaterialSupplierPrice.objects.filter(material=material, status=MaterialSupplierPrice.PriceStatus.ACTIVE).update(is_default=False)
    return MaterialSupplierPrice.objects.create(
        material=material,
        supplier=supplier_map[_clean(row.get("supplier_no"))],
        purchase_price=_parse_decimal(_clean(row.get("purchase_price"))) or Decimal("0"),
        currency=_clean(row.get("currency")) or "CNY",
        effective_from=_parse_date(_clean(row.get("effective_from"))),
        effective_to=_parse_date(_clean(row.get("effective_to"))),
        is_default=is_default,
        status=_clean(row.get("status")) or MaterialSupplierPrice.PriceStatus.ACTIVE,
        created_by_id=operator_id,
        updated_by_id=operator_id,
    )


def _parse_decimal(value: str) -> Decimal | None:
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _parse_bool(value: str) -> bool | None:
    if not value:
        return False
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "y", "是"}:
        return True
    if normalized in {"false", "0", "no", "n", "否"}:
        return False
    return None


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
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

