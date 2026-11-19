from django.core.management.base import BaseCommand
from groups.models import Group
from ratings.services import evaluate_group

class Command(BaseCommand):
    help = "Refresh current ratings and initialize missing ratings without rewriting history."
    def handle(self,*args,**options):
        count=0
        for group_id in Group.objects.filter(is_archived=False).values_list("pk",flat=True).iterator():
            count += evaluate_group(group_id)
        self.stdout.write(self.style.SUCCESS(f"Recalculated {count} membership ratings."))
