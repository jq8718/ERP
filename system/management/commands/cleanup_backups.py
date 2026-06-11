from django.core.management.base import BaseCommand, CommandError

from system.backup_services import cleanup_backups
from system.services import fail_background_job, finish_background_job, start_background_job


class Command(BaseCommand):
    help = "按保留策略清理旧备份文件和记录"

    def add_arguments(self, parser):
        parser.add_argument("--keep-daily-days", type=int, default=30, help="最近多少天的成功备份全部保留")
        parser.add_argument("--keep-weekly", type=int, default=12, help="更早成功备份中保留多少个周备份")
        parser.add_argument("--keep-monthly", type=int, default=12, help="更早成功备份中保留多少个月备份")
        parser.add_argument("--keep-failed-days", type=int, default=30, help="失败备份记录保留天数")
        parser.add_argument("--trigger", default="manual", help="触发来源，例如 manual/schedule")

    def handle(self, *args, **options):
        input_params = {
            "keep_daily_days": options["keep_daily_days"],
            "keep_weekly": options["keep_weekly"],
            "keep_monthly": options["keep_monthly"],
            "keep_failed_days": options["keep_failed_days"],
        }
        start_result = start_background_job("backup_cleanup", trigger_type=options["trigger"], input_params=input_params)
        if not start_result.success:
            raise CommandError(start_result.message)

        job_id = start_result.data["job_id"]
        result = cleanup_backups(**input_params)
        if result.success:
            finish_background_job(job_id, result.data)
            self.stdout.write(self.style.SUCCESS(result.message))
            return

        fail_background_job(job_id, result.message, result.data)
        raise CommandError(result.message or result.error_code)
