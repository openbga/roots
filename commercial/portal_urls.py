from django.urls import path

from . import portal_views as views

app_name = "client_commercial"
urlpatterns = [
    path("quotes/", views.quote_list, name="quote-list"),
    path("quotes/<int:pk>/", views.quote_detail, name="quote-detail"),
    path("quotes/<int:pk>/accept/", views.quote_accept, name="quote-accept"),
    path("quotes/<int:pk>/decline/", views.quote_decline, name="quote-decline"),
    path("submissions/", views.submission_list, name="submission-list"),
    path("submissions/new/", views.submission_create, name="submission-create"),
    path("submissions/<int:pk>/", views.submission_detail, name="submission-detail"),
    path("submissions/<int:pk>/edit/", views.submission_edit, name="submission-edit"),
    path("submissions/<int:pk>/submit/", views.submission_submit, name="submission-submit"),
    path("submissions/<int:pk>/discard/", views.submission_discard, name="submission-discard"),
    path("submissions/<int:pk>/samples/new/", views.sample_edit, name="sample-create"),
    path("submissions/<int:pk>/samples/<int:sample_pk>/", views.sample_edit, name="sample-edit"),
    path(
        "submissions/<int:pk>/samples/<int:sample_pk>/delete/",
        views.sample_delete,
        name="sample-delete",
    ),
]
