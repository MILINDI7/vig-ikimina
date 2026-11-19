from datetime import datetime
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from groups.models import Group
from contributions.models import Contribution
from contributions.services import ensure_period, recalculate

class Command(BaseCommand):
    help = "Open the current month and assess due/late notices and penalties. Safe to repeat."
    def add_arguments(self, parser):
        parser.add_argument("--month", help="Explicitly backfill one month, YYYY-MM; uses currently active memberships eligible for that month.")
    def handle(self, *args, **options):
        month = timezone.localdate().replace(day=1)
        if options["month"]:
            try: month = datetime.strptime(options["month"], "%Y-%m").date()
            except ValueError: raise CommandError("Use YYYY-MM.")
            if month > timezone.localdate().replace(day=1): raise CommandError("Cannot open future months.")
        opened = 0
        for group_id in Group.objects.filter(is_archived=False).values_list("pk", flat=True).iterator():
            try:
                ensure_period(group_id, month)
                opened += 1
            except ValidationError as error:
                # Groups not yet founded in a backfilled month have no obligation.
                self.stdout.write(f"Group {group_id}: {'; '.join(error.messages)}")
        count = 0
        for pk in Contribution.objects.filter(period__group__is_archived=False).exclude(status=Contribution.Status.APPROVED).values_list("pk", flat=True).iterator():
            recalculate(pk)
            count += 1
        self.stdout.write(self.style.SUCCESS(f"Opened/checked {opened} group periods; recalculated {count} contributions."))
