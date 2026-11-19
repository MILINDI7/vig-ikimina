from django import forms
from django.utils import timezone
from accounts.models import User
from .models import Group, Membership, GroupSettings

DEFAULT_RATINGS = [{"code": "A", "name": "Good standing", "description": "No late payments or defaults", "max_days_late": 0, "max_late_payments": 0, "max_defaults": 0, "loan_eligible": True, "interest_bps": 500, "max_loan_multiple": 3}, {"code": "E", "name": "Restricted", "description": "Fallback rating", "max_days_late": None, "max_late_payments": None, "max_defaults": None, "loan_eligible": False, "interest_bps": 0, "max_loan_multiple": 0}]

class GroupForm(forms.ModelForm):
    class Meta:
        model = Group
        fields = ["name", "description"]

class MembershipForm(forms.ModelForm):
    class Meta:
        model = Membership
        fields = ["user", "role"]
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["user"].queryset = User.objects.filter(is_active=True).order_by("username")

def integer(value, minimum=0):
    return type(value) is int and value >= minimum

class SettingsForm(forms.ModelForm):
    class Meta:
        model = GroupSettings
        fields = ["contribution_amount", "due_day", "loan_default_days", "effective_from", "lateness_tiers", "rating_classes"]
        widgets = {"effective_from": forms.DateInput(attrs={"type": "date"}), "lateness_tiers": forms.Textarea(attrs={"rows": 8}), "rating_classes": forms.Textarea(attrs={"rows": 16})}
        help_texts = {"loan_default_days": "Loan defaults after this many days past an unpaid installment due date (1-365). Fixed at approval; blank uses 30.", "due_day": "Day of the following month (1-28), so every month has a valid due date.", "lateness_tiers": 'JSON list: [{"days_late": 1, "flat_fee": 500, "percentage_bps": 0}]. Each tier is assessed once when its threshold is reached. Percentages use the contribution principal; 100 basis points = 1%.', "rating_classes": "Ordered best to worst. Rules use a rolling 12-month window; the last class must be an unrestricted fallback. Optional max_outstanding_loans limits open loans; omitted/null means unrestricted. Interest is a flat rate per loan, in basis points; loan limits are integer multiples of approved savings."}
    def __init__(self, *args, new_group=False, **kwargs):
        self.new_group = new_group
        super().__init__(*args, **kwargs)
        self.fields["loan_default_days"].required = False
    def clean_loan_default_days(self):
        value = self.cleaned_data.get("loan_default_days")
        return 30 if value is None else value
    def clean_effective_from(self):
        day = self.cleaned_data["effective_from"]
        today = timezone.localdate()
        if (self.new_group and day != today) or (not self.new_group and day <= today):
            raise forms.ValidationError("Initial settings must start today; subsequent versions must start on a future date.")
        return day
    def clean_lateness_tiers(self):
        tiers = self.cleaned_data["lateness_tiers"]
        if not isinstance(tiers, list): raise forms.ValidationError("Enter a JSON list.")
        previous = 0
        for tier in tiers:
            if not isinstance(tier, dict) or set(tier) != {"days_late", "flat_fee", "percentage_bps"}:
                raise forms.ValidationError("Every tier needs days_late, flat_fee and percentage_bps.")
            if not integer(tier["days_late"], previous + 1) or not integer(tier["flat_fee"]) or not integer(tier["percentage_bps"]) or tier["percentage_bps"] > 10000:
                raise forms.ValidationError("Use increasing positive day thresholds, nonnegative integer fees and basis points from 0 to 10000.")
            previous = tier["days_late"]
        return tiers
    def clean_rating_classes(self):
        classes = self.cleaned_data["rating_classes"]
        if not isinstance(classes, list) or not classes:
            raise forms.ValidationError("Provide at least one rating class.")
        codes = set()
        keys = {"code", "name", "description", "max_days_late", "max_late_payments", "max_defaults", "loan_eligible", "interest_bps", "max_loan_multiple"}
        for rating in classes:
            if not isinstance(rating, dict) or not keys.issubset(rating) or set(rating) - keys - {"max_outstanding_loans"}:
                raise forms.ValidationError("Every class must contain all fields shown in the default example.")
            if any(not isinstance(rating[k], str) for k in ["code", "name", "description"]) or not rating["code"].strip() or not rating["name"].strip() or rating["code"] in codes:
                raise forms.ValidationError("Classes need unique nonempty codes and names, and text descriptions.")
            if len(rating["code"]) > 100:
                raise forms.ValidationError("Rating codes must be at most 100 characters.")
            codes.add(rating["code"])
            if rating.get("max_outstanding_loans") is not None and not integer(rating["max_outstanding_loans"]):
                raise forms.ValidationError("Maximum outstanding loans must be a nonnegative integer or null.")
            if any(rating[k] is not None and not integer(rating[k]) for k in ["max_days_late", "max_late_payments", "max_defaults"]):
                raise forms.ValidationError("Rating thresholds must be nonnegative integers or null.")
            if type(rating["loan_eligible"]) is not bool or not integer(rating["interest_bps"]) or rating["interest_bps"] > 10000 or not integer(rating["max_loan_multiple"]):
                raise forms.ValidationError("Use a boolean eligibility, integer interest basis points (0-10000), and a nonnegative integer savings multiple.")
        if any(classes[-1].get(k) is not None for k in ["max_days_late", "max_late_payments", "max_defaults", "max_outstanding_loans"]):
            raise forms.ValidationError("The final class must have null thresholds to serve as a fallback.")
        return classes
