from django.core.management.base import BaseCommand, CommandError

from system.backup_services import restore_drill
from system.services import fail_background_job, finish_background_job, start_background_job


class Command(BaseCommand):
    help = "执行备份恢复演练校验，不覆盖当前数据库"

    def add_arguments(self, parser):
        parser.add_argument("--backup-id", type=int, default=None, help="指定备份记录 ID，默认使用最近一次成功备份")
        parser.add_argument("--backup-no", default="", help="指定备份单号")
        parser.add_argument("--extract-dir", default="", help="演练解压目录，默认使用临时目录并在完成后清理")
        parser.add_argument("--trigger", default="manual", help="触发来源，例如 manual/schedule")

    def handle(self, *args, **options):
        input_params = {
            "backup_id": options["backup_id"],
            "backup_no": options["backup_no"],
            "extract_dir": options["extract_dir"],
        }
        start_result = start_background_job("restore_drill", trigger_type=options["trigger"], input_params=input_params)
        if not start_result.success:
            raise CommandError(start_result.message)

        job_id = start_result.data["job_id"]
        result = restore_drill(
            backup_id=options["backup_id"],
            backup_no=options["backup_no"],
            extract_dir=options["extract_dir"] or None,
        )
        if result.success:
            finish_background_job(job_id, result.data)
            self.stdout.write(self.style.SUCCESS(result.message))
            return

        fail_background_job(job_id, result.message, result.data)
        raise CommandError(result.message or result.error_code)
