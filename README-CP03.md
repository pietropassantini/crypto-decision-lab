# Crypto Decision Lab – S02-CP03 diagnostics patch

Copy `src/crypto_decision_lab/forecast_diagnostics.py` into the existing repository at the same path. Do **not** replace the repository or `data/`.

From `~/projects/crypto-decision-lab`, activate `.venv` and run:

```bash
python -m crypto_decision_lab.forecast_diagnostics
```

Output: `data/diagnostics/S02-CP03-batch-100-v1/{diagnostics.parquet,schema.json,summary.json}`.

The feature/outcome split is explicit in schema.json. Data never needs to be redownloaded, and Timestral does not run. The program rejects inconsistent archived hashes, actual values, timestamps, and MAEs. Destination must not exist to prevent silent overwrites.

Note: This is a diagnostics artifact, not training-ready data. Time-separated validation, rigorous feature-selection and model revision control are still required.
