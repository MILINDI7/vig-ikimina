from copy import deepcopy
from datetime import date, datetime
from io import BytesIO, StringIO
import json
import tempfile
from unittest.mock import patch
from PIL import Image
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from accounts.models import User, AuditLog
from contributions.models import Contribution
from contributions.services import ensure_period, recalculate, submit_proof, review_proof
from groups.models import Group, GroupSettings, Membership
from groups.forms import SettingsForm
from .models import MemberRating, RatingHistory
from .services import evaluate_membership, evaluate_group, loan_terms, choose_class, window_start

@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class RatingTests(TestCase):
    def setUp(self):
        clock=patch("django.utils.timezone.localdate",return_value=date(2025,12,10))
        clock.start()
        self.addCleanup(clock.stop)
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        storage=override_settings(STORAGES={"proofs":{"BACKEND":"django.core.files.storage.FileSystemStorage","OPTIONS":{"location":directory.name}},"default":{"BACKEND":"django.core.files.storage.FileSystemStorage","OPTIONS":{"location":directory.name}},"staticfiles":{"BACKEND":"django.contrib.staticfiles.storage.StaticFilesStorage"}})
        storage.enable()
        self.addCleanup(storage.disable)
        self.admin=User.objects.create_user(username="admin",role="admin",must_change_password=False)
        self.member=User.objects.create_user(username="member",must_change_password=False)
        self.treasurer=User.objects.create_user(username="treasurer",must_change_password=False)
        self.outsider=User.objects.create_user(username="outsider",role="treasurer",must_change_password=False)
        self.group=Group.objects.create(name="First group")
        self.other=Group.objects.create(name="Other group")
        self.classes=[{"code":"A","name":"Excellent","description":"On time","max_days_late":0,"max_late_payments":0,"max_defaults":0,"max_outstanding_loans":0,"loan_eligible":True,"interest_bps":500,"max_loan_multiple":3},{"code":"B","name":"Watch","description":"Some late payments","max_days_late":10,"max_late_payments":2,"max_defaults":0,"loan_eligible":True,"interest_bps":1000,"max_loan_multiple":2},{"code":"E","name":"Restricted","description":"Fallback","max_days_late":None,"max_late_payments":None,"max_defaults":None,"loan_eligible":False,"interest_bps":0,"max_loan_multiple":0}]
        self.version=GroupSettings.objects.create(group=self.group,effective_from=date(2024,1,1),rating_classes=self.classes,created_by=self.admin)
        GroupSettings.objects.create(group=self.other,effective_from=date(2024,1,1),rating_classes=self.classes,created_by=self.admin)
        self.membership=Membership.objects.create(group=self.group,user=self.member)
        self.second=Membership.objects.create(group=self.other,user=self.member)
        self.reviewer=Membership.objects.create(group=self.group,user=self.treasurer,role="treasurer")
        Membership.objects.create(group=self.other,user=self.outsider,role="treasurer")
        Membership.objects.all().update(joined_at=timezone.make_aware(datetime(2024,1,1)))
        self.period=ensure_period(self.group.pk,date(2025,11,1))
        self.contribution=Contribution.objects.get(period=self.period,membership=self.membership)
    def image(self):
        output=BytesIO()
        Image.new("RGB",(10,10),"green").save(output,format="JPEG")
        return ContentFile(output.getvalue(),name="proof.jpg")
    def submit_on(self,day):
        with patch("django.utils.timezone.localdate",return_value=day):
            return submit_proof(self.contribution.pk,self.member,self.image())
    def approved(self,month,days):
        period=ensure_period(self.group.pk,month)
        row=Contribution.objects.get(period=period,membership=self.membership)
        Contribution.objects.filter(pk=row.pk).update(status="approved",payment_late_days=days,approved_at=timezone.now())
        return row
    def test_initial_rating_and_unchanged_evaluations_do_not_duplicate_history(self):
        with patch("django.utils.timezone.localdate",return_value=date(2025,12,5)):
            first=evaluate_membership(self.membership.pk)
            second=evaluate_membership(self.membership.pk)
        self.assertEqual(first.code,"A")
        self.assertEqual(first.pk,second.pk)
        self.assertEqual(self.membership.rating_history.count(),1)
        history=self.membership.rating_history.get()
        self.assertEqual(history.previous_code,"")
        self.assertEqual(history.metrics["max_days_late"],0)
    def test_late_event_changes_rating_and_writes_history_and_audit(self):
        with patch("django.utils.timezone.localdate",return_value=date(2025,12,5)):
            evaluate_membership(self.membership.pk)
        recalculate(self.contribution.pk)
        current=MemberRating.objects.get(membership=self.membership)
        self.assertEqual(current.code,"B")
        self.assertEqual(current.metrics["max_days_late"],5)
        self.assertEqual(current.metrics["late_payments"],1)
        latest=self.membership.rating_history.first()
        self.assertEqual(latest.previous_code,"A")
        self.assertEqual(latest.code,"B")
        self.assertTrue(AuditLog.objects.filter(action="rating_changed",details__history_id=latest.pk).exists())
    def test_pending_exempt_then_decline_restores_rating_automatically(self):
        proof=self.submit_on(date(2025,12,4))
        recalculate(self.contribution.pk)
        rating=MemberRating.objects.get(membership=self.membership)
        self.assertEqual(rating.code,"A")
        self.assertEqual(rating.metrics["late_payments"],0)
        self.assertIn(self.contribution.pk,rating.metrics["pending_excluded"])
        review_proof(self.contribution.pk,self.treasurer,"declined","Wrong reference",proof.pk)
        rating.refresh_from_db()
        self.assertEqual(rating.code,"B")
        self.assertEqual(self.membership.rating_history.first().trigger,"proof_declined")
    def test_late_upload_is_exempt_while_pending_and_approval_uses_submission_date(self):
        proof=self.submit_on(date(2025,12,7))
        self.assertEqual(MemberRating.objects.get(membership=self.membership).code,"A")
        with patch("django.utils.timezone.localdate",return_value=date(2025,12,30)):
            review_proof(self.contribution.pk,self.treasurer,"approved","",proof.pk)
        rating=MemberRating.objects.get(membership=self.membership)
        self.assertEqual(rating.code,"B")
        self.assertEqual(rating.metrics["max_days_late"],2)
        self.assertEqual(self.membership.rating_history.first().trigger,"proof_approved")
    def test_on_time_submission_stays_good_after_slow_review(self):
        proof=self.submit_on(date(2025,12,4))
        with patch("django.utils.timezone.localdate",return_value=date(2025,12,30)):
            review_proof(self.contribution.pk,self.treasurer,"approved","",proof.pk)
        self.assertEqual(MemberRating.objects.get(membership=self.membership).code,"A")
    def test_late_payment_count_threshold(self):
        self.approved(date(2025,11,1),2)
        self.approved(date(2025,10,1),3)
        self.assertEqual(evaluate_membership(self.membership.pk).code,"B")
        self.approved(date(2025,9,1),1)
        self.assertEqual(evaluate_membership(self.membership.pk).code,"E")
    def test_twelve_month_window_ages_out_settled_payments_but_not_debts(self):
        self.approved(date(2025,11,1),0)
        old=self.approved(date(2024,11,1),15)
        with patch("django.utils.timezone.localdate",return_value=date(2025,12,4)):
            self.assertEqual(evaluate_membership(self.membership.pk).code,"E")
        with patch("django.utils.timezone.localdate",return_value=date(2025,12,5)):
            self.assertEqual(evaluate_membership(self.membership.pk).code,"A")
        rating=evaluate_membership(self.membership.pk)
        self.assertEqual(rating.code,"A")
        Contribution.objects.filter(pk=old.pk).update(status="not_paid",approved_at=None)
        rating=evaluate_membership(self.membership.pk)
        self.assertEqual(rating.code,"E")
        self.assertTrue(rating.metrics["late_contributions"][0]["unresolved_outside_window"])
    def test_calendar_window_handles_leap_day(self):
        self.assertEqual(window_start(date(2024,2,29)),date(2023,3,1))
        self.assertEqual(window_start(date(2025,12,10)),date(2024,12,11))
    def test_groups_have_independent_ratings_and_rules(self):
        self.assertEqual(evaluate_membership(self.membership.pk).code,"B")
        self.assertEqual(evaluate_membership(self.second.pk).code,"A")
        self.assertEqual(self.membership.rating_history.count(),1)
        self.assertEqual(self.second.rating_history.count(),1)
    def test_future_settings_apply_only_when_effective_and_preserve_history(self):
        initial=evaluate_membership(self.membership.pk)
        history=self.membership.rating_history.first()
        old_rules=deepcopy(history.rules_snapshot)
        classes=deepcopy(self.classes)
        classes[1]["interest_bps"]=1500
        classes[1]["name"]="New watch rules"
        new=GroupSettings.objects.create(group=self.group,effective_from=date(2025,12,11),rating_classes=classes,created_by=self.admin)
        self.assertEqual(evaluate_membership(self.membership.pk).settings_version_id,self.version.pk)
        with patch("django.utils.timezone.localdate",return_value=date(2025,12,11)):
            current=evaluate_membership(self.membership.pk)
        self.assertEqual(current.settings_version_id,new.pk)
        self.assertEqual(current.class_snapshot["interest_bps"],1500)
        self.assertEqual(self.membership.rating_history.count(),2)
        history.refresh_from_db()
        self.assertEqual(history.rules_snapshot,old_rules)
        self.assertEqual(history.class_snapshot["interest_bps"],1000)
        with self.assertRaises(ValidationError): history.save()
        with self.assertRaises(ValidationError): history.delete()
    def test_loan_preview_uses_approved_savings_and_rating_limits(self):
        self.approved(date(2025,11,1),0)
        terms=loan_terms(self.membership.pk)
        self.assertTrue(terms["eligible"])
        self.assertEqual(terms["approved_savings"],10000)
        self.assertEqual(terms["max_amount"],30000)
        self.assertEqual(terms["interest_bps"],500)
        self.assertFalse(terms["loan_data_available"])
        self.approved(date(2025,10,1),2)
        terms=loan_terms(self.membership.pk)
        self.assertEqual(terms["max_amount"],40000)
        self.assertEqual(terms["interest_bps"],1000)
        self.approved(date(2025,9,1),50)
        self.assertFalse(loan_terms(self.membership.pk)["eligible"])
        self.assertFalse(loan_terms(self.second.pk)["eligible"])
    def test_default_and_outstanding_loan_rules_use_integration_metrics(self):
        self.approved(date(2025,11,1),0)
        with patch("ratings.services.loan_metrics",return_value={"defaults":1,"outstanding_loans":1,"loan_data_available":True}):
            self.assertEqual(evaluate_membership(self.membership.pk).code,"E")
        with patch("ratings.services.loan_metrics",return_value={"defaults":0,"outstanding_loans":1,"loan_data_available":True}):
            self.assertEqual(evaluate_membership(self.membership.pk).code,"B")
    def test_removed_and_archived_memberships_cannot_borrow(self):
        self.approved(date(2025,11,1),0)
        self.assertTrue(loan_terms(self.membership.pk)["eligible"])
        self.membership.is_active=False
        self.membership.save()
        self.assertFalse(loan_terms(self.membership.pk)["eligible"])
        self.group.is_archived=True
        self.group.save()
        old_count=self.membership.rating_history.count()
        self.assertFalse(loan_terms(self.membership.pk)["eligible"])
        self.assertEqual(evaluate_group(self.group.pk),0)
        self.assertEqual(self.membership.rating_history.count(),old_count)
    def test_rating_view_is_owner_or_group_reviewer_only_and_uncached(self):
        url=reverse("rating_detail",args=[self.membership.pk])
        self.assertEqual(self.client.get(url).status_code,302)
        for user in [self.member,self.treasurer,self.admin]:
            self.client.force_login(user)
            response=self.client.get(url)
            self.assertEqual(response.status_code,200)
            self.assertContains(response,"Rating history")
            self.assertIn("no-store",response["Cache-Control"])
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(url).status_code,404)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("rating_detail",args=[self.reviewer.pk])).status_code,404)
        self.client.force_login(self.treasurer)
        self.assertEqual(self.client.get(reverse("rating_detail",args=[self.second.pk])).status_code,404)
    def test_commands_refresh_rules_without_unpaid_contributions(self):
        self.approved(date(2025,11,1),0)
        output=StringIO()
        call_command("recalculate_ratings",stdout=output)
        first_count=RatingHistory.objects.count()
        call_command("recalculate_ratings",stdout=output)
        self.assertEqual(RatingHistory.objects.count(),first_count)
        classes=deepcopy(self.classes)
        classes[0]["code"]="A+"
        GroupSettings.objects.create(group=self.group,effective_from=date(2025,12,11),rating_classes=classes,created_by=self.admin)
        with patch("django.utils.timezone.localdate",return_value=date(2025,12,11)):
            call_command("recalculate_contributions",stdout=output)
        self.assertEqual(MemberRating.objects.get(membership=self.membership).code,"A+")
        self.assertIn("membership ratings",output.getvalue())
    def test_settings_validate_optional_outstanding_rule_and_code_length(self):
        data={"contribution_amount":10000,"due_day":5,"effective_from":"2025-12-11","lateness_tiers":"[]","rating_classes":json.dumps(self.classes)}
        self.assertTrue(SettingsForm(data=data).is_valid())
        for value in [-1,True,1.5]:
            classes=deepcopy(self.classes)
            classes[0]["max_outstanding_loans"]=value
            data["rating_classes"]=json.dumps(classes)
            self.assertFalse(SettingsForm(data=data).is_valid())
        classes=deepcopy(self.classes)
        classes[-1]["max_outstanding_loans"]=0
        data["rating_classes"]=json.dumps(classes)
        self.assertFalse(SettingsForm(data=data).is_valid())
        classes=deepcopy(self.classes)
        classes[0]["code"]="X"*101
        data["rating_classes"]=json.dumps(classes)
        self.assertFalse(SettingsForm(data=data).is_valid())
    def test_rating_change_rolls_back_when_audit_cannot_be_saved(self):
        with patch("ratings.services.AuditLog.objects.create",side_effect=RuntimeError("Audit unavailable")):
            with self.assertRaises(RuntimeError): evaluate_membership(self.membership.pk)
        self.assertFalse(MemberRating.objects.filter(membership=self.membership).exists())
        self.assertFalse(self.membership.rating_history.exists())
    def test_rule_order_is_admin_defined_and_thresholds_are_inclusive(self):
        metrics={"max_days_late":10,"late_payments":2,"defaults":0,"outstanding_loans":0}
        self.assertEqual(choose_class(self.classes,metrics)["code"],"B")
        metrics["max_days_late"]=11
        self.assertEqual(choose_class(self.classes,metrics)["code"],"E")

    def test_rating_endpoint_rejects_manual_overrides_and_revoked_reviewers(self):
        url=reverse("rating_detail",args=[self.membership.pk])
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(url,{"code":"A","loan_eligible":True}).status_code,405)
        self.client.force_login(self.treasurer)
        self.reviewer.is_active=False
        self.reviewer.save()
        self.assertEqual(self.client.get(url).status_code,404)
    def test_group_page_shows_own_current_rating(self):
        self.client.force_login(self.member)
        response=self.client.get(reverse("group_detail",args=[self.group.pk]))
        self.assertContains(response,"Your rating: B")
        self.assertContains(response,reverse("rating_detail",args=[self.membership.pk]))
