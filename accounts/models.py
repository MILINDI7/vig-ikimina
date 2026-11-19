from django.contrib.auth.models import AbstractUser
from django.db import models
from django.conf import settings
from django.utils import timezone

class User(AbstractUser):
    class Role(models.TextChoices):
        SUPER_ADMIN = "super_admin", "Super Admin"
        ADMIN = "admin", "Admin"
        TREASURER = "treasurer", "Treasurer"
        MEMBER = "member", "Member"
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)
    phone = models.CharField(max_length=30, blank=True)
    national_id = models.CharField(max_length=40, blank=True)
    must_change_password = models.BooleanField(default=True)
    @property
    def is_system_admin(self):
        return self.is_superuser or self.role in (self.Role.SUPER_ADMIN, self.Role.ADMIN)
    @property
    def is_top_admin(self):
        return self.is_superuser or self.role == self.Role.SUPER_ADMIN

class AuditLog(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="actions")
    target = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="account_events")
    action = models.CharField(max_length=80)
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

class LoginAttempt(models.Model):
    key = models.CharField(max_length=64, db_index=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
