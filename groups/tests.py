import json
from datetime import timedelta
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.core.exceptions import ValidationError
from accounts.models import User, AuditLog
from .models import Group, Membership, GroupSettings
from .forms import DEFAULT_RATINGS, SettingsForm
from .permissions import can_review_group

@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class GroupTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="admin", role="admin", must_change_password=False)
        self.member = User.objects.create_user(username="member", must_change_password=False)
        self.other = User.objects.create_user(username="other", role="treasurer", must_change_password=False)
        self.group = Group.objects.create(name="Community One")
        self.second = Group.objects.create(name="Private Two")
        self.version = GroupSettings.objects.create(group=self.group, created_by=self.admin, rating_classes=DEFAULT_RATINGS)
        self.membership = Membership.objects.create(group=self.group, user=self.member)
    def data(self, **changes):
        data = {"contribution_amount": 15000, "due_day": 5, "effective_from": (timezone.localdate()+timedelta(days=1)).isoformat(), "lateness_tiers": json.dumps([{"days_late": 1, "flat_fee": 500, "percentage_bps": 0}]), "rating_classes": json.dumps(DEFAULT_RATINGS)}
        data.update(changes)
        return data
    def test_member_only_sees_own_groups_and_no_roster(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("group_list"))
        self.assertContains(response, "Community One")
        self.assertNotContains(response, "Private Two")
        response = self.client.get(reverse("group_detail", args=[self.group.pk]))
        self.assertContains(response, "10000 RWF")
        self.assertIsNone(response.context["memberships"])
        self.assertEqual(self.client.get(reverse("group_detail", args=[self.second.pk])).status_code, 404)
    def test_all_mutations_require_admin(self):
        self.client.force_login(self.member)
        for name, args in [("group_create", []), ("group_edit", [self.group.pk]), ("membership_edit", [self.group.pk]), ("settings_create", [self.group.pk]), ("group_archive", [self.group.pk]), ("membership_remove", [self.group.pk, self.membership.pk])]:
            self.assertEqual(self.client.post(reverse(name, args=args), {}).status_code, 403)
    def test_create_group_and_initial_settings_atomic(self):
        self.client.force_login(self.admin)
        data = self.data(name="New Group", description="Shared savings", effective_from=timezone.localdate().isoformat())
        self.assertEqual(self.client.post(reverse("group_create"), data).status_code, 302)
        group = Group.objects.get(name="New Group")
        self.assertEqual(group.settings_on().contribution_amount, 15000)
        self.assertTrue(AuditLog.objects.filter(action="group_created", details__group_id=group.pk).exists())
        data.update(name="Invalid", due_day=31)
        self.assertEqual(self.client.post(reverse("group_create"), data).status_code, 200)
        self.assertFalse(Group.objects.filter(name="Invalid").exists())
    def test_settings_future_and_past_retained(self):
        self.client.force_login(self.admin)
        url = reverse("settings_create", args=[self.group.pk])
        self.assertEqual(self.client.post(url, self.data()).status_code, 302)
        future = timezone.localdate()+timedelta(days=1)
        self.assertEqual(self.group.settings_on().pk, self.version.pk)
        self.assertEqual(self.group.settings_on(future).contribution_amount, 15000)
        self.version.refresh_from_db()
        self.assertEqual(self.version.contribution_amount, 10000)
        with self.assertRaises(ValidationError): self.version.save()
        with self.assertRaises(ValidationError): self.version.delete()
        self.client.post(url, self.data(effective_from=timezone.localdate().isoformat()))
        self.assertEqual(self.group.settings_versions.count(), 2)
    def test_settings_validation(self):
        for changes in [{"contribution_amount": 0}, {"due_day": 29}, {"lateness_tiers": '[{"days_late": 1, "flat_fee": -1, "percentage_bps": 0}]'}, {"lateness_tiers": '{}'}, {"rating_classes": '[]'}, {"rating_classes": '[{"code": "A"}]'}]:
            self.assertFalse(SettingsForm(data=self.data(**changes)).is_valid(), changes)
        self.assertTrue(SettingsForm(data=self.data()).is_valid())
    def test_treasurer_scope_and_membership_restore(self):
        self.assertFalse(can_review_group(self.other, self.group))
        self.client.force_login(self.admin)
        url = reverse("membership_edit", args=[self.group.pk])
        self.assertEqual(self.client.post(url, {"user": self.other.pk, "role": "treasurer"}).status_code, 302)
        self.assertTrue(can_review_group(self.other, self.group))
        self.assertFalse(can_review_group(self.other, self.second))
        membership = Membership.objects.get(group=self.group, user=self.other)
        self.client.post(reverse("membership_remove", args=[self.group.pk, membership.pk]))
        self.assertFalse(can_review_group(self.other, self.group))
        self.client.post(url, {"user": self.other.pk, "role": "member"})
        self.assertEqual(Membership.objects.filter(group=self.group, user=self.other).count(), 1)
        membership.refresh_from_db()
        self.assertTrue(membership.is_active)
        self.assertEqual(membership.role, "member")
    def test_archive_post_only_readonly_and_restore(self):
        self.client.force_login(self.admin)
        url = reverse("group_archive", args=[self.group.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.post(url)
        self.group.refresh_from_db()
        self.assertTrue(self.group.is_archived)
        self.assertFalse(can_review_group(self.admin, self.group))
        self.assertEqual(self.client.get(reverse("settings_create", args=[self.group.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("group_edit", args=[self.group.pk])).status_code, 403)
        self.client.force_login(self.member)
        self.assertContains(self.client.get(reverse("group_detail", args=[self.group.pk])), "Archived")
        self.client.force_login(self.admin)
        self.client.post(url)
        self.group.refresh_from_db()
        self.assertFalse(self.group.is_archived)
    def test_cross_group_removal_denied(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(reverse("membership_remove", args=[self.second.pk, self.membership.pk])).status_code, 404)
        self.membership.refresh_from_db()
        self.assertTrue(self.membership.is_active)
    def test_removed_member_loses_access(self):
        self.membership.is_active = False
        self.membership.save()
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("group_detail", args=[self.group.pk])).status_code, 404)
