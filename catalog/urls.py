from django.urls import path

from . import views

app_name = "catalog"
urlpatterns = [
    path("", views.test_list, name="test-list"),
    path("tests/new/", views.test_edit, name="test-create"),
    path("tests/<int:pk>/", views.test_edit, name="test-edit"),
    path("methods/", views.method_list, name="method-list"),
    path("methods/new/", views.method_edit, name="method-create"),
    path("methods/<int:pk>/", views.method_edit, name="method-edit"),
    path("methods/<int:pk>/new-version/", views.method_new_version, name="method-new-version"),
    path("analytes/", views.analyte_list, name="analyte-list"),
    path("analytes/new/", views.analyte_edit, name="analyte-create"),
    path("analytes/<int:pk>/", views.analyte_edit, name="analyte-edit"),
]
