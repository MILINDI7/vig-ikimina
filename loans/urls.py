from django.urls import path
from . import views
urlpatterns=[path("",views.loan_list,name="loan_list"),path("memberships/<int:pk>/request/",views.create,name="loan_request_create"),path("requests/<int:pk>/",views.detail,name="loan_request_detail"),path("<int:pk>/repay/",views.repayment,name="loan_repayment")]
