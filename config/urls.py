from django.contrib import admin
from django.urls import path, include
from django.contrib.auth.views import LogoutView
from accounts import views
urlpatterns = [path("ratings/", include("ratings.urls")), path("contributions/", include("contributions.urls")), path("groups/", include("groups.urls")), path("admin/", admin.site.urls), path("", views.dashboard, name="dashboard"), path("login/", views.RateLimitedLoginView.as_view(), name="login"), path("logout/", LogoutView.as_view(), name="logout"), path("password/change/", views.ChangePasswordView.as_view(), name="password_change"), path("accounts/new/", views.create_account, name="account_create"), path("accounts/<int:pk>/role/", views.change_role, name="role_change")]
