from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "用生产环境变量模拟导入 settings 并执行 check --deploy，不连接生产数据库"

    def add_arguments(self, parser):
        parser.add_argument("--host", default="erp.example.com", help="模拟生产域名")
        parser.add_argument("--secret-key", default="", help="模拟生产密钥；为空时使用内置长随机样例")
        parser.add_argument("--timeout-seconds", type=int, default=120, help="子进程超时时间")

    def handle(self, *args, **options):
        result = run_simulated_production_check(
            host=options["host"],
            secret_key=options["secret_key"],
            timeout_seconds=options["timeout_seconds"],
        )
        if not result["ok"]:
            raise CommandError(f"生产配置模拟检查未通过：{result['message']}")

        self.stdout.write(self.style.SUCCESS("生产配置模拟检查通过"))
        self.stdout.write(result["message"])


def run_simulated_production_check(
    host: str = "erp.example.com",
    secret_key: str = "",
    timeout_seconds: int = 120,
) -> dict:
    env = _production_env(host, secret_key)
    command = [sys.executable, str(Path(settings.BASE_DIR) / "manage.py"), "check", "--deploy", "--fail-level", "WARNING"]
    try:
        result = subprocess.run(
            command,
            cwd=Path(settings.BASE_DIR),
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": f"执行超过 {timeout_seconds} 秒"}

    if result.returncode != 0:
        summary = _last_output_line(result.stderr) or _last_output_line(result.stdout) or f"退出码 {result.returncode}"
        return {"ok": False, "message": summary}
    return {
        "ok": True,
        "message": _last_output_line(result.stdout) or "System check identified no issues",
    }


def _production_env(host: str, secret_key: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "DJANGO_ENV": "production",
            "DJANGO_DEBUG": "false",
            "DJANGO_SECRET_KEY": secret_key or "production-secret-key-with-more-than-50-characters-1234567890",
            "DJANGO_ALLOWED_HOSTS": host,
            "DB_ENGINE": "postgres",
            "POSTGRES_PASSWORD": env.get("POSTGRES_PASSWORD") or "dummy-password-for-settings-check-only",
            "DJANGO_CSRF_TRUSTED_ORIGINS": f"https://{host}",
            "DJANGO_SECURE_SSL_REDIRECT": "true",
            "DJANGO_SESSION_COOKIE_SECURE": "true",
            "DJANGO_CSRF_COOKIE_SECURE": "true",
            "DJANGO_SECURE_HSTS_SECONDS": "31536000",
            "DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS": "true",
            "DJANGO_SECURE_HSTS_PRELOAD": "true",
        }
    )
    return env


def _last_output_line(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1] if lines else ""
