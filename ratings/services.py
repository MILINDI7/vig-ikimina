from calendar import monthrange
from copy import deepcopy
from datetime import timedelta
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from accounts.models import AuditLog
from contributions.models import Contribution
from groups.models import Group, Membership
from .models import MemberRating, RatingHistory

def window_start(day):
    """Twelve calendar months, excluding the anniversary and including today."""
    anniversary = day.replace(year=day.year-1, day=min(day.day, monthrange(day.year-1,day.month)[1]))
    return anniversary + timedelta(days=1)

def loan_metrics(membership, day):
    """Phase 5 integration seam; no loan records exist in the current phase."""
    return {"outstanding_loans": 0, "defaults": 0, "loan_data_available": False}

def payment_metrics(membership, day):
    start = window_start(day)
    late = []
    pending = []
    for contribution in Contribution.objects.filter(membership=membership).select_related("period").order_by("pk"):
        due = contribution.period.due_date
        if due > day: continue
        if contribution.status == Contribution.Status.PENDING:
            pending.append(contribution.pk)
            continue
        if contribution.status == Contribution.Status.APPROVED:
            if due < start: continue
            days = contribution.payment_late_days
        else:
            # An unresolved debt cannot age out of the rating while still unpaid.
            days = contribution.days_late_on(day)
        if days:
            late.append({"contribution_id": contribution.pk, "due_date": due.isoformat(), "status": contribution.status, "days_late": days, "unresolved_outside_window": due < start})
    return {"window_start": start.isoformat(), "window_end": day.isoformat(), "max_days_late": max((item["days_late"] for item in late), default=0), "late_payments": len(late), "late_contributions": late, "pending_excluded": pending, **loan_metrics(membership,day)}

def choose_class(classes, metrics):
    thresholds = {"max_days_late": "max_days_late", "max_late_payments": "late_payments", "max_defaults": "defaults", "max_outstanding_loans": "outstanding_loans"}
    for rating in classes:
        if all(rating.get(rule) is None or metrics[metric] <= rating[rule] for rule,metric in thresholds.items()):
            return deepcopy(rating)
    raise ValidationError("Rating rules require an unrestricted fallback class.")

@transaction.atomic
def evaluate_membership(membership_id, *, actor=None, trigger="on_demand"):
    """Current-date evaluation only, serialized with all group financial changes."""
    group_id = Membership.objects.values_list("group_id",flat=True).get(pk=membership_id)
    group = Group.objects.select_for_update().get(pk=group_id)
    membership = Membership.objects.select_for_update().get(pk=membership_id)
    current = MemberRating.objects.filter(membership=membership).first()
    if group.is_archived: return current
    day = timezone.localdate()
    version = group.settings_on(day)
    if version is None: return current
    metrics = payment_metrics(membership,day)
    chosen = choose_class(version.rating_classes,metrics)
    changed = current is None or current.code != chosen["code"] or current.class_snapshot != chosen or current.settings_version_id != version.pk
    previous = current.code if current else ""
    current,_ = MemberRating.objects.update_or_create(membership=membership,defaults={"settings_version": version, "code": chosen["code"], "class_snapshot": chosen, "metrics": metrics, "evaluated_on": day})
    if changed:
        history = RatingHistory.objects.create(membership=membership,settings_version=version,previous_code=previous,code=chosen["code"],class_snapshot=deepcopy(chosen),rules_snapshot={"version_id": version.pk,"rating_classes": deepcopy(version.rating_classes)},metrics=deepcopy(metrics),evaluated_on=day,trigger=trigger,actor=actor)
        AuditLog.objects.create(actor=actor,target_id=membership.user_id,action="rating_changed",details={"group_id": group.pk,"membership_id": membership.pk,"history_id": history.pk,"from": previous,"to": chosen["code"],"settings_version_id": version.pk,"trigger": trigger})
    return current

@transaction.atomic
def evaluate_group(group_id, *, actor=None, trigger="daily"):
    group = Group.objects.select_for_update().get(pk=group_id)
    if group.is_archived: return 0
    count=0
    for pk in group.memberships.values_list("pk",flat=True):
        evaluate_membership(pk,actor=actor,trigger=trigger)
        count += 1
    return count

@transaction.atomic
def loan_terms(membership_id):
    """Preview configured terms; Phase 5 must recheck inside its approval transaction."""
    group_id = Membership.objects.values_list("group_id",flat=True).get(pk=membership_id)
    group = Group.objects.select_for_update().get(pk=group_id)
    membership = Membership.objects.select_for_update().select_related("user").get(pk=membership_id)
    rating = evaluate_membership(membership_id)
    savings = Contribution.objects.filter(membership=membership,status=Contribution.Status.APPROVED).aggregate(total=Sum("period__amount"))["total"] or 0
    if rating is None:
        return {"eligible": False,"max_amount": 0,"interest_bps": 0,"approved_savings": savings,"reason": "No effective rating rules.","loan_data_available": False}
    configured_limit = savings * rating.class_snapshot["max_loan_multiple"]
    allowed = membership.is_active and membership.user.is_active and not group.is_archived and rating.class_snapshot["loan_eligible"] and configured_limit > 0
    return {"eligible": bool(allowed),"max_amount": configured_limit if allowed else 0,"interest_bps": rating.class_snapshot["interest_bps"],"approved_savings": savings,"reason": "Configured terms only; loan requests are added in Phase 5.","loan_data_available": rating.metrics["loan_data_available"]}
