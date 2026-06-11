from django.core.management.base import BaseCommand, CommandError

from system.backup_services import backup_daily
from system.services import fail_background_job, finish_background_job, start_background_job


class Command(BaseCommand):
    help = "执行每日数据库和附件备份"

    def add_arguments(self, parser):
        parser.add_argument("--backup-dir", default="", help="备份输出目录，默认使用 ERP_BACKUP_DIR 或项目 backups 目录")
        parser.add_argument("--no-media", action="store_true", help="只备份数据库，不包含附件目录")
        parser.add_argument("--trigger", default="manual", help="触发来源，例如 manual/schedule")

    def handle(self, *args, **options):
        start_result = start_background_job(
            "backup",
            trigger_type=options["trigger"],
            input_params={"backup_dir": options["backup_dir"], "include_media": not options["no_media"]},
        )
        if not start_result.success:
            raise CommandError(start_result.message)

        job_id = start_result.data["job_id"]
        result = backup_daily(
            backup_dir=options["backup_dir"] or None,
            include_media=not options["no_media"],
        )
        if result.success:
            finish_background_job(job_id, result.data)
            self.stdout.write(self.style.SUCCESS(result.message))
            self.stdout.write(result.data.get("file_path", ""))
            return

        fail_background_job(job_id, result.message, result.data)
        raise CommandError(result.message or result.error_code)
