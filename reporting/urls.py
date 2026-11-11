from django.urls import path

from . import views
from .edd import views as edd

app_name = "reporting"
urlpatterns = [
    path("", views.report_list, name="report-list"),
    path("new/", views.report_create, name="report-create"),
    path("<int:pk>/", views.report_detail, name="report-detail"),
    path("<int:pk>/edit/", views.report_edit, name="report-edit"),
    path("<int:pk>/preview/", views.report_preview, name="report-preview"),
    path("<int:pk>/preview.pdf", views.report_preview_pdf, name="report-preview-pdf"),
    path("<int:pk>/release/", views.report_release, name="report-release"),
    path("<int:pk>/v<int:number>.pdf", views.version_pdf, name="version-pdf"),
    path("<int:pk>/edd/nysdec-readiness/", edd.edd_readiness, name="edd-readiness"),
    path("<int:pk>/v<int:number>/edd/nysdec/", edd.edd_preview, name="edd-preview"),
    path("<int:pk>/v<int:number>/edd/context/", edd.edd_context, name="edd-context"),
    path("edd/<int:export_pk>.zip", edd.edd_download, name="edd-download"),
    path("nysdec/projects/<int:project_pk>/", edd.nysdec_project, name="nysdec-project"),
    path("nysdec/tests/<int:definition_pk>/", edd.nysdec_test, name="nysdec-test"),
    path("nysdec/qualifiers/<int:qualifier_pk>/", edd.nysdec_qualifier, name="nysdec-qualifier"),
]
