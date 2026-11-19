import hashlib, secrets
from datetime import timedelta
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, PasswordChangeView
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse_lazy
from django.utils import timezone
from .forms import AccountForm, RoleForm
from .models import User, AuditLog, LoginAttempt

class RateLimitedLoginView(LoginView):
    template_name = "registration/login.html"
    def post(self, request, *args, **kwargs):
        # IP-wide budget also prevents bypass by rotating usernames. Trust no proxy headers.
        key = hashlib.sha256(request.META.get("REMOTE_ADDR", "unknown").encode()).hexdigest()
        cutoff = timezone.now() - timedelta(minutes=15)
        if LoginAttempt.objects.filter(key=key, created_at__gte=cutoff).count() >= 10:
            return HttpResponse("Too many sign-in attempts. Try again in 15 minutes.", status=429)
        LoginAttempt.objects.create(key=key)
        return super().post(request, *args, **kwargs)

class ChangePasswordView(PasswordChangeView):
    template_name = "registration/password_change_form.html"
    success_url = reverse_lazy("dashboard")
    @transaction.atomic
    def form_valid(self, form):
        response = super().form_valid(form)
        self.request.user.must_change_password = False
        self.request.user.save(update_fields=["must_change_password"])
        AuditLog.objects.create(actor=self.request.user, target=self.request.user, action="password_changed")
        return response

@login_required
def dashboard(request):
    users = User.objects.order_by("first_name", "username") if request.user.is_system_admin else None
    return render(request, "dashboard.html", {"users": users})

@login_required
@transaction.atomic
def create_account(request):
    if not request.user.is_system_admin:
        raise PermissionDenied
    form = AccountForm(request.POST or None, actor=request.user)
    if request.method == "POST" and form.is_valid():
        user = form.save(commit=False)
        password = secrets.token_urlsafe(18)
        user.set_password(password)
        user.must_change_password = True
        user.save()
        AuditLog.objects.create(actor=request.user, target=user, action="account_created", details={"role": user.role})
        response = render(request, "account_created.html", {"member": user, "temporary_password": password})
        response["Cache-Control"] = "no-store"
        return response
    return render(request, "form.html", {"form": form, "title": "Create an account", "description": "Give a member secure access to Savings Group Manager."})

@login_required
@transaction.atomic
def change_role(request, pk):
    if not request.user.is_system_admin:
        raise PermissionDenied
    target = get_object_or_404(User.objects.select_for_update(), pk=pk)
    if target.pk == request.user.pk or (not request.user.is_top_admin and (target.is_system_admin or target.is_superuser)):
        raise PermissionDenied
    form = RoleForm(request.POST or None, actor=request.user, initial={"role": target.role})
    if request.method == "POST" and form.is_valid():
        old = target.role
        target.role = form.cleaned_data["role"]
        target.save(update_fields=["role"])
        AuditLog.objects.create(actor=request.user, target=target, action="role_changed", details={"from": old, "to": target.role})
        return redirect("dashboard")
    return render(request, "form.html", {"form": form, "title": "Change role: " + target.username})
