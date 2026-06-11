from django.core.management.base import BaseCommand, CommandError

from system.services import fail_background_job, finish_background_job, process_pending_events, start_background_job


class Command(BaseCommand):
    help = "处理事务提交后的待处理业务事件"

    def add_arguments(self, parser):
        parser.add_argument("--event-type", default="", help="只处理指定事件类型")
        parser.add_argument("--limit", type=int, default=100, help="单次最多处理事件数量")
        parser.add_argument("--trigger", default="manual", help="触发来源，例如 manual/schedule")

    def handle(self, *args, **options):
        start_result = start_background_job(
            "process_pending_events",
            trigger_type=options["trigger"],
            input_params={"event_type": options["event_type"], "limit": options["limit"]},
        )
        if not start_result.success:
            raise CommandError(start_result.message)

        job_id = start_result.data["job_id"]
        result = process_pending_events(
            event_type=options["event_type"] or None,
            limit=options["limit"],
        )
        if result.success:
            finish_background_job(job_id, result.data)
            self.stdout.write(self.style.SUCCESS(result.message))
            return

        fail_background_job(job_id, result.message, result.data)
        raise CommandError(result.message or result.error_code)
