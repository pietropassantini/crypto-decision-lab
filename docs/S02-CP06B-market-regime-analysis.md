# S02-CP06B · Market Regime Analysis & Selective Forecasting

**Repository:** `crypto-decision-lab`  
**Obiettivo:** determinare se esistono regimi di mercato identificabili al cutoff in cui Timestral-209M batte la baseline Naive, e valutare una policy selettiva USE_TIMESTRAL/USE_NAIVE con validazione temporale rigorosa.  
**Stato:** **COMPLETE** — 500/500 finestre riconciliate, 87 test superati, nessun leakage rilevato.  
**Intern-Decision-0.8B: non addestrato**, come richiesto.

---

## Verdetto sintetico

**Nessun regime cutoff-osservabile offre un vantaggio Timestral stabile.** Su 300 finestre out-of-sample:

| Policy | MAE OOS (USDT) | Δ vs Always Naive | CI95 (block bootstrap) | Share Timestral |
|---|---:|---:|---:|---:|
| Always Naive | **1.288,13** | 0,00 | — | 0% |
| Always Timestral | 1.323,90 | +35,77 | [−0,04, +78,69] | 100% |
| Regime rules | 1.293,95 | +5,81 | [−11,00, +18,76] | 40,3% |
| Logistic (L2) | 1.313,76 | +25,63 | [+6,05, +46,74] | 30,0% |
| **Oracle (teorico)** | **1.215,57** | **−72,56** | [−98,66, −54,29] | 44,7% |

- Nessuna policy selettiva batte Always Naive out-of-sample.
- La policy a regole è statisticamente indistinguibile dal naive (CI include 0).
- La logistic è **significativamente peggiore** del naive (CI interamente positivo).
- Il bound teorico oracle dimostra che esiste margine (−72,56 USDT), ma non è catturabile con le feature cutoff-osservabili testate.
- L'in-sample suggerirebbe che le regole battono il naive (1.166,11 vs 1.176,92): il risultato si inverte out-of-sample → **overfitting documentato**.

**Raccomandazione:** non distribuire la policy selettiva; il naive resta il riferimento. Non procedere all'addestramento di Intern-Decision-0.8B su questo segnale senza nuove feature o dati aggiuntivi.

---

## STEP 1 — Forecast Error Analysis

Fonte: `data/experiments/S02-CP06A-historical-expansion-v1/canonical_predictions.parquet` (schema `cdl.canonical.v1`), 500 righe h24.

Definizioni:

```text
signed_error = predicted - actual          (USDT)
abs_error    = |predicted - actual|
delta_mae    = abs_error_timestral - abs_error_naive   (negativo → Timestral migliore)
winner       = TIMESTRAL | NAIVE | TIE
predicted_direction = sign(timestral_p50 - anchor)
actual_direction    = sign(actual - anchor)
direction_correct   = 1 se entrambe le direzioni sono non nulle e coincidono
```

Complessivamente su 500 finestre:

| Metrica | Valore |
|---|---:|
| MAE Timestral | 1.192,991 USDT |
| MAE Naive | 1.176,916 USDT |
| Δ MAE medio | +16,075 USDT (CI95 [−10,183, +43,812]) |
| Win rate Timestral | 48,8% |
| Directional accuracy | 51,6% (500 valutabili) |

### Riconciliazione con il benchmark CP06A

Tutti i controlli automatici passano (`regime_analysis.reconcile_with_benchmark`):

| Controllo | Esito |
|---|---|
| numero finestre = 500 | PASS |
| MAE Timestral = 1192,9910546875 | PASS |
| MAE Naive = 1176,916380859375 | PASS |
| RMSE Timestral / Naive | PASS |
| bias Timestral / Naive | PASS |
| directional accuracy = 0,516, valutabili = 500 | PASS |
| anchor = close di mercato a cutoff−1h | PASS (max Δ 0,00375 USDT, tolleranza float32) |
| feature bar = cutoff−1h | PASS |
| actual = close di mercato al target (cutoff+23h) | PASS (max Δ 0,00375 USDT) |

Modulo: `src/crypto_decision_lab/regime_analysis.py` (funzione `build_errors`).

---

## STEP 2 — Market Regime Features

Tutte le feature sono calcolate **esclusivamente su barre con `timestamp < cutoff`**. Il valore di ogni indicatore causale è letto alla barra `cutoff − 1h`, l'ultima chiusa nota al momento della previsione. Il calcolo riusa il registry causale della dashboard (`crypto_decision_lab.dashboard.indicators`) per SMA/EMA/RSI/ATR.

### Formule (close = chiusure orarie con timestamp < cutoff)

| Feature | Formula | Unità |
|---|---|---|
| sma20, sma50 | media mobile semplice delle chiusure, finestre 20/50 barre | USDT |
| ema20, ema50 | media esponenziale (adjust=False), finestre 20/50 | USDT |
| rsi14 | Wilder RSI a 14 barre | 0–100 |
| atr14 | Average True Range a 14 barre | USDT |
| atr_pct | 100 × atr14 / close[-1] | % |
| volatility_24h | std(ddof=1) dei log-rendimenti sulle ultime 24 chiusure (23 rendimenti) | decimale per barra |
| volatility_168h | std(ddof=1) dei log-rendimenti sulle ultime 168 chiusure (167 rendimenti) — come S02-CP03 `volatility_168h` | decimale per barra |
| momentum_24h | close[-1]/close[-25] − 1 | decimale |
| momentum_168h | close[-1]/close[-168] − 1 (finestra trailing 168 barre, come S02-CP03 `trend_168h`) | decimale |
| dist_sma50_pct | 100 × (close[-1]/sma50 − 1) | % |
| trend_strength_pct | 100 × (ema20/ema50 − 1) | % |
| volume_ratio | media(volume, 24 barre) / media(volume, 168 barre) | rapporto |

Nota storica: `momentum_168h` usa la finestra trailing di 168 barre (primo→ultimo, 167 ore) per coerenza con la definizione `trend_168h` già usata in S02-CP03/CP04 e per garantire la copertura della finestra 0 (il cui contesto congelato conta esattamente 168 barre).

### Regimi

```text
TREND_UP / SIDEWAYS / TREND_DOWN  ← terzili di momentum_168h
VOL_LOW / VOL_MEDIUM / VOL_HIGH   ← terzili di volatility_168h
combined_regime = "<trend>/<vol>"  (9 celle)
```

**Le soglie sono terzili fittate solo sulle righe di training di ciascun fold** e applicate ai test senza ricalibrazione (`fit_tercile_thresholds` + `assign_regimes`). Per la statistica descrittiva full-sample del §STEP 3 si usano terzili sull'intero periodo, dichiarati come descrittivi.

Dataset: `data/datasets/S02-CP06B-regime-features-v1/`
- `features.parquet` (500×18, solo cutoff-info) — SHA-256 `df70249cbd6fa5e9b9f2440cb1b7e8c496187610b7c61ffd324f3061ed1d27ac`
- `errors.parquet` (500×18, esiti) — SHA-256 `bfc237901f002cb519eae9c35c816e2cb3b37886ca45e11baf0b62152fef9193`
- `manifest.json` verifica che gli hash sorgente siano invariati (true).

Test di causalità: `test_compute_features_are_causal_against_future_perturbation` perturba tutte le barre ≥ cutoff e verifica che le feature non cambino.

---

## STEP 3 — Regime Analysis

CI = moving-block bootstrap, blocco 10 finestre cronologiche, 1000 draw, seed 42. Sottogruppi con `< 30` osservazioni sono marcati `reliable=false`.

### Trend regime (full sample, terzili descrittivi)

| Regime | n | MAE Tim | MAE Naive | Δ MAE | CI95 | Win Tim |
|---|---:|---:|---:|---:|---:|---:|
| SIDEWAYS | 166 | 1.113,18 | 1.118,18 | −5,00 | [−25,08, +23,68] | 50,6% |
| TREND_DOWN | 167 | 1.263,99 | 1.250,46 | +13,54 | [−46,59, +62,20] | 50,9% |
| TREND_UP | 167 | 1.201,32 | 1.161,76 | **+39,56** | **[+6,48, +85,78]** | 44,9% |

In TREND_UP il naive è **significativamente migliore**: quando il mercato è già in trend stabilito, la persistenza è difficile da battere a 24h.

### Vol regime

| Regime | n | MAE Tim | MAE Naive | Δ MAE | CI95 | Win Tim |
|---|---:|---:|---:|---:|---:|---:|
| VOL_LOW | 167 | 946,52 | 955,87 | −9,35 | [−40,26, +14,64] | 49,7% |
| VOL_MEDIUM | 166 | 1.223,44 | 1.165,76 | **+57,68** | **[+18,26, +106,71]** | 43,4% |
| VOL_HIGH | 167 | 1.409,20 | 1.409,06 | +0,14 | [−54,60, +55,38] | 53,3% |

In VOL_MEDIUM il naive è significativamente migliore. VOL_HIGH è neutro: la maggiore volatilità aumenta gli errori di entrambi.

### Celle combinate (estratto)

| Cella | n | Δ MAE | CI95 | Win Tim |
|---|---:|---:|---:|---:|
| TREND_DOWN/VOL_LOW | 31 | **−30,82** | **[−84,57, −3,21]** | 51,6% |
| SIDEWAYS/VOL_HIGH | 37 | −24,49 | [−33,40, +35,74] | 56,8% |
| TREND_DOWN/VOL_HIGH | 67 | −38,21 | [−165,54, +23,62] | 61,2% |
| TREND_DOWN/VOL_MEDIUM | 69 | **+83,71** | **[+20,35, +153,72]** | 40,6% |
| TREND_UP/VOL_HIGH | 63 | +55,39 | [+0,69, +104,62] | 42,9% |

L'unica cella con vantaggio Timestral significativo è `TREND_DOWN/VOL_LOW` (n=31, appena sopra la soglia di affidabilità, CI ampio). Le celle favorevoli a Timestral non sono stabili né sufficientemente popolate; le celle contrarie (TREND_UP, VOL_MEDIUM, TREND_DOWN/VOL_MEDIUM) sono più robuste.

### Stabilità temporale (per anno di cutoff)

| Anno | Finestre | MAE Tim | MAE Naive | Δ MAE | Win Tim |
|---|---:|---:|---:|---:|---:|
| 2021 | 75 | 1.567,12 | 1.616,79 | −49,67 | 60,0% |
| 2022 | 89 | 670,64 | 663,86 | +6,78 | 53,9% |
| 2023 | 86 | 489,76 | 481,05 | +8,71 | 51,2% |
| 2024 | 89 | 1.477,88 | 1.429,53 | +48,35 | 34,8% |
| 2025 | 88 | 1.813,75 | 1.752,46 | +61,29 | 43,2% |
| 2026 | 73 | 1.178,27 | 1.168,50 | +9,77 | 52,1% |

Il vantaggio Timestral emerge solo nel 2021; dal 2022 al 2026 il naive è costantemente pari o migliore. **Le differenze per regime non sono stabili nel tempo** → forte rischio che qualsiasi mappatura regime→modello fittata storica non generalizzi.

---

## STEP 4 — Selective Forecast Policy

Policy implementate (`src/crypto_decision_lab/policy_evaluation.py`):

1. **Always Naive** — persistenza su ogni finestra.
2. **Always Timestral** — p50 su ogni finestra.
3. **Regime rules** — per cella `combined_regime` sceglie TIMESTRAL se il Δ MAE medio di training è negativo; celle con <10 osservazioni di training ricadono sul vincitore globale di training. Soglie terzili di training.
4. **Logistic (L2)** — classificatore su feature cutoff (rsi14, atr_pct, vol 24/168, momentum 24/168, dist_sma50, trend_strength, volume_ratio) **più il valore della previsione Timestral** (`timestral_return_pct_24h`, disponibile al cutoff). Label di training: `winner == TIMESTRAL`. C selezionato su split cronologico interno al training (ultimi 20%) minimizzando la MAE della policy; griglia {0,01; 0,1; 1; 10}. Soglia 0,5.
5. **Oracle** — sceglie il più vicino all'actual per finestra. **Usa il target reale, è un limite teorico non implementabile**, flaggato `uses_target=true` in ogni output.

Vincolo di informazione: le decisioni delle policy reali dipendono solo da righe di training + feature del test al cutoff. Nessuna usa actual, residuo futuro o label del vincitore del test. Verificato dai controlli automatici che perturbano gli esiti del test e verificano che le decisioni non cambino (l'oracle invece reagisce, a riprova del flag).

---

## STEP 5 — Walk-Forward Validation

Configurazione:

```text
500 finestre ordinate per cutoff
5 fold expanding-window, nessuno shuffle
training iniziale 200, test 60 per fold  →  300 finestre out-of-sample
purge 24h: nessun target di training può sovrapporsi a un target di test
preprocessing (scaler) e soglie fittati solo sul training del fold
iperparametro C selezionato solo dentro il training (split cronologico interno)
metriche aggregate e per fold, confronto paired sulle stesse finestre
CI: moving-block bootstrap (blocco 10, 1000 draw, seed 42)
```

### Aggregato out-of-sample (300 finestre)

| Policy | MAE | Δ vs Naive | CI95 | Share Tim | Win vs Naive |
|---|---:|---:|---:|---:|---:|
| Always Naive | 1.288,13 | 0,00 | — | 0% | — |
| Always Timestral | 1.323,90 | +35,77 | [−0,04, +78,69] | 100% | 44,7% |
| Regime rules | 1.293,95 | +5,81 | [−11,00, +18,76] | 40,3% | 17,3% |
| Logistic (L2) | 1.313,76 | +25,63 | [+6,05, +46,74] | 30,0% | 10,0% |
| Oracle | 1.215,57 | −72,56 | [−98,66, −54,29] | 44,7% | 44,7% |

### Per fold (Δ MAE vs Always Naive, USDT)

| Fold | Train | Test | Naive MAE | Tim | Rules | Logistic | Oracle |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 200 | 60 | 415,25 | +21,43 | +17,30 | +26,42 | −26,57 |
| 1 | 260 | 60 | 1.519,19 | +15,32 | +1,49 | +16,68 | −81,53 |
| 2 | 320 | 60 | 1.688,03 | +77,37 | **−1,87** | +53,62 | −131,86 |
| 3 | 380 | 60 | 1.781,62 | +73,13 | +22,31 | +36,06 | −69,60 |
| 4 | 440 | 60 | 1.036,58 | −8,39 | **−10,17** | −4,65 | −53,25 |

Le regole battono il naive solo nei fold 2 e 4 e solo di pochi USDT; la logistic solo nel fold 4. **Nessuna policy è consistente nei fold.**

### In-sample vs out-of-sample

| Policy | MAE in-sample (500) | MAE OOS (300) | Gap overfitting |
|---|---:|---:|---:|
| Always Naive | 1.176,92 | 1.288,13 | +111,21 |
| Always Timestral | 1.192,99 | 1.323,90 | +130,91 |
| Regime rules | **1.166,11** | 1.293,95 | +127,84 |
| Logistic (L2) | 1.171,87 | 1.313,76 | +141,89 |
| Oracle | 1.111,11 | 1.215,57 | +104,46 |

In-sample le regole sembrano battere il naive (−10,81 USDT); out-of-sample sono +5,81. È il pattern classico di overfitting su segnale debole e non stazionario.

### Controlli di leakage (9/9 PASS)

1. training precedente al test in ogni fold;
2. nessun target di training sovrapposto ai target di test (purge 24h);
3. tutte le policy valutate sulle stesse 300 finestre (confronto paired);
4. oracle è lower bound per le policy reali;
5–8. Always/Regime/Logistic: le decisioni non cambiano perturbando gli esiti del test;
9. l'oracle reagisce agli esiti perturbati e il flag `uses_target` è onesto.

Risultati: `data/experiments/S02-CP06B-selective-policy-v1/`
- `decisions.parquet` (1500 righe = 300 finestre × 5 policy) — SHA-256 `597b8f74ff8fbbcc8d9a9df8430786962419e3030aa700ade8dc732f4a28e65d`
- `fold_summary.json` — SHA-256 `bb683d9365de817ac3200b92cec5122c66de37d7885bc40a72d0c3770fe23482`
- `summary.json` — SHA-256 `98a8140f44837f4e2fcd1e9895c8fcd9955aaf929cf342e4023795db855ad1df`
- `manifest.json`: hash sorgente invariati (true).

Nota: `fit_states` in `fold_summary.json` riporta lo stato dell'ultimo fold; le soglie per-cell di ogni fold sono ricostruibili rieseguendo `evaluate_policies` (deterministico).

---

## STEP 6 — Dashboard

Nuova scheda **“Regime & Policy”** (read-only), componente `dashboard/components/regime_policy.py`:

- distribuzione Δ MAE con media e riferimento zero;
- win rate Timestral per trend/vol regime (full sample, terzili descrittivi dichiarati);
- tabella statistiche per regime con CI e flag `reliable`;
- errori Timestral/Naive nel tempo sul target h24;
- performance policy out-of-sample con CI e linea Always Naive;
- tabella in-sample vs out-of-sample con gap di overfitting;
- dettaglio decisioni USE_TIMESTRAL/USE_NAIVE per policy e fold, con spiegazione (cella, probabilità, C scelto);
- selettore periodo storico;
- pannello controlli di leakage;
- export CSV delle decisioni.

Verifica AppTest: 5 tab, 0 eccezioni, 13 grafici, 8 tabelle.

Configurazione (`config/dashboard.example.yaml`):

```yaml
forecast:
  regime_dataset_dir: data/datasets/S02-CP06B-regime-features-v1
  policy_results_dir: data/experiments/S02-CP06B-selective-policy-v1
```

---

## Riproducibilità

```bash
# STEP 1-3: errori + feature + statistiche regime (dataset additivo)
python -m crypto_decision_lab.regime_analysis

# STEP 4-5: policy walk-forward (esperimento additivo)
python -m crypto_decision_lab.policy_evaluation

# Test
pytest tests/test_regime_analysis.py tests/test_policy_evaluation.py -q   # 19 passed
pytest -q                                                                # 87 passed
```

Entrambi i comandi rifiutano di sovrascrivere directory esistenti e verificano gli hash sorgente prima/dopo.

---

## Test automatici

| Suite | Test | Copertura |
|---|---:|---|
| `tests/test_regime_analysis.py` | 9 | errori hand-computed, winner/direzioni, riconciliazione benchmark, causalità feature, guardia storico, terzili train-only, assegnazione regimi, bootstrap deterministico, flag sottogruppi |
| `tests/test_policy_evaluation.py` | 10 | fold cronologici+purge, rigetto dati insufficienti, apprendimento regola da segnale di training, soglie da training, selezione C e indipendenza dagli esiti del test, always/oracle, campioni paired e bound oracle, in-sample separato, verifica no-leakage, lettura dataset |
| Suite completa | **87** | tutte verdi |

---

## Limiti e prossimi passi

- 500 finestre restano un campione modesto per 9 celle di regime: diverse celle hanno 30–70 osservazioni.
- Il vantaggio Timestral del 2021 potrebbe riflettere condizioni di mercato non più riproducibili.
- La policy oracle (−72,56 USDT) indica margine teorico: il collo di bottiglia è il segnale delle feature, non il limite informativo assoluto.
- Prossimi candidati: feature di microstruttura (volume/taker), orizzonti diversi da h24, quantili Timestral invece del solo p50, modelli sequenziali semplici. Nessuno di questi è stato tentato in questa milestone.
- **Intern-Decision-0.8B non è stato addestrato**: sulla base di questi risultati, l'addestramento su queste feature non è giustificato.
