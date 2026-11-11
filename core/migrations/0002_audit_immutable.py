from django.db import migrations

SQL = """
CREATE FUNCTION prevent_record_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'Historical records cannot be updated or deleted';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER audit_immutable BEFORE UPDATE OR DELETE ON core_auditevent
FOR EACH ROW EXECUTE FUNCTION prevent_record_mutation();
CREATE TRIGGER custody_immutable BEFORE UPDATE OR DELETE ON receipt_custodyevent
FOR EACH ROW EXECUTE FUNCTION prevent_record_mutation();
"""
REVERSE = """
DROP TRIGGER custody_immutable ON receipt_custodyevent;
DROP TRIGGER audit_immutable ON core_auditevent;
DROP FUNCTION prevent_record_mutation();
"""


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial"), ("receipt", "0001_initial")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
