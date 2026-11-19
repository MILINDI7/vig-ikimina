from django.urls import path
from . import views
urlpatterns = [path("memberships/<int:pk>/",views.detail,name="rating_detail")]
