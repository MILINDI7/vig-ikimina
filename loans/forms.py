import uuid
from django import forms
from django.utils import timezone
from .models import MAX_LOAN_AMOUNT

class RequestForm(forms.Form):
    amount = forms.IntegerField(min_value=1, max_value=MAX_LOAN_AMOUNT, label="Amount (RWF)")
    purpose = forms.CharField(max_length=2000, widget=forms.Textarea(attrs={"rows":3}))
    repayment_months = forms.IntegerField(min_value=1, max_value=60)
    submission_token = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)

class DecisionForm(forms.Form):
    decision = forms.ChoiceField(choices=[("approved","Approve"),("rejected","Reject")])
    reason = forms.CharField(required=False, max_length=2000, widget=forms.Textarea(attrs={"rows":3}))
    first_due_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type":"date"}))
    repayment_months = forms.IntegerField(required=False, min_value=1, max_value=60)
    def clean(self):
        data=super().clean()
        if data.get("decision")=="rejected" and not data.get("reason"):
            self.add_error("reason","A rejection reason is required.")
        if data.get("decision")=="approved":
            if data.get("first_due_date") is None or data["first_due_date"]<=timezone.localdate():
                self.add_error("first_due_date","Choose a first due date after today.")
            if not data.get("repayment_months"):
                self.add_error("repayment_months","Choose the number of monthly installments.")
        return data

class RepaymentForm(forms.Form):
    amount = forms.IntegerField(min_value=1,max_value=2*MAX_LOAN_AMOUNT,label="Amount received (RWF)")
    reference = forms.CharField(max_length=120,help_text="Verified payment reference. Stored uppercase; a reference cannot be reused for this loan.")
    submission_token = forms.UUIDField(widget=forms.HiddenInput,initial=uuid.uuid4)
