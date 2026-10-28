# S02-CP04: Signed Residual Dataset

This is an **additive patch** to `crypto-decision-lab`. Unzip at the project root; it adds `src/crypto_decision_lab/residual_dataset.py` and `tests/test_residual_dataset.py` without modifying existing datasets or forecasting scripts.

## Run on Sirio

```bash
cd ~/projects/crypto-decision-lab
source .venv/bin/activate
python -m pytest tests/test_residual_dataset.py -q
python -m crypto_decision_lab.residual_dataset
```

Outputs under `data/datasets/S02-CP04-residuals-v1/`:

- `features.parquet`: strictly information available when the prediction was issued.
- `targets.parquet`: observed signed errors at 4, 12 and 24 hours, realized returns, quantile positions and other future outcomes.
- `manifest.json`: source hashes and separated schemas.
- `summary.json`: measured results.

These 100 examples are **exploratory, not a suitable standalone training/holdout split**. An actual residual prediction model needs substantially more time-separated examples, train-only normalization, and validation on untouched later periods. Note that `max_adverse_excursion_pct_24h` and `max_favorable_excursion_pct_24h` refer to hourly *closes*, not intra-hour OHLC highs/lows or trade execution.

The baseline uses median p50, not automatically an action recommendation. Do not use targets as live inputs.
