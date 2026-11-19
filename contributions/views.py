from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.decorators.cache import never_cache
from groups.models import Group, Membership
from .forms import ProofForm, ReviewForm, PeriodForm
from .models import Contribution, ProofSubmission, Notification
from .services import ensure_period, recalculate, submit_proof, review_proof

def review_groups(user, include_archived=False):
    groups = Group.objects.all() if user.is_system_admin else Group.objects.filter(memberships__user=user, memberships__is_active=True, memberships__role=Membership.Role.TREASURER)
    return groups if include_archived else groups.filter(is_archived=False)

def visible_contributions(user):
    from django.db.models import Q
    return Contribution.objects.filter(Q(membership__user=user) | Q(period__group__in=review_groups(user, include_archived=True))).select_related("period__group", "membership__user").distinct()

def summary(rows):
    return {"saved": sum(c.period.amount for c in rows if c.status == Contribution.Status.APPROVED), "pending": sum(c.period.amount for c in rows if c.status == Contribution.Status.PENDING), "overdue": sum(c.period.amount for c in rows if c.is_late), "penalties": sum(c.penalty_total for c in rows)}

@login_required
@never_cache
def group_contributions(request, pk):
    group = get_object_or_404(Group, pk=pk)
    reviewer = review_groups(request.user, include_archived=True).filter(pk=pk).exists()
    membership = Membership.objects.filter(group=group, user=request.user).first()
    if not reviewer and not membership: raise Http404
    if not group.is_archived:
        ensure_period(group.pk, actor=request.user)
    query = visible_contributions(request.user).filter(period__group=group).order_by("-period__month", "membership__user__username")
    # Recalculate on read as well as in the daily command, so a missed job does not hide penalties.
    for contribution_id in query.values_list("pk", flat=True): recalculate(contribution_id)
    rows = list(query.prefetch_related("penalties"))
    return render(request, "contributions/list.html", {"group": group, "rows": rows, "totals": summary(rows), "reviewer": reviewer, "has_group_access": reviewer or bool(membership and membership.is_active)})

@login_required
@never_cache
def upload(request, pk):
    contribution = get_object_or_404(Contribution.objects.select_related("period__group", "membership"), pk=pk, membership__user=request.user)
    if contribution.period.group.is_archived: raise PermissionDenied
    form = ProofForm(request.POST if request.method == "POST" else None, request.FILES if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            submit_proof(pk, request.user, form.cleaned_data["image"])
            return redirect("contribution_detail", pk=pk)
        except ValidationError as error: form.add_error(None, error)
    return render(request, "contributions/upload.html", {"form": form, "contribution": contribution})

@login_required
@never_cache
def detail(request, pk):
    contribution = get_object_or_404(visible_contributions(request.user), pk=pk)
    contribution = recalculate(pk)
    is_owner = contribution.membership.user_id == request.user.pk
    can_review = not is_owner and review_groups(request.user).filter(pk=contribution.period.group_id).exists()
    submission = contribution.submissions.filter(status=Contribution.Status.PENDING).first()
    form = ReviewForm(request.POST if request.method == "POST" else None, initial={"submission_id": submission.pk if submission else None})
    if request.method == "POST":
        if not can_review: raise PermissionDenied
        if form.is_valid():
            try:
                review_proof(pk, request.user, **form.cleaned_data)
                return redirect("contribution_detail", pk=pk)
            except ValidationError as error: form.add_error(None, error)
    response = render(request, "contributions/detail.html", {"contribution": contribution, "submissions": contribution.submissions.all(), "can_review": can_review, "form": form, "pending_submission": submission, "is_owner": is_owner})
    response["Cache-Control"] = "no-store"
    return response

@login_required
@never_cache
def proof_image(request, pk):
    submission = get_object_or_404(ProofSubmission.objects.select_related("contribution__period", "contribution__membership"), pk=pk)
    if not visible_contributions(request.user).filter(pk=submission.contribution_id).exists(): raise Http404
    try:
        response = FileResponse(submission.image.open("rb"), content_type="image/jpeg")
    except (FileNotFoundError, OSError): raise Http404("Proof image is unavailable.")
    response["Content-Disposition"] = 'inline; filename="contribution-proof.jpg"'
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response

class QueueFilter(forms.Form):
    group = forms.ModelChoiceField(queryset=Group.objects.none(), required=False)
    month = forms.DateField(required=False, input_formats=["%Y-%m"], widget=forms.DateInput(format="%Y-%m", attrs={"type": "month"}))
    member = forms.ModelChoiceField(queryset=Membership.objects.none(), required=False)
    def __init__(self, *args, groups, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["group"].queryset = groups
        self.fields["member"].queryset = Membership.objects.filter(group__in=groups).select_related("user", "group")
        self.fields["member"].label_from_instance = lambda membership: f"{membership.user.username} ({membership.group.name})"

@login_required
@never_cache
def review_queue(request):
    groups = review_groups(request.user)
    if not request.user.is_system_admin and not groups.exists(): raise PermissionDenied
    form = QueueFilter(request.GET or None, groups=groups)
    rows = visible_contributions(request.user).filter(period__group__in=groups, status=Contribution.Status.PENDING).exclude(membership__user=request.user)
    if form.is_bound:
        if form.is_valid():
            data = form.cleaned_data
            if data.get("group"): rows = rows.filter(period__group=data["group"])
            if data.get("month"): rows = rows.filter(period__month=data["month"].replace(day=1))
            if data.get("member"): rows = rows.filter(membership=data["member"])
        else: rows = rows.none()
    return render(request, "contributions/queue.html", {"form": form, "rows": rows.order_by("period__month", "pk")})

@login_required
@never_cache
def open_period(request, pk):
    if not request.user.is_system_admin: raise PermissionDenied
    group = get_object_or_404(Group, pk=pk, is_archived=False)
    form = PeriodForm(request.POST if request.method == "POST" else None, initial={"month": timezone.localdate().replace(day=1)})
    if request.method == "POST" and form.is_valid():
        try:
            ensure_period(group.pk, form.cleaned_data["month"], actor=request.user)
            return redirect("group_contributions", pk=pk)
        except ValidationError as error: form.add_error(None, error)
    return render(request, "groups/form.html", {"form": form, "title": "Open contribution month", "description": "Uses memberships that are active now and joined by that month's end. Historical removals and rejoin intervals cannot be reconstructed automatically; check membership eligibility before backfilling."})

@login_required
@never_cache
def notifications(request):
    return render(request, "contributions/notifications.html", {"notifications": Notification.objects.filter(recipient=request.user)})

@login_required
@never_cache
@require_POST
def mark_read(request, pk):
    notification = get_object_or_404(Notification, pk=pk, recipient=request.user)
    Notification.objects.filter(pk=notification.pk, read_at__isnull=True).update(read_at=timezone.now())
    return redirect("notifications")
