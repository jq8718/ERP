from django import template

from accounts.permissions import user_has_any_permission, user_has_permission
from system.display import code_label, field_label
from system.view_helpers import row_value

register = template.Library()


@register.filter
def attr(obj, attr_path):
    return row_value(obj, attr_path)


@register.simple_tag
def has_erp_perm(user, permission_code):
    return user_has_permission(user, permission_code)


@register.simple_tag
def has_any_erp_perm(user, *permission_codes):
    return user_has_any_permission(user, permission_codes)


@register.filter
def contains(value, item):
    return item in value


@register.filter
def get_item(value, key):
    if isinstance(value, dict):
        return value.get(key)
    return None


@register.filter
def code_name(value):
    return code_label(value)


@register.filter
def field_name(value):
    return field_label(value)


@register.simple_tag
def source_doc_url(user, source_doc_type, source_doc_id):
    from files.permissions import can_access_source_doc, resolve_source_doc_url

    if not source_doc_type or not source_doc_id:
        return ""
    if not can_access_source_doc(user, source_doc_type, source_doc_id):
        return ""
    return resolve_source_doc_url(source_doc_type, source_doc_id)
