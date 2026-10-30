# S02-CP08 · Momentum Robustness & Independent Validation

**Repository:** `crypto-decision-lab`  
**Scope CP08-01:** integrity audit read-only del simulatore CP07 e robustezza del Momentum Threshold.  
**Scope CP08-02** (Parte II): stabilità e concentrazione del profitto sui soli artefatti CP07 + CP08-01, senza training, inferenza o download.  
**Scope CP08-03** (Parte III): robustezza di parametri, costi ed esecuzione su griglia di soglie pre-registrata, selezione solo training, superficie OOS esplorativa, due esperimenti sui costi, latenza 0/1/2h (1h principale) e carry SHORT come ipotesi.  
**Data audit:** 2026-10-29  
**Stato:** **CP08-01 COMPLETE** · 42 controlli automatici: 33 PASS, 8 WARN, 1 FIXED, 0 FAIL. **CP08-02 COMPLETE** · 67 controlli di riconciliazione: 67 PASS, 0 FAIL. **CP08-03 COMPLETE** · 13 controlli: 12 PASS, 1 WARN, 0 FAIL. Suite: **220 test PASS** (178 al termine di CP08-03; CP08-04 aggiunge 42 test).  
**Verdetto integrità:** **PASS WITH WARNINGS** — nessun look-ahead, nessuna sovrapposizione, rendimenti e metriche archiviate riproducibili bit-per-bit.  
**Verdetto strategico:** **INCONCLUSIVE confermato** — il Momentum Threshold è riproducibile ma fragile: dipende da poche operazioni, da una soglia che non è un ottimo stabile e da esecuzione istantanea sull'open. CP08-03 non produce un GO: nel suo scenario conservativo principale (latenza 1h) il rendimento scende a +23,59% con DD mark-to-market −30,76%, e l'envelope 1h+costi+carry ha P(>0)=51,8%. Nessun training, nessuna integrazione exchange, nessuna modifica agli artefatti CP07/CP08.  
**Indice:** §1–11 = CP08-01 (integrity audit); §12–24 = CP08-02 (stability & profit concentration); §25–38 = CP08-03 (parameter & execution robustness).  
**CP08-04:** protocollo di validazione indipendente in [`S02-CP08-independent-validation-protocol.md`](S02-CP08-independent-validation-protocol.md) — congela benchmark, dati di ricerca, scenario primario, griglia, walk-forward, criteri GO/NO-GO e controlli anti-riuso. Stato: `NOT_EVALUATED`, gate strategico `NO-GO`.

---

## 1. Executive summary

L'audit ricostruisce **tutte** le 1.800 decisioni archiviate (300 finestre OOS × 6 policy) dai dataset congelati, le ri-esegue con il simulatore e confronta ogni metrica con CP07.

Risultati principali:

| Verifica | Esito |
|---|---|
| Rendimenti netti e prezzi di esecuzione ricostruiti dagli open grezzi | **identici** (errore massimo 0,0) |
| Metriche per fold CP07 (30 righe policy/fold) | **riprodotte con errore 0,0** |
| Rendimento aggregato CP07 (6 policy) | **riprodotto** (max dev. 5,7e−14 pct) |
| Griglia fee × slippage CP07 (9 scenari) | **riprodotta** (dev. 0,0) |
| Look-ahead su feature, soglie e label retrospettive | **assente** |
| Sovrapposizione posizioni / skip | **0 sovrapposizioni, 0 skip** |
| Equity composta | **identity esatta** `E_i = E_{i−1}·(1+net_i)` |
| Curva equity calendar-time | **29.545 punti orari**, DD invariante |
| Profit factor CP07 | **convenzione percentuale**, non monetaria: PF 1,411 vs 1,3295 |
| Drawdown CP07 | **trade-level**: −21,18% vs −27,20% mark-to-market orario |

Il risultato CP07 del momentum (+39,18%, 106 trade, 1 fold negativo) è quindi **aritmeticamente corretto e riproducibile**, ma:

- il **profit factor reale** (P&L monetario, equity composta) è **1,3295**, non 1,4111;
- il **drawdown reale** misurato sul mark-to-market orario è **−27,20%**, non −21,18%;
- il rendimento dipende in modo critico da **3 operazioni** (senza le prime 3: +8,70%; senza le prime 5: −5,79%);
- la soglia 5% è **al bordo della griglia** in 5/5 fold; su una griglia train-only estesa, 4/5 fold avrebbero preferito 8–10%;
- l'esecuzione all'open del cutoff vale **15,6 punti percentuali** del rendimento (39,18% → 23,59% con 1h di ritardo).

Nessuno di questi fatti è un errore di calcolo; sono **limiti di robustezza**. Il gate di promozione resta **NO-GO per produzione / INCONCLUSIVE per ricerca**.

---

## 2. Metodo

### 2.1 Principi

1. **Read-only.** Nessun artefatto CP07/CP06B/CP06A viene riscritto. Il manifest dell'audit registra gli hash sorgente *prima* e *dopo*; se cambiano, l'audit fallisce e cancella l'output.
2. **Nessun refit sui dati di test.** L'audit non seleziona soglie, non sceglie policy, non riottimizza costi. Le soglie sono solo **riprodotte** dal training di ogni fold.
3. **Ricostruzione indipendente.** Rendimenti, prezzi di esecuzione e curva equity sono ricalcolati dagli open grezzi del dataset di label e dallo snapshot di mercato, non ricopiati dall'archivio.
4. **Convenzioni esplicite.** Dove esistono due definizioni legittime (PF percentuale vs monetario, DD trade-level vs mark-to-market) l'audit riporta **entrambe** e quantifica il divario; non ne sceglie una in modo silenzioso.

### 2.2 Oggetti esaminati

| Modulo | Ruolo nell'audit |
|---|---|
| `decision_contract.py` | schema, semantica temporale, formule net/gross, validazione |
| `trading_simulator.py` | compounding, skip, ledger, metriche, curva equity |
| `decision_labeling.py` | prezzi di entry/exit e label retrospettive |
| `trading_policy.py` | soglie walk-forward, aggregazione, sensitivity, leakage check |
| `policy_evaluation.py` | fold cronologici e purge 24h |

### 2.3 Dataset dell'audit

| Oggetto | Righe | Note |
|---|---:|---|
| `labels.parquet` (CP07) | 500 finestre | 2021-01 → 2026-10 |
| `decisions.parquet` (CP07) | 1.800 | 300 finestre OOS × 6 policy, 5 fold |
| `features.parquet` (CP06B) | 500 | feature cutoff-only |
| snapshot BTCUSDT 1h | 50.906 barre | 14 ore mancanti, 5 chiusure irregolari, 6 chunk `REQUIRES_REVIEW` |

Le 300 finestre OOS iniziano il **2023-06-10**: nessuna di esse ricade in un chunk messo in quarantena (verifica `CP08-01.3.f`).

---

## 3. Audit CP08-01 — esito dei 42 controlli

| Item | Controlli | Esito |
|---|---:|---|
| CP08-01.1 · Assenza di look-ahead | 5 | PASS |
| CP08-01.2 · Entry/exit sui timestamp corretti | 4 | PASS + 1 WARN (schema archivio) |
| CP08-01.3 · Disponibilità del prezzo open | 7 | PASS + 1 WARN (latenza) |
| CP08-01.4 · Sovrapposizione posizioni | 3 | PASS |
| CP08-01.5 · Segnali saltati in ordine cronologico | 2 | PASS + 1 FIXED |
| CP08-01.6 · Compounding del capitale | 3 | PASS |
| CP08-01.7 · Fee e slippage su entrata e uscita | 6 | PASS + 1 WARN (confondimento costi/soglia) |
| CP08-01.8 · SHORT: esposizione e collaterale | 3 | PASS + 1 WARN (funding) |
| CP08-01.9 · Equity curve calendar-time | 4 | PASS + 1 WARN (mark-to-market) |
| CP08-01.10 · Max drawdown e profit factor | 5 | PASS + 3 WARN (convenzioni metriche) |

**Totale: 33 PASS · 8 WARN · 1 FIXED · 0 FAIL.**

### 3.1 No look-ahead (CP08-01.1)

| Controllo | Evidenza |
|---|---|
| `momentum_168h` usa solo chiusure **strettamente precedenti** al cutoff | ricalcolato da zero su 500 finestre: max \|Δ\| = **0,0** |
| `decision_timestamp ≤ entry_timestamp`, entry bar successiva all'anchor bar | 1.800/1.800 righe OK |
| Soglie walk-forward riproducibili **solo** dal training | 10/10 refit identici a CP07 (5 fold × 2 policy), 0 mismatch |
| Perturbazione delle label retrospettive (`best_action`, `long_net_pct`, …) | **0 decisioni cambiate** sulle policy reali |
| Controlli di leakage CP07 ereditati | 5/5 PASS |

La feature è ricostruita esattamente dalla formula documentata (`close[-1]/close[-168]−1` su barre `< cutoff`), quindi non esiste una via per cui il target a 24h entri nel segnale.

### 3.2 Entry/exit (CP08-01.2)

- `entry_timestamp == cutoff` per 1.800/1.800 righe;
- `exit_timestamp − entry_timestamp == 24h` (unico valore osservato: 24,000);
- `decision_timestamp == cutoff_timestamp` per 1.800/1.800;
- fold cronologici, blocchi di test disgiunti;
- **WARN**: `decisions.parquet` non è consumabile dal contratto senza rinominare colonne — mancano `decision_timestamp`, `horizon_hours`, `policy_version`. L'audit li **ricostruisce esplicitamente** (`policy → policy_version`, `cutoff → decision_timestamp`, `horizon_hours = 24`) e valida il risultato con `validate_decisions`. Nessuna inferenza nascosta.

### 3.3 Disponibilità del prezzo open (CP08-01.3)

| Controllo | Evidenza |
|---|---|
| Open di entry, open di exit, close di anchor presenti **senza fill** | 300/300, 300/300, 300/300 finestre OOS |
| Barriere di finestra dentro un buco dati | 14 ore mancanti nello snapshot, **0** barriere nei buchi |
| `reference_price == close(anchor bar)` | deviazione relativa max **5,575e−08** (float32) |
| Prezzi di esecuzione = open direzionale ± slippage | errore relativo max **0,0** |
| Barriere su candele a chiusura irregolare | 5 eventi nello snapshot, **0** collisioni |
| Chunk in quarantena nel periodo OOS | 6 chunk, **0/300** finestre coinvolte |
| **Latenza di esecuzione** | **WARN**: 39,18% (0h) → 23,59% (1h) → 23,90% (2h) |

L'ultimo punto è sostanziale: la strategia assume di comprare/vendere **esattamente all'open della barra del cutoff**, cioè nello stesso istante in cui si chiude la barra che ha generato il segnale. Con un ritardo di un'ora il rendimento scende a 23,59% (−15,6 pct). Il segno resta positivo, ma un terzo del vantaggio dichiarato vive nel primo open.

### 3.4 Nessuna sovrapposizione (CP08-01.4)

- 1.121 trade eseguiti su 6 policy: **0 sovrapposizioni** (`entry_i ≥ exit_{i−1}`);
- skip archiviati: **0** per ogni policy; la spaziatura minima tra cutoff consecutivi è **24h** = holding period, quindi la condizione non può verificarsi su questo dataset;
- sizing implicito: notional/equity ∈ **[0,9985, 1,0005]** → nessuna leva. LONG resta sotto l'equity per fee+slippage; SHORT lo supera solo dello slippage di entry (0,05%).

### 3.5 Segnali saltati (CP08-01.5)

| Controllo | Evidenza |
|---|---|
| Segnale sovrapposto → saltato, net 0, equity invariata, mai riprezzato | replay sintetico: 1 trade, 1 skip, equity costante |
| **FIXED**: `NO_TRADE` non deve occupare il mercato | replay sintetico: `NO_TRADE` + segnale a +12h → 1 trade, **0 skip** |

Il difetto trovato in CP07: `simulate_sequence` aggiornava `last_exit` **anche per le righe NO_TRADE**, così una finestra piatta avrebbe potuto bloccare un segnale successivo (e sarebbe stata contata come "skipped"). Sul dataset CP07 l'impatto è **esattamente zero** (0 skip in tutte le policy, spaziatura ≥ 24h), ma su dati futuri più densi sarebbe stato un bug silenzioso. Corretto in `trading_simulator.py` con regression test dedicato.

### 3.6 Compounding (CP08-01.6)

- `E_i = E_{i−1}·(1 + net_i/100)` verificato su ogni riga eseguita: errore massimo **0,0**;
- equity finale = prodotto di tutti i rendimenti eseguiti: deviazione max **5,46e−12**;
- concatenamento dei 5 fold = rendimento aggregato: deviazione max **4,55e−12** pct;
- nessuna equity ≤ 0 (minimo: 3.238 USDT per Always SHORT).

### 3.7 Fee e slippage (CP08-01.7)

- rendimenti ricostruiti dagli open grezzi: errore max **0,0** (net e gross);
- round trip a prezzo piatto: **−0,2996%** (= fee 0,10% + slippage 0,05% per lato, composti);
- monotonia: aumentare i costi non migliora mai un round trip (griglia 0% → 0,50% per lato);
- righe NO_TRADE senza prezzo e senza costo: **0 violazioni**;
- griglia fee × slippage CP07 ri-eseguita: deviazione max **0,0** su 9 scenari × 6 policy;
- **WARN — confondimento**: la griglia ri-seleziona la soglia sul training a ogni scenario di costo. Poiché il costo round trip dipende solo da `fee + slippage`, un effetto-costo puro sarebbe monotono; i valori archiviati non lo sono (0,10% → +11,40%, 0,15% → +39,18%, 0,20% → +25,18%). La “sensitivity ai costi” di CP07 misura quindi **costi + ri-selezione della soglia**, non i soli costi.

### 3.8 SHORT (CP08-01.8)

- 543 SHORT su tutte le policy (49 nel momentum): rendimento peggiore **−11,13%**, quindi perdita limitata e nessuna equity negativa;
- con costi zero LONG(+20%) e SHORT(−16,67%) sono speculari: `(1+r_L)(1+r_S) = 1`, nessun moltiplicatore di leva nascosto;
- collaterale: lo SHORT è **fully collateralized** (i proventi della vendita sono il collaterale, nessun margine aggiuntivo);
- **WARN**: non sono modellati **funding rate, costo di prestito, liquidazione**. Con un funding perp di 0,01%/8h su 24h il drag sarebbe ≈ 0,03% per trade, ≈ −1,4% cumulato sui 49 SHORT del momentum. Il dato disponibile è spot: uno short spot richiede margin borrow nella realtà.

### 3.9 Equity curve calendar-time (CP08-01.9)

- curva oraria da 2023-06-10 a 2026-10-23: **29.545 punti**, nessun passo irregolare;
- equity della curva ai timestamp di exit = equity del ledger: deviazione max **0,0**;
- drawdown della curva calendar-time **identico** a quello event-time (−21,1804%);
- **WARN**: la curva trade-level nasconde l'excursion intra-hold. Mark-to-market orario (valore di liquidazione con fee/slippage di uscita):

| Policy | DD exit-marked | DD mark-to-market | Δ |
|---|---:|---:|---:|
| Momentum | −21,18% | **−27,20%** | −6,02 |
| Always LONG | −58,30% | −58,69% | −0,39 |
| Always SHORT | −69,82% | −70,36% | −0,54 |
| Timestral | −59,52% | −60,34% | −0,81 |
| Oracle | 0,00% | **−9,62%** | −9,62 |

L'oracle è l'esempio limite: nessun trade in perdita a chiusura, quindi DD trade-level nullo, ma il percorso intra-hold arriva a −9,62%. Per il momentum il rischio reale è **circa un quarto più alto** di quanto riportato in CP07.

### 3.10 Max drawdown e profit factor (CP08-01.10)

- metriche per fold: **30/30 righe riprodotte con errore 0,0** (trades, return, DD, win rate, exposure, equity finale);
- rendimento aggregato: riprodotto (max dev. 5,68e−14 pct);
- **WARN — profit factor**: CP07 usa `Σ guadagni% / Σ perdite%` (convenzione event-study). Con equity composta al 100% il PF di portafoglio è monetario:

| Policy | PF percentuale (CP07) | PF monetario | Δ |
|---|---:|---:|---:|
| Momentum | **1,4111** | **1,3295** | −5,8% |
| Always LONG | 0,8028 | 0,8012 | −0,2% |
| Always SHORT | 0,6730 | 0,6132 | −8,9% |
| Timestral | 0,5664 | 0,5795 | +2,3% |

- **WARN — win rate / media**: l'aggregato CP07 è per trade, i fold e i breakdown per anno sono per finestra. Per il momentum: win rate **43,40% per trade** vs **15,33% per finestra**; media **+0,3556% per trade** vs **+0,1256% per finestra**. La tabella CP07 etichetta la media per finestra come "avg net/trade".
- **WARN — drawdown**: la convenzione è trade-close; per l'oracle produce 0,00% pur con DD mark-to-market −9,62%.

Nessuna di queste tre voci cambia il verdetto: sono **definizioni**, non errori di calcolo. Ma i numeri citati in CP07 vanno letti con la convenzione corretta.

---

## 4. Robustezza del Momentum Threshold

Tutte le diagnostiche seguenti usano la policy **congelata** (soglia 5%, costi 0,10%/0,05%, entry/exit sull'open). Nessuna riottimizzazione, nessun dato di test usato per scegliere parametri.

### 4.1 Riproducibilità

| Metrica | Valore |
|---|---:|
| Trade | 106 (57 LONG, 49 SHORT) |
| Rendimento composto | **+39,18%** (13.917,58 USDT) |
| Profit factor monetario | **1,3295** |
| Profit factor percentuale | 1,4111 |
| Max drawdown trade-level | −21,18% |
| Max drawdown mark-to-market | **−27,20%** |
| Win rate per trade | 43,40% (46W / 60L) |
| Media per trade | +0,3556% |
| Media per finestra | +0,1256% |
| Migliore / peggiore | +9,13% / −11,13% |
| Profitto lordo / perdita lorda | 15.805 / 11.888 USDT |
| Fold negativi | 1 (fold 3, −13,72%) |
| Anni negativi | 1 (2025, −3,13%) |

### 4.2 Bootstrap (moving block, blocco 10, 2.000 draw, seed 42)

| Statistica | Punto | CI 95% | Probabilità |
|---|---:|---:|---:|
| Rendimento composto | +39,18% | **[−24,01%, +148,19%]** | P(>0) = **84,4%** |
| Profit factor (pct) | 1,411 | **[0,798, 2,296]** | P(>1) = **87,7%** |
| Media net per finestra | +0,1256% | [−0,0793%, +0,3272%] | — |

L'intervallo del rendimento composto include lo zero: la strategia **non è distinguibile da NO_TRADE** con questo campione. È lo stesso risultato del paired test CP07, qui espresso sulla metrica che conta (equity finale).

### 4.3 Concentrazione

| Esclusione | Rendimento composto |
|---|---:|
| Nessuna | +39,18% |
| Senza la migliore operazione | +27,53% |
| Senza le 2 migliori | +17,18% |
| Senza le 3 migliori | **+8,70%** |
| Senza le 5 migliori | **−5,79%** |
| Senza le 10 migliori | **−27,61%** |

Il risultato è un **right-tail trade**: poche operazioni di trend estremo sostengono l'intero profitto. Su 106 trade questo è compatibile con la natura momentum, ma significa che il campione effettivo di "eventi che pagano" è dell'ordine di 3–5, non di 106.

### 4.4 Selezione della soglia (solo training)

La griglia CP07 si ferma a **5%**, ed è proprio 5% la soglia scelta in 5/5 fold: selezione **al bordo**.

Ricalcolo train-only su griglia estesa (0% … 20%):

| Fold | Soglia CP07 | Multiplo training @5% | Migliore su griglia estesa | Multiplo |
|---:|---:|---:|---:|---:|
| 0 | 5% | 1,3157 | **10%** | 1,5430 |
| 1 | 5% | 1,3500 | **10%** | 1,4902 |
| 2 | 5% | 1,6871 | **8%** | 1,7307 |
| 3 | 5% | 1,8157 | 5% | 1,8157 |
| 4 | 5% | 1,5665 | **10%** | 1,6300 |

In **4/5 fold** il training avrebbe preferito una soglia fuori griglia. Il profilo dei multipli è inoltre non monotono (a 0,5% e 1% il training perde), segno che la selezione della soglia è **rumorosa**: la superficie è piatta/irregolare e il massimo è instabile. Questo non autorizza a ri-selezionare la soglia sui dati OOS (vietato dal protocollo); autorizza solo a dichiarare che **5% non è un ottimo stabile**, ma il punto scelto da un vincolo di griglia.

### 4.5 Decomposizione LONG/SHORT

| Direzione | Trade | Rendimento composto (isolato) |
|---|---:|---:|
| LONG | 57 | +27,88% |
| SHORT | 49 | +8,83% |

Il contributo principale è LONG, coerente con il bias rialzista di BTC nel periodo. Lo SHORT aggiunge poco e paga costi di funding non modellati (vedi §3.8).

### 4.6 Costi e latenza

| Scenario | Rendimento |
|---|---:|
| Base: fee 0,10% + slippage 0,05% (0,15% per lato) | +39,18% |
| Costo totale 0% per lato (limite teorico) | +119,10% |
| Costo totale 0,10% per lato | +11,40% |
| Costo totale 0,20% per lato | +25,18% |
| Ritardo di esecuzione 1h | **+23,59%** |
| Ritardo di esecuzione 2h | +23,90% |

La strategia resta positiva in tutti gli scenari CP07, ma il margine si assottiglia rapidamente: metà del rendimento base è dovuto all'assenza di costi (119,10% → 39,18%) e un terzo all'esecuzione istantanea. La non monotonia della curva di costo (0,10% → +11,40% ma 0,15% → +39,18%) **non** è un paradosso: è l'effetto della soglia ri-ottimizzata sul training per ogni scenario, quindi non va letta come “costa meno, rende meno”.

---

## 5. Findings e remediation

| ID | Severità | Finding | Impatto su CP07 | Stato |
|---|---|---|---|---|
| F1 | Media | PF calcolato su percentuali, non su P&L monetario | PF 1,4111 vs 1,3295 (−5,8%) | Documentato; nessuna modifica retroattiva |
| F2 | Media | DD trade-level vs mark-to-market orario | −21,18% vs −27,20% | Documentato |
| F3 | Media | Soglia 5% al bordo della griglia; 4/5 fold preferiscono 8–10% su training esteso | Policy congelata, nessuna ri-selezione | Documentato; candidato per CP08-02 pre-registrato |
| F4 | Media | Concentrazione: senza 5 trade il rendimento è negativo | +39,18% → −5,79% | Documentato |
| F5 | Bassa | Win rate/media: aggregato per trade, fold/anno per finestra; doc CP07 etichetta la media per finestra come "per trade" | Nessuno sul rendimento | Documentato |
| F6 | Bassa | Esecuzione assunta all'open istantaneo del cutoff | +39,18% → +23,59% con 1h | Documentato; stress test disponibile |
| F7 | Bassa | Funding/borrow e liquidazione SHORT non modellati | ≈ −1,4% cumulato stimato sui 49 SHORT | Documentato |
| F8 | Bassa | `NO_TRADE` occupava il mercato e poteva bloccare un segnale | **0** su CP07 | **Corretto** + test di regressione |
| F9 | Bassa | `decisions.parquet` non è consumabile dal contratto senza mapping | Nessuno (audit mappa esplicitamente) | Documentato |
| F10 | Media | La griglia costi CP07 ri-fit la soglia per scenario: la sensitivity non isola l'effetto costo | Tabella costi CP07 non monotona | Documentato; non ri-eseguito |

### 5.1 Modifiche applicate al codice

Solo due, entrambe a impatto **nullo** sui numeri CP07 (verificato ri-eseguendo l'archivio):

1. `trading_simulator.simulate_sequence`: `last_exit` viene aggiornato **solo** dai trade; una decisione `NO_TRADE` non blocca più i segnali successivi e non può essere contata come "skipped".
2. `trading_simulator.equity_curve`: il primo punto della curva è l'equity iniziale **al primo entry**, non un'ora prima del primo exit.

Nessuna formula di rendimento, costo, metrica o soglia è stata modificata: le metriche CP07 restano riproducibili bit-per-bit.

---

## 6. Cosa cambia (e cosa non cambia) rispetto a CP07

**Non cambia**

- rendimento +39,18%, 106 trade, 1 fold negativo, 0 skip, 0 sovrapposizioni;
- confronto paired vs NO_TRADE non significativo (CI media net include 0 in CP07 e nel bootstrap CP08);
- verdetto: **INCONCLUSIVE**;
- nessun training di Intern-Decision-0.8B.

**Cambia la lettura dei numeri**

| Voce | CP07 | CP08 (convenzione corretta) |
|---|---|---|
| Profit factor momentum | 1,411 | **1,3295** monetario |
| Max drawdown momentum | −21,18% | **−27,20%** mark-to-market |
| Win rate momentum | 43,40% (aggregato) | 43,40% per trade / 15,33% per finestra |
| "Avg net/trade" | +0,1256% | +0,3556% per trade (0,1256% è per finestra) |
| Significatività del rendimento | CI media net [−0,083%, +0,332%] | CI rendimento composto [−24,0%, +148,2%], P(>0)=84% |

---

## 7. Artefatti

Output additivo, nessuna sovrascrittura: `data/experiments/S02-CP08-simulator-integrity-audit-v1/`

| File | Contenuto | SHA-256 |
|---|---|---|
| `summary.json` | verdetto, finding, metriche, robustezza | `278fb37b3c63a512fb8d8835d6f85e9154b03e957b2859ab461b404b2c8eab75` |
| `checks.json` | 42 controlli con evidenza | `541066f75e58ee8d3c76ddc14af2376e1339b6154ae590a8bcd1037b2201812b` |
| `metrics_comparison.csv` | metriche per policy, PF monetario e percentuale, DD, win rate | `68273750aed882cc5bf43e2711c4a90fbcb86683b734eb4e9707869168d3a81f` |
| `equity_curves.parquet` | curve orarie step e mark-to-market per 6 policy | `3de4a500ac7ea46306619c2f1a08963075a9b9756a4c53b365c321488efd8981` |
| `threshold_robustness.json` | griglia train-only estesa per fold | `d658bc5593fa4051e0075d3333b4b1f9823003580f49137589c53f05a4f7921e` |
| `manifest.json` | hash sorgente prima/dopo, hash output | — |

`manifest.json` riporta `source_hashes_unchanged: true` e `original_artifacts_modified: false`.

---

## 8. Riproducibilità

```bash
# audit completo (read-only, rifiuta di sovrascrivere l'output)
python -m crypto_decision_lab.simulator_integrity_audit

# solo stampa, senza scrittura
python -m crypto_decision_lab.simulator_integrity_audit --no-write

# test di audit + simulatore
pytest tests/test_simulator_integrity_audit.py tests/test_trading_simulator.py -q
pytest -q
```

Tempi osservati: ~25–45 s per l'audit (refit train-only della robustezza soglie e ri-esecuzione della griglia costi inclusi); suite completa 124 test in ~30 s. **(CP08-02 aggiunge 18 test: suite attuale 142.)**

---

## 9. Test automatici aggiunti

| Test | Copertura |
|---|---|
| `test_contract_frame_reconstructs_missing_contract_columns` | mapping archivio → contratto, senza inferenze |
| `test_reconstruct_returns_matches_archived_decisions` | rendimenti e prezzi ricostruiti dagli open |
| `test_dollar_profit_factor_differs_from_percent_sums_under_compounding` | PF monetario ≠ PF percentuale |
| `test_no_trade_never_blocks_a_later_signal` | regressione F8 |
| `test_calendar_equity_curves_are_hourly_and_match_ledger` | curva oraria, equity ai timestamp di exit |
| `test_mark_to_market_drawdown_exceeds_exit_marked_drawdown` | DD intra-hold |
| `test_execution_delay_stress_penalises_late_entry` | stress di latenza |
| `test_concentration_profile_removes_best_trades` | leave-top-k |
| `test_bootstrap_profile_is_deterministic_and_reports_uncertainty` | bootstrap riproducibile |
| `test_threshold_robustness_reports_grid_boundary` | soglia al bordo, refit == archivio |
| `test_cost_sensitivity_confound_detection` | non monotonia della griglia costi = ri-selezione soglia |
| `test_max_drawdown_includes_initial_capital` | DD dal capitale iniziale |

Totale suite: **124 PASS** (112 CP07 + 12 CP08).

---

## 10. Gate CP08-01

| Gate | Esito |
|---|---|
| Nessun look-ahead | **PASS** (5/5) |
| Entry/exit e orizzonte corretti | **PASS** |
| Prezzi open disponibili e usabili | **PASS** |
| Nessuna sovrapposizione di posizioni | **PASS** |
| Segnali saltati gestiti cronologicamente | **PASS** (+1 difetto latente corretto) |
| Compounding self-financing | **PASS** |
| Fee e slippage su entrambi i lati | **PASS** |
| SHORT con esposizione e collaterale espliciti | **PASS** (funding non modellato: WARN) |
| Equity curve calendar-time | **PASS** (DD mark-to-market più profondo: WARN) |
| DD e PF con convenzione esplicita | **PASS** (3 WARN di definizione) |
| Artefatti CP07 intatti | **PASS** (hash invariati) |
| Riesecuzione indipendente delle metriche | **PASS** (errore 0,0) |
| Nessun training / nessun ordine reale | **PASS** |

**Promozione della strategia: NO-GO.** Il Momentum Threshold non soddisfa il criterio di robustezza (dipendenza da 3–5 trade, soglia al bordo, sensibilità alla latenza, CI del rendimento che include lo zero). Resta un candidato di ricerca, non una strategia di produzione.

---

## 11. Prossimi passi (validazione indipendente, solo requisiti)

> **Nota (aggiunta in CP08-02).** Il CP08-02 implementato nella Parte II è l'analisi di **stabilità e concentrazione** del profitto, non la validazione indipendente descritta qui: i sei requisiti seguenti restano **aperti** e nessuno di essi è stato soddisfatto da CP08-02.

Nessun risultato anticipato. Per una validazione indipendente servono, in ordine di valore:

1. **Pre-registrazione** della griglia di soglie (inclusi 8–10%) e della regola di scelta *prima* di vedere nuovi dati OOS.
2. **Dati fuori campione genuini**: finestre successive al 2026-10-23, oppure un secondo simbolo (BTC spot vs perp) con lo stesso protocollo.
3. **Costi completi**: funding/borrow per gli SHORT, fee tier realistiche, latenza di 1 barra come scenario base (non come stress).
4. **Walk-forward più fine**: più fold, test block più corti, riportando la distribuzione dei rendimenti per fold e non solo l'aggregato.
5. **Metriche duali obbligatorie** in ogni report: PF monetario + percentuale, DD trade-level + mark-to-market, win rate per trade + per finestra.
6. **Effettivo numero di eventi indipendenti**: riportare la concentrazione (leave-top-k) accanto a ogni rendimento cumulato.

Finché questi punti non sono soddisfatti, il Momentum Threshold resta **INCONCLUSIVE** e Intern-Decision-0.8B **non va addestrato**.

---
---

# Parte II · S02-CP08-02 — Momentum Stability & Profit Concentration

**Scope CP08-02:** stabilità descrittiva e concentrazione del profitto del Momentum Threshold, usando **solo** artefatti CP07 e CP08-01 già congelati.  
**Data analisi:** 2026-10-29 · **Stato:** **CP08-02 COMPLETE** · **67 controlli di riconciliazione: 67 PASS, 0 FAIL** (delta massimo 3,64e−12) · **18 nuovi test** (11 core + 7 dashboard).  
**Nessuna** nuova inferenza Timestral, **nessun** training, **nessun** download, **nessuna** modifica agli artefatti originali, **nessuna** ri-selezione delle soglie.  
**Verdetto di questo step:** la strategia è **riproducibile e descrivibile**, ma **fragile**; il gate statistico di promozione **non è stato rieseguito** e resta **INCONCLUSIVE / NO-GO** per il trading.

> **Avvertenza di metodo.** Questa parte è un'analisi **retrospettiva descrittiva** su un singolo campione OOS. Nessun risultato qui dentro — incluso il fatto che la volatilità alta sia negativa o che il fold 1 spieghi il 65% del P&L — è una strategia validata, un filtro selezionabile o una prova di edge. I condizionamenti per regime sono **letture sul campione**, non regole operative: selezionarli *ora* sarebbe ottimizzazione sui dati OOS, vietata dal protocollo.

---

## 12. Metodo e vincoli operativi

| Vincolo | Implementazione verificabile |
|---|---|
| Nessun nuovo training | nessun fit di modello, nessuna soglia ri-stimata; `policy_frozen.threshold = 0.05` in `manifest.json` |
| Nessuna nuova inferenza Timestral | gli unici input sono `labels`/`features`/`decisions` già congelati |
| Nessun download | `load_archive` apre solo file locali presenti nel repository |
| Nessun artefatto modificato | hash di 17 sorgenti prima/dopo (CP07, CP06B, snapshot, archivio CP08-01) |
| Nessuna ottimizzazione su OOS | la soglia archiviata è l'unico valore osservato (`[0.05]`); i tercili di regime usano **solo** le finestre di training del fold |
| P&L monetario per metriche capital-dependent | PF, concentrazione, contributi e drawdown calcolati in USDT sull'equity composta |
| Separazione trade / finestra / periodo | `trade_attribution.parquet` (106 righe), `period_summary.parquet` (365 righe: 300 finestre + 5 fold + 41 mesi + 15 trimestri + 4 anni) |
| Riuso del simulatore auditato | `simulator_integrity_audit.contract_frame / reconstruct_returns / replay_policy / dollar_metrics / calendar_equity_curves / concentration_profile` |

### 12.1 Pipeline

```text
CP07 decisions.parquet (300 righe momentum)
        |  contract_frame + reconstruct_returns   (CP08-01, riusati)
        v
   replay_policy -> ledger (300 finestre, 0 skip)
        |  equity monetaria per finestra (E_before, E_after, pnl_usdt)
        +- join features CP06B (solo barre < cutoff) -> trend/vol buckets train-only
        v
 trade_attribution (106)     period_summary (365)     regime_breakdown (21)
        +- concentration_summary.json  (top-k, HHI/Gini, concentrazione temporale)
        +- drawdown_analysis.json      (exit-marked + mark-to-market, 2 convenzioni)
        +- streak_analysis.json        (run-length + runs test descrittivo)
        +- equity_curves.parquet       (59.090 punti orari step + MTM)
```

### 12.2 Guardie anti-look-ahead introdotte in questo step

| Guardia | Esito |
|---|---|
| `feature_bar == cutoff − 1h` per tutte le finestre usate | **500/500** |
| Bucket di trend/volatilità da tercili **solo training** (per fold) | applicati ai soli test block |
| Regime di trend ricalcolato vs colonna `regime` archiviata da CP07 | **300/300 identici** |
| Fold di appartenenza ricalcolato da `make_folds` vs archivio | **300/300** |
| Valori di soglia presenti nell'archivio | **{0.05}** — nessuna soglia ri-negoziata |
| Somma del P&L monetario per finestra vs `E_final − E_0` | errore **1,8e−12 USDT** |
| Catena di hash con CP08-01 (`trading_decisions`) | **identica** `66c45b67…9cf8` |

---

## 13. Ricostruzione dei 106 trade e riconciliazione con il simulatore (Q1–Q2)

I 106 trade sono ricostruiti **da zero** dagli open grezzi del dataset di label e rieseguiti con il simulatore del CP08-01, non ricopiati dall'archivio.

### 13.1 Riconciliazione — 67 controlli, 0 fallimenti

| Famiglia | Controlli | Falliti | Delta assoluto massimo |
|---|---:|---:|---:|
| CP07 (aggregato, 5 fold × 5 metriche, 4 anni × 3, 3 regimi × 3) | 53 | 0 | **3,64e−12** |
| CP08-01 (PF monetario/percentuale, win rate, equity, DD exit/MTM, leave-top-k 0/1/2/3/5/10) | 14 | 0 | **0,0** |

| Metrica | CP07 / CP08-01 | Ricostruito CP08-02 | Delta |
|---|---:|---:|---:|
| Trade eseguiti | 106 | 106 | 0 |
| NO_TRADE / skip | 194 / 0 | 194 / 0 | 0 |
| Rendimento composto | +39,17576% | +39,17576% | 5,7e−14 |
| Equity finale | 13.917,5764 USDT | 13.917,5764 USDT | 0,0 |
| PF monetario (CP08-01) | 1,3295465 | 1,3295465 | 0,0 |
| PF percentuale (CP07) | 1,4111430 | 1,4111430 | 0,0 |
| Max DD exit-marked | −21,180367% | −21,180367% | 0,0 |
| Max DD mark-to-market (CP08-01) | −27,202367% | −27,202367% | 0,0 |
| Win rate per trade / per finestra | 43,396% / 15,333% | 43,396% / 15,333% | 0,0 |
| Leave-top-3 (percentuale) | +8,69657% | +8,69657% | 0,0 |
| Leave-top-5 (percentuale) | −5,79015% | −5,79015% | 0,0 |

**Additività del P&L monetario** (usando il P&L monetario, la somma per periodo *deve* coincidere con il totale):

| Granularità | Righe | Σ P&L periodo (USDT) | Errore vs totale |
|---|---:|---:|---:|
| Finestra | 300 | 3.917,5764 | 1,4e−12 |
| Fold | 5 | 3.917,5764 | 0,0 |
| Mese | 41 | 3.917,5764 | 4,5e−13 |
| Trimestre | 15 | 3.917,5764 | 9,1e−13 |
| Anno | 4 | 3.917,5764 | 9,1e−13 |

Conclusione Q1–Q2: **i 106 trade sono equi-ricostruibili e il P&L monetario è additivo a ogni granularità**, coerente con il simulatore auditato.

---

## 14. Statistiche dei rendimenti per trade (Q3)

106 trade = 57 LONG + 49 SHORT; 46 vincenti, 60 perdenti (43,40%), 0 piatti.

| Statistica | Net per trade (%) | P&L monetario (USDT) |
|---|---:|---:|
| Media | **+0,3556** | **+36,96** |
| Mediana | **−0,2343** | **−30,48** |
| Dev. std | 2,9705 | 370,26 |
| Min | −11,1346 | −1.458,05 |
| p05 | −2,9118 | −371,84 |
| p25 | −1,3651 | −154,45 |
| p75 | +1,8036 | +200,37 |
| p95 | +6,6230 | +839,64 |
| Max | +9,1304 | +1.044,40 |
| Skew | +0,480 | +0,270 |
| Kurtosi (excess) | +2,568 | +2,597 |
| Trade positivi | 43,40% | 43,40% |

**Il trade mediano perde denaro.** La media positiva nasce dal rapporto di payoff: vincita media **+343,59 USDT** vs perdita media **−198,13 USDT** (payoff 1,73). L'expectancy monetaria (0,434·343,59 − 0,566·198,13 = +36,96 USDT) coincide con la media osservata: la strategia è un sistema a bassa win-rate e coda destra lunga, non un sistema di vincita frequente. Questo è il segnale per-trade della concentrazione del §16.

---

## 15. Analisi per periodo (Q4)

Le metriche per finestra (300 righe) e per periodo (fold / anno / trimestre / mese) sono in `period_summary.parquet`; qui le viste principali. Il **P&L monetario** è additivo; il **return ricomposto** del periodo è il prodotto dei soli trade del periodo e non è additivo.

### 15.1 Per fold

| Fold | Finestre | Trade | P&L USDT | Share P&L | Return ricomposto | PF monetario | Win rate trade |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 60 | 15 | +260,84 | 6,66% | +2,61% | 1,279 | 26,67% |
| 1 | 60 | 35 | **+2.561,84** | **65,39%** | +24,97% | 1,590 | 42,86% |
| 2 | 60 | 23 | +977,12 | 24,94% | +7,62% | 1,419 | 43,48% |
| 3 | 60 | 19 | **−1.893,76** | **−48,34%** | **−13,72%** | 0,493 | 42,11% |
| 4 | 60 | 14 | +2.011,54 | 51,35% | +16,90% | 4,698 | 64,29% |

Un solo fold (il 1) genera il 65% del P&L netto; un solo fold (il 3) ne distrugge il 48%. La distribuzione per fold **non è** stabile: 2 fold su 5 hanno |share| > 48%.

### 15.2 Per anno

| Anno | Finestre | Trade | P&L USDT | Share P&L | Return ricomposto | PF monetario | Win rate trade |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2023 (giu–dic) | 50 | 11 | +174,76 | 4,46% | +1,75% | 1,251 | 27,27% |
| 2024 | 89 | 45 | **+3.308,93** | **84,46%** | +32,52% | 1,654 | 42,22% |
| 2025 | 88 | 30 | −421,88 | −10,77% | −3,13% | 0,882 | 40,00% |
| 2026 (gen–ott) | 73 | 20 | +855,77 | 21,84% | +6,55% | 1,335 | 60,00% |

### 15.3 Trimestri e mesi estremi

| Periodo | Trade | P&L USDT | Share P&L | Return ricomposto | Note |
|---|---:|---:|---:|---:|---|
| 2024Q1 | 13 | **+2.488,19** | **63,51%** | +24,45% | miglior trimestre |
| 2026Q1 | 9 | −1.283,36 | −32,76% | −9,83% | peggior trimestre |
| 2025Q3 | 4 | −687,11 | −17,54% | −5,05% | 0/4 vincenti |
| 2026Q3 | 5 | +1.433,14 | 36,58% | +11,48% | PF 10,69 |
| 2026Q4 (parziale) | 0 | 0,00 | 0,00% | 0,00% | 6 finestre, 0 segnali |

- mesi con trade: **36** su 41 analizzati (5 mesi senza alcun segnale: 2023-09, 2025-12, 2026-05, 2026-07, 2026-10);
- mesi positivi **15**, negativi **21**: il 58% dei mesi operativi è in perdita;
- miglior mese **2026-08** (+1.581,00 USDT, 3 trade, 100% vincenti, **40,36%** del P&L netto);
- peggior mese **2024-04** (−1.065,83 USDT, 4 trade, 0 vincenti, −27,21% del P&L netto).

**Lettura monetaria vs percentuale.** Il 2024 pesa l'84,46% del P&L monetario ma solo +32,52% di rendimento ricomposto: il P&L monetario attribuisce più capitale agli anni recenti (equity maggiore), quindi le due letture non vanno sommate né confrontate come se fossero la stessa cosa. Entrambe sono archiviate.

---

## 16. Concentrazione dei profitti (Q5, Q9)

### 16.1 Top-k

| k | P&L dei k trade (USDT) | Share P&L netto | Share profitto lordo | Return dopo rimozione (monetario) | Return dopo rimozione (ricomposto) | Mesi distinti | Fold distinti |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | — | 100% | 100% | +39,18% | +39,18% | — | — |
| 1 | 1.044,40 | **26,66%** | 6,61% | +28,73% | +27,53% | 1 | 1 |
| 3 | 2.990,46 | **76,33%** | 18,92% | +9,27% | +8,70% | 3 | 1 |
| 5 | 4.793,45 | **122,36%** | 30,33% | **−8,76%** | **−5,79%** | 5 | 2 |
| 10 | 8.146,55 | **207,95%** | 51,54% | **−42,29%** | **−27,61%** | 8 | 3 |

Due definizioni, entrambe archiviate: la rimozione **monetaria** sottrae il P&L dei k trade migliori dall'equity finale (additiva, non riordina la curva); la rimozione **ricomposta** replica CP08-01 (rinegozia l'equity senza quei trade). Convergono sullo stesso messaggio: **senza 5 trade il rendimento è negativo**.

Indici di concentrazione: Gini sul P&L positivo **0,470** (0,499 sul valore assoluto), HHI sul P&L positivo **0,0380** → *effective trades* **26,3** su 46 vincenti; HHI sulle perdite 0,0358 → 27,9. Il decile migliore dei trade vincenti (4 trade) produce il **24,69%** del profitto lordo.

### 16.2 Concentrazione temporale

| Misura | Valore |
|---|---:|
| Miglior mese (2026-08) — share del P&L netto | **40,36%** |
| Miglior trimestre (2024Q1) — share | **63,51%** |
| Miglior fold (1) — share | **65,39%** |
| Top-1 mese / top-3 mesi / top-5 mesi — share cumulata | 40,36% / **103,61%** / 157,38% |
| Mesi migliori necessari a coprire il 50% / 80% / 100% del P&L netto | **2** / **3** / 3 |
| Fold migliori necessari a coprire il 50% / 80% | **1** / **2** |

I top-3 mesi **da soli superano** il P&L netto totale (+103,6%): tutto il resto dell'attività, in aggregato, sottrae.

### 16.3 I trade migliori sono concentrati in pochi periodi?

| Evidenza | Valore |
|---|---|
| Top-5 trade: mesi distinti | 5 su 5 (nessuna duplicazione mensile) |
| Top-5 trade: fold distinti | **2** (4 su 5 nel fold 1) |
| Top-3 trade: fold distinti | **1** |
| Contributo di ciascun top-5 trade al P&L netto | 22,8% – 26,7% ciascuno |
| Miglior mese per P&L complessivo | 2026-08 (3 trade, solo 1 dei top-5) |

Risposta: **sì a livello di fold, no a livello di mese**. I trade più redditizi si addensano in **un singolo fold** (il fold 1, feb–ott 2024), ma non in un singolo mese; il mese migliore per P&L complessivo (2026-08) è un cluster *diverso* da quello dei trade migliori. Esistono quindi **due** fenomeni di concentrazione sovrapposti — un fold fortunato e un mese fortunato — e nessuno dei due è un evento ripetuto nel campione.

---

## 17. Drawdown mark-to-market e durata (Q6)

Due convenzioni, entrambe misurate sull'equity **monetaria** (100% equity, nessuna leva):

| Metrica | Exit-marked (ledger) | Mark-to-market orario |
|---|---:|---:|
| Max drawdown | −21,1804% | **−27,2024%** |
| Rapporto MTM / exit-marked | — | **1,284×** |
| Episodi di drawdown | 7 | 29 |
| Episodi ≥ 1% / 2% / 5% / 10% | 7 / 6 / 4 / 2 | 12 / 10 / 5 / 2 |
| Episodi recuperati / aperti | 6 / **1** | 28 / **1** |
| Tempo sotto il picco | 94,80% | 98,26% |
| Durata massima sott'acqua | **14.449 h** (≈602 giorni) | **19.404 h** (≈808 giorni) |
| Recovery mediana | 1.561 h (≈65 giorni) | 5 h (micro-episodi da costo di entrata) |
| Ulcer index | 9,60 | **14,30** |

### 17.1 L'episodio peggiore è ancora aperto

| Convenzione | Picco | Profondità | Trough | Recovery |
|---|---|---|---:|---|
| Exit-marked | 2025-02-27 23:00 · 14.631,96 USDT | −21,18% | 2026-04-19 · 11.532,86 | **mai** |
| Mark-to-market | 2024-08-05 12:00 · 15.731,81 USDT | −27,20% | 2026-06-04 04:00 · 11.452,38 | **mai** |

L'equity finale (13.917,58 USDT) è **−4,88%** sotto il massimo exit-marked e **−11,53%** sotto il massimo mark-to-market. Il drawdown massimo non è un evento storico chiuso: è lo stato corrente, con **oltre due anni** di calendario sotto il picco MTM.

### 17.2 Per fold

| Fold | Trade | P&L USDT | Max DD exit-marked | Max DD MTM | Ore MTM sotto il picco |
|---:|---:|---:|---:|---:|---:|
| 0 | 15 | +260,84 | −3,95% | −6,09% | 3.083 |
| 1 | 35 | +2.561,84 | −11,59% | **−18,49%** | 2.081 |
| 2 | 23 | +977,12 | −7,39% | −10,95% | 2.483 |
| 3 | 19 | −1.893,76 | **−15,91%** | **−19,68%** | 3.463 |
| 4 | 14 | +2.011,54 | −3,13% | −4,83% | 2.228 |

Il fold più redditizio (1) è anche quello con il drawdown intra-hold più profondo dopo il fold perdente: **rendimento e rischio arrivano dallo stesso blocco di trade**.

> Nota di lettura: il "tempo sotto il picco" in mark-to-market include i micro-drawdown da costo di entrata (l'equity MTM parte già al netto di fee/slippage di entrata). Per la *persistenza* usare l'exit-marked; per il *rischio capitale* il mark-to-market. Entrambi sono riportati per non scegliere in silenzio.

---

## 18. Sequenze di vittorie e perdite (Q7)

| Metrica | Valore |
|---|---:|
| Vincite / perdite / piatte | 46 / 60 / 0 |
| Win rate | 43,40% |
| Streak vincente massima | **8** |
| Streak perdente massima | **6** |
| Streak media vincente / perdente | 1,92 / 2,50 |
| Run totali osservati | 48 |
| Run attesi sotto iid (stessa win rate) | 53,08 |
| Runs test z (Wald–Wolfowitz) | **−1,008** (nessuna dipendenza seriale significativa) |

| Lunghezza | Streak vincenti | Streak perdenti |
|---:|---:|---:|
| 1 | 16 | 8 |
| 2 | 4 | 6 |
| 3 | 0 | 4 |
| 4 | 2 | 3 |
| 5 | 0 | 2 |
| 6 | 1 | 1 |
| 8 | 1 | 0 |

**Contare le perdite consecutive non basta.** La streak perdente più lunga (6, 15 ago–16 set 2024) costa −1.196,01 USDT, ma una streak di **4** perdite (24 nov 2025 – 21 gen 2026) costa **−1.330,91 USDT** e la singola perdita peggiore (2026-02-06) costa **−1.458,05 USDT**. In un sistema a equity composta il costo di una sequenza dipende dal *capitale a rischio in quel momento*, non dal numero di esiti negativi.

Il runs test (z = −1,008) non rifiuta l'indipendenza: nessuna evidenza di dipendenza seriale degli esiti. È un test descrittivo e **non** un gate di promozione.

---

## 19. Regime di volatilità e trend (Q8)

Bucket costruiti **solo** con feature disponibili al cutoff (`momentum_168h`, `volatility_168h`, `atr_pct`, …), con soglie = **tercili delle sole finestre di training** di ciascun fold. Nessun target, nessun rendimento OOS entra nella classificazione. Il regime di trend così ricostruito coincide con quello archiviato da CP07 in **300/300** finestre.

### 19.1 Volatilità (tercili train-only di `volatility_168h`)

| Bucket | Finestre | Trade | P&L USDT | Share P&L | Return ricomposto | PF monetario | Media net/trade | Vol media 168h |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| VOL_LOW | 184 | 43 | +2.286,28 | 58,36% | +21,80% | 1,569 | +0,497% | 0,0089 |
| VOL_MEDIUM | 81 | 40 | +3.762,53 | 96,04% | +33,51% | 2,171 | +0,760% | 0,0139 |
| VOL_HIGH | 35 | 23 | **−2.131,24** | **−54,40%** | **−14,41%** | **0,542** | **−0,612%** | 0,0272 |

### 19.2 Trend (tercili train-only di `momentum_168h`)

| Bucket | Finestre | Trade | P&L USDT | Share P&L | Return ricomposto | PF monetario | Media net/trade |
|---|---:|---:|---:|---:|---:|---:|---:|
| TREND_DOWN | 80 | 49 | +1.073,68 | 27,41% | +8,83% | 1,174 | +0,220% |
| SIDEWAYS | 119 | **0** | 0,00 | 0,00% | 0,00% | — | — |
| TREND_UP | 101 | 57 | +2.843,89 | 72,59% | +27,88% | 1,498 | +0,472% |

### 19.3 Direzione e incrocio trend × volatilità

| Vista | Bucket | Trade | P&L USDT | PF monetario | Return ricomposto |
|---|---|---:|---:|---:|---:|
| Direzione | LONG | 57 | +2.843,89 | 1,498 | +27,88% |
| Direzione | SHORT | 49 | +1.073,68 | 1,174 | +8,83% |
| Trend × vol | TREND_DOWN / VOL_MEDIUM | 17 | +2.548,45 | 4,361 | +21,54% |
| Trend × vol | TREND_UP / VOL_LOW | 24 | +2.152,99 | 2,039 | +21,09% |
| Trend × vol | TREND_UP / VOL_MEDIUM | 23 | +1.214,08 | 1,494 | +9,86% |
| Trend × vol | TREND_DOWN / VOL_LOW | 19 | +133,29 | 1,068 | +0,59% |
| Trend × vol | TREND_DOWN / VOL_HIGH | 13 | **−1.608,06** | 0,537 | −10,98% |
| Trend × vol | TREND_UP / VOL_HIGH | 10 | **−523,17** | 0,555 | −3,86% |

**Due avvertenze strutturali, da non ignorare:**

1. **Collinearità direzione/trend.** Con soglia 5% e tercili di training, ogni LONG cade in TREND_UP e ogni SHORT in TREND_DOWN (0 trade in SIDEWAYS). Il "breakdown per trend" **è** il breakdown per direzione: non contiene informazione indipendente. La variabile discriminante aggiuntiva è la **volatilità**.
2. **Campioni piccoli e condizionamento retrospettivo.** Le celle trend×vol hanno 10–24 trade; quella negativa più ampia (TREND_DOWN/VOL_HIGH) ne ha 13. Anche se il segno è coerente in entrambe le direzioni (volatilità alta negativa), questa è una **lettura sul campione**: non è un filtro validato, è stata osservata *dopo* aver visto i risultati OOS e non è mai stata pre-registrata. La strategia **congelata** non contiene alcun filtro di volatilità.

---

## 20. Visualizzazioni

Nuove figure read-only (Plotly) nella tab dashboard **Momentum Stability** e funzioni in `dashboard/charts.py`:

| Figura | Funzione | Contenuto |
|---|---|---|
| Equity OOS | `make_stability_equity_figure` | exit-marked vs mark-to-market + capitale iniziale |
| Underwater | `make_underwater_figure` | drawdown % dal picco per entrambe le convenzioni |
| Distribuzione P&L | `make_pnl_distribution_figure` | istogramma vincite/perdite + media/mediana (USDT o %) |
| Profit concentration | `make_profit_concentration_figure` | P&L per trade ordinato + share cumulata, marker top 1/3/5/10 |
| P&L per periodo | `make_period_pnl_figure` | barre monetarie per mese/trimestre/anno/fold + cumulato |

Le figure leggono **solo** l'archivio CP08-02 (`equity_curves.parquet`, `trade_attribution.parquet`, …): la dashboard non ricalcola metriche.

Rendering statico (read-only) in `docs/screenshots/`, rigenerabile con `python scripts/render_screenshots.py`:

| File | Contenuto |
|---|---|
| `momentum_stability_equity.png` | equity exit-marked vs mark-to-market |
| `momentum_stability_underwater.png` | underwater curve delle due convenzioni |
| `momentum_stability_pnl_distribution.png` | distribuzione del P&L monetario per trade |
| `momentum_stability_concentration.png` | P&L ordinato + share cumulata (picco = profitto lordo) |
| `momentum_stability_period_pnl.png` | P&L monetario mensile + cumulato |

---

## 21. Artefatti

Archivio additivo: `data/experiments/S02-CP08-momentum-stability-v1/`

| File | Contenuto | Righe / blocchi | SHA-256 |
|---|---|---:|---|
| `trade_attribution.parquet` | 1 riga per trade eseguito, P&L monetario, contributi, rank, bucket di regime | 106 | `87e0f72f1039d82f04905a3111fbf0930fc97a23f777ccbe33a57197a4be287a` |
| `period_summary.parquet` | metrica per finestra/fold/mese/trimestre/anno | 365 | `851cff37dbae2e8bafcee26894b00bcabf297a3aa3d40de80d59d66130b6a292` |
| `concentration_summary.json` | top-k, share, Gini/HHI, concentrazione temporale, cluster top-5 | — | `f79fd1d4909d0c0120a61d915f46a2bc30254b57175363bc42ac55c784bab97f` |
| `drawdown_analysis.json` | episodi DD exit-marked e MTM, durate, ulcer, per fold | — | `b94b74d7cc29e121169be3d72ab6a47181f739a98831152e6b33549765579e18` |
| `manifest.json` | hash sorgenti prima/dopo, hash output, riconciliazione, policy congelata | — | — |
| `regime_breakdown.parquet` *(aggiuntivo)* | metriche per bucket trend/vol/direzione/celle | 21 | `83ca519c6b4559a06e9e9edb4997aba95960c6ee242ad41f53bae99c7e2630bf` |
| `streak_analysis.json` *(aggiuntivo)* | run-length, distribuzioni, runs test | — | `0852b8204ff5a50f39f3b5c9b66bb3afcd3c656d8e1f5405826d484fdfeab80c` |
| `equity_curves.parquet` *(aggiuntivo)* | 59.090 punti orari step + MTM con drawdown | 59.090 | `f70d1e1bc2377efdf11931d173ce1aa7e1f8372e8692f5699128e9d61379137b` |

`manifest.json` riporta `source_hashes_unchanged: true`, `original_artifacts_modified: false`, `policy_frozen: {threshold: 0.05, threshold_reselected: false, training_performed: false, new_inference: false, data_downloaded: false}`, la riconciliazione completa (67 controlli) e la catena di hash con CP08-01.

---

## 22. Riproducibilità e test

```bash
# analisi completa (read-only, rifiuta di sovrascrivere l'output)
python -m crypto_decision_lab.momentum_stability

# solo stampa, senza scrittura
python -m crypto_decision_lab.momentum_stability --no-write

# test dedicati
pytest tests/test_momentum_stability.py tests/dashboard/test_stability_dashboard.py -q
pytest -q
```

Tempi osservati: ~6–8 s per l'analisi completa (caricamento snapshot incluso); suite completa **142 test** in ~27–45 s (124 preesistenti + 18 nuovi). Exit code del CLI: `0` se tutti i 67 controlli di riconciliazione passano, `2` altrimenti.

| Test | Copertura |
|---|---|
| `test_trade_attribution_monetary_pnl_is_additive_and_chains_equity` | identità `pnl = E_before·net`, equity finale, rank, share |
| `test_period_summary_granularities_are_additive_and_reconcile` | 5 granularità additive, formato finestre, doppio win rate |
| `test_concentration_top_k_is_monotone_and_matches_audited_leave_out` | monotonia leave-top-k + uguaglianza con CP08-01 |
| `test_concentration_single_winning_trade_is_maximally_concentrated` | HHI/Gini nei casi limite |
| `test_streak_analysis_counts_runs_on_a_known_sequence` | run-length e distribuzioni su sequenza nota |
| `test_drawdown_episodes_tracks_recovery_and_open_episode` | episodio recuperato + episodio aperto + durate |
| `test_drawdown_analysis_reports_both_conventions` | MTM più profondo dell'exit-marked |
| `test_regime_breakdown_shares_are_additive_and_flags_small_samples` | share additive, NaN su equity non contigua, flag campione |
| `test_real_archive_reconciles_with_cp07_and_cp08` | 67 controlli reali, hash chain, numeri CP07/CP08 |
| `test_archive_write_is_additive_and_refuses_overwrite` | hash sorgenti invariati, no overwrite |
| 7 test dashboard | adapter read-only, file mancanti, riconciliazione non-ok, 5 figure |

---

## 23. Gate CP08-02

| Gate | Esito |
|---|---|
| Ricostruzione completa dei 106 trade OOS | **PASS** (106/106) |
| Riconciliazione equity e P&L monetario con il simulatore auditato | **PASS** (67/67, delta max 3,64e−12) |
| P&L monetario additivo a ogni granularità | **PASS** (errore max 1,4e−12 USDT) |
| Statistiche per trade, finestra e periodo | **PASS** |
| Analisi fold / anno / mese | **PASS** |
| Concentrazione top 1/3/5/10 | **PASS** |
| Max drawdown mark-to-market e durata | **PASS** |
| Sequenze vinte/perse | **PASS** |
| Regime con sole feature al cutoff | **PASS** (500/500 guardia, 300/300 regime, soglie train-only) |
| Cluster temporale dei trade migliori | **PASS** |
| Grafici e dashboard read-only | **PASS** (5 figure, nuovo tab, adapter read-only) |
| Nessun training / inferenza / download | **PASS** |
| Nessuna modifica agli artefatti originali | **PASS** (hash invariati) |
| Nessuna ottimizzazione delle soglie su OOS | **PASS** (soglia archiviata unica: 5%) |

**Gate statistico di promozione: NON eseguito in questo step e invariato → INCONCLUSIVE.**  
**Promozione al trading: NO-GO.** L'analisi descrittiva non produce, e non può produrre, un GO.

---

## 24. Cosa aggiunge CP08-02 (e cosa non cambia)

**Aggiunge**

- la prova che il P&L monetario è **additivo** e riconciliato bit-per-bit con il simulatore auditato su ogni granularità;
- il profilo completo del rischio **mark-to-market**: −27,20%, episodio **ancora aperto** da oltre 800 giorni, ulcer index 14,30;
- la misura esplicita della concentrazione: senza 5 trade il rendimento è **negativo**; i top-3 trade valgono il 76% del P&L netto; i top-3 mesi coprono il 104% del P&L netto;
- la separazione tra **concentrazione per fold** (reale, 4/5 dei trade migliori nel fold 1) e **concentrazione per mese** (assente tra i top trade);
- la caratterizzazione per **volatilità al cutoff**: VOL_HIGH negativo in entrambe le direzioni (−14,41%, PF 0,542) — descrittiva, non un filtro;
- il fatto che la **streak perdente più lunga non è la più costosa** (−1.196 vs −1.458 USDT di una singola perdita);
- 18 test automatici e una vista dashboard read-only dedicata.

**Non cambia**

- rendimento +39,18%, 106 trade, PF monetario 1,3295, DD MTM −27,20%;
- il verdetto **INCONCLUSIVE** e il **NO-GO** alla produzione;
- il gate statistico: nessun bootstrap, nessun paired test, nessuna rivalutazione di significatività è stata rifatta qui (i numeri CP08-01 restano l'unico riferimento inferenziale);
- i requisiti §11 restano **aperti**: questo step **non** è una validazione indipendente. Mancano ancora pre-registrazione della griglia, dati OOS genuinamente nuovi, funding/borrow, latenza come scenario base, walk-forward più fine. Senza quelli, il Momentum Threshold resta un candidato di ricerca e Intern-Decision-0.8B **non va addestrato**.

---
---

# Parte III · S02-CP08-03 — Parameter & Execution Robustness

**Scope CP08-03:** robustezza di parametri, costi ed esecuzione del Momentum Threshold, con **griglia di soglie pre-registrata**, selezione **solo sul training** di ogni fold e superficie OOS **esclusivamente esplorativa**.  
**Data analisi:** 2026-10-29 · **Stato:** **CP08-03 COMPLETE** · **13 controlli: 12 PASS, 1 WARN, 0 FAIL** · **36 nuovi test** (27 core + 9 dashboard; suite **178 PASS**).  
**Vincoli rispettati:** nessun training di Intern-Decision-0.8B, nessuna nuova inferenza Timestral, nessun download, nessuna modifica agli archivi originali, nessuna promozione di parametri scelti sui dati OOS, nessun trading reale.  
**Gate tecnico:** **PASS** (esperimenti riproducibili, senza leakage, metriche riconciliate).  
**Gate strategico:** **INCONCLUSIVE** — invariato. **Promozione al trading: NO-GO.**

> **Avvertenza di metodo.** Questa parte risponde a una domanda diversa da CP08-02: non «quanto è stabile il risultato?» ma «quanto sopravvive se cambiamo i parametri e le ipotesi di esecuzione?». Non produce una nuova strategia. La griglia di soglie è fissata *prima* di eseguire gli esperimenti di questo step, ma i dati OOS erano già stati osservati in CP07/CP08-01/CP08-02: per questo la superficie OOS è **descrittiva e non promuovibile**. Un PASS tecnico non è un GO.

---

## 25. Metodo, pre-registrazione e separazione degli esperimenti

### 25.1 Riutilizzo del simulatore auditato

Nessuna formula di rendimento è stata riscritta. Il modulo `parameter_robustness.py` importa da `simulator_integrity_audit`:

| Funzione riusata | Uso in CP08-03 |
|---|---|
| `contract_frame` | normalizzazione dell'archivio CP07 al contratto decisionale |
| `reconstruct_returns` / `replay_policy` | ricostruzione dei rendimenti dagli open grezzi e replay del ledger |
| `dollar_metrics` | PF monetario, P&L USDT, win rate per trade/finestra |
| `calendar_equity_curves` | curva oraria exit-marked e mark-to-market |
| `concentration_profile` | leave-top-k ricomposto |
| `bootstrap_profile` | CI del rendimento composto, del PF percentuale e della media |

È stata aggiunta **una sola** funzione numerica nuova, `vectorized_net_pct`, che applica la stessa formula di `net_return` su array (per rendere sostenibile una griglia da 15 soglie × 5 fold × 9 scenari). Un test verifica l'uguaglianza element-wise con la funzione auditata a 1e−12 su prezzi reali.

### 25.2 Griglia di soglie pre-registrata

Costante di modulo, scritta nel manifest, **non modificata dopo aver visto un risultato**:

```text
0% · 0,5% · 1% · 2% · 3% · 4% · 5% · 6% · 7% · 8% · 9% · 10% · 12% · 15% · 20%
```

Contiene esplicitamente 5% (CP07), 8% e 10% (le alternative emerse in CP08-01). La regola di selezione è quella di CP07: **argmax del multiplo composto sul training del fold**, con pareggio vinto dalla soglia più bassa. Nessun dato di test entra nella scelta.

### 25.3 Due esperimenti sui costi, non uno

| Esperimento | Cosa cambia | Cosa resta fermo | Interpretazione |
|---|---|---|---|
| **A · `cost_frozen`** | solo il modello di costo | 106 decisioni, date, direzioni, soglia 5% | isola l'effetto del costo → **deve essere monotono** |
| **B · `policy_refit`** | costo **e** soglia riselezionata sul training | la regola di selezione | misura costo **+ ri-selezione** → **non** è monotono |

Questa separazione risolve il finding **F10** di CP08-01: la tabella costi CP07 era non monotona perché ogni scenario rifittava la soglia, non perché «costasse meno e rendesse meno».

### 25.4 Esecuzione e carry

* **Latenza 0h / 1h / 2h**: entry e exit spostati sugli **open reali** delle barre ritardate; l'holding resta 24h. Nessuna barra mancante (0/106 in tutti e tre gli scenari). **1h è lo scenario conservativo principale.**
* **Borrow/funding SHORT**: quattro **ipotesi parametriche** (`carry_credit`, `carry_neutral`, `carry_stressed`, `carry_severe`), applicate al solo nozionale SHORT alla chiusura del hold. Il repository contiene solo prezzi spot: **non esiste una serie storica di funding** e nessun dato è stato inventato.
* **Envelope conservativo**: latenza 1h + costo 0,20% per lato + carry SHORT stressato.

### 25.5 Garanzie anti-leakage

| Guardia | Esito |
|---|---|
| Fold cronologici, blocchi di test disgiunti, purge 24h | 5/5 fold |
| `max(train cutoff) < min(test cutoff)` | 5/5 fold |
| Selezione soglia ricalcolata **corrompendo con NaN l'intero test block** di ciascun fold | soglie **identiche** in 5/5 fold |
| Soglie selezionate ⊆ griglia pre-registrata | 100% (45 selezioni di scenario + 75 di superficie) |
| Feature `momentum_168h` disponibile al cutoff | ereditata da CP06B/CP08-02 (barra = cutoff − 1h) |
| Superficie OOS | colonna `exploratory=True`, `promotion=False` |

---

## 26. Riferimento congelato e riconciliazione

La policy CP07 (soglia 5%, fee 0,10%, slippage 0,05%) è riprodotta dagli open grezzi, non copiata dall'archivio:

| Metrica | Valore CP08-03 | Riferimento | Delta |
|---|---:|---:|---:|
| Trade | 106 | 106 (CP07/CP08-02) | 0 |
| Rendimento composto | **+39,175764%** | +39,175764% | 5,7e−14 |
| Equity finale | 13.917,5764 USDT | 13.917,5764 USDT | 0,0 |
| PF monetario | **1,3295465** | 1,3295465 | 0,0 |
| PF percentuale | 1,4111430 | 1,4111430 | 0,0 |
| Max DD exit-marked | **−21,180367%** | −21,180367% | 0,0 |
| Max DD mark-to-market | **−27,202367%** | −27,202367% | 0,0 |
| CI 95% rendimento composto | **[−24,0057%, +148,1896%]** | idem CP08-01 | 0,0 |
| P(rendimento > 0) | 84,40% | 84,40% | 0,0 |
| PF percentuale CI / P(PF>1) | [0,7984; 2,2964] / 87,70% | idem | 0,0 |

Inoltre la griglia costi di CP07 è stata **ricalcolata sulla griglia CP07** (9 scenari × soglia del training): delta massimo **0,0** rispetto a `sensitivity.json`. È l'anchor di regressione che lega CP08-03 all'archivio CP07.

I 13 controlli automatici sono in `checks.json`; l'unico WARN (`CP08-03.6`) è **voluto**: certifica che la curva con refit *non* è monotona, cioè che i due esperimenti sono davvero distinti.

---

## 27. Esperimento A · costi con decisioni congelate (monotono)

Stesso ledger di 106 trade, stesse date, stesse direzioni. Cambia solo il costo per lato:

| Costo per lato | Fee | Slippage | Trade | Rendimento | PF monetario | Max DD MTM | CI 95% rendimento | P(>0) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0,00% | 0,00% | 0,00% | 106 | **+91,2799%** | 1,7817 | −17,98% | [+4,46%, +252,80%] | 98,40% |
| 0,05% | 0,025% | 0,025% | 106 | +72,0418% | 1,6137 | −19,50% | [−6,35%, +216,07%] | 95,75% |
| 0,10% | 0,05% | 0,05% | 106 | +54,7387% | 1,4631 | −23,05% | [−16,03%, +183,14%] | 91,35% |
| **0,15% (base CP07)** | 0,10% | 0,05% | 106 | **+39,1758%** | **1,3295** | **−27,20%** | [−24,01%, +148,19%] | 84,40% |
| 0,20% | 0,10% | 0,10% | 106 | +25,1781% | 1,2102 | −31,24% | [−31,45%, +124,69%] | 73,40% |
| 0,25% | 0,15% | 0,10% | 106 | +12,5883% | 1,1044 | −35,11% | [−38,49%, +101,37%] | 62,30% |
| 0,30% | 0,20% | 0,10% | 106 | +1,2646% | 1,0104 | −38,77% | [−44,86%, +78,00%] | 48,80% |
| 0,40% | 0,267% | 0,133% | 106 | −18,0806% | 0,8524 | −45,47% | [−55,80%, +42,23%] | 22,45% |
| 0,50% | 0,333% | 0,167% | 106 | −33,7302% | 0,7262 | −51,45% | [−64,35%, +15,41%] | 6,75% |

### 27.1 Monotonia verificata (CP08-03.5 → PASS)

| Da → a (per lato) | Δ rendimento | Strettamente decrescente |
|---|---:|:---:|
| 0,00% → 0,05% | −19,2381 pp | ✓ |
| 0,05% → 0,10% | −17,3031 pp | ✓ |
| 0,10% → 0,15% | −15,5629 pp | ✓ |
| 0,15% → 0,20% | −13,9977 pp | ✓ |
| 0,20% → 0,25% | −12,5898 pp | ✓ |
| 0,25% → 0,30% | −11,3237 pp | ✓ |
| 0,30% → 0,40% | −19,3452 pp | ✓ |
| 0,40% → 0,50% | −15,6496 pp | ✓ |

L'effetto è **strettamente monotono** su tutto l'intervallo: ogni aumento di costo riduce il rendimento composto e il PF monetario. Il break-even del ledger congelato cade tra **0,25% e 0,30% per lato** (a 0,30% resta +1,26%). Il rendimento non è quindi «robusto ai costi»: metà del risultato base vive nei 15 bp per lato ipotizzati da CP07, e a 30 bp è già a zero.

**Lettura corretta:** la monotonia vale *solo* a decisioni ferme. Non dice che la strategia sceglierebbe le stesse soglie a costi diversi — quello è l'esperimento B.

---

## 28. Esperimento B · refit della soglia sul training (confondimento)

Per ogni scenario di costo la soglia è ri-selezionata **solo sul training** di ciascun fold, poi applicata al test block. Soglie scelte (in %):

| Costo per lato | Fold 0 | Fold 1 | Fold 2 | Fold 3 | Fold 4 | Trade OOS | Rendimento OOS | PF monetario | Max DD MTM |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0,00% | 0 | 0 | 0 | 0 | 0 | 300 | **+119,8473%** | 1,3049 | −25,97% |
| 0,05% | 9 | 0 | 0 | 0 | 0 | 246 | +33,6252% | 1,1243 | −27,58% |
| 0,10% | 9 | 9 | 9 | 9 | 9 | 39 | **+40,5712%** | 2,0180 | −16,84% |
| 0,15% (base) | 9 | 9 | 9 | 9 | 9 | 39 | +35,1944% | 1,8624 | −18,08% |
| 0,20% | 9 | 9 | 9 | 9 | 9 | 39 | +30,0233% | 1,7136 | −19,30% |
| 0,25% | 9 | 9 | 9 | 9 | 9 | 39 | +25,0500% | 1,5771 | −20,50% |
| 0,30% | 9 | 9 | 9 | 9 | 9 | 39 | +20,2669% | 1,4524 | −21,71% |
| 0,40% | 15 | 15 | 15 | 15 | 12 | 10 | +0,7217% | 1,0375 | −20,48% |
| 0,50% | 15 | 15 | 15 | 15 | 15 | 8 | −6,4827% | 0,6733 | −21,40% |

La curva **non è monotona** (0,05% → 0,10% risale di +6,95 pp) e la soglia scelta cambia con il costo: 0% a costo zero, 9% a 0,10–0,30%, 12–15% a 0,40–0,50%. La regola di selezione è **instabile rispetto a un parametro che non dovrebbe cambiare la decisione strategica**.

### 28.1 La selezione train-only sulla griglia estesa non migliora l'OOS

| Configurazione | Trade OOS | Rendimento OOS | PF monetario | Max DD MTM | CI 95% | P(>0) |
|---|---:|---:|---:|---:|---:|---:|
| **Congelata CP07 · 5%** | 106 | **+39,1758%** | 1,3295 | −27,20% | [−24,01%, +148,19%] | 84,40% |
| Refit train-only · 9% (base cost) | 39 | +35,1944% | 1,8624 | −18,08% | [−13,02%, +136,48%] | 90,55% |

Il refit **non batte** la policy congelata in rendimento: la griglia estesa sceglie 9% in tutti i fold e rende 4,0 pp in meno, con **39 trade invece di 106**. Il PF monetario e il drawdown migliorano, ma su un campione di trade completamente diverso e più piccolo: non sono metriche comparabili come se fossero lo stesso portafoglio. Il dato importante è un altro: **l'osservazione di CP08-01 (F3) che «4/5 fold preferirebbero 8–10%» resta un problema di stabilità della selezione, non la prova che 8–10% sia meglio.** Su questo campione OOS non lo è.

Questo è esattamente il motivo per cui la soglia OOS migliore non va promossa: cambiare regola dopo aver visto l'OOS produrrebbe una strategia diversa, scelta sui dati di test.

---

## 29. Superficie OOS · esplorativa, non promossa

Ogni soglia della griglia applicata uniformemente ai 300 window OOS (stesse 5 fold, stessi costi base). **Nessuna di queste righe è selezionabile.**

| Soglia | Trade | Rendimento OOS | PF monetario | Max DD MTM | Win rate | CI 95% rendimento |
|---:|---:|---:|---:|---:|---:|---:|
| 0% | 300 | −10,62% | 0,9638 | −39,89% | 42,67% | [−62,66%, +137,31%] |
| 0,5% | 266 | −10,40% | 0,9594 | −33,76% | 42,11% | [−58,85%, +109,74%] |
| 1% | 238 | +4,43% | 1,0195 | −27,92% | 43,28% | [−49,18%, +128,04%] |
| 2% | 197 | +32,85% | 1,1648 | −26,70% | 44,16% | [−33,62%, +189,35%] |
| 3% | 161 | +29,03% | 1,1747 | −28,12% | 44,10% | [−36,82%, +167,87%] |
| 4% | 130 | +27,82% | 1,1926 | −29,04% | 42,31% | [−32,29%, +147,63%] |
| **5% (congelata)** | **106** | **+39,18%** | **1,3295** | **−27,20%** | **43,40%** | [−24,01%, +148,19%] |
| 6% | 83 | **+54,53%** | 1,6193 | −20,71% | 45,78% | [−11,67%, +177,55%] |
| 7% | 63 | +37,49% | 1,5593 | −22,90% | 44,44% | [−19,11%, +132,57%] |
| 8% | 46 | +38,82% | 1,7666 | −20,66% | 52,17% | [−14,55%, +136,56%] |
| 9% | 39 | +35,19% | 1,8624 | −18,08% | 53,85% | [−13,02%, +136,48%] |
| 10% | 31 | +14,83% | 1,4421 | −17,61% | 51,61% | [−22,07%, +71,33%] |
| 12% | 16 | +15,71% | 1,8340 | −15,92% | 62,50% | [−19,84%, +67,90%] |
| 15% | 8 | −1,10% | 0,9340 | −19,73% | 37,50% | [−28,60%, +34,94%] |
| 20% | 3 | −3,89% | 0,6756 | −12,25% | 66,67% | [−29,82%, +25,69%] |

### 29.1 Superficie di training (l'unica informazione selezionabile)

| Soglia | Rendimento training medio (5 fold) | Min | Max |
|---:|---:|---:|---:|
| 0% | +27,64% | +20,91% | +32,89% |
| 1% | −0,12% | −4,20% | +5,89% |
| 5% | +54,70% | +31,57% | +81,57% |
| 6% | +56,25% | +28,65% | +84,12% |
| 8% | +56,04% | +32,00% | +73,07% |
| **9%** | **+89,20%** | **+64,59%** | **+109,33%** |
| 10% | +63,82% | +49,02% | +79,85% |
| 12% | +60,30% | +43,23% | +77,12% |
| 15% | +51,64% | +41,43% | +63,68% |
| 20% | +19,44% | +11,87% | +25,89% |

Tre fatti, tutti scomodi:

1. **La superficie è irregolare.** Il massimo OOS (6%) non è né il massimo di training (9%) né un punto stabile: 5% → 6% sale, 6% → 7% scende, 7% → 8% risale, 8% → 10% crolla.
2. **Il massimo di training non predice l'OOS.** Il training preferisce 9% in 5/5 fold, ma il 9% OOS rende 35,19% contro 39,18% del 5% congelato. Il ranking train→test non è monotono né stabile.
3. **Tutti gli intervalli OOS includono lo zero**, incluso il 6% ([−11,67%, +177,55%]). Nessuna soglia è distinguibile da NO_TRADE con questo campione.

**Nessuna configurazione viene promossa.** Il 6% è riportato come *osservazione* (`best_oos_threshold_exploratory = 6%`), non come raccomandazione: è stato scelto guardando i dati OOS, cioè con la stessa informazione che il protocollo vieta di usare per selezionare.

---

## 30. Latenza di esecuzione (0h / 1h / 2h)

| Latenza | Trade | Rendimento | PF monetario | Max DD exit | Max DD MTM | CI 95% rendimento | P(>0) |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0h (base) | 106 | +39,1758% | 1,3295 | −21,18% | −27,20% | [−24,01%, +148,19%] | 84,40% |
| **1h (conservativo principale)** | 106 | **+23,5905%** | **1,2217** | −20,94% | **−30,76%** | [−29,53%, +108,90%] | 73,90% |
| 2h | 106 | +23,8973% | 1,2002 | −22,52% | −30,03% | [−30,44%, +120,11%] | 74,55% |

Barre mancanti: **0/106** in tutti e tre gli scenari. Il rendimento è ricostruito dagli open reali delle barre spostate, non da un prezzo inventato.

**1h è lo scenario conservativo principale** per due ragioni: (a) è il ritardo più plausibile tra chiusura della barra e ordine eseguito; (b) ha il **drawdown mark-to-market più profondo** (−30,76% vs −30,03% del 2h). Il 2h ha un PF monetario leggermente più basso (1,2002 vs 1,2217) e un rendimento 0,31 pp più alto: le differenze tra 1h e 2h sono dentro il rumore (intervalli bootstrap quasi interamente sovrapposti) e **non** giustificano una scelta. **Non si seleziona 2h.**

Il punto sostanziale: 1h di ritardo costa **15,59 pp di rendimento** e **peggiora il rischio di percorso di 3,56 pp**. La latenza non è solo un costo di rendimento: è un aumento del drawdown mark-to-market. Metà del vantaggio dichiarato da CP07 vive nell'esecuzione istantanea sull'open del cutoff.

---

## 31. Borrow/funding SHORT · ipotesi, non dati storici

**Nessun dato storico di funding o borrow è disponibile nel repository.** Questi scenari sono un'ipotesi parametrica dichiarata, applicata al **solo nozionale SHORT** alla chiusura del hold di 24h; i LONG sono bit-per-bit identici alla base (verificato trade per trade).

| Scenario | Funding | Borrow | Carry per trade SHORT | Rendimento | Δ vs base | PF monetario | Max DD MTM |
|---|---|---:|---:|---:|---:|---:|---:|
| `carry_credit` | −0,01%/8h | 0 | **−0,03%** (credito) | +41,2364% | +2,06 pp | 1,3474 | −26,57% |
| `carry_neutral` | +0,01%/8h | 0,02%/giorno | +0,05% | +35,8066% | −3,37 pp | 1,3004 | −28,25% |
| `carry_stressed` | +0,03%/8h | 0,05%/giorno | +0,14% | +29,9422% | −9,23 pp | 1,2500 | −30,13% |
| `carry_severe` | +0,05%/8h | 0,10%/giorno | +0,25% | +23,1107% | −16,07 pp | 1,1918 | −32,40% |

Il rendimento è **monotono non crescente nel carry** e il PF scende da 1,35 a 1,19. Lo scenario severo (−16 pp) è più grande dell'intero vantaggio dell'esecuzione istantanea: se lo SHORT spot costasse davvero 25 bp per trade, il risultato del Momentum sarebbe molto più vicino a zero.

Due limiti da dichiarare:
- il mark-to-market di questi scenari **non** include la maturazione intra-hold del carry (applicato alla chiusura): il DD MTM è un limite inferiore;
- il funding reale è stocastico e può essere a favore o contro lo short; qui è un parametro fisso, non una simulazione storica.

---

## 32. Envelope conservativo e intervalli di confidenza

| Scenario | Trade | Rendimento | PF monetario | Max DD MTM | CI 95% | P(>0) |
|---|---:|---:|---:|---:|---:|---:|
| Base CP07 | 106 | +39,1758% | 1,3295 | −27,20% | [−24,01%, +148,19%] | 84,40% |
| **1h conservativo** | 106 | **+23,5905%** | 1,2217 | **−30,76%** | [−29,53%, +108,90%] | 73,90% |
| 1h + 0,20%/lato | 106 | +11,1603% | 1,1039 | −34,66% | [−36,67%, +86,02%] | 61,80% |
| 1h + 0,20%/lato + carry stressato | 106 | **+3,7854%** | **1,0348** | **−37,35%** | [−41,63%, +74,19%] | **51,80%** |

Nell'envelope conservativo il punto stimato resta positivo (+3,79%) ma **P(rendimento > 0) = 51,8%**: è indistinguibile da un lancio di moneta. Il gate non cambia: con questo campione la strategia non è separabile da NO_TRADE in nessuno scenario realistico.

Bootstrap: moving block, blocco 10, 2.000 draw, seed 42, sulle **300 finestre OOS** (inclusi i NO_TRADE con rendimento zero), come in CP08-01/CP08-02.

---

## 33. Concentrazione dei profitti e stabilità per fold

### 33.1 Concentrazione (leave-top-k)

| k | Base CP07 · share P&L netto | Base · rendimento senza i top-k | **1h** · share P&L netto | **1h** · rendimento senza i top-k |
|---:|---:|---:|---:|---:|
| 1 | 26,66% | +27,53% | **41,24%** | +14,63% |
| 2 | 51,99% | +17,18% | 75,12% | +6,83% |
| 3 | 76,33% | +8,70% | **108,15%** | **−0,14%** |
| 5 | **122,36%** | **−5,79%** | **169,65%** | **−11,74%** |
| 10 | 207,95% | −27,61% | 293,13% | −31,29% |

Nello scenario conservativo 1h la concentrazione **peggiora in termini relativi**: il singolo trade migliore vale il **41,2%** del P&L netto (contro 26,7% nella base) e bastano **3 trade** rimossi per portare il rendimento sotto zero. Gini sul P&L positivo 0,5075 (base 0,4703); HHI → 25,5 trade effettivi (base 26,3).

### 33.2 Stabilità per fold · base CP07

| Fold | Trade | P&L concatenato | Share P&L | Return fold | PF monetario | Max DD MTM |
|---:|---:|---:|---:|---:|---:|---:|
| 0 | 15 | +260,84 | 6,66% | +2,61% | 1,279 | −6,09% |
| 1 | 35 | **+2.561,84** | **65,39%** | +24,97% | 1,590 | **−18,49%** |
| 2 | 23 | +977,12 | 24,94% | +7,62% | 1,419 | −10,95% |
| 3 | 19 | **−1.893,76** | **−48,34%** | **−13,72%** | 0,493 | −19,68% |
| 4 | 14 | +2.011,54 | 51,35% | +16,90% | 4,698 | −4,83% |

### 33.3 Stabilità per fold · conservativo 1h

| Fold | Trade | P&L concatenato | Share P&L | Return fold | PF monetario | Max DD MTM |
|---:|---:|---:|---:|---:|---:|---:|
| 0 | 15 | +357,10 | 15,14% | +3,57% | 1,418 | −6,17% |
| 1 | 35 | +1.180,80 | 50,05% | +11,40% | 1,267 | **−23,53%** |
| 2 | 23 | +1.128,98 | 47,86% | +9,78% | 1,681 | −9,33% |
| 3 | 19 | **−1.477,03** | **−62,61%** | **−11,66%** | 0,500 | −17,93% |
| 4 | 14 | +1.169,20 | 49,56% | +10,45% | 2,566 | −6,64% |

Con 1h di latenza il fold 1 resta il motore del profitto ma dimezza il contributo (+50% del netto) e il fold 3 diventa ancora più dominante in negativo (−62,6%). Il **drawdown mark-to-market peggiore per fold** passa da −19,68% (fold 3, base) a **−23,53%** (fold 1, 1h): il fold più redditizio diventa anche il più rischioso. La distribuzione per fold non è stabile in nessuno dei due scenari.

---

## 34. Findings CP08-03

| ID | Severità | Finding | Evidenza | Stato |
|---|---|---|---|---|
| R1 | Media | La soglia 5% non è un ottimo: la superficie OOS è irregolare (5% → 6% ↑, 6% → 7% ↓, 8% → 10% ↓). | §29 | Documentato; nessuna promozione |
| R2 | Media | L'effetto dei costi è monotono **solo** a decisioni congelate; la sensitivity CP07 non lo era perché rifittava la soglia. | §27–28, F10 chiuso | Due esperimenti separati |
| R3 | Alta | La selezione train-only su griglia estesa sceglie 9% in 5/5 fold e rende **meno** del 5% congelato (35,19% vs 39,18%). | §28.1 | Documentato; 5% resta il riferimento |
| R4 | Alta | 1h di latenza: −15,59 pp di rendimento e **+3,56 pp di drawdown MTM**. | §30 | 1h = scenario conservativo principale |
| R5 | Media | Funding/borrow SHORT non sono nei dati: scenari parametrici, −3,4 / −9,2 / −16,1 pp. | §31 | Ipotesi dichiarate |
| R6 | Alta | La concentrazione resta estrema: nel 1h senza i top-3 trade il rendimento è negativo; il top-1 vale il 41% del netto. | §33 | Documentato |
| R7 | Media | L'envelope conservativo (1h + 0,20% + carry) ha P(>0) = 51,8%: indistinguibile da NO_TRADE. | §32 | Nessun GO |

### 34.1 Cosa **non** è stato fatto

- Nessuna soglia è stata promossa sulla base della superficie OOS.
- Nessun nuovo modello è stato addestrato (Intern-Decision-0.8B incluso).
- Nessun dato è stato scaricato, nessun forecast Timestral è stato rigenerato.
- Nessun archivio CP07/CP06B/CP08-01/CP08-02 è stato modificato: hash prima/dopo identici, `source_hashes_unchanged: true`.

---

## 35. Artefatti

Archivio additivo: `data/experiments/S02-CP08-parameter-robustness-v1/`

| File | Contenuto | Righe / blocchi | SHA-256 |
|---|---|---:|---|
| `scenario_metrics.parquet` | 28 scenari (9 costi congelati, 3 latenze, 4 carry, 3 combined, 9 refit) con rendimento, PF monetario/percentuale, DD exit/MTM, CI bootstrap | 28 | `c306c677dda72ecfbe8e0b7b108d53c3f944244b5dfa7bed26f8172a202af810` |
| `threshold_surface.parquet` | superficie training (75 righe), OOS per fold (75), OOS aggregata (15) — tutte le righe OOS `exploratory=True`, `promoted=False` | 165 | `db84b34bf12e9ae37f148314383cc05fdef86d918144cbc4de1b3652d77bdd78` |
| `threshold_selection.parquet` | soglie scelte sul training per scenario × fold, con multiplo e trade di training | 45 | `1fe36befbb5b884eb78904c362e8c47c3a3da109756a9ef9d161bfe6f1bd0a7e` |
| `fold_stability.parquet` | metriche per fold × scenario: P&L locale e concatenato, share, return, PF, DD exit/MTM | 140 | `edb48eed343872c10cf81281e88ed3c025984b52c4e33e3f3f4ee7188b232b49` |
| `concentration.json` | leave-top-k monetario e ricomposto per base, 1h, combined e carry; Gini/HHI | — | `ceff139cd528fd8eab8d277c84360ea96533ba0fc671708c46e8e09db5442a8b` |
| `checks.json` | 13 controlli con evidenza (12 PASS, 1 WARN, 0 FAIL) | 13 | `fc6c2c04077f5da8628c5e6e53f0046360f56f847f9717dc39bec17f9bb32f08` |
| `summary.json` | pre-registrazione, riferimento congelato, esperimenti, findings, gate | — | `218b0e135b17fc3429d20e2cd044f02d0c1bd61adc5c8b04cd97af5353c747e9` |
| `manifest.json` | hash sorgenti prima/dopo, hash output, pre-registrazione, gate, metodo | — | `f066d8ebee451de1a2205930ec2bb4a262748a4e29d7bb4f3244512deb756da6` |

`manifest.json` riporta `source_hashes_unchanged: true`, `original_artifacts_modified: false`, `policy_frozen: {threshold: 0.05, threshold_reselected_on_oos: false, training_performed: false, new_inference: false, data_downloaded: false, promotion_on_oos: false}`, `technical_gate: PASS`, `strategic_gate: INCONCLUSIVE`, `promotion: NO-GO`.

---

## 36. Dashboard, riproducibilità e test

### 36.1 Dashboard read-only

Nuovo tab **Parameter Robustness** (adapter `dashboard/parameter_robustness_adapter.py`, component `components/parameter_robustness.py`), che legge **solo** l'archivio CP08-03 e non ricalcola metriche. Sezioni: costi (A vs B), latenza e carry, superficie soglia, fold & concentrazione, controlli. Quattro nuove figure Plotly:

| Figura | Funzione | Contenuto |
|---|---|---|
| Costi | `make_cost_sensitivity_figure` | curva congelati (monotona) vs refit (non monotona) |
| Latenza | `make_latency_robustness_figure` | rendimento e DD MTM per 0h/1h/2h |
| Superficie | `make_threshold_surface_figure` | training per fold, OOS con banda CI, linee 5/8/10% |
| Intervalli | `make_scenario_interval_figure` | CI bootstrap degli scenari chiave |

Screenshot statici read-only in `docs/screenshots/`, rigenerabili con `python scripts/render_screenshots.py`:

| File | Contenuto |
|---|---|
| `parameter_robustness_cost.png` | costi congelati vs refit |
| `parameter_robustness_latency.png` | latenza 0/1/2h, rendimento e DD MTM |
| `parameter_robustness_threshold_surface.png` | superficie soglia training/OOS |
| `parameter_robustness_intervals.png` | CI bootstrap scenari chiave |

La dashboard mostra esplicitamente i due avvisi: **superficie OOS non promuovibile** e **carry SHORT = ipotesi, non dati storici**.

### 36.2 Comandi

```bash
# analisi completa (read-only, rifiuta di sovrascrivere l'output)
python -m crypto_decision_lab.parameter_robustness

# solo stampa, senza scrittura
python -m crypto_decision_lab.parameter_robustness --no-write

# test dedicati
pytest tests/test_parameter_robustness.py tests/dashboard/test_parameter_robustness_dashboard.py -q
pytest -q
```

Tempi osservati: **~25 s** per l'analisi completa; suite completa **178 test** in ~52 s al termine di CP08-03 (**220** con CP08-04). Exit code del CLI: `0` se il gate tecnico è PASS, `2` altrimenti.

**Determinismo verificato:** una seconda esecuzione completa su directory di output temporanea riproduce **bit-per-bit** `scenario_metrics.parquet`, `threshold_surface.parquet`, `threshold_selection.parquet`, `fold_stability.parquet`, `concentration.json` e `checks.json` (SHA-256 identici). Solo `summary.json`/`manifest.json` cambiano, per il timestamp di generazione.

### 36.3 Test aggiunti (36)

| Test | Copertura |
|---|---|
| `test_preregistered_grid_contains_required_thresholds` | griglia ordinata, 5/8/10 presenti |
| `test_cost_scenarios_are_ordered_and_include_the_cp07_base` | costi ordinati, base esatta 0,10%/0,05% |
| `test_latency_scenarios_include_the_primary_one_hour` | 0/1/2h, 1h primaria |
| `test_vectorized_net_matches_audited_net_return` | uguaglianza con la formula auditata (1e−12) |
| `test_threshold_selection_picks_the_train_argmax_with_lowest_tie_break` | argmax + tie-break |
| `test_threshold_selection_uses_training_rows_only` | poisoning del test block, 0 leakage |
| `test_folds_are_chronological_and_purged` | cronologia e purge |
| `test_cost_sensitivity_is_monotone_on_the_same_ledger` | monotonia su ledger congelato |
| `test_cost_transform_keeps_the_same_actions_and_timestamps` | decisioni congelate |
| `test_latency_shifts_entry_and_exit_on_real_opens` | prezzi degli open ritardati |
| `test_latency_reports_missing_bars_instead_of_inventing_prices` | barre mancanti contate |
| `test_carry_fraction_matches_the_declared_hypothesis_formula` | formula carry |
| `test_carry_touches_short_trades_only_and_is_multiplicative` | solo SHORT, moltiplicativo |
| `test_evaluate_frame_reports_every_required_metric` | rendimento, PF, DD MTM, trade, CI |
| `test_concentration_leave_top_k_is_monotone_and_additive` | leave-top-k |
| `test_threshold_surface_is_exploratory_and_scoped` | scope e flag esplorativo |
| 11 test di regressione sull'archivio reale | 106 trade, latenze CP08-01, monotonia, refit, CP07 grid (delta 0), superficie, carry, fold CP08-02, concentrazione, gate, scrittura additiva/no-overwrite |
| 9 test dashboard | adapter, file mancanti, gate rotto, 4 figure, frame vuoti, render del tab reale senza eccezioni |

---

## 37. Gate CP08-03

| Gate | Esito |
|---|---|
| Simulatore auditato riusato, nessuna formula privata | **PASS** |
| Policy CP07 congelata riprodotta bit-per-bit | **PASS** (106 trade, +39,175764%, PF 1,3295465, DD MTM −27,202367%) |
| Griglia pre-registrata con 5%/8%/10% | **PASS** |
| Selezione soglia solo sul training | **PASS** (poisoning 5/5 fold, 0 differenze) |
| Superficie OOS solo esplorativa, nessuna promozione | **PASS** |
| Esperimento A costi con decisioni congelate | **PASS** (monotonia stretta, 8/8 step) |
| Esperimento B refit separato e confondimento documentato | **PASS + WARN** (non monotono, come atteso) |
| Griglia costi CP07 riprodotta | **PASS** (delta 0,0) |
| Latenza 0h/1h/2h, 1h principale, 0 barre mancanti | **PASS** |
| Carry SHORT come ipotesi, solo SHORT | **PASS** (+1 ipotesi dichiarata) |
| Rendimento, PF monetario, DD MTM, trade, CI per ogni scenario | **PASS** (28/28) |
| Concentrazione e stabilità per fold | **PASS** (5/5 fold) |
| Nessun training / inferenza / download / modifica archivi | **PASS** |
| Metriche riconciliate con CP07/CP08-01/CP08-02 | **PASS** |

**Gate tecnico: PASS** (12 PASS, 1 WARN, 0 FAIL).  
**Gate strategico: INCONCLUSIVE.**  
**Promozione al trading: NO-GO.** Un PASS tecnico certifica riproducibilità, assenza di leakage e riconciliazione delle metriche: **non** è una prova di edge e **non** è un GO.

---

## 38. Come leggere CP08-03 insieme a CP08-01/02

| Domanda | Risposta CP08-03 |
|---|---|
| Il risultato +39,18% sopravvive ai costi? | Sì fino a ~0,25% per lato; a 0,30% è +1,26%; a 0,50% è −33,73%. |
| La soglia 5% è un ottimo robusto? | No: superficie irregolare, massimo OOS instabile, massimo di training non predittivo. |
| Un refit train-only della soglia migliora l'OOS? | No: 9% in 5/5 fold, +35,19% contro +39,18% del 5% congelato. |
| L'esecuzione istantanea è necessaria? | Sì: 1h di latenza costa 15,59 pp e peggiora il DD MTM di 3,56 pp. |
| Il risultato regge con carry SHORT realistico? | Ipotesi neutrali/stressate: −3,4 / −9,2 pp; envelope conservativo: +3,79% con P(>0)=51,8%. |
| Il profitto è diversificato? | No: nel 1h senza i top-3 trade il rendimento è negativo; il top-1 vale il 41% del netto. |
| Questo è un GO? | **No.** INCONCLUSIVE / NO-GO. |

I sei requisiti di validazione indipendente di §11 restano **aperti**: CP08-03 soddisfa solo il primo in forma parziale (la griglia è pre-registrata *per questo step*, ma i dati OOS erano già noti) e documenta i punti 3 (costi/latenza come scenari) e 6 (concentrazione accanto al rendimento). Mancano ancora dati OOS genuinamente nuovi, funding/borrow storici, un walk-forward più fine e la replica su un secondo simbolo/mercato. Finché mancano, il Momentum Threshold resta **INCONCLUSIVE** e Intern-Decision-0.8B **non va addestrato**.

> **Aggiornamento CP08-04.** Il protocollo di validazione indipendente [`S02-CP08-independent-validation-protocol.md`](S02-CP08-independent-validation-protocol.md) formalizza i requisiti §11 in un documento immutabile e pre-registrato: benchmark congelato, registro dei dati di ricerca (inclusa la coda osservata 2026-10-23 00:00–16:00Z), scenario primario latenza 1h + 0,20%/lato + carry SHORT ipotetico, griglia chiusa, walk-forward espandente, criteri GO/NO-GO e controlli anti-riuso. Non esegue alcun backtest e non dichiara la validazione: lo stato resta **`NOT_EVALUATED` / NO-GO** finché non esistono dati realmente nuovi.
