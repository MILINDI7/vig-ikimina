from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

class MemberRating(models.Model):
    membership = models.OneToOneField("groups.Membership", on_delete=models.PROTECT, related_name="current_rating")
    settings_version = models.ForeignKey("groups.GroupSettings", on_delete=models.PROTECT)
    code = models.CharField(max_length=100)
    class_snapshot = models.JSONField()
    metrics = models.JSONField()
    evaluated_on = models.DateField()
    evaluated_at = models.DateTimeField(auto_now=True)

class RatingHistory(models.Model):
    membership = models.ForeignKey("groups.Membership", on_delete=models.PROTECT, related_name="rating_history")
    settings_version = models.ForeignKey("groups.GroupSettings", on_delete=models.PROTECT)
    previous_code = models.CharField(max_length=100, blank=True)
    code = models.CharField(max_length=100)
    class_snapshot = models.JSONField()
    rules_snapshot = models.JSONField()
    metrics = models.JSONField()
    evaluated_on = models.DateField()
    trigger = models.CharField(max_length=80)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["-pk"]
    def save(self, *args, **kwargs):
        if self.pk: raise ValidationError("Rating history is immutable.")
        return super().save(*args, **kwargs)
    def delete(self, *args, **kwargs):
        raise ValidationError("Rating history must be retained.")
