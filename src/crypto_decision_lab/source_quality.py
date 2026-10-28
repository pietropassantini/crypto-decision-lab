"""Strict QC with explicit, exact-match, source exceptions."""
from __future__ import annotations

import pandas as pd
from .market import HOUR_MS
from .quality import check_quality
from .exceptions import KNOWN_SOURCE_EVENTS


def assess_source_quality(df: pd.DataFrame, start_ms: int, end_ms: int, symbol: str) -> dict:
    base = check_quality(df, start_ms, end_ms)
    if base['ok']:
        return {**base, 'status': 'VALID', 'exceptions': []}

    # Exception acceptance is permitted ONLY if all ordinary integrity checks pass
    # after correcting the two specifically acknowledged structural deviations.
    adjusted = df.copy()
    accepted = []
    for event in KNOWN_SOURCE_EVENTS:
        if event['symbol'] != symbol or event['interval'] != '1h':
            continue
        missing_ms = event['missing_open_ms']
        partial_ms = event['partial_open_ms']
        if not (start_ms <= missing_ms < end_ms and start_ms <= partial_ms < end_ms):
            continue
        open_ts = set(df['open_time'].astype('int64').tolist())
        subset = df.loc[df['open_time'] == partial_ms]
        if (missing_ms not in open_ts and len(subset) == 1
            and int(subset.iloc[0]['close_time']) == event['actual_close_ms']):
            # Insert an audit-only placeholder in a COPY for structural validation.
            # Never persist it or expose it to training.
            placeholder = subset.copy()
            placeholder.loc[:, 'open_time'] = missing_ms
            placeholder.loc[:, 'timestamp'] = pd.to_datetime(missing_ms, unit='ms', utc=True)
            placeholder.loc[:, 'close_time'] = missing_ms + HOUR_MS - 1
            adjusted.loc[adjusted['open_time'] == partial_ms, 'close_time'] = partial_ms + HOUR_MS - 1
            adjusted = pd.concat([adjusted, placeholder], ignore_index=True).sort_values('open_time').reset_index(drop=True)
            accepted.append({'event_id': event['id'], 'missing_utc': str(pd.to_datetime(missing_ms,unit='ms',utc=True)),
                             'partial_utc': str(pd.to_datetime(partial_ms,unit='ms',utc=True)),
                             'observed_close_ms': event['actual_close_ms'], 'reason': event['reason']})
    corrected_quality = check_quality(adjusted, start_ms, end_ms) if accepted else base
    passed = bool(accepted) and corrected_quality['ok']
    return {**base, 'ok': passed, 'status': 'ACCEPTED_WITH_WARNINGS' if passed else 'ERROR',
            'exceptions': accepted if passed else [], 'unresolved_issues': [] if passed else base['issues']}
