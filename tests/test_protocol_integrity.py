"""Tests for S02-CP08-05R: frozen protocol integrity reconciliation (LF/CRLF).

Covers the four required scenarios:
  * LF/CRLF normalisation equivalence (byte-level evidence);
  * a REAL content mismatch is tampering, not a representation change;
  * a missing archive yields ARCHIVE_MISSING with the archive gate blocked;
  * the frozen v1.0.0 version is preserved (v1 sidecar untouched, both hashes
    pinned, parsed rules semantically identical to the frozen canonical hash).
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from crypto_decision_lab.independent_validation import (
    ValidationPaths,
    _sidecar_check,
    load_protocol,
)
from crypto_decision_lab.protocol_integrity import (
    ARCHIVE_CORRUPT,
    ARCHIVE_MISSING,
    ARCHIVE_VERIFIED,
    GATE_BLOCKED,
    GATE_PASS,
    PROTOCOL_TAMPERED,
    PROTOCOL_VERIFIED,
    SIDECAR_V2_INCONSISTENT,
    SIDECAR_V2_MISSING,
    denormalize_crlf,
    normalize_lf,
    read_sidecar_v1,
    read_sidecar_v2,
    reconcile,
    representation_evidence,
    semantic_equivalence,
    verify_archive,
    verify_protocol_integrity,
    write_sidecar_v2,
)

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_DIR = ROOT / "config" / "protocols"
PROTOCOL_YAML = PROTOCOL_DIR / "momentum-independent-validation-v1.yaml"
ENGINE_YAML = PROTOCOL_DIR / "momentum-confirmation-engine-v1.yaml"
ARCHIVE_DIR = ROOT / "data" / "experiments" / "S02-CP08-independent-validation-protocol-v1"

# Historical evidence, cross-checked from three independent sources: the
# original v1 sidecar (CRLF freeze), the Git blob (LF canonical form, stored
# by ``text eol=lf``) and the authentic CP08-04 archive manifest.
FROZEN_LF_SHA256 = "cda9eab6c2943b9515f8fbabf6089851398ec2c578a697f90399ea46ba569665"
FROZEN_CRLF_SHA256 = "b5a7d248bcfde6b02b879292097ca8f758f66e7b7528dc3d6db45237f246b80e"
# Canonical hash of the parsed protocol, pinned by the authentic archive manifest.
FROZEN_CANONICAL_SHA256 = "7b32d50693906f8d818f2cb839ad70b489b5f124497635f203b0c98ebd792eb4"


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _stage_config(target: Path, *, line_endings: str, with_v2: bool = True) -> Path:
    """Materialise the frozen config like a Git checkout would."""
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = PROTOCOL_YAML.read_bytes()
    payload = normalize_lf(raw) if line_endings == "lf" else denormalize_crlf(raw)
    target.write_bytes(payload)
    shutil.copyfile(PROTOCOL_YAML.with_suffix(".yaml.sha256"), target.with_suffix(".yaml.sha256"))
    if with_v2:
        shutil.copyfile(
            PROTOCOL_YAML.with_suffix(".yaml.sha256.v2.json"),
            target.with_suffix(".yaml.sha256.v2.json"),
        )
    return target


# ---------------------------------------------------------------------------
# 1. LF/CRLF normalisation: the difference is line endings only
# ---------------------------------------------------------------------------


def test_frozen_hashes_are_the_two_pure_line_ending_representations():
    raw = PROTOCOL_YAML.read_bytes()
    evidence = representation_evidence(raw)
    assert evidence["lf_sha256"] == FROZEN_LF_SHA256
    assert evidence["crlf_sha256"] == FROZEN_CRLF_SHA256
    assert evidence["only_line_endings"] is True
    assert evidence["round_trip_lf_to_crlf_to_lf"] is True
    assert evidence["round_trip_crlf_to_lf_to_crlf"] is True
    # byte-level arithmetic: CRLF = LF + one CR per line
    profile = evidence["profile"]
    assert profile["lone_cr"] == 0
    assert profile["lone_lf"] == 0
    assert evidence["crlf_bytes"] - evidence["lf_bytes"] == profile["crlf_pairs"]
    assert denormalize_crlf(normalize_lf(raw)) == raw


def test_both_working_tree_representations_verify(tmp_path):
    crlf_file = _stage_config(tmp_path / "crlf" / "protocol.yaml", line_endings="crlf")
    lf_file = _stage_config(tmp_path / "lf" / "protocol.yaml", line_endings="lf")
    crlf_result = verify_protocol_integrity(crlf_file)
    lf_result = verify_protocol_integrity(lf_file)
    assert crlf_result["status"] == PROTOCOL_VERIFIED
    assert crlf_result["representation"] == "CRLF"
    assert lf_result["status"] == PROTOCOL_VERIFIED  # the Sirio scenario
    assert lf_result["representation"] == "LF"
    assert lf_result["raw_sha256"] == FROZEN_LF_SHA256
    assert crlf_result["raw_sha256"] == FROZEN_CRLF_SHA256


def test_v1_only_sidecar_cannot_verify_lf_checkout(tmp_path):
    """The original incident, now an explicit state instead of a silent FAIL."""
    lf_file = _stage_config(tmp_path / "protocol.yaml", line_endings="lf", with_v2=False)
    result = verify_protocol_integrity(lf_file)
    assert result["status"] == SIDECAR_V2_MISSING
    assert FROZEN_CRLF_SHA256 in result["reason"] or "S02-CP08-05R" in result["reason"]


# ---------------------------------------------------------------------------
# 2. real mismatches are tampering, never a "representation" excuse
# ---------------------------------------------------------------------------


def test_real_content_change_is_tampering_not_representation(tmp_path):
    target = _stage_config(tmp_path / "protocol.yaml", line_endings="lf")
    raw = target.read_bytes()
    tampered = raw.replace(b"threshold: 0.05", b"threshold: 0.51", 1)
    assert tampered != raw  # a rule value really changed
    target.write_bytes(tampered)
    result = verify_protocol_integrity(target)
    assert result["status"] == PROTOCOL_TAMPERED
    # the file is still a pure LF form: what fails is the frozen anchor,
    # i.e. the re-encoded CRLF bytes no longer match the v1 sidecar pin
    assert result["crlf_sha256"] != FROZEN_CRLF_SHA256
    assert result["lf_sha256"] != FROZEN_LF_SHA256
    assert "v1 sidecar" in result["reason"]


def test_mixed_line_endings_are_not_a_pure_representation(tmp_path):
    target = _stage_config(tmp_path / "protocol.yaml", line_endings="crlf")
    raw = target.read_bytes()
    half = len(raw) // 2
    mixed = raw[:half] + normalize_lf(raw[half:])  # half CRLF, half LF
    target.write_bytes(mixed)
    result = verify_protocol_integrity(target)
    assert result["status"] == PROTOCOL_TAMPERED
    assert result["byte_level_evidence"]["only_line_endings"] is False


def test_v2_sidecar_tampering_is_detected(tmp_path):
    target = _stage_config(tmp_path / "protocol.yaml", line_endings="lf")
    v2_path = target.with_suffix(".yaml.sha256.v2.json")
    payload = json.loads(v2_path.read_text(encoding="utf-8"))
    payload["representations"]["lf_sha256"] = "0" * 64  # forged alias
    v2_path.write_text(json.dumps(payload), encoding="utf-8")
    result = verify_protocol_integrity(target)
    assert result["status"] == SIDECAR_V2_INCONSISTENT


def test_migration_refuses_tampered_content_and_refuses_overwrite(tmp_path):
    target = _stage_config(tmp_path / "tampered.yaml", line_endings="lf")
    target.write_bytes(target.read_bytes() + b"\n# silent edit\n")
    with pytest.raises(ValueError):
        write_sidecar_v2(target, reason="must refuse: content changed")

    clean = _stage_config(tmp_path / "clean.yaml", line_endings="lf", with_v2=False)
    first = write_sidecar_v2(clean, reason="test migration")
    assert first["status"] == PROTOCOL_VERIFIED
    with pytest.raises(FileExistsError):
        write_sidecar_v2(clean, reason="must refuse: already migrated")


# ---------------------------------------------------------------------------
# 3. archive presence is a separate control
# ---------------------------------------------------------------------------


def test_missing_archive_is_reported_and_blocks_only_the_archive_gate(tmp_path):
    result = verify_archive(tmp_path / "nope")
    assert result["status"] == ARCHIVE_MISSING
    report = reconcile(ROOT, archive_dir=tmp_path / "nope")
    assert report["protocol_integrity"]["protocol"]["status"] == PROTOCOL_VERIFIED
    assert report["archive"]["status"] == ARCHIVE_MISSING
    assert report["gates"]["protocol_integrity_gate"] == GATE_PASS
    assert report["gates"]["archive_gate"] == GATE_BLOCKED
    assert report["gates"]["independent_validation"] == "NOT_EVALUATED"
    assert report["gates"]["strategic_gate"] == "NO-GO"
    assert report["constraints"]["evaluation_performed"] is False


def test_authentic_archive_verifies_against_its_own_manifest():
    result = verify_archive(ARCHIVE_DIR)
    assert result["status"] == ARCHIVE_VERIFIED
    assert result["outputs_total"] == 10
    assert result["outputs_matched"] == 10
    # the archive pins the original CRLF freeze of the protocol YAML
    pinned = next(f for f in result["files"] if f["file"].endswith(".yaml") and "independent" in f["file"])
    assert pinned["expected_sha256"] == FROZEN_CRLF_SHA256
    assert result["protocol_canonical_sha256"] == FROZEN_CANONICAL_SHA256


def test_corrupt_archive_is_detected(tmp_path):
    copy = tmp_path / "archive"
    shutil.copytree(ARCHIVE_DIR, copy)
    summary = copy / "summary.json"
    summary.write_bytes(summary.read_bytes().replace(b"NO-GO", b"GO--", 1))
    result = verify_archive(copy)
    assert result["status"] == ARCHIVE_CORRUPT
    assert "summary.json" in result["mismatched"]


def test_missing_manifest_inside_directory_is_archive_missing(tmp_path):
    directory = tmp_path / "archive"
    directory.mkdir()
    (directory / "stray.txt").write_text("not an archive", encoding="utf-8")
    assert verify_archive(directory)["status"] == ARCHIVE_MISSING


# ---------------------------------------------------------------------------
# 4. the frozen v1.0.0 version is preserved
# ---------------------------------------------------------------------------


def test_v1_sidecar_is_preserved_and_pins_the_original_crlf_freeze():
    v1 = read_sidecar_v1(PROTOCOL_YAML)
    assert v1["sidecar_present"] is True
    assert v1["declared_sha256"] == FROZEN_CRLF_SHA256
    assert v1["sidecar_sha256"] == _sha((PROTOCOL_DIR / "momentum-independent-validation-v1.yaml.sha256").read_bytes())
    # single-line sha256sum-style format, untouched
    line = (PROTOCOL_DIR / "momentum-independent-validation-v1.yaml.sha256").read_text(encoding="utf-8").strip()
    assert line == f"{FROZEN_CRLF_SHA256}  momentum-independent-validation-v1.yaml"


def test_v2_sidecar_records_both_representations_without_inventing_hashes():
    v2 = read_sidecar_v2(PROTOCOL_YAML)
    assert v2["sidecar_present"] is True
    payload = v2["payload"]
    assert payload["schema_version"] == "cdl.protocol_integrity.sidecar.v2"
    assert payload["representations"]["lf_sha256"] == FROZEN_LF_SHA256
    assert payload["representations"]["crlf_sha256"] == FROZEN_CRLF_SHA256
    assert payload["canonical_line_ending"] == "lf"
    assert payload["migration"]["id"] == "S02-CP08-05R"
    assert payload["migration"]["rules_unchanged"] is True


def test_parsed_protocol_is_semantically_identical_across_representations():
    lf = semantic_equivalence(PROTOCOL_YAML)
    assert lf["semantically_identical"] is True
    assert lf["parsed_canonical_sha256_lf"] == lf["parsed_canonical_sha256_crlf"]
    # identical to the canonical hash frozen inside the authentic archive
    assert lf["parsed_canonical_sha256_lf"] == FROZEN_CANONICAL_SHA256


def test_reconcile_on_real_repo_keeps_gates_honest():
    report = reconcile(ROOT)
    gates = report["gates"]
    assert report["protocol_integrity"]["protocol"]["status"] == PROTOCOL_VERIFIED
    assert report["protocol_integrity"]["engine"]["status"] == PROTOCOL_VERIFIED
    assert gates["protocol_integrity_gate"] == GATE_PASS
    assert report["archive"]["status"] == ARCHIVE_VERIFIED
    assert gates["archive_gate"] == GATE_PASS
    assert gates["independent_validation"] == "NOT_EVALUATED"
    assert gates["strategic_gate"] == "NO-GO"
    constraints = report["constraints"]
    assert constraints == {
        "protocol_rules_modified": False,
        "original_archive_rewritten": False,
        "v1_sidecar_modified": False,
        "evaluation_performed": False,
        "forward_dataset_used": False,
    }


# ---------------------------------------------------------------------------
# integration with the versioned readiness verifier
# ---------------------------------------------------------------------------


def test_readiness_sidecar_check_passes_on_lf_checkout(tmp_path):
    """End-to-end Sirio simulation at the check that used to fail (CP08-04.2)."""
    root = tmp_path
    for name, endings in (
        ("momentum-independent-validation-v1.yaml", "lf"),
        ("momentum-confirmation-engine-v1.yaml", "lf"),
    ):
        source = PROTOCOL_DIR / name
        target = root / "config" / "protocols" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = source.read_bytes()
        target.write_bytes(normalize_lf(raw) if endings == "lf" else denormalize_crlf(raw))
        for suffix in (".sha256", ".sha256.v2.json"):
            shutil.copyfile(source.with_suffix(".yaml" + suffix), target.with_suffix(".yaml" + suffix))
    paths = ValidationPaths(repo_root=root)
    loaded = load_protocol(paths)
    result = _sidecar_check(paths, loaded)
    assert result["match"] is True
    assert result["status"] == PROTOCOL_VERIFIED
    assert result["representation"] == "LF"
    assert loaded["config_sha256"] == FROZEN_LF_SHA256
    assert loaded["config_sha256_crlf"] == FROZEN_CRLF_SHA256
