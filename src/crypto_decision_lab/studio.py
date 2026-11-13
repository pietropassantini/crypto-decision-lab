"""S04 Strategy Studio backend (UI-independent).

* **Configurations** are immutable: the identifier contains the SHA-256 of the
  normalized :class:`~crypto_decision_lab.simulation.RunConfig`; files are
  created exclusively and verified on every read.  Saving the same content
  again returns the existing configuration, a changed content gets a new id.
* **Sources** come only from the S02 catalog and only when
  :func:`~crypto_decision_lab.dataset_catalog.assert_research_admissible`
  accepts them; reserved forward collections and their copies are never
  offered nor loaded.
* **Runs** call the S03 engine on the selected period and persist
  ``manifest.json`` (engine manifest + dataset, period, configuration and
  source provenance), the canonical ``ledger.json`` and ``summary.json``
  (metrics recomputed from the ledger by :mod:`.studio_analytics`).
* **Comparison** separates the conditions that must match for two runs to be
  comparable (dataset, inputs, period, execution hypothesis, capital, engine)
  from the object of the comparison (strategy, risk limits).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable

import pandas as pd

from . import studio_analytics as analytics
from .dataset_catalog import (
    DEFAULT_CATALOG_PATH,
    Catalog,
    CatalogError,
    assert_research_admissible,
    load_market,
)
from .execution_contract import ExecutionHypothesis
from .forward_isolation import ForwardIsolationViolation, guard_research_output
from .simulation import RiskLimits, RunConfig, market_events, run

STUDIO_SCHEMA = "cdl.s04.studio.v1"
DEFAULT_STUDIO_ROOT = Path("var/studio")
RUN_MODES = ("backtest", "paper-replay")

# Conditions that must coincide for a like-for-like comparison.
CONDITION_FIELDS = (
    ("dataset_id", "Dataset"),
    ("source_manifest_sha256", "Manifest sorgente (sha256)"),
    ("requested_start", "Periodo richiesto · inizio"),
    ("requested_end_exclusive", "Periodo richiesto · fine (esclusa)"),
    ("first_event", "Primo evento"),
    ("last_event", "Ultimo evento"),
    ("input_digest", "Digest eventi in ingresso"),
    ("hypothesis.latency_hours", "Latenza (h)"),
    ("hypothesis.horizon_hours", "Orizzonte (h)"),
    ("hypothesis.fee_rate", "Fee per lato"),
    ("hypothesis.slippage", "Slippage per lato"),
    ("initial_cash", "Capitale iniziale"),
    ("engine_sha256", "Revisione motore (sha256)"),
)
# What the comparison is about.
SUBJECT_FIELDS = (
    ("strategy", "Strategia"),
    ("risk.max_exposure", "Esposizione massima"),
    ("risk.min_notional", "Nozionale minimo"),
    ("risk.allow_open", "Aperture consentite"),
    ("mode", "Modalità"),
    ("status", "Esito"),
)


class StudioError(ValueError):
    """Raised for invalid Studio requests or tampered Studio artefacts."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug[:40] or "config"


def _write_exclusive(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _utc(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")


def build_run_config(raw: dict) -> RunConfig:
    """Validate a raw configuration through the S03 dataclasses."""

    try:
        return RunConfig(
            strategy=dict(raw.get("strategy") or {"name": "constant"}),
            initial_cash=float(raw.get("initial_cash", 10000.0)),
            hypothesis=ExecutionHypothesis(**dict(raw.get("hypothesis") or {})),
            risk=RiskLimits(**dict(raw.get("risk") or {})),
        )
    except (TypeError, ValueError) as exc:
        raise StudioError(f"configurazione non valida: {exc}") from exc


def normalize_config(raw: dict) -> dict:
    """Canonical JSON-compatible form of a validated configuration."""

    config = asdict(build_run_config(raw))
    config["strategy"] = {
        "name": config["strategy"]["name"],
        "parameters": dict(sorted((config["strategy"].get("parameters") or {}).items())),
    }
    # Same semantics => same digest: 0 and 0.0, 1 and 1.0 are not distinct configurations.
    config["initial_cash"] = float(config["initial_cash"])
    config["hypothesis"]["latency_hours"] = int(config["hypothesis"]["latency_hours"])
    config["hypothesis"]["horizon_hours"] = int(config["hypothesis"]["horizon_hours"])
    for key in ("fee_rate", "slippage"):
        config["hypothesis"][key] = float(config["hypothesis"][key])
    for key in ("max_exposure", "min_notional"):
        config["risk"][key] = float(config["risk"][key])
    config["risk"]["allow_open"] = bool(config["risk"]["allow_open"])
    config["strategy"]["parameters"] = {
        key: float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value
        for key, value in config["strategy"]["parameters"].items()
    }
    return json.loads(_canonical(config))


def config_digest(normalized: dict) -> str:
    return hashlib.sha256(_canonical({"schema": STUDIO_SCHEMA, "config": normalized}).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SavedConfig:
    config_id: str
    name: str
    digest: str
    created_utc: str
    config: dict
    path: Path

    def run_config(self) -> RunConfig:
        return build_run_config(self.config)

    def label(self) -> str:
        strategy = self.config["strategy"]
        params = ", ".join(f"{k}={v}" for k, v in strategy.get("parameters", {}).items())
        return f"{self.name} · {strategy['name']}({params}) · {self.config_id}"


@dataclass(frozen=True)
class SourceStatus:
    dataset_id: str
    role: str
    admitted: bool
    reason: str
    notes: str = ""


@dataclass
class StoredRun:
    run_id: str
    path: Path
    manifest: dict
    ledger: list[dict]
    summary: dict

    @property
    def initial_cash(self) -> float:
        return float(self.manifest["config"]["initial_cash"])

    def label(self) -> str:
        studio = self.manifest.get("studio", {})
        return (
            f"{self.run_id} · {studio.get('config_name', '?')} · {self.manifest.get('source_id')} · "
            f"{str(studio.get('first_event', ''))[:10]}→{str(studio.get('last_event', ''))[:10]}"
        )


class Studio:
    """Filesystem-backed Studio rooted at ``root`` (default ``var/studio``)."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        repo_root: str | Path = ".",
        catalog_path: str | Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        root_path = Path(root) if root is not None else DEFAULT_STUDIO_ROOT
        if not root_path.is_absolute():
            root_path = self.repo_root / root_path
        try:
            self.root = guard_research_output(root_path, purpose="Strategy Studio storage")
        except ForwardIsolationViolation as exc:
            raise StudioError(str(exc)) from exc
        catalog = Path(catalog_path) if catalog_path is not None else DEFAULT_CATALOG_PATH
        self.catalog_path = catalog if catalog.is_absolute() else self.repo_root / catalog
        self.configs_dir = self.root / "configs"
        self.runs_dir = self.root / "runs"

    # ------------------------------------------------------------------ configs
    def save_config(self, name: str, raw: dict) -> SavedConfig:
        if not name or not name.strip():
            raise StudioError("la configurazione richiede un nome")
        normalized = normalize_config(raw)
        digest = config_digest(normalized)
        config_id = f"{_slug(name)}-{digest[:12]}"
        self.configs_dir.mkdir(parents=True, exist_ok=True)
        path = self.configs_dir / f"{config_id}.json"
        if path.exists():
            return self.load_config(config_id)
        record = {
            "schema_version": STUDIO_SCHEMA,
            "config_id": config_id,
            "name": name.strip(),
            "config_sha256": digest,
            "created_utc": _now(),
            "config": normalized,
        }
        _write_exclusive(path, json.dumps(record, indent=2, sort_keys=True) + "\n")
        return self.load_config(config_id)

    def load_config(self, config_id: str) -> SavedConfig:
        path = self.configs_dir / f"{config_id}.json"
        if not path.is_file():
            raise StudioError(f"configurazione non trovata: {config_id}")
        record = json.loads(path.read_text(encoding="utf-8"))
        normalized = normalize_config(record.get("config") or {})
        digest = config_digest(normalized)
        if (
            record.get("schema_version") != STUDIO_SCHEMA
            or record.get("config_sha256") != digest
            or record.get("config_id") != config_id
            or not config_id.endswith(digest[:12])
            or normalized != record.get("config")
        ):
            raise StudioError(f"configurazione alterata o non canonica: {config_id}")
        return SavedConfig(config_id, record["name"], digest, record["created_utc"], normalized, path)

    def list_configs(self) -> list[SavedConfig]:
        if not self.configs_dir.is_dir():
            return []
        configs = []
        for path in sorted(self.configs_dir.glob("*.json")):
            try:
                configs.append(self.load_config(path.stem))
            except (StudioError, json.JSONDecodeError, KeyError):
                continue  # tampered files are never offered
        return sorted(configs, key=lambda item: (item.created_utc, item.config_id))

    # ------------------------------------------------------------------ sources
    def catalog(self) -> Catalog:
        return Catalog.load(self.catalog_path)

    def source_statuses(self) -> list[SourceStatus]:
        catalog = self.catalog()
        statuses = []
        for entry in catalog.entries:
            try:
                assert_research_admissible(catalog, entry.dataset_id, repo_root=self.repo_root)
                statuses.append(SourceStatus(entry.dataset_id, entry.role, True, "ammessa", entry.notes))
            except (CatalogError, ForwardIsolationViolation, OSError) as exc:
                statuses.append(SourceStatus(entry.dataset_id, entry.role, False, str(exc), entry.notes))
        return statuses

    def authorized_sources(self) -> list[str]:
        """Only admitted research sources are exposed by the Studio (CP04.3)."""

        return [status.dataset_id for status in self.source_statuses() if status.admitted]

    def load_market(self, dataset_id: str):
        if dataset_id not in self.authorized_sources():
            raise StudioError(f"fonte non autorizzata per lo Studio: {dataset_id}")
        return load_market(self.catalog(), dataset_id, repo_root=self.repo_root)

    # --------------------------------------------------------------------- runs
    def run_backtest(
        self,
        config_id: str,
        dataset_id: str,
        *,
        start: Any = None,
        end_exclusive: Any = None,
        mode: str = "backtest",
        market=None,
    ) -> StoredRun:
        if mode not in RUN_MODES:
            raise StudioError(f"modalità non supportata: {mode}")
        saved = self.load_config(config_id)
        if market is None:
            market = self.load_market(dataset_id)
        elif dataset_id not in self.authorized_sources() or market.dataset_id != dataset_id:
            raise StudioError(f"dati di mercato non corrispondenti a una fonte autorizzata: {dataset_id}")
        start_ts = _utc(start) if start is not None else None
        end_ts = _utc(end_exclusive) if end_exclusive is not None else None
        if start_ts is not None and end_ts is not None and start_ts >= end_ts:
            raise StudioError("l'inizio del periodo deve precedere la fine")
        events = market_events(market.frame, start_ts, end_ts)
        if not events:
            raise StudioError("nessun evento di mercato nel periodo selezionato")
        result = run(events, saved.run_config(), mode=mode, source_id=dataset_id)
        summary = analytics.summarize(result.ledger, saved.config["initial_cash"])
        manifest = dict(result.manifest)
        manifest["source_manifest_sha256"] = market.source_manifest_sha256
        manifest["loaded_dataset_id"] = market.dataset_id
        manifest["studio"] = {
            "schema_version": STUDIO_SCHEMA,
            "config_id": saved.config_id,
            "config_name": saved.name,
            "config_sha256": saved.digest,
            "dataset_id": dataset_id,
            "requested_start": str(start_ts) if start_ts is not None else None,
            "requested_end_exclusive": str(end_ts) if end_ts is not None else None,
            "first_event": summary["first_event"],
            "last_event": summary["last_event"],
            "events_in_period": len(events),
            "execution_hypothesis": saved.run_config().hypothesis.declaration(),
            "status": result.status,
        }
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        created = _now()
        fingerprint = hashlib.sha256(
            _canonical([saved.digest, manifest["input_digest"], mode, dataset_id, created]).encode()
        ).hexdigest()[:10]
        base_id = f"{created.replace('-', '').replace(':', '')}-{fingerprint}"
        run_id, counter = base_id, 1
        while True:
            run_dir = self.runs_dir / run_id
            try:
                run_dir.mkdir(parents=False, exist_ok=False)
                break
            except FileExistsError:
                counter += 1
                run_id = f"{base_id}-{counter}"
        manifest["studio"]["run_id"] = run_id
        manifest["studio"]["created_utc"] = created
        ledger_text = result.canonical_ledger() + "\n"
        manifest["ledger_sha256"] = hashlib.sha256(ledger_text.encode("utf-8")).hexdigest()
        _write_exclusive(run_dir / "ledger.json", ledger_text)
        _write_exclusive(run_dir / "summary.json", json.dumps(summary, indent=2, sort_keys=True) + "\n")
        _write_exclusive(run_dir / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        return self.load_run(run_id)

    def load_run(self, run_id: str) -> StoredRun:
        run_dir = self.runs_dir / run_id
        if not (run_dir / "manifest.json").is_file():
            raise StudioError(f"run non trovato: {run_id}")
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        ledger_bytes = (run_dir / "ledger.json").read_bytes()
        if hashlib.sha256(ledger_bytes).hexdigest() != manifest.get("ledger_sha256"):
            raise StudioError(f"registro del run alterato: {run_id}")
        ledger = json.loads(ledger_bytes.decode("utf-8"))
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        return StoredRun(run_id, run_dir, manifest, ledger, summary)

    def list_runs(self) -> list[StoredRun]:
        if not self.runs_dir.is_dir():
            return []
        runs = []
        for path in sorted(self.runs_dir.iterdir(), reverse=True):
            if path.is_dir():
                try:
                    runs.append(self.load_run(path.name))
                except (StudioError, json.JSONDecodeError, OSError, KeyError):
                    continue
        return runs


# ----------------------------------------------------------------- comparison
def _field(stored: StoredRun, dotted: str) -> Any:
    manifest = stored.manifest
    studio = manifest.get("studio", {})
    config = manifest.get("config", {})
    if dotted == "strategy":
        strategy = config.get("strategy", {})
        params = ", ".join(f"{k}={v}" for k, v in sorted((strategy.get("parameters") or {}).items()))
        return f"{strategy.get('name')}({params})"
    if dotted in ("hypothesis.latency_hours", "hypothesis.horizon_hours", "hypothesis.fee_rate", "hypothesis.slippage"):
        return config.get("hypothesis", {}).get(dotted.split(".", 1)[1])
    if dotted.startswith("risk."):
        return config.get("risk", {}).get(dotted.split(".", 1)[1])
    if dotted == "initial_cash":
        return config.get("initial_cash")
    if dotted in ("engine_sha256", "input_digest", "source_manifest_sha256", "mode"):
        return manifest.get(dotted)
    return studio.get(dotted)


def compare_runs(runs: Iterable[StoredRun]) -> dict[str, Any]:
    """Like-for-like verdict, differences and side-by-side metrics (CP04.3)."""

    selected = list(runs)
    if len(selected) < 2:
        raise StudioError("servono almeno due run per il confronto")

    def table(fields):
        rows = []
        for key, label in fields:
            values = [_field(item, key) for item in selected]
            rows.append({"key": key, "label": label, "values": values,
                         "differs": len({_canonical(value) for value in values}) > 1})
        return rows

    conditions = table(CONDITION_FIELDS)
    subject = table(SUBJECT_FIELDS)
    differing = [row["label"] for row in conditions if row["differs"]]
    metrics = []
    for item in selected:
        summary = analytics.summarize(item.ledger, item.initial_cash)
        metrics.append({"run_id": item.run_id, **summary})
    return {
        "run_ids": [item.run_id for item in selected],
        "comparable": not differing,
        "differing_conditions": differing,
        "conditions": conditions,
        "subject": subject,
        "metrics": metrics,
    }


def contiguous_segments(index: pd.DatetimeIndex) -> pd.DataFrame:
    """Gap-free hourly segments: the S03 engine halts at the first gap."""

    if len(index) == 0:
        return pd.DataFrame(columns=["start", "end", "hours"])
    ordered = pd.DatetimeIndex(index).sort_values()
    breaks = ordered.to_series().diff() != pd.Timedelta(hours=1)
    group = breaks.cumsum()
    rows = [
        {"start": chunk.index[0], "end": chunk.index[-1], "hours": int(len(chunk))}
        for _, chunk in ordered.to_series().groupby(group.to_numpy())
    ]
    return pd.DataFrame(rows).sort_values("start").reset_index(drop=True)


__all__ = [
    "CONDITION_FIELDS",
    "DEFAULT_STUDIO_ROOT",
    "RUN_MODES",
    "STUDIO_SCHEMA",
    "SavedConfig",
    "SourceStatus",
    "StoredRun",
    "Studio",
    "StudioError",
    "SUBJECT_FIELDS",
    "build_run_config",
    "compare_runs",
    "config_digest",
    "contiguous_segments",
    "normalize_config",
]
