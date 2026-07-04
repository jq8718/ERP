from __future__ import annotations

import csv
import json
import shlex
import subprocess
from io import StringIO
from pathlib import Path, PurePosixPath
from uuid import uuid4

from django.conf import settings
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from system.services import ServiceResult, next_document_no

from .models import Attachment, AttachmentAccessLog, ExportLog, PrintLog


ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".xlsx", ".docx"}
MAX_ATTACHMENT_SIZE = 20 * 1024 * 1024
MAX_CSV_IMPORT_SIZE = 5 * 1024 * 1024
MAX_CSV_IMPORT_ROWS = 5000
CSV_IMPORT_ENCODINGS = ("utf-8-sig", "gb18030")


CSV_IMPORT_COLUMN_LABELS = {
    "accepted_qty": "合格数量",
    "address": "地址",
    "address_type": "地址类型",
    "adjust_reason": "调整原因",
    "base_unit": "基本单位",
    "batch_no": "批次号",
    "bom_no": "BOM 编号",
    "bom_version": "BOM 版本",
    "contact_name": "联系人",
    "contact_phone": "联系电话",
    "cost_price": "成本单价",
    "currency": "币种",
    "customer_address_id": "客户地址 ID",
    "customer_name": "客户名称",
    "customer_no": "客户编号",
    "customer_product_name": "客户产品名称",
    "customer_product_no": "客户产品编号",
    "default_sale_price": "默认销售价",
    "delivery_date": "交期",
    "effective_from": "生效日期",
    "effective_to": "失效日期",
    "expected_return_date": "预计归还日期",
    "finished_material_code": "关联成品编码",
    "initial_qty": "期初数量",
    "inventory_type": "库存类型",
    "is_default": "是否默认",
    "issued_qty": "实发数量",
    "latest_purchase_price": "最近采购价",
    "line_expected_return_date": "明细预计归还日期",
    "line_needed_date": "明细需求日期",
    "loan_date": "借样日期",
    "loan_qty": "借样数量",
    "location_code": "库位编码",
    "location_name": "库位名称",
    "material_code": "物料编码",
    "material_name": "物料名称",
    "material_type": "物料类型",
    "min_stock_qty": "最低库存",
    "needed_date": "需求日期",
    "order_date": "订单日期",
    "order_qty": "订单数量",
    "payment_amount": "付款金额",
    "payment_date": "付款日期",
    "payment_method": "付款方式",
    "payment_no": "付款单号",
    "planned_finish_date": "计划完成日期",
    "planned_start_date": "计划开始日期",
    "production_order_no": "生产指令号",
    "production_qty": "生产数量",
    "production_receipt_no": "生产入库单号",
    "purchase_order_line_no": "采购订单行号",
    "purchase_order_no": "采购单号",
    "purchase_price": "采购价",
    "purchase_receipt_no": "进货单号",
    "purchase_request_no": "采购需求单号",
    "quality_status": "质量状态",
    "qty_precision": "数量精度",
    "ratio": "换算比例",
    "receipt_amount": "收款金额",
    "receipt_date": "单据日期",
    "receipt_method": "收款方式",
    "receipt_no": "收款单号",
    "receipt_qty": "入库数量",
    "received_at": "入库时间",
    "received_qty": "到货数量",
    "receiver_name": "收件人",
    "receiver_phone": "收件电话",
    "rejected_qty": "不合格数量",
    "remark": "备注",
    "request_qty": "需求数量",
    "requisition_date": "领料日期",
    "requisition_no": "领料单号",
    "required_qty": "应发数量",
    "return_date": "退货日期",
    "return_no": "客户退货单号",
    "return_qty": "退货数量",
    "return_reason": "退货原因",
    "sales_order_line_no": "销售订单行号",
    "sales_order_no": "销售订单号",
    "sales_owner_username": "销售负责人账号",
    "sample_loan_no": "借样单号",
    "settlement_method": "结算方式",
    "shipment_date": "出库日期",
    "shipment_no": "销售出库单号",
    "shipment_qty": "出库数量",
    "short_name": "简称",
    "source_unit": "源单位",
    "spec": "规格",
    "status": "状态",
    "suggested_supplier_no": "建议供应商编号",
    "supplier_name": "供应商名称",
    "supplier_no": "供应商编号",
    "supplier_return_no": "供应商退货单号",
    "supplier_type": "供应商类型",
    "target_unit": "目标单位",
    "unit_price": "单价",
}

CSV_IMPORT_HEADER_ALIASES = {
    label: field for field, label in CSV_IMPORT_COLUMN_LABELS.items()
}
CSV_IMPORT_HEADER_ALIASES.update(
    {
        "采购日期": "order_date",
        "收款日期": "receipt_date",
        "进货日期": "receipt_date",
        "入库日期": "receipt_date",
        "生产入库日期": "receipt_date",
    }
)


class CsvImportReadError(Exception):
    def __init__(self, message: str, error_code: str = "FILE_IMPORT_VALIDATION_FAILED"):
        super().__init__(message)
        self.error_code = error_code


def csv_upload_validation_error(upload) -> str:
    if not upload:
        return "请选择要导入的 CSV 文件"
    if not upload.name.lower().endswith(".csv"):
        return "只支持 CSV 文件"
    max_size = getattr(settings, "ERP_MAX_CSV_IMPORT_SIZE", MAX_CSV_IMPORT_SIZE)
    if upload.size > max_size:
        return f"CSV 文件大小超过 {_format_size_limit(max_size)} 限制"
    return ""


def uploaded_csv_text_file(upload):
    try:
        upload.file.seek(0)
    except (AttributeError, OSError):
        pass
    data = upload.file.read()
    for encoding in CSV_IMPORT_ENCODINGS:
        try:
            return StringIO(data.decode(encoding), newline="")
        except UnicodeDecodeError:
            continue
    return StringIO(data.decode("utf-8-sig", errors="replace"), newline="")


def read_csv_dict_rows(file_obj) -> list[dict[str, str]]:
    max_rows = int(getattr(settings, "ERP_MAX_CSV_IMPORT_ROWS", MAX_CSV_IMPORT_ROWS))
    rows = []
    try:
        reader = csv.DictReader(file_obj, strict=True)
        header_map = _csv_import_header_map(reader.fieldnames)
        for row_index, row in enumerate(reader, start=1):
            if row_index > max_rows:
                raise CsvImportReadError(f"CSV 数据行数超过 {max_rows} 行限制")
            if None in row:
                raise CsvImportReadError(f"第 {row_index + 1} 行列数超过表头，请检查逗号和引号")
            rows.append(_normalize_csv_row_headers(row, header_map))
    except CsvImportReadError:
        raise
    except csv.Error as exc:
        raise CsvImportReadError(f"CSV 格式错误：{exc}") from exc
    return rows


def csv_import_header_row(columns: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(CSV_IMPORT_COLUMN_LABELS.get(column, column) for column in columns)


def _csv_import_header_map(fieldnames: list[str] | None) -> dict[str, str]:
    if not fieldnames:
        return {}
    header_map = {}
    seen_labels = set()
    seen_fields = set()
    for raw_header in fieldnames:
        header = (raw_header or "").strip()
        if not header:
            raise CsvImportReadError("第 1 行存在空表头，请使用系统下载的中文导入模板")
        if header in seen_labels:
            raise CsvImportReadError(f"第 1 行存在重复表头：{header}")
        seen_labels.add(header)
        field_name = CSV_IMPORT_HEADER_ALIASES.get(header)
        if not field_name:
            raise CsvImportReadError(f"第 1 行存在不支持的表头：{header}，请使用系统下载的中文导入模板")
        if field_name in seen_fields:
            raise CsvImportReadError(f"第 1 行存在重复含义的表头：{header}")
        seen_fields.add(field_name)
        header_map[raw_header] = field_name
    return header_map


def _normalize_csv_row_headers(row: dict[str, str], header_map: dict[str, str]) -> dict[str, str]:
    normalized = {}
    for raw_header, value in row.items():
        normalized[header_map[raw_header]] = value
    return normalized


def _format_size_limit(size: int) -> str:
    if size >= 1024 * 1024 and size % (1024 * 1024) == 0:
        return f"{size // 1024 // 1024}MB"
    if size >= 1024 and size % 1024 == 0:
        return f"{size // 1024}KB"
    return f"{size}B"


def register_attachment(
    source_doc_type: str,
    source_doc_id: int,
    original_filename: str,
    stored_filename: str,
    file_path: str,
    file_size: int,
    mime_type: str,
    uploaded_by_id: int,
    source_doc_no: str = "",
    checksum_sha256: str = "",
    is_sensitive: bool = False,
) -> ServiceResult:
    original_filename = safe_attachment_filename(original_filename)
    stored_filename = safe_attachment_filename(stored_filename)
    suffix = Path(original_filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return ServiceResult(False, "FILE_TYPE_NOT_ALLOWED", "附件类型不允许上传")
    if file_size > MAX_ATTACHMENT_SIZE:
        return ServiceResult(False, "FILE_TYPE_NOT_ALLOWED", "附件大小超过限制")
    resolved_file_path = resolve_attachment_storage_path(file_path)
    if not resolved_file_path:
        return ServiceResult(False, "FILE_PATH_INVALID", "附件存储路径不合法")
    if PurePosixPath(resolved_file_path).name != stored_filename:
        return ServiceResult(False, "FILE_PATH_INVALID", "附件文件名和存储路径不一致")

    scan_result = scan_attachment_file(resolved_file_path)
    if not scan_result.success:
        return scan_result

    attachment = Attachment.objects.create(
        attachment_no=next_document_no("ATT"),
        source_doc_type=source_doc_type,
        source_doc_id=source_doc_id,
        source_doc_no=source_doc_no,
        original_filename=original_filename,
        stored_filename=stored_filename,
        file_path=resolved_file_path,
        file_size=file_size,
        mime_type=mime_type,
        checksum_sha256=checksum_sha256,
        is_sensitive=is_sensitive,
        scan_status=scan_result.data.get("scan_status", Attachment.ScanStatus.NOT_REQUIRED),
        uploaded_by_id=uploaded_by_id,
    )
    return ServiceResult(True, message="附件已登记", data={"attachment_id": attachment.id})


def scan_attachment_file(file_path: str) -> ServiceResult:
    resolved_file_path = resolve_attachment_storage_path(file_path)
    if not resolved_file_path:
        return ServiceResult(False, "FILE_PATH_INVALID", "附件存储路径不合法")

    scan_command = getattr(settings, "ERP_ATTACHMENT_SCAN_COMMAND", "").strip()
    if not scan_command:
        return ServiceResult(
            True,
            message="附件无需扫描",
            data={"scan_status": Attachment.ScanStatus.NOT_REQUIRED},
        )

    try:
        absolute_path = Path(default_storage.path(resolved_file_path)).resolve(strict=True)
    except Exception as exc:
        return ServiceResult(False, "FILE_PATH_INVALID", f"附件扫描前无法读取文件：{exc}")

    command = _attachment_scan_command(scan_command, absolute_path)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=int(getattr(settings, "ERP_ATTACHMENT_SCAN_TIMEOUT", 30)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ServiceResult(False, "FILE_SCAN_FAILED", "附件安全扫描超时")
    except OSError as exc:
        return ServiceResult(False, "FILE_SCAN_FAILED", f"附件安全扫描无法执行：{exc}")

    if completed.returncode != 0:
        message = (completed.stdout or completed.stderr or "附件安全扫描未通过").strip()
        return ServiceResult(
            False,
            "FILE_SCAN_FAILED",
            f"附件安全扫描未通过：{message[:200]}",
            data={"scan_status": Attachment.ScanStatus.FAILED, "returncode": completed.returncode},
        )

    return ServiceResult(
        True,
        message="附件安全扫描通过",
        data={"scan_status": Attachment.ScanStatus.PASSED, "returncode": completed.returncode},
    )


def _attachment_scan_command(scan_command: str, absolute_path: Path) -> list[str]:
    command = [_strip_wrapping_quotes(part) for part in shlex.split(scan_command, posix=False)]
    file_argument = str(absolute_path)
    if any("{file}" in arg for arg in command):
        return [arg.replace("{file}", file_argument) for arg in command]
    return [*command, file_argument]


def _strip_wrapping_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def safe_attachment_filename(filename: str) -> str:
    normalized = (filename or "").replace("\\", "/")
    return PurePosixPath(normalized).name[:255]


def resolve_attachment_storage_path(file_path: str) -> str | None:
    if not file_path or "\\" in file_path:
        return None
    path = PurePosixPath(file_path)
    if path.is_absolute():
        return None
    if any(part in {"", ".", ".."} for part in path.parts):
        return None
    if len(path.parts) != 2 or path.parts[0] != "attachments":
        return None
    if path.suffix.lower() not in ALLOWED_EXTENSIONS:
        return None
    return path.as_posix()


def delete_attachment(attachment_id: int, operator_id: int, reason: str) -> ServiceResult:
    if not reason:
        return ServiceResult(False, "STATE_INVALID_TRANSITION", "删除附件必须填写原因")
    try:
        with transaction.atomic():
            attachment = Attachment.objects.select_for_update().get(id=attachment_id)
            if attachment.status != Attachment.AttachmentStatus.ACTIVE:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "附件已删除，不能重复删除")
            attachment.status = Attachment.AttachmentStatus.DELETED
            attachment.deleted_by_id = operator_id
            attachment.deleted_at = timezone.now()
            attachment.delete_reason = reason
            attachment.save(update_fields=["status", "deleted_by", "deleted_at", "delete_reason"])
            AttachmentAccessLog.objects.create(attachment=attachment, operator_id=operator_id, action="delete")
    except Attachment.DoesNotExist:
        return ServiceResult(False, "DOC_NOT_FOUND", "附件不存在")
    return ServiceResult(True, message="附件已删除", data={"attachment_id": attachment.id})


def record_attachment_access(
    attachment_id: int,
    operator_id: int,
    action: str = "download",
    ip_address: str | None = None,
    user_agent: str = "",
) -> ServiceResult:
    try:
        attachment = Attachment.objects.get(id=attachment_id, status=Attachment.AttachmentStatus.ACTIVE)
    except Attachment.DoesNotExist:
        return ServiceResult(False, "DOC_NOT_FOUND", "附件不存在或已删除")
    log = AttachmentAccessLog.objects.create(
        attachment=attachment,
        operator_id=operator_id,
        action=action,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return ServiceResult(True, message="附件访问已记录", data={"access_log_id": log.id, "file_path": attachment.file_path})


def export_queryset_to_csv(
    module: str,
    queryset,
    columns: tuple[tuple[str, str], ...],
    exported_by_id: int | None,
    filter_json: dict | None = None,
    mask_fields: tuple[str, ...] = (),
) -> ServiceResult:
    export_no = next_document_no("EXP")
    export_dir = Path(settings.MEDIA_ROOT) / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    download_filename = f"{export_no}.csv"
    file_path = export_dir / f"{export_no}-{uuid4().hex[:8]}.csv"
    row_count = 0

    try:
        with file_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow([label for label, _field in columns])
            for row in queryset:
                writer.writerow(
                    ["******" if field in mask_fields else _csv_value(_row_value(row, field)) for _label, field in columns]
                )
                row_count += 1
        export_log = ExportLog.objects.create(
            export_no=export_no,
            module=module,
            filter_json=filter_json or {},
            file_path=str(file_path),
            row_count=row_count,
            exported_by_id=exported_by_id,
        )
    except Exception as exc:
        return ServiceResult(False, "FILE_EXPORT_FAILED", f"导出失败：{exc}")

    return ServiceResult(
        True,
        message="导出完成",
        data={
            "export_log_id": export_log.id,
            "export_no": export_no,
            "file_path": str(file_path),
            "row_count": row_count,
            "filename": download_filename,
        },
    )


def resolve_export_file_path(file_path: str, export_no: str = "") -> Path | None:
    if not file_path:
        return None

    export_dir = (Path(settings.MEDIA_ROOT) / "exports").resolve()
    try:
        candidate = Path(file_path)
        if not candidate.is_absolute():
            candidate = export_dir / candidate
        resolved_path = candidate.resolve(strict=False)
        resolved_path.relative_to(export_dir)
    except (OSError, ValueError):
        return None

    if export_no:
        expected_name = f"{export_no}.csv"
        expected_prefix = f"{export_no}-"
        if resolved_path.name != expected_name and not resolved_path.name.startswith(expected_prefix):
            return None
    if resolved_path.suffix.lower() != ".csv":
        return None
    if not resolved_path.is_file():
        return None
    return resolved_path


def record_print_log(
    template_type: str,
    source_doc_type: str,
    source_doc_id: int,
    source_doc_no: str,
    printed_by_id: int | None,
) -> ServiceResult:
    print_log = PrintLog.objects.create(
        print_no=next_document_no("PRT"),
        template_type=template_type,
        source_doc_type=source_doc_type,
        source_doc_id=source_doc_id,
        source_doc_no=source_doc_no,
        printed_by_id=printed_by_id,
    )
    return ServiceResult(True, message="打印记录已写入", data={"print_log_id": print_log.id, "print_no": print_log.print_no})


def _row_value(obj, attr_path: str):
    value = obj
    for attr in attr_path.split("."):
        value = getattr(value, attr, "")
        if value is None:
            return ""
        if callable(value):
            value = value()
    return value


def _csv_value(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value
