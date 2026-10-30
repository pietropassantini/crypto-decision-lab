"""Read-only audit freeze: verify every source object, inventory defects, export safe windows."""
from __future__ import annotations
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
from .market import HOUR_MS, utc_ms
from .forward_isolation import guard_research_output
from .sync import digest
from .quality import check_quality


def _json_atomic(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    try:
        temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _file(root: Path, relative: str) -> Path:
    name = Path(relative)
    if name.is_absolute() or '..' in name.parts:
        raise ValueError(f'Unsafe manifest path: {relative}')
    result = (root / name).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError('File escapes data directory')
    return result


def inventory(root: Path, manifest_path: Path) -> dict:
    """Fail closed on corrupt/missing/overlapping chunks or unexplained OHLC errors."""
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest['interval'] != '1h' or not manifest.get('chunks'):
        raise ValueError('Expected nonempty 1h audit manifest')
    if manifest.get('quality_status') not in ('REQUIRES_REVIEW', 'VALIDATED'):
        raise ValueError('Unexpected manifest status')
    start_ms = utc_ms(manifest['start_utc'])
    end_ms = utc_ms(manifest['end_exclusive_utc'])
    cursor = start_ms
    coverage = []
    anomalies = []
    rows = 0
    hashsum = hashlib.sha256()
    seen_paths = set()
    for entry in manifest['chunks']:
        s, e = int(entry['start_ms']), int(entry['end_ms'])
        if s != cursor or e <= s or (e-s) % HOUR_MS:
            raise ValueError(f'Noncontiguous/unaligned manifest interval: {s} {e}')
        cursor = e
        name = entry['file']
        if name in seen_paths:
            raise ValueError('Duplicate chunk path')
        seen_paths.add(name)
        path = _file(root, name)
        if not path.exists() or digest(path) != entry['sha256']:
            raise ValueError(f'Checksum mismatch or missing chunk: {path}')
        df = pd.read_parquet(path)
        if len(df) != entry['rows']:
            raise ValueError(f'Row count mismatch: {path}')
        if not {'open_time','close_time','timestamp','open','high','low','close','volume'}.issubset(df.columns):
            raise ValueError(f'Invalid schema: {path}')
        ts = df['open_time'].astype('int64')
        if ts.duplicated().any() or not ts.is_monotonic_increasing or (ts % HOUR_MS).any() or ((ts < s) | (ts >= e)).any():
            raise ValueError(f'Timestamps malformed: {path}')
        if df.isna().any().any() or (df[['open','high','low','close']] <= 0).any().any():
            raise ValueError(f'Invalid price/NaN: {path}')
        if (df['high'] < df[['open','close','low']].max(axis=1)).any() or (df['low'] > df[['open','close','high']].min(axis=1)).any() or (df['volume'] < 0).any():
            raise ValueError(f'Invalid OHLCV: {path}')
        times = set(ts.tolist())
        missing = set(range(s, e, HOUR_MS)) - times
        unexpected = set(times) - set(range(s,e,HOUR_MS))
        if unexpected:
            raise ValueError(f'Out of range timestamps: {path}')
        partial = df.loc[df['close_time'].astype('int64') != ts + HOUR_MS - 1]
        known_bad = []
        for t in sorted(missing):
            known_bad.append({'type':'MISSING_CANDLE','timestamp_utc':pd.Timestamp(t,unit='ms',tz='UTC').isoformat(),'timestamp_ms':t,'chunk':name})
        for r in partial.itertuples():
            known_bad.append({'type':'IRREGULAR_CLOSE','timestamp_utc':pd.Timestamp(int(r.open_time),unit='ms',tz='UTC').isoformat(),
                              'timestamp_ms':int(r.open_time),'observed_close_ms':int(r.close_time),'chunk':name})
        # Do not quietly approve an unknown structural defect from a trusted-looking chunk.
        if entry['status'] == 'VALID' and known_bad:
            raise ValueError(f'Unexpected anomaly in VALID chunk: {name}')
        if entry['status'] not in ('VALID','ACCEPTED_WITH_WARNINGS','REQUIRES_REVIEW'):
            raise ValueError(f'Unknown entry status: {entry["status"]}')
        if entry['status'] == 'REQUIRES_REVIEW' and not known_bad:
            raise ValueError(f'Quarantine without recognized structural anomaly: {name}')
        # Check stored per-chunk quality report, if present.
        details = entry.get('details', {})
        if entry['status'] == 'REQUIRES_REVIEW':
            if {x['timestamp_utc'] for x in known_bad if x['type'] == 'MISSING_CANDLE'} != set(details.get('missing_utc', [])):
                raise ValueError(f'Quarantine missing-list mismatch: {name}')
            sidecar = path.with_suffix('.json')
            if not sidecar.exists() or json.loads(sidecar.read_text())['sha256'] != entry['sha256']:
                raise ValueError(f'Quarantine sidecar mismatch: {name}')
        elif entry['status'] == 'ACCEPTED_WITH_WARNINGS' and not entry.get('exceptions'):
            raise ValueError(f'Accepted chunk without registered exception: {name}')
        coverage.append({'file':name,'sha256':entry['sha256'],'status':entry['status'],'rows':len(df),'expected_rows':(e-s)//HOUR_MS,
                         'start_ms':s,'end_ms':e})
        anomalies.extend(known_bad)
        rows += len(df)
        hashsum.update(f'{name}\0{entry["sha256"]}\n'.encode())
    if cursor != end_ms or rows != manifest['total_rows'] or (end_ms-start_ms)//HOUR_MS != manifest['expected_rows']:
        raise ValueError('Global manifest totals or date range mismatch')
    if len([x for x in coverage if x['status']=='REQUIRES_REVIEW']) != len(manifest.get('unresolved_chunks', [])):
        raise ValueError('Quarantined chunk count mismatch')
    if len([x for x in anomalies if x['type']=='MISSING_CANDLE']) != manifest['expected_rows']-rows:
        raise ValueError('Missing hours do not reconcile with total rows')
    anomalies.sort(key=lambda x:(x['timestamp_ms'], x['type']))
    return {'schema_version':1,'dataset_id':f'{manifest["symbol"]}-{manifest["interval"]}-raw-v1',
            'source_manifest':str(manifest_path),'source_manifest_sha256':hashlib.sha256(manifest_bytes).hexdigest(),
            'chunk_chain_sha256':hashsum.hexdigest(),'symbol':manifest['symbol'],'interval':manifest['interval'],
            'start_utc':manifest['start_utc'],'end_exclusive_utc':manifest['end_exclusive_utc'],
            'expected_rows':manifest['expected_rows'],'observed_rows':rows,'missing_count':sum(a['type']=='MISSING_CANDLE' for a in anomalies),
            'irregular_close_count':sum(a['type']=='IRREGULAR_CLOSE' for a in anomalies),'chunks':coverage,'anomalies':anomalies,
            'quality_status':'REQUIRES_REVIEW' if anomalies else 'VALIDATED',
            'policy':'Raw market data unmodified. No gap filling. Forecast windows crossing bad timestamps excluded.'}


def generate_windows(snapshot: dict, root: Path, lookback: int, horizon: int, stride: int = 1) -> pd.DataFrame:
    """Window ending at t uses exactly lookback valid hours; target next horizon hours."""
    if min(lookback,horizon,stride) <= 0:
        raise ValueError('lookback, horizon, stride must be positive')
    s = utc_ms(snapshot['start_utc'])
    e = utc_ms(snapshot['end_exclusive_utc'])
    # We do not materialize synthetic data or aggregate all price frames in RAM.
    # First build a set of verified physical timestamps from checksummed chunks.
    observed = set()
    for ch in snapshot['chunks']:
        path = _file(root,ch['file'])
        if digest(path) != ch['sha256']:
            raise ValueError(f'Chunk changed since freeze: {path}')
        frame = pd.read_parquet(path,columns=['open_time'])
        observed.update(frame['open_time'].astype('int64').tolist())
    excluded = set(a['timestamp_ms'] for a in snapshot['anomalies'])
    # A window is acceptable only if every observation, including the full target period, is real and normal.
    bad = {t for t in range(s,e,HOUR_MS) if t not in observed or t in excluded}
    prefix = [0]
    for t in range(s,e,HOUR_MS):
        prefix.append(prefix[-1] + (t in bad))
    count = (e-s)//HOUR_MS
    records = []
    for offset in range(0,count-lookback-horizon+1,stride):
        if prefix[offset+lookback+horizon] != prefix[offset]:
            continue
        context_start = s + offset*HOUR_MS
        forecast_start = context_start + lookback*HOUR_MS
        target_end = forecast_start + horizon*HOUR_MS
        records.append((pd.Timestamp(context_start,unit='ms',tz='UTC'),
                        pd.Timestamp(forecast_start,unit='ms',tz='UTC'),
                        pd.Timestamp(target_end,unit='ms',tz='UTC')))
    return pd.DataFrame(records,columns=['context_start_utc','forecast_start_utc','target_end_exclusive_utc'])


def freeze(root: str, manifest: str, output: str, lookback: int, horizon: int, stride: int) -> dict:
    base = Path(root).resolve()
    manifest_path = Path(manifest).resolve()
    # S02-CP08-05: the research freeze must not read from or write into the forward archive.
    guard_research_output(base, purpose="research freeze input")
    guard_research_output(output, purpose="research freeze output")
    snapshot = inventory(base,manifest_path)
    out = Path(output)
    if out.exists():
        raise FileExistsError(f'Freeze directory exists, refusing to overwrite: {out}')
    # Generate all artifacts before publishing freeze directory.
    windows = generate_windows(snapshot,base,lookback,horizon,stride)
    out.mkdir(parents=True,exist_ok=False)
    try:
        _json_atomic(out/'raw-manifest.json',snapshot)
        _json_atomic(out/'anomalies.json',{'dataset_id':snapshot['dataset_id'],'events':snapshot['anomalies']})
        windows.to_parquet(out/'forecast-windows.parquet',index=False,engine='pyarrow',compression='zstd')
        report = {'dataset_id':snapshot['dataset_id'],'source_manifest_sha256':snapshot['source_manifest_sha256'],
                  'lookback_hours':lookback,'horizon_hours':horizon,'stride_hours':stride,'valid_windows':len(windows),
                  'windows_parquet_sha256':digest(out/'forecast-windows.parquet'),
                  'data_scope':'INDEX ONLY: no OHLCV copying and no interpolation',
                  'created_utc':datetime.now(timezone.utc).isoformat()}
        _json_atomic(out/'report.json',report)
    except BaseException:
        import shutil
        shutil.rmtree(out)
        raise
    return {'output':str(out),'expected_rows':snapshot['expected_rows'],'observed_rows':snapshot['observed_rows'],
            'missing_count':snapshot['missing_count'],'irregular_close_count':snapshot['irregular_close_count'],
            'valid_windows':len(windows),'source_manifest_sha256':snapshot['source_manifest_sha256']}
