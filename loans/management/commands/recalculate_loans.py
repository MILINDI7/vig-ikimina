from django.core.management.base import BaseCommand
from groups.models import Group
from ratings.services import evaluate_group

class Command(BaseCommand):
    help="Refresh loan defaults and affected ratings. Also included in the daily contribution task."
    def handle(self,*args,**options):
        count=0
        for pk in Group.objects.filter(is_archived=False).values_list("pk",flat=True).iterator():
            count+=evaluate_group(pk,trigger="loan_daily")
        self.stdout.write(self.style.SUCCESS(f"Refreshed loans and ratings for {count} memberships."))
