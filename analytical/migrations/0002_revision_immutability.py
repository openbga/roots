from django.db import migrations

SQL = """
CREATE FUNCTION protect_result_revision() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'Result revisions cannot be deleted' USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.status NOT IN ('draft', 'submitted') THEN
    RAISE EXCEPTION 'Reviewed result revisions cannot be changed' USING ERRCODE = 'check_violation';
  END IF;
  IF (to_jsonb(NEW) - ARRAY['status', 'submitted_by_id', 'submitted_at',
        'reviewed_by_id', 'reviewed_at', 'review_note', 'qc_evidence'])
     IS DISTINCT FROM
     (to_jsonb(OLD) - ARRAY['status', 'submitted_by_id', 'submitted_at',
        'reviewed_by_id', 'reviewed_at', 'review_note', 'qc_evidence']) THEN
    RAISE EXCEPTION 'Result observations cannot be changed' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER result_revision_immutable BEFORE UPDATE OR DELETE ON analytical_resultrevision
FOR EACH ROW EXECUTE FUNCTION protect_result_revision();
CREATE TRIGGER qc_action_immutable BEFORE UPDATE OR DELETE ON analytical_qccorrectiveaction
FOR EACH ROW EXECUTE FUNCTION prevent_record_mutation();
"""
REVERSE = """
DROP TRIGGER qc_action_immutable ON analytical_qccorrectiveaction;
DROP TRIGGER result_revision_immutable ON analytical_resultrevision;
DROP FUNCTION protect_result_revision();
"""


class Migration(migrations.Migration):
    dependencies = [("analytical", "0001_initial"), ("core", "0002_audit_immutable")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
