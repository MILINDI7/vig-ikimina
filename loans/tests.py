from copy import deepcopy
from datetime import date, datetime
from io import StringIO
from uuid import uuid4
from unittest.mock import patch
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db.models import Sum
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone
from accounts.models import User, AuditLog
from contributions.models import Contribution, Notification
from contributions.services import ensure_period
from groups.models import Group, Membership, GroupSettings
from ratings.models import MemberRating
from ratings.services import evaluate_membership, loan_terms
from .models import LoanRequest, Loan, LoanRepayment, RepaymentAllocation, LoanDefaultEvent
from .services import request_loan, decide_request, record_repayment, month_due

@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class LoanTests(TestCase):
    def setUp(self):
        clock=patch("django.utils.timezone.localdate",return_value=date(2025,12,10))
        clock.start()
        self.addCleanup(clock.stop)
        self.admin=User.objects.create_user(username="admin",role="admin",must_change_password=False)
        self.member=User.objects.create_user(username="member",must_change_password=False)
        self.treasurer=User.objects.create_user(username="treasurer",role="treasurer",must_change_password=False)
        self.other=User.objects.create_user(username="other",must_change_password=False)
        self.group=Group.objects.create(name="Community")
        self.group2=Group.objects.create(name="Separate")
        self.classes=[{"code":"A","name":"Good","description":"On time","max_days_late":0,"max_late_payments":0,"max_defaults":0,"loan_eligible":True,"interest_bps":500,"max_loan_multiple":3},{"code":"E","name":"Restricted","description":"Fallback","max_days_late":None,"max_late_payments":None,"max_defaults":None,"loan_eligible":False,"interest_bps":0,"max_loan_multiple":0}]
        self.version=GroupSettings.objects.create(group=self.group,effective_from=date(2025,11,1),rating_classes=self.classes,created_by=self.admin)
        GroupSettings.objects.create(group=self.group2,effective_from=date(2025,11,1),rating_classes=self.classes,created_by=self.admin)
        self.membership=Membership.objects.create(group=self.group,user=self.member)
        self.second=Membership.objects.create(group=self.group2,user=self.member)
        self.review_member=Membership.objects.create(group=self.group,user=self.treasurer,role="treasurer")
        self.other_member=Membership.objects.create(group=self.group,user=self.other)
        Membership.objects.all().update(joined_at=timezone.make_aware(datetime(2025,11,1)))
        period=ensure_period(self.group.pk,date(2025,11,1))
        self.saving=Contribution.objects.get(period=period,membership=self.membership)
        Contribution.objects.filter(pk=self.saving.pk).update(status="approved",approved_at=timezone.now())
    def request(self,amount=10001,months=3,token=None):
        return request_loan(self.membership.pk,self.member,amount,"Business stock",months,token or uuid4())
    def approve(self,item=None,first=date(2026,1,31),months=None):
        item=item or self.request()
        decide_request(item.pk,self.admin,"approved",first_due_date=first,repayment_months=months)
        return Loan.objects.get(request=item)
    def pay(self,loan,amount,reference="PAY-001",token=None):
        return record_repayment(loan.pk,self.admin,amount,reference,token or uuid4())
    def test_request_eligibility_positive_integer_amount_and_pending_limit(self):
        for amount in [0,-1,1.2,True,30001]:
            with self.assertRaises(ValidationError): self.request(amount=amount)
        for months in [0,61,1.5]:
            with self.assertRaises(ValidationError): self.request(months=months)
        with self.assertRaises(ValidationError): self.request(amount=2,months=3)
        item=self.request()
        self.assertEqual(item.status,"pending")
        self.assertEqual(item.terms_snapshot["max_amount"],30000)
        with self.assertRaises(ValidationError): self.request()
        self.assertTrue(AuditLog.objects.filter(action="loan_requested").exists())
    def test_request_token_replay_is_idempotent_and_cannot_change_payload(self):
        token=uuid4()
        first=self.request(token=token)
        self.assertEqual(self.request(token=token).pk,first.pk)
        self.assertEqual(LoanRequest.objects.count(),1)
        with self.assertRaises(ValidationError): self.request(amount=10002,token=token)
    def test_request_owner_and_membership_permissions(self):
        with self.assertRaises(PermissionDenied): request_loan(self.membership.pk,self.other,1000,"Purpose",1,uuid4())
        self.membership.is_active=False
        self.membership.save()
        with self.assertRaises(PermissionDenied): self.request()
        self.membership.is_active=True
        self.membership.save()
        self.group.is_archived=True
        self.group.save()
        with self.assertRaises(PermissionDenied): self.request()
    def test_approval_interest_schedule_exact_sums_and_month_end(self):
        item=self.request()
        loan=self.approve(item)
        self.assertEqual(loan.principal,10001)
        self.assertEqual(loan.interest_amount,501)
        self.assertEqual(loan.total_due,10502)
        rows=list(loan.installments.all())
        self.assertEqual([row.due_date for row in rows],[date(2026,1,31),date(2026,2,28),date(2026,3,31)])
        self.assertEqual(sum(row.principal_due for row in rows),10001)
        self.assertEqual(sum(row.interest_due for row in rows),501)
        self.assertEqual(sum(row.total_due for row in rows),10502)
        self.assertTrue(all(row.total_due>0 for row in rows))
        self.assertEqual(loan.terms_snapshot["rating_code"],"A")
        self.assertTrue(Notification.objects.filter(loan_request=item,event_key=f"loan-decision:{item.pk}").exists())
        with self.assertRaises(ValidationError): loan.save()
        self.assertEqual(month_due(date(2024,1,31),1),date(2024,2,29))
    def test_approval_rechecks_latest_rules_and_preserves_request_terms(self):
        item=self.request()
        classes=deepcopy(self.classes)
        classes[0]["interest_bps"]=1000
        version=GroupSettings.objects.create(group=self.group,effective_from=date(2025,12,11),rating_classes=classes,loan_default_days=10,created_by=self.admin)
        with patch("django.utils.timezone.localdate",return_value=date(2025,12,11)):
            loan=self.approve(item)
        item.refresh_from_db()
        self.assertEqual(item.terms_snapshot["interest_bps"],500)
        self.assertEqual(loan.interest_bps,1000)
        self.assertEqual(loan.interest_amount,1001)
        self.assertEqual(loan.settings_version_id,version.pk)
        self.assertEqual(loan.default_after_days,10)
    def test_approval_rejects_changed_eligibility(self):
        item=self.request(amount=25000)
        classes=deepcopy(self.classes)
        classes[0]["max_loan_multiple"]=1
        GroupSettings.objects.create(group=self.group,effective_from=date(2025,12,11),rating_classes=classes,created_by=self.admin)
        with patch("django.utils.timezone.localdate",return_value=date(2025,12,11)):
            with self.assertRaises(ValidationError): self.approve(item)
        self.assertFalse(Loan.objects.exists())
        item.refresh_from_db()
        self.assertEqual(item.status,"pending")
    def test_only_admin_can_decide_and_cannot_decide_own_request(self):
        item=self.request()
        for actor in [self.member,self.other,self.treasurer]:
            with self.assertRaises(PermissionDenied): decide_request(item.pk,actor,"rejected","Reason")
        self.member.role="admin"
        self.member.save()
        with self.assertRaises(PermissionDenied): decide_request(item.pk,self.member,"approved",first_due_date=date(2026,1,1))
    def test_rejection_reason_and_duplicate_decision(self):
        item=self.request()
        with self.assertRaises(ValidationError): decide_request(item.pk,self.admin,"rejected"," ")
        decide_request(item.pk,self.admin,"rejected","Not enough liquidity")
        self.assertFalse(Loan.objects.exists())
        self.assertIn("Not enough liquidity",Notification.objects.get(loan_request=item).message)
        with self.assertRaises(ValidationError): self.approve(item)
        self.assertNotEqual(self.request().pk,item.pk)
    def test_first_due_date_and_admin_schedule_validation(self):
        item=self.request()
        for day in [None,date(2025,12,10),date(2025,12,9)]:
            with self.assertRaises(ValidationError): self.approve(item,first=day)
        with self.assertRaises(ValidationError): self.approve(item,months=61)
        loan=self.approve(item,months=5)
        self.assertEqual(loan.installments.count(),5)
    def test_capacity_deducts_existing_principal_and_prevents_overcommit(self):
        loan=self.approve(self.request(amount=25000))
        terms=loan_terms(self.membership.pk)
        self.assertEqual(terms["outstanding_principal"],25000)
        self.assertEqual(terms["max_amount"],5000)
        with self.assertRaises(ValidationError): self.request(amount=5001)
        item=self.request(amount=5000)
        self.approve(item)
        self.assertEqual(loan_terms(self.membership.pk)["max_amount"],0)
        self.assertEqual(MemberRating.objects.get(membership=self.membership).metrics["outstanding_loans"],2)
    def test_repayment_allocation_partial_fifo_interest_then_principal(self):
        loan=self.approve()
        first=loan.installments.first()
        payment=self.pay(loan,100)
        first.refresh_from_db()
        self.assertEqual(first.interest_paid,100)
        self.assertEqual(first.principal_paid,0)
        self.assertEqual(payment.allocations.get().interest,100)
        self.pay(loan,first.total_due-100+20,reference="PAY-002")
        rows=list(loan.installments.all())
        self.assertEqual(rows[0].balance,0)
        self.assertEqual(rows[1].interest_paid,20)
        self.assertEqual(rows[1].principal_paid,0)
        self.assertEqual(sum(a.principal+a.interest for a in RepaymentAllocation.objects.all()),LoanRepayment.objects.aggregate(total=Sum("amount"))["total"])
    def test_full_repayment_closes_loan_and_restores_capacity(self):
        loan=self.approve()
        payment=self.pay(loan,loan.total_due)
        loan.refresh_from_db()
        self.assertEqual(loan.status,"repaid")
        self.assertEqual(loan.balance,0)
        self.assertEqual(loan.principal_outstanding,0)
        self.assertEqual(loan_terms(self.membership.pk)["max_amount"],30000)
        self.assertEqual(MemberRating.objects.get(membership=self.membership).metrics["outstanding_loans"],0)
        with self.assertRaises(ValidationError): payment.save()
        with self.assertRaises(ValidationError): payment.delete()
    def test_repayment_duplicate_tokens_references_and_overpayment(self):
        loan=self.approve()
        token=uuid4()
        first=self.pay(loan,100,token=token)
        self.assertEqual(self.pay(loan,100,token=token).pk,first.pk)
        with self.assertRaises(ValidationError): self.pay(loan,101,token=token)
        with self.assertRaises(ValidationError): self.pay(loan,100,reference=" pay-001 ")
        with self.assertRaises(ValidationError): self.pay(loan,loan.total_due,reference="OVER")
        for amount in [0,-1,1.5,True]:
            with self.assertRaises(ValidationError): self.pay(loan,amount,reference="INVALID")
        self.assertEqual(loan.repayments.count(),1)
    def test_repayment_permissions_and_archived_group(self):
        loan=self.approve()
        for actor in [self.member,self.other,self.treasurer]:
            with self.assertRaises(PermissionDenied): record_repayment(loan.pk,actor,100,"REF",uuid4())
        self.member.role="admin"
        self.member.save()
        with self.assertRaises(PermissionDenied): record_repayment(loan.pk,self.member,100,"REF",uuid4())
        self.group.is_archived=True
        self.group.save()
        with self.assertRaises(PermissionDenied): self.pay(loan,100)
    def test_removed_members_can_repay_prior_loans(self):
        loan=self.approve()
        self.membership.is_active=False
        self.membership.save()
        self.pay(loan,100)
        self.assertEqual(loan.balance,loan.total_due-100)
        with self.assertRaises(PermissionDenied): self.request()
    def test_due_inclusive_default_threshold_and_idempotence(self):
        loan=self.approve(first=date(2025,12,11))
        for day in [date(2025,12,11),date(2026,1,9)]:
            with patch("django.utils.timezone.localdate",return_value=day):
                rating=evaluate_membership(self.membership.pk)
                self.assertEqual(rating.metrics["defaults"],0)
        with patch("django.utils.timezone.localdate",return_value=date(2026,1,10)):
            rating=evaluate_membership(self.membership.pk)
            evaluate_membership(self.membership.pk)
        loan.refresh_from_db()
        self.assertEqual(loan.status,"defaulted")
        self.assertEqual(rating.code,"E")
        self.assertEqual(rating.metrics["defaults"],1)
        self.assertEqual(loan.default_events.count(),1)
        self.assertEqual(loan.default_events.get().started_on,date(2026,1,10))
        self.assertEqual(Notification.objects.filter(event_key__startswith="loan-default:").count(),1)
    def test_default_is_captured_before_repayment_even_when_job_was_missed(self):
        loan=self.approve(first=date(2025,12,11))
        with patch("django.utils.timezone.localdate",return_value=date(2026,1,15)):
            self.pay(loan,loan.total_due)
        event=loan.default_events.get()
        self.assertEqual(event.started_on,date(2026,1,10))
        self.assertEqual(event.cured_on,date(2026,1,15))
        self.assertEqual(MemberRating.objects.get(membership=self.membership).metrics["defaults"],1)
        with patch("django.utils.timezone.localdate",return_value=date(2027,1,16)):
            rating=evaluate_membership(self.membership.pk)
        self.assertEqual(rating.metrics["defaults"],0)
        self.assertEqual(rating.code,"A")
    def test_partial_cure_closes_default_but_preserves_history(self):
        loan=self.approve(first=date(2025,12,11))
        first=loan.installments.first()
        with patch("django.utils.timezone.localdate",return_value=date(2026,1,10)):
            self.pay(loan,first.total_due)
        loan.refresh_from_db()
        self.assertEqual(loan.status,"active")
        self.assertIsNotNone(loan.default_events.get().cured_on)
        self.assertEqual(MemberRating.objects.get(membership=self.membership).metrics["defaults"],1)
    def test_approved_settings_remain_fixed_after_new_group_rules(self):
        loan=self.approve()
        snapshot=deepcopy(loan.terms_snapshot)
        classes=deepcopy(self.classes)
        classes[0]["interest_bps"]=2500
        GroupSettings.objects.create(group=self.group,effective_from=date(2025,12,11),rating_classes=classes,loan_default_days=1,created_by=self.admin)
        with patch("django.utils.timezone.localdate",return_value=date(2025,12,12)):
            evaluate_membership(self.membership.pk)
        loan.refresh_from_db()
        self.assertEqual(loan.interest_bps,500)
        self.assertEqual(loan.default_after_days,30)
        self.assertEqual(loan.terms_snapshot,snapshot)
    def test_repayment_rolls_back_if_audit_fails(self):
        loan=self.approve()
        with patch("loans.services.audit",side_effect=RuntimeError("Audit unavailable")):
            with self.assertRaises(RuntimeError): self.pay(loan,100)
        self.assertFalse(loan.repayments.exists())
        self.assertFalse(RepaymentAllocation.objects.exists())
        self.assertEqual(loan.installments.first().interest_paid,0)
        self.assertEqual(loan.balance,loan.total_due)
    def test_approval_rolls_back_contract_schedule_and_notice_if_audit_fails(self):
        item=self.request()
        with patch("loans.services.audit",side_effect=RuntimeError("Audit unavailable")):
            with self.assertRaises(RuntimeError): self.approve(item)
        item.refresh_from_db()
        self.assertEqual(item.status,"pending")
        self.assertFalse(Loan.objects.exists())
        self.assertFalse(Notification.objects.filter(loan_request=item).exists())
    def test_http_owner_privacy_admin_decisions_and_repayments(self):
        self.client.force_login(self.member)
        url=reverse("loan_request_create",args=[self.membership.pk])
        self.assertEqual(self.client.get(url).status_code,200)
        self.assertEqual(self.client.post(url,{"amount":10001,"purpose":"Business stock","repayment_months":3,"submission_token":uuid4()}).status_code,302)
        item=LoanRequest.objects.get()
        detail=reverse("loan_request_detail",args=[item.pk])
        self.assertContains(self.client.get(detail),"Business stock")
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(detail).status_code,404)
        self.assertEqual(self.client.get(url).status_code,404)
        self.assertNotContains(self.client.get(reverse("loan_list")),"Business stock")
        self.client.force_login(self.treasurer)
        self.assertEqual(self.client.get(detail).status_code,404)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(detail),"Review request")
        self.assertEqual(self.client.post(detail,{"decision":"approved","first_due_date":"2026-01-31","repayment_months":3}).status_code,302)
        loan=Loan.objects.get(request=item)
        repayment=reverse("loan_repayment",args=[loan.pk])
        self.assertEqual(self.client.get(repayment).status_code,200)
        self.assertEqual(self.client.post(repayment,{"amount":100,"reference":"HTTP-REF","submission_token":uuid4()}).status_code,302)
        self.assertContains(self.client.get(detail),"Repayment schedule")
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(repayment).status_code,403)
        response=self.client.get(detail)
        self.assertIn("no-store",response["Cache-Control"])
        self.assertContains(response,"HTTP-REF")
        self.assertContains(self.client.get(reverse("notifications")),reverse("loan_request_detail",args=[item.pk]))
    def test_csrf_on_all_loan_mutations(self):
        item=self.request()
        client=Client(enforce_csrf_checks=True)
        client.force_login(self.member)
        self.assertEqual(client.post(reverse("loan_request_create",args=[self.membership.pk]),{}).status_code,403)
        client.force_login(self.admin)
        self.assertEqual(client.post(reverse("loan_request_detail",args=[item.pk]),{}).status_code,403)
        loan=self.approve(item)
        self.assertEqual(client.post(reverse("loan_repayment",args=[loan.pk]),{}).status_code,403)
    def test_loans_are_separate_between_groups(self):
        self.approve()
        self.assertEqual(loan_terms(self.second.pk)["outstanding_principal"],0)
        self.assertEqual(evaluate_membership(self.second.pk).metrics["outstanding_loans"],0)
    def test_daily_command_and_archival_do_not_duplicate_defaults(self):
        loan=self.approve(first=date(2025,12,11))
        with patch("django.utils.timezone.localdate",return_value=date(2026,1,10)):
            output=StringIO()
            call_command("recalculate_loans",stdout=output)
            call_command("recalculate_contributions",stdout=output)
            self.assertEqual(loan.default_events.count(),1)
            self.assertIn("membership",output.getvalue())
        self.group.is_archived=True
        self.group.save()
        count=AuditLog.objects.filter(details__group_id=self.group.pk).count()
        with patch("django.utils.timezone.localdate",return_value=date(2026,4,1)):
            call_command("recalculate_loans",stdout=StringIO())
        self.assertEqual(AuditLog.objects.filter(details__group_id=self.group.pk).count(),count)

    def test_schedule_rejects_date_overflow_without_partial_contract(self):
        item=self.request()
        with self.assertRaises(ValidationError): self.approve(item,first=date(9999,12,31))
        self.assertFalse(Loan.objects.exists())
        item.refresh_from_db()
        self.assertEqual(item.status,"pending")
    def test_pending_unapproved_savings_do_not_raise_borrowing_capacity(self):
        Contribution.objects.filter(pk=self.saving.pk).update(status="pending")
        self.assertEqual(loan_terms(self.membership.pk)["max_amount"],0)
        with self.assertRaises(ValidationError): self.request()
    def test_repeated_default_episodes_count_once_per_loan(self):
        loan=self.approve(first=date(2025,12,11))
        with patch("django.utils.timezone.localdate",return_value=date(2026,1,10)):
            self.pay(loan,loan.installments.first().total_due)
        with patch("django.utils.timezone.localdate",return_value=date(2026,2,10)):
            rating=evaluate_membership(self.membership.pk)
        self.assertEqual(loan.default_events.count(),2)
        self.assertEqual(loan.default_events.filter(cured_on__isnull=True).count(),1)
        self.assertEqual(rating.metrics["defaults"],1)
    def test_http_blank_rejection_and_stale_approval_are_safe(self):
        item=self.request()
        self.client.force_login(self.admin)
        url=reverse("loan_request_detail",args=[item.pk])
        self.assertEqual(self.client.post(url,{"decision":"rejected","reason":""}).status_code,200)
        item.refresh_from_db()
        self.assertEqual(item.status,"pending")
        self.approve(item)
        response=self.client.post(url,{"decision":"approved","first_due_date":"2026-01-31","repayment_months":3})
        self.assertEqual(response.status_code,200)
        self.assertContains(response,"already been decided")
        self.assertEqual(Loan.objects.count(),1)

    def test_large_integer_principal_and_interest_can_be_repaid_in_full(self):
        classes=deepcopy(self.classes)
        classes[0]["interest_bps"]=10000
        classes[0]["max_loan_multiple"]=100000000
        GroupSettings.objects.create(group=self.group,effective_from=date(2025,12,10),rating_classes=classes,created_by=self.admin)
        loan=self.approve(self.request(amount=10**12,months=1))
        self.assertEqual(loan.total_due,2*10**12)
        self.pay(loan,loan.total_due)
        self.assertEqual(loan.balance,0)
    def test_zero_interest_preserves_integer_installment_totals(self):
        classes=deepcopy(self.classes)
        classes[0]["interest_bps"]=0
        GroupSettings.objects.create(group=self.group,effective_from=date(2025,12,10),rating_classes=classes,created_by=self.admin)
        loan=self.approve(self.request(amount=10,months=3))
        self.assertEqual(loan.interest_amount,0)
        self.assertEqual([row.total_due for row in loan.installments.all()],[4,3,3])
        self.pay(loan,10)
        self.assertEqual(loan.balance,0)
    def test_default_days_form_validation_and_legacy_blank_default(self):
        import json
        from groups.forms import SettingsForm
        data={"contribution_amount":10000,"due_day":5,"effective_from":"2025-12-11","lateness_tiers":"[]","rating_classes":json.dumps(self.classes)}
        for invalid in [0,-1,366]:
            self.assertFalse(SettingsForm(data={**data,"loan_default_days":invalid}).is_valid())
        form=SettingsForm(data=data)
        self.assertTrue(form.is_valid(),form.errors)
        self.assertEqual(form.cleaned_data["loan_default_days"],30)
        form=SettingsForm(data={**data,"loan_default_days":15})
        self.assertTrue(form.is_valid(),form.errors)
        self.assertEqual(form.cleaned_data["loan_default_days"],15)
