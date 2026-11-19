import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.db.models import Sum
from django.utils import timezone

MAX_LOAN_AMOUNT = 10**12

class LoanRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
    membership = models.ForeignKey("groups.Membership", on_delete=models.PROTECT, related_name="loan_requests")
    amount = models.PositiveBigIntegerField(validators=[MinValueValidator(1), MaxValueValidator(MAX_LOAN_AMOUNT)])
    purpose = models.TextField(max_length=2000)
    repayment_months = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(60)])
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    submission_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    terms_snapshot = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.TextField(blank=True)
    class Meta:
        ordering = ["-pk"]
        constraints = [models.UniqueConstraint(fields=["membership"], condition=models.Q(status="pending"), name="one_pending_loan_request")]

class Loan(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        DEFAULTED = "defaulted", "Defaulted"
        REPAID = "repaid", "Repaid"
    request = models.OneToOneField(LoanRequest, on_delete=models.PROTECT, related_name="loan")
    membership = models.ForeignKey("groups.Membership", on_delete=models.PROTECT, related_name="loans")
    settings_version = models.ForeignKey("groups.GroupSettings", on_delete=models.PROTECT)
    principal = models.PositiveBigIntegerField()
    interest_bps = models.PositiveIntegerField()
    interest_amount = models.PositiveBigIntegerField()
    total_due = models.PositiveBigIntegerField()
    repayment_months = models.PositiveSmallIntegerField()
    first_due_date = models.DateField()
    default_after_days = models.PositiveSmallIntegerField()
    terms_snapshot = models.JSONField()
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    approved_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    @property
    def paid_total(self): return self.repayments.aggregate(value=Sum("amount"))["value"] or 0
    @property
    def balance(self): return self.total_due - self.paid_total
    @property
    def principal_outstanding(self):
        paid = self.installments.aggregate(value=Sum("principal_paid"))["value"] or 0
        return self.principal - paid
    def save(self, *args, **kwargs):
        if self.pk and set(kwargs.get("update_fields") or []) != {"status"}:
            raise ValidationError("Approved loan terms cannot be changed.")
        return super().save(*args, **kwargs)

class LoanInstallment(models.Model):
    loan = models.ForeignKey(Loan, on_delete=models.PROTECT, related_name="installments")
    number = models.PositiveSmallIntegerField()
    due_date = models.DateField()
    principal_due = models.PositiveBigIntegerField()
    interest_due = models.PositiveBigIntegerField()
    principal_paid = models.PositiveBigIntegerField(default=0)
    interest_paid = models.PositiveBigIntegerField(default=0)
    class Meta:
        ordering = ["number"]
        constraints = [models.UniqueConstraint(fields=["loan", "number"], name="unique_loan_installment"), models.CheckConstraint(condition=models.Q(principal_paid__lte=models.F("principal_due")), name="installment_principal_not_overpaid"), models.CheckConstraint(condition=models.Q(interest_paid__lte=models.F("interest_due")), name="installment_interest_not_overpaid")]
    @property
    def total_due(self): return self.principal_due + self.interest_due
    @property
    def balance(self): return self.total_due - self.principal_paid - self.interest_paid
    @property
    def days_late(self): return max(0,(timezone.localdate()-self.due_date).days) if self.balance else 0

class ImmutableRecord(models.Model):
    class Meta: abstract = True
    def save(self, *args, **kwargs):
        if self.pk: raise ValidationError("Financial ledger records cannot be edited.")
        return super().save(*args, **kwargs)
    def delete(self, *args, **kwargs): raise ValidationError("Financial ledger records must be retained.")

class LoanRepayment(ImmutableRecord):
    loan = models.ForeignKey(Loan, on_delete=models.PROTECT, related_name="repayments")
    amount = models.PositiveBigIntegerField(validators=[MinValueValidator(1)])
    reference = models.CharField(max_length=120)
    submission_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    received_on = models.DateField(default=timezone.localdate)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["-pk"]
        constraints = [models.UniqueConstraint(fields=["loan", "reference"], name="unique_loan_payment_reference")]

class RepaymentAllocation(ImmutableRecord):
    repayment = models.ForeignKey(LoanRepayment, on_delete=models.PROTECT, related_name="allocations")
    installment = models.ForeignKey(LoanInstallment, on_delete=models.PROTECT)
    principal = models.PositiveBigIntegerField()
    interest = models.PositiveBigIntegerField()
    class Meta:
        constraints = [models.UniqueConstraint(fields=["repayment", "installment"], name="unique_repayment_allocation")]

class LoanDefaultEvent(models.Model):
    loan = models.ForeignKey(Loan, on_delete=models.PROTECT, related_name="default_events")
    started_on = models.DateField()
    cured_on = models.DateField(null=True, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["loan"], condition=models.Q(cured_on__isnull=True), name="one_open_loan_default")]
