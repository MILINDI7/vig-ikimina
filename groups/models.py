from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.utils import timezone

class Group(models.Model):
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    is_archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["name", "pk"]
    def __str__(self): return self.name
    def settings_on(self, day=None):
        return self.settings_versions.filter(effective_from__lte=day or timezone.localdate()).order_by("-effective_from", "-pk").first()

class Membership(models.Model):
    class Role(models.TextChoices):
        MEMBER = "member", "Member"
        TREASURER = "treasurer", "Treasurer"
    group = models.ForeignKey(Group, on_delete=models.PROTECT, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="memberships")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)
    is_active = models.BooleanField(default=True)
    joined_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["group", "user"], name="unique_group_member")]

class GroupSettings(models.Model):
    group = models.ForeignKey(Group, on_delete=models.PROTECT, related_name="settings_versions")
    contribution_amount = models.PositiveBigIntegerField(default=10000, validators=[MinValueValidator(1)])
    due_day = models.PositiveSmallIntegerField(default=5, validators=[MinValueValidator(1), MaxValueValidator(28)])
    effective_from = models.DateField(default=timezone.localdate)
    lateness_tiers = models.JSONField(default=list, blank=True)
    rating_classes = models.JSONField(default=list)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["-effective_from", "-pk"]
    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Settings versions cannot be edited. Create a new version.")
        return super().save(*args, **kwargs)
    def delete(self, *args, **kwargs):
        raise ValidationError("Settings versions must be retained.")
