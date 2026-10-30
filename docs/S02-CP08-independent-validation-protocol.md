# S02-CP08-04 · Independent Validation Protocol & Research Freeze

**Repository:** `crypto-decision-lab`  
**Scope:** protocollo di validazione indipendente, pre-registrato e immutabile, per il Momentum Threshold congelato e per le future strategie di conferma.  
**Data congelamento protocollo:** 2026-10-30 · **Stato:** **CP08-04 COMPLETE — PROTOCOL FROZEN, VALIDATION NOT STARTED**  
**Gate tecnico:** **PASS** — 15 controlli: 14 PASS, 1 WARN, 0 FAIL. Il WARN dichiara che non esiste ancora alcun dato indipendente.  
**Gate strategico:** **NO-GO** — invariato.  
**Validazione indipendente:** **NON COMPLETATA / NOT_EVALUATED** — nessun dato successivo al `2026-10-23T16:00:00Z` è disponibile nel repository.  
**Intern-Decision-0.8B:** **NON AVVIATO**.

> **Dichiarazione di metodo.** Questo documento non riporta un risultato di validazione: riporta le regole con cui una validazione potrà essere eseguita **una sola volta**, su dati realmente nuovi, e con criteri fissati *prima* di vederli. Tutto ciò che è già stato osservato — le 500 finestre CP07, i 300 OOS, le superfici CP08-03, la coda 2026-10-23 00:00–16:00 — è classificato come **ricerca** e non è utilizzabile come evidenza indipendente. Un PASS tecnico del protocollo non è un GO.

---

## 1. Obiettivo e non-obiettivi

### 1.1 Obiettivo

1. Congelare il benchmark Momentum originale e tutte le configurazioni CP07/CP08.
2. Registrare formalmente quali dati storici sono già stati usati per ricerca e selezione.
3. Fissare una policy di riferimento immutabile.
4. Definire uno scenario primario realistico: latenza 1h, fee e slippage espliciti, costi SHORT.
5. Pre-registrare la griglia di soglie e la regola di selezione train-only.
6. Definire un protocollo walk-forward su dati futuri non ancora osservati.
7. Definire criteri GO/NO-GO per rendimento netto, drawdown, stabilità e incertezza statistica.
8. Separare la validazione di una strategia congelata dalla ricerca esplorativa di nuove strategie.
9. Preparare una configurazione versionata per il futuro Momentum Confirmation Engine.
10. Implementare controlli automatici che impediscano il riuso improprio del periodo di test indipendente.

### 1.2 Non-obiettivi (vietati in questo step)

- Nessun nuovo training, nessuna nuova inferenza Timestral, nessun download di dati, nessun trading reale.
- Nessun nuovo backtest ottimizzato sul periodo di ricerca già osservato.
- Nessuna modifica agli artefatti storici CP07/CP08-01/02/03.
- Nessun parametro scelto guardando il periodo OOS già osservato.
- Nessuna dichiarazione di validazione indipendente completata senza dati realmente indipendenti.

---

## 2. Cronologia dei dati e ruoli

| Intervallo (UTC) | Ruolo | Utilizzo | Evidenza indipendente? |
|---|---|---|---|
| 2021-01-01 → 2026-10-23T16:00 | **Ricerca già osservata** | CP07 (500 finestre, 300 OOS, 5 fold), CP08-01/02/03 | **No** |
| 2026-10-23T00:00 → 2026-10-23T16:00 | **Coda osservata ma non usata** | presente nello snapshot, nessuna label/decisione/target/metrica la usa | **No** (era comunque osservabile) |
| ≥ 2026-10-23T16:00 | **Validazione indipendente** | nessun dato presente nel repository | **Sì, quando esisterà** |

| Fatto verificato | Valore |
|---|---|
| Finestre di ricerca (`labels.parquet`) | **500** |
| Finestre OOS CP07 | **300** (2023-06-10 → 2026-10-22) |
| Fold di ricerca | **5** |
| Ultimo target usato | **2026-10-23T00:00:00Z** |
| Fine esclusiva snapshot congelato | **2026-10-23T16:00:00Z** |
| Inizio validazione indipendente | **2026-10-23T16:00:00Z** |
| Dati indipendenti disponibili | **0** |

La coda osservata è dichiarata esplicitamente per evitare un errore sottile: *«non l'ho usata nei calcoli»* non significa *«non l'ho vista»*. Poiché era nello snapshot durante la ricerca, non è evidenza indipendente e il protocollo la rifiuta.

Separazione macchina-verificabile (`data_separation` nel manifest):

```text
research_last_used_target_end = 2026-10-23T00:00:00Z
observed_tail                 = [2026-10-23T00:00:00Z, 2026-10-23T16:00:00Z)
independent_start             = 2026-10-23T16:00:00Z
research_target_before_tail   = true
tail_before_independent_start = true
independent_after_research    = true
tail_not_independent          = true
all_ok                        = true
```

---

## 3. Benchmark Momentum congelato

La policy di riferimento è quella di CP07, riprodotta bit-per-bit da CP08-03. Non è un target e non è modificabile in questo step: è solo l'anchor di riconciliazione.

| Metrica | Valore congelato |
|---|---:|
| Policy | `momentum_threshold_5pct` |
| Segnale | `momentum_168h` |
| Soglia | **5%** |
| Direzione | simmetrica (LONG > +5%, SHORT < −5%, altrimenti NO_TRADE) |
| Orizzonte | 24h |
| Entry / exit | open della barra al cutoff / open della barra a cutoff+24h |
| Capitale iniziale | 10.000 USDT |
| Sizing | 100% equity, nessuna leva, una posizione alla volta |
| Costi di riferimento | fee 0,10% + slippage 0,05% per lato |
| Trade | **106** |
| Rendimento composto | **+39,175764%** |
| Equity finale | 13.917,5764 USDT |
| PF monetario | **1,3295465** |
| PF percentuale | 1,4111430 |
| Max DD exit-marked | **−21,180367%** |
| Max DD mark-to-market | **−27,202367%** |
| CI 95% rendimento composto | **[−24,0057%, +148,1896%]** |
| P(rendimento > 0) | **84,40%** |

Riferimenti CP08-03 usati per definire lo scenario primario:

| Scenario CP08-03 | Rendimento | PF monetario | Max DD MTM | CI 95% | P(>0) |
|---|---:|---:|---:|---:|---:|
| Latenza 1h, costi base | +23,590451% | 1,2216575 | −30,760346% | [−29,5255%, +108,8953%] | 73,90% |
| **Envelope conservativo** (1h + 0,20%/lato + carry SHORT 0,14%) | **+3,785384%** | **1,0348455** | **−37,351216%** | **[−41,6273%, +74,1906%]** | **51,80%** |

Tutti i 17 artefatti dichiarati nel protocollo hanno hash SHA-256 verificato (CP07 decisions/labels/fold summary/sensitivity/manifest, CP06B features/errors, snapshot manifest/anomalies, CP08-01/02/03 manifest e summary/scenario metrics/checks). Il confronto è in `frozen_benchmark.json` e nel controllo `CP08-04.4`, con delta massimo **< 1e−9**.

---

## 4. Immutabilità, versioning e manifest

| Elemento | Percorso | SHA-256 |
|---|---|---|
| Configurazione protocollo (YAML) | `config/protocols/momentum-independent-validation-v1.yaml` | `b5a7d248bcfde6b02b879292097ca8f758f66e7b7528dc3d6db45237f246b80e` |
| Sidecar hash protocollo | `config/protocols/momentum-independent-validation-v1.yaml.sha256` | pin dell'hash sopra |
| Hash canonico (JSON ordinato) | calcolato dal modulo | `7b32d50693906f8d818f2cb839ad70b489b5f124497635f203b0c98ebd792eb4` |
| Specifica Momentum Confirmation Engine | `config/protocols/momentum-confirmation-engine-v1.yaml` | `3953117d87ee059be2f1fb5b8d69390272369761b70f28e957cdea4f491f1e67` |
| Archivio protocollo | `data/experiments/S02-CP08-independent-validation-protocol-v1/` | manifest `eabe2d11b0441fd2771ec41099006bf74477103f92da24a08f4aa409ec20c87a` |

Regole:

1. **Nessuna modifica in place.** Un cambiamento richiede `protocol_version` nuovo, file YAML nuovo (`...-v2.yaml`), sidecar nuovo e archivio nuovo. La versione vecchia resta archiviata.
2. **Sidecar obbligatorio.** Il controllo `CP08-04.2` fallisce se il file YAML non corrisponde byte-per-byte all'hash del sidecar. Serve a intercettare modifiche accidentali.
3. **Archivio additivo.** `write_artifacts` rifiuta di sovrascrivere una directory esistente e verifica gli hash degli artefatti sorgente *prima e dopo* la scrittura (`frozen_artifacts_unchanged: true`, `original_artifacts_modified: false`).
4. **Copia byte-identica.** L'archivio contiene copie esatte dei due YAML (`momentum-independent-validation-v1.yaml`, `momentum-confirmation-engine-v1.yaml`), con hash identici ai sorgenti.

> **Limite di fiducia, dichiarato.** I controlli sono strutturali: impediscono riusi, scambi e modifiche accidentali, e rendono ogni scostamento visibile nel manifest. Non sono una difesa crittografica contro un operatore che modifichi insieme YAML, sidecar e archivio: quello sarebbe un cambio di versione deliberato e fraudolento, fuori dal perimetro del protocollo.

---

## 5. Policy di riferimento immutabile

| Campo | Valore | Modificabile? |
|---|---|---|
| `signal_column` | `momentum_168h` | **No** |
| `threshold` | 0,05 | **No** (selezione solo training, griglia chiusa) |
| `horizon_hours` | 24 | **No** |
| `direction` | simmetrica | **No** |
| `initial_capital_usdt` | 10.000 | **No** |
| `sizing` | 100% equity, nessuna leva | **No** |
| `overlap_rule` | segnale in overlap saltato e contato | **No** |
| `no_trade_rule` | flat, zero costo, nessuna occupazione del mercato | **No** |

La policy è congelata *come oggetto di validazione*: il protocollo non può cambiarla per farla passare. Il Momentum Confirmation Engine, quando esisterà, potrà solo **downgradare** una decisione (LONG/SHORT → NO_TRADE), mai invertire la direzione o cambiare soglia/orizzonte.

---

## 6. Scenario primario realistico

Lo scenario primario è l'unico che **gatta** il verdetto. Coincide con l'envelope conservativo di CP08-03, l'unico scenario vicino al break-even.

| Parametro | Valore |
|---|---|
| `id` | `realistic_1h_cost20_carry` |
| Latenza | **1h** (entry/exit sugli open reali della barra +1h; holding sempre 24h) |
| Fee | **0,10%** per lato |
| Slippage | **0,10%** per lato |
| Costo totale per lato | **0,20%** |
| Carry SHORT | funding **0,03%/8h** + borrow **0,05%/giorno** → **0,14% per trade SHORT** |
| Stato carry | **`HYPOTHESIS_NOT_HISTORICAL`** — ipotesi parametrica, non dato storico |
| Applicazione carry | **solo SHORT**, al nozionale, alla chiusura del hold |
| Mark-to-market | orario, valore di liquidazione (fee/slippage di uscita inclusi) |
| Carry nel MTM | accreditato solo all'uscita: il DD MTM è un limite inferiore |
| Bootstrap | moving block, blocco 10, 2.000 draw, seed 42, confidenza 95% |

Scenari secondari **non gating** (descrittivi): CP07 base 0h/0,15%, latenza 1h con costi base, latenza 1h + 0,20% senza carry. Non possono né alzare né abbassare il verdetto.

> Il carry SHORT resta un'ipotesi perché nel repository esistono solo prezzi spot: **non c'è una serie storica di funding o borrow**. Il protocollo non inventa dati e non spaccia lo scenario per una stima empirica.

---

## 7. Griglia di soglie e regola di selezione (pre-registrate)

| Elemento | Valore |
|---|---|
| Griglia | `0% · 0,5% · 1% · 2% · 3% · 4% · 5% · 6% · 7% · 8% · 9% · 10% · 12% · 15% · 20%` |
| Valori obbligatori | **5%, 8%, 10%** |
| Regola | argmax del **multiplo composto netto sul training** del fold |
| Pareggio | vince la soglia più bassa nell'ordine della griglia |
| Split | **train-only** |
| Rifit | una volta per fold |
| Training | tutte le finestre con cutoff precedente al primo cutoff di test, con purge |
| Griglia chiusa | **sì** — nessun ampliamento dopo il congelamento |
| Ampliamento dopo dati indipendenti | **vietato** |
| Provenienza | copiata verbatim da `PREREGISTERED_THRESHOLD_GRID` di CP08-03 |

Il modulo verifica che la griglia del protocollo sia **identica** a quella di CP08-03 (`CP08-04.7`): una divergenza è un FAIL, non un warning.

---

## 8. Walk-forward su dati futuri

| Parametro | Valore |
|---|---|
| Tipo | **expanding** |
| Minimo storico di training | 200 finestre (ricerca + indipendenti precedenti) |
| Blocco di test | **60 finestre** consecutive |
| Purge | **24h** (nessun target di training può sovrapporsi a un target di test) |
| Fold minimi per conclusività | **5** (300 finestre indipendenti) |
| Fold massimi | 10 |
| Finestre residue | riportate, **non** usate per il verdetto |
| Ordine | cronologico |
| Look intermedi | ammessi, **descrittivi e non gating** |
| Valutazione finale | **una sola** per versione di protocollo |

Prima del primo test block il training contiene l'intero periodo di ricerca + le finestre indipendenti già chiuse. La selezione della soglia avviene **sempre e solo** sul training del fold, con lo stesso purge di CP07: un test dimostra che il fold builder del protocollo coincide, quando lo storico è 200 finestre, con il `make_folds` auditato di CP07 (train/test/purge identici).

---

## 9. Criteri GO / NO-GO / INCONCLUSIVE

### 9.1 Requisiti minimi di campione (per un verdetto conclusivo)

| Requisito | Soglia |
|---|---:|
| Trade eseguiti | ≥ 100 |
| Fold | ≥ 5 |
| Giorni indipendenti | ≥ 180 |

### 9.2 Criteri GO (tutti necessari)

| ID | Metrica | Operatore | Soglia | Significato |
|---|---|---|---:|---|
| `net_return_positive` | `total_return_pct` | `>` | 0 | rendimento netto positivo |
| `net_return_ci_low_positive` | `ci_return_low_pct` | `>` | 0 | incertezza statistica: CI 95% sopra zero |
| `profit_factor_min` | `profit_factor_usdt` | `>=` | 1,10 | qualità monetaria |
| `max_drawdown_mtm_limit` | `max_drawdown_mtm_pct` | `>=` | −30 | rischio di percorso |
| `positive_folds` | `positive_folds` | `>=` | 3 | stabilità tra fold |
| `max_fold_share` | `max_abs_fold_share_pct` | `<=` | 60 | nessun fold spiega oltre il 60% del P&L |
| `leave_top5_positive` | `return_after_removing_top5_pct` | `>` | 0 | non dipendere da 5 trade |
| `thresholds_in_grid` | `selected_thresholds_in_grid` | `==` | 1 | nessuna soglia fuori griglia |
| `no_protocol_violation` | `protocol_violations` | `==` | 0 | nessuna violazione di boundary/hash/riuso |

### 9.3 Criteri NO-GO (un trigger è sufficiente)

| ID | Metrica | Operatore | Soglia | Significato |
|---|---|---|---:|---|
| `net_return_non_positive` | `total_return_pct` | `<=` | 0 | rendimento non positivo |
| `profit_factor_below_one` | `profit_factor_usdt` | `<` | 1,0 | PF monetario sotto 1 |
| `drawdown_breach` | `max_drawdown_mtm_pct` | `<` | −40 | rischio oltre il limite di rischio |
| `concentration_failure` | `return_after_removing_top5_pct` | `<=` | 0 | profitto interamente nei top-5 |
| `majority_folds_negative` | `positive_folds` | `<=` | 2 | instabilità tra fold |
| `protocol_violation` | `protocol_violations` | `>` | 0 | violazione anti-riuso/boundary/hash |

### 9.4 Regola di decisione (deterministica)

```text
NO-GO        se almeno un criterio NO-GO è soddisfatto (anche con campione piccolo)
INCONCLUSIVE se i requisiti minimi di campione non sono soddisfatti e nessun NO-GO scatta
GO           se i requisiti di campione sono soddisfatti e TUTTI i criteri GO passano
INCONCLUSIVE in tutti gli altri casi (qualche criterio GO non passa, nessun hard-fail)
```

Un look intermedio produce sempre `INTERIM_DESCRIPTIVE` e non può mai essere GO.

### 9.5 Perché questi criteri e non altri

- Sono fissati **prima** che esistano dati indipendenti, e sono deliberatamente più severi di una ripetizione del risultato di ricerca (es. DD MTM ≤ 30% contro i −37,35% dell'envelope di ricerca).
- Sono **criteri di policy**, non parametri stimati: derivano dai limiti di rischio e dalla struttura dell'edge discusso in CP08-01/03, non da un fit sui dati.
- **Non possono essere rilassati** dopo aver visto i dati indipendenti: un rilassamento richiede una nuova versione del protocollo e un nuovo periodo indipendente, e la versione vecchia resta archiviata con il suo esito.

---

## 10. Validazione di una strategia congelata vs ricerca esplorativa

| Dimensione | **Validazione** (questo protocollo) | **Ricerca esplorativa** |
|---|---|---|
| Dati utilizzabili | solo finestre con cutoff ≥ 2026-10-23T16:00Z | periodo di ricerca 2021–2026 |
| Parametri | congelati, griglia chiusa, selezione train-only | possono essere ipotizzati e confrontati |
| Può produrre GO? | **Sì**, una sola volta e solo con tutti i criteri GO | **No, mai** |
| Può modificare la policy? | **No** | Sì, ma solo come nuova ipotesi da pre-registrare |
| Output | verdetto GO / NO-GO / INCONCLUSIVE | descrizione, superficie, sensibilità |
| Riutilizzo | vietato dopo la valutazione finale | consentito, ma resta non-GO |
| Etichettatura | `gating: true/false` dal registro | `exploratory: true` |

Regola di separazione: un risultato esplorativo (per esempio "il 6% è la soglia OOS migliore" in CP08-03) **non può** diventare un parametro del protocollo. Se una nuova idea nasce dalla ricerca, deve essere congelata in un nuovo protocollo **prima** di leggere nuovi dati indipendenti.

---

## 11. Controlli automatici anti-riuso

| Controllo | Implementazione | Cosa impedisce |
|---|---|---|
| **Boundary guard** | `validate_independent_windows` | cutoff prima dell'inizio indipendente, target dentro la ricerca o la coda osservata, holding ≠ 24h, duplicati |
| **Observed-tail guard** | `check_data_separation` + `validate_independent_windows` | usare la coda 2026-10-23 00:00–16:00 come evidenza |
| **Fold guard** | `build_independent_folds` | blocchi di test che contengono finestre di ricerca; history oltre il periodo congelato; history < 200 |
| **Protocol hash pin** | `evaluate_independent(expected_protocol_sha256=...)` | eseguire con una copia modificata del protocollo |
| **Dataset pinning** | `ValidationRegistry.register_dataset` | dataset non registrato o hash diverso |
| **One final look** | `ValidationRegistry.record_evaluation` | seconda valutazione finale per la stessa versione |
| **Interim non-gating** | `decide_verdict(final=False)` + registro | promozione da un look intermedio |
| **Immutable config** | sidecar SHA-256 + archivio + check `CP08-04.2` | modifica accidentale del YAML |
| **Archive additive** | `write_artifacts` | sovrascrittura dell'archivio e modifica degli artefatti storici |
| **Closed grid** | `validate_protocol` | ampliamento della griglia dopo il congelamento |

Semantica del registro (append-only JSONL, default `data/validation_registry/`):

```text
datasets.jsonl     dataset_id, sha256, role=independent, registered_utc, coverage
evaluations.jsonl  protocol_id/version/hash, dataset_id/hash, final, verdict, metrics_sha256
```

Regole: il dataset deve essere registrato **prima** della valutazione; la stessa `dataset_id` non può cambiare hash; dopo una valutazione `final` nessuna ulteriore valutazione della stessa versione è ammessa; i look `final=false` sono ammessi ma forzati a `INTERIM_DESCRIPTIVE` e `gating=false`.

> **Limite di fiducia, ribadito.** Il boundary guard è temporale e strutturale. Se un operatore riscrivesse i timestamp di un dataset di ricerca fingendo che sia futuro, il modulo non potrebbe distinguerlo da un dataset nuovo: la provenienza va verificata a monte (fonte, manifest, chunk, hash) e registrata. Il protocollo rende il riuso *esplicito e tracciato*, non fisicamente impossibile.

---

## 12. Momentum Confirmation Engine · configurazione versionata

`config/protocols/momentum-confirmation-engine-v1.yaml` — **status: `SPECIFICATION_ONLY`**, `runnable: false`, `active rules: 0`.

| Elemento | Definizione |
|---|---|
| Base | protocollo `momentum-independent-validation` v1.0.0, policy `momentum_threshold_5pct` |
| Interfaccia | `confirm(base_decision, features_at_cutoff) -> {decision, confirmation, confidence}` |
| Determinismo | sì · stateful: no · input causali: sì |
| Input ammessi | solo feature cutoff-only CP06B (`rsi14`, `atr_pct`, `volatility_24h/168h`, `momentum_24h/168h`, `dist_sma50_pct`, `trend_strength_pct`, `volume_ratio`) |
| Input vietati | `best_action`, `best_net_pct`, `long/short_net_pct`, `long/short_gross_pct`, `entry_open`, `exit_open`, `actual_price_24h`, qualunque target/futuro |
| Decision contract | può solo **downgradare** la decisione base; non può invertire la direzione |
| Regole attive | **nessuna** — `rules.active = []` |
| Provenienza vietata | bucket di volatilità CP08-02 (`VOL_HIGH` negativo) e superficie OOS CP08-03 (6%) perché osservati sul periodo OOS |
| Promozione | richiede una nuova versione del protocollo + una nuova valutazione indipendente; l'engine non può resuscitare una base che fallisce |

Il file esiste per versionare l'interfaccia *prima* che qualcuno proponga una regola: qualunque regola futura dovrà essere un nuovo `engine_version` con YAML congelato **prima** della lettura dei dati indipendenti.

---

## 13. Runbook per quando arriveranno i dati indipendenti

> Da eseguire **una sola volta** per versione. Nessun passaggio qui sotto è stato eseguito in CP08-04.

1. **Acquisizione (fuori da questo modulo).** Ottenere dati BTCUSDT 1h successivi a `2026-10-23T16:00:00Z`, come estensione continua dello snapshot congelato. Nessun download è autorizzato da questo protocollo.
2. **Costruzione del dataset.** Generare le finestre con lo stesso contratto di CP07 (cutoff, entry/exit open, 24h, feature `momentum_168h` calcolata solo su barre `< cutoff`). Salvare il dataset e il suo manifest.
3. **Verifica di continuità.** Confrontare l'inizio del nuovo dataset con la fine dello snapshot (nessun buco non spiegato, stesso simbolo/interval, chunk e hash verificati).
4. **Registrazione.** Registrare il dataset nel registro append-only con `dataset_id`, `sha256`, coverage. Il dataset assume ruolo `independent` solo dopo i controlli di boundary.
5. **Boundary check.** `validate_independent_windows(...)` deve passare: ogni cutoff ≥ `independent_start`, ogni target > ultimo target di ricerca, holding 24h, nessun duplicato.
6. **Verifica dell'hash di protocollo.** Confrontare `canonical_sha256` del protocollo in uso con `manifest.json` dell'archivio CP08-04.
7. **Una sola valutazione finale.** `evaluate_independent(..., registry=..., expected_protocol_sha256=..., final=True)`. Il registro scrive l'esito e brucia il periodo.
8. **Decisione.** Applicare `decide_verdict`. GO solo se tutti i criteri GO passano; altrimenti NO-GO o INCONCLUSIVE.
9. **Archiviazione.** Salvare il report di valutazione in una nuova directory di esperimento (additiva), con manifest e hash. Non modificare l'archivio CP08-04.

Snippet operativo (da eseguire solo con dati reali nuovi):

```python
from crypto_decision_lab.independent_validation import (
    ValidationPaths, ValidationRegistry, load_protocol,
    evaluate_independent, canonical_hash, build_readiness,
)

paths = ValidationPaths()
bundle = load_protocol(paths)
protocol = bundle["config"]
registry = ValidationRegistry(paths.registry_root)

report = evaluate_independent(
    future_windows,            # finestre indipendenti validate
    history_windows,           # 500 finestre di ricerca (solo training)
    market_1h,                 # OHLCV orario che copre entry/exit + latenza
    protocol,
    dataset_id="btcusdt-1h-extension-v1",
    registry=registry,
    expected_protocol_sha256=canonical_hash(protocol),
    final=True,
)
print(report["verdict"]["verdict"], report["metrics"])
```

Nota: `history_windows` serve **solo** come contesto di training per la selezione train-only; l'evidenza è interamente nei blocchi di test futuri. `evaluate_independent` è coperto da test su dati sintetici, così il protocollo è eseguibile e verificabile *prima* dei dati reali.

---

## 14. Verifica di readiness eseguita in CP08-04

Comando: `python -m crypto_decision_lab.independent_validation` (read-only, additivo).

| ID | Controllo | Esito |
|---|---|---|
| CP08-04.1 | Protocollo caricato, schema valido, stato FROZEN | **PASS** |
| CP08-04.2 | Sidecar SHA-256 di protocollo e specifica engine | **PASS** |
| CP08-04.3 | Hash di tutti gli artefatti congelati | **PASS** (17/17) |
| CP08-04.4 | Benchmark congelato riconciliato con CP08-03 | **PASS** (delta max < 1e−9) |
| CP08-04.5 | Registro dati di ricerca coerente con gli artefatti | **PASS** |
| CP08-04.6 | Separazione ricerca / coda osservata / validazione | **PASS** |
| CP08-04.7 | Griglia identica a CP08-03, chiusa, train-only | **PASS** |
| CP08-04.8 | Scenario primario completo e allineato a CP08-03 | **PASS** |
| CP08-04.9 | Walk-forward e requisiti minimi di campione | **PASS** |
| CP08-04.10 | Criteri GO/NO-GO completi e non contraddittori | **PASS** |
| CP08-04.11 | Confirmation Engine `SPECIFICATION_ONLY`, 0 regole attive | **PASS** |
| CP08-04.12 | Controlli anti-riuso attivi e registro pulito | **PASS** (0 valutazioni) |
| CP08-04.13 | Nessun dato indipendente: verdetto NOT_EVALUATED | **WARN** (atteso) |
| CP08-04.14 | Nessun training/inferenza/download/modifica archivi | **PASS** |
| CP08-04.15 | Determinismo dell'hash canonico | **PASS** |

**Gate tecnico: PASS** (14 PASS, 1 WARN, 0 FAIL).  
**Gate strategico: NO-GO.**  
**Verdetto di validazione: NOT_EVALUATED.**

---

## 15. Artefatti

Archivio additivo: `data/experiments/S02-CP08-independent-validation-protocol-v1/`

| File | Contenuto | SHA-256 |
|---|---|---|
| `protocol.json` | configurazione completa + metadati e hash | `546c20bfbf753d13f3a59b48579f406a31a24dc7ddbd4214d30f287d040b8a91` |
| `criteria.json` | criteri GO/NO-GO, requisiti di campione, regola di decisione | `6f87b9ed51c0327b31e142de7926cf30450cf7a404d22573ae48bf493c0ba3f9` |
| `data_registry.json` | periodi, ruoli, confronto dichiarato/osservato, separazione | `634145e6f018773ffc1992753ec0146a53c892486818384e4a4ed3c955e91462` |
| `frozen_benchmark.json` | riconciliazione numerica con CP08-03 | `3c7904c670bdc59d54cf63d6228a8e3342c58e1cc1ad7aa2a88475bd925dfca6` |
| `confirmation_engine.json` | specifica engine e hash | `a83c0c3ce730adfc08bb5841ee1fe7bce3697f897f80c084dd0cd21d19726297` |
| `anti_reuse.json` | controlli attivi + stato registro | `4dd962209a77037da40058c5f7c99f5a4509fe8f47cc9fc06dbfafd78549fe57` |
| `checks.json` | 15 controlli con evidenza | `db335b834736568240d9f356da2c8d6fd132f9377d86992b6768f87a944fc0fa` |
| `summary.json` | sintesi per dashboard e gate | `330b9c07af4718113850bde5af3e8b8c1a4472244e33838d8f30f77b39c26dce` |
| `momentum-independent-validation-v1.yaml` | copia byte-identica del protocollo | `b5a7d248bcfde6b02b879292097ca8f758f66e7b7528dc3d6db45237f246b80e` |
| `momentum-confirmation-engine-v1.yaml` | copia byte-identica della specifica engine | `3953117d87ee059be2f1fb5b8d69390272369761b70f28e957cdea4f491f1e67` |
| `manifest.json` | hash sorgenti, hash output, gate, versioni | `eabe2d11b0441fd2771ec41099006bf74477103f92da24a08f4aa409ec20c87a` |

`manifest.json` riporta `frozen_artifacts_unchanged: true`, `original_artifacts_modified: false`, `independent_data_available: false`, `verdict: NOT_EVALUATED`, `technical_gate: PASS`, `strategic_gate: NO-GO`, `confirmation_engine_started: false`, `intern_decision_0_8b_started: false`.

---

## 16. Dashboard, test e riproducibilità

### 16.1 Dashboard read-only

Nuovo tab **Validation Protocol** (adapter `dashboard/validation_adapter.py`, component `components/validation_protocol.py`, figura `make_validation_timeline_figure` in `charts.py`). Mostra: banner «NESSUN DATO INDIPENDENTE · NON VALUTATO · NO-GO», timeline ricerca/coda/validazione attesa, policy congelata, scenario primario, griglia e walk-forward, riconciliazione del benchmark, criteri GO/NO-GO, registro dati, stato anti-riuso, specifica engine, 15 controlli e hash. Non ricalcola metriche e non consuma dati.

Screenshot: `docs/screenshots/validation_protocol_timeline.png` (rigenerabile con `python scripts/render_screenshots.py`).

### 16.2 Comandi

```bash
# verifica del protocollo (read-only, additiva, rifiuta di sovrascrivere)
python -m crypto_decision_lab.independent_validation

# solo stampa, senza scrittura
python -m crypto_decision_lab.independent_validation --no-write

# test dedicati
pytest tests/test_independent_validation.py tests/dashboard/test_validation_protocol_dashboard.py -q
pytest -q
```

Tempi osservati: ~1–2 s per la verifica; suite completa **220 test** in ~60–120 s (dipende dal refit CP08-03 nei test di regressione). Exit code CLI: `0` se il gate tecnico è PASS, `2` altrimenti.

### 16.3 Test aggiunti (42)

| Famiglia | Copertura |
|---|---|
| Struttura protocollo (7) | schema, FROZEN, griglia identica a CP08-03, rifiuto di drift/overlap/carry non dichiarato, hash canonico deterministico, sidecar |
| Engine (1) | `SPECIFICATION_ONLY`, nessuna regola attiva, input vietati |
| Artefatti e benchmark (4) | 17 hash, riconciliazione CP08-03, registro dati, separazione |
| Guardie anti-riuso (7) | rifiuto periodo di ricerca, coda osservata, schema frame, fold indipendenti/purgati, fold builder == `make_folds` CP07, history minima |
| Selezione (1) | poisoning del test block: soglie invariate |
| Verdetto (5) | GO, NO-GO, INCONCLUSIVE per campione, INCONCLUSIVE per CI, interim mai GO |
| Registro (4) | dataset pinning, blocco seconda valutazione finale, hash swap, interim→final |
| Harness end-to-end (3) | valutazione sintetica, hash protocollo obbligatorio, rifiuto dati di ricerca |
| Readiness/archivio/CLI (4) | gate e stato, determinismo, scrittura additiva/no-overwrite, CLI exit 0 |
| Dashboard (6) | adapter, file mancanti, gate rotto, timeline, frame vuoto, render del tab reale |

---

## 17. Gate CP08-04 e cosa serve per cambiare il verdetto

| Gate | Esito |
|---|---|
| Benchmark e configurazioni CP07/CP08 congelati con hash | **PASS** (17/17) |
| Dati storici usati per ricerca dichiarati formalmente | **PASS** |
| Policy di riferimento immutabile | **PASS** |
| Scenario primario realistico (1h, costi espliciti, carry SHORT ipotetico) | **PASS** |
| Griglia soglie e regola train-only pre-registrate e chiuse | **PASS** |
| Protocollo walk-forward su dati futuri | **PASS** |
| Criteri GO/NO-GO per rendimento, drawdown, stabilità, incertezza | **PASS** |
| Separazione validazione vs ricerca esplorativa | **PASS** |
| Configurazione versionata Momentum Confirmation Engine | **PASS** (`SPECIFICATION_ONLY`, 0 regole) |
| Controlli automatici anti-riuso | **PASS** (registro pulito) |
| Nessuna modifica agli archivi storici | **PASS** |
| Nessun training/inferenza/download/trading | **PASS** |
| Indipendenza dimostrata con dati nuovi | **NON ANCORA POSSIBILE** (nessun dato) |

**Gate tecnico: PASS.** Il protocollo è completo, verificabile e riproducibile.  
**Gate strategico: NO-GO.** Invariato fino a nuova evidenza indipendente.

Per cambiare il verdetto serve **tutto** questo:

1. dati BTCUSDT 1h realmente nuovi, successivi al `2026-10-23T16:00:00Z`, non presenti nello snapshot e non derivati dal periodo di ricerca;
2. almeno 300 finestre indipendenti (5 fold × 60) e ≥ 100 trade;
3. una **sola** valutazione finale sul protocollo v1.0.0 (o su una sua nuova versione pre-registrata prima della lettura);
4. tutti i criteri GO soddisfatti, nessun criterio NO-GO;
5. archiviazione del report di valutazione con manifest e hash.

Nessuna di queste condizioni è soddisfatta oggi. La validazione indipendente **non è completata** e non può essere dichiarata tale sulla base di CP08-01/02/03: quei dati sono il periodo di ricerca.

---

## 18. Stato finale

- **Protocollo:** congelato, versionato, verificato (15 controlli, 14 PASS + 1 WARN, 0 FAIL).
- **Validazione indipendente:** **NON INIZIATA** — `NOT_EVALUATED` — nessun dato nuovo.
- **Gate strategico:** **NO-GO**.
- **Momentum Confirmation Engine:** configurazione `SPECIFICATION_ONLY`, nessuna regola attiva, non eseguibile.
- **Intern-Decision-0.8B:** **non avviato**.
- **Trading reale:** **nessuno**.

Il prossimo passo legittimo non è un altro esperimento sul passato: è attendere (o acquisire con un processo separato e tracciato) dati realmente indipendenti, registrarli, e consumare **una sola** valutazione finale secondo questo protocollo.
