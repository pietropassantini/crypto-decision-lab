import json
import pandas as pd
import pytest
from crypto_decision_lab.market import HOUR_MS, normalize_klines, utc_ms
from crypto_decision_lab.sync import boundaries, sync

def candle(t):
    return [t,'100','105','95','101','10',t+HOUR_MS-1,'1010',12,'4','404','0']

def mock_fetch(start_ms, end_ms, *, symbol):
    assert symbol == 'BTCUSDT'
    return normalize_klines([candle(t) for t in range(start_ms, end_ms, HOUR_MS)])

def test_boundaries_partial_month():
    parts=list(boundaries('2024-01-15T00:00:00Z','2024-03-03T00:00:00Z'))
    assert len(parts)==3
    assert parts[0][1]==utc_ms('2024-02-01T00:00:00Z')
    assert parts[1][1]==utc_ms('2024-03-01T00:00:00Z')

def test_sync_resume_and_checksum(tmp_path):
    a=sync('BTCUSDT','2024-01-31T00:00:00Z','2024-02-02T00:00:00Z',str(tmp_path), downloader=mock_fetch)
    assert a['downloaded_chunks']==2 and a['total_rows']==48
    b=sync('BTCUSDT','2024-01-31T00:00:00Z','2024-02-02T00:00:00Z',str(tmp_path), downloader=lambda *a,**k: pytest.fail('should reuse'))
    assert b['reused_chunks']==2
    manifest=json.loads(open(b['manifest']).read())
    target=tmp_path / manifest['chunks'][0]['file']
    target.write_bytes(target.read_bytes()+b'corrupt')
    with pytest.raises(RuntimeError, match='invalid'):
        sync('BTCUSDT','2024-01-31T00:00:00Z','2024-02-02T00:00:00Z',str(tmp_path), downloader=mock_fetch)


def test_known_source_gap_is_audited_without_fabrication(tmp_path):
    from crypto_decision_lab.exceptions import KNOWN_SOURCE_EVENTS
    ev = KNOWN_SOURCE_EVENTS[0]
    start = '2021-02-11T00:00:00Z'
    end = '2021-02-11T08:00:00Z'

    def known_gap_fetch(start_ms, end_ms, *, symbol):
        values = [candle(t) for t in range(start_ms, end_ms, HOUR_MS) if t != ev['missing_open_ms']]
        for item in values:
            if item[0] == ev['partial_open_ms']:
                item[6] = ev['actual_close_ms']
                item[8] = 0
                item[5] = '0'
                item[9] = '0'
                item[10] = '0'
        return normalize_klines(values)

    a = sync('BTCUSDT', start, end, str(tmp_path), downloader=known_gap_fetch)
    assert a['total_rows'] == 7 and a['expected_rows'] == 8 and a['warning_count'] == 1
    manifest = json.loads(open(a['manifest']).read())
    assert manifest['chunks'][0]['status'] == 'ACCEPTED_WITH_WARNINGS'
    saved = pd.read_parquet(tmp_path / manifest['chunks'][0]['file'])
    assert len(saved) == 7
    assert ev['missing_open_ms'] not in set(saved.open_time)
    assert int(saved.loc[saved.open_time == ev['partial_open_ms'], 'close_time'].iloc[0]) == ev['actual_close_ms']
    b = sync('BTCUSDT', start, end, str(tmp_path), downloader=lambda *a,**k: pytest.fail('should reuse'))
    assert b['reused_chunks'] == 1


def test_unknown_missing_hour_is_rejected(tmp_path):
    def bad_fetch(start_ms, end_ms, *, symbol):
        return normalize_klines([candle(t) for t in range(start_ms, end_ms, HOUR_MS) if t != start_ms + HOUR_MS])
    with pytest.raises(RuntimeError, match='Bad quality'):
        sync('BTCUSDT', '2024-04-01T00:00:00Z', '2024-04-02T00:00:00Z', str(tmp_path), downloader=bad_fetch)
