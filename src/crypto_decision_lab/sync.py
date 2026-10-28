"""Resumable monthly UTC collection; verified immutable chunks and atomic manifest."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

import pandas as pd
from .market import HOUR_MS, fetch_klines, utc_ms
from .source_quality import assess_source_quality
from .quality import check_quality


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def boundaries(start: str, end: str):
    s, e = utc_ms(start), utc_ms(end)
    if s >= e or s % HOUR_MS or e % HOUR_MS:
        raise ValueError('Use increasing UTC hour boundaries')
    cursor = pd.Timestamp(s, unit='ms', tz='UTC')
    finish = pd.Timestamp(e, unit='ms', tz='UTC')
    while cursor < finish:
        nxt = min(cursor + pd.offsets.MonthBegin(1), finish)
        # MonthBegin advances to next month's first day, also for first-of-month
        yield int(cursor.timestamp()*1000), int(nxt.timestamp()*1000)
        cursor = nxt


def _assess(frame, start_ms, end_ms, symbol, mode):
    if mode == 'strict':
        q = check_quality(frame, start_ms, end_ms)
        return {**q, 'status': 'VALID' if q['ok'] else 'ERROR', 'exceptions': []}
    return assess_source_quality(frame, start_ms, end_ms, symbol)


def _anomalies(frame, start_ms, end_ms, quality):
    times = set(frame['open_time'].astype('int64').tolist())
    missing = [pd.Timestamp(t, unit='ms', tz='UTC').isoformat()
               for t in range(start_ms, end_ms, HOUR_MS) if t not in times]
    partial = frame.loc[frame['close_time'] != frame['open_time'] + HOUR_MS - 1]
    return {'missing_utc': missing,
            'incorrect_close': [{'open_utc': pd.Timestamp(int(r.open_time), unit='ms', tz='UTC').isoformat(),
                                 'close_time_ms': int(r.close_time)} for r in partial.itertuples()],
            'issues': quality['issues']}


def sync(symbol: str, start: str, end: str, output_dir: str = 'data', *, downloader=fetch_klines,
         mode: str = 'verified') -> dict:
    if mode not in ('strict', 'verified', 'audit'):
        raise ValueError('Unsupported mode')
    if not symbol.isalnum() or symbol.upper() != symbol:
        raise ValueError('symbol must be uppercase alphanumeric')
    root = Path(output_dir)
    chunkdir = root / 'chunks' / symbol / '1h'
    chunkdir.mkdir(parents=True, exist_ok=True)
    entries = []
    fetched = reused = quarantined = 0
    issues = []
    for s, e in boundaries(start, end):
        start_tag = pd.Timestamp(s, unit='ms', tz='UTC').strftime('%Y%m%dT%H%M')
        end_tag = pd.Timestamp(e, unit='ms', tz='UTC').strftime('%Y%m%dT%H%M')
        target = chunkdir / f'{start_tag}_{end_tag}.parquet'
        meta_path = target.with_suffix('.json')
        valid_existing = False
        if target.exists() and meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding='utf-8'))
            if (meta.get('start_ms') == s and meta.get('end_ms') == e
                and meta.get('sha256') == digest(target)):
                quality = _assess(pd.read_parquet(target), s, e, symbol, mode)
                valid_existing = quality['ok'] and (meta.get('quality') == quality or
                        (meta.get('schema_version') == 1 and 'quality' not in meta and quality['status'] == 'VALID') or
                        (mode == 'strict' and meta.get('quality', {}).get('status') == 'VALID'))
            if not valid_existing:
                raise RuntimeError(f'Existing chunk invalid; refusing overwrite: {target}')
        elif target.exists() or meta_path.exists():
            raise RuntimeError(f'Incomplete chunk pair; inspect manually: {target}')
        if valid_existing:
            reused += 1
        else:
            frame = downloader(s, e, symbol=symbol)
            quality = _assess(frame, s, e, symbol, mode)
            if not quality['ok']:
                if mode != 'audit':
                    raise RuntimeError(f'Bad quality for {target.name}: {quality["issues"]}')
                # Audit copies are deliberately NOT in the verified chunks directory.
                quarantine = root / 'quarantine' / symbol / '1h'
                quarantine.mkdir(parents=True, exist_ok=True)
                qpath = quarantine / target.name
                qmeta = qpath.with_suffix('.json')
                details = _anomalies(frame, s, e, quality)
                # Re-download is permitted for unapproved chunks, but do not silently replace
                # an existing quarantined snapshot from a previous audit.
                if not qpath.exists() and not qmeta.exists():
                    temp = qpath.with_suffix('.parquet.tmp')
                    try:
                        frame.to_parquet(temp, index=False, engine='pyarrow', compression='zstd')
                        if not pd.read_parquet(temp).equals(frame):
                            raise RuntimeError('Quarantine Parquet roundtrip failed')
                        os.replace(temp, qpath)
                        qmeta.write_text(json.dumps({'start_ms': s, 'end_ms': e,
                            'sha256': digest(qpath), 'quality': quality,
                            'details': details}, indent=2)+'\n', encoding='utf-8')
                    finally:
                        temp.unlink(missing_ok=True)
                elif not (qpath.exists() and qmeta.exists()
                          and json.loads(qmeta.read_text()).get('sha256') == digest(qpath)):
                    raise RuntimeError(f'Quarantine snapshot corrupted: {qpath}')
                record = {'file': str(qpath.relative_to(root)), 'status': 'REQUIRES_REVIEW',
                          'rows': len(frame), 'expected_rows': (e-s)//HOUR_MS,
                          'start_ms': s, 'end_ms': e, 'details': details,
                          'sha256': digest(qpath)}
                entries.append(record)
                issues.append(record)
                quarantined += 1
                continue
            tmp = target.with_suffix('.parquet.tmp')
            try:
                frame.to_parquet(tmp, index=False, engine='pyarrow', compression='zstd')
                if _assess(pd.read_parquet(tmp), s, e, symbol, mode) != quality:
                    raise RuntimeError('Parquet roundtrip quality failed')
                sha = digest(tmp)
                os.replace(tmp, target)
                meta = {'schema_version': 1, 'provider': 'binance-spot', 'symbol': symbol,
                        'interval': '1h', 'start_ms': s, 'end_ms': e, 'rows': len(frame),
                        'sha256': sha, 'collected_utc': datetime.now(timezone.utc).isoformat(),
                        'quality': quality}
                temp_meta = meta_path.with_suffix('.json.tmp')
                temp_meta.write_text(json.dumps(meta, indent=2) + '\n', encoding='utf-8')
                os.replace(temp_meta, meta_path)
            finally:
                tmp.unlink(missing_ok=True)
            fetched += 1
        entries.append({'file': str(target.relative_to(root)), 'sha256': digest(target),
                        'rows': quality['rows'], 'expected_rows': (e-s)//HOUR_MS,
                        'status': quality['status'], 'exceptions': quality['exceptions'],
                        'start_ms': s, 'end_ms': e})
    manifest = {'schema_version': 1, 'symbol': symbol, 'interval': '1h',
                'start_utc': start, 'end_exclusive_utc': end, 'chunks': entries,
                'total_rows': sum(x['rows'] for x in entries),
                'expected_rows': sum(x['expected_rows'] for x in entries),
                'warnings': [exc for x in entries for exc in x.get('exceptions', [])],
                'mode': mode, 'quality_status': 'REQUIRES_REVIEW' if issues else 'VALIDATED',
                'unresolved_chunks': issues}
    dest = root / 'manifests' / f'{symbol}_1h_{utc_ms(start)}_{utc_ms(end)}.json'
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_manifest = dest.with_suffix('.json.tmp')
    tmp_manifest.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp_manifest, dest)
    return {'manifest': str(dest), 'downloaded_chunks': fetched, 'reused_chunks': reused,
            'total_rows': manifest['total_rows'],
            'expected_rows': manifest['expected_rows'], 'warning_count': len(manifest['warnings']),
            'quarantined_chunks': quarantined, 'quality_status': manifest['quality_status']}
