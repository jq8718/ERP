from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from system.models import ReleaseRecord


class Command(BaseCommand):
    help = "登记一次应用发布记录，用于上线后追踪版本、执行人和发布摘要"

    def add_arguments(self, parser):
        parser.add_argument("version", nargs="?", help="发布版本号，例如 2026.06.11.1")
        parser.add_argument("--release-version", default="", help="发布版本号；用于部署脚本显式传参")
        parser.add_argument("--summary", default="", help="发布摘要")
        parser.add_argument("--released-by", default="", help="发布人用户名；为空则不关联用户")
        parser.add_argument("--noinput", action="store_true", help="兼容部署脚本；本命令始终非交互执行")

    @transaction.atomic
    def handle(self, *args, **options):
        version_no = (options["release_version"] or options["version"] or "").strip()
        if not version_no:
            raise CommandError("发布版本号不能为空")

        released_by = self._resolve_user(options["released_by"].strip())
        record, created = ReleaseRecord.objects.select_for_update().get_or_create(
            version_no=version_no,
            defaults={
                "released_at": timezone.now(),
                "released_by": released_by,
                "summary": options["summary"].strip(),
            },
        )
        if not created:
            raise CommandError(f"发布版本已存在：{version_no}")

        self.stdout.write(
            self.style.SUCCESS(
                f"Release recorded: version={record.version_no}, released_by={released_by.username if released_by else '-'}"
            )
        )

    def _resolve_user(self, username: str):
        if not username:
            return None
        User = get_user_model()
        user = User.objects.filter(username=username, is_deleted=False).first()
        if user is None:
            raise CommandError(f"发布人不存在：{username}")
        return user
