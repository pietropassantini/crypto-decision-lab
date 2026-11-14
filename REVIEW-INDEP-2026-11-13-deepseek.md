# Revisione indipendente — 2026-11-13 (DeepSeek v4 Pro)

**Revisore:** DeepSeek v4 Pro (provider DeepSeek, agente pi, sessione `01a1d6bf-ee65-7112-963a-b71aab145289`). Modello diverso sia dall'autore S04–S09 e delle correzioni F1–F4/R1–R3 (Claude Opus 5.5) sia dai revisori precedenti (Codex GPT-6, GLM-5.3): la ripetizione delle prove determinanti è indipendente per il §5.

**Commit di base:** `53f15efd2f4e3a0020313dea08049d7c6cfa9741` (branch `prd/integration`). Ho inoltre riveduto il **working tree** corrente = `53f15ef` + le correzioni R1–R3 non committate (`live.py`, `exchange.py`, `binance_spot.py`, `tests/test_live.py`; +430/−36 righe).

## Vincoli rispettati

- `data/forward/` mai letto né materializzato: clone con sparse-checkout `!/data/forward/` (18 file tracciati presenti nell'index ma assenti dal working tree, `ls data/forward` → assente).
- Nessuna credenziale, nessuna chiamata di rete, `scripts/s07_testnet_smoke.py` non eseguito (richiede chiavi dell'operatore).
- Nessun commit, push o modifica a codice/backlog. Lavoro in `var/review-indep/deepseek/` (git-ignorata).
- Stati del backlog non modificati: solo raccomandazioni.

## Metodo

Due checkout: `clone` (53f15ef, stato committato) e `clone-wt` (53f15ef + file R1–R3 copiati dal working tree). Venv nuovo con `--system-site-packages` (come le review precedenti: dipendenze condivise, non ricostruite da zero), `pip install -e . --no-deps --no-build-isolation`, pacchetto importato dal checkout in esame (verificato con `crypto_decision_lab.__file__`). Probe propri scritti da zero in `var/review-indep/deepseek/probe.py` (mai eseguiti quelli di Codex/GLM).

## Prove ripetute

| # | Prova | Comando | Atteso | Osservato | Esito |
|---|---|---|---|---|---|
| 1 | Suite completa su clone pulito 53f15ef | `pytest -q -p no:cacheprovider` | 480 passed | **480 passed in 259.69s**, exit 0 | PASS |
| 2 | Suite completa sul working tree (R1–R3) | `pytest -q -p no:cacheprovider` in `clone-wt` | 488 passed | **488 passed in 263.03s**, exit 0 | PASS |
| 3 | CP06.1 ripresa dopo interruzione | `pytest tests/test_paper.py -k "cp061 or stale_version or replay_session" tests/test_simulation.py -k s06` | verde | 4 passed | PASS |
| 4 | CP06.2 ciclo di vita feed laboratorio | `pytest tests/test_paper.py -k "cp062 or revised or cp08" tests/dashboard/test_strategy_studio.py -k cp062` | verde | 3 passed | PASS |
| 5 | CP06.3 pin modello/motore | `pytest tests/test_paper.py -k cp063` | verde | 2 passed | PASS |
| 6 | CP07.1 invio/rifiuto/parziale/annullamento | `pytest tests/test_live.py -k "cp071 or adapter or signature or end_to_end"` | verde | 16 passed | PASS |
| 7 | CP07.2 timeout e riavvio senza duplicati | `pytest tests/test_live.py -k cp072` | verde | 6 passed | PASS |
| 8 | CP07.3 guardie e mainnet | `pytest tests/test_live.py -k "cp073 or mainnet or pins"` | verde | 7 passed | PASS |
| 9 | CP08.1 drill runbook | `pytest tests/test_release.py -k cp081` | verde | 1 passed (103s) | PASS |
| 10 | CP08.2 scheda/criteri | `pytest tests/test_release.py -k "cp082 or criteria or backtest"` | verde | 4 passed | PASS |
| 11 | CP08.3 decisione/gate | `pytest tests/test_release.py -k "cp083 or activation or approval or mainnet"` | verde | 5 passed | PASS |
| 12 | CP09.1 report riconciliato | `pytest tests/test_ops_report.py -k "cp091 or totals or clean or paper or cli"` | verde | 5 passed | PASS |
| 13 | CP09.2 esperimento preregistrato | `pytest tests/test_ops_report.py -k cp092` | verde | 1 passed | PASS |
| 14 | Regressioni F1–F4 (committate) | `pytest tests/test_live.py tests/test_paper.py -k review` | 6 passed | 6 passed | PASS |
| 15 | Regressioni R1–R3 (working tree) | `pytest tests/test_live.py -k review_r` | 8 passed | 8 passed | PASS |
| 16 | S03: ledger CLI byte-identico | `python -m crypto_decision_lab.simulation --source BTCUSDT-1h-raw-v1 --config config/simulation.example.json --output-dir sim-a` poi `sim-b` (×2) | identiche | sha256 `a9d732eecbd4f2bff035d6c488e20b59fe528143c0d9189333bb3735f763b5ea` entrambe, `cmp` byte-identico; `HALTED_GAP`, cash `9834.02390961377` (uguale ai riferimenti delle review precedenti) | PASS |
| 17 | CP09.1 report riproducibile | rigenerazione `ops_report` dal registro `var/release/drill-btcusdt-momentum-v1/live/sessions/drill-c-4345b936` con l'etichetta originale | `content_sha256` = `0fae4fc9…` | `0fae4fc93e7a52eebbee0f77868ca5a0af5fa0dc70dda651dd949f94e0d80540`, identico al riferimento `reports/s09/drill-c-btcusdt-momentum-v1/report.json` | PASS |
| 18 | Probe propri F1–F4 + R1–R3 | `var/review-indep/deepseek/probe.py` (36 probe) | tutti attesi | **36/36 PASS** | PASS |

Nota sulla prova 17: una prima rigenerazione con `--label "drill-c"` ha prodotto un `content_sha256` diverso (`0f7ff810…`); il confronto ha mostrato che l'hash copre `content.data_label` e che l'etichetta originale è la stringa lunga "fixture-live etichettata: …"; rigenerando con quella esatta l'hash torna `0fae4fc9…`. Non è un difetto: la riproducibilità richiede gli stessi argomenti (il `generated_utc` è invece escluso dall'hash).

## Verdetto F1–F4 (correzioni committate in 53f15ef, autore Claude Opus 5.5)

| ID | Verdetto | Evidenza (probe propri + test) |
|---|---|---|
| F1 | **Confermata** nel percorso sequenziale | Probe: dopo `session-finish` tutti i comandi operatore (suspend/resume/cancel/flatten/rebaseline) rifiutati con `LiveError`; `poll` resta `COMPLETED` e il contatore `submit` della fake non aumenta. Il percorso *concorrente* è coperto da R3 (sotto) |
| F2 | **Confermata** | Probe: con `ENGINE_MISMATCH` suspend/resume lasciano lo stato invariato; flatten/cancel/rebaseline/finish/resolve rifiutati; nessun ordine inviato (`orders=0, submits=0`); `accept-code-revision` su sessione fixture ripristina `RUNNING`. Il varco via `accept-code-revision` su capitale reale è chiuso da R1 |
| F3 | **Confermata** | Probe: ordine LIMIT `NEW` rimosso dalla fake → `MISSING` + `UNRESOLVED` entro la tolleranza → `HALTED` oltre la tolleranza ("open orders no longer known by the exchange") → `resolve-missing-order` → `LOST` → `rebaseline` → `WAITING` |
| F4 | **Confermata** | Probe: sessione paper replay `COMPLETED` rifiuta suspend/resume/accept_engine_revision e resta `COMPLETED`; `resume` ricontrolla i pin (motore cambiato → `ENGINE_MISMATCH`, non accettazione silenziosa) |

## Verdetto R1–R3 (correzioni non committate nel working tree)

| ID | Verdetto | Evidenza |
|---|---|---|
| R1 | **Confermata** | Probe: sessione mainnet (`real_capital()` = True) in `ENGINE_MISMATCH` → `accept_code_revision` senza `--approval` rifiutato ("real-capital session; a code change needs a new S08 release card"); con approval `{}` rifiutato da `verify_activation` ("unsupported approval schema"); il pin resta invariato e lo stato `ENGINE_MISMATCH`. Sessione testnet/fixture accetta senza approval (comportamento preservato). Il gate usa la `verify_activation` reale di `release.py` (schema, `card_sha256`, decisione READY, `live_config_sha256`, exchange/ambiente/simbolo, `code_sha256`) |
| R2 | **Confermata** | Probe: `hide_fills` → ordine terminale con `fills=0` ma `executedQty>0` diventa `FILLS_PENDING` (non `FILLED`), resta riconciliabile, stato `UNRESOLVED` senza `halted`; `release_fills` → `FILLED` con 1 fill e ritorno `WAITING`; oltre la tolleranza `HALTED` con messaggio "trades are still incomplete", risolto con `resolve-missing-order` (`FILLS_INCOMPLETE`) + `rebaseline`. `binance_spot.trades` pagina `myTrades` con `fromId` (test `test_review_r2_adapter_paginates_my_trades` verde) |
| R3 | **Confermata** | Probe: due `LiveSession` sulla stessa directory; durante `poll` un `finish`/`poll`/`suspend` dalla seconda connessione riceve `SessionBusy` (mai intercalato); nessun record `end` intercalato, l'ordine del runner è l'unico inviato. Il test `test_review_r3_lock_is_released_when_the_holder_dies` (subprocess con `os._exit(97)`) conferma che l'OS rilascia il lock alla morte del processo |

## Rilievi nuovi (miei)

I precedenti revisori hanno già usato R1–R5; uso R6+ per i miei.

| ID | Sev. | Step | Rilievo | Riproduzione |
|---|---|---|---|---|
| R6 | LOW | S07 | `binance_spot.trades` pagina `myTrades` con `fromId = max(id)+1`, che presuppone ordine **crescente** dei `tradeId` nella pagina. La documentazione Binance citata ("Default gets most recent trades") non chiarisce la direzione quando si usa `fromId`; se l'API restituisse in ordine decrescente, per un ordine con >1000 fill i più vecchi non verrebbero mai letti. Blast radius contenuto: il `book()` deriverebbe una posizione errata e `balance_mismatch()` porterebbe a `HALTED`, non a perdita silenziosa. Da confermare sul testnet reale prima di capitale reale (già indicato come aperto dall'autore in S07-HANDOFF). | Statico + test con transport sintetico crescente; nessuna verifica sull'API reale (offline) |
| R7 | INFO | S07 | Il lock `session.lock` (msvcrt/fcntl) serializza solo processi sulla **stessa macchina**. Se runner e comandi operatore girassero su host diversi su filesystem condiviso, il lock non coordinerebbe. Nel perimetro attuale (operatore e runner sulla stessa macchina) non è un difetto. | Lettura di `_lock_file`/`_SessionLock` |
| R8 | LOW | S07 | `_apply_report` gestisce `executed < executedQty` (→ `FILLS_PENDING`) ma non il caso speculare `executed > executedQty` (fill in eccesso). In quel caso l'ordine resterebbe `FILLED` con fill importati in eccesso: la posizione/cassa slitterebbero e verrebbero fermate solo da `balance_mismatch` → `HALTED`. Caso molto remoto (incoerenza lato exchange), ma l'asimmetria è da annotare. | `live.py` `_apply_report` (riga ~563) |

## Verdetto F5–F10 (rilievi aperti di REVIEW-2026-11-13.md)

| ID | Verdetto | Evidenza |
|---|---|---|
| F5 | Confermato LOW | `ops_report.py:190–192` stima la quantità richiesta di un MARKET `quoteOrderQty` come `quote_quantity / reference_open`: su fill reali con slippage il `fill_ratio` può essere < 1 anche su ordini `FILLED`. File invariato nel working tree |
| F6 | Confermato LOW (statico) | `build_adapter`/`open_feed` risolvono i percorsi relativi rispetto a `repo_root`; `FakeExchange` crea uno stato nuovo se il file manca. Solo fixture |
| F7 | Confermato LOW | `git status --porcelain` espone ancora `bash.exe.stackdump`, `data/reports/`, `data/manifests/`, `data/diagnostics/`, `docs/screenshots/` untracked e non ignorati da `.gitignore` |
| F8 | Confermato (record storico) | Il `engine_sha256` nel report S05 è precedente al refactor S06; gli esiti coincidono, è un artefatto storico non rigenerato |
| F9 | Confermato INFO | `report.json` e `forecast-windows.parquet` dello snapshot restano non tracciati; decisione di packaging aperta |
| F10 | Confermato INFO | Il CLI di simulazione sullo snapshot completo termina `HALTED_GAP` alla prima ora mancante (osservato nella prova 16, cash 9834,02); comportamento previsto, il README dell'esempio non lo dichiara |

## Raccomandazioni al coordinatore (S00A–S09)

| Step | Raccomandazione | Motivo |
|---|---|---|
| S00A | resta IN_REVIEW | F7/F9 aperti; CP00A.4 non ripetibile da me senza leggere `data/forward/` (vincolo di perimetro) |
| S01 | resta IN_REVIEW | CP01.1–3 verdi nella suite del clone pulito, ma autore non registrato nell'handoff e dipendenza S00A aperta |
| S02 | resta IN_REVIEW | Suite verde; build doppia CP02.1 non ripetuta in questa sessione; dipende da S00A |
| S03 | **VERIFIED** | CP03.1–3 verdi; ledger CLI byte-identico su due esecuzioni e uguale al riferimento pre-refactor `a9d732ee…`; revisore (DeepSeek) diverso da autore (GPT-6) e dal refactor (Claude) |
| S04 | resta IN_REVIEW | Solo prove automatiche (CP04.1–3 verdi); la prova manuale dell'app autonoma resta da eseguire |
| S05 | resta IN_REVIEW | CP05.1–3 verdi; esperimento completo non rigenerato in questa sessione; F8 documentato |
| S06 | **VERIFIED** | CP06.1–3 e F4 confermati con probe propri; il refactor Engine non ha alterato il CLI S03 |
| S07 | resta IN_REVIEW | R1–R3 confermati (parte codice chiusa); resta il collaudo reale `scripts/s07_testnet_smoke.py` con chiavi dell'operatore, inclusa la conferma della paginazione `orderId+fromId` di `myTrades` (R6) |
| S08 | resta IN_REVIEW | R1 confermato sul percorso post-creazione; scheda `fd1153cd` BLOCKED e decisione NOT_ACTIVATED coerenti; dipendenze S01/S05/S07 aperte; nessuna candidata READY |
| S09 | **VERIFIED** (nei limiti dichiarati) | `content_sha256` identico rigenerando dal registro drill-c; totali/riconciliazione coperti dai test; F5 LOW resta aperto; valido per fixture/paper, nessuna esecuzione live reale |

Nessuna dichiarazione di readiness live: la candidata attuale è BLOCKED e NOT_ACTIVATED, correttamente.

## Limiti di questa review

- Venv con `--system-site-packages`: dipendenze non ricostruite da zero (procedura identica alle review precedenti); il pacchetto in esame è però installato editable dal checkout al commit esatto e importato da lì.
- Nessun accesso alla rete: la documentazione Binance è verificata staticamente (firma HMAC-SHA256 = vettore ufficiale coperto da `test_signature_matches_the_official_example`; endpoint/errori letti in `binance_spot.py`); non ripetuta sulla documentazione live.
- `data/forward/` non aperto (vincolo): l'isolamento CP08 è verificato solo dai test della suite.
- Drill S08 tramite wheel installato non rieseguito: verificata l'evidenza registrata (`drill.json`) e la copertura `test_release.py::test_cp081`.

## Artefatti creati (nessuno committato)

- Questo report: `REVIEW-INDEP-2026-11-13-deepseek.md`.
- `var/review-indep/deepseek/`: `clone/` e `clone-wt/` (sparse, `data/forward` escluso), `venv/`, `pytest-clean.log` (480), `pytest-wt.log` (488), `sim-a/`/`sim-b/` (ledger S03), `ops-regen/`/`ops-regen2/` (report drill-c rigenerato), `probe.py` + `probe-data/` (36/36), log vari.
