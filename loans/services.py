from calendar import monthrange
from copy import deepcopy
from datetime import date, timedelta
from uuid import UUID
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from accounts.models import AuditLog
from contributions.models import Notification
from groups.models import Group, Membership
from .models import LoanRequest, Loan, LoanInstallment, LoanRepayment, RepaymentAllocation, LoanDefaultEvent, MAX_LOAN_AMOUNT

def audit(actor, request, action, **details):
    AuditLog.objects.create(actor=actor,target_id=request.membership.user_id,action=action,details={"group_id":request.membership.group_id,"loan_request_id":request.pk,**details})

def notify(request, key, message):
    Notification.objects.get_or_create(event_key=key,defaults={"recipient_id":request.membership.user_id,"loan_request":request,"message":message})

def month_due(first, offset):
    index = first.year*12 + first.month-1 + offset
    year, month = divmod(index,12)
    return first.replace(year=year,month=month+1,day=min(first.day,monthrange(year,month+1)[1]))

def _membership_lock(pk):
    group_id=Membership.objects.values_list("group_id",flat=True).get(pk=pk)
    group=Group.objects.select_for_update().get(pk=group_id)
    membership=Membership.objects.select_for_update().get(pk=pk)
    return group,membership

def _admin(actor, group, borrower_id):
    if not actor.is_authenticated or not actor.is_active or not actor.is_system_admin or group.is_archived or actor.pk==borrower_id:
        raise PermissionDenied

def _token(value):
    try: return UUID(str(value))
    except (ValueError,TypeError,AttributeError): raise ValidationError("Invalid submission token. Reload this page.")

def _amount(value, maximum=MAX_LOAN_AMOUNT):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValidationError("Use a positive whole RWF amount within the supported limit.")

@transaction.atomic
def request_loan(membership_id, owner, amount, purpose, repayment_months, submission_token):
    group,membership=_membership_lock(membership_id)
    if not owner.is_authenticated or membership.user_id!=owner.pk or not owner.is_active or not membership.is_active or group.is_archived:
        raise PermissionDenied
    token=_token(submission_token)
    _amount(amount)
    if type(repayment_months) is not int or not 1 <= repayment_months <= 60 or amount < repayment_months:
        raise ValidationError("Choose 1-60 months and at least 1 RWF of principal per installment.")
    purpose=purpose.strip()
    if not purpose or len(purpose)>2000: raise ValidationError("Provide a purpose of at most 2000 characters.")
    prior=LoanRequest.objects.filter(submission_token=token).first()
    if prior:
        if (prior.membership_id,prior.amount,prior.purpose,prior.repayment_months)!=(membership.pk,amount,purpose,repayment_months):
            raise ValidationError("This submission token was already used for a different request.")
        return prior
    if membership.loan_requests.filter(status=LoanRequest.Status.PENDING).exists():
        raise ValidationError("You already have a pending request in this group.")
    from ratings.services import loan_terms
    terms=loan_terms(membership.pk)
    if not terms["eligible"] or amount>terms["max_amount"]:
        raise ValidationError("This amount exceeds your current rating-based available limit.")
    request=LoanRequest.objects.create(membership=membership,amount=amount,purpose=purpose,repayment_months=repayment_months,submission_token=token,terms_snapshot=deepcopy(terms))
    audit(owner,request,"loan_requested",amount_rwf=amount,repayment_months=repayment_months)
    return request

@transaction.atomic
def decide_request(request_id, actor, decision, reason="", first_due_date=None, repayment_months=None):
    membership_id=LoanRequest.objects.values_list("membership_id",flat=True).get(pk=request_id)
    group,membership=_membership_lock(membership_id)
    _admin(actor,group,membership.user_id)
    request=LoanRequest.objects.select_for_update().get(pk=request_id)
    if request.status!=LoanRequest.Status.PENDING: raise ValidationError("This request has already been decided.")
    reason=reason.strip()
    if len(reason)>2000: raise ValidationError("Decision notes must be at most 2000 characters.")
    if decision==LoanRequest.Status.REJECTED:
        if not reason: raise ValidationError("A reason is required when rejecting a loan.")
    elif decision==LoanRequest.Status.APPROVED:
        from ratings.services import loan_terms, evaluate_membership
        terms=loan_terms(membership.pk)
        if not terms["eligible"] or request.amount>terms["max_amount"]:
            raise ValidationError("The borrower is no longer eligible for this amount. Reject it or ask for a new request.")
        months=repayment_months if repayment_months is not None else request.repayment_months
        if type(months) is not int or not 1<=months<=60 or request.amount<months:
            raise ValidationError("Choose 1-60 installments with at least 1 RWF principal each.")
        if type(first_due_date) is not date or first_due_date<=timezone.localdate():
            raise ValidationError("The first installment must be due after today.")
        try:
            month_due(first_due_date,months-1)
        except ValueError:
            raise ValidationError("The repayment schedule exceeds the supported date range.")
        version=group.settings_versions.get(pk=terms["settings_version_id"])
        interest=(request.amount*terms["interest_bps"]+9999)//10000
        loan=Loan.objects.create(request=request,membership=membership,settings_version=version,principal=request.amount,interest_bps=terms["interest_bps"],interest_amount=interest,total_due=request.amount+interest,repayment_months=months,first_due_date=first_due_date,default_after_days=version.loan_default_days,terms_snapshot={**deepcopy(terms),"rating_rules":deepcopy(version.rating_classes),"loan_default_days":version.loan_default_days,"interest_policy":"flat_once_ceiling_rwf","allocation_policy":"oldest_installment_interest_then_principal"},approved_by=actor)
        principal_base,principal_extra=divmod(request.amount,months)
        interest_base,interest_extra=divmod(interest,months)
        LoanInstallment.objects.bulk_create([LoanInstallment(loan=loan,number=i+1,due_date=month_due(first_due_date,i),principal_due=principal_base+(i<principal_extra),interest_due=interest_base+(i<interest_extra)) for i in range(months)])
    else: raise ValidationError("Choose approve or reject.")
    request.status,request.reviewed_by,request.reviewed_at,request.decision_reason=decision,actor,timezone.now(),reason
    request.save(update_fields=["status","reviewed_by","reviewed_at","decision_reason"])
    audit(actor,request,"loan_"+decision,reason=reason,loan_id=loan.pk if decision=="approved" else None)
    notify(request,f"loan-decision:{request.pk}",f"{group.name}: your {request.amount} RWF loan request was {decision}."+(f" Reason: {reason}" if reason else ""))
    if decision==LoanRequest.Status.APPROVED:
        evaluate_membership(membership.pk,actor=actor,trigger="loan_approved")
    return request

def sync_defaults(membership, day):
    """Caller holds the group lock. Record threshold transitions without invoking ratings."""
    evidence=[]
    for loan in Loan.objects.select_for_update().filter(membership=membership).select_related("request__membership"):
        unpaid=[item for item in loan.installments.all() if item.balance]
        oldest=min((item.due_date for item in unpaid),default=None)
        defaulted=oldest is not None and (day-oldest).days>=loan.default_after_days
        opened=loan.default_events.filter(cured_on__isnull=True).first()
        if defaulted and opened is None:
            opened=LoanDefaultEvent.objects.create(loan=loan,started_on=oldest+timedelta(days=loan.default_after_days))
            audit(None,loan.request,"loan_defaulted",loan_id=loan.pk,default_event_id=opened.pk,started_on=opened.started_on.isoformat())
            notify(loan.request,f"loan-default:{opened.pk}",f"Your loan {loan.pk} in {membership.group.name} is in default. Please review the overdue schedule.")
        elif not defaulted and opened:
            opened.cured_on=day
            opened.save(update_fields=["cured_on"])
            audit(None,loan.request,"loan_default_cured",loan_id=loan.pk,default_event_id=opened.pk)
        status=Loan.Status.REPAID if not unpaid else Loan.Status.DEFAULTED if defaulted else Loan.Status.ACTIVE
        if loan.status!=status:
            loan.status=status
            loan.save(update_fields=["status"])
        evidence.append({"loan_id":loan.pk,"balance_rwf":sum(item.balance for item in unpaid),"principal_outstanding_rwf":sum(item.principal_due-item.principal_paid for item in unpaid),"oldest_unpaid_due_date":oldest.isoformat() if oldest else None,"currently_defaulted":defaulted})
    return evidence

@transaction.atomic
def record_repayment(loan_id, actor, amount, reference, submission_token):
    membership_id=Loan.objects.values_list("membership_id",flat=True).get(pk=loan_id)
    group,membership=_membership_lock(membership_id)
    _admin(actor,group,membership.user_id)
    loan=Loan.objects.select_for_update().get(pk=loan_id)
    token=_token(submission_token)
    _amount(amount,2*MAX_LOAN_AMOUNT)
    reference=reference.strip().upper()
    if not reference or len(reference)>120: raise ValidationError("Provide a payment reference of at most 120 characters.")
    prior=LoanRepayment.objects.filter(submission_token=token).first()
    if prior:
        if (prior.loan_id,prior.amount,prior.reference)!=(loan.pk,amount,reference): raise ValidationError("This submission token belongs to a different payment.")
        return prior
    if loan.repayments.filter(reference=reference).exists(): raise ValidationError("This payment reference is already recorded for this loan.")
    if amount>loan.balance: raise ValidationError("The payment exceeds the remaining loan balance.")
    # Capture any default reached before this payment, even if the daily task was missed.
    sync_defaults(membership,timezone.localdate())
    repayment=LoanRepayment.objects.create(loan=loan,amount=amount,reference=reference,submission_token=token,recorded_by=actor)
    remaining=amount
    for installment in loan.installments.select_for_update().order_by("number"):
        interest=min(remaining,installment.interest_due-installment.interest_paid)
        remaining-=interest
        principal=min(remaining,installment.principal_due-installment.principal_paid)
        remaining-=principal
        if interest or principal:
            installment.interest_paid+=interest
            installment.principal_paid+=principal
            installment.save(update_fields=["interest_paid","principal_paid"])
            RepaymentAllocation.objects.create(repayment=repayment,installment=installment,interest=interest,principal=principal)
        if not remaining: break
    if remaining: raise ValidationError("The repayment could not be fully allocated.")
    audit(actor,loan.request,"loan_repayment_recorded",loan_id=loan.pk,repayment_id=repayment.pk,amount_rwf=amount,reference=reference)
    from ratings.services import evaluate_membership
    evaluate_membership(membership.pk,actor=actor,trigger="loan_repayment")
    return repayment
