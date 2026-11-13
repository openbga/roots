from django.urls import path

from . import views

app_name = "dataimport"
urlpatterns = [
    path("", views.index, name="index"),
    path("starter/<str:name>", views.starter_download, name="starter"),
    path("<slug:kind>/", views.upload, name="upload"),
    path("<slug:kind>/template.csv", views.template, name="template"),
]
