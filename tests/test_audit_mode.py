import json
import pandas as pd
import pytest
from crypto_decision_lab.market import HOUR_MS, normalize_klines
from crypto_decision_lab.sync import sync


def candle(t):
    return [t, '100', '105', '95', '101', '10', t + HOUR_MS - 1, '1010', 12, '4', '404', '0']


def test_audit_quarantines_unknown_gap_and_reuses_clean_blocks(tmp_path, monkeypatch):
    # Test storage path without needing optional pyarrow in the test runner.
    monkeypatch.setattr(pd.DataFrame, 'to_parquet', lambda self, path, **kw: self.to_pickle(path))
    monkeypatch.setattr(pd, 'read_parquet', lambda path, **kw: pd.read_pickle(path))
    calls = []
    def fetch(s, e, *, symbol):
        calls.append((s, e))
        times = list(range(s, e, HOUR_MS))
        if len(calls) == 1:
            times.remove(s + HOUR_MS)
        return normalize_klines([candle(t) for t in times])
    a = sync('BTCUSDT', '2024-01-31T00:00:00Z', '2024-02-02T00:00:00Z',
             str(tmp_path), downloader=fetch, mode='audit')
    assert a['quarantined_chunks'] == 1
    assert a['downloaded_chunks'] == 1
    manifest = json.loads(open(a['manifest']).read())
    assert manifest['quality_status'] == 'REQUIRES_REVIEW'
    assert len(manifest['unresolved_chunks'][0]['details']['missing_utc']) == 1
    assert not list((tmp_path / 'chunks').rglob('20240131T0000_20240201T0000.parquet'))
    assert list((tmp_path / 'quarantine').rglob('*.parquet'))
    b = sync('BTCUSDT', '2024-01-31T00:00:00Z', '2024-02-02T00:00:00Z',
             str(tmp_path), downloader=fetch, mode='audit')
    assert b['reused_chunks'] == 1
    assert b['quarantined_chunks'] == 0  # now clean response promoted after new fetch
    assert b['downloaded_chunks'] == 1


def test_strict_rejects_unknown_gap(tmp_path):
    def fetch(s, e, *, symbol):
        return normalize_klines([candle(t) for t in range(s, e, HOUR_MS) if t != s+HOUR_MS])
    with pytest.raises(RuntimeError, match='Bad quality'):
        sync('BTCUSDT', '2024-04-01T00:00:00Z', '2024-04-02T00:00:00Z',
             str(tmp_path), downloader=fetch, mode='strict')
