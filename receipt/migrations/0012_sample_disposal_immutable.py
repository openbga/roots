"""Physical disposal is final in the database too, as custody and external sources are.

Before disposal, retention and the disposal hold stay editable through the audited services.
Once `disposed_at` is set, the row can neither change nor be deleted, and no storage record ever
moves to another sample. Existing rows, including disposed ones, are not rewritten."""

from django.db import migrations

SQL = """
CREATE FUNCTION protect_sample_disposal() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.disposed_at IS NOT NULL THEN
      RAISE EXCEPTION 'Sample disposal records cannot be deleted' USING ERRCODE = 'check_violation';
    END IF;
    RETURN OLD;
  END IF;
  IF NEW.sample_id <> OLD.sample_id THEN
    RAISE EXCEPTION 'A storage record cannot move to another sample' USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.disposed_at IS NOT NULL AND to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD) THEN
    RAISE EXCEPTION 'Sample disposal records cannot be changed' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER sample_disposal_immutable BEFORE UPDATE OR DELETE ON receipt_samplestorage
FOR EACH ROW EXECUTE FUNCTION protect_sample_disposal();
"""
REVERSE = """
DROP TRIGGER sample_disposal_immutable ON receipt_samplestorage;
DROP FUNCTION protect_sample_disposal();
"""


class Migration(migrations.Migration):
    dependencies = [("receipt", "0011_entity_master_data")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
