"""S02-CP08-05R: versioned reconciliation of the frozen protocol integrity.

Incident
--------
The S02-CP08-04 protocol was frozen on a Windows working tree whose YAML file
used CRLF line endings.  The original sidecar (v1) therefore pins the SHA-256 of
the *CRLF* byte sequence.  ``.gitattributes`` normalises ``config/protocols``
to LF on check-in, so a Linux / ``eol=lf`` checkout (Sirio) materialises the
*LF* byte sequence: a different SHA-256, and the readiness check CP08-04.2
failed even though no rule of the protocol had changed.

Reconciliation policy (explicit and versioned, never silent)
------------------------------------------------------------
* The v1 sidecar is kept byte-identical: it is historical evidence and the
  integrity anchor of the freeze.
* A **v2 sidecar** (this module, schema ``cdl.protocol_integrity.sidecar.v2``)
  records BOTH representations of the same frozen content:

    - ``crlf_sha256``: the original freeze hash (identical to the v1 sidecar);
    - ``lf_sha256``:   the canonical representation stored by Git and used on
      Linux checkouts.

* A file verifies as ``PROTOCOL_VERIFIED`` only when its bytes are exactly one
  of the two pinned representations AND the other pinned representation can be
  reproduced from it by changing *line endings only* (byte-level proof).
* Any other difference is a real mismatch (``PROTOCOL_TAMPERED``): the verifier
  never "fixes" a sidecar and never accepts unexplained bytes.
* Archive presence is a SEPARATE control (``ARCHIVE_VERIFIED`` /
  ``ARCHIVE_MISSING`` / ``ARCHIVE_CORRUPT``): a missing archive blocks the
  archive gate, it does not invalidate the protocol and vice versa.

This module performs no evaluation, no training and no trading; the independent
validation verdict stays ``NOT_EVALUATED`` and the strategic gate stays ``NO-GO``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .sync import digest

INTEGRITY_SCHEMA = "cdl.protocol_integrity.sidecar.v2"
MIGRATION_ID = "S02-CP08-05R"

PROTOCOL_VERIFIED = "PROTOCOL_VERIFIED"
PROTOCOL_TAMPERED = "PROTOCOL_TAMPERED"
SIDECAR_V2_MISSING = "SIDECAR_V2_MISSING"
SIDECAR_V2_INCONSISTENT = "SIDECAR_V2_INCONSISTENT"

ARCHIVE_VERIFIED = "ARCHIVE_VERIFIED"
ARCHIVE_MISSING = "ARCHIVE_MISSING"
ARCHIVE_CORRUPT = "ARCHIVE_CORRUPT"

GATE_PASS = "PASS"
GATE_BLOCKED = "BLOCKED"
GATE_FAIL = "FAIL"

VERDICT_NOT_EVALUATED = "NOT_EVALUATED"
STRATEGIC_GATE = "NO-GO"

DEFAULT_PROTOCOL_CONFIG = Path("config/protocols/momentum-independent-validation-v1.yaml")
DEFAULT_ENGINE_CONFIG = Path("config/protocols/momentum-confirmation-engine-v1.yaml")
DEFAULT_ARCHIVE_DIR = Path("data/experiments/S02-CP08-independent-validation-protocol-v1")

V2_SUFFIX = ".sha256.v2.json"


# ---------------------------------------------------------------------------
# line-ending primitives
# ---------------------------------------------------------------------------


def normalize_lf(raw: bytes) -> bytes:
    """Canonical LF representation (what Git stores for text eol=lf artifacts)."""
    return raw.replace(b"\r\n", b"\n")


def denormalize_crlf(raw: bytes) -> bytes:
    """CRLF representation (the original Windows freeze form)."""
    return normalize_lf(raw).replace(b"\n", b"\r\n")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def line_ending_profile(raw: bytes) -> dict[str, int]:
    crlf = raw.count(b"\r\n")
    return {
        "bytes": len(raw),
        "crlf_pairs": crlf,
        "lone_cr": raw.count(b"\r") - crlf,
        "lone_lf": raw.count(b"\n") - crlf,
    }


def representation_evidence(raw: bytes) -> dict[str, Any]:
    """Byte-level evidence that two hashes differ by line endings only.

    ``only_line_endings`` is True iff the file is *exactly* one of the two pure
    forms (all LF, or all CRLF): re-encoding the canonical LF form back to CRLF
    must reproduce the file byte-for-byte (and vice versa).  Anything else
    (mixed endings, lone CR, real edits) fails this proof.
    """
    lf = normalize_lf(raw)
    crlf = denormalize_crlf(raw)
    return {
        "profile": line_ending_profile(raw),
        "raw_sha256": _sha256(raw),
        "lf_sha256": _sha256(lf),
        "crlf_sha256": _sha256(crlf),
        "lf_bytes": len(lf),
        "crlf_bytes": len(crlf),
        "is_pure_lf_form": raw == lf,
        "is_pure_crlf_form": raw == crlf,
        "only_line_endings": raw in (lf, crlf),
        "round_trip_lf_to_crlf_to_lf": normalize_lf(denormalize_crlf(lf)) == lf,
        "round_trip_crlf_to_lf_to_crlf": denormalize_crlf(normalize_lf(crlf)) == crlf,
    }


# ---------------------------------------------------------------------------
# sidecars
# ---------------------------------------------------------------------------


def v1_sidecar_path(config_file: Path) -> Path:
    return config_file.with_suffix(config_file.suffix + ".sha256")


def v2_sidecar_path(config_file: Path) -> Path:
    return config_file.with_suffix(config_file.suffix + V2_SUFFIX)


def read_sidecar_v1(config_file: Path) -> dict[str, Any]:
    """Read the original single-hash sidecar (untouched historical evidence)."""
    sidecar = v1_sidecar_path(config_file)
    if not sidecar.is_file():
        return {"path": str(sidecar), "sidecar_present": False, "declared_sha256": None}
    declared = sidecar.read_text(encoding="utf-8").strip().split()[0]
    return {
        "path": str(sidecar),
        "sidecar_present": True,
        "declared_sha256": declared,
        "sidecar_sha256": digest(sidecar),
    }


def read_sidecar_v2(config_file: Path) -> dict[str, Any]:
    sidecar = v2_sidecar_path(config_file)
    if not sidecar.is_file():
        return {"path": str(sidecar), "sidecar_present": False}
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"path": str(sidecar), "sidecar_present": True, "parse_error": str(exc)}
    return {"path": str(sidecar), "sidecar_present": True, "payload": payload}


def write_sidecar_v2(
    config_file: Path,
    *,
    frozen_utc: str | None = None,
    reason: str,
) -> dict[str, Any]:
    """Explicit, versioned migration: record BOTH representations. Never overwrites.

    Refuses to run unless the current bytes are a pure line-ending form of the
    content anchored by the v1 sidecar: hashes are never invented.
    """
    pre = verify_protocol_integrity(config_file)
    if pre["status"] == PROTOCOL_TAMPERED:
        raise ValueError(
            f"Refusing to migrate {config_file}: bytes are not a pure EOL form of "
            "the frozen content (see evidence)."
        )
    if pre["status"] == PROTOCOL_VERIFIED:
        raise FileExistsError(
            f"v2 sidecar already exists and is consistent: {v2_sidecar_path(config_file)}"
        )
    if pre["status"] != SIDECAR_V2_MISSING:
        raise ValueError(f"Unexpected pre-migration state: {pre['status']}")

    sidecar = v2_sidecar_path(config_file)
    payload = {
        "schema_version": INTEGRITY_SCHEMA,
        "artifact": config_file.name,
        "hash_algorithm": "sha256",
        "frozen_utc": frozen_utc,
        "representations": {
            "crlf_sha256": pre["crlf_sha256"],
            "lf_sha256": pre["lf_sha256"],
        },
        "canonical_line_ending": "lf",
        "canonical_rationale": (
            "Git stores this artifact with text eol=lf; LF is the canonical "
            "representation on Linux checkouts. The CRLF hash is preserved as "
            "the original freeze evidence and remains verifiable byte-for-byte."
        ),
        "original_sidecar": v1_sidecar_path(config_file).name,
        "original_sidecar_sha256": pre["v1"]["sidecar_sha256"],
        "migration": {
            "id": MIGRATION_ID,
            "reason": reason,
            "rules_unchanged": True,
            "note": (
                "No rule, value or comment of the frozen protocol was modified: "
                "the two hashes differ exclusively in line-ending encoding."
            ),
        },
    }
    sidecar.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    post = verify_protocol_integrity(config_file)
    if post["status"] != PROTOCOL_VERIFIED:
        sidecar.unlink(missing_ok=True)
        raise ValueError(f"Post-migration verification failed: {post['status']}")
    return post


# ---------------------------------------------------------------------------
# protocol integrity (separate from archive presence)
# ---------------------------------------------------------------------------


def verify_protocol_integrity(config_file: Path) -> dict[str, Any]:
    """Verify the frozen protocol file against the v1 anchor and the v2 sidecar.

    Statuses:
      * ``PROTOCOL_VERIFIED``      - bytes are a pinned representation, both
                                     hashes mutually consistent, rules untouched.
      * ``PROTOCOL_TAMPERED``      - the content no longer matches the frozen
                                     anchor (difference is NOT line endings, or
                                     the v1 sidecar no longer pins the freeze).
      * ``SIDECAR_V2_MISSING``     - v1-only: LF checkouts cannot verify yet.
      * ``SIDECAR_V2_INCONSISTENT``- the v2 sidecar disagrees with the derived
                                     hashes (sidecar tampering).
    """
    raw = config_file.read_bytes()
    evidence = representation_evidence(raw)
    v1 = read_sidecar_v1(config_file)
    v2 = read_sidecar_v2(config_file)

    result: dict[str, Any] = {
        "config_file": str(config_file),
        "representation": ("CRLF" if evidence["is_pure_crlf_form"] else "LF")
        if evidence["only_line_endings"]
        else "UNKNOWN",
        "crlf_sha256": evidence["crlf_sha256"],
        "lf_sha256": evidence["lf_sha256"],
        "raw_sha256": evidence["raw_sha256"],
        "byte_level_evidence": evidence,
        "v1": v1,
        "v2_present": bool(v2.get("sidecar_present")),
    }

    v1_declared = v1.get("declared_sha256")
    if not v1.get("sidecar_present") or v1_declared is None:
        result.update(
            status=PROTOCOL_TAMPERED,
            reason="The original v1 sidecar is missing: no frozen anchor to verify against.",
        )
        return result

    if not evidence["only_line_endings"]:
        result.update(
            status=PROTOCOL_TAMPERED,
            reason=(
                "Bytes are not a pure line-ending form of the frozen content "
                "(mixed endings, lone CR, or real content difference)."
            ),
        )
        return result

    if v1_declared.lower() != evidence["crlf_sha256"]:
        result.update(
            status=PROTOCOL_TAMPERED,
            reason=(
                "The CRLF representation no longer matches the hash pinned by the "
                "original v1 sidecar: the frozen content itself has changed."
            ),
        )
        return result

    if not v2.get("sidecar_present"):
        result.update(
            status=SIDECAR_V2_MISSING,
            reason=(
                "Only the v1 sidecar exists: it pins the CRLF freeze form, so an "
                "LF (Git/Linux) working tree cannot be verified. Run the explicit "
                f"{MIGRATION_ID} migration to record both representations."
            ),
        )
        return result

    payload = v2.get("payload")
    if payload is None or not isinstance(payload, dict):
        result.update(
            status=SIDECAR_V2_INCONSISTENT,
            reason=f"v2 sidecar is unreadable: {v2.get('parse_error', 'not a JSON object')}",
        )
        return result

    reps = payload.get("representations", {})
    problems = []
    if payload.get("schema_version") != INTEGRITY_SCHEMA:
        problems.append(f"schema_version != {INTEGRITY_SCHEMA}")
    if payload.get("hash_algorithm") != "sha256":
        problems.append("hash_algorithm != sha256")
    if str(reps.get("crlf_sha256", "")).lower() != evidence["crlf_sha256"]:
        problems.append("crlf_sha256 does not match the derived CRLF hash")
    if str(reps.get("lf_sha256", "")).lower() != evidence["lf_sha256"]:
        problems.append("lf_sha256 does not match the derived LF hash")
    if problems:
        result.update(
            status=SIDECAR_V2_INCONSISTENT,
            reason="v2 sidecar disagrees with the file: " + "; ".join(problems),
        )
        return result

    result.update(
        status=PROTOCOL_VERIFIED,
        reason=(
            "Working-tree bytes are exactly one of the two pinned representations "
            "of the frozen content; the other is reproduced by line-ending "
            "conversion only; the v1 anchor still pins the original freeze."
        ),
    )
    return result


def semantic_equivalence(config_file: Path) -> dict[str, Any]:
    """Prove the parsed configuration is identical under both representations."""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - dashboard extra guard
        raise ValueError("PyYAML is required for the semantic equivalence proof") from exc
    raw = config_file.read_bytes()
    lf_doc = yaml.safe_load(normalize_lf(raw).decode("utf-8"))
    crlf_doc = yaml.safe_load(denormalize_crlf(raw).decode("utf-8"))
    canonical = json.dumps(lf_doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    canonical_crlf = json.dumps(crlf_doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        "config_file": str(config_file),
        "parsed_canonical_sha256_lf": _sha256(canonical.encode("utf-8")),
        "parsed_canonical_sha256_crlf": _sha256(canonical_crlf.encode("utf-8")),
        "semantically_identical": canonical == canonical_crlf,
    }


# ---------------------------------------------------------------------------
# archive presence and integrity (separate control)
# ---------------------------------------------------------------------------


def verify_archive(directory: Path) -> dict[str, Any]:
    """Verify the frozen CP08-04 archive without ever rewriting it.

    Statuses: ``ARCHIVE_VERIFIED`` / ``ARCHIVE_MISSING`` / ``ARCHIVE_CORRUPT``.
    No hash or content is ever invented: only the authentic manifest pins them.
    """
    directory = Path(directory)
    base: dict[str, Any] = {"directory": str(directory)}
    if not directory.is_dir():
        return {**base, "status": ARCHIVE_MISSING, "reason": "Archive directory absent."}
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        return {**base, "status": ARCHIVE_MISSING, "reason": "manifest.json absent: archive not recoverable."}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {**base, "status": ARCHIVE_CORRUPT, "reason": f"manifest.json unreadable: {ex}"}
    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict) or not outputs:
        return {**base, "status": ARCHIVE_CORRUPT, "reason": "manifest has no output hash inventory."}

    files: list[dict[str, Any]] = []
    mismatches: list[str] = []
    for name, meta in sorted(outputs.items()):
        path = directory / name
        if not path.is_file():
            mismatches.append(name)
            files.append({"file": name, "present": False, "match": False})
            continue
        actual = digest(path)
        match = actual == meta.get("sha256")
        if not match:
            mismatches.append(name)
        files.append(
            {
                "file": name,
                "present": True,
                "expected_sha256": meta.get("sha256"),
                "actual_sha256": actual,
                "match": match,
            }
        )
    protocol_meta = manifest.get("protocol", {})
    result = {
        **base,
        "status": ARCHIVE_VERIFIED if not mismatches else ARCHIVE_CORRUPT,
        "manifest_sha256": digest(manifest_path),
        "created_utc": manifest.get("created_utc"),
        "technical_gate": manifest.get("technical_gate"),
        "strategic_gate": manifest.get("strategic_gate"),
        "verdict": manifest.get("verdict"),
        "protocol_config_sha256": protocol_meta.get("config_sha256"),
        "protocol_canonical_sha256": protocol_meta.get("canonical_sha256"),
        "outputs_total": len(outputs),
        "outputs_matched": len(outputs) - len(mismatches),
        "mismatched": mismatches,
        "files": files,
    }
    if mismatches:
        result["reason"] = "One or more archived files fail the hashes pinned by their own manifest."
    return result


# ---------------------------------------------------------------------------
# reconciliation report
# ---------------------------------------------------------------------------


def _gate_for(status: str) -> str:
    if status == PROTOCOL_VERIFIED:
        return GATE_PASS
    if status in (ARCHIVE_VERIFIED,):
        return GATE_PASS
    if status == ARCHIVE_MISSING:
        return GATE_BLOCKED
    return GATE_FAIL


def reconcile(
    repo_root: Path | str = ".",
    *,
    protocol_config: Path | str = DEFAULT_PROTOCOL_CONFIG,
    engine_config: Path | str = DEFAULT_ENGINE_CONFIG,
    archive_dir: Path | str = DEFAULT_ARCHIVE_DIR,
) -> dict[str, Any]:
    """Full reconciliation: protocol integrity, semantic identity, archive state."""
    root = Path(repo_root)

    def _p(value: Path | str) -> Path:
        candidate = Path(value)
        return candidate if candidate.is_absolute() else root / candidate

    protocol = verify_protocol_integrity(_p(protocol_config))
    engine = verify_protocol_integrity(_p(engine_config))
    semantics = {
        "protocol": semantic_equivalence(_p(protocol_config)),
        "engine": semantic_equivalence(_p(engine_config)),
    }
    archive = verify_archive(_p(archive_dir))

    protocol_gate = (
        GATE_PASS
        if protocol["status"] == PROTOCOL_VERIFIED and engine["status"] == PROTOCOL_VERIFIED
        else GATE_FAIL
    )
    archive_gate = _gate_for(archive["status"])

    return {
        "schema_version": "cdl.cp08_05r.protocol_integrity_reconciliation.v1",
        "migration_id": MIGRATION_ID,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "protocol_integrity": {"protocol": protocol, "engine": engine},
        "semantic_equivalence": semantics,
        "archive": archive,
        "gates": {
            "protocol_integrity_gate": protocol_gate,
            "archive_gate": archive_gate,
            "independent_validation": VERDICT_NOT_EVALUATED,
            "strategic_gate": STRATEGIC_GATE,
        },
        "constraints": {
            "protocol_rules_modified": False,
            "original_archive_rewritten": False,
            "v1_sidecar_modified": False,
            "evaluation_performed": False,
            "forward_dataset_used": False,
        },
    }


def _write_evidence(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n"
    path.write_text(payload, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S02-CP08-05R protocol integrity reconciliation")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo-root", default=".")

    mig = sub.add_parser("migrate", parents=[common], help="explicitly write the v2 dual-hash sidecars")
    mig.add_argument("--config", default=str(DEFAULT_PROTOCOL_CONFIG))
    mig.add_argument("--engine-config", default=str(DEFAULT_ENGINE_CONFIG))
    mig.add_argument("--frozen-utc", default=None)

    ver = sub.add_parser("verify", parents=[common], help="verify one frozen configuration file")
    ver.add_argument("--config", default=str(DEFAULT_PROTOCOL_CONFIG))

    rec = sub.add_parser("reconcile", parents=[common], help="full reconciliation report")
    rec.add_argument("--config", default=str(DEFAULT_PROTOCOL_CONFIG))
    rec.add_argument("--engine-config", default=str(DEFAULT_ENGINE_CONFIG))
    rec.add_argument("--archive-dir", default=str(DEFAULT_ARCHIVE_DIR))
    rec.add_argument("--write-evidence", default=None, help="write the JSON evidence file")

    args = parser.parse_args(argv)
    root = Path(args.repo_root)

    if args.command == "migrate":
        results = [
            write_sidecar_v2(
                root / args.config,
                frozen_utc=args.frozen_utc,
                reason=(
                    "S02-CP08-05R: Git (text eol=lf) materialises the LF form of the "
                    "frozen YAML while the original sidecar pinned the CRLF freeze. "
                    "Both representations of the unchanged v1.0.0 content are now recorded."
                ),
            ),
            write_sidecar_v2(
                root / args.engine_config,
                frozen_utc=args.frozen_utc,
                reason=(
                    "S02-CP08-05R: same line-ending reconciliation applied to the "
                    "frozen confirmation-engine specification."
                ),
            ),
        ]
        print(json.dumps([{"status": r["status"], "lf_sha256": r["lf_sha256"], "crlf_sha256": r["crlf_sha256"]} for r in results], indent=2))
        return 0

    if args.command == "verify":
        result = verify_protocol_integrity(root / args.config)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result["status"] == PROTOCOL_VERIFIED else 2

    report = reconcile(
        root,
        protocol_config=Path(args.config),
        engine_config=Path(args.engine_config),
        archive_dir=Path(args.archive_dir),
    )
    if args.write_evidence:
        _write_evidence(Path(args.write_evidence), report)
        report["evidence_written"] = str(args.write_evidence)
    summary = {
        "protocol_integrity_gate": report["gates"]["protocol_integrity_gate"],
        "protocol_status": report["protocol_integrity"]["protocol"]["status"],
        "engine_status": report["protocol_integrity"]["engine"]["status"],
        "protocol_sha256": {
            "lf": report["protocol_integrity"]["protocol"]["lf_sha256"],
            "crlf": report["protocol_integrity"]["protocol"]["crlf_sha256"],
        },
        "semantically_identical": all(
            block["semantically_identical"] for block in report["semantic_equivalence"].values()
        ),
        "archive_status": report["archive"]["status"],
        "archive_gate": report["gates"]["archive_gate"],
        "independent_validation": VERDICT_NOT_EVALUATED,
        "strategic_gate": STRATEGIC_GATE,
    }
    print(json.dumps(summary, indent=2))
    ok = report["gates"]["protocol_integrity_gate"] == GATE_PASS
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
