from django.urls import path

from . import views

app_name = "commercial"
urlpatterns = [
    path("price-lists/", views.price_list_list, name="price-list-list"),
    path("price-lists/new/", views.price_list_edit, name="price-list-create"),
    path("price-lists/<int:pk>/", views.price_list_edit, name="price-list-edit"),
    path("quotes/", views.quote_list, name="quote-list"),
    path("quotes/new/", views.quote_create, name="quote-create"),
    path("quotes/<int:pk>/", views.quote_detail, name="quote-detail"),
    path("quotes/<int:pk>/edit/", views.quote_edit, name="quote-edit"),
    path("quotes/<int:pk>/issue/", views.quote_issue, name="quote-issue"),
    path("quotes/<int:pk>/revise/", views.quote_revise, name="quote-revise"),
    path("quotes/<int:pk>/withdraw/", views.quote_withdraw, name="quote-withdraw"),
    path("submissions/", views.submission_list, name="submission-list"),
    path("submissions/<int:pk>/", views.submission_detail, name="submission-detail"),
    path("submissions/<int:pk>/labels/", views.submission_labels, name="submission-labels"),
    path("submissions/<int:pk>/return/", views.submission_return, name="submission-return"),
    path("submissions/<int:pk>/cancel/", views.submission_cancel, name="submission-cancel"),
    path(
        "submissions/<int:pk>/reinstate/", views.submission_reinstate, name="submission-reinstate"
    ),
    path("submissions/<int:pk>/accept/", views.submission_accept, name="submission-accept"),
    path(
        "submissions/<int:pk>/samples/<int:sample_pk>/receive/",
        views.submission_receive,
        name="submission-receive",
    ),
]
