from .models import Membership

def can_review_group(user, group):
    """Group-scoped review permission for contribution views in Phase 3."""
    return bool(user.is_authenticated and user.is_active and not group.is_archived and (user.is_system_admin or Membership.objects.filter(group=group, user=user, is_active=True, role=Membership.Role.TREASURER).exists()))
