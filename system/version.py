from pathlib import Path

from django.conf import settings


DEFAULT_APP_VERSION = "开发版"


def get_app_version() -> str:
    configured = getattr(settings, "ERP_APP_VERSION", "").strip()
    if configured:
        return configured

    version_file = Path(getattr(settings, "ERP_VERSION_FILE", settings.BASE_DIR / "VERSION"))
    try:
        version = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return DEFAULT_APP_VERSION
    return version or DEFAULT_APP_VERSION
