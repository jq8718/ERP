from django.core.management.base import BaseCommand, CommandError

from system.backup_services import verify_backups
from system.services import fail_background_job, finish_background_job, start_background_job


class Command(BaseCommand):
    help = "校验备份文件可读性和 SHA-256 校验值"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=30, help="校验最近多少条成功备份记录")
        parser.add_argument("--trigger", default="manual", help="触发来源，例如 manual/schedule")

    def handle(self, *args, **options):
        start_result = start_background_job(
            "backup_verify",
            trigger_type=options["trigger"],
            input_params={"limit": options["limit"]},
        )
        if not start_result.success:
            raise CommandError(start_result.message)

        job_id = start_result.data["job_id"]
        result = verify_backups(limit=options["limit"])
        if result.success:
            finish_background_job(job_id, result.data)
            self.stdout.write(self.style.SUCCESS(result.message))
            return

        fail_background_job(job_id, result.message, result.data)
        raise CommandError(result.message or result.error_code)
