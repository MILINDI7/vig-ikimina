from datetime import timedelta
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST
from accounts.models import AuditLog
from .models import Group, Membership
from .forms import GroupForm, MembershipForm, SettingsForm, DEFAULT_RATINGS

def require_admin(user):
    if not user.is_system_admin: raise PermissionDenied

def visible_groups(user):
    if user.is_system_admin: return Group.objects.all()
    return Group.objects.filter(memberships__user=user, memberships__is_active=True).distinct()

def log(request, group, action, **details):
    AuditLog.objects.create(actor=request.user, action=action, details={"group_id": group.pk, **details})

@login_required
def group_list(request):
    return render(request, "groups/list.html", {"groups": visible_groups(request.user)})

@login_required
@transaction.atomic
def group_create(request):
    require_admin(request.user)
    form = GroupForm(request.POST if request.method == "POST" else None)
    settings_form = SettingsForm(request.POST if request.method == "POST" else None, new_group=True, initial={"contribution_amount": 10000, "due_day": 5, "effective_from": timezone.localdate(), "lateness_tiers": [], "rating_classes": DEFAULT_RATINGS})
    if request.method == "POST" and form.is_valid() and settings_form.is_valid():
        group = form.save()
        version = settings_form.save(commit=False)
        version.group, version.created_by = group, request.user
        version.save()
        log(request, group, "group_created", settings_version_id=version.pk)
        return redirect("group_detail", pk=group.pk)
    return render(request, "groups/form.html", {"form": form, "settings_form": settings_form, "title": "Create a savings group"})

@login_required
def group_detail(request, pk):
    group = get_object_or_404(visible_groups(request.user), pk=pk)
    membership = group.memberships.filter(user=request.user, is_active=True).first()
    return render(request, "groups/detail.html", {"group": group, "settings": group.settings_on(), "membership": membership, "memberships": group.memberships.select_related("user").all() if request.user.is_system_admin else None, "versions": group.settings_versions.all() if request.user.is_system_admin else None})

@login_required
@transaction.atomic
def group_edit(request, pk):
    require_admin(request.user)
    group = get_object_or_404(Group.objects.select_for_update(), pk=pk)
    if group.is_archived: raise PermissionDenied
    form = GroupForm(request.POST if request.method == "POST" else None, instance=group)
    if request.method == "POST" and form.is_valid():
        form.save()
        log(request, group, "group_updated", name=group.name, description=group.description)
        return redirect("group_detail", pk=pk)
    return render(request, "groups/form.html", {"form": form, "title": "Edit group"})

@login_required
@require_POST
@transaction.atomic
def group_archive(request, pk):
    require_admin(request.user)
    group = get_object_or_404(Group.objects.select_for_update(), pk=pk)
    group.is_archived = not group.is_archived
    group.save(update_fields=["is_archived"])
    log(request, group, "group_archived" if group.is_archived else "group_restored")
    return redirect("group_detail", pk=pk)

@login_required
@transaction.atomic
def membership_edit(request, pk):
    require_admin(request.user)
    group = get_object_or_404(Group.objects.select_for_update(), pk=pk)
    if group.is_archived: raise PermissionDenied
    form = MembershipForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        membership, created = Membership.objects.update_or_create(group=group, user=form.cleaned_data["user"], defaults={"role": form.cleaned_data["role"], "is_active": True})
        log(request, group, "membership_saved", user_id=membership.user_id, role=membership.role, created=created)
        return redirect("group_detail", pk=pk)
    return render(request, "groups/form.html", {"form": form, "title": "Add member or change group role", "description": "Selecting an existing member updates their role or restores their membership. Treasurer access applies only to this group."})

@login_required
@require_POST
@transaction.atomic
def membership_remove(request, pk, membership_pk):
    require_admin(request.user)
    group = get_object_or_404(Group.objects.select_for_update(), pk=pk)
    if group.is_archived: raise PermissionDenied
    membership = get_object_or_404(Membership, pk=membership_pk, group=group)
    membership.is_active = False
    membership.save(update_fields=["is_active"])
    log(request, group, "membership_removed", user_id=membership.user_id)
    return redirect("group_detail", pk=pk)

@login_required
@transaction.atomic
def settings_create(request, pk):
    require_admin(request.user)
    group = get_object_or_404(Group.objects.select_for_update(), pk=pk)
    if group.is_archived: raise PermissionDenied
    latest = group.settings_versions.first()
    initial = {key: getattr(latest, key) for key in ["contribution_amount", "due_day", "lateness_tiers", "rating_classes"]} if latest else {}
    initial["effective_from"] = timezone.localdate() + timedelta(days=1)
    form = SettingsForm(request.POST if request.method == "POST" else None, initial=initial)
    if request.method == "POST" and form.is_valid():
        version = form.save(commit=False)
        version.group, version.created_by = group, request.user
        version.save()
        log(request, group, "settings_version_created", settings_version_id=version.pk, effective_from=version.effective_from.isoformat())
        return redirect("group_detail", pk=pk)
    return render(request, "groups/form.html", {"form": form, "title": "Schedule new settings", "description": "Creates a retained version effective on a future date. Earlier records keep their original version. If dates match, the newest version takes precedence."})
