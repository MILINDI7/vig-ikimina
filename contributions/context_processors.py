from .views import review_groups

def navigation(request):
    return {"can_review_any": request.user.is_authenticated and review_groups(request.user).exists()}
