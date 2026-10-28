"""S02-CP05: walk-forward residual correction baselines (research only)."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
from .sync import digest

FEATURE_COLUMNS = (
    'volatility_168h', 'trend_168h', 'momentum_24h',
    'forecast_return_24h', 'interval_width_pct', 'mean_interval_width_pct',
)


def prepare(features: pd.DataFrame, targets: pd.DataFrame) -> pd.DataFrame:
    """Join only on identity; target columns never enter X."""
    from sklearn.utils.validation import check_array
    required_f = {'window_index', 'forecast_start_utc', 'last_context_close', *FEATURE_COLUMNS}
    required_t = {'window_index', 'forecast_start_utc', 'residual_pct_24h', 'actual_return_pct_24h'}
    if not required_f.issubset(features.columns) or not required_t.issubset(targets.columns):
        raise ValueError('Required columns missing')
    if features.window_index.duplicated().any() or targets.window_index.duplicated().any():
        raise ValueError('Duplicate window indices')
    if len(features) != len(targets) or set(features.window_index) != set(targets.window_index):
        raise ValueError('Features/targets index mismatch')
    joined = features.merge(targets[list(required_t)], on='window_index', how='inner', validate='one_to_one', suffixes=('', '_target'))
    lhs = pd.to_datetime(joined.forecast_start_utc, utc=True)
    rhs = pd.to_datetime(joined.forecast_start_utc_target, utc=True)
    if not lhs.eq(rhs).all():
        raise ValueError('Feature/target forecast time mismatch')
    joined = joined.drop(columns='forecast_start_utc_target').sort_values('forecast_start_utc').reset_index(drop=True)
    if lhs.isna().any() or joined.forecast_start_utc.duplicated().any():
        raise ValueError('Invalid or duplicate forecast time')
    check_array(joined[list(FEATURE_COLUMNS)].to_numpy(float), ensure_all_finite=True)
    check_array(joined[['residual_pct_24h', 'actual_return_pct_24h', 'last_context_close']].to_numpy(float), ensure_all_finite=True)
    if (joined.last_context_close <= 0).any():
        raise ValueError('Nonpositive anchor price')
    return joined


def walk_forward(df: pd.DataFrame, min_train: int = 50, test_size: int = 10) -> pd.DataFrame:
    """Expanding training on strict past, disjoint later evaluations.

    No model or scaler is fitted using an evaluation sample or later sample.
    Fixed hyperparameters: no tuning on reported evaluation rows.
    """
    from sklearn.base import clone
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    n = len(df)
    if min_train < 25 or test_size < 1 or n <= min_train:
        raise ValueError('Not enough observations for walk-forward')
    prototypes = {
        'ridge': make_pipeline(StandardScaler(), Ridge(alpha=10.0)),
        'histgb': HistGradientBoostingRegressor(
            max_iter=80, max_leaf_nodes=7, min_samples_leaf=10,
            l2_regularization=10.0, learning_rate=0.05,
            early_stopping=False, random_state=42,
        ),
    }
    x = df[list(FEATURE_COLUMNS)].to_numpy(dtype=float)
    y = df.residual_pct_24h.to_numpy(dtype=float)
    rows = []
    fold = 0
    for start in range(min_train, n, test_size):
        stop = min(start + test_size, n)
        models = {name: clone(model).fit(x[:start], y[:start]) for name, model in prototypes.items()}
        predicted = {name: model.predict(x[start:stop]) for name, model in models.items()}
        for offset, j in enumerate(range(start, stop)):
            r = df.iloc[j]
            anchor = float(r.last_context_close)
            timestral_pct = float(r.forecast_return_24h)
            actual_pct = float(r.actual_return_pct_24h)
            entry = {
                'fold': fold, 'window_index': int(r.window_index),
                'forecast_start_utc': str(r.forecast_start_utc),
                'train_count': start, 'actual_return_pct_24h': actual_pct,
                'timestral_return_pct_24h': timestral_pct,
                'naive_return_pct_24h': 0.0,
                'actual_price_24h': anchor * (1 + actual_pct/100),
                'timestral_price_24h': anchor * (1 + timestral_pct/100),
                'naive_price_24h': anchor,
            }
            for name in prototypes:
                correction = float(predicted[name][offset])
                entry[f'{name}_predicted_residual_pct'] = correction
                entry[f'{name}_return_pct_24h'] = timestral_pct + correction
                entry[f'{name}_price_24h'] = anchor * (1 + (timestral_pct+correction)/100)
            rows.append(entry)
        fold += 1
    return pd.DataFrame(rows)


def summarize(predictions: pd.DataFrame) -> dict:
    actual = predictions.actual_price_24h.to_numpy(float)
    metrics = {}
    for name in ('timestral', 'naive', 'ridge', 'histgb'):
        predicted = predictions[f'{name}_price_24h'].to_numpy(float)
        diff = predicted - actual
        metrics[name] = {
            'mae_price_24h': float(np.mean(np.abs(diff))),
            'rmse_price_24h': float(np.sqrt(np.mean(diff**2))),
            'direction_accuracy_24h': float(np.mean(
                np.sign(predictions[f'{name}_return_pct_24h'].to_numpy(float)) ==
                np.sign(predictions.actual_return_pct_24h.to_numpy(float))
            )),
        }
    return {
        'evaluation_rows': len(predictions),
        'folds': int(predictions.fold.nunique()),
        'metrics': metrics,
        'mae_improvement_vs_timestral_pct': {
            name: float(100*(1-metrics[name]['mae_price_24h']/metrics['timestral']['mae_price_24h']))
            for name in ('ridge', 'histgb')
        },
        'notice': 'Exploratory historical backtest, no transaction costs, no independent model-pretraining holdout.',
    }


def run(dataset_dir: Path, output_dir: Path, min_train: int = 50, test_size: int = 10) -> dict:
    if output_dir.exists():
        raise FileExistsError(f'Output already exists: {output_dir}')
    metadata = json.loads((dataset_dir/'manifest.json').read_text())
    fp, tp = dataset_dir/'features.parquet', dataset_dir/'targets.parquet'
    if digest(fp) != metadata['features_sha256'] or digest(tp) != metadata['targets_sha256']:
        raise ValueError('Dataset hash mismatch')
    data = prepare(pd.read_parquet(fp), pd.read_parquet(tp))
    preds = walk_forward(data, min_train, test_size)
    report = summarize(preds)
    report['source_features_sha256'] = metadata['features_sha256']
    report['source_targets_sha256'] = metadata['targets_sha256']
    report['train_min'] = min_train
    report['fold_test_size'] = test_size
    report['feature_columns'] = list(FEATURE_COLUMNS)
    output_dir.mkdir(parents=True)
    try:
        preds.to_parquet(output_dir/'predictions.parquet', index=False)
        report['predictions_sha256'] = digest(output_dir/'predictions.parquet')
        (output_dir/'summary.json').write_text(json.dumps(report, indent=2)+'\n')
    except BaseException:
        shutil.rmtree(output_dir)
        raise
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description='Walk-forward baseline: Timestral residual correction')
    parser.add_argument('--dataset-dir', default='data/datasets/S02-CP04-residuals-v1')
    parser.add_argument('--output-dir', default='data/experiments/S02-CP05-residual-baselines-v1')
    parser.add_argument('--min-train', type=int, default=50)
    parser.add_argument('--test-size', type=int, default=10)
    args = parser.parse_args(argv)
    print(json.dumps(run(Path(args.dataset_dir), Path(args.output_dir), args.min_train, args.test_size), indent=2))

if __name__ == '__main__':
    main()
