# Revisione indipendente — 2026-11-15 (GLM-5.3, commit 73e7277)

**Revisore:** GLM-5.3 (provider Z.ai), agente pi, sessione `01a1df74-6e48-7433-a743-ef557ad8aa93`.
**Base:** commit `73e7277` su `main` (il prompt del revisore è `a64b49e`, che aggiunge solo `docs/REVIEWER-PROMPT-2026-11-15.md`). Clone pulito in `var/review-indep/clone-e05`, venv nuovo Python 3.14.0, `pip install -e ".[dashboard,dev]"`.
**Indipendenza (PRD §5):** autori del codice rivisto = Claude Opus 5.5 (E01, E03), Claude Fable 5.2 (E04, E05, protocollo condiviso), Claude Sonnet 5.5 (paper runner, public_klines, session_fidelity, prompt). Questo revisore è un modello diverso dagli autori, quindi la ripetizione delle prove determinanti è indipendente rispetto a chi ha scritto il codice. **Trasparenza (come richiesto dal coordinatore):** GLM-5.3 è lo stesso modello che ha firmato le review del 13-11 (`REVIEW-INDEP-2026-11-13-glm.md`) e del 14-11 (`REVIEW-INDEP-2026-11-14-glm.md`, `REVIEW-INDEP-2026-11-14-glm2.md`). L'indipendenza *modello-vs-autore* sussiste; quella *modello-vs-revisore precedente* no: occhi nuovi rispetto alle review passate, non un modello nuovo. Il coordinatore lo pesi in fase di accettazione (è la stessa situazione dichiarata per le review del 14-11).

## Vincoli rispettati

- `data/forward/` mai letto né modificato. Esperimenti rieseguiti sul solo snapshot di ricerca `BTCUSDT-1h-raw-v1` (ruolo `research` nel catalogo, ammesso da `assert_research_admissible`); output in `var/review-indep/e0X-run-{a,b}` (ignorati da Git).
- Nessuna credenziale, nessuna chiamata di rete: `public_klines` è stato verificato solo con il client HTTP finto dei test e per lettura codice/documentazione; il runner non è mai stato eseguito contro il mercato reale.
- Nessun commit, push o modifica a codice e backlog. Questo report non è committato.
- Collaudo S07 **non eseguito** (compito dell'operatore). R13, R14 (INFO) restano aperti come da review del 14-11.

## 1. Tabella delle prove

| # | Comando / verifica | Atteso | Osservato | Esito |
|---|---|---|---|---|
| 1 | `pytest -q -p no:cacheprovider` (clone, venv nuovo) | 597 passed | 597 passed (428 s) | PASS |
| 2 | sha256 (LF) delle 4 specifiche vs righe backlog E01/E03/E04/E05 | d5527a6a…, 41ddad98…, 9aecd5c0…, 7054becb… | tutte uguali; `status: PREREGISTERED` anche al commit di preregistrazione (94cd1d7/4476553/f11dfe3/3b7a42c) e a HEAD | PASS |
| 3 | E01 `turnover_experiment develop` → `evaluate` → `all --output-dir` | g11, REJECTED, 17 `ledger_sha256` identici | g11; REJECTED in run A e B; 17 digests identici tra committato/run A/run B | PASS |
| 4 | E03 `regime_experiment` (come sopra) | g09, REJECTED, 23 digests | g09; REJECTED; 23 digests identici | PASS |
| 5 | E04 `reversion_experiment` (come sopra) | g05, REJECTED, 20 digests | g05; REJECTED; 20 digests identici | PASS |
| 6 | E05 `daily_experiment` (come sopra) | g10, CANDIDATE, 20 digests | g10; CANDIDATE; 20 digesti identici; `selection.json` di run B differisce solo in `locked_utc` (timestamp, atteso) | PASS |
| 7 | `reports/eNN/selection.json` al commit di blocco vs HEAD | identici byte per byte | identici per E01/E03/E04/E05; `selection_sha256` nel report corrisponde | PASS |
| 8 | Ordine dei commit (§3 sotto) | spec PREREGISTERED → selection committata → report | nessuna violazione | PASS |
| 9 | Probe di causalità delle feature su frame sintetico (var/review-indep/probe_leakage.py) | invarianza a barra corrente/future, sensibilità al passato | E05 `daily_features`, E03 `trend_features`, E04 `zscore_features`/`reversion_features`: tutte PASS; giorno senza barra 23:00 → feature indefinite (fail closed) | PASS |
| 10 | E05 su dati reali: ricalcolo indipendente di `d_momentum`/`d_trend` dal frame ammesso vs registro | coincidenza | 1.999 decisioni con feature ricondotte, 0 discrepanze (>5e-7); 102 ordini su tre finestre, **tutti alle 00:00 UTC** | PASS |
| 11 | Ricalcolo manuale metriche dal ledger (walk indipendente di cash/quantità/equity) | coincidenza col report | tutti i 15.312 record `balance` della conferma E05 riprodotti (0 differenze >1e-9); E05: 28,19/70,87/4,98%, costi 12,8/17,6/12,8, lordo 153,8/371,9/37,7, 16/17/17 trade; E01 val. −20,28% (206 trade, lordo 36,1, costi 137,5); E03 val. −4,65%; E04 val. −7,21% | PASS |
| 12 | Concentrazione E05 (handoff: conferma +24,9 / best +48,8 / senza −23,9; val. senza 2 best +44,9; svil. +140,9→+28,6) | conferma cifre | confermate **con una precisazione (R16)**: il "+48,8" è la posizione aperta a fine finestra (aperta 2026-08-20) valutata mark-to-market, non un trade chiuso; i trade chiusi in conferma realizzano −23,9 | PASS (con rilievo) |
| 13 | Selezione E05 dal solo sviluppo, minimi 12/8 | g11 (11 trade) escluso; g10 massimo tra gli ammissibili | confermato: 11 configurazioni su 12 con ≥12 trade; g10 netto massimo (+28,19%); criterio `closed_trades >= 8` sostituito come da specifica | PASS |
| 14 | Criteri nel codice vs testo specifica | coincidono letteralmente | `TURNOVER_RATIO_MAX = 0.30`, `COSTS_SHARE_MAX = 0.50`; testi dei criteri identici; `_select`/`_validation_criteria`/`_decide` verificati | PASS |
| 15 | Finestre sviluppo/validazione/conferma | contigue, non sovrapposte | eventi: dev 2021-02-01 00:00 → 2023-06-30 23:00; val 2023-07-01 00:00 → 2024-12-31 23:00; conf 2025-01-01 00:00 → 2026-09-30 23:00 (`[start, end)`); inoltre `load_spec` rifiuta finestre sovrapposte | PASS |
| 16 | `tests/test_paper.py` + `tests/test_paper_daily.py` | verdi | 28 passed | PASS |
| 17 | session_id sessioni senza features | invariato | formula pre-modifica e attuale danno lo stesso id (verifica diretta su digest); test: `features` assente dai meta | PASS |
| 18 | Manomissione `session_fidelity` (probe_fidelity.py + verifiche mirate) | ogni alterazione rilevata | lato ordine (BUY→SELL), equity di un `balance`, fee di un `execution`, open di una barra (`record`, o `raw`+`record`), `prev_close` visibile (1e-9), cancellazione evento: **tutte rilevate**; non rilevate e per design: nota dei gap e `prev_close` a 1e-16 (sotto la tolleranza dichiarata di 12 cifre significative) | PASS |
| 19 | `public_klines` vs documentazione Binance Spot | endpoint/parametri corretti | vedi §6 | PASS (con note) |
| 20 | Manifest degli artefatti committati | nessuna discrepanza | nessuna per E01/E03/E04/E05 (i ledger citati restano volutamente fuori Git; i loro digest sono riprodotti dalle mie run) | PASS |

## 2. Ripetizione degli esperimenti

Tutti e quattro gli esperimenti sono stati rieseguiti da clone pulito nelle due modalità (due fasi e fase unica) con esiti e digest identici ai report committati (prove 3-6). `preregistration.json` committati contengono la specifica con lo stesso sha256 e status `PREREGISTERED`; la specifica non è mai cambiata dopo la preregistrazione.

## 3. Ordine di preregistrazione (git log)

| Esperimento | Preregistrazione | Selezione committata | Esito committato | Verifica tempi |
|---|---|---|---|---|
| E01 | spec PREREGISTERED già al commit `7101740` del 13-11; runner `94cd1d7` (14-11 22:35, solo src+tests) | `ca74636` (22:37) | `c1c87a0` (22:40) | `locked_utc` 21:37:17Z precede il commit di selezione; `evaluated_utc` 21:37:51Z segue il lock e precede il commit di esito |
| E03 | `f8f7490` (strategia+DRAFT) → `4476553` (12:06, diff solo `status`/`registered`) | `afc21f4` (12:09) | `e318fc3` (12:13) | coerente (11:09:17Z / 11:10:23Z) |
| E04 | `ecea601` → `f11dfe3` (12:51) | `b32db6a` (12:53) | `69a39f5` (12:57) | coerente |
| E05 | `8e1d2c9` → `3b7a42c` (13:13) | `32d137b` (13:16) | `1ef1a1e` (13:21) | coerente (12:16:26Z / 12:17:22Z) |

Nessuna violazione: in ogni esperimento la specifica è preregistrata prima di ogni lettura di dati (`develop` scrive `preregistration.json` *prima* di costruire la sessione dati, verificato in `experiment_protocol.develop` e `turnover_experiment.develop`), `selection.json` è nel repository prima della fase `evaluate` (che oltretutto rifiuta digest di specifica o engine diversi), e il report arriva dopo. I diff DRAFT→PREREGISTERED toccano solo `status` e `registered`.

## 4. Data leakage ed errori di protocollo

- **Causalità delle feature** (prove 9-10): le quattro famiglie (`momentum_24h` con `shift(1)`, `trend_ratio` con `shift(1)`, `zscore` con `shift(1)`, feature giornaliere su chiusura delle 23:00 indicizzata alle 00:00 successive) sono invarianti ad alterazioni della barra corrente e delle barre future e sensibili alle barre passate. Nessun confronto attraverso i gap: barra mancante → feature assente → `FLAT` (fail closed). Su dati reali, le feature di 1.999 decisioni di g10 ricalcolate dal frame coincidono col registro.
- **Esecuzione**: decisione alle 00:00 con `latency_hours=0` → fill all'open della barra 00:00 (osservabile all'istante) con slippage incorporato nel prezzo di esecuzione; nessun uso della barra in corso.
- **Ordini E05**: 102 su 102 alle 00:00 UTC (chiusure di finestra incluse, che cadono a 00:00).
- **Selezione e criteri** (prove 13-14): il minimo trade è applicato come dichiarato (12 in selezione, 8 in validazione per E05; 30/15 per E01/E03/E04 via default del protocollo condiviso); i cinque criteri di validazione e i due di conferma coincidono col testo delle specifiche.
- **Metriche ricalcolate a mano** (prove 11-12): vedasi tabella. Nota INFO R19 sul drawdown (±0,07 pp tra metodi equivalenti, nessun impatto sui criteri).
- **Isolamento forward**: i runner caricano solo dataset di ruolo `research` (`assert_research_admissible`); gli output passano da `guard_research_output`; `data/forward` mai toccato.

## 5. Robustezza del CANDIDATE E05 e giudizio sull'uso

Fatti confermati dai ledger (già in parte dichiarati nel handoff):

- Conferma: netto +24,9 su 500 = **−23,9 realizzati** (17 trade chiusi, migliore +19,6) **+ 48,8 non realizzati** su una posizione aperta il 2026-08-20 e valorizzata all'open dell'ultima barra. Il superamento della conferma dipende interamente da un plusvalore **non realizzato** di una singola posizione ancora aperta (R16).
- Validazione: +354,3 netto; senza i due migliori trade chiusi (+186,1 e +123,4) resta +44,9. Sviluppo: +140,9 → +28,6 senza i due migliori. Profilo tipico di trend following (35-50% di vincenti), ma fragile.
- 16-17 trade per finestra; quinto esperimento sugli stessi dati; la validazione era nota favorevole al long (buy and hold +103,2%).

**Giudizio:** l'esito CANDIDATE è proceduralmente corretto (criteri registrati, calcoli riprodotti) ma statisticamente debole, e la conferma — la prova dura — passa solo grazie a un mark-to-market di una posizione aperta. Questo **giustifica** la sessione paper su dati nuovi prevista dalla specifica: è esattamente il test di falsificazione a basso costo (candidato congelato, solo dati nuovi, stop a −20%) che questo profilo richiede. **Non giustifica** alcun passo S08 né capitale reale. Che cosa cambierei prima (non registrato, suggerimento): nella specifica E05-P1 il criterio sul vantaggio dovrebbe essere valutato/riportato anche senza la posizione aperta (v. R16 e R22-R23).

## 6. Review del codice nuovo

### 6.1 Protocollo condiviso (`experiment_protocol.py`) — favorevole

Separazione delle fasi imposta dal codice (directory write-once, `evaluate` richiede `selection.json` con lo stesso digest di specifica e engine e rifiuta di ricalcolare un report esistente), preregistratio­ne scritta prima del caricamento dati, criteri ripresi alla lettera da `turnover_experiment`, minimi per esperimento che sostituiscono solo il criterio `closed_trades`. `exit_reasons` è descrittivo. Verificato che `Adapter.selection_min_trades=None` mantiene i valori E01 (E04 invariato) e che `_validate` di E05 rifiuta specifiche con minimi diversi dal runner.

### 6.2 Modifiche a `paper.py` (S06, VERIFIED) — favorevoli, S06 resta VERIFIED

- **session_id/metadati invariati** per le strategie senza features (prova 17): la lista di parti dell'id riceve `[features]` solo se presenti.
- `_check_features`: `daily_trend` richiede esattamente `{momentum_days, sma_days}` interi positivi (`sma_days>=2`); le altre strategie rifiutano il parametro (test compresi).
- `_features`/`_history_close`: le feature giornaliere derivano dalla **storia registrata dalla sessione** (`daily_features_at`), con equivalenza verificata ai test (rel 1e-12, anche con barre mancanti) e confermata dalla mia prova 10 sul backtest.
- Blocco `try/except FeedError` in `poll`: `DATA_ERROR` non è bloccante → il poll successivo riprova; lo stato del motore non viene toccato (nessuna transazione aperta in quel punto). `snapshot()` non solleva più se la fonte è giù (`_feed_clock`). Test di outage a metà sessione verde.
- Regola "sessione sul mercato reale dall'ora corrente in poi": applicata alla creazione con l'ora del server; impedisce di rigiocare barre già pubblicate.
- Percorsi incoerenti cercati: il passo di evento è in transazione con rollback; `record.valid` falso → `DATA_ERROR` prima di qualunque scrittura; `_revisions` confronta l'`raw` (open, prev_close) di ogni barra già processata a ogni poll. Solo rilievo minore: `acknowledge_gap` chiama `source.records()` fuori da gestione errori → un `FeedError` arriva raw al chiamante (stato coerente, resta `PAUSED_GAP`): cosmetico (R26).

### 6.3 `public_klines.py` vs documentazione Binance Spot — favorevole (INFO R20)

- Host `data-api.binance.vision`: l'host pubblico documentato da Binance per i soli market data Spot (nessuna superficie account, nessuna chiave). Endpoint `/api/v3/klines` e `/api/v3/time` con parametri `symbol`, `interval=1h`, `startTime`/`endTime` (ms, open time, endTime inclusivo), `limit` ≤1000: tutti conformi; peso per pagina 2, ~4 richieste per poll: ampio margine ai limiti.
- Paginazione corretta: cursore = open time ultimo + 1h, `endTime = ora_attuale − 1ms` copre tutto il resto del range, quindi l'interruzione su pagina corta (<1000) è corretta; ordinamento/duplicati/range validati per riga.
- La barra in corso contribuisce solo con l'open (la sua close non è mai letta; `endTime` la esclude dal loop delle chiuse). Il rollover d'ora durante una chiamata è sicuro (il confronto `raw` non cambia tra poll).
- 418/429/5xx → `FeedError` con retry esponenziale (cap 12 s) poi fallimento pulito del poll (`DATA_ERROR`); ore mancanti semplicemente assenti (→ `PAUSED_GAP`/feature indefinite); righe malformite → `FeedError`.
- Revisioni retroattive di barre chiuse: durante la sessione sono rilevate da `_revisions` ad ogni poll (modifica di open o prev_close di una barra processata → `DATA_ERROR`). Rimane il limite R17 sulle barre di solo warmup.

### 6.4 `session_fidelity.py` — favorevole per lo scopo dichiarato (limite R18)

Il confronto ammette solo le due differenze dichiarate (feature a 12 cifre significative, nota dei gap). Le manomissioni di prova 18 sono tutte rilevate; le uniche non-rilevazioni sono esattamente le due escluse per design più una sotto-tolleranza a 1e-16. Limite intrinseco: lo strumento certifica la coerenza *interna* (registro vs barre registrate), non l'autenticità delle barre; una riscrittura coordinata di eventi+storia+registro (sessione interamente riforgiata) resterebbe invisibile — ed è fuori dal suo scopo (R18).

### 6.5 `live.py` (S07, solo informativo)

Il mirror opzionale su testnet calcola ancora solo `momentum_24h` (dichiarato in PRE-1): `daily_trend` senza feature giornaliere resterebbe flat — direzione fail-safe ma silenziosa. Da estendere (con test) solo se il mirror viene attivato.

## 7. Valutazione della bozza E05-P1 (CP-E05.3)

Punti di forza: candidato congelato e identico a g10; solo dati nuovi (start ≥ registrazione); warmup da klines pubblici con digest registrato; separazione onesta tra checkpoint operativo (12 settimane ≈ 2-3 trade: serve solo a provare il runner, non il vantaggio) e checkpoint sul vantaggio (8 trade o 52 settimane); stop a drawdown −20%; regola `INVALID` per feature da barre non chiuse; verifica di fedeltà; sezione minacce onesta. **Non è però pronta per la preregistrazione**: vedi R21-R25.

## 8. Rilievi

| ID | Severità | Step | Descrizione | Riproduzione minima |
|---|---|---|---|---|
| R16 | LOW | E05 | La formulazione del handoff "in conferma il netto dipende da un solo trade (+48,8)" è imprecisa: +48,8 è il mark-to-market **non realizzato** di una posizione ancora aperta (2026-08-20); i trade chiusi realizzano −23,9. La fragilità della conferma è quindi maggiore di come descritta (il segno del netto dipende dalla data di fine finestra). Non invalida l'esito registrato. | walk indipendente di `ledgers/confirmation-g10.json`: realizzato −23,93 + non realizzato +48,84 = +24,91 |
| R17 | LOW | paper.py / public_klines | Le righe di warmup entrano in `history` con `INSERT OR IGNORE`: una revisione retroattiva di una barra di solo warmup resterebbe congelata al primo valore letta. Coerenza interna preservata (le feature derivano dalla stessa storia), Binance non revisiona in pratica le barre chiuse; rilevante solo per una riconciliazione esterna. | `grep -n "INSERT OR IGNORE INTO history" src/crypto_decision_lab/paper.py`; `_revisions` copre solo la tabella `events` |
| R18 | LOW | session_fidelity | Lo strumento certifica coerenza registro-vs-barre-registrate, non l'autenticità dei dati; riscritture coordinate o modifiche sotto 1e-12 non sono (e non possono essere) rilevate. Da documentare nel criterio P1.week12. | probe 18: `prev_close * (1+1e-16)` → match resta true |
| R19 | INFO | E01-E05 | `max_drawdown_mtm_pct` calcolato sulla equity dei `balance` differisce da un ricalcolo su equity degli `market` di ±0,07 pp (E01 val −24,26 vs −24,21; E04 −13,40 vs −13,47): due stime mark-to-market entrambe legittime, nessun criterio vicino al confine cambia segno. | prova 11 + `max_drawdown_pct(equity_frame(...))` |
| R20 | INFO | public_klines | Anche gli errori 4xx vengono ritentati come transitori (innocuo, solo ritardo); un ban 418 non si smonta in 4 retry: fallimento pulito in `DATA_ERROR` e retry al poll successivo (orario): accettabile. | lettura `_get` in `public_klines.py` |
| R21 | MEDIUM | E05-P1 | **Deviazione dal percorso registrato in E05**: `acceptance.outcomes.CANDIDATE` di E05 (preregistrata) indica "Binance Spot **testnet** klines … S06/S07 runner with the operator's keys"; la bozza usa klines del **mercato reale** (pubblici, senza chiavi) con testnet solo mirror opzionale. La motivazione è sensata (i prezzi testnet non sono il mercato reale; i dati pubblici reali sono prova migliore; niente chiavi = meno rischio) ma la deviazione va registrata esplicitamente nell'`origin` della specifica e accettata dal coordinatore prima della preregistrazione. | confronto `E05-daily-trend-v1.json:acceptance.outcomes.CANDIDATE` vs `E05-P1-paper-forward-v1.json:data/execution` |
| R22 | MEDIUM | E05-P1 | Punto `edge` ambiguo: `INCONCLUSIVE` solo se "fewer than 5 closed trades after 52 weeks" ⇒ con 5-7 trade chiusi a 52 settimane i criteri `edge` possono dare `CONFIRMED` su 5-7 trade, contro l'intento dichiarato ("first of: 8 closed trades"). Rendere esplicito: criteri valutati solo a ≥8 trade chiusi oppure, a 52 settimane, esito `INCONCLUSIVE` se <8 (o dichiarare che 5-7 bastano). | lettura `criteria.outcomes.edge` |
| R23 | MEDIUM | E05-P1 | Il comparatore buy-and-hold è sotto-specificato: esposizione (0,5 su 500?), modello di costi (un solo round trip?), finestra (start sessione → data del checkpoint, entrambe a 00:00?), calcolato con la stessa engine (`constant` LONG). Senza questi dettagli il criterio "non inferiore a B&H meno 10 punti" è aggirabile in più modi. | lettura `criteria.edge[3]` |
| R24 | LOW | E05-P1 | Il criterio week-12 "decisioni processate entro 2h dal 00:00 nel 95% dei giorni" non dichiara la fonte della prova (orario bar vs orario wall-clock del poll: `events` memorizza l'ora della barra, `status_log.at` l'ora reale). Specificarla. | lettura `criteria.week12_operational[0]` |
| R25 | LOW | E05-P1 | Semantica dei restart dopo `OPERATIONAL_FAIL`: quale orologio riparte (12 settimane e 52 settimane "after start" della *nuova* sessione?) e quanti restart sono ammessi. Da esplicitare per evitare dilazioni indefinite del checkpoint sul vantaggio. | lettura `criteria.outcomes.week12.OPERATIONAL_FAIL` |
| R26 | INFO | paper.py | `acknowledge_gap` chiama `source.records()` fuori dalla gestione `FeedError`: con fonte giù solleva l'eccezione raw al chiamante invece di `DATA_ERROR` (lo stato resta coerente in `PAUSED_GAP`). Cosmetico, simile a R14. | lettura `paper.py: acknowledge_gap` |

Rilievi portati avanti dalle review precedenti: R13, R14 (INFO) e il collaudo S07 (operatore) restano aperti.

## 9. Verdetti

| Oggetto | Verdetto | Motivazione |
|---|---|---|
| E01 | **VERIFIED** (consiglio) | prove 2-3, 8, 11: REJECTED riprodotto due volte, 17 digests, criteri e metriche ricalcolati; backlog in ordine |
| E03 | **VERIFIED** (consiglio) | prove 2, 4, 8, 11: REJECTED riprodotto, 23 digests |
| E04 | **VERIFIED** (consiglio) | prove 2, 5, 8, 11: REJECTED riprodotto, 20 digests |
| E05 (CP-E05.0-2) | **VERIFIED** (consiglio) | prove 2, 6, 8-14: CANDIDATE riprodotto, 20 digests, nessun leakage, selezione e criteri corretti. CP-E05.3 resta **OPEN**: richiede la preregistrazione di E05-P1 con le modifiche R21-R25. L'esito può essere usato come base della sessione paper (è ciò che la specifica prevede), non per S08 |
| Protocollo condiviso (`experiment_protocol.py`) | **VERIFIED** (consiglio) | §6.1 + ripetizione E04/E05 che ne fanno uso |
| Modifiche a `paper.py` (S06) | **VERIFIED** resta | §6.2: session_id/metadati invariati per le strategie esistenti, 28 test verdi, nessuno stato incoerente trovato; rilievi R17, R26 |
| `public_klines.py` | **VERIFIED** (consiglio) | §6.3: conforme alla documentazione ufficiale per endpoint, parametri, paginazione, gestione 418/429/5xx, ore mancanti, barra in corso |
| `session_fidelity.py` | **VERIFIED** (consiglio) | §6.4 + prova 18: tutte le manomissioni realistiche rilevate; limite dichiarato R18 |
| E05-P1 (bozza) | **Non preregistrabile così** | servono prima R21, R22, R23 (e consigliabile R24, R25, più il legame con R16 per il criterio sul vantaggio) |

### Modifiche richieste a E05-P1 prima della preregistrazione

1. R21: registrare la deviazione dal percorso "testnet klines" scritto in E05 e farla approvare dal coordinatore.
2. R22: eliminare l'ambiguità sul numero minimo di trade chiusi per `CONFIRMED` (allineare a 8 o dichiarare 5-7 ammessi).
3. R23: specificare operativamente il buy-and-hold (esposizione, costi, finestra allineata a 00:00, engine `constant`).
4. Consigliati: R24 (fonte della prova di tempestività), R25 (orologi e limite dei restart), e per il criterio `edge` il riporto del netto con e senza la posizione aperta (collegato a R16).

## 10. Nota finale sull'indipendenza

Questa review è di GLM-5.3, stesso modello delle review 13-11 e 14-11 (dichiarato su richiesta del coordinatore, come da promemoria nel prompt). Le prove sono state scritte da zero in questa sessione (probe di leakage, probe di fedeltà con manomissione, ricalcolo manuale delle metriche) e ogni affermazione poggia su un comando eseguito o un file letto nel clone `73e7277`.
