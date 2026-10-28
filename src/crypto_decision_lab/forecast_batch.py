"""S02-CP02: resumable evaluation of frozen windows; Timestral loaded once."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import numpy as np
import pandas as pd
from .forecast_smoke import load_window, evaluate
from .sync import digest


def sample_indices(total: int, count: int, start: int = 0, stop: int | None = None) -> list[int]:
    stop = total if stop is None else stop
    if not 0 <= start < stop <= total:
        raise ValueError('Invalid selection bounds')
    if not 1 <= count <= stop - start:
        raise ValueError('Count must be between 1 and available windows')
    # Evenly spaced in chronological index, unique and reproducible.
    return np.linspace(start, stop - 1, num=count, dtype=int).tolist()


def summarize(records: list[dict]) -> dict:
    if not records:
        return {'completed': 0}
    tm = np.array([r['metrics']['timestral']['mae'] for r in records], dtype=float)
    nv = np.array([r['metrics']['naive']['mae'] for r in records], dtype=float)
    coverage = np.array([r['metrics']['quantile_10_90_empirical_coverage'] for r in records], dtype=float)
    direction = [r['metrics']['timestral']['direction_correct_24h'] for r in records]
    eligible = [d for d in direction if d is not None]
    return {
        'completed': len(records), 'timestral_mean_mae': float(tm.mean()),
        'naive_mean_mae': float(nv.mean()),
        'mean_mae_improvement_pct': float(100 * (1 - tm.mean() / nv.mean())) if nv.mean() else None,
        'timestral_better_windows': int((tm < nv).sum()),
        'ties': int((tm == nv).sum()),
        'mean_interval_coverage': float(coverage.mean()),
        'direction_accuracy': float(sum(eligible) / len(eligible)) if eligible else None,
        'direction_evaluable': len(eligible),
    }


def run_batch(data_root: Path, snapshot_dir: Path, output_dir: Path,
              count: int, start: int = 0, stop: int | None = None,
              model_name: str = 'mistralai/Timestral-209M', revision: str | None = None) -> dict:
    report = json.loads((snapshot_dir / 'report.json').read_text())
    window_path = snapshot_dir / 'forecast-windows.parquet'
    if digest(window_path) != report['windows_parquet_sha256']:
        raise ValueError('Window index checksum mismatch')
    total = len(pd.read_parquet(window_path, columns=['forecast_start_utc']))
    indices = sample_indices(total, count, start, stop)
    cfg = {'schema_version': 1, 'experiment': 'S02-CP02', 'model': model_name,
           'revision': revision, 'device': 'cpu', 'window_indices': indices,
           'source_manifest_sha256': report['source_manifest_sha256'],
           'windows_sha256': report['windows_parquet_sha256'],
           'lookback': report['lookback_hours'], 'horizon': report['horizon_hours']}
    output_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = output_dir / 'config.json'
    if cfg_path.exists():
        if json.loads(cfg_path.read_text()) != cfg:
            raise ValueError('Existing run config differs; choose a new output directory')
    else:
        if any(output_dir.iterdir()):
            raise ValueError('Nonempty output directory without config; refusing to mix runs')
        cfg_path.write_text(json.dumps(cfg, indent=2) + '\n')
    records = []
    pending = []
    for i in indices:
        folder = output_dir / f'window-{i:04d}'
        metrics_path, forecast_path = folder / 'metrics.json', folder / 'forecast.parquet'
        if folder.exists():
            if not metrics_path.is_file() or not forecast_path.is_file():
                raise ValueError(f'Incomplete existing window: {folder} (inspect manually)')
            rec = json.loads(metrics_path.read_text())
            if rec.get('window_index') != i or rec.get('model') != model_name or rec.get('forecast_sha256') != digest(forecast_path) or rec.get('provenance', {}).get('windows_sha256') != cfg['windows_sha256']:
                raise ValueError(f'Existing result invalid: {folder}')
            records.append(rec)
        else:
            pending.append(i)
    if pending:
        import torch
        from timestral import TimestralForecaster
        torch.set_num_threads(min(8, torch.get_num_threads()))
        kwargs = {'device': 'cpu'}
        if revision is not None:
            kwargs['revision'] = revision
        model = TimestralForecaster.from_pretrained(model_name, **kwargs)
        for num, i in enumerate(pending, 1):
            context, actual, provenance = load_window(data_root, snapshot_dir, i)
            with torch.inference_mode():
                pred = model.forecast(context, prediction_length=len(actual))
            def flat(v):
                if hasattr(v, 'detach'):
                    v = v.detach().cpu().numpy()
                out = np.asarray(v, dtype=np.float64).reshape(-1)
                if len(out) != len(actual) or not np.isfinite(out).all():
                    raise ValueError(f'Invalid prediction for window {i}')
                return out
            p50, p10, p90 = flat(pred.median), flat(pred.quantile(0.1)), flat(pred.quantile(0.9))
            if np.any(p10 > p50) or np.any(p50 > p90):
                raise ValueError(f'Quantiles out of order in window {i}')
            naive = np.full(len(actual), float(context[-1]))
            nv_mae = np.mean(np.abs(naive - actual))
            metrics = {
                'timestral': evaluate(actual, p50, float(context[-1])),
                'naive': evaluate(actual, naive, float(context[-1])),
                'quantile_10_90_empirical_coverage': float(np.mean((actual >= p10) & (actual <= p90))),
                'mae_improvement_pct': float(100*(1-np.mean(np.abs(p50-actual))/nv_mae)) if nv_mae else None,
            }
            rec = {'experiment': 'S02-CP02', 'model': model_name, 'revision_requested': revision,
                   'window_index': i, 'device': 'cpu', 'provenance': provenance,
                   'last_context_close': float(context[-1]), 'metrics': metrics}
            folder = output_dir / f'window-{i:04d}'
            folder.mkdir(exist_ok=False)
            try:
                frame = pd.DataFrame({'timestamp': pd.date_range(provenance['forecast_start_utc'], periods=len(actual), freq='h', tz='UTC'),
                                      'actual': actual.astype(float), 'timestral_p10': p10,
                                      'timestral_p50': p50, 'timestral_p90': p90, 'naive': naive})
                frame.to_parquet(folder/'forecast.parquet', index=False)
                rec['forecast_sha256'] = digest(folder/'forecast.parquet')
                (folder/'metrics.json').write_text(json.dumps(rec, indent=2) + '\n')
            except BaseException:
                import shutil
                shutil.rmtree(folder)
                raise
            records.append(rec)
            print(f'[{num}/{len(pending)}] window {i}: MAE {metrics["timestral"]["mae"]:.2f} / naive {metrics["naive"]["mae"]:.2f}', flush=True)
    records.sort(key=lambda x: x['window_index'])
    result = {'config': cfg, 'summary': summarize(records)}
    tmp = output_dir / 'summary.json.tmp'
    tmp.write_text(json.dumps(result, indent=2) + '\n')
    os.replace(tmp, output_dir / 'summary.json')
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description='CPU Timestral batch evaluation, resumable with hashes')
    p.add_argument('--data-root', default='data')
    p.add_argument('--snapshot-dir', default='data/snapshots/BTCUSDT-1h-raw-v1')
    p.add_argument('--output-dir', default='data/experiments/S02-CP02-batch-100')
    p.add_argument('--count', type=int, default=100)
    p.add_argument('--start', type=int, default=0)
    p.add_argument('--stop', type=int)
    p.add_argument('--model', default='mistralai/Timestral-209M')
    p.add_argument('--revision', help='Optional exact Hugging Face model revision/commit')
    args = p.parse_args(argv)
    result = run_batch(Path(args.data_root), Path(args.snapshot_dir), Path(args.output_dir),
                       args.count, args.start, args.stop, args.model, args.revision)
    print(json.dumps(result['summary'], indent=2))

if __name__ == '__main__':
    main()
