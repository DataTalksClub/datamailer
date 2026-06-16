import signal

from django.core.management.base import BaseCommand

from mailing.sqs_worker import WORKER_NAMES, SqsWorker, get_worker_config


class Command(BaseCommand):
    help = "Poll a Datamailer SQS queue and process messages with the matching worker handler."

    def add_arguments(self, parser):
        parser.add_argument("worker", choices=WORKER_NAMES)
        parser.add_argument("--once", action="store_true", help="Process at most one SQS batch and exit.")
        parser.add_argument("--batch-size", type=int, default=10, help="SQS receive batch size, from 1 to 10.")
        parser.add_argument("--wait-time", type=int, default=20, help="SQS long-poll wait time in seconds, from 0 to 20.")
        parser.add_argument("--visibility-timeout", type=int, default=None, help="Optional SQS visibility timeout.")
        parser.add_argument("--idle-sleep", type=float, default=0, help="Seconds to sleep after an empty poll.")

    def handle(self, *args, **options):
        stop_requested = False

        def request_stop(signum, frame):
            nonlocal stop_requested
            stop_requested = True

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)

        worker_name = options["worker"]
        worker = SqsWorker(
            get_worker_config(worker_name),
            batch_size=options["batch_size"],
            wait_time=options["wait_time"],
            visibility_timeout=options["visibility_timeout"],
        )

        self.stdout.write(f"Starting {worker_name} SQS worker")
        if options["once"]:
            self._write_result(worker.run_once())
            return

        for result in worker.run_forever(
            should_stop=lambda: stop_requested,
            idle_sleep=options["idle_sleep"],
        ):
            if result.received:
                self._write_result(result)

        self.stdout.write(f"Stopped {worker_name} SQS worker")

    def _write_result(self, result):
        self.stdout.write(
            f"received={result.received} deleted={result.deleted} failed={result.failed}"
        )
