import json
import pandas as pd
import pytest
from crypto_decision_lab.market import HOUR_MS, normalize_klines, utc_ms
from crypto_decision_lab.sync import sync
from crypto_decision_lab.freeze import inventory, generate_windows, freeze


def candle(t):
    return [t,'100','105','95','101','10',t+HOUR_MS-1,'1010',12,'4','404','0']


def test_freeze_keeps_gap_out_of_context_and_target(tmp_path, monkeypatch):
    # In-memory pickle substituted for Parquet in this isolated test environment.
    monkeypatch.setattr(pd.DataFrame,'to_parquet',lambda self,path,**kw:self.to_pickle(path))
    monkeypatch.setattr(pd,'read_parquet',lambda path,**kw:pd.read_pickle(path))
    s = utc_ms('2024-02-01T00:00:00Z')
    missing = s+4*HOUR_MS
    def fetch(start,end,*,symbol):
        return normalize_klines([candle(t) for t in range(start,end,HOUR_MS) if t!=missing])
    output = sync('BTCUSDT','2024-02-01T00:00:00Z','2024-02-01T12:00:00Z',str(tmp_path/'data'),downloader=fetch,mode='audit')
    source = tmp_path/'data'/'manifests'/f'BTCUSDT_1h_{s}_{s+12*HOUR_MS}.json'
    snapshot = inventory(tmp_path/'data',source)
    assert snapshot['missing_count']==1
    assert snapshot['observed_rows']==11
    windows = generate_windows(snapshot,tmp_path/'data',lookback=3,horizon=2,stride=1)
    assert len(windows)==3  # starts 5,6,7,8; neither context nor target contains hour 4
    assert windows['context_start_utc'].min()==pd.Timestamp('2024-02-01T05:00:00Z')
    dest = tmp_path/'freeze'
    result = freeze(str(tmp_path/'data'),str(source),str(dest),3,2,1)
    assert result['valid_windows']==3
    assert (dest/'anomalies.json').exists()
    with pytest.raises(FileExistsError):
        freeze(str(tmp_path/'data'),str(source),str(dest),3,2,1)
    # Tampering with a source chunk is a hard failure, not a data warning.
    path = tmp_path/'data'/snapshot['chunks'][0]['file']
    with path.open('ab') as f:
        f.write(b'bad')
    with pytest.raises(ValueError,match='Checksum'):
        inventory(tmp_path/'data',source)


def test_gap_can_be_in_future_target_no_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(pd.DataFrame,'to_parquet',lambda self,path,**kw:self.to_pickle(path))
    monkeypatch.setattr(pd,'read_parquet',lambda path,**kw:pd.read_pickle(path))
    s = utc_ms('2024-02-01T00:00:00Z')
    def fetch(start,end,*,symbol):
        return normalize_klines([candle(t) for t in range(start,end,HOUR_MS) if t!=s+3*HOUR_MS])
    sync('BTCUSDT','2024-02-01T00:00:00Z','2024-02-01T06:00:00Z',str(tmp_path/'data'),downloader=fetch,mode='audit')
    source = tmp_path/'data'/'manifests'/f'BTCUSDT_1h_{s}_{s+6*HOUR_MS}.json'
    snapshot = inventory(tmp_path/'data',source)
    assert len(generate_windows(snapshot,tmp_path/'data',3,3))==0
