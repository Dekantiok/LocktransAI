from django.core.management.base import BaseCommand

from escrow.services import auto_release_expired_inspections


class Command(BaseCommand):
    help = (
        "Release escrows whose inspection window has elapsed without buyer "
        "action. Designed to run on a schedule (cron/beat), NOT as a "
        "persistent worker: each invocation does one sweep and exits."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help="List the escrows that would be released without paying anyone.",
        )

    def handle(self, *args, **options):
        if options['dry_run']:
            pending = auto_release_expired_inspections(dry_run=True)
            for contract in pending:
                self.stdout.write(f"Would release {contract.code} ({contract.title})")
            self.stdout.write(self.style.WARNING(f'{len(pending)} escrow(s) eligible.'))
            return

        released, skipped = auto_release_expired_inspections()
        self.stdout.write(
            self.style.SUCCESS(f'Released {released} expired escrow(s), skipped {skipped}.')
        )
