import pandas as pd
import pytest
from crypto_decision_lab.market import HOUR_MS, normalize_klines, fetch_klines, utc_ms
from crypto_decision_lab.quality import check_quality

START = utc_ms("2024-01-01T00:00:00Z")

def candle(t):
    return [t,"100","105","95","101","10",t+HOUR_MS-1,"1010",12,"4","404","0"]

class Response:
    status_code = 200
    def __init__(self, rows): self.rows=rows
    def raise_for_status(self): pass
    def json(self): return self.rows

class Session:
    def __init__(self, rows): self.rows=rows; self.calls=0
    def get(self, url, params, timeout):
        self.calls += 1
        return Response([r for r in self.rows if params['startTime'] <= r[0] <= params['endTime']][:1000])

def test_good_frame():
    df = normalize_klines([candle(START+i*HOUR_MS) for i in range(3)])
    assert check_quality(df, START, START+3*HOUR_MS)['ok']
    assert str(df.timestamp.dtype) == 'datetime64[ns, UTC]'

def test_gap_is_hard_failure():
    df=normalize_klines([candle(START),candle(START+2*HOUR_MS)])
    quality=check_quality(df, START, START+3*HOUR_MS)
    assert not quality['ok'] and 'missing hours: 1' in str(quality['issues'])

def test_duplicate_is_hard_failure():
    df=normalize_klines([candle(START),candle(START)])
    assert not check_quality(df, START, START+HOUR_MS)['ok']

def test_bad_ohlc_is_hard_failure():
    row=candle(START); row[2]="98"
    assert not check_quality(normalize_klines([row]), START, START+HOUR_MS)['ok']

def test_paginate_1000_limit():
    rows=[candle(START+i*HOUR_MS) for i in range(1003)]
    session=Session(rows)
    df=fetch_klines(START,START+1003*HOUR_MS,session=session,pause=0)
    assert session.calls == 2
    assert check_quality(df, START, START+1003*HOUR_MS)['ok']

def test_utc_required():
    with pytest.raises(ValueError): utc_ms('2024-01-01T00:00:00')

def test_incomplete_end_rejected():
    import time
    boundary = int(time.time()*1000)//HOUR_MS*HOUR_MS
    with pytest.raises(ValueError): fetch_klines(boundary, boundary+HOUR_MS,session=Session([]))
