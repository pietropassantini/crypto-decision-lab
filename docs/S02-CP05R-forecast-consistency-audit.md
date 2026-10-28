# S02-CP05R · Timestral Forecast Consistency Audit

**Repository:** `crypto-decision-lab`  
**Scope:** audit read-only della rappresentazione Timestral in S02-CP02 / S02-CP04 / S02-CP05.  
**Data audit:** 2026-10-28  
**Stato:** **ROOT CAUSE CONFIRMED** · nessun training, nessuna nuova inferenza Timestral, artefatti originali intatti.

---

## 1. Executive summary

La differenza non deriva da una previsione Timestral diversa, da un disallineamento di timestamp o da una normalizzazione dei prezzi. Deriva da un **errore di unità nella ricostruzione dei prezzi CP05**:

- `forecast_return_24h` in CP04 è un **rendimento decimale** (`0.05` = 5%).
- `residual_pct_24h` è un **rendimento percentuale** (`5.0` = 5%).
- CP05 salva il rendimento decimale in un campo chiamato `timestral_return_pct_24h`, poi lo divide nuovamente per 100:

```text
CP02 / CP03 / CP04:
  forecast_return_24h = p50_target / anchor - 1          # decimale

CP05:
  timestral_pct = forecast_return_24h                    # decimale, nome fuorviante
  timestral_price_24h = anchor * (1 + timestral_pct/100) # ERRATO: /100 su decimale
  ridge_return_pct_24h = timestral_pct + correction      # ERRATO: decimale + percento
  ridge_price_24h = anchor * (1 + (timestral_pct+correction)/100)
```

La formula canonica è:

```text
timestral_price_24h = anchor * (1 + forecast_return_24h)
ridge_price_24h     = anchor * (1 + forecast_return_24h + ridge_residual_pct/100)
histgb_price_24h    = anchor * (1 + forecast_return_24h + histgb_residual_pct/100)
```

I residui appresi da Ridge/HistGB sono già in percento e sono riutilizzabili: **non è necessario riaddestrare** per produrre il benchmark corretto. I file CP05 originali non sono stati modificati; è stato creato un benchmark additivo `S02-CP05R-corrected-benchmark-v1`.

### Verdetto

| Oggetto | Verdetto |
|---|---|
| CP05 originale — rappresentazione prezzi | **FAIL** |
| Root cause | **CONFIRMED** |
| CP05R — benchmark corretto | **PASS** |
| Gate S02-CP06A | **PASS WITH CANONICAL CP05R** |

CP06A può procedere **solo** usando la rappresentazione canonica del §7; i campi prezzo originali CP05 non sono idonei.

---

## 2. Evidenze e trace del codice

### CP02 — forecast Timestral

`src/crypto_decision_lab/forecast_smoke.py` salva il p50 Timestral per ogni target orario:

- `timestamp` = target orario;
- `timestral_p50` = prezzo previsto in USDT;
- `actual` = close reale al target;
- `naive` = `last_context_close`, costante.

Nessuna normalizzazione/denormalizzazione è applicata ai prezzi in CP02: sono già in USDT.

### CP03 / CP04 — feature e target

`src/crypto_decision_lab/forecast_diagnostics.py:67`:

```python
'forecast_return_24h': float(p50[-1]/last-1)
```

Quindi `forecast_return_24h` è **decimale**.

`src/crypto_decision_lab/residual_dataset.py:78-79`:

```python
f'actual_return_pct_{h}h': float(100 * (obs[i] / anchor - 1)),
f'predicted_return_pct_{h}h': float(100 * (pred[i] / anchor - 1)),
```

e riga 76:

```python
f'residual_pct_{h}h': float(residual[i])
# residual = (obs - pred) / anchor * 100
```

Quindi i target sono in **percento**, mentre la feature Timestral è decimale. Questa differenza di unità tra feature e target non invalida il fit — il target del modello residuo è chiaramente percentuale — ma rende obbligatoria una conversione esplicita in ricostruzione.

### CP05 — errore di ricostruzione

`src/crypto_decision_lab/residual_baselines.py:79-95`:

```python
timestral_pct = float(r.forecast_return_24h)             # decimale
entry['timestral_return_pct_24h'] = timestral_pct       # nome errato: non è percento
entry['timestral_price_24h'] = anchor * (1 + timestral_pct/100)
...
correction = float(predicted[name][offset])              # percento
entry[f'{name}_return_pct_24h'] = timestral_pct + correction
entry[f'{name}_price_24h'] = anchor * (1 + (timestral_pct+correction)/100)
```

Sono presenti due difetti collegati:

1. il rendimento decimale Timestral viene diviso per 100 come se fosse percentuale;
2. al rendimento decimale Timestral viene sommato un residuo percentuale senza conversione.

Il secondo difetto rende `ridge_return_pct_24h` e `histgb_return_pct_24h` disomogenei; il primo rende errato anche il prezzo Timestral ricostruito.

### Cosa non è la causa

Non sono cause:

- una differente inferenza Timestral;
- un differente cutoff o target;
- un differente anchor;
- una normalizzazione/denormalizzazione dei prezzi;
- un errore nei residui appresi da Ridge/HistGB;
- un disallineamento di timestamp.

Tutti questi elementi sono stati verificati sui 50 campioni comuni.

---

## 3. Allineamento temporale verificato

Per ogni finestra CP05:

| Elemento | Valore verificato |
|---|---|
| `cutoff` | `forecast_start_utc` |
| anchor | close all'ultima barra **prima** del cutoff |
| target h24 | `cutoff + 23h` (24ª barra oraria, 1-indexed) |
| forecast index h24 | riga 23 del `forecast.parquet` CP02 |
| actual | close di mercato al target |
| CP05 actual | uguale a CP02 actual: max differenza `1.45519e-11` USDT |
| CP05 anchor | uguale a CP02 `last_context_close`: differenza `0` |
| CP05 target | uguale al target CP02 per tutti i 50 campioni |
| actual vs snapshot | max differenza `1.45519e-11` USDT |

Il target corretto non è `cutoff + 24h`; la convenzione corretta è `cutoff + 23h` con orizzonte 1-indexed. CP05 rispetta questa convenzione.

---

## 4. Campioni di confronto numerico

Tabella sui primi 5 campioni comuni. Prezzi in USDT; `return` decimale; `residual` percentuale.

| window | cutoff | target | anchor | actual | Tim return | Tim CP05 errato | Tim canonico | Ridge resid | Ridge CP05 errato | Ridge canonico | HistGB resid | HistGB CP05 errato | HistGB canonico |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1036 | 2024-01-10 00:00 | 2024-01-10 23:00 | 46,110.000 | 46,653.988 | -0.006365 | 46,107.065 | 45,816.492 | -0.642095 | 45,810.995 | 45,520.422 | -2.921264 | 44,760.070 | 44,469.497 |
| 1057 | 2024-01-31 00:00 | 2024-01-31 23:00 | 42,941.102 | 42,580.000 | 0.000593 | 42,941.356 | 42,966.578 | -0.172085 | 42,867.461 | 42,892.683 | -0.144762 | 42,879.194 | 42,904.416 |
| 1077 | 2024-02-20 00:00 | 2024-02-20 23:00 | 51,774.730 | 52,258.820 | 0.000235 | 51,774.852 | 51,786.922 | -0.166974 | 51,688.402 | 51,700.472 | 0.500320 | 52,033.892 | 52,045.961 |
| 1098 | 2024-03-12 00:00 | 2024-03-12 23:00 | 72,078.102 | 71,452.008 | -0.005942 | 72,073.818 | 71,649.781 | -1.456743 | 71,023.826 | 70,599.789 | -1.361440 | 71,092.518 | 70,668.481 |
| 1119 | 2024-04-02 00:00 | 2024-04-02 23:00 | 69,649.797 | 65,463.988 | 0.004062 | 69,652.626 | 69,932.703 | 0.312803 | 69,870.492 | 70,150.570 | 0.658870 | 70,111.527 | 70,391.605 |

### Divergenza massima

Il caso con maggiore divergenza è `window 1554`:

| Campo | Valore |
|---|---:|
| anchor | 110,274.391 |
| actual | 108,645.117 |
| Timestral decimal return | -0.011382 |
| Timestral CP05 | 110,261.839 |
| Timestral canonico | 109,019.219 |
| Ridge residual | -0.469744% |
| Ridge CP05 | 109,743.832 |
| Ridge canonico | 108,501.212 |
| HistGB residual | 0.761100% |
| HistGB CP05 | 111,101.137 |
| HistGB canonico | 109,858.517 |

La differenza massima tra prezzo CP05 e prezzo canonico è **1,242.620 USDT** per Timestral/Ridge/HistGB su questo campione. La differenza è proporzionale all'errore di scala del rendimento Timestral, non al residuo del modello.

---

## 5. Impatto sulle metriche

I 50 campioni CP05 comuni sono stati ricostruiti in due modi:

- **originale**: prezzi CP05 archiviati;
- **corretto**: formula canonica CP05R.

Le metriche originali sono state ricalcolate e coincidono esattamente con `data/experiments/S02-CP05-residual-baselines-v1/summary.json` (tutti i 12 controlli MAE/RMSE/direction PASS).

| Modello | MAE orig. | MAE corr. | Δ MAE | RMSE orig. | RMSE corr. | Δ RMSE | Dir. orig. | Dir. corr. |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Timestral | 1,457.951 | 1,458.318 | +0.366 | 1,858.543 | 1,896.155 | +37.613 | 0.50 | 0.50 |
| Naive | 1,458.101 | 1,458.101 | 0.000 | 1,858.523 | 1,858.523 | 0.000 | 0.00* | n/d* |
| Ridge | 1,543.648 | 1,557.421 | +13.773 | 1,946.832 | 2,024.903 | +78.071 | 0.42 | 0.48 |
| HistGB | 1,658.013 | 1,642.790 | -15.222 | 2,128.814 | 2,135.142 | +6.328 | 0.48 | 0.54 |

\* La direction accuracy naive è `0.00` solo per l'artefatto `sign(0) != sign(actual)`; con la definizione canonica “solo campioni con previsione non nulla” il naive ha **0 campioni valutabili** e accuratezza `n/d`. Non è un fallimento del modello: è una metrica non definita.

### Effetto sul ranking MAE

```text
originale:  Timestral 1457.951 < Naive 1458.101 < Ridge 1543.648 < HistGB 1658.013
corretto:   Naive 1458.101 < Timestral 1458.318 < Ridge 1557.421 < HistGB 1642.790
```

Il ranking **cambia**: con la ricostruzione corretta il naive è marginalmente migliore di Timestral sul MAE (differenza 0.216 USDT). Il risultato resta statisticamente fragile, ma la conclusione precedente “Timestral meglio del naive” non è supportata dai prezzi corretti.

Il miglioramento % vs Timestral cambia:

| Modello | Miglioramento orig. | Miglioramento corr. |
|---|---:|---:|
| Ridge | -5.878% | -6.796% |
| HistGB | -13.722% | -12.650% |

Nessuno dei due baseline residui batte Timestral/naive su questo campione.

---

## 6. Root cause: confermata

**Causa:** errore di unità nella fase di ricostruzione dei prezzi CP05, non nella fase di inferenza o di training.

- Il forecast Timestral p50 è corretto in CP02.
- L'anchor e il target sono corretti.
- Il target residuo CP04 è in percento ed è corretto.
- Le previsioni residue Ridge/HistGB sono in percento e sono riutilizzabili.
- CP05 applica `/100` a un rendimento già decimale e somma percento a decimale.
- Le metriche CP05 originali sono state calcolate su prezzi non correttamente ricostruiti.

Nessuna ipotesi aperta sulla causa. Restano aperti solo temi di validità statistica del benchmark (50 finestre, nessun holdout indipendente dal pretraining Timestral), che non riguardano questo bug.

---

## 7. Rappresentazione canonica per S02-CP06A e dashboard

### Convenzione temporale

```text
cutoff                 = forecast_start_utc
anchor_price           = close all'ultima barra < cutoff
horizon_hours          = 1..24 (1-indexed)
target(h)              = cutoff + (h-1) ore
target(h=24)           = cutoff + 23h
```

### Unità

| Grandezza | Nome canonico | Unità | Esempio |
|---|---|---|---|
| rendimento Timestral | `timestral_return_24h` | decimale | `0.05` = 5% |
| rendimento Timestral percentuale | `timestral_return_pct_24h` | percento | `5.0` = 5% |
| residuo appreso | `*_predicted_residual_pct` | percento dell'anchor | `1.2` = +1.2% |
| prezzo | `*_price_24h` | USDT | `46110.0` |
| actual | `actual_price_24h` | USDT | `46653.99` |

### Formule canoniche

```text
timestral_price_24h = anchor_price * (1 + timestral_return_24h)
timestral_return_pct_24h = 100 * timestral_return_24h

ridge_return_24h   = timestral_return_24h + ridge_predicted_residual_pct / 100
histgb_return_24h  = timestral_return_24h + histgb_predicted_residual_pct / 100

ridge_price_24h    = anchor_price * (1 + ridge_return_24h)
histgb_price_24h   = anchor_price * (1 + histgb_return_24h)

residual_pct       = 100 * (actual_price_24h - timestral_price_24h) / anchor_price
actual_return_pct  = 100 * (actual_price_24h / anchor_price - 1)
residual_pct       = actual_return_pct - timestral_return_pct_24h
```

### Regole operative

1. **Non** usare i campi prezzo originali CP05 per CP06A, dashboard o valutazioni.
2. Timestral deve essere letto da CP02 `timestral_p50` alla riga h24, oppure dal campo canonico CP05R.
3. Ridge/HistGB devono usare i prezzi CP05R, non i prezzi CP05 originali.
4. Il confronto multi-modello deve usare chiavi `(window_index, target, horizon_hours)` comuni.
5. Le finestre indipendenti non devono essere concatenate in una traiettoria continua.
6. Direction accuracy deve escludere previsioni nulle/non definite e riportare il numero di campioni valutabili.
7. I quantili p10/p90 restano in USDT e non vanno ricalcolati da rendimenti percentuali.
8. Il target h24 è `cutoff + 23h`; non `cutoff + 24h`.

La dashboard è stata aggiornata per **preferire** CP05R quando presente e per usare CP05 originale solo come fallback con warning esplicito.

---

## 8. Benchmark corretto prodotto

Percorso additivo, nessuna sovrascrittura degli originali:

```text
data/experiments/S02-CP05R-corrected-benchmark-v1/
    predictions.parquet
    audit_samples.parquet
    summary.json
    manifest.json
```

Hash:

| File | SHA-256 |
|---|---|
| `predictions.parquet` | `87912643632293e3c60e40cddaf02a825433f97f854d9a0c161bf75199915560` |
| `audit_samples.parquet` | `16a5982d1e452a30e08e27371e27716042cdb4af5ef2f2a5fe97ea114ee3f179` |

Hash sorgenti principali:

| Sorgente | SHA-256 |
|---|---|
| CP05 predictions | `f65df16dd889d4641d5ad26220b63a3120b19297343932e5f7c56a020216b5ab` |
| CP05 summary | `4c9474abfbd1752c9516083c696dc9ec49275be3e0a54600f8f8a595e5490205` |
| CP04 features | `193e2657f148e8762f6795f222ab858f53f95c9bc45e516963f82065716b497c` |
| CP04 targets | `ae6b2c6c7e0f4fb85cacdabe4006fd3169ca48b3fdbad689596edf002482fa67` |
| CP02 config | `f8c6054f797d1253dd06b19cc4ca371e9fbe71da61fbbd23cd898e84d4def503` |

`manifest.json` verifica che gli hash sorgente siano identici prima e dopo la scrittura del benchmark:

```text
source_hashes == source_hashes_after  → true
original_artifacts_modified           → false
```

Nessun file CP02/CP04/CP05 o snapshot è stato modificato.

---

## 9. Test di regressione

Implementati in:

```text
src/crypto_decision_lab/forecast_consistency_audit.py
tests/test_forecast_consistency_audit.py
```

Test:

1. il rendimento decimale non viene diviso due volte;
2. il residuo percentuale viene convertito una sola volta;
3. il ranking MAE cambia quando si passa dalla ricostruzione errata a quella canonica;
4. la direction accuracy naive non viene riportata come `0%` ma come non valutabile;
5. le metriche originali e corrette restano finite e con lo stesso numero di campioni.

Verifica automatica dell'audit:

```text
50 campioni comuni
22/22 check PASS
```

Check principali:

- actual CP05 = CP02 actual;
- anchor CP05 = CP02 last context close;
- Timestral canonico = CP02 p50 h24;
- `timestral_return_pct_24h` CP05 contiene il decimale;
- prezzi CP05 originali riproducibili con la formula errata;
- `predicted_return_pct_24h` CP04 = 100 × decimale;
- `residual_pct_24h` = actual_return_pct − predicted_return_pct;
- actual archiviati = close di mercato al target;
- metriche originali ricalcolate = summary CP05 archiviato.

Comandi:

```bash
python -m crypto_decision_lab.forecast_consistency_audit --no-write
python -m pytest tests/test_forecast_consistency_audit.py -q
python -m pytest -q
```

Risultato suite alla milestone CP05R/CP06A: **68 passed** (27 dashboard + 5 audit + 6 canonical + 4 expansion + test storici); la suite è stata estesa a 87 test da S02-CP06B.

---

## 10. Verdetto finale

### Original CP05

**FAIL.** I campi `timestral_price_24h`, `ridge_price_24h`, `histgb_price_24h` e i rendimenti associati non sono in unità coerenti. Le metriche CP05 originali non sono affidabili come metriche di prezzo.

### Root cause

**CONFIRMED.** Errore di scala in ricostruzione CP05, non errore Timestral, non disallineamento temporale, non normalizzazione dei prezzi.

### CP05R

**PASS.** Benchmark ricostruito senza training/inferenza, con artefatti originali intatti e controllo hash.

### Gate S02-CP06A

**PASS WITH CANONICAL CP05R.** CP06A può procedere alle seguenti condizioni:

1. usare CP05R o ricostruire i prezzi con le formule del §7;
2. non usare i campi prezzo CP05 originali;
3. mantenere l'allineamento `cutoff/target/horizon`;
4. riportare campioni comuni e direzione valutabile;
5. trattare il risultato come backtest esplorativo, non come validazione di trading.

---

## 11. Limiti residui

- CP05R ricostruisce i prezzi dai residui archiviati; **non riaddestra** Ridge/HistGB e non rivalida la loro qualità.
- I 50 campioni CP05 restano esplorativi e non sono un holdout indipendente dal pretraining Timestral.
- Il campo legacy `timestral_return_pct_24h` in CP05 contiene un decimale: va considerato deprecato, non corretto silenziosamente.
- La dashboard preferisce CP05R se presente; senza CP05R mostra il fallback CP05 con warning e non lo considera canonico.
- Le metriche percentuali e la direzione devono sempre dichiarare l'unità e il numero di campioni valutabili.
