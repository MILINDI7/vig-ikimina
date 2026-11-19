import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import storages
from django.db import models
from django.utils import timezone

def proof_storage():
    return storages["proofs"]

def proof_path(instance, filename):
    return f"proofs/{uuid.uuid4().hex}.jpg"

class ContributionPeriod(models.Model):
    group = models.ForeignKey("groups.Group", on_delete=models.PROTECT, related_name="contribution_periods")
    month = models.DateField()
    due_date = models.DateField()
    amount = models.PositiveBigIntegerField()
    settings_version = models.ForeignKey("groups.GroupSettings", on_delete=models.PROTECT)
    rules_snapshot = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["-month"]
        constraints = [models.UniqueConstraint(fields=["group", "month"], name="unique_group_month")]
    def save(self, *args, **kwargs):
        if self.pk: raise ValidationError("Contribution periods are immutable.")
        return super().save(*args, **kwargs)

class Contribution(models.Model):
    class Status(models.TextChoices):
        NOT_PAID = "not_paid", "Not Paid"
        PENDING = "pending", "Pending Approval"
        APPROVED = "approved", "Approved"
        DECLINED = "declined", "Declined"
    period = models.ForeignKey(ContributionPeriod, on_delete=models.PROTECT, related_name="contributions")
    membership = models.ForeignKey("groups.Membership", on_delete=models.PROTECT, related_name="contributions")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.NOT_PAID)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="reviewed_contributions")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    decline_reason = models.TextField(blank=True)
    payment_late_days = models.PositiveIntegerField(default=0)
    last_calculated_at = models.DateTimeField(null=True, blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["period", "membership"], name="unique_member_period")]
    def days_late_on(self, day=None):
        if self.status not in (self.Status.NOT_PAID, self.Status.DECLINED): return 0
        return max(0, ((day or timezone.localdate()) - self.period.due_date).days)
    @property
    def days_late(self): return self.days_late_on()
    @property
    def is_late(self): return self.days_late > 0
    @property
    def penalty_total(self):
        return sum(p.amount for p in self.penalties.all())
    @property
    def amount_due(self):
        return (0 if self.status == self.Status.APPROVED else self.period.amount) + self.penalty_total

class ProofSubmission(models.Model):
    contribution = models.ForeignKey(Contribution, on_delete=models.PROTECT, related_name="submissions")
    image = models.FileField(storage=proof_storage, upload_to=proof_path)
    submitted_at = models.DateTimeField(auto_now_add=True)
    submitted_late_days = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=16, choices=Contribution.Status.choices, default=Contribution.Status.PENDING)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    decline_reason = models.TextField(blank=True)
    class Meta:
        ordering = ["-pk"]
        constraints = [models.UniqueConstraint(fields=["contribution"], condition=models.Q(status="pending"), name="one_pending_proof")]

class Penalty(models.Model):
    contribution = models.ForeignKey(Contribution, on_delete=models.PROTECT, related_name="penalties")
    threshold_days = models.PositiveIntegerField()
    amount = models.PositiveBigIntegerField()
    effective_date = models.DateField()
    assessed_at = models.DateTimeField(auto_now_add=True)
    calculation = models.JSONField()
    class Meta:
        ordering = ["threshold_days"]
        constraints = [models.UniqueConstraint(fields=["contribution", "threshold_days"], name="unique_penalty_threshold")]

class Notification(models.Model):
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="notifications")
    contribution = models.ForeignKey(Contribution, on_delete=models.PROTECT, null=True, blank=True)
    loan_request = models.ForeignKey("loans.LoanRequest", on_delete=models.PROTECT, null=True, blank=True)
    event_key = models.CharField(max_length=120, unique=True)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)
    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [models.CheckConstraint(condition=(models.Q(contribution__isnull=False, loan_request__isnull=True) | models.Q(contribution__isnull=True, loan_request__isnull=False)), name="notification_exactly_one_target")]
