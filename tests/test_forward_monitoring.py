"""CP08-07 smoke and fail-closed tests. Run in the existing project's venv."""
from pathlib import Path
from crypto_decision_lab.forward_monitoring import audit, main


def test_missing_dataset_fails_closed(tmp_path: Path):
    report = audit(tmp_path)
    assert report["gate"] == "FAIL"
    assert any(c["name"] == "AUDIT_EXCEPTION" for c in report["failures"])


def test_missing_dataset_cli_nonzero(tmp_path: Path):
    assert main(["--root", str(tmp_path)]) == 1


def test_report_written_outside_dataset(tmp_path: Path):
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    report = tmp_path / "reports" / "audit.json"
    assert main(["--root", str(dataset), "--report", str(report)]) == 1
    assert report.is_file()


def test_report_inside_dataset_forbidden(tmp_path: Path):
    import pytest
    with pytest.raises(SystemExit):
        main(["--root", str(tmp_path), "--report", str(tmp_path / "result.json")])
