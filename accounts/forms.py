from django import forms
from .models import User
class AccountForm(forms.ModelForm):
    first_name = forms.CharField(label="Full name", max_length=150)
    email = forms.EmailField(required=True)
    phone = forms.CharField(max_length=30)
    class Meta:
        model = User
        fields = ("username", "first_name", "email", "phone", "national_id", "role")
    def __init__(self, *args, actor, **kwargs):
        super().__init__(*args, **kwargs)
        if not actor.is_top_admin:
            self.fields["role"].choices = [(r, label) for r, label in User.Role.choices if r in (User.Role.MEMBER, User.Role.TREASURER)]
class RoleForm(forms.Form):
    role = forms.ChoiceField(choices=User.Role.choices)
    def __init__(self, *args, actor, **kwargs):
        super().__init__(*args, **kwargs)
        if not actor.is_top_admin:
            self.fields["role"].choices = [(r, label) for r, label in User.Role.choices if r in (User.Role.MEMBER, User.Role.TREASURER)]
