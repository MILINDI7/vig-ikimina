from copy import deepcopy
from datetime import date, datetime, time, timedelta
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from accounts.models import AuditLog
from groups.models import Group, Membership
from groups.permissions import can_review_group
from .models import ContributionPeriod, Contribution, ProofSubmission, Penalty, Notification

def next_month(month):
    return date(month.year + (month.month == 12), 1 if month.month == 12 else month.month + 1, 1)

def audit(actor, contribution, action, **details):
    AuditLog.objects.create(actor=actor, target=contribution.membership.user, action=action, details={"group_id": contribution.period.group_id, "contribution_id": contribution.pk, **details})

def notify(contribution, key, message):
    return Notification.objects.get_or_create(event_key=key, defaults={"recipient_id": contribution.membership.user_id, "contribution": contribution, "message": message})

@transaction.atomic
def ensure_period(group_id, month=None, actor=None):
    """Materialize one month under a group lock; repeated calls add no duplicates."""
    group = Group.objects.select_for_update().get(pk=group_id)
    if group.is_archived: raise ValidationError("Archived groups cannot open periods.")
    month = (month or timezone.localdate()).replace(day=1)
    if month > timezone.localdate().replace(day=1): raise ValidationError("Future months cannot be opened.")
    first_version = group.settings_versions.order_by("effective_from", "pk").first()
    if not first_version or month < first_version.effective_from.replace(day=1):
        raise ValidationError("This month predates the group's initial settings.")
    version = group.settings_on(month)
    if version is None:
        # A newly created group can start in its first, partial month.
        version = first_version
    following = next_month(month)
    period, created = ContributionPeriod.objects.get_or_create(group=group, month=month, defaults={"due_date": following.replace(day=version.due_day), "amount": version.contribution_amount, "settings_version": version, "rules_snapshot": {"version_id": version.pk, "contribution_amount": version.contribution_amount, "due_day": version.due_day, "lateness_tiers": deepcopy(version.lateness_tiers), "rating_classes": deepcopy(version.rating_classes)}})
    added = 0
    for membership in group.memberships.filter(is_active=True, user__is_active=True, joined_at__lt=timezone.make_aware(datetime.combine(following, time.min))):
        _, new = Contribution.objects.get_or_create(period=period, membership=membership)
        added += new
    if created or added:
        AuditLog.objects.create(actor=actor, action="contribution_period_opened", details={"group_id": group.pk, "period_id": period.pk, "month": month.isoformat(), "settings_version_id": period.settings_version_id, "members_added": added})
    return period

def _calculate(contribution, day, actor=None, rating_trigger="contribution_recalculated"):
    """Caller holds the group and contribution locks. Never assess pending/approved."""
    days = contribution.days_late_on(day)
    if days:
        for tier in contribution.period.rules_snapshot["lateness_tiers"]:
            if days < tier["days_late"]: continue
            percentage_fee = (contribution.period.amount * tier["percentage_bps"] + 9999) // 10000
            amount = tier["flat_fee"] + percentage_fee
            penalty, created = Penalty.objects.get_or_create(contribution=contribution, threshold_days=tier["days_late"], defaults={"amount": amount, "effective_date": contribution.period.due_date + timedelta(days=tier["days_late"]), "calculation": {"settings_version_id": contribution.period.settings_version_id, "tier": deepcopy(tier), "principal_rwf": contribution.period.amount, "percentage_fee_rwf": percentage_fee, "amount_rwf": amount, "rounding": "ceiling_to_integer_rwf", "days_late_when_assessed": days, "as_of": day.isoformat()}})
            if created: audit(actor, contribution, "penalty_assessed", penalty_id=penalty.pk, amount_rwf=amount, calculation=penalty.calculation)
        _, created = notify(contribution, f"late:{contribution.pk}", f"{contribution.period.group.name}: your {contribution.period.month:%B %Y} contribution is late. Due date: {contribution.period.due_date}. Penalties follow the saved period rules.")
        if created: audit(actor, contribution, "contribution_became_late", days_late=days)
    elif contribution.status in (Contribution.Status.NOT_PAID, Contribution.Status.DECLINED) and 0 <= (contribution.period.due_date-day).days <= 3:
        notify(contribution, f"due:{contribution.pk}", f"{contribution.period.group.name}: {contribution.period.amount} RWF for {contribution.period.month:%B %Y} is due by {contribution.period.due_date}.")
    contribution.last_calculated_at = timezone.now()
    contribution.save(update_fields=["last_calculated_at"])
    from ratings.services import evaluate_membership
    evaluate_membership(contribution.membership_id, actor=actor, trigger=rating_trigger)
    return contribution

def _locked(contribution_id):
    group_id = Contribution.objects.values_list("period__group_id", flat=True).get(pk=contribution_id)
    group = Group.objects.select_for_update().get(pk=group_id)
    # Keep nullable review relations out of SELECT FOR UPDATE (PostgreSQL).
    contribution = Contribution.objects.select_for_update().select_related("period", "membership__user", "period__group").get(pk=contribution_id)
    return group, contribution

@transaction.atomic
def recalculate(contribution_id, day=None, actor=None):
    group, contribution = _locked(contribution_id)
    if not group.is_archived:
        _calculate(contribution, day or timezone.localdate(), actor)
    return contribution

@transaction.atomic
def submit_proof(contribution_id, owner, image):
    group, contribution = _locked(contribution_id)
    if contribution.membership.user_id != owner.pk or not owner.is_active or group.is_archived:
        raise PermissionDenied
    if contribution.status not in (Contribution.Status.NOT_PAID, Contribution.Status.DECLINED):
        raise ValidationError("This contribution already has pending or approved proof.")
    day = timezone.localdate()
    _calculate(contribution, day, owner)
    submission = ProofSubmission(contribution=contribution, submitted_late_days=max(0, (day-contribution.period.due_date).days))
    # Storage is not transactional; remove the newly written blob if this operation fails.
    try:
        submission.image.save("proof.jpg", image, save=False)
        submission.save()
        contribution.status = Contribution.Status.PENDING
        contribution.reviewed_by = None
        contribution.reviewed_at = None
        contribution.decline_reason = ""
        contribution.save(update_fields=["status", "reviewed_by", "reviewed_at", "decline_reason"])
        audit(owner, contribution, "proof_uploaded", submission_id=submission.pk)
        from ratings.services import evaluate_membership
        evaluate_membership(contribution.membership_id, actor=owner, trigger="proof_uploaded")
    except Exception:
        if submission.image.name:
            submission.image.storage.delete(submission.image.name)
        raise
    return submission

@transaction.atomic
def review_proof(contribution_id, reviewer, decision, reason, submission_id):
    group, contribution = _locked(contribution_id)
    if not can_review_group(reviewer, group) or contribution.membership.user_id == reviewer.pk:
        raise PermissionDenied
    if decision not in (Contribution.Status.APPROVED, Contribution.Status.DECLINED):
        raise ValidationError("Choose approve or decline.")
    reason = reason.strip()
    if decision == Contribution.Status.DECLINED and not reason:
        raise ValidationError("A decline reason is required.")
    submission = ProofSubmission.objects.filter(pk=submission_id, contribution=contribution, status=Contribution.Status.PENDING).first()
    if contribution.status != Contribution.Status.PENDING or submission is None:
        raise ValidationError("This proof has already been reviewed or replaced. Reload the queue.")
    now = timezone.now()
    submission.status, submission.reviewed_by, submission.reviewed_at = decision, reviewer, now
    submission.decline_reason = reason if decision == Contribution.Status.DECLINED else ""
    submission.save(update_fields=["status", "reviewed_by", "reviewed_at", "decline_reason"])
    contribution.status, contribution.reviewed_by, contribution.reviewed_at = decision, reviewer, now
    contribution.decline_reason = submission.decline_reason
    if decision == Contribution.Status.APPROVED:
        contribution.approved_at = now
        contribution.payment_late_days = submission.submitted_late_days
    contribution.save(update_fields=["status", "reviewed_by", "reviewed_at", "decline_reason", "approved_at", "payment_late_days"])
    # Decline reactivates the ORIGINAL due date; approval never backfills the pending interval.
    _calculate(contribution, timezone.localdate(), reviewer, rating_trigger="proof_"+decision)
    audit(reviewer, contribution, "proof_"+decision, submission_id=submission.pk, reason=submission.decline_reason)
    message = f"{group.name}: your {contribution.period.month:%B %Y} contribution was {decision}."
    if decision == Contribution.Status.DECLINED: message += f" Reason: {reason}. Lateness is counted from the original due date {contribution.period.due_date}."
    notify(contribution, f"review:{submission.pk}", message)
    return contribution
