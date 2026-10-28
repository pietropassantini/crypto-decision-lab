"""S02-CP04: horizon-specific residual targets from archived forecast experiments.

Only forecast-time information goes in features.parquet. Actuals, errors and
post-hoc scores go in targets.parquet. No Timestral inference required.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
from .sync import digest

HORIZONS = (4, 12, 24)
FEATURES = ('window_index', 'forecast_start_utc', 'last_context_close',
            'volatility_168h', 'trend_168h', 'momentum_24h',
            'forecast_return_24h', 'interval_width_pct', 'mean_interval_width_pct')
TARGETS = ('window_index', 'forecast_start_utc') + tuple(
    f'{prefix}_{h}h' for h in HORIZONS for prefix in
    ('residual_pct', 'abs_residual_pct', 'actual_return_pct',
     'predicted_return_pct', 'quantile_position', 'within_p10_p90')
) + ('max_abs_residual_pct_24h', 'max_adverse_excursion_pct_24h',
     'max_favorable_excursion_pct_24h', 'timestral_better', 'mae_improvement_pct')


def _read_forecast(folder: Path, idx: int) -> pd.DataFrame:
    meta = json.loads((folder / 'metrics.json').read_text())
    fp = folder / 'forecast.parquet'
    if meta['window_index'] != idx or digest(fp) != meta['forecast_sha256']:
        raise ValueError(f'Invalid archived forecast or checksum: {idx}')
    df = pd.read_parquet(fp)
    needed = {'timestamp', 'actual', 'timestral_p10', 'timestral_p50', 'timestral_p90', 'naive'}
    if not needed.issubset(df.columns) or len(df) < max(HORIZONS):
        raise ValueError(f'Incomplete forecast: {idx}')
    if df.timestamp.duplicated().any() or not df.timestamp.is_monotonic_increasing:
        raise ValueError(f'Invalid timestamps: {idx}')
    ts = pd.to_datetime(df.timestamp, utc=True)
    if not ts.diff().dropna().dt.total_seconds().eq(3600).all():
        raise ValueError(f'Non-hourly forecast: {idx}')
    if str(ts.iloc[0]) != str(pd.Timestamp(meta['provenance']['forecast_start_utc'])):
        raise ValueError(f'Forecast start mismatch: {idx}')
    values = df[list(needed - {'timestamp'})].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError(f'Nonfinite values: {idx}')
    if not ((df.timestral_p10 <= df.timestral_p50) & (df.timestral_p50 <= df.timestral_p90)).all():
        raise ValueError(f'Quantiles invalid: {idx}')
    return df


def derive(features: pd.DataFrame, forecasts: dict[int, pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Pure transformation, allowing synthetic fixtures to exercise leakage separation."""
    if set(FEATURES) - set(features.columns):
        raise ValueError('Missing required forecast-time features')
    if features.window_index.duplicated().any() or set(features.window_index) != set(forecasts):
        raise ValueError('Feature/forecast index mismatch')
    feat = features[list(FEATURES)].sort_values('forecast_start_utc').reset_index(drop=True).copy()
    outputs = []
    for r in feat.itertuples(index=False):
        f = forecasts[int(r.window_index)]
        anchor = float(r.last_context_close)
        if not np.isfinite(anchor) or anchor <= 0 or not np.allclose(f.naive.to_numpy(float), anchor, rtol=0, atol=1e-6):
            raise ValueError(f'Incorrect price anchor: {r.window_index}')
        obs = f.actual.to_numpy(float)
        pred = f.timestral_p50.to_numpy(float)
        low = f.timestral_p10.to_numpy(float)
        high = f.timestral_p90.to_numpy(float)
        residual = (obs - pred) / anchor * 100
        record = {'window_index': int(r.window_index), 'forecast_start_utc': r.forecast_start_utc}
        for h in HORIZONS:
            i = h - 1
            width = high[i] - low[i]
            record.update({
                f'residual_pct_{h}h': float(residual[i]),
                f'abs_residual_pct_{h}h': float(abs(residual[i])),
                f'actual_return_pct_{h}h': float(100 * (obs[i] / anchor - 1)),
                f'predicted_return_pct_{h}h': float(100 * (pred[i] / anchor - 1)),
                f'quantile_position_{h}h': float((obs[i] - low[i]) / width) if width > 0 else None,
                f'within_p10_p90_{h}h': bool(low[i] <= obs[i] <= high[i]),
            })
        realized = (obs[:24] - anchor) / anchor * 100
        record.update({
            'max_abs_residual_pct_24h': float(np.max(np.abs(residual[:24]))),
            'max_adverse_excursion_pct_24h': float(min(0, np.min(realized))),
            'max_favorable_excursion_pct_24h': float(max(0, np.max(realized))),
        })
        mae_t = np.mean(np.abs(pred[:24] - obs[:24]))
        mae_n = np.mean(np.abs(anchor - obs[:24]))
        record['timestral_better'] = bool(mae_t < mae_n)
        record['mae_improvement_pct'] = float(100*(1-mae_t/mae_n)) if mae_n else None
        outputs.append(record)
    return feat, pd.DataFrame(outputs)[list(TARGETS)]


def build(diagnostics_dir: Path, batch_dir: Path, output_dir: Path) -> dict:
    if output_dir.exists():
        raise FileExistsError(f'Output already exists: {output_dir}')
    schema = json.loads((diagnostics_dir / 'schema.json').read_text())
    diag_path = diagnostics_dir / 'diagnostics.parquet'
    if digest(diag_path) != schema['diagnostics_sha256']:
        raise ValueError('Diagnostic parquet checksum mismatch')
    diagnostics = pd.read_parquet(diag_path)
    cfg = json.loads((batch_dir / 'config.json').read_text())
    if schema['windows_sha256'] != cfg['windows_sha256'] or schema['source_manifest_sha256'] != cfg['source_manifest_sha256']:
        raise ValueError('Different source datasets')
    if set(diagnostics.window_index) != set(cfg['window_indices']):
        raise ValueError('Incomplete or mismatched experiment indices')
    frames = {}
    for idx in diagnostics.window_index:
        frames[int(idx)] = _read_forecast(batch_dir / f'window-{int(idx):04d}', int(idx))
    feat, targets = derive(diagnostics, frames)
    # Cross-check the former diagnostic labels with independently recomputed targets.
    aligned = diagnostics.set_index('window_index').loc[targets.window_index]
    if not np.array_equal(aligned.timestral_better.to_numpy(dtype=bool), targets.timestral_better.to_numpy(dtype=bool)):
        raise ValueError('Inconsistent archived comparison labels')
    if not np.allclose(aligned.mae_improvement_pct.to_numpy(float), targets.mae_improvement_pct.to_numpy(float), rtol=1e-6, atol=1e-5):
        raise ValueError('Inconsistent archived MAE improvements')
    output_dir.mkdir(parents=True)
    try:
        feat.to_parquet(output_dir / 'features.parquet', index=False)
        targets.to_parquet(output_dir / 'targets.parquet', index=False)
        metadata = {
            'schema_version': 'cdl.residuals.v1', 'rows': len(feat),
            'horizons_hours': list(HORIZONS),
            'residual_definition': '100 * (actual_close - timestral_p50) / last_context_close',
            'warning': 'targets contain future observations; NEVER join target columns to live inference inputs',
            'feature_columns': list(FEATURES), 'target_columns': list(TARGETS),
            'diagnostics_sha256': schema['diagnostics_sha256'],
            'source_manifest_sha256': schema['source_manifest_sha256'],
            'windows_sha256': schema['windows_sha256'],
            'features_sha256': digest(output_dir / 'features.parquet'),
            'targets_sha256': digest(output_dir / 'targets.parquet'),
            'quantile_position_meaning': '0=p10, 1=p90, values outside [0,1] are outside forecast interval',
        }
        (output_dir/'manifest.json').write_text(json.dumps(metadata, indent=2)+'\n')
        summary = {'rows':len(feat), 'timestral_better':int(targets.timestral_better.sum()),
                   'mean_residual_pct_24h':float(targets.residual_pct_24h.mean()),
                   'mean_abs_residual_pct_24h':float(targets.abs_residual_pct_24h.mean()),
                   'coverage_24h':float(targets.within_p10_p90_24h.mean())}
        (output_dir/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
        return summary
    except BaseException:
        shutil.rmtree(output_dir)
        raise


def main(argv=None):
    p = argparse.ArgumentParser(description='S02-CP04: signed residual targets for Decision Model research')
    p.add_argument('--diagnostics-dir', default='data/diagnostics/S02-CP03-batch-100-v1')
    p.add_argument('--batch-dir', default='data/experiments/S02-CP02-batch-100')
    p.add_argument('--output-dir', default='data/datasets/S02-CP04-residuals-v1')
    a = p.parse_args(argv)
    print(json.dumps(build(Path(a.diagnostics_dir), Path(a.batch_dir), Path(a.output_dir)), indent=2))

if __name__ == '__main__':
    main()
