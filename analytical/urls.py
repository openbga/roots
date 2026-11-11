from django.urls import path

from . import import_views, mdl_views, preparation_views, preservation_views, views

app_name = "analytical"
urlpatterns = [
    path("preparation/", preparation_views.preparation_list, name="prep-list"),
    path("preparation/new/", preparation_views.preparation_create, name="prep-create"),
    path("preparation/<int:pk>/", preparation_views.preparation_detail, name="prep-detail"),
    path(
        "preparation/<int:pk>/aliquots/new/", preparation_views.aliquot_add, name="prep-aliquot-add"
    ),
    path(
        "preparation/<int:pk>/aliquots/<int:aliquot_pk>/cancel/",
        preparation_views.aliquot_cancel,
        name="prep-aliquot-cancel",
    ),
    path("preparation/<int:pk>/start/", preparation_views.preparation_start, name="prep-start"),
    path(
        "preparation/<int:pk>/complete/",
        preparation_views.preparation_complete,
        name="prep-complete",
    ),
    path("preparation/<int:pk>/close/", preparation_views.preparation_close, name="prep-close"),
    path("preservation/<int:pk>/", preservation_views.assess, name="preservation-assess"),
    path("worklist/", views.worklist, name="batch-worklist"),
    path("batches/", views.batch_list, name="batch-list"),
    path("batches/new/", views.batch_create, name="batch-create"),
    path("batches/<int:pk>/", views.batch_detail, name="batch-detail"),
    path("batches/<int:pk>/tests/", views.batch_add_tests, name="batch-add-tests"),
    path(
        "batches/<int:pk>/tests/<int:test_pk>/remove/",
        views.batch_remove_test,
        name="batch-remove-test",
    ),
    path("batches/<int:pk>/close/", views.batch_close, name="batch-close"),
    path("batches/<int:pk>/preparation/", views.batch_preparation, name="batch-preparation"),
    path("batches/<int:pk>/equipment/", views.batch_equipment, name="batch-equipment"),
    path("batches/<int:pk>/qc/new/", views.qc_sample_add, name="qc-sample-add"),
    path("batches/<int:pk>/qc/action/", views.qc_action, name="qc-action"),
    path("batches/<int:pk>/entry/", views.batch_entry, name="batch-entry"),
    path("batches/<int:pk>/import/", views.batch_import, name="batch-import"),
    path(
        "batches/<int:pk>/import-mapped/",
        import_views.batch_import_mapped,
        name="batch-import-mapped",
    ),
    path("import-profiles/", import_views.profile_list, name="import-profile-list"),
    path("import-profiles/new/", import_views.profile_create, name="import-profile-create"),
    path("import-profiles/<int:pk>/edit/", import_views.profile_edit, name="import-profile-edit"),
    path("batches/<int:pk>/submit/", views.batch_submit, name="batch-submit"),
    path("qc/<int:pk>/measure/", views.qc_measure, name="qc-measure"),
    path("results/<int:pk>/", views.result_detail, name="result-detail"),
    path("review/", views.review_list, name="review-list"),
    path("review/batches/<int:pk>/", views.review_batch, name="review-batch"),
    path("config/", views.qc_config, name="qc-config"),
    path("config/<slug:kind>/new/", views.config_edit, name="config-new"),
    path("config/<slug:kind>/<int:pk>/", views.config_edit, name="config-edit"),
    path("mdl/", mdl_views.mdl_list, name="mdl-list"),
    path("mdl/new/", mdl_views.mdl_create, name="mdl-create"),
    path("mdl/<int:pk>/", mdl_views.mdl_detail, name="mdl-detail"),
    path(
        "mdl/<int:pk>/observations/new/", mdl_views.mdl_observation_add, name="mdl-observation-add"
    ),
    path("mdl/<int:pk>/import-blanks/", mdl_views.mdl_import_blanks, name="mdl-import-blanks"),
    path("mdl/<int:pk>/calculate/", mdl_views.mdl_calculate, name="mdl-calculate"),
    path("mdl/<int:pk>/close/", mdl_views.mdl_close, name="mdl-close"),
    path("mdl/observations/<int:pk>/exclude/", mdl_views.mdl_exclude, name="mdl-exclude"),
    path("mdl/calculations/<int:pk>/", mdl_views.mdl_calculation, name="mdl-calculation"),
    path("charts/", mdl_views.chart_list, name="chart-list"),
    path(
        "charts/<int:definition_id>/<slug:qc_type>/<slug:measure>/",
        mdl_views.chart_detail,
        name="chart-detail",
    ),
]
