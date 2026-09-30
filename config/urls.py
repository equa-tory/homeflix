from django.contrib import admin
from django.urls import path, include

from library.auth import ThrottledLoginView, logout_view

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", ThrottledLoginView.as_view(), name="login"),
    path("logout/", logout_view, name="logout"),
    path("", include("library.urls")),
]
