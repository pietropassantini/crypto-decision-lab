# Revisione indipendente — 2026-11-14 (GLM-5.3, commit b212033)

**Revisore:** GLM-5.3 (provider Z.ai), agente pi, sessione `01a1d94c-ab7d-7112-963a-b71ddf031b58`.
**Base:** commit `b212033` su branch `prd/integration` (clone pulito in `var/review-indep/glm2/clone`, sparse-checkout con `data/forward/` esclusa dal working tree).
**Indipendenza (PRD §5):** autore di S04–S09, delle correzioni F1–F4, delle correzioni R1–R3/R8 (`900d7d2`) e delle correzioni S00A A1/A3/A4/F9 (`b6cb237`) = Claude Opus 5.5 (le ultime con co-autore dichiarato). Questo revisore è un modello diverso, quindi la ripetizione delle prove determinanti è indipendente per tutti i punti sopra. **Trasparenza:** GLM-5.3 ha già firmato la review del 2026-11-13 (`REVIEW-INDEP-2026-11-13-glm.md`): stessa famiglia di modello, sessione diversa. I probe di questa review sono stati scritti da zero (`var/review-indep/glm2/probe.py`) e mai eseguiti quelli degli altri revisori; le review precedenti nel repo sono state lette come contesto (la direzione delle indagini R1–R3 non è nascente), gli esiti sono stati osservati direttamente.

**Perimetro di questa review** (delta rispetto alle review indipendenti del 2026-11-13 su `53f15ef`):
1. correzioni R1, R2, R3, R8 committate in `900d7d2` (all'epoca viste solo nel working tree da DeepSeek);
2. correzioni S00A: A1, A3, A4 e F9 (`b6cb237`), F7 (`ecef172`);
3. ripetizione dei checkpoint e delle prove determinanti sul commit integrato, come da `docs/REVIEWER-PROMPT.md`.

## Vincoli rispettati

- `data/forward/` mai letto né modificato: clone con sparse-checkout `!data/forward/` (18 file tracciati assenti dal working tree, verificato). CP00A.4 verificato a livello di blob Git (vedi limite sotto).
- Nessuna credenziale, nessuna chiamata di rete agli exchange, `scripts/s07_testnet_smoke.py` non eseguito (riservato all'operatore).
- Nessun commit, push o modifica a codice e backlog. Lavoro in `var/review-indep/glm2/` (ignorata da git).
- Stati del backlog non modificati: qui sotto solo raccomandazioni.

## Prove ripetute

| # | Prova | Comando | Atteso | Osservato | Esito |
|---|---|---|---|---|---|
| 1 | Clone pulito `b212033` + **venv isolato** (senza `--system-site-packages`) | `python -m venv venv-iso`; `pip install -e .` | A1: PyYAML nel nucleo | `yaml 6.0.3`, `pip show PyYAML` → Required-by: crypto-decision-lab; `forward_collection`, `independent_validation`, `protocol_integrity` importano | PASS |
| 2 | Suite completa nel venv isolato (`.[dashboard,dev]`, streamlit 1.67.0) | `pytest -q -p no:cacheprovider` | 489 raccolti; 1 fallito (A2 noto) | **488 passed, 1 failed in 356 s** — solo `test_cp062_paper_tab…` (A2: pulsante `paper_ack` disabilitato, streamlit 1.67) | PASS |
| 3 | CP06.1 ripresa dopo interruzione | `pytest tests/test_paper.py -k "cp061 or stale_version or replay_session"` | verde | 10 passed | PASS |
| 4 | CP06.2 ciclo di vita feed | `tests/test_paper.py -k "cp062 or revised"` (lato paper) | verde | 3 passed | PASS |
| 5 | CP06.3 pin modello/motore | `tests/test_paper.py -k cp063` | verde | 2 passed | PASS |
| 6 | CP07.1 invio/rifiuto/parziale/annullamento + adattatore + firma | `tests/test_live.py -k "cp071 or adapter or signature or end_to_end"` | verde | 17 passed | PASS |
| 7 | CP07.2 timeout e riavvio | `tests/test_live.py -k cp072` | verde | 6 passed | PASS |
| 8 | CP07.3 guardie e mainnet | `tests/test_live.py -k "cp073 or mainnet or pins"` | verde | 7 passed | PASS |
| 9 | Regressioni F1–F4 + R1–R8 | `tests/test_live.py tests/test_paper.py -k review` | 15 verdi | 15 passed | PASS |
| 10 | CP08.1–3 drill/scheda/gate/decisioni | `pytest tests/test_release.py` | verde | 10 passed (83 s) | PASS |
| 11 | CP09.1–2 report e esperimenti | `pytest tests/test_ops_report.py` | verde | 6 passed | PASS |
| 12 | Probe propri R1–R3, R8, F1–F3 (scritti da zero) | `var/review-indep/glm2/probe.py` | vedi riga 13 | **51/51 PASS** (log in `probe.log`) | PASS |
| 13 | Dettaglio probe (estratto) | — | — | R1: `accept-code-revision` su sessione real-capital senza `--approval` rifiutato; con approval `{}` rifiutato dal **gate reale** `verify_activation` (nessuno stub); con approval valida del nuovo codice → RUNNING, pin e activation aggiornati; testnet accetta senza approval. R2: ordine eseguito senza trades → `FILLS_PENDING`, `UNRESOLVED`, riconciliabile, nessun nuovo ordine; `release_fills` → `FILLED`/`WAITING`; oltre tolleranza → `HALTED` "trades are still incomplete"; resolve → `FILLS_INCOMPLETE`; rebaseline → ripresa. R2-paginazione: `myTrades` 2 pagine con `fromId=1001`, 1004 fill letti. R3: durante un `poll` del runner, `finish`/`poll`/`suspend` dalla seconda connessione → tutti `SessionBusy`, nessun record `end` interleaved, 1 ordine/1 submit del runner, `finish` riesce dopo; lock rilasciato alla morte del processo detentore (subprocess `os._exit(97)`); `_send` difensivo → `NOT_SENT`/`NOT_PLACED`, 0 submit. R8: `understate_executed` → `FILLS_EXCESS`, `HALTED` immediato "imported trades exceed", rebaseline rifiutato, resolve → `FILLS_EXCESS_ACCEPTED`, ripresa. F1: 7 comandi rifiutati su `COMPLETED`, poll non invia. F2: `ENGINE_MISMATCH` sticky su suspend/resume, trading rifiutato, accept → RUNNING. F3: `MISSING` → `UNRESOLVED` → `HALTED` → `LOST` → rebaseline → nuovo client id | PASS |
| 14 | S03: ledger CLI byte-identico | `python -m crypto_decision_lab.simulation --source BTCUSDT-1h-raw-v1 --config config/simulation.example.json --output-dir s03-{a,b}` (×2) | identiche | sha256 `a9d732ee…b5ea` entrambe, **uguale al riferimento pre-refactor** `var/fix-s03-bt/ledger.json`; `gap`→`end`, cash `9834.02390961377` (F10 confermato) | PASS |
| 15 | CP09.1 report riproducibile | `ops_report report --session-dir var/release/…/drill-c-4345b936 --label "<etichetta originale>" --out ops-regen` | `content_sha256` = `0fae4fc9…` | `0fae4fc93e7a52ee…d80540` identico a `reports/s09/drill-c…/report.json` | PASS |
| 16 | CP02.1 build doppia del dataset (parametri reali: latency 0, horizon 24, fee 0,1%, slippage 0,05%, stride 24) | `cli build-dataset --source BTCUSDT-1h-raw-v1 --dataset-id BTCUSDT-1h-research-v1 --output-dir …` (×2) | contenuti identici e uguali ai pin | features `content_sha256 2cb8f741…489b`, targets `8fffb2ec…5f30f`, 2061 righe: file e digest identici tra le build e uguali ai pin di spec S05 e handoff S02 | PASS |
| 17 | S05: ripetizione esperimento preregistrato | `pattern_experiment --spec config/experiments/S05-momentum-filter-v1.json --output-dir s05-rerun` | REJECTED, spec identica | REJECTED; `spec_sha256 ba6754d9…` identico; **421/421 valori numerici dei fold identici** al riferimento; `model_sha256` diverso (R11) | PASS |
| 18 | A3: conteggio test su `0daf402` | clone pulito `0daf402`, `pytest --collect-only -q` | 288 raccolti | **288 collected**; suite completa: **285 passed, 3 failed** — esattamente i 3 test citati nella correzione (leggono evidenze CP08 non tracciate a quel commit) | PASS |
| 19 | A4: esclusioni `.gitignore` | `git check-ignore` su 7 percorsi campione | tutti ignorati | `docs/screenshots/*.png`, chunk nuovi, `data/quarantine/`, `*.stackdump`, `data/reports/BTCUSDT_*.json`, `data/manifests/`, `data/diagnostics/` ignorati; `git ls-files data/chunks data/quarantine` → 140 (70 chunk fissati) | PASS |
| 20 | F9: artefatti snapshot ritracciati | `git rev-parse b212033:<f>` vs `0daf402:<f>` | blob identici | `report.json` e `forecast-windows.parquet` **byte-identici** a `0daf402` (blob `ebeff510…`, `96d80dad…`) | PASS |
| 21 | F7: output rigenerabili | `git status --porcelain` (repo principale) | pulito | working tree pulito: stackdump, reports, manifests, diagnostics, screenshots ora ignorati | PASS |
| 22 | CP00A.4: percorsi congelati (livello Git) | `git diff --name-status 0daf402 b212033 -- data/forward data/experiments data/snapshots data/datasets data/protocols data/chunks data/quarantine` | nessuna modifica | `data/forward`: **zero differenze blob**; globale: 0 `M`, 224 `D` (esperimenti rigenerabili rimossi da S00A) + 6 `A` (evidenze CP08 ritracciate); F9 net-zero (rimozione+ritracciamento stesso blob) | PASS |
| 23 | CP00A.3: segreti su indice e storia | `git grep`/`git log -G` (AKIA, ghp_, xox, chiavi private, `api_secret=`) | nessuno | nessun riscontro; `c8db5682…` presente solo nel test della firma (vettore ufficiale Binance, pubblico) e nelle review che lo citano | PASS |
| 24 | Coerenza documenti corretti (A1/A3/A6) | lettura `README.md`, `S00-BASELINE-REPORT.md`, `S00A-INVENTORY.md`, `docs/S07-live-adapter.md` | correzioni in posto | README: `.[dashboard,dev]` con nota extra + "Dati di test" allineato (A6); baseline S00 corretta con §7 (A3); inventario corretto (A3); runbook S07 aggiornato su lock, `FILLS_*`, `--approval`, R6 | PASS |

Prove non eseguite, con motivo: collaudo testnet reale (chiavi dell'operatore; include la conferma R6 su `orderId`+`fromId`); prova manuale dell'app Streamlit autonoma S04; drill S08 tramite wheel installato (evidenza `drill.json` 15/15 verificata dalle review del 13-11, `release.py` invariato da `53f15ef`: nessuna modifica tra `53f15ef..b212033`); CP00A.4 con `check_frozen_artifacts()`/`protocol_authority()` richiederebbe leggere `data/forward/` (vietato dal perimetro): verificato a livello di blob Git, che è comunque una verifica di integrità del contenuto tra i due commit.

## Verdetto sulle correzioni R1–R3, R8 (autore Claude Opus 5.5, commit `900d7d2`)

| ID | Verdetto | Evidenza (probe propri, gate reale senza stub) |
|---|---|---|
| R1 | **Confermata** | `accept_code_revision` su sessione real-capital (`meta.activation` presente, `real_capital()` vero) senza `--approval` → rifiutato ("a code change needs a new S08 release card"); con approval non valida → rifiutato dalla **vera** `verify_activation` (schema, card, READY, config, exchange, simbolo, `code_sha256`); con approval valida del nuovo digest → RUNNING con pin e activation aggiornati e persistenti alla riapertura. Sessione testnet/fixture: accetta senza approval (comportamento preservato). Nota cosmetica: `--approval` passato su sessione testnet viene ignorato in silenzio. |
| R2 | **Confermata** | Ordine terminale con trades mancanti → `FILLS_PENDING`, resta in `unresolved_orders`, nessun nuovo ordine; `myTrades` paginato con `fromId` (probe con transport sintetico: 2 pagine, 1004 fill); oltre `unknown_grace_seconds` → `HALTED` con messaggio esplicito; `resolve-missing-order` → `FILLS_INCOMPLETE`; rebaseline → ripresa. `BinanceSpotAdapter.query()` unisce i fill di `trades()` al report quando `executedQty > 0`: il recupero funziona anche sul percorso reale. |
| R3 | **Confermata** | Lock esclusivo `session.lock` (msvcrt/fcntl) per l'intero `poll` e per ogni comando operatore: `finish`/`poll`/`suspend` concorrenti → `SessionBusy`, mai interleaved; nessun record `end` interleaved; un solo ordine/submit (del runner); il lock è rilasciato dal sistema operativo alla morte del detentore (subprocess `os._exit(97)`); `_send` ha la difesa in più `NOT_SENT`/`NOT_PLACED` su sessione chiusa o intento non più `INTENT`; il ciclo di `poll` ricontrolla gli stati sticky tra le barre. |
| R8 | **Confermata** | Trades importati oltre `executedQty` finale → `FILLS_EXCESS` e `HALTED` immediato (senza tolleranza) con messaggio esplicito; rebaseline rifiutato finché non risolto; `resolve-missing-order` → `FILLS_EXCESS_ACCEPTED`; poi rebaseline e ripresa. |

## Verdetto su F1–F4 e sui rilievi aperti F5–F10, A1–A6

| ID | Verdetto | Evidenza |
|---|---|---|
| F1 | **Confermata** | Probe `f1`: tutti i comandi operatore rifiutati su `COMPLETED`; `poll` resta `COMPLETED` senza nuovi submit. Il percorso concorrente è chiuso dal lock di R3. |
| F2 | **Confermata** | Probe `f2`: `ENGINE_MISMATCH` sopravvive a suspend/resume; flatten/cancel/rebaseline/finish rifiutati; nessun ordine; accept → RUNNING. L'aggiramento via `accept-code-revision` è chiuso da R1 (verificato con il gate reale). |
| F3 | **Confermata** | Probe `f3`: `MISSING` → `UNRESOLVED` → oltre tolleranza `HALTED` "no longer known by the exchange" → resolve `LOST` → rebaseline → nuovo ordine con client id diverso; 1 solo submit. |
| F4 | **Confermata (via suite + review precedenti)** | `paper.py` invariato da `53f15ef` (diff vuoto); i 2 test di regressione verdi nella suite di oggi; confermata con probe propri da GLM-5.3 e DeepSeek il 13-11. Non ripetuta con probe dedicato in questa sessione. |
| F5 | Confermato LOW, aperto | `ops_report.py` invariato: `fill_ratio` dei MARKET `quoteOrderQty` ancora stimato da quote/reference_open (drill-c: 0,9945–0,9987 su ordini `FILLED`). |
| F6 | Confermato LOW (statico), aperto | `build_adapter`/`open_feed` risolvono i percorsi relativi contro `repo_root`; `FakeExchange` crea stato nuovo se il file manca. Solo fixture. |
| F7 | **Chiuso** | Prova 21: working tree pulito, output rigenerabili ignorati. |
| F8 | Confermato LOW, aperto | S05 report `engine_sha256 6ff68d76…` vs motore attuale `b3c69252…`: artefatto storico non rigenerato. |
| F9 | **Chiuso** | Prova 20: i due artefatti sono ritracciati e byte-identici a `0daf402`. |
| F10 | Confermato INFO, aperto | Prova 14: esempio che termina su `gap`→`end` (HALTED_GAP) con cash 9834,02; comportamento previsto, non dichiarato nel README dell'esempio. |
| A1 | **Chiuso** | Prova 1: PyYAML dipendenza principale; installazione del solo nucleo importa `yaml` e i moduli che lo usano. |
| A2 | Aperto (su S06) | Prova 2: con streamlit 1.67.0 (ammesso da `<2`) `test_cp062_paper_tab…` fallisce: pulsante "Riconosci gap" disegnato disabilitato con lo stato vecchio. Riconfermato identico al rilievo originale. Suggerimento: `st.rerun()` dopo le azioni di `paper_sessions.py` o un vincolo di versione per i test. |
| A3 | **Chiuso** | Prova 18: `0daf402` raccoglie 288 test; clone pulito 285P/3F con esattamente i 3 test citati; documenti corretti (prova 24). |
| A4 | **Chiuso** | Prova 19: esclusioni verificate, 70 chunk fissati tracciati per indice (140 file). |
| A5 | Aperto INFO | Percorsi locali in artefatti storici (archivio congelato, non modificabile). Nessuna azione. |
| A6 | **Chiuso** | README "Dati di test" allineato alla realtà (prova 24). |

## Rilievi nuovi (R9+; nessun HIGH, nessun MEDIUM)

| ID | Sev. | Step | Rilievo | Riproduzione |
|---|---|---|---|---|
| R9 | INFO | S07 | I comandi operatore CLI non gestiscono `SessionBusy`: se un comando arriva mentre il runner tiene il lock, il CLI termina con un traceback invece di un messaggio JSON pulito (solo `session-run --loop` gestisce il caso e riprova). Il comportamento è sicuro (rifiuto + ripetizione, come dice il runbook) ed è solo un difetto di presentazione. | `LiveSession` A tiene il lock (poll lungo); CLI `live suspend-openings …` dalla stessa sessione → traceback `SessionBusy` |
| R10 | INFO | S07 | Un BUY risolto `FILLS_INCOMPLETE` con **zero** fill importati non fa scattare l'uscita a orizzonte: `_last_buy_bar` unisce `orders` con `fills`, quindi l'orologio dell'orizzonte non parte. La posizione accettata col rebaseline resta in portafoglio finché la strategia non va FLAT o l'operatore non fa `flatten`. Nessuna perdita silenziosa: la posizione esiste, è negoziabile e visibile. | Probe r2b: BUY con `hide_fills` mai rilasciato → `HALTED` tolleranza → resolve `FILLS_INCOMPLETE` → rebaseline → barre successive: "exposure unchanged", nessun nuovo ordine |
| R11 | INFO | S05 | `model_sha256` non è bit-riproducibile tra versioni consentite di scikit-learn: la rigenerazione in venv isolato (sklearn 1.9.1) produce metriche identiche (421/421 valori, esito REJECTED) ma un artefatto modello diverso (origine: riferimento con 1.9.0). L'identità registrata dell'esperimento è quindi sensibile all'ambiente, oltre al già noto F8 (`engine_sha256`). Nessun impatto operativo: l'esperimento è REJECTED, nessun candidato promosso. | Prova 17: rerun con `pip install -e '.[dashboard,dev]'` fresco → `model_sha256 dbddca2e…` vs riferimento `85975023…` |

## Raccomandazioni al coordinatore (S00A–S09)

| Step | Raccomandazione | Motivo |
|---|---|---|
| S00A | **VERIFIED** | CP00A.1–4 ripetuti da un revisore non Claude: installazione pulita e suite in venv isolato (A1 chiuso), esclusioni e segreti (A4 chiuso), percorsi congelati intatti a livello di blob Git con `data/forward` a zero differenze (limite dichiarato: la verifica hash a livello di protocollo è quella, completa, della review S00A del 13-11), inventario e baseline corretti (A3 chiuso), F9 chiuso (byte-identico). Residui: A2 (registrato su S06) e A5 (INFO). |
| S01 | resta IN_REVIEW | CP01.1–3 verdi nella suite di oggi, ma l'autore non è registrato nell'handoff e CP01.3 tocca l'archivio forward, che questo revisore non può aprire. Decisione di accettazione al coordinatore. |
| S02 | **VERIFIED** | CP02.1 ripetuto oggi da un revisore indipendente (due build: file e digest identici, uguali ai pin di spec S05 e handoff S02; 2061 finestre); CP02.2–3 verdi nella suite; dipendenza S00A ora VERIFIED. Il rilievo LOW del 13-11 (unione `reserved_dataset_ids` con i ruoli reserved-forward) resta suggerimento non bloccante. |
| S03 | **VERIFIED** | Confermato di nuovo oggi: ledger CLI byte-identico su due esecuzioni e uguale al riferimento pre-refactor (`a9d732ee…`). |
| S04 | resta IN_REVIEW | Prove automatiche CP04.1–3 verdi; la prova manuale dell'app autonoma non è stata eseguita da alcun revisore indipendente. |
| S05 | **VERIFIED** | Esperimento preregistrato rigenerato oggi da un revisore indipendente: esito REJECTED, `spec_sha256` identico, 421/421 valori numerici identici; CP05.1–3 verdi nella suite. Rilievi aperti sull'artefatto storico: F8 (LOW) e R11 (INFO), nessuno dei quali tocca l'esito. |
| S06 | **VERIFIED** (confermato) | CP06.1–3 verdi oggi; F4 coperta da suite e dalle conferme indipendenti del 13-11 (codice paper invariato da allora). A2 resta un rilievo UI aperto, non una regressione. |
| S07 | resta IN_REVIEW | Parte codice chiusa: R1–R3, R8 confermate oggi con probe indipendenti e gate reale; F1–F3 ricontrollate; runbook aggiornato e coerente. Resta il collaudo testnet reale dell'operatore (`scripts/s07_testnet_smoke.py`), inclusa la conferma R6 sulla direzione di `myTrades` con `orderId`+`fromId`, prima del VERIFIED. |
| S08 | resta IN_REVIEW | R1 confermato corretto sul percorso post-creazione (gate reale); scheda `fd1153cd` BLOCKED e decisione NOT_ACTIVATED coerenti (verificate dalle review del 13-11; `release.py` invariato); CP08.1–3 verdi oggi. Dipendenze S01 e S04 ancora IN_REVIEW; nessuna candidata READY. |
| S09 | **VERIFIED** (confermato) | `content_sha256` identico rigenerando il report dal registro drill-c (oggi); totali e riconciliazione coperti dai test; F5 LOW resta aperto; valido per fixture/paper, nessuna esecuzione live reale. |

Nessuna dichiarazione di readiness per il live: la candidata attuale è BLOCKED e NOT_ACTIVATED, correttamente.

## Limiti di questa review

- Venv isolato ma costruito oggi (dipendenze risolte da PyPI): la suite è stata eseguita una sola volta con streamlit 1.67.0 (il run a 489 passed con streamlit 1.55 è l'evidenza della review S00A del 13-11, non ripetuto qui).
- Documentazione Binance verificata staticamente e tramite il vettore ufficiale della firma nei test (verde); nessun accesso alla documentazione live né alle API.
- `data/forward/` mai aperto: CP00A.4 verificato a livello di blob Git; le prove CP08 di isolamento sono quelle della suite.
- F4 non ripetuta con probe dedicato (codice paper invariato da `53f15ef`; suite e due conferme indipendenti del 13-11).
- Il drill S08 tramite wheel installato non è stato rieseguito (evidenza registrata verificata; test CP08.1 verdi).

## Artefatti creati (nessuno committato)

- Questo report: `REVIEW-INDEP-2026-11-14-glm.md`.
- `var/review-indep/glm2/`: `clone/` (b212033, sparse), `clone-base/` (0daf402), `venv-iso/`, `pytest-iso.log`, `probe.py` + `probe.log` (51/51) + `probe-data/`, `s03-a/`/`s03-b/` (ledger S03), `ops-regen/` (report drill-c), `s02-a/`/`s02-v1-a/` e dataset in `clone/data/datasets/research/…` (CP02.1), `s05-rerun/` (esperimento S05), `pytest-tmp/`, `pt2/`, `pt-base/`.

## Comandi principali

```bash
git clone --no-local . var/review-indep/glm2/clone && cd var/review-indep/glm2/clone
git checkout b212033 && git sparse-checkout init --no-cone && git sparse-checkout set '/*' '!data/forward/'
python -m venv ../venv-iso && ../venv-iso/Scripts/python -m pip install -e .            # A1
../venv-iso/Scripts/python -m pip install -e '.[dashboard,dev]'
../venv-iso/Scripts/python -m pytest -q -p no:cacheprovider --basetemp ../pytest-tmp    # 488P, 1F (A2)
../venv-iso/Scripts/python ../probe.py                                                  # 51/51
../venv-iso/Scripts/python -m crypto_decision_lab.simulation --source BTCUSDT-1h-raw-v1 \
    --config config/simulation.example.json --output-dir ../s03-a                        # e -b
../venv-iso/Scripts/python -m crypto_decision_lab.cli build-dataset --source BTCUSDT-1h-raw-v1 \
    --dataset-id BTCUSDT-1h-research-v1 --output-dir ../s02-v1-a \
    --latency-hours 0 --horizon-hours 24 --fee-rate 0.001 --slippage 0.0005 --stride-hours 24
../venv-iso/Scripts/python -m crypto_decision_lab.pattern_experiment \
    --spec config/experiments/S05-momentum-filter-v1.json --output-dir ../s05-rerun
# CP00A.4/A4/F9 (livello git, senza leggere data/forward):
git diff --name-status 0daf402 b212033 -- data/forward data/experiments data/snapshots data/datasets \
    data/protocols data/chunks data/quarantine | awk '{print $1}' | sort | uniq -c       # 6 A, 224 D, 0 M
```
