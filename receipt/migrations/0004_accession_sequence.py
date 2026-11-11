from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("receipt", "0003_client_is_active_client_revision_and_more")]
    operations = [
        migrations.RunSQL(
            """
            CREATE SEQUENCE receipt_accession_seq;
            SELECT setval('receipt_accession_seq',
                COALESCE((SELECT MAX(substring(lab_id FROM '^VR-([0-9]+)$')::bigint)
                          FROM receipt_sample), 0) + 1, false);
            """,
            "DROP SEQUENCE receipt_accession_seq;",
        )
    ]
