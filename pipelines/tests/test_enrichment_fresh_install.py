"""enrichment.py on a machine with no monitoring/ directory.

monitoring/ is gitignored, so a fresh clone, the Vultr box and the client's Mac
app all start without it. The dry-run report writes its manifest and approval
report there, and the whole-catalog audit imports monitoring/gap_report.py,
which exists only on a dev machine. Before this was handled, every DT Tire
enrich step on the Mac app's first run died with FileNotFoundError.

Each test points enrichment's ROOT at an empty temp directory.
"""

import sys
from pathlib import Path

_PIPELINES_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_PIPELINES_DIR / "shared"))

import enrichment as E  # noqa: E402


def test_free_field_audit_is_empty_without_gap_report(monkeypatch, tmp_path):
    monkeypatch.setattr(E, "ROOT", str(tmp_path))
    assert E.audit_free_fields() == {}


def test_approval_report_creates_monitoring_dir(monkeypatch, tmp_path):
    target = tmp_path / "monitoring" / "ENRICHMENT_APPROVAL.md"
    monkeypatch.setattr(E, "APPROVAL_MD", str(target))
    E.write_approval_report({}, {}, {}, total_needs_paid=0)
    assert target.read_text(encoding="utf-8").startswith("# Enrichment Approval Report")
