# Revisione indipendente — 2026-11-13 (GLM-5.3)

**Revisore:** GLM-5.3 (provider Z.ai), agente pi, sessione `01a1d41e-5f0e-7112-963a-b718f4519190`.
**Base:** commit `53f15ef` su branch `prd/integration` (clone pulito in `var/review-indep/claude/clone`).
**Indipendenza (PRD §5):** autore di S04–S09 e delle correzioni F1–F4 = Claude Opus 5.5; autore del codice S03 originale = GPT-6 (Codex). Questo revisore è un modello diverso da entrambi, quindi la ripetizione delle prove determinanti è indipendente per tutti gli step. **Trasparenza:** nel repo era già presente `REVIEW-INDEP-2026-11-13.md` (Codex GPT-6): l'ho letto prima di scrivere i probe, quindi la direzione delle indagini R1–R3 non è nascente; le riproduzioni però sono mie (script scritto da zero, mai eseguito quello di Codex) e ogni esito è stato osservato direttamente. Per non sovrascrivere quel report questo file usa il suffisso `-glm`.

## Vincoli rispettati

- `data/forward/` mai letto né modificato: clone con sparse-checkout che lo esclude (18 file tracciati esclusi dal checkout, verificato con `git ls-files | grep -c ^data/forward` e `ls data/forward` → assente).
- Nessuna credenziale, nessuna chiamata di rete, `scripts/s07_testnet_smoke.py` non eseguito.
- Nessun commit, push o modifica a codice e backlog. Lavoro in `var/review-indep/claude/` (ignorata da git).
- Nessuna modifica agli stati del backlog: qui sotto solo raccomandazioni.

## Prove ripetute

| # | Prova | Comando | Atteso | Osservato | Esito |
|---|---|---|---|---|---|
| 1 | Suite completa su clone pulito | venv nuovo (`--system-site-packages`), `pip install -e . --no-deps --no-build-isolation`, `pytest -q -p no:cacheprovider` | 480 passed | **480 passed in 247.87 s**, exit 0, pacchetto importato dal clone | PASS |
| 2 | CP06.1 ripresa dopo interruzione | `pytest tests/test_paper.py -k "cp061 or stale_version or replay_session" tests/test_simulation.py -k s06` | verde | 4 passed | PASS |
| 3 | CP06.2 ciclo di vita feed di laboratorio | `pytest tests/test_paper.py -k "cp062 or revised or cp08" tests/dashboard/test_strategy_studio.py -k cp062` | verde | 3 passed | PASS |
| 4 | CP06.3 pin di modello e motore | `pytest tests/test_paper.py -k cp063` | verde | 2 passed | PASS |
| 5 | CP07.1 invio/rifiuto/parziale/annullamento + adattatore | `pytest tests/test_live.py -k "cp071 or adapter or signature or end_to_end"` | verde | 16 passed | PASS |
| 6 | CP07.2 timeout e riavvio senza duplicati | `pytest tests/test_live.py -k cp072` | verde | 6 passed | PASS |
| 7 | CP07.3 guardie e mainnet | `pytest tests/test_live.py -k "cp073 or mainnet or pins"` | verde | 7 passed | PASS |
| 8 | Regressioni F1–F4 | `pytest tests/test_live.py tests/test_paper.py -k review` | 6 passed | 6 passed | PASS |
| 9 | CP08 + CP09 suite | `pytest tests/test_release.py tests/test_ops_report.py` | verde | 16 passed in 122.40 s | PASS |
| 10 | S03: ledger CLI byte-identico | `python -m crypto_decision_lab.simulation --source BTCUSDT-1h-raw-v1 --config config/simulation.example.json --output-dir sim-a` poi `sim-b` (×2) | identiche tra loro | sha256 `a9d732ee…b5ea` entrambe, `cmp` byte-identico; **uguale anche al riferimento pre-refactor `var/fix-s03-bt/ledger.json`** (stesso hash) → il refactor Engine S06 ha lasciato invariato il CLI | PASS |
| 11 | CP09.1 report riproducibile | `python -m crypto_decision_lab.ops_report report --session-dir var/release/drill-btcusdt-momentum-v1/live/sessions/drill-c-4345b936 --label "…"` | content_sha256 = riferimento `0fae4fc9…` | `0fae4fc93e7a52ee…d80540` identico | PASS |
| 12 | CP08.2/3 scheda e decisioni | lettura `releases/btcusdt-momentum-v1/fd1153cd/{release-card.json,decisions.jsonl}` | BLOCKED motivata; NOT_ACTIVATED confermata dall'operatore | BLOCKED con 14/26 controlli falliti (backtest/paper net_return e drawdown, git non pulito, testnet assente, S00A–S07 non VERIFIED); `decisions.jsonl` append-only con 2 voci NOT_ACTIVATED: gate 17:24:43Z + operatore 17:35:54Z | PASS |
| 13 | F1–F4 + percorsi alternativi | `var/review-indep/claude/probe.py` (9 probe) | vedi rilievi | 9/9 con comportamento atteso (log in `probe.log`) | PASS |
| 14 | F5 fill_ratio ordini FILLED | analisi `reports/s09/drill-c…/report.json` | fino a 0,995 su FILLED | 3/6 ordini MARKET FILLED con fill_ratio 0,9945 / 0,9972 / 0,9987 | CONFERMATO |
| 15 | F7 output rigenerabili non ignorati | `git status --porcelain`, `git check-ignore` | non tracciati, non ignorati | `bash.exe.stackdump`, `data/reports/`, `data/diagnostics/`, `data/manifests/` untracked; check-ignore exit 1 | CONFERMATO |
| 16 | F8 engine_sha256 S05 storico | `engine_digests()` vs `data/experiments/S05-momentum-filter-v1/report.json` | diverso | attuale `b3c69252…` vs report `6ff68d76…` | CONFERMATO |
| 17 | F9 artefatti snapshot non tracciati | `git ls-files "data/snapshots/**/report.json" …` | assenti | nessun risultato | CONFERMATO |
| 18 | F10 esempio termina HALTED_GAP | prova 10 | HALTED_GAP alla prima ora mancante | `HALTED_GAP`, cash 9834.02390961377 (entrambe le run) | CONFERMATO |
| 19 | Gate mainnet offline | `BinanceSpotAdapter("mainnet")`; `LiveSession.create` binance-mainnet senza approval | rifiutati | PermissionError; LiveError "mainnet requires an approved READY release (S08)" | PASS |
| 20 | API Binance | statico + `test_signature_matches_the_official_example` | firma/endpoint/codici conformi | firma HMAC-SHA256 = vettore ufficiale (query LTCBTC, segreto e firma `c8db5682…` documentati); endpoint `POST/GET/DELETE /api/v3/order`, `origClientOrderId`, `myTrades?orderId=`, `openOrders`, `account`, `exchangeInfo`, `klines`, `time`; codici −2013/−2011/−2010/−1013/−1121, −1006/−1007 e 5xx come esito incerto — coerenti con la documentazione ufficiale citata in `docs/S07-live-adapter.md`. **Verifica sul web non possibile da questo ambiente (offline): dichiarato, non ripetuto sulla documentazione live** | PASS con limite |

Prove non eseguite, con motivo: collaudo testnet reale (richiede chiavi dell'operatore); build doppia del dataset S02, esperimento S05 e UI manuale S04 (fuori dal perimetro assegnato di questa review, già coperti dalla review del 2026-11-13); drill S08 tramite wheel installato (evidenza dell'autore verificata nei file `drill.json` 15/15 PASS e nei test CP08.1); CP00A.4 (richiederebbe leggere `data/forward/`, vietato).

## Rilievi

### R1 — HIGH — S07/S08: `accept-code-revision` sostituisce il pin del codice senza una nuova approvazione S08

`live.py:832` (`accept_code_revision`) aggiorna `code_sha256` e imposta `RUNNING` senza consultare `meta["activation"]` né richiamare `verify_activation`. Per una sessione mainnet il gate S08 è verificato solo in `create`; un cambio di codice successivo richiede, per il runbook stesso (`docs/S08-release.md`, Ripristino punto 5: "Un cambio di codice o di limiti richiede una **nuova scheda e una nuova approvazione**"), una nuova scheda READY e approvazione. Il metodo non ha alcuna condizione sull'ambiente.
Riproduzione (offline, `var/review-indep/claude/probe.py` → probe `r1`): sessione FakeExchange con `meta.activation` sintetico (`{"approval_sha256": "deadbeef", …}`) a simulare una sessione nata con gate S08; digest alterato in processo → `ENGINE_MISMATCH`; `accept_code_revision` → `RUNNING` con nuovo pin e activation invariato. Il valore sintetico dimostra che il metodo non lo consulta.
Correzione proposta: per sessioni mainnet (o sempre), `accept_code_revision` deve richiedere e verificare una nuova approvazione coerente con il nuovo digest.

### R2 — MEDIUM — S07: ordini terminali con fill incompleti non vengono mai recuperati

`live.py:423–458` (`_apply_report`) accetta un report terminale (es. `FILLED`) la cui somma dei fill differisce da `executedQty` e si limita ad annotare "fills pending". L'ordine esce da `unresolved_orders()` e non sarà più interrogato. `binance_spot.py:trades` chiama `myTrades?orderId=` una sola volta, senza paginazione `fromId` (limite predefinito 500): un ordine con molti fill perde fill per sempre. Nel caso reale la guardia saldi ferma la sessione (`HALTED`), ma i fill non vengono recuperati: il rebaseline li trasforma in una rettifica opaca e `ops_report` segnala il controllo "fills = executedQty" come KO.
Riproduzione (probe `r2`): report `FILLED` con `executedQty` 0,5 e fill vuoti applicato a un intento locale → stato `FILLED`, dettaglio "fills 0 differ from executedQty 0.5: fills pending", ordine non più tracciato tra quelli da riconciliare.
Correzione proposta: paginare `myTrades` e tenere riconciliabile ogni ordine con fill incompleti anche se terminale lato exchange.

### R3 — HIGH — S07: ordine inviato dopo un `session-finish` concorrente (F1 insufficiente come garanzia)

`poll()` verifica `COMPLETED`/`ENGINE_MISMATCH` solo all'ingresso; nel ciclo delle barre non ricontrolla lo stato prima di decidere e inviare. Un secondo processo (operatore) può chiudere la sessione tra l'ingresso del runner e l'invio: il runner prosegue e invia l'ordine. Con capitale reale significa trading su una sessione chiusa.
Riproduzione deterministica (probe `r3`, senza thread né rete): due `LiveSession` sulla stessa directory; al primo `records()` dentro `poll()` un hook completa la sessione dalla seconda connessione, poi restituisce le barre. Osservato: stato finale `COMPLETED` con record `end` seguito da `market, decision, risk, order, balance, execution, order_update`.
Correzione proposta: ricontrollare lo stato dentro la protezione transazionale prima del commit dell'intento e prima dell'invio (o serializzare runner e comandi con un lock di processo).

### R4 — INFO — S07: `parse_rules` usa `MARKET_LOT_SIZE` anche per gli ordini LIMIT

`binance_spot.py:parse_rules` sceglie `MARKET_LOT_SIZE` (se `stepSize` > 0) per step/min/max applicati poi a ogni tipo d'ordine. Su BTCUSDT coincide con `LOT_SIZE`; su altri simboli un LIMIT potrebbe subire un filtro `LOT_SIZE` diverso. Nessun effetto sul perimetro attuale.

### R5 — INFO — S07: ordini molto vecchi potrebbero non essere trovati da `GET /api/v3/order`

La documentazione Binance indica che gli ordini oltre la finestra di archivio (~3 mesi) possono non essere restituiti: verrebbero classificati `MISSING` e gestiti dal percorso operatore già previsto (F3). Per sessioni orarie attive il rischio è remoto.

## Verdetto sulle correzioni F1–F4 (autore: Claude Opus 5.5)

| ID | Verdetto | Evidenza |
|---|---|---|
| F1 | **Insufficiente** come garanzia assoluta | Percorso sequenziale chiuso (probe `f1`: tutti i comandi rifiutati su COMPLETED, stato sticky, nessun ordine dopo finish); ma R3 riproduce un ordine dopo un finish concorrente |
| F2 | **Insufficiente** come garanzia del gate S08 | `ENGINE_MISMATCH` sopravvive a suspend/resume e flatten/cancel/rebaseline/finish sono rifiutati fino ad accept (probe `f2`); ma R1 mostra che `accept-code-revision` sostituisce il pin senza nuova approvazione |
| F3 | **Confermata** nel percorso coperto | Probe `f3`: ordine aperto rimosso dall'exchange → `MISSING`, `UNRESOLVED` nella tolleranza, `HALTED` oltre ("open orders no longer known by the exchange"), `resolve-missing-order` → `LOST`, `rebaseline` → `WAITING`; 1 solo submit |
| F4 | **Confermata** | Probe `f4`: paper `COMPLETED` rifiuta suspend/accept/resume e poll resta COMPLETED; riapertura con motore diverso → `ENGINE_MISMATCH`, suspend → `SUSPENDED`, resume → `ENGINE_MISMATCH` di nuovo (pin ricontrollati) |

## Verdetto sui rilievi aperti F5–F10 (REVIEW-2026-11-13.md)

| ID | Verdetto | Evidenza |
|---|---|---|
| F5 | Confermato LOW | `ops_report.py` stima la quantità richiesta di un MARKET `quoteOrderQty` come quote/reference_open: drill-c mostra 3 ordini FILLED con fill_ratio 0,9945–0,9987. Dovrebbe confrontare il quote eseguito con quello richiesto |
| F6 | Confermato LOW (statico) | `FakeExchange.__init__` crea uno stato nuovo se il file manca; `build_adapter`/`open_feed` risolvono i percorsi relativi contro `repo_root` corrente. Solo fixture, nessun impatto runtime reale |
| F7 | Confermato LOW | Prova 15: output rigenerabili untracked e non ignorati da `.gitignore` |
| F8 | Confermato LOW | Prova 16: `6ff68d76…` storico contro `b3c69252…` attuale; esiti S05 identici, identità registrata superata |
| F9 | Confermato INFO | Prova 17: i due artefatti dello snapshot non sono in git; decisione di packaging aperta (la vista Forecast li richiede) |
| F10 | Confermato INFO | Prova 18: `HALTED_GAP` con cassa 9834,02: comportamento previsto, il README dell'esempio non lo dichiara |

## Raccomandazioni al coordinatore (per step S00A–S09)

| Step | Raccomandazione | Motivo |
|---|---|---|
| S00A | resta IN_REVIEW | F7 aperto e decisione F9 pendente; CP00A.4 non ripetibile da questo revisore senza accedere a `data/forward/` (vincolo di perimetro) |
| S01 | resta IN_REVIEW | CP01.1–3 verdi nel clone pulito, ma l'autore non è registrato nell'handoff e la dipendenza S00A è aperta |
| S02 | resta IN_REVIEW | Suite verde nel clone; la build doppia CP02.1 non è stata ripetuta in questa sessione (già confermata dalla review del 2026-11-13); dipende da S00A |
| S03 | **VERIFIED** (raccomandazione) | CP03.1–3 verdi; ledger CLI byte-identico su due esecuzioni **e uguale al riferimento pre-refactor** (`a9d732ee…`); refactor S06 (Claude) e codice originale (GPT-6) entrambi esaminati da un modello diverso da entrambi |
| S04 | resta IN_REVIEW | Solo prove automatiche (CP04.1–3 verdi); la prova manuale dell'app autonoma resta da eseguire |
| S05 | resta IN_REVIEW | CP05.1–3 verdi nella suite; l'esperimento completo non è stato rigenerato in questa sessione; F8 documentato |
| S06 | **VERIFIED** (raccomandazione) | CP06.1–3 ripetuti; correzione F4 verificata con probe proprio; il refactor del motore ha lasciato invariato il CLI S03 |
| S07 | resta IN_REVIEW | R1 e R3 HIGH e R2 MEDIUM aperti; collaudo testnet reale demandato all'operatore (chiavi richieste) |
| S08 | resta IN_REVIEW | R1 indebolisce il gate sul percorso post-creazione; scheda BLOCKED e decisione NOT_ACTIVATA verificate e coerenti; dipendenze S01/S05/S07 aperte |
| S09 | **VERIFIED** (raccomandazione, nei limiti dichiarati) | `content_sha256` identico rigenerando il report dal registro drill-c; totali e riconciliazione coperti dai test; F5 LOW resta aperto; valido per fixture/paper, non per esecuzioni live reali (inesistenti) |

Nessuna dichiarazione di readiness per il live: la candidata attuale è BLOCKED e NOT_ACTIVATED, correttamente.

## Limiti di questa review

- Venv creato con `--system-site-packages`: le dipendenze non sono state ricostruite da zero (procedura identica alle review precedenti); l'installazione del pacchetto in esame resta editable dal clone al commit esatto.
- Nessun accesso alla rete dalla sessione di review: la documentazione Binance è stata verificata solo staticamente e tramite il vettore ufficiale della firma incluso nei test.
- `data/forward/` non aperto (vincolo): le prove CP08 di isolamento sono quelle della suite (verdi) e non un audit dell'archivio.
- Il drill S08 tramite wheel installato non è stato rieseguito: ne è stata verificata l'evidenza registrata (`drill.json` 15/15 PASS) e la copertura nei test CP08.1.

## Artefatti creati (nessuno committato)

- Questo report: `REVIEW-INDEP-2026-11-13-glm.md`.
- `var/review-indep/claude/`: `clone/` (sparse, data/forward escluso), `venv/`, `probe.py` + `probe.log` (9/9), `sim-a/`, `sim-b/` (ledger S03), `ops-regen/` (report drill-c rigenerato), directory temporanee pytest e dati dei probe (`probe-data/`).
