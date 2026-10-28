"""S02-CP03: diagnostic features derived exclusively from archived forecasts and past market data."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
from .forecast_smoke import load_window
from .sync import digest

SCHEMA = 'cdl.diagnostics.v1'


def build(data_root: Path, snapshot_dir: Path, batch_dir: Path, output_dir: Path) -> dict:
    cfg = json.loads((batch_dir / 'config.json').read_text())
    report = json.loads((snapshot_dir / 'report.json').read_text())
    if cfg['windows_sha256'] != report['windows_parquet_sha256'] or cfg['source_manifest_sha256'] != report['source_manifest_sha256']:
        raise ValueError('Batch and snapshot provenance mismatch')
    indices = cfg['window_indices']
    if len(indices) != len(set(indices)):
        raise ValueError('Duplicate indices in batch config')
    records = []
    for idx in indices:
        folder = batch_dir / f'window-{idx:04d}'
        meta = json.loads((folder / 'metrics.json').read_text())
        fp = folder / 'forecast.parquet'
        if digest(fp) != meta['forecast_sha256']:
            raise ValueError(f'Forecast hash mismatch: {idx}')
        if meta['window_index'] != idx or meta['model'] != cfg['model'] or meta['provenance']['windows_sha256'] != cfg['windows_sha256']:
            raise ValueError(f'Metadata mismatch: {idx}')
        frame = pd.read_parquet(fp).sort_values('timestamp')
        required = {'timestamp','actual','timestral_p10','timestral_p50','timestral_p90','naive'}
        if not required.issubset(frame.columns):
            raise ValueError(f'Missing forecast columns: {idx}')
        context, actual, provenance = load_window(data_root, snapshot_dir, idx)
        if meta['provenance'] != provenance:
            raise ValueError(f'Context provenance mismatch: {idx}')
        horizon = len(actual)
        expected_ts = pd.date_range(provenance['forecast_start_utc'],periods=horizon,freq='h',tz='UTC')
        if not frame['timestamp'].reset_index(drop=True).equals(pd.Series(expected_ts,name='timestamp')):
            raise ValueError(f'Forecast timestamps mismatch: {idx}')
        if not np.allclose(frame['actual'].to_numpy(float),actual.astype(float),atol=1e-6,rtol=0):
            raise ValueError(f'Actual target mismatch: {idx}')
        cols = ['actual','timestral_p10','timestral_p50','timestral_p90','naive']
        if not np.isfinite(frame[cols].to_numpy(float)).all():
            raise ValueError(f'Nonfinite forecast: {idx}')
        if not ((frame.timestral_p10 <= frame.timestral_p50) & (frame.timestral_p50 <= frame.timestral_p90)).all():
            raise ValueError(f'Invalid quantiles: {idx}')
        last = float(context[-1]); close = context.astype(float)
        r = np.diff(np.log(close))
        p10,p50,p90 = (frame[c].to_numpy(float) for c in ('timestral_p10','timestral_p50','timestral_p90'))
        obs = frame.actual.to_numpy(float)
        mae_t = float(np.mean(np.abs(p50-obs)))
        mae_n = float(np.mean(np.abs(last-obs)))
        if not np.isclose(mae_t,meta['metrics']['timestral']['mae'],rtol=1e-6,atol=1e-4):
            raise ValueError(f'MAE metadata mismatch: {idx}')
        if not np.isclose(mae_n,meta['metrics']['naive']['mae'],rtol=1e-6,atol=1e-4):
            raise ValueError(f'Naive MAE metadata mismatch: {idx}')
        records.append({
            'window_index':idx, 'forecast_start_utc':pd.Timestamp(provenance['forecast_start_utc']),
            'model':cfg['model'],
            # Everything above the outcome boundary is known when forecast is issued.
            'last_context_close':last,
            'volatility_168h':float(np.std(r,ddof=1)),
            'trend_168h':float(close[-1]/close[0]-1),
            'momentum_24h':float(close[-1]/close[-25]-1),
            'forecast_return_24h':float(p50[-1]/last-1),
            'interval_width_pct':float((p90[-1]-p10[-1])/last),
            'mean_interval_width_pct':float(np.mean((p90-p10)/last)),
            # OUTCOMES: never supply these as model input.
            'realized_return_24h':float(obs[-1]/last-1),
            'timestral_mae':mae_t, 'naive_mae':mae_n,
            'mae_improvement_pct':float(100*(1-mae_t/mae_n)) if mae_n else np.nan,
            'timestral_better':int(mae_t<mae_n),
            'direction_correct':int(np.sign(p50[-1]-last)==np.sign(obs[-1]-last)) if p50[-1]!=last and obs[-1]!=last else pd.NA,
            'interval_coverage':float(np.mean((obs>=p10)&(obs<=p90))),
        })
    df = pd.DataFrame(records).sort_values('forecast_start_utc').reset_index(drop=True)
    if len(df)!=len(indices):
        raise ValueError('Incomplete diagnostics')
    if output_dir.exists():
        raise FileExistsError(f'Destination already exists: {output_dir}')
    output_dir.mkdir(parents=True)
    try:
        df.to_parquet(output_dir/'diagnostics.parquet',index=False)
        input_features = ['last_context_close','volatility_168h','trend_168h','momentum_24h',
                          'forecast_return_24h','interval_width_pct','mean_interval_width_pct']
        outcomes = ['realized_return_24h','timestral_mae','naive_mae','mae_improvement_pct',
                    'timestral_better','direction_correct','interval_coverage']
        schema = {'schema_version':SCHEMA,'rows':len(df), 'model':cfg['model'],
                  'source_manifest_sha256':cfg['source_manifest_sha256'],
                  'windows_sha256':cfg['windows_sha256'],
                  'features_available_at_forecast':input_features,'future_outcomes_do_not_use_as_features':outcomes,
                  'diagnostics_sha256':digest(output_dir/'diagnostics.parquet')}
        (output_dir/'schema.json').write_text(json.dumps(schema,indent=2)+'\n')
        summary = {'rows':len(df),'wins':int(df.timestral_better.sum()),
                   'timestral_mean_mae':float(df.timestral_mae.mean()),
                   'naive_mean_mae':float(df.naive_mae.mean()),
                   'mean_interval_coverage':float(df.interval_coverage.mean()),
                   'by_year':[]}
        for year,g in df.groupby(df.forecast_start_utc.dt.year):
            summary['by_year'].append({'year':int(year),'windows':len(g),'wins':int(g.timestral_better.sum()),
                                        'mae_improvement_pct':float(100*(1-g.timestral_mae.mean()/g.naive_mae.mean())) if g.naive_mae.mean() else None,
                                        'interval_coverage':float(g.interval_coverage.mean())})
        (output_dir/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
        return summary
    except BaseException:
        import shutil
        shutil.rmtree(output_dir)
        raise


def main(argv=None):
    p=argparse.ArgumentParser(description='Build leak-safe diagnostics from already archived Timestral predictions')
    p.add_argument('--data-root',default='data')
    p.add_argument('--snapshot-dir',default='data/snapshots/BTCUSDT-1h-raw-v1')
    p.add_argument('--batch-dir',default='data/experiments/S02-CP02-batch-100')
    p.add_argument('--output-dir',default='data/diagnostics/S02-CP03-batch-100-v1')
    args=p.parse_args(argv)
    print(json.dumps(build(Path(args.data_root),Path(args.snapshot_dir),Path(args.batch_dir),Path(args.output_dir)),indent=2))

if __name__=='__main__':
    main()
