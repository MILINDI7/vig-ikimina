import json
import tempfile
from datetime import date, datetime
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch
from PIL import Image, PngImagePlugin
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone
from accounts.models import User, AuditLog
from groups.models import Group, Membership, GroupSettings
from groups.forms import DEFAULT_RATINGS
from .models import Contribution, ContributionPeriod, ProofSubmission, Penalty, Notification
from .forms import ProofForm, MAX_PROOF_BYTES
from .services import ensure_period, recalculate, submit_proof, review_proof

@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ContributionTests(TestCase):
    def setUp(self):
        clock = patch("django.utils.timezone.localdate", return_value=date(2025, 12, 10))
        clock.start()
        self.addCleanup(clock.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        storage = override_settings(STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage", "OPTIONS": {"location": self.directory.name}}, "proofs": {"BACKEND": "django.core.files.storage.FileSystemStorage", "OPTIONS": {"location": self.directory.name}}, "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
        storage.enable()
        self.addCleanup(storage.disable)
        self.admin = User.objects.create_user(username="admin", role="admin", must_change_password=False)
        self.owner = User.objects.create_user(username="owner", must_change_password=False)
        self.treasurer = User.objects.create_user(username="treasurer", role="treasurer", must_change_password=False)
        self.outsider = User.objects.create_user(username="outsider", role="treasurer", must_change_password=False)
        self.group = Group.objects.create(name="Savings One")
        self.other_group = Group.objects.create(name="Savings Two")
        self.rules = [{"days_late": 1, "flat_fee": 500, "percentage_bps": 0}, {"days_late": 5, "flat_fee": 0, "percentage_bps": 125}, {"days_late": 10, "flat_fee": 1000, "percentage_bps": 0}]
        self.settings = GroupSettings.objects.create(group=self.group, effective_from=date(2025,11,1), contribution_amount=10001, due_day=5, lateness_tiers=self.rules, rating_classes=DEFAULT_RATINGS, created_by=self.admin)
        GroupSettings.objects.create(group=self.other_group, effective_from=date(2025,11,1), rating_classes=DEFAULT_RATINGS, created_by=self.admin)
        self.membership = Membership.objects.create(user=self.owner, group=self.group)
        self.reviewer_membership = Membership.objects.create(user=self.treasurer, group=self.group, role="treasurer")
        self.other_membership = Membership.objects.create(user=self.outsider, group=self.other_group, role="treasurer")
        Membership.objects.filter(pk__in=[self.membership.pk,self.reviewer_membership.pk,self.other_membership.pk]).update(joined_at=timezone.make_aware(datetime(2025,11,1)))
        self.period = ensure_period(self.group.pk, date(2025,11,1))
        self.contribution = Contribution.objects.get(period=self.period, membership=self.membership)
    def upload_file(self, fmt="PNG", name="proof.png", metadata=False):
        output = BytesIO()
        image = Image.new("RGB", (20,20), "green")
        if metadata and fmt == "PNG":
            info = PngImagePlugin.PngInfo()
            info.add_text("PrivateData", "secret-location")
            image.save(output, format=fmt, pnginfo=info)
        else: image.save(output, format=fmt)
        return SimpleUploadedFile(name, output.getvalue(), content_type="image/png")
    def image(self):
        form = ProofForm(files={"image": self.upload_file()})
        self.assertTrue(form.is_valid(), form.errors)
        return form.cleaned_data["image"]
    def submit_on(self, day):
        with patch("django.utils.timezone.localdate", return_value=day):
            return submit_proof(self.contribution.pk, self.owner, self.image())
    def test_next_month_due_date_and_inclusive_boundary(self):
        self.assertEqual(self.period.due_date, date(2025,12,5))
        self.assertEqual(recalculate(self.contribution.pk, date(2025,12,5)).days_late_on(date(2025,12,5)),0)
        self.assertEqual(Penalty.objects.count(),0)
        row = recalculate(self.contribution.pk,date(2025,12,6))
        self.assertEqual(row.days_late_on(date(2025,12,6)),1)
        self.assertEqual(row.penalty_total,500)
        december = ensure_period(self.group.pk, date(2025,12,1))
        self.assertEqual(december.due_date,date(2026,1,5))
    def test_pending_is_never_late_and_does_not_accrue(self):
        self.submit_on(date(2025,12,4))
        row = recalculate(self.contribution.pk,date(2025,12,30))
        self.assertEqual(row.status,Contribution.Status.PENDING)
        self.assertEqual(row.days_late_on(date(2025,12,30)),0)
        self.assertEqual(row.penalty_total,0)
        self.assertFalse(Notification.objects.filter(event_key=f"late:{row.pk}").exists())
    def test_decline_restores_original_lateness_and_notifies_reason(self):
        proof = self.submit_on(date(2025,12,4))
        row = review_proof(self.contribution.pk,self.treasurer,"declined","Unreadable transaction reference",proof.pk)
        self.assertEqual(row.days_late_on(date(2025,12,10)),5)
        self.assertEqual(row.penalty_total,626)
        notice = Notification.objects.get(event_key=f"review:{proof.pk}")
        self.assertIn("Unreadable transaction reference",notice.message)
        self.assertIn("2025-12-05",notice.message)
        self.assertTrue(AuditLog.objects.filter(action="proof_declined").exists())
    def test_late_upload_freezes_existing_penalties_and_approval_stops_new(self):
        proof = self.submit_on(date(2025,12,7))
        self.assertEqual(self.contribution.penalty_total,500)
        self.assertEqual(recalculate(self.contribution.pk,date(2025,12,20)).penalty_total,500)
        with patch("django.utils.timezone.localdate",return_value=date(2025,12,20)):
            row = review_proof(self.contribution.pk,self.treasurer,"approved","",proof.pk)
        self.assertEqual(row.payment_late_days,2)
        self.assertIsNotNone(row.approved_at)
        self.assertEqual(recalculate(row.pk,date(2026,1,20)).penalty_total,500)
        self.assertEqual(row.amount_due,500)
    def test_penalty_idempotence_rounding_and_audit(self):
        for _ in range(3): recalculate(self.contribution.pk,date(2025,12,10))
        self.assertEqual(self.contribution.penalties.count(),2)
        fee = self.contribution.penalties.get(threshold_days=5)
        self.assertEqual(fee.amount,126)
        self.assertEqual(fee.calculation["principal_rwf"],10001)
        self.assertEqual(fee.effective_date,date(2025,12,10))
        self.assertEqual(AuditLog.objects.filter(action="penalty_assessed").count(),2)
        self.assertEqual(Notification.objects.filter(event_key=f"late:{self.contribution.pk}").count(),1)
    def test_period_snapshot_survives_new_settings(self):
        GroupSettings.objects.create(group=self.group,effective_from=date(2025,12,1),contribution_amount=25000,due_day=9,lateness_tiers=[],rating_classes=DEFAULT_RATINGS,created_by=self.admin)
        again = ensure_period(self.group.pk,date(2025,11,1))
        self.assertEqual(again.pk,self.period.pk)
        self.assertEqual(again.amount,10001)
        self.assertEqual(again.due_date,date(2025,12,5))
        self.assertEqual(recalculate(self.contribution.pk,date(2025,12,10)).penalty_total,626)
        december=ensure_period(self.group.pk,date(2025,12,1))
        self.assertEqual(december.amount,25000)
        self.assertEqual(december.due_date,date(2026,1,9))
    def test_review_permissions_and_no_self_approval(self):
        proof=self.submit_on(date(2025,12,4))
        for actor in [self.owner,self.outsider]:
            with self.assertRaises(PermissionDenied): review_proof(self.contribution.pk,actor,"approved","",proof.pk)
        self.reviewer_membership.is_active=False
        self.reviewer_membership.save()
        with self.assertRaises(PermissionDenied): review_proof(self.contribution.pk,self.treasurer,"approved","",proof.pk)
        self.owner.role="admin"
        self.owner.save()
        with self.assertRaises(PermissionDenied): review_proof(self.contribution.pk,self.owner,"approved","",proof.pk)
        self.assertEqual(review_proof(self.contribution.pk,self.admin,"approved","",proof.pk).status,"approved")
    def test_blank_decline_duplicate_review_and_stale_form(self):
        first=self.submit_on(date(2025,12,4))
        with self.assertRaises(ValidationError): review_proof(self.contribution.pk,self.treasurer,"declined","  ",first.pk)
        review_proof(self.contribution.pk,self.treasurer,"declined","Wrong amount",first.pk)
        second=self.submit_on(date(2025,12,10))
        with self.assertRaises(ValidationError): review_proof(self.contribution.pk,self.treasurer,"approved","",first.pk)
        review_proof(self.contribution.pk,self.treasurer,"approved","",second.pk)
        with self.assertRaises(ValidationError): review_proof(self.contribution.pk,self.treasurer,"approved","",second.pk)
        self.assertEqual(self.contribution.submissions.count(),2)
        self.assertEqual(Notification.objects.filter(event_key__startswith="review:").count(),2)
    def test_pending_and_approved_cannot_be_replaced(self):
        proof=self.submit_on(date(2025,12,4))
        with self.assertRaises(ValidationError): submit_proof(self.contribution.pk,self.owner,self.image())
        review_proof(self.contribution.pk,self.treasurer,"approved","",proof.pk)
        with self.assertRaises(ValidationError): submit_proof(self.contribution.pk,self.owner,self.image())
    def test_image_validation_and_metadata_removal(self):
        form=ProofForm(files={"image":self.upload_file(metadata=True)})
        self.assertTrue(form.is_valid(),form.errors)
        output=form.cleaned_data["image"].read()
        self.assertNotIn(b"secret-location",output)
        with Image.open(BytesIO(output)) as image:
            self.assertEqual(image.format,"JPEG")
            self.assertFalse(image.getexif())
        for upload in [SimpleUploadedFile("proof.jpg",b"not an image"),self.upload_file(fmt="GIF",name="proof.jpg"),self.upload_file(name="proof.svg"),SimpleUploadedFile("proof.png",b"0"*(MAX_PROOF_BYTES+1))]:
            self.assertFalse(ProofForm(files={"image":upload}).is_valid())
        with patch("contributions.forms.MAX_PIXELS",100):
            self.assertFalse(ProofForm(files={"image":self.upload_file()}).is_valid())
        for fmt,name in [("JPEG","proof.jpg"),("WEBP","proof.webp")]:
            self.assertTrue(ProofForm(files={"image":self.upload_file(fmt=fmt,name=name)}).is_valid())
    def test_private_proof_and_upload_http_permissions(self):
        proof=self.submit_on(date(2025,12,4))
        url=reverse("proof_image",args=[proof.pk])
        self.assertEqual(self.client.get(url).status_code,302)
        for user in [self.owner,self.treasurer,self.admin]:
            self.client.force_login(user)
            response=self.client.get(url)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response["Content-Type"],"image/jpeg")
            self.assertIn("no-store",response["Cache-Control"])
            response.close()
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(url).status_code,404)
        self.assertEqual(self.client.post(reverse("proof_upload",args=[self.contribution.pk]),{"image":self.upload_file()}).status_code,404)
        self.assertEqual(self.client.get(reverse("contribution_detail",args=[self.contribution.pk])).status_code,404)
    def test_member_page_shows_only_own_finances(self):
        self.client.force_login(self.owner)
        response=self.client.get(reverse("group_contributions",args=[self.group.pk]))
        self.assertEqual(response.status_code,200)
        self.assertTrue(all(c.membership.user_id==self.owner.pk for c in response.context["rows"]))
        self.assertEqual(response.context["totals"]["overdue"],10001)
        self.assertEqual(response.context["totals"]["penalties"],626)
        self.assertEqual(self.client.get(reverse("group_contributions",args=[self.other_group.pk])).status_code,404)
        self.assertEqual(self.client.get(reverse("review_queue")).status_code,403)
    def test_http_upload_review_queue_and_notifications(self):
        self.client.force_login(self.owner)
        response=self.client.post(reverse("proof_upload",args=[self.contribution.pk]),{"image":self.upload_file()})
        self.assertEqual(response.status_code,302)
        proof=self.contribution.submissions.get()
        self.client.force_login(self.treasurer)
        queue=self.client.get(reverse("review_queue"),{"group":self.group.pk,"month":"2025-11","member":self.membership.pk})
        self.assertEqual(len(queue.context["rows"]),1)
        self.assertContains(self.client.get(reverse("contribution_detail",args=[self.contribution.pk])),"Review this proof")
        self.assertEqual(len(self.client.get(reverse("review_queue"),{"group":self.other_group.pk}).context["rows"]),0)
        url=reverse("contribution_detail",args=[self.contribution.pk])
        self.assertEqual(self.client.post(url,{"submission_id":proof.pk,"decision":"declined","reason":""}).status_code,200)
        self.assertEqual(self.client.post(url,{"submission_id":proof.pk,"decision":"declined","reason":"No payment reference"}).status_code,302)
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("notifications")),"No payment reference")
        notice=Notification.objects.get(event_key=f"review:{proof.pk}")
        self.assertEqual(self.client.get(reverse("notification_read",args=[notice.pk])).status_code,405)
        self.client.post(reverse("notification_read",args=[notice.pk]))
        notice.refresh_from_db()
        self.assertIsNotNone(notice.read_at)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.post(reverse("notification_read",args=[notice.pk])).status_code,404)
    def test_archived_group_readable_but_not_mutable(self):
        proof=self.submit_on(date(2025,12,4))
        self.group.is_archived=True
        self.group.save()
        with self.assertRaises(PermissionDenied): review_proof(self.contribution.pk,self.admin,"declined","Invalid",proof.pk)
        with self.assertRaises(ValidationError): ensure_period(self.group.pk,date(2025,12,1))
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("group_contributions",args=[self.group.pk])).status_code,200)
        self.assertEqual(self.client.get(reverse("proof_upload",args=[self.contribution.pk])).status_code,403)
        self.assertEqual(recalculate(self.contribution.pk,date(2026,1,1)).penalty_total,0)
    def test_daily_command_is_repeatable_and_keeps_pending_not_late(self):
        self.submit_on(date(2025,12,4))
        output=StringIO()
        call_command("recalculate_contributions",stdout=output)
        call_command("recalculate_contributions",stdout=output)
        self.assertEqual(ContributionPeriod.objects.filter(group=self.group,month=date(2025,12,1)).count(),1)
        self.assertEqual(self.contribution.penalties.count(),0)
        self.assertEqual(self.group.contribution_periods.get(month=date(2025,12,1)).contributions.count(),2)
        call_command("recalculate_contributions",month="2025-11",stdout=output)
        self.assertIn("recalculated",output.getvalue())
    def test_on_demand_period_generation_and_first_partial_month(self):
        today=date(2025,12,10)
        group=Group.objects.create(name="Brand new")
        GroupSettings.objects.create(group=group,effective_from=today,rating_classes=DEFAULT_RATINGS,created_by=self.admin)
        membership=Membership.objects.create(group=group,user=self.owner)
        Membership.objects.filter(pk=membership.pk).update(joined_at=timezone.make_aware(datetime(2025,12,10)))
        self.assertEqual(ensure_period(group.pk).contributions.count(),1)
        self.assertEqual(ensure_period(group.pk).contributions.count(),1)
        with self.assertRaises(ValidationError): ensure_period(group.pk,date(2025,11,1))
        with self.assertRaises(ValidationError): ensure_period(group.pk,date(2026,1,1))
    def test_csrf_required_for_proof_upload_and_review(self):
        client=Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post(reverse("proof_upload",args=[self.contribution.pk]),{"image":self.upload_file()}).status_code,403)
        client.force_login(self.treasurer)
        self.assertEqual(client.post(reverse("contribution_detail",args=[self.contribution.pk]),{}).status_code,403)

    def test_approval_of_on_time_pending_proof_does_not_charge_review_delay(self):
        proof=self.submit_on(date(2025,12,4))
        with patch("django.utils.timezone.localdate",return_value=date(2025,12,30)):
            row=review_proof(self.contribution.pk,self.treasurer,"approved","",proof.pk)
        self.assertEqual(row.penalty_total,0)
        self.assertEqual(row.payment_late_days,0)
        self.assertEqual(row.amount_due,0)
    def test_failed_upload_transaction_removes_new_blob_and_rolls_back(self):
        with patch("django.utils.timezone.localdate",return_value=date(2025,12,4)):
            with patch("contributions.services.audit",side_effect=RuntimeError("Audit write failed")):
                with self.assertRaises(RuntimeError):
                    submit_proof(self.contribution.pk,self.owner,self.image())
        self.contribution.refresh_from_db()
        self.assertEqual(self.contribution.status,"not_paid")
        self.assertFalse(self.contribution.submissions.exists())
        self.assertFalse(Notification.objects.filter(contribution=self.contribution).exists())
        self.assertEqual(list(Path(self.directory.name).rglob("*.jpg")),[])
