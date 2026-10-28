"""S02-CP01: zero-shot Timestral against a persistence baseline on one frozen safe window."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
from .sync import digest

HOUR_MS = 3_600_000


def load_window(data_root: Path, snapshot_dir: Path, index: int):
    raw_path = snapshot_dir / 'raw-manifest.json'
    report = json.loads((snapshot_dir / 'report.json').read_text())
    raw = json.loads(raw_path.read_text())
    windows_path = snapshot_dir / 'forecast-windows.parquet'
    if digest(windows_path) != report['windows_parquet_sha256']:
        raise ValueError('Frozen windows index checksum mismatch')
    if raw['source_manifest_sha256'] != report['source_manifest_sha256']:
        raise ValueError('Snapshot and report provenance mismatch')
    windows = pd.read_parquet(windows_path)
    if not 0 <= index < len(windows):
        raise IndexError(f'Window index must be 0..{len(windows)-1}')
    row = windows.iloc[index]
    start = pd.Timestamp(row['context_start_utc'])
    pivot = pd.Timestamp(row['forecast_start_utc'])
    end = pd.Timestamp(row['target_end_exclusive_utc'])
    start_ms, pivot_ms, end_ms = (int(x.timestamp() * 1000) for x in (start, pivot, end))
    frames = []
    for chunk in raw['chunks']:
        if chunk['end_ms'] <= start_ms or chunk['start_ms'] >= end_ms:
            continue
        relative = Path(chunk['file'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Unsafe chunk path in manifest')
        path = (data_root / relative).resolve()
        if not path.is_relative_to(data_root.resolve()):
            raise ValueError('Chunk escapes data root')
        if digest(path) != chunk['sha256']:
            raise ValueError(f'Checksum changed: {path}')
        f = pd.read_parquet(path, columns=['open_time', 'close', 'close_time'])
        frames.append(f.loc[(f.open_time >= start_ms) & (f.open_time < end_ms)])
    if not frames:
        raise ValueError('No source rows for window')
    df = pd.concat(frames).sort_values('open_time').reset_index(drop=True)
    expected = np.arange(start_ms, end_ms, HOUR_MS, dtype=np.int64)
    actual = df['open_time'].to_numpy(dtype=np.int64)
    if not np.array_equal(actual, expected):
        raise ValueError('Window timestamps not contiguous')
    if not np.array_equal(df['close_time'].to_numpy(dtype=np.int64), expected + HOUR_MS - 1):
        raise ValueError('Window has irregular close_time')
    if not np.isfinite(df['close'].to_numpy(dtype=float)).all() or (df['close'] <= 0).any():
        raise ValueError('Invalid close prices')
    context_len = (pivot_ms-start_ms)//HOUR_MS
    horizon_len = (end_ms-pivot_ms)//HOUR_MS
    if context_len != report['lookback_hours'] or horizon_len != report['horizon_hours']:
        raise ValueError('Window dimensions differ from freeze report')
    close = df['close'].to_numpy(dtype=np.float32)
    return close[:context_len], close[context_len:], {'start_utc':start.isoformat(), 'forecast_start_utc':pivot.isoformat(), 'target_end_exclusive_utc':end.isoformat(), 'source_manifest_sha256':raw['source_manifest_sha256'], 'windows_sha256':report['windows_parquet_sha256']}


def evaluate(actual, predicted, last_price):
    actual = np.asarray(actual, dtype=np.float64).reshape(-1)
    predicted = np.asarray(predicted, dtype=np.float64).reshape(-1)
    if actual.shape != predicted.shape or not np.isfinite(predicted).all():
        raise ValueError('Forecast invalid shape or nonfinite output')
    error = predicted-actual
    return {'mae':float(np.abs(error).mean()), 'rmse':float(np.sqrt(np.mean(error**2))), 'final_forecast':float(predicted[-1]), 'final_actual':float(actual[-1]), 'direction_correct_24h':bool((predicted[-1]>last_price)==(actual[-1]>last_price)) if predicted[-1]!=last_price and actual[-1]!=last_price else None}


def run(data_root: Path, snapshot_dir: Path, index: int, output_dir: Path, model_name='mistralai/Timestral-209M'):
    context, actual, provenance = load_window(data_root, snapshot_dir, index)
    from timestral import TimestralForecaster
    import torch
    torch.set_num_threads(min(8, torch.get_num_threads()))
    model = TimestralForecaster.from_pretrained(model_name, device='cpu')
    with torch.inference_mode():
        prediction = model.forecast(context, prediction_length=len(actual))
    def flatten(x):
        if hasattr(x, 'detach'):
            x = x.detach().cpu().numpy()
        x = np.asarray(x, dtype=np.float64).reshape(-1)
        if len(x) != len(actual):
            raise ValueError(f'Expected {len(actual)} forecast values, received {len(x)}')
        return x
    median = flatten(prediction.median)
    p10 = flatten(prediction.quantile(0.1))
    p90 = flatten(prediction.quantile(0.9))
    baseline = np.repeat(float(context[-1]), len(actual))
    metrics = {'timestral': evaluate(actual, median, float(context[-1])), 'naive': evaluate(actual, baseline, float(context[-1])), 'quantile_10_90_empirical_coverage':float(np.mean((actual>=p10)&(actual<=p90))), 'mae_improvement_pct': float(100*(1 - np.mean(np.abs(median-actual))/np.mean(np.abs(baseline-actual)))) if np.mean(np.abs(baseline-actual)) else None}
    result = {'experiment':'S02-CP01', 'model':model_name,'device':'cpu','window_index':index,'provenance':provenance,'lookback':len(context),'horizon':len(actual),'last_context_close':float(context[-1]), 'metrics':metrics}
    if output_dir.exists():
        raise FileExistsError(f'Output exists, not overwriting: {output_dir}')
    output_dir.mkdir(parents=True)
    try:
        pd.DataFrame({'timestamp':pd.date_range(provenance['forecast_start_utc'],periods=len(actual),freq='h',tz='UTC'), 'actual':actual.astype(float), 'timestral_p10':p10, 'timestral_p50':median, 'timestral_p90':p90, 'naive':baseline}).to_parquet(output_dir/'forecast.parquet',index=False)
        result['forecast_sha256'] = digest(output_dir/'forecast.parquet')
        (output_dir/'metrics.json').write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
    except BaseException:
        import shutil
        shutil.rmtree(output_dir)
        raise
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description='Run one Timestral frozen-window comparison with persistence baseline')
    p.add_argument('--data-root',default='data')
    p.add_argument('--snapshot-dir',default='data/snapshots/BTCUSDT-1h-raw-v1')
    p.add_argument('--window-index',type=int,default=0)
    p.add_argument('--output-dir',default='data/experiments/S02-CP01-window-0000')
    p.add_argument('--model',default='mistralai/Timestral-209M')
    args=p.parse_args(argv)
    print(json.dumps(run(Path(args.data_root),Path(args.snapshot_dir),args.window_index,Path(args.output_dir),args.model),indent=2))

if __name__=='__main__':
    main()
