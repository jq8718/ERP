from datetime import date, datetime

from django import forms


ERP_DATE_INPUT_FORMATS = ["%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日"]
ERP_DATE_INPUT_TITLE = "请选择日期，或输入 2026-07-04、2026/07/04、2026.07.04、2026年07月04日"


def erp_date_input(attrs: dict | None = None) -> forms.DateInput:
    merged_attrs = {
        "type": "date",
        "data-erp-date": "1",
        "placeholder": "YYYY-MM-DD",
        "title": ERP_DATE_INPUT_TITLE,
    }
    merged_attrs.update(attrs or {})
    merged_attrs["type"] = "date"
    return forms.DateInput(attrs=merged_attrs, format="%Y-%m-%d")


def apply_erp_date_inputs(form, field_names: tuple[str, ...] | list[str] | None = None) -> None:
    target_names = set(field_names) if field_names else None
    for field_name, field in form.fields.items():
        if target_names is not None and field_name not in target_names:
            continue
        if not isinstance(field, forms.DateField) or isinstance(field, forms.DateTimeField):
            continue
        field.widget = erp_date_input(dict(getattr(field.widget, "attrs", {}) or {}))
        field.input_formats = _dedupe(ERP_DATE_INPUT_FORMATS + list(field.input_formats or []))


def parse_user_date(value, default=None):
    if value in [None, ""]:
        return default
    text = str(value).strip()
    if not text:
        return default

    normalized = (
        text.replace("／", "/")
        .replace("．", ".")
        .replace("。", ".")
        .replace("－", "-")
        .replace("—", "-")
        .replace("–", "-")
    )
    normalized = "".join(normalized.split())

    for date_format in ERP_DATE_INPUT_FORMATS:
        try:
            return datetime.strptime(normalized, date_format).date()
        except ValueError:
            continue

    try:
        return date.fromisoformat(normalized)
    except ValueError:
        return default


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
