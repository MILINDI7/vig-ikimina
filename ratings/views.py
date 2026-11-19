from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe
from contributions.views import review_groups
from groups.models import Membership
from .services import evaluate_membership, loan_terms

@login_required
@never_cache
@require_safe
def detail(request, pk):
    visible = Membership.objects.filter(Q(user=request.user) | Q(group__in=review_groups(request.user,include_archived=True))).select_related("user","group").distinct()
    membership = get_object_or_404(visible,pk=pk)
    rating = evaluate_membership(pk)
    return render(request,"ratings/detail.html",{"membership": membership,"rating": rating,"history": membership.rating_history.all(),"terms": loan_terms(pk)})
