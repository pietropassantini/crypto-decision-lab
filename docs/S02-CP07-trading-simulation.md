# S02-CP07 · Decision Labeling & Offline Trading Simulation

**Repository:** `crypto-decision-lab`  
**Obiettivo:** simulatore deterministico LONG / SHORT / NO_TRADE su dati storici, con costi, walk-forward e nessuna integrazione exchange.  
**Stato:** **COMPLETE** — 500 finestre etichettate, 300 osservazioni OOS, 112 test PASS.  
**Verdetto:** **INCONCLUSIVE**. Nessuna policy semplice batte tutte le baseline appropriate con evidenza stabile; il momentum threshold è promettente ma non significativo contro NO_TRADE.  
**Intern-Decision-0.8B:** non addestrato.

---

## 1. Execution contract

Implementato in `src/crypto_decision_lab/decision_contract.py` (schema `cdl.cp07.decision.v1`).

### Semantica temporale

```text
bar(cutoff - 1h)   chiude al cutoff      -> la sua CLOSE è reference_price
                                            (unico prezzo usato per la decisione)
bar(cutoff)        apre al cutoff        -> ENTRY: open = primo prezzo disponibile
                                            dopo la decisione
bar(cutoff + 24h)  apre al cutoff + 24h  -> EXIT: open
```

Regole applicate e testate:

- l'entry bar è **sempre strettamente successiva** alla barra la cui close ha generato la decisione;
- `decision_timestamp == cutoff_timestamp <= entry_timestamp < exit_timestamp`;
- `exit_timestamp − entry_timestamp == horizon_hours == 24`;
- NO_TRADE non ha prezzi di esecuzione né costi e rendimento netto 0;
- nessuna leva; sizing 100% dell'equity corrente; una posizione alla volta;
- il segnale la cui entry cade prima dell'uscita della posizione precedente viene **saltato** e contato, non anticipato né sovrapposto.

Verifica dati: per tutte le 500 finestre esistono barre open a `cutoff` e `cutoff+24h`; la spaziatura minima tra cutoff consecutivi è 24h, quindi nella pratica non ci sono skip (0 in tutte le policy).

### Campi della decisione

| Campo | Significato |
|---|---|
| `decision_id` | `CP07:window-NNNN:<policy>` |
| `sample_id` | `S02-CP06A:window-NNNN` |
| `cutoff_timestamp` | cutoff canonico |
| `decision_timestamp` | uguale al cutoff |
| `entry_timestamp` | open bar al cutoff |
| `exit_timestamp` | open bar al cutoff+24h |
| `horizon_hours` | 24 |
| `action` | LONG / SHORT / NO_TRADE |
| `reference_price` | close anchor a cutoff−1h |
| `execution_price` | entry open con slippage direzionale |
| `exit_price` | exit open con slippage direzionale |
| `fee_rate`, `slippage` | per lato, frazione del nozionale |
| `gross_return_pct`, `net_return_pct` | percento |
| `data_version`, `policy_version` | provenienza |

### Costi e rendimenti

```text
LONG : net = [exit_exec × (1 − fee)] / [entry_exec × (1 + fee)] − 1
SHORT: net = [entry_exec × (1 − fee)] / [exit_exec × (1 + fee)] − 1
entry_exec/exit_exec includono lo slippage direzionale
```

Default: **fee 0,10% per side**, **slippage 0,05% per side**. Il modello è self-financing, senza leva, con equity composta.

---

## 2. Simulator

Implementato in `src/crypto_decision_lab/trading_simulator.py`.

- equity iniziale configurabile (default 10.000 USDT);
- equity curve, max drawdown, trade count, win rate, exposure time, profit factor;
- una posizione alla volta, 100% equity, nessuna leva;
- NO_TRADE = equity invariata, zero costi;
- skip di segnali sovrapposti conteggiato e registrato nel ledger;
- nessun ordine reale, nessuna connessione exchange.

Metriche per policy: `trades`, `skipped_signals`, `no_trade`, `total_return_pct`, `max_drawdown_pct`, `win_rate_pct`, `profit_factor`, `avg_net_return_pct`, `exposure_pct`.

---

## 3. Historical decision labels

Implementate in `src/crypto_decision_lab/decision_labeling.py` (schema `cdl.cp07.label.v1`).

Per ogni finestra:

```text
long_net_pct   = rendimento netto LONG con costi
short_net_pct  = rendimento netto SHORT con costi
no_trade_net   = 0
best_action    = argmax(long, short, no_trade)
margin_vs_second_pct = best − secondo migliore
ambiguous      = margin < 0,10% (soglia economica configurabile)
```

Distribuzione su 500 finestre:

| Label | Finestre |
|---|---:|
| SHORT | 226 |
| LONG | 210 |
| NO_TRADE | 64 |
| Ambiguous (`margin < 0,1%`) | 36 (7,2%) |

Rendimento medio: LONG −0,267%, SHORT −0,224%, margine medio 1,981%.

**Queste label sono retrospettive e usano il futuro.** Non vengono mai usate come feature: le policy reali leggono solo `timestral_return_24h`, `momentum_168h` e feature di cutoff; il controllo automatico verifica che le decisioni non cambino perturbando `best_action`.

Dataset: `data/datasets/S02-CP07-decision-labels-v1/`
- `labels.parquet` — SHA-256 `71c1e9cfad30c21a16de884ff147bb0dc41a4d609d6086c20f2fa39fce87e7aa`
- `manifest.json`: source hash invariati (`true`).

---

## 4. Baseline policies

Implementate in `src/crypto_decision_lab/trading_policy.py`.

| Policy | Segnale | Target usato |
|---|---|---|
| Always NO_TRADE | nessuno | no |
| Always LONG | nessuno | no |
| Always SHORT | nessuno | no |
| Timestral threshold | `timestral_return_24h` canonico | no |
| Momentum threshold | `momentum_168h` | no |
| Oracle retrospective | `best_action` realizzato | **sì, limite teorico** |

Timestral threshold: LONG se rendimento previsto > soglia, SHORT se < −soglia, altrimenti NO_TRADE. Soglie selezionate **solo sul training di ogni fold** su griglia {0%, 0,1%, 0,2%, 0,5%, 1%, 2%, 5%}, massimizzando il rendimento composto di training.

Soglie scelte:

- Timestral: 0,1% nei fold 0–3, 2% nel fold 4 (nessun trade);
- Momentum: 5% in tutti i fold.

L'oracle è chiaramente flaggato `uses_target=true` e non è una policy implementabile.

---

## 5. Walk-forward evaluation

Riusa `make_folds` di CP06B:

```text
5 fold cronologici expanding-window
500 finestre ordinate per cutoff
train iniziale 200, test 60 per fold -> 300 finestre OOS
purge 24h = holding period: nessun target/uscita di training può sovrapporsi
             all'entry di test
nessuno shuffle
soglie e iperparametri fit solo sul training
confronto paired sulle stesse 300 finestre
CI: moving-block bootstrap (blocco 10, 1000 draw, seed 42)
```

### Performance out-of-sample (costi base)

| Policy | Trades | NO_TRADE | Total return | Avg net/trade | Win rate | Max DD | Profit factor |
|---|---:|---:|---:|---:|---:|---:|---:|
| Always NO_TRADE | 0 | 300 | 0,00% | 0,00% | — | 0,00% | — |
| Always LONG | 300 | 0 | **−48,95%** | −0,1935% | 43,33% | −58,30% | 0,803 |
| Always SHORT | 300 | 0 | **−67,62%** | −0,3451% | 42,00% | −69,82% | 0,673 |
| Timestral threshold | 159 | 141 | **−58,11%** | −0,2718% | 40,25% | −59,52% | 0,566 |
| Momentum threshold | 106 | 194 | **+39,18%** | +0,1256% | 43,40% | −21,18% | **1,411** |
| Oracle retrospective | 256 | 44 | **+8.173,74%** | +1,4979% | 100% | 0,00% | ∞ |

### Confronto paired (differenza media net return per trade)

| Policy vs baseline | Δ medio | CI95 | Batte baseline? |
|---|---:|---:|---|
| Momentum vs NO_TRADE | +0,1256% | [−0,0826%, +0,3318%] | **No** (CI include 0) |
| Momentum vs Always LONG | +0,3192% | [+0,0318%, +0,6082%] | Sì |
| Momentum vs Always SHORT | +0,4707% | [+0,0297%, +0,8816%] | Sì |
| Timestral vs NO_TRADE | −0,2718% | [−0,4649%, −0,0973%] | No, significativamente peggio |
| Timestral vs Always LONG | −0,0783% | [−0,4492%, +0,2629%] | No |
| Timestral vs Always SHORT | +0,0732% | [−0,2854%, +0,4100%] | No |

Il momentum threshold batte LONG e SHORT ma **non** batte Always NO_TRADE in modo statisticamente significativo. Poiché NO_TRADE è la baseline appropriata in assenza di segnale, il gate non può essere GO.

### Per fold — Momentum threshold

| Fold | Train | Soglia | Trades | Total return | Max DD |
|---:|---:|---:|---:|---:|---:|
| 0 | 200 | 5% | 15 | +2,61% | −3,95% |
| 1 | 260 | 5% | 35 | +24,97% | −11,59% |
| 2 | 320 | 5% | 23 | +7,62% | −7,39% |
| 3 | 380 | 5% | 19 | **−13,72%** | −15,91% |
| 4 | 440 | 5% | 14 | +16,90% | −3,13% |

Il fold 3 è negativo: la performance non è monotona né stabile.

### Per anno — Momentum threshold

| Anno | Finestre | Trades | Total return | Win rate |
|---:|---:|---:|---:|---:|
| 2023 | 50 | 11 | +1,75% | 6,0% |
| 2024 | 89 | 45 | +32,52% | 21,3% |
| 2025 | 88 | 30 | **−3,13%** | 13,6% |
| 2026 | 73 | 20 | +6,55% | 16,4% |

### Per regime — Momentum threshold

| Regime | Finestre | Trades | Total return |
|---|---:|---:|---:|
| SIDEWAYS | 119 | 0 | 0,00% |
| TREND_UP | 101 | 57 | +27,88% |
| TREND_DOWN | 80 | 49 | +8,83% |

La policy non opera in SIDEWAYS e guadagna nei trend. È coerente con il segnale momentum, ma il campione è modesto e dipende dal periodo.

### Sensitivity fee × slippage — Momentum threshold

Total return OOS su 9 scenari:

| Fee \ Slippage | 0% | 0,05% | 0,10% |
|---|---:|---:|---:|
| **0%** | +119,10% | +63,94% | +11,40% |
| **0,05%** | +63,94% | +11,40% | +39,18%* |
| **0,10%** | +11,40% | +39,18%* | +25,18% |

\* i valori con la stessa somma fee+slippage coincidono per costruzione; la griglia è simmetrica.

Il momentum threshold resta positivo in tutti gli scenari, ma il risultato base +39,18% non è accompagnato da significatività vs NO_TRADE.

### Controlli di leakage (5/5 PASS)

1. training precede test in ogni fold;
2. purge 24h = holding period, nessuna sovrapposizione;
3. tutte le policy sulle stesse 300 finestre (paired);
4. le policy reali ignorano `best_action` retrospettivo (0 decision changes);
5. l'oracle è upper bound ed è flaggato `uses_target=true`.

Esperimento: `data/experiments/S02-CP07-trading-simulation-v1/`
- `decisions.parquet` (1.800 righe = 300 × 6 policy) — SHA-256 `66c45b672f96c33f31049941957bb6aef5141d0d237b7d593a89055b354e9cf8`
- `fold_summary.json` — `37014ddf2a9697a501f9f07f37f171d2b1e1de160db243c7993c8a70baa396a9`
- `sensitivity.json` — `dede55898022afe9c058d99b97d3380d71f886a3dd34ac5c428d52e99c9e9db1`
- `summary.json` — `629136fc08a6185d494516d92d9ee09fe0af9be27257d0b002e6aa57c92b18ca`
- `manifest.json`: source hash invariati.

---

## 6. Verdetto

**INCONCLUSIVE.**

- Always NO_TRADE (0%) è la migliore baseline robusta dopo costi.
- Always LONG e Always SHORT perdono rispettivamente 48,95% e 67,62%.
- Timestral threshold perde 58,11%, con CI negativo vs NO_TRADE: il segnale direzionale Timestral non è monetizzabile con questa regola.
- Momentum threshold guadagna +39,18% con PF 1,41 e DD −21,18%, batte significativamente LONG/SHORT, ma **non** batte NO_TRADE (CI include 0), ha un fold negativo e un anno negativo.
- L'oracle +8.173% è solo un limite teorico: **non giustifica alcun training di Intern-Decision-0.8B**.

Nessuna policy è promossa. Il momentum threshold merita un'analisi successiva con più dati, diversa soglia/volatilità e test su costi più realistici, ma non è un GO di produzione.

---

## 7. Dashboard

Nuova scheda **Trading Simulation** (read-only), componente `dashboard/components/trading_simulation.py`:

- equity curve OOS per policy;
- drawdown;
- segnali LONG/SHORT/NO_TRADE;
- performance per policy con tabella;
- sensitivity fee × slippage (heatmap + tabella);
- trade log filtrabile e scaricabile;
- dettaglio decisione (window/action/reference/execution/net);
- verdetto e controlli leakage;
- filtro periodo.

AppTest: 6 tab, 0 eccezioni, 18 grafici, 11 tabelle. Health endpoint `ok`.

Config (`config/dashboard.example.yaml`):

```yaml
forecast:
  trading_label_dir: data/datasets/S02-CP07-decision-labels-v1
  trading_experiment_dir: data/experiments/S02-CP07-trading-simulation-v1
```

---

## 8. Riproducibilità

```bash
# 1. label retrospettive (costi default)
python -m crypto_decision_lab.decision_labeling

# 2. simulazione walk-forward e sensitivity
python -m crypto_decision_lab.trading_policy

# 3. test
pytest tests/test_trading_simulator.py tests/test_decision_labeling.py tests/test_trading_policy.py -q
pytest -q
```

I comandi rifiutano di sovrascrivere directory esistenti e verificano gli hash sorgente prima/dopo.

---

## 9. Test automatici

| Suite | Test | Copertura |
|---|---:|---|
| `tests/test_trading_simulator.py` | 11 | formule net/gross LONG/SHORT, NO_TRADE, slippage, contratto entry, self-financing, determinismo, skip sovrapposizioni, drawdown/win rate/profit factor, exposure, validazione |
| `tests/test_decision_labeling.py` | 6 | label LONG/SHORT/NO_TRADE, costi, ambiguità, monotonicità costi, summary |
| `tests/test_trading_policy.py` | 8 | soglie train-only, decisioni indipendenti dal label retrospettivo, walk-forward deterministico e paired, oracle bound, paired comparison, verdict NO-GO, sensitivity grid |
| Suite completa | **112** | tutte verdi |

---

## 10. Acceptance gates

| Gate | Esito |
|---|---|
| Nessun leakage temporale | PASS (5/5 controlli + test) |
| Simulazione deterministica | PASS (test ripetizione) |
| Costi documentati | PASS (fee/slippage per side, sensitivity) |
| Metriche OOS riproducibili | PASS (hash e manifest) |
| Test automatici PASS | PASS (112) |
| Dataset originali intatti | PASS (source hash invariati) |
| Nessun training Intern-Decision-0.8B | PASS (non eseguito) |
| Nessuna integrazione exchange | PASS (solo dati locali) |

**Gate di promozione: NO-GO per produzione / INCONCLUSIVE per ulteriore ricerca.**
