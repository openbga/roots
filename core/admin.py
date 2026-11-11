from django import forms
from django.contrib import admin

from accounts.roles import has_role

from .models import Lab
from .retention import set_record_retention, validate_retention_change


class RecordRetentionForm(forms.ModelForm):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    reason = forms.CharField(max_length=2000, widget=forms.Textarea(attrs={"rows": 3}))

    class Meta:
        model = Lab
        fields = ["record_retention_statement"]
        widgets = {"record_retention_statement": forms.Textarea(attrs={"rows": 8})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["expected_revision"] = self.instance.record_retention_revision

    def clean(self):
        data = super().clean()
        if not self.errors:
            # Django admin wraps change-form validation and save in one atomic transaction.
            lab = Lab.objects.select_for_update().get(pk=self.instance.pk)
            validate_retention_change(
                lab,
                expected_revision=data["expected_revision"],
                statement=data["record_retention_statement"],
                reason=data["reason"],
            )
        return data


@admin.register(Lab)
class LabAdmin(admin.ModelAdmin):
    form = RecordRetentionForm
    list_display = ["name", "record_retention_revision"]
    readonly_fields = ["name", "timezone"]
    fields = ["name", "timezone", "record_retention_statement", "reason", "expected_revision"]
    actions = None

    def _allowed(self, request):
        return request.user.is_superuser and has_role(request.user, "admin")

    def has_module_permission(self, request):
        return self._allowed(request)

    def has_view_permission(self, request, obj=None):
        return self._allowed(request)

    def has_change_permission(self, request, obj=None):
        return self._allowed(request)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        saved = set_record_retention(
            actor=request.user,
            lab_id=obj.pk,
            expected_revision=form.cleaned_data["expected_revision"],
            statement=form.cleaned_data["record_retention_statement"],
            reason=form.cleaned_data["reason"],
        )
        obj.record_retention_statement = saved.record_retention_statement
        obj.record_retention_revision = saved.record_retention_revision
