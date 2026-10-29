# Crypto Decision Lab · Sprint 01

Pipeline **Spot BTCUSDT 1h**, sola lettura, dati di mercato pubblici. Nessuna chiave API, ordine o operazione finanziaria. Per ogni intervallo `[start, end)` in UTC: download a pagine (max 1000), controlli integrità rigorosi, Parquet ZSTD, report JSON e SHA256.

## Installazione su Sirio (Ubuntu)

```bash
sudo apt-get update && sudo apt-get install -y python3-venv python3-pip
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e '.[dev]'
pytest -q
```

Se il repository è stato estratto da ZIP, entra prima nella cartella `crypto-decision-lab`.

## Primo test reale (24 candele)

```bash
cdl ingest --start 2024-01-01T00:00:00Z --end 2024-01-02T00:00:00Z
```

## Storico 2023-2025 (esempio)

```bash
cdl ingest --start 2023-01-01T00:00:00Z --end 2026-01-01T00:00:00Z
```

I grandi intervalli impiegano varie chiamate pubbliche: evitare di sovraccaricare il servizio. Per estendere lo storico, preferire intervalli mensili e conservare i file verificati. `--force` sovrascrive un output esistente, ma è preferibile non usarlo per dataset già versionati.

## Output

- `data/processed/BTCUSDT_1h_YYYY-MM-DD_YYYY-MM-DD.parquet`: candele in UTC
- `data/reports/...json`: intervallo, righe attese, anomalie, data acquisizione, SHA256
- in caso di buchi o errori il processo **non pubblica** il Parquet e termina con exit code 2

## Qualità e semantica

- Spot Binance BTCUSDT: **non** futures; prezzi in USDT, quantità in BTC.
- Intervallo **inizio incluso, fine esclusa**; sono ammesse solo ore UTC complete e chiuse.
- Timestamp da API REST in **millisecondi**; non confondere con gli archivi CSV spot dal 2025, in microsecondi.
- Verifica ore mancanti, duplicati, ordine, confini, `close_time`, OHLC, volumi, NaN.
- Nessuna interpolazione, nessuna deduplicazione silenziosa, nessun forward-fill.
- Le date senza dati reali (es. prima della quotazione) generano un errore, non candele sintetiche.
- Il report contiene il SHA256 del Parquet; la risposta HTTP non viene conservata come snapshot raw in v0.1.

## Verifiche sprint

1. `pytest -q`: i test di paginazione, timestamp, duplicati, gap e OHLC devono passare.
2. Esegui il test reale da 24 ore. Verifica `quality.ok=true` e `rows=expected_rows=24`.
3. Apri il Parquet con `pandas.read_parquet` e confronta prima/ultima ora.
4. Per controllo negativo elimina una riga da un DataFrame e verifica che `check_quality` fallisca.

## Fonte

API pubblica: https://developers.binance.com/en/docs/binance-spot-api-docs/rest-api/market-data-endpoints
Endpoint market-data-only: https://data-api.binance.vision/api/v3/klines

## Fase successiva

M2: forecast Timestral su split cronologici e salvataggio delle predizioni con `as_of_utc`, senza leakage.

## CP04: sync mensile riprendibile (v0.2)

```bash
cdl sync --symbol BTCUSDT --start 2024-01-01T00:00:00Z --end 2025-01-01T00:00:00Z
```

Il comando salva file separati in `data/chunks/BTCUSDT/1h/`, ognuno con metadati JSON e hash SHA-256; genera un manifesto per l'intervallo in `data/manifests/`. Al secondo avvio verifica checksum e qualità dei blocchi e scarica solo quelli mancanti. Se un blocco esistente è corrotto o non ha metadati, si interrompe **senza sovrascriverlo**; intervenire manualmente dopo aver salvato una copia. L'output esistente prodotto da `cdl ingest` non viene riutilizzato automaticamente: CP04 crea il proprio archivio a blocchi.

Test: `pytest -q`. Il test `test_sync_resume_and_checksum` richiede `pyarrow` (dipendenza del progetto).


## CP05-B: known source anomalies

The `cdl sync` command permits only explicitly whitelisted, precisely matched
source deviations. Binance Spot BTCUSDT 1h, 2021-02-11, has a missing 04:00 UTC
bar and an early-closing 03:00 UTC bar. Original rows are never modified or filled.
Chunk sidecars report `quality.status = ACCEPTED_WITH_WARNINGS`; the full
manifest includes `warnings`, `total_rows`, and `expected_rows`. Any unknown
quality defect remains a hard failure. Previously created valid CP04 chunks
can still be reused with their old sidecars.

For the first verification, run:

```bash
cdl sync --symbol BTCUSDT --start 2021-02-01T00:00:00Z --end 2021-03-01T00:00:00Z
```

Expected: `total_rows=671`, `expected_rows=672`, `warning_count=1`.
No synthetic candle is introduced.

## CP05-D: full-history audit (`cdl sync --mode audit`)

`--mode verified` (default) accepts only known source exceptions and stops on new errors.
`--mode strict` stops for *any* quality deviation (including registered exceptions).
`--mode audit` scans each monthly chunk and **quarantines** unknown-quality records in
`data/quarantine/<SYMBOL>/1h/`. It does **not** add them to the validated `data/chunks/`.
The aggregated manifest reports `quality_status=REQUIRES_REVIEW`, `unresolved_chunks`,
`warnings`, and per-chunk UTC timestamps of missing and irregular-close candles.
Transport failures and corrupt previously validated files still stop the run.

```bash
cd ~/projects/crypto-decision-lab
source .venv/bin/activate
cdl sync --mode audit --symbol BTCUSDT \
  --start 2021-01-01T00:00:00Z --end 2026-10-23T16:00:00Z
```

**Important:** paths are relative to the current working directory; run commands
from the repository root, not `~/projects`. A quarantined snapshot will not be
silently overwritten on repeat audits; its checksum must match the metadata.
Unknown-source anomalies must be reviewed before a subsequent verified run.
The audit never interpolates missing candles. Existing verified files must pass
checksum validation and their quality check before reuse.

## CP06: Dataset freeze and Timestral-safe window index

**Do not run another `sync`** before freezing. The CLI `freeze` verifies the previously audited manifest, every source chunk SHA-256 (including quarantined Parquet), row counts, chronology, and structural candle anomalies. It creates a separate, new snapshot directory; it never rewrites raw market files, and it never fills missing prices.

From `~/projects/crypto-decision-lab`:

```bash
cdl freeze \
  --manifest data/manifests/BTCUSDT_1h_1609459200000_1792771200000.json \
  --output-dir data \
  --snapshot-dir data/snapshots/BTCUSDT-1h-raw-v1 \
  --lookback 168 --horizon 24 --stride 24
```

Files produced in snapshot directory:
- `raw-manifest.json`: hashed list of all verified source files, with the original manifest SHA-256.
- `anomalies.json`: exact missing-hour and irregular-close timestamps, without editing the input candles.
- `forecast-windows.parquet`: *index only*: context start, forecast start, target exclusive end. Both the input context and future evaluation horizon must be contiguous, present, and structurally normal.
- `report.json`: window settings and a SHA-256 hash of the window index.

A blocked or failed freeze does not certify the dataset. The output directory cannot already exist: this prevents accidental replacement of a frozen version. Copies of RAW files are **not** made, so retain and protect the original `data/chunks` and `data/quarantine` directories. For a genuinely portable immutable archive, also copy these raw objects with permissions preventing modification, e.g. to immutable/versioned storage.

The 2021-02-11 event is recognized in the source manifest; the new freeze counts the absent hour and partial candle from the raw bytes, even though the month itself was previously marked `ACCEPTED_WITH_WARNINGS`. An audit may contain 14 missing hour slots and 5 irregular-close candles without inventing them.

`--stride 24` yields one candidate 24h forecast per day, reducing redundant candidate windows. This is a **technical integrity filter**, not yet a temporal train/test split or a leakage-free investment backtest. A proper walk-forward evaluator comes next.

## S02-CP01 · Forecast zero-shot (CPU)

Eseguire dalla radice del repository con `.venv` attivo:

```bash
pip install 'git+https://github.com/mistralai/timestral.git'
python -m crypto_decision_lab.forecast_smoke --window-index 0
```

Il comando verifica gli hash della snapshot e dei blocchi Parquet, legge esclusivamente una finestra regolare del dataset CP06, invia a Timestral le 168 chiusure precedenti, confronta le 24 previsioni con il futuro effettivo e con il benchmark naïve (ultimo prezzo costante). Scrive `metrics.json` e `forecast.parquet` in `data/experiments/S02-CP01-window-0000/`. Rifiuta di sovrascrivere esperimenti esistenti. Il download iniziale dei pesi ~837 MB richiede internet; in seguito è possibile lavorare offline con modello in cache.

Questo è un *smoke test*, non una valutazione statistica. Il modello usa prezzi di chiusura, senza volumi né covariate. La revisione esatta dei pesi non è ancora pinning in CP01: da aggiungere nel batch storico CP02.

## S02-CP02 · Batch CPU con ripresa

Il comando batch carica Timestral **una sola volta per esecuzione** e seleziona
`--count` finestre equidistanti nell'indice cronologico congelato.
È un campionamento esplorativo, **non** una separazione train/test né una
valutazione statistica definitiva. La revisione effettiva dei pesi resta da
bloccare mediante `--revision` con un commit verificato.

```bash
python -m crypto_decision_lab.forecast_batch --count 5 \
  --output-dir data/experiments/S02-CP02-pilot-5
python -m crypto_decision_lab.forecast_batch --count 100 \
  --output-dir data/experiments/S02-CP02-batch-100
```

Ripetendo lo stesso comando e la stessa directory, i risultati esistenti
sono verificati via checksum e saltati. Non si cancellano né sovrascrivono
risultati: in caso di inconsistenza il programma si arresta. I risultati
individuali sono in `window-NNNN/{forecast.parquet,metrics.json}`;
`summary.json` contiene la statistica aggregata e `config.json` la selezione
riproducibile. Salvare **sempre** la directory `data/` e la snapshot CP06.

Nota: la selezione equidistante usa l'ordine di finestre valide, non la
spaziatura uniforme del calendario; le lacune storiche sono già escluse.
Il benchmark naïve è persistenza di prezzo. MAE/RMSE in USDT non normalizzati
sono sensibili al livello del prezzo; la successiva fase includerà metriche
percentuali, split cronologici e analisi per regime.

## S03-CP01 · Dashboard read-only

La dashboard Streamlit per BTCUSDT, Timestral e baseline statistiche è descritta in
[`README-DASHBOARD.md`](README-DASHBOARD.md). Avvio dalla radice del repository:

```bash
pip install -e '.[dashboard,dev]'
streamlit run src/crypto_decision_lab/dashboard/app.py
```

Legge esclusivamente gli archivi già presenti in `data/`, non richiede un server
Timestral attivo e non modifica lo snapshot congelato. Test: `pytest tests/dashboard -q`.

Report milestone: [`docs/S03-CP01-report.md`](docs/S03-CP01-report.md).

## S02-CP05R · Timestral forecast consistency audit

Audit read-only della rappresentazione Timestral CP02/CP04/CP05 e benchmark canonico
additivo CP05R:

- report: [`docs/S02-CP05R-forecast-consistency-audit.md`](docs/S02-CP05R-forecast-consistency-audit.md)
- benchmark: `data/experiments/S02-CP05R-corrected-benchmark-v1/`
- audit riproducibile: `python -m crypto_decision_lab.forecast_consistency_audit --no-write`

I file CP02/CP04/CP05 e lo snapshot originale restano invariati. La dashboard preferisce
CP05R quando presente.


## S02-CP06A · Historical forecast expansion

Archivio additivo di 500 finestre Timestral canoniche (100 CP02 riusate + 400 nuove inferenze),
con checkpoint/resume/dedup e benchmark ricalcolato:

- report: [`docs/S02-CP06A-historical-forecast-expansion.md`](docs/S02-CP06A-historical-forecast-expansion.md)
- archivio: `data/experiments/S02-CP06A-historical-expansion-v1/`
- comandi: `python -m crypto_decision_lab.forecast_expansion prepare|run|benchmark|status`
- dipendenze inferenza: `pip install -e '.[forecast]'`
- convenzione condivisa: `src/crypto_decision_lab/canonical.py`

La dashboard preferisce automaticamente questo archivio quando presente.


## S02-CP06B · Market regime analysis & selective forecasting

Analisi dei regimi di mercato al cutoff e valutazione di policy selettive
USE_TIMESTRAL/USE_NAIVE con walk-forward temporale (5 fold, purge 24h):

- report: [`docs/S02-CP06B-market-regime-analysis.md`](docs/S02-CP06B-market-regime-analysis.md)
- dataset: `data/datasets/S02-CP06B-regime-features-v1/`
- esperimento: `data/experiments/S02-CP06B-selective-policy-v1/`
- comandi: `python -m crypto_decision_lab.regime_analysis` e
  `python -m crypto_decision_lab.policy_evaluation`

Risultato: nessuna policy selettiva batte Always Naive out-of-sample
(300 finestre); l'oracle teorico indica un margine di -72,56 USDT non
catturabile con le feature testate. Intern-Decision-0.8B non addestrato.
La dashboard espone la vista "Regime & Policy".


## S02-CP07 · Decision labeling & offline trading simulation

Simulatore deterministico LONG/SHORT/NO_TRADE con costi, label retrospettive e
walk-forward a 5 fold:

- report: [`docs/S02-CP07-trading-simulation.md`](docs/S02-CP07-trading-simulation.md)
- label: `data/datasets/S02-CP07-decision-labels-v1/`
- esperimento: `data/experiments/S02-CP07-trading-simulation-v1/`
- comandi: `python -m crypto_decision_lab.decision_labeling` e
  `python -m crypto_decision_lab.trading_policy`

Verdetto: **INCONCLUSIVE**. Nessuna policy batte Always NO_TRADE in modo
statisticamente stabile; il momentum threshold è positivo ma non significativo
contro la baseline no-trade. Intern-Decision-0.8B non addestrato.
La dashboard espone la vista "Trading Simulation".
