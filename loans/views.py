from datetime import timedelta
from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe
from groups.models import Membership, Group
from ratings.services import loan_terms, evaluate_membership
from .models import LoanRequest, Loan
from .forms import RequestForm, DecisionForm, RepaymentForm
from .services import request_loan, decide_request, record_repayment

def visible_requests(user):
    query=LoanRequest.objects.select_related("membership__user","membership__group","loan")
    return query if user.is_system_admin else query.filter(membership__user=user)

class FilterForm(forms.Form):
    group=forms.ModelChoiceField(queryset=Group.objects.none(),required=False)
    status=forms.ChoiceField(choices=[("","All statuses"),*LoanRequest.Status.choices],required=False)
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields["group"].queryset=Group.objects.all() if user.is_system_admin else Group.objects.filter(memberships__user=user).distinct()

@login_required
@never_cache
@require_safe
def loan_list(request):
    query=visible_requests(request.user)
    form=FilterForm(request.GET or None,user=request.user)
    if form.is_bound:
        if form.is_valid():
            if form.cleaned_data["group"]: query=query.filter(membership__group=form.cleaned_data["group"])
            if form.cleaned_data["status"]: query=query.filter(status=form.cleaned_data["status"])
        else: query=query.none()
    memberships=Membership.objects.filter(user=request.user,is_active=True,group__is_archived=False).select_related("group")
    return render(request,"loans/list.html",{"requests":query,"memberships":memberships,"form":form})

@login_required
@never_cache
def create(request,pk):
    membership=get_object_or_404(Membership.objects.select_related("group"),pk=pk,user=request.user,is_active=True,group__is_archived=False)
    terms=loan_terms(pk)
    form=RequestForm(request.POST if request.method=="POST" else None)
    if request.method=="POST" and form.is_valid():
        try:
            item=request_loan(pk,request.user,**form.cleaned_data)
            return redirect("loan_request_detail",pk=item.pk)
        except ValidationError as error: form.add_error(None,error)
    return render(request,"loans/create.html",{"membership":membership,"terms":terms,"form":form})

@login_required
@never_cache
def detail(request,pk):
    item=get_object_or_404(visible_requests(request.user),pk=pk)
    evaluate_membership(item.membership_id)
    loan=Loan.objects.filter(request=item).first()
    can_manage=request.user.is_system_admin and item.membership.user_id!=request.user.pk and not item.membership.group.is_archived
    form=DecisionForm(request.POST if request.method=="POST" else None,initial={"repayment_months":item.repayment_months,"first_due_date":timezone.localdate()+timedelta(days=30)})
    if request.method=="POST":
        if not can_manage: raise PermissionDenied
        if form.is_valid():
            try:
                decide_request(pk,request.user,**form.cleaned_data)
                return redirect("loan_request_detail",pk=pk)
            except ValidationError as error: form.add_error(None,error)
    return render(request,"loans/detail.html",{"item":item,"loan":loan,"form":form,"can_manage":can_manage,"current_terms":loan_terms(item.membership_id) if can_manage and item.status=="pending" else None})

@login_required
@never_cache
def repayment(request,pk):
    if not request.user.is_system_admin: raise PermissionDenied
    loan=get_object_or_404(Loan.objects.select_related("membership__group","membership__user","request"),pk=pk)
    if loan.membership.user_id==request.user.pk or loan.membership.group.is_archived: raise PermissionDenied
    evaluate_membership(loan.membership_id)
    form=RepaymentForm(request.POST if request.method=="POST" else None)
    if request.method=="POST" and form.is_valid():
        try:
            record_repayment(pk,request.user,**form.cleaned_data)
            return redirect("loan_request_detail",pk=loan.request_id)
        except ValidationError as error: form.add_error(None,error)
    return render(request,"loans/repayment.html",{"loan":loan,"form":form})
