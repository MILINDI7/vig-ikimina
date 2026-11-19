from django.test import TestCase, Client
from django.urls import reverse
from .models import User, AuditLog

class AccessTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="admin", password="OriginalPass!928", role="admin", must_change_password=False)
        self.member = User.objects.create_user(username="member", password="OriginalPass!928", must_change_password=False)
        self.top = User.objects.create_superuser(username="root", password="OriginalPass!928", must_change_password=False)
    def test_anonymous_requires_login(self):
        self.assertEqual(self.client.get(reverse("account_create")).status_code, 302)
    def test_member_cannot_manage_accounts(self):
        self.client.force_login(self.member)
        for url in [reverse("account_create"), reverse("role_change", args=[self.admin.pk])]:
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self.client.post(url, {"role": "super_admin"}).status_code, 403)
        self.assertNotContains(self.client.get(reverse("dashboard")), "Manage access")
    def test_admin_cannot_promote_to_admin(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("role_change", args=[self.member.pk]), {"role": "admin"})
        self.assertEqual(response.status_code, 200)
        self.member.refresh_from_db()
        self.assertEqual(self.member.role, "member")
        self.assertEqual(self.client.post(reverse("role_change", args=[self.top.pk]), {"role": "member"}).status_code, 403)
    def test_superadmin_can_promote_and_audits(self):
        self.client.force_login(self.top)
        self.assertEqual(self.client.post(reverse("role_change", args=[self.member.pk]), {"role": "admin"}).status_code, 302)
        self.member.refresh_from_db()
        self.assertEqual(self.member.role, "admin")
        self.assertTrue(AuditLog.objects.filter(action="role_changed", actor=self.top, target=self.member).exists())
    def test_create_and_password_gate(self):
        self.client.force_login(self.admin)
        data = {"username": "new", "first_name": "New Member", "email": "new@example.com", "phone": "0781234567", "role": "member"}
        response = self.client.post(reverse("account_create"), data)
        user = User.objects.get(username="new")
        self.assertEqual(response["Cache-Control"], "no-store")
        password = response.context["temporary_password"]
        self.assertTrue(user.check_password(password))
        self.assertNotEqual(user.password, password)
        self.client.force_login(user)
        self.assertRedirects(self.client.get(reverse("dashboard")), reverse("password_change"))
        self.assertRedirects(self.client.get("/admin/"), reverse("password_change"))
        response = self.client.post(reverse("password_change"), {"old_password": password, "new_password1": "BrandNewSecret!7294", "new_password2": "BrandNewSecret!7294"})
        self.assertEqual(response.status_code, 302)
        user.refresh_from_db()
        self.assertFalse(user.must_change_password)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
    def test_admin_cannot_create_privileged_account(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("account_create"), {"username": "evil", "first_name": "Name", "email": "test@example.com", "phone": "123", "role": "super_admin"})
        self.assertFalse(User.objects.filter(username="evil").exists())
    def test_login_throttle(self):
        for i in range(10):
            self.assertEqual(self.client.post(reverse("login"), {"username": "wrong", "password": "bad"}).status_code, 200)
        self.assertEqual(self.client.post(reverse("login"), {"username": "admin", "password": "OriginalPass!928"}).status_code, 429)
    def test_csrf_is_enforced(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post(reverse("account_create"), {}).status_code, 403)
    def test_treasurer_cannot_manage_accounts(self):
        self.member.role = "treasurer"
        self.member.save()
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("account_create")).status_code, 403)
