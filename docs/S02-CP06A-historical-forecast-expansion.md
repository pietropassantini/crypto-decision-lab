# S02-CP06A · Historical Forecast Expansion

**Repository:** `crypto-decision-lab`  
**Obiettivo:** 500 previsioni storiche Timestral valide in rappresentazione canonica CP05R.  
**Stato:** **COMPLETE** — 500/500 finestre validate, 100 riutilizzate da CP02, 400 nuove inferenze, benchmark ricalcolato, dashboard aggiornata.

---

## 1. Risultato sintetico

| Metrica | Valore |
|---|---:|
| Finestre forecast totali | **500** |
| Finestre riutilizzate da CP02 | 100 |
| Nuove inferenze Timestral | 400 |
| Target orari totali (500 × 24) | **12.000** |
| Righe h24 canoniche per benchmark | 500 |
| Campioni smoke | 5 |
| Campioni verification | 50 |
| Modello | `mistralai/Timestral-209M` |
| Revisione usata per le nuove inferenze | `9874d252ab0aae16f7f7c999f98f98d6ad7d8365` |
| Device | CPU |
| Training eseguito | **No** |
| Artefatti originali modificati | **No** |

I 100 forecast CP02 compatibili sono stati **copiati e riusati senza nuova inferenza**. Solo le 400 finestre mancanti sono state inferite. Ogni finestra contiene 24 target orari; il benchmark principale usa il target h24 (`cutoff + 23h`).

**Provenienza mista:** le nuove inferenze sono pinnate alla revisione
`9874d252ab0aae16f7f7c999f98f98d6ad7d8365`; il config CP02 originale non
conteneva una revisione pinning (`revision_requested = null`). I campioni CP02
conservano quindi la loro provenienza storica; il benchmark riporta separatamente
le metriche CP02-riusate e nuove-inferenze.

---

## 2. Convenzione canonica condivisa

La convenzione è ora centralizzata in:

```text
src/crypto_decision_lab/canonical.py
```

Contratto:

```text
cutoff                  = forecast_start_utc
anchor_price            = close all'ultima barra < cutoff
target(h)               = cutoff + (h - 1) ore
target(h=24)            = cutoff + 23h

timestral_return_24h     = decimale       (0.05 = 5%)
timestral_return_pct_24h = percento       (5.0 = 5%)
residual_pct             = percento dell'anchor

timestral_price_24h      = anchor_price * (1 + timestral_return_24h)
ridge_price_24h          = anchor_price * (1 + timestral_return_24h + ridge_residual_pct/100)
histgb_price_24h         = anchor_price * (1 + timestral_return_24h + histgb_residual_pct/100)
```

`validate_h24_frame()` verifica:

- timestamp target = `cutoff + 23h`;
- unicità di `(window_index, target)`;
- prezzi positivi e finiti;
- `timestral_price_24h = anchor × (1 + return decimale)`;
- `timestral_return_pct_24h = 100 × return decimale`;
- quantili ordinati;
- schema `cdl.canonical.v1`.

Test dedicati: `tests/test_canonical.py`.

---

## 3. Selezione e riuso

### Riuso CP02

- 100 finestre CP02 originali;
- checksum `forecast_sha256` verificato;
- nessuna inferenza rieseguita;
- copia nell’archivio additivo con `source = S02-CP02-reused`, `reused = true`;
- hash originale registrato in `source_forecast_sha256`.

### Nuove finestre

- candidate = 2.053 finestre valide dello snapshot meno le 100 CP02;
- selezione deterministica, equidistante in ordine cronologico, senza sovrapposizioni;
- 400 nuove finestre;
- revisione HF fissata;
- ogni inferenza usa il contesto congelato di 168 chiusure e produce 24 target.

### Stage

| Stage | Finestre | Esito |
|---|---:|---|
| smoke | 5 | completato |
| verification | 50 | completato |
| expand | 345 | completato |
| **Totale nuovo** | **400** | **completato** |

Smoke iniziali: `[1, 5, 10, 15, 21]`.

Tempi misurati:

- smoke: ~7 s (prima esecuzione);
- verification 50: 38,4 s;
- expand 345: 182,6 s;
- nessun training.

---

## 4. Checkpoint, resume e deduplicazione

Struttura:

```text
data/experiments/S02-CP06A-historical-expansion-v1/
    config.json
    windows.parquet
    progress.json
    manifest.json
    canonical_predictions.parquet
    benchmark_summary.json
    summary.json
    stages/
        smoke.json
        verification.json
        expand.json
    window-0000/
        forecast.parquet
        metrics.json
    ...
    window-2051/
```

Comportamento:

- ogni finestra è scritta in una directory dedicata con `forecast_sha256`;
- un rerun valida le finestre esistenti e le salta;
- una finestra esistente invalida **non viene sovrascritta**;
- la selezione è deterministica e deduplicata;
- `progress.json` riporta completate/pending/invalide;
- `stages/*.json` registra i checkpoint di stage.

Verifica resume: rilancio dello stage smoke:

```text
planned=5, pending=0, completed=0, skipped=5
```

Nessuna inferenza duplicata, nessun overwrite.

---

## 5. Benchmark ricalcolato

Fonte: `data/experiments/S02-CP06A-historical-expansion-v1/benchmark_summary.json`.

### 500 finestre comuni — Timestral vs naive

| Modello | Campioni | MAE USDT | RMSE USDT | Bias USDT | MAPE | Directional accuracy | Dir. valutabili |
|---|---:|---:|---:|---:|---:|---:|---:|
| Timestral | 500 | 1.192,991 | 1.778,276 | 35,309 | 2,206% | 51,6% | 500 |
| Naive | 500 | 1.176,916 | 1.757,551 | 15,567 | 2,187% | n/d | 0 |

Su questa espansione il naive ha MAE/RMSE leggermente migliori di Timestral. La differenza è modesta ma il risultato va riportato senza invertire il segno: l’espansione non dimostra un vantaggio di Timestral.

### Per fonte

| Fonte | Finestre | Tim MAE | Naive MAE | Tim RMSE | Naive RMSE |
|---|---:|---:|---:|---:|---:|
| CP02 riusato | 100 | 1.181,981 | 1.191,716 | 1.633,172 | 1.638,146 |
| Nuova inferenza | 400 | 1.195,744 | 1.173,216 | 1.812,738 | 1.786,156 |

Sul sottoinsieme CP02 Timestral batte il naive; sulle nuove 400 il naive batte Timestral. La differenza tra i due sottoinsiemi è un segnale di eterogeneità temporale, non un errore di pipeline.

### Per anno (MAE h24)

| Anno | Finestre | Timestral | Naive |
|---:|---:|---:|---:|
| 2021 | 75 | 1.567,123 | 1.616,795 |
| 2022 | 89 | 670,638 | 663,857 |
| 2023 | 86 | 489,760 | 481,049 |
| 2024 | 89 | 1.477,880 | 1.429,529 |
| 2025 | 88 | 1.813,752 | 1.752,462 |
| 2026 | 73 | 1.178,269 | 1.168,498 |

### Overlap CP05R — 50 finestre

| Modello | MAE USDT | RMSE USDT | Directional accuracy |
|---|---:|---:|---:|
| Timestral | 1.458,318 | 1.896,155 | 0.50 |
| Naive | 1.458,101 | 1.858,523 | n/d |
| Ridge | 1.557,421 | 2.024,903 | 0.48 |
| HistGB | 1.642,790 | 2.135,142 | 0.54 |

Questi valori coincidono con il benchmark CP05R corretto. Ridge/HistGB restano disponibili solo sulle 50 finestre CP05R; non sono stati addestrati nuovi modelli residui e non sono stati inventati valori per le altre 450 finestre.

---

## 6. Dashboard

La dashboard ora:

- preferisce l’archivio `S02-CP06A-historical-expansion-v1` se presente;
- mostra 500 finestre e 12.000 righe forecast;
- usa `S02-CP05R-corrected-benchmark-v1` per Ridge/HistGB;
- imposta di default le serie **Timestral + naive** per visualizzare l’espansione completa;
- mantiene Ridge/HistGB selezionabili per il confronto sulle 50 finestre comuni.

Verifica AppTest:

```text
exceptions: []
Campioni forecast: 12.000
MAE Timestral h24: 1.192,99 USDT
MAE naive h24: 1.176,92 USDT
```

Configurazione:

```yaml
forecast:
  expansion_dir: data/experiments/S02-CP06A-historical-expansion-v1
  default_models: [timestral, naive]
```

---

## 7. Artefatti e hash

| File | SHA-256 |
|---|---|
| `canonical_predictions.parquet` | `fce620e2b425cb97c679927cb2ad523b5bf554295942ec915bd0cf0ba700f67a` |
| `benchmark_summary.json` | `92222fac49e1d601ffbbef5d926c7fac947b3e46e4e06bd2ab551f732d81aab9` |
| `summary.json` | `38a1a12cb0e0dc7bf692c99942a045a607d21334076b2730321464d426e9c3fe` |
| `config.json` | `3d8325f78c7b5059e54aca7a4ad7dba88d5c8311c2037a9db14ef60af91aac66` |
| `windows.parquet` | `21f7d091cc0b80ff1f4dca414890af2aa4b0219373a6c15b955ce2b4daac5bad` |
| `manifest.json` | `2ec314659eb80afd232d6d4789d0274c78789484dd28304e0cc1d97688ed5156` |

`manifest.json` contiene:

```text
source_hashes_before == source_hashes_after  → true
original_artifacts_modified                   → false
model_revision = 9874d252ab0aae16f7f7c999f98f98d6ad7d8365
```

Sorgenti principali invariati:

- snapshot `BTCUSDT-1h-raw-v1`;
- CP02 config/summary;
- CP05R predictions/summary.

---

## 8. Test e riproducibilità

Test aggiunti:

```text
tests/test_canonical.py                  6 test
tests/test_forecast_expansion.py         4 test
tests/test_forecast_consistency_audit.py 5 test
```

Suite completa:

```text
68 passed
```

(La suite è stata estesa a 87 test dalla successiva S02-CP06B.)

Comandi:

```bash
pip install -e '.[forecast]'

# 1. prepara piano, copia CP02, crea checkpoint
python -m crypto_decision_lab.forecast_expansion prepare

# 2. smoke su 5 nuove finestre
python -m crypto_decision_lab.forecast_expansion run --stage smoke

# 3. batch di verifica da 50
python -m crypto_decision_lab.forecast_expansion run --stage verification

# 4. espansione fino a 500, con resume
python -m crypto_decision_lab.forecast_expansion run --stage expand

# 5. benchmark canonico
python -m crypto_decision_lab.forecast_expansion benchmark

# 6. stato
python -m crypto_decision_lab.forecast_expansion status
```

Tutti i comandi sono idempotenti sulle finestre già valide.

---

## 9. Verdetto

**COMPLETE.** L’obiettivo di 500 previsioni storiche Timestral valide è raggiunto:

- 500 finestre canoniche;
- 12.000 target orari;
- 500 righe h24 per benchmark;
- riuso completo delle 100 CP02;
- 400 nuove inferenze riproducibili;
- checkpoint/resume/dedup verificati;
- benchmark ricalcolato;
- dashboard aggiornata.

**Caveat:** il benchmark resta esplorativo. Su 500 finestre il naive ha MAE/RMSE leggermente migliori di Timestral; il risultato non va usato come prova di superiorità del modello o per trading. Ridge/HistGB restano limitati alle 50 finestre CP05R e non sono stati riaddestrati. La revisione CP02 storica non era pinnata; le nuove 400 inferenze usano la revisione indicata.
