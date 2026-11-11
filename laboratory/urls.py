from django.urls import path

from . import equipment_views, inventory_views, nc_views, training_views, views

app_name = "laboratory"
urlpatterns = [
    path("", views.overview, name="overview"),
    path("evidence/<str:kind>/<int:pk>/", views.evidence, name="evidence"),
    # inventory
    path("inventory/", inventory_views.lot_list, name="lot-list"),
    path("inventory/lots/new/", inventory_views.lot_create, name="lot-create"),
    path("inventory/lots/<int:pk>/", inventory_views.lot_detail, name="lot-detail"),
    path("inventory/lots/<int:pk>/edit/", inventory_views.lot_edit, name="lot-edit"),
    path("inventory/lots/<int:pk>/use/", inventory_views.lot_use, name="lot-use"),
    path("inventory/lots/<int:pk>/receive/", inventory_views.lot_receive_more, name="lot-receive"),
    path("inventory/lots/<int:pk>/adjust/", inventory_views.lot_adjust, name="lot-adjust"),
    path("inventory/lots/<int:pk>/retire/", inventory_views.lot_retire, name="lot-retire"),
    path(
        "inventory/movements/<int:pk>/reverse/",
        inventory_views.movement_reverse,
        name="movement-reverse",
    ),
    path("inventory/materials/", inventory_views.material_list, name="material-list"),
    path("inventory/materials/new/", inventory_views.material_edit, name="material-create"),
    path("inventory/materials/<int:pk>/", inventory_views.material_detail, name="material-detail"),
    path("inventory/materials/<int:pk>/edit/", inventory_views.material_edit, name="material-edit"),
    # equipment
    path("equipment/", equipment_views.equipment_list, name="equipment-list"),
    path("equipment/new/", equipment_views.equipment_edit, name="equipment-create"),
    path("equipment/<int:pk>/", equipment_views.equipment_detail, name="equipment-detail"),
    path("equipment/<int:pk>/edit/", equipment_views.equipment_edit, name="equipment-edit"),
    path("equipment/<int:pk>/status/", equipment_views.equipment_status, name="equipment-status"),
    path("equipment/<int:pk>/events/new/", equipment_views.event_edit, name="event-create"),
    path(
        "equipment/<int:pk>/events/<int:event_pk>/correct/",
        equipment_views.event_edit,
        name="event-correct",
    ),
    # training
    path("training/", training_views.training_list, name="training-list"),
    path("training/new/", training_views.training_edit, name="training-create"),
    path("training/<int:pk>/", training_views.training_detail, name="training-detail"),
    path(
        "training/<int:pk>/renew/",
        training_views.training_edit,
        {"mode": "renew"},
        name="training-renew",
    ),
    path(
        "training/<int:pk>/correct/",
        training_views.training_edit,
        {"mode": "correct"},
        name="training-correct",
    ),
    # nonconformance
    path("nonconformances/", nc_views.nc_list, name="nc-list"),
    path("nonconformances/new/", nc_views.nc_create, name="nc-create"),
    path("nonconformances/<int:pk>/", nc_views.nc_detail, name="nc-detail"),
    path("nonconformances/<int:pk>/edit/", nc_views.nc_edit, name="nc-edit"),
    path("nonconformances/<int:pk>/note/", nc_views.nc_note, name="nc-note"),
    path("nonconformances/<int:pk>/step/<slug:action>/", nc_views.nc_step, name="nc-step"),
]
