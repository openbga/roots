from django.urls import path

from . import client_views, correction_views, storage_views, subcontract_views, views

app_name = "receipt"
urlpatterns = [
    path(
        "samples/<uuid:pk>/correct/<str:section>/",
        correction_views.sample_correct,
        name="sample-correct",
    ),
    path(
        "orders/<uuid:pk>/correct-reference/",
        correction_views.order_correct_reference,
        name="order-correct-reference",
    ),
    path("subcontracts/", subcontract_views.subcontract_list, name="subcontract-list"),
    path(
        "requested-tests/<int:pk>/subcontract/",
        subcontract_views.subcontract_create,
        name="subcontract-create",
    ),
    path("subcontracts/<int:pk>/", subcontract_views.subcontract_detail, name="subcontract-detail"),
    path(
        "subcontracts/<int:pk>/results/new/",
        subcontract_views.subcontract_result_create,
        name="subcontract-result-create",
    ),
    path(
        "subcontract-results/<int:pk>/review/",
        subcontract_views.subcontract_result_review,
        name="subcontract-result-review",
    ),
    path(
        "subcontracts/<int:pk>/cancel/",
        subcontract_views.subcontract_cancel,
        name="subcontract-cancel",
    ),
    path(
        "subcontract-results/<int:pk>/source/",
        subcontract_views.subcontract_source_download,
        name="subcontract-source-download",
    ),
    path("storage/", storage_views.storage_list, name="storage-list"),
    path("samples/<uuid:pk>/storage/", storage_views.sample_storage, name="sample-storage"),
    path(
        "samples/<uuid:pk>/storage/retention/",
        storage_views.sample_retention,
        name="sample-retention",
    ),
    path(
        "samples/<uuid:pk>/storage/hold/",
        storage_views.sample_disposal_hold,
        name="sample-disposal-hold",
    ),
    path("samples/<uuid:pk>/storage/dispose/", storage_views.sample_dispose, name="sample-dispose"),
    path("orders/<uuid:pk>/labels/", views.order_labels, name="order-labels"),
    path("samples/<uuid:pk>/labels/", views.sample_label_download, name="sample-labels"),
    path("schedule/", views.schedule, name="schedule"),
    path("orders/<uuid:pk>/schedule/", views.order_schedule, name="order-schedule"),
    path("orders/", views.order_list, name="order-list"),
    path("orders/new/", views.order_create, name="order-create"),
    path("orders/<uuid:pk>/", views.order_detail, name="order-detail"),
    path("orders/<uuid:pk>/edit/", views.order_edit, name="order-edit"),
    path("orders/<uuid:pk>/cancel/", views.order_cancel, name="order-cancel"),
    path("orders/<uuid:pk>/reinstate/", views.order_reinstate, name="order-reinstate"),
    path("orders/<uuid:pk>/samples/new/", views.order_add_sample, name="order-add-sample"),
    path("samples/<uuid:pk>/disposition/", views.sample_disposition, name="sample-disposition"),
    path("clients/<int:pk>/", client_views.client_detail, name="client-detail"),
    path("clients/<int:pk>/contacts/new/", client_views.contact_edit, name="contact-create"),
    path(
        "clients/<int:pk>/contacts/<int:contact_pk>/",
        client_views.contact_edit,
        name="contact-edit",
    ),
    path("clients/<int:pk>/edit/", views.client_edit, name="client-edit"),
    path("projects/<int:pk>/edit/", views.project_edit, name="project-edit"),
    path("samples/", views.sample_list, name="sample-list"),
    path("samples/new/", views.sample_create, name="sample-create"),
    path("samples/<uuid:pk>/", views.sample_detail, name="sample-detail"),
    path("clients/", views.client_list, name="client-list"),
    path("clients/new/", views.client_create, name="client-create"),
    path("projects/", views.project_list, name="project-list"),
    path("projects/new/", views.project_create, name="project-create"),
]
