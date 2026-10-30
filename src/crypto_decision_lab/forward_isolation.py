"""S02-CP08-05: hard separation between the forward dataset and research.

The forward archive (``data/forward/``) is the *only* legal source of
independent evidence for the frozen validation protocol.  It must never be
consumed by the research pipeline (CP07/CP09): no training, no feature build,
no label build, no threshold selection, no policy evaluation.

This module is deliberately dependency-free (stdlib only) so that every
acquisition / preprocessing entry point can import it without cycles:

* :func:`guard_research_path` refuses to read a path inside a forward archive;
* :func:`guard_research_output` refuses to write research artefacts inside a
  forward archive (which would corrupt the immutable evidence);
* :func:`guard_no_evaluation_metrics` refuses strategy metrics that were
  produced from forward data before the validation is explicitly run.

Raising :class:`ForwardIsolationViolation` is always a hard failure: the caller
must abort, never degrade to a warning.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Mapping

FORWARD_ROOT = "data/forward"
FORWARD_ROLE = "independent_forward_collection"
FORWARD_CONSUMER_ROLE = "protocol_validation_only"
RESEARCH_ROLE = "research_forbidden"

# Strategy artefacts that must never be emitted by the collection pipeline.
FORBIDDEN_ARTEFACT_STEMS = (
    "decisions",
    "labels",
    "equity",
    "equity_curve",
    "pnl",
    "profit",
    "signals",
    "trades",
    "backtest",
    "evaluation",
    "verdict",
)

# Metric keys that only a real evaluation may produce.
FORBIDDEN_METRIC_KEYS = (
    "total_return_pct",
    "profit_factor_usdt",
    "max_drawdown_mtm_pct",
    "max_drawdown_exit_pct",
    "ci_return_low_pct",
    "ci_return_high_pct",
    "win_rate_trade_pct",
    "sharpe",
    "mae",
    "rmse",
    "final_equity_usdt",
)


class ForwardIsolationViolation(RuntimeError):
    """Raised when forward data and research data are mixed."""


def _resolve(path: str | Path, repo_root: str | Path | None) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        base = Path(repo_root) if repo_root is not None else Path.cwd()
        candidate = base / candidate
    return candidate.resolve()


def forward_root(repo_root: str | Path | None = None) -> Path:
    """Absolute path of the forward archive root."""

    return _resolve(FORWARD_ROOT, repo_root)


def is_forward_path(path: str | Path, *, repo_root: str | Path | None = None) -> bool:
    """True when ``path`` resolves inside a forward archive.

    The check is deliberately independent of ``repo_root``: any path carrying a
    ``data/forward`` segment (in any repository, any working directory) is
    treated as independent evidence.  A research job pointed at another
    checkout's forward archive is therefore refused too.
    """

    try:
        resolved = _resolve(path, repo_root)
    except OSError:  # pragma: no cover - defensive
        return False
    parts = [part.lower() for part in resolved.parts]
    for index in range(len(parts) - 1):
        if parts[index] == "data" and parts[index + 1] == "forward":
            return True
    root = forward_root(repo_root)
    return resolved == root or resolved.is_relative_to(root)


def guard_research_path(
    path: str | Path,
    *,
    repo_root: str | Path | None = None,
    purpose: str = "research input",
) -> Path:
    """Refuse to use forward data as a research input.  Returns the path otherwise."""

    resolved = _resolve(path, repo_root)
    if is_forward_path(resolved, repo_root=repo_root):
        raise ForwardIsolationViolation(
            f"{purpose} cannot read the forward archive: {resolved} is independent "
            "evidence reserved for the frozen validation protocol (CP08-05)"
        )
    return resolved


def guard_research_output(
    path: str | Path,
    *,
    repo_root: str | Path | None = None,
    purpose: str = "research output",
) -> Path:
    """Refuse to write research artefacts inside the forward archive."""

    resolved = _resolve(path, repo_root)
    if is_forward_path(resolved, repo_root=repo_root):
        raise ForwardIsolationViolation(
            f"{purpose} cannot write inside the forward archive: {resolved}. "
            "The forward dataset is immutable and additive"
        )
    return resolved


def guard_archive_layout(
    path: str | Path, *, repo_root: str | Path | None = None
) -> Path:
    """The forward archive must be disjoint from research data directories."""

    resolved = _resolve(path, repo_root)
    base = (Path(repo_root) if repo_root is not None else Path.cwd()).resolve()
    forbidden = [base / "data" / name for name in ("chunks", "snapshots", "datasets", "experiments", "processed")]
    for directory in forbidden:
        if resolved == directory or resolved.is_relative_to(directory):
            raise ForwardIsolationViolation(
                f"Forward archive cannot live inside research data: {resolved}"
            )
    return resolved


def guard_metric_payload(payload: Mapping[str, object], *, source: str) -> None:
    """Refuse to publish strategy/metrics computed from forward data."""

    keys = {str(key).lower() for key in payload}
    hits = sorted(set(FORBIDDEN_METRIC_KEYS) & keys)
    if hits:
        raise ForwardIsolationViolation(
            f"{source} contains strategy metrics ({', '.join(hits)}): forward data "
            "cannot be evaluated outside the frozen protocol"
        )


def guard_artefact_names(paths: Iterable[str | Path]) -> list[str]:
    """Return forbidden strategy artefact file names found in ``paths``."""

    found: list[str] = []
    for item in paths:
        stem = Path(item).name.split(".")[0].lower()
        if stem in FORBIDDEN_ARTEFACT_STEMS:
            found.append(Path(item).name)
    return found
