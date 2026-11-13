"""Acceptance scripts a laboratory runs on its own installation and keeps as evidence.

`manage.py run_acceptance` creates a separate scratch database, migrates it from empty, runs
the numbered steps in `journey.py` and `controls.py` on synthetic records, writes an evidence
package and removes the scratch database. The laboratory's own records are never read or
changed. Each step is a plain function with its identifier, title, requirement and expected
result declared beside it; `runner.py` runs them in order and records what was observed.
"""
