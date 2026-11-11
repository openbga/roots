from django.db import migrations

TABLES = (
    "reporting_reportversion",
    "reporting_reportfile",
    "reporting_reportversion_result_revisions",
)


class Migration(migrations.Migration):
    dependencies = [("reporting", "0001_initial"), ("core", "0002_audit_immutable")]
    operations = [
        migrations.RunSQL(
            f"CREATE TRIGGER issued_record_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION prevent_record_mutation();",
            f"DROP TRIGGER issued_record_immutable ON {table};",
        )
        for table in TABLES
    ]
