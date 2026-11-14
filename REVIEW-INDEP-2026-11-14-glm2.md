# Revisione indipendente — 2026-11-14 (GLM-5.3, commit 6651afc)

**Revisore:** GLM-5.3 (provider Z.ai), agente pi, sessione `01a1dbbe-15f2-7112-963a-b71e93626b41`.
**Base:** commit `6651afc` su `main` (clone pulito in `var/review-indep/clone`, venv nuovo Python 3.14.0, `pip install -e ".[dashboard,dev]"`).
**Indipendenza (PRD §5):** autori del codice rivisto e delle correzioni = Claude Opus 5.5 e Claude Sonnet 5.5 (S01, S04, S07, S08, E02; correzioni A2/R9/R10/F5 e `validate_card`). Questo revisore è un modello diverso dagli autori: la ripetizione delle prove determinanti è indipendente. **Trasparenza:** GLM-5.3 ha già firmato la review del 2026-11-14 su `b212033` (`REVIEW-INDEP-2026-11-14-glm.md`) in un'altra sessione: stesso modello, sessione e mandato diversi; i probe di questa review sono stati scritti da zero e mai riusati quelli altrui; le review precedenti sono state lette come contesto, gli esiti osservati direttamente.

## Vincoli rispettati

- `data/forward/` mai letto né modificato: tutte le prove usano fixture sintetiche, dati di ricerca (`data/chunks`, `data/snapshots`) o copie in `var/review-indep/`. La parte di CP01.3 che tocca l'archivio forward reale **non è eseguibile** in questo perimetro (v. tabella).
- Nessuna credenziale, nessuna chiamata di rete a exchange. `scripts/s07_testnet_smoke.py` **non eseguito** (riservato all'operatore).
- Nessun commit, push o modifica a codice e backlog; lavoro in `var/review-indep/` (ignorata da Git); questo report non è committato.
- Stati del backlog non modificati: qui sotto solo raccomandazioni.

## Prove ripetute

Ambiente: venv nuovo (Python 3.14.0; streamlit 1.67.0, pandas 2.3.3, scikit-learn 1.9.1, pyarrow 23.0.1, PyYAML 6.0.3, pytest 9.2.0, requests 2.34.2, numpy 2.5.4). Atteso 544 passed: **nessuno scostamento**.

| # | Prova | Comando | Atteso | Osservato | Esito |
|---|---|---|---|---|---|
| 1 | Suite completa, clone pulito | `pytest -q -p no:cacheprovider` | 544 passed | **544 passed** in 299 s | PASS |
| 2 | S01 CP01.1 vista senza snapshot | `pytest tests/dashboard/test_forward_status_dashboard.py::test_view_without_snapshot_data -q` | 1 passed | 1 passed | PASS |
| 3 | S01 CP01.2 stati audit | `pytest …test_forward_status_dashboard.py -k audit -q` | 4 stati distinti | 16 passed (9 stati UI + malformati + luogo di ricerca) | PASS |
| 4 | S01 CP01.3 no-write | `pytest … -k never_write -q` | il caricamento non scrive | 2 passed su archivi di fixture | PASS (limitata) |
| 5 | S01 probe proprio banner | `probe_s01.py` | banner di successo solo con AUDIT_CURRENT | **5/5**: success solo AUDIT_CURRENT (18 combinazioni di stato/report), `AUDIT_FAILED` elenca i fallimenti o lo dichiara, vista senza archivio renderizza con messaggio informativo | PASS |
| 6 | S04 CP04.1 | `pytest tests/dashboard/test_strategy_studio.py tests/test_studio.py -k "cp041 or Immutable or tampered"` | 8 passed | 8 passed | PASS |
| 7 | S04 CP04.2 | `pytest tests/test_studio.py -k CP042` | 5 passed | 5 passed | PASS |
| 8 | S04 CP04.3 | `pytest … -k "CP043 or authorized" …` | 6 passed | 6 passed | PASS |
| 9 | S04 app autonoma (CP04.1–3 per mano del revisore) | `streamlit run …studio_app.py` (headless, porta 8623) + flusso via AppTest su `studio_app.py` con dati reali del catalogo (`probe_s04.py`) | app avviata; salva config → backtest → rilegge in sessione nuova → confronta due run | **9/9**: HTTP 200; 5 schede; config salvata immutabile; solo `BTCUSDT-1h-raw-v1` offerto; run con 620 BUY; sessione nuova rilegge run e metriche identiche (Trade 620, P&L −8.158,48); secondo run con strategia diversa; Confronto: "Run confrontabili a parità di condizioni". Con condizioni volontariamente diverse (latenza/orizzonte) la vista segnala "Condizioni diverse tra i run" (CP04.3) | PASS |
| 10 | S07 CP07.1 | `pytest tests/test_live.py -k "cp071 or adapter or signature or end_to_end"` | 17 passed | 17 passed | PASS |
| 11 | S07 CP07.2 | `pytest tests/test_live.py -k cp072` | 6 passed | 6 passed | PASS |
| 12 | S07 CP07.3 | `pytest tests/test_live.py -k "cp073 or mainnet or pins"` | 7 passed | 7 passed | PASS |
| 13 | S07 regressioni review | `pytest tests/test_live.py -k review` | 15 passed | 15 passed | PASS |
| 14 | S07 probe propri R1/R3/R8/R9/R10 | `probe_s07.py` (FakeExchange + lab feed, repo sintetico) | correzioni confermate | **18/18**: R8 `FILLS_EXCESS`→HALTED immediato→resolve→rebaseline→ripresa; R3 lock (comandi concorrenti tutti `SessionBusy`, nessun record `end` interleaved, 1 ordine); R1 real-capital senza `--approval` rifiutato, approval contraffatta `{}` rifiutata dal **gate reale**, sessione resta `ENGINE_MISMATCH`; R9 CLI `{"status":"BUSY"}` con exit 75 senza traceback; R10 BUY senza fill importati esce a orizzonte dopo rebaseline (SELL FILLED a +6h) | PASS |
| 15 | S08 suite | `pytest tests/test_release.py -q` | verde | 16 passed | PASS |
| 16 | S08 CP08.1 drill dal pacchetto | `release package --out …` + `…/venv/Scripts/python -m crypto_decision_lab.release drill --candidate btcusdt-momentum-v1 …` | 15/15 step PASS dal wheel installato | wheel `crypto_decision_lab-0.2.0` in venv nuovo, **15/15 step PASS** (`rel/drill-btcusdt-momentum-v1/drill.json`) | PASS (dopo R12) |
| 17 | S08 CP08.2 scheda ricostruita su 6651afc | `release build-card … --drill … --package-out …` (registry di prova) | BLOCKED con i motivi giusti | **BLOCKED**, 8 motivi: backtest −96,0%/DD −96,4%, paper −91,1%/DD −91,1%, testnet assente, S01/S04/S07 `IN_REVIEW`; `git_clean` ora 0 e commit `6651afc` (pulito); **ledger backtest e paper byte-identici alla scheda committata** (`8450c9d1…`, `324dea26…`, `matches_backtest: true`); drill PASS | PASS |
| 18 | S08 scheda committata + decisioni | `validate_card(releases/…/fd1153cd/release-card.json)`, `decisions(…)` | BLOCKED valida; 2×NOT_ACTIVATED | validate_card OK: BLOCKED, 14 controlli falliti (i motivi sopra + 6 percorsi sporchi e 8 step non VERIFIED allo stato dell'epoca); `decisions.jsonl`: 2 voci NOT_ACTIVATED (gate 17:24:43Z, operatore 17:35:54Z) | PASS |
| 19 | S08 attacchi al gate | `probe_s08_gate.py` (26 attacchi su copia del registro) | tutti rifiutati | **26/26**: A1 scheda scritta a mano minimale rifiutata; B1–B9 scheda BLOCKED manipolata (passed ribaltati, controlli tolti, soglie/capitale/limiti/simbolo cambiati, passed incoerente con observed, scheda spostata) sempre rifiutata; C2 approvazione con campi diversi rifiutata; C3 scheda modificata dopo l'approvazione rifiutata; C4 config/ambiente/exchange/codice diversi rifiutati; D1 decide ACTIVATED su BLOCKED rifiutato; D2 ACTIVATED senza approval rifiutato; D3 approve su BLOCKED rifiutato; D4 criteri riregistrati rifiutato (write-once); D5 registro solo NOT_ACTIVATED. C0/C1 confermano il **limite dichiarato**: una catena auto-consistente ricostruita a mano (valori osservati non rimisurati) passa `validate_card`/`verify_activation` — richiede scrittura diretta su `releases/` | PASS |
| 20 | S08 gate dal CLI reale | `live session-create --exchange binance-mainnet` senza e con approval contraffatta | rifiutato | senza approval: "mainnet requires an approved READY release"; con approval `{}` e con campi manomessi: "activation refused: …" (gate reale, nessuna rete) | PASS |
| 21 | E02 CP-E02.0 | `pytest tests/test_pattern_detector.py tests/test_pattern_formation_experiment.py -q`; sha256 della specifica | 35 passed; sha `b0b757a0…` | 35 passed; sha256 LF = `b0b757a02dd14d12d26f36e5daf726070eb2f3e71aec9a3bb8ff8844ce0900d4` (il file è già LF) | PASS |
| 22 | E02 CP-E02.1 due esecuzioni su dati reali | `python -m crypto_decision_lab.pattern_formation_experiment --spec …` (run A default) e `--output-dir …` (run B) | 16 `ledger_sha256` identici | **16/16 ledger byte-identici** tra A e B e identici ai digest del `manifest.json`; report identico; unica differenza `locked_utc` (timestamp) in preregistration/selection; preregistration scritta prima dei dati, selection prima di validazione/conferma | PASS |
| 23 | E02 CP-E02.2 esito | lettura `report.json` | REJECTED sui criteri registrati | **REJECTED** ("at least one validation criterion failed"): validazione g05 `costs_share_of_gross` 0,8574 > 0,5; sviluppo g05 −35,92% / 113 trade; validazione +55 trade, uscite per invalidazione 37/55; conferma 68 trade, invalidazione 55/68 — numeri uguali all'handoff e al backlog | PASS |
| 24 | E02 causalità del detector | `probe_e02.py` (scritto dal revisore) | nessuna barra corrente/futura nelle feature | **6/6**: invarianza sui prefissi (4 tagli); alterare la barra 79 non cambia le feature degli eventi ≤79 mentre la barra entra in quelle dell'evento successivo; reset a gap (feature vuote dopo l'ora mancante); strategia fail-closed senza feature | PASS |
| 25 | Correzioni A2, R9, R10, F5 | test di regressione del commit `6811579` eseguiti sul padre `4e79c23` (worktree `clone-nofix`) e su `6651afc` | falliscono senza, passano con | **senza**: `test_review_r10_…`, `test_review_r9_…`, `test_f5_quote_sized_market_fill_ratio…`, CP062 paper tab (A2, streamlit 1.67) **failed**; **con**: tutti **passed**; suite completa 544 | PASS |
| 26 | Correzione S08 `validate_card` (`55174e7`) | test `hand_written/tampered/approval_must` sul padre `cb406a0` e su `6651afc` | falliscono senza, passano con | senza: **6 failed**; con: passed (inclusi nella suite 16/16 di prova 15 e nei probe 19) | PASS |
| 27 | R11 (per la valutazione) | `pattern_experiment --spec config/experiments/S05-momentum-filter-v1.json` (sklearn 1.9.1) | esito invariato | REJECTED; `spec_sha256 ba6754d9…` identico; `model_sha256 dbddca2e…` ≠ riferimento `85975023…` (1.9.0); digest dataset = pin (`2cb8f741…`, `8fffb2ec…`) | PASS |
| 28 | F10 (per la valutazione) | `simulation --source BTCUSDT-1h-raw-v1 --config config/simulation.example.json` | comportamento noto | esito `HALTED_GAP`, cassa 9834,0239; ledger sha `a9d732ee…` (identico alle review precedenti: riproducibilità cross-revisore confermata) | PASS |
| 29 | A5 (per la valutazione) | `git grep -l "C:\\|C:/Users\|/home/\|/Users/"` su tracciati | percorsi locali solo in artefatti congelati | 3 file (`raw-manifest.json` dello snapshot congelato, inventario S00A, una review): confermato, nessuna azione possibile su archivio congelato | PASS |

**Non eseguibili, con motivo.**
- *CP01.3 sull'archivio forward reale*: richiede di leggere `data/forward/` (confronto di hash dell'archivio), vietato dal perimetro CP08 di questo revisore. Verificato l'equivalente su fixture (prova 4) e la logica no-write del codice; l'integrità a livello di blob Git tra commit è stata verificata dalle review del 13/14-11.
- *Collaudo testnet S07 (`scripts/s07_testnet_smoke.py`)*: riservato all'operatore con le sue chiavi; include la conferma R6 (ordinamento `myTrades` con `orderId`+`fromId`).
- *Prova manuale da browser dell'app S04*: non eseguibile da un agente CLI. Sostituita con avvio reale del server Streamlit + percorso completo CP04.1–3 guidato via AppTest sul medesimo albero di componenti (prova 9). La prova manuale dell'operatore del 2026-11-14 resta evidenza complementare (documentata in S04-HANDOFF).

## Rilievi nuovi

| ID | Sev. | Step | Rilievo | Riproduzione minima |
|---|---|---|---|---|
| R12 | LOW | S08 | `release package` fallisce da un ambiente installato come documentato su Python ≥3.12: `pip wheel --no-build-isolation` richiede `setuptools` nell'ambiente, ma nessun extra lo dichiara (i `build-system requires` valgono solo con isolamento). Il venv di Python 3.14 non lo preinstalla. Nessun effetto sul gate; rallenta la riproducibilità del drill CP08.1 da installazione pulita. | venv nuovo (Py 3.14): `pip install -e ".[dashboard,dev]"` → `python -m crypto_decision_lab.release package --out …` → `ReleaseError: wheel build failed: … No module named 'setuptools'`. Corretto da `pip install setuptools wheel` (drill poi 15/15). Suggerimento: aggiungere `setuptools`/`wheel` all'extra `dev` o usare l'isolamento di build |
| R13 | INFO | S08 | Il comando `release verify` costruisce l'exchange come `{"name": "binance-spot", "environment": args.environment}` (default `mainnet`): nome exchange non configurabile e ambiente di default "ottimista". È solo un ausilio pre-volo; il gate reale in `LiveSession.create` usa i valori della sessione. | lettura `release.py`, ramo `verify` di `main()`. Nessun bypass: le prove 19–20 usano il gate reale |
| R14 | INFO | S07 | `accept-code-revision --approval <file>` su sessione **non** a capitale reale ignora il file in silenzio (il parametro è letto solo se `real_capital()`). Cosmetico; già notato come nota cosmetica dalla review GLM del 14-11 | lettura `live.py:accept_code_revision`; `probe_s07.py` R1-testnet incluso nella suite (passa senza approval) |
| R15 | INFO | S09 | `ops_report._live_orders(register, hypothesis)` riceve `hypothesis` e non lo usa (avanzo del refactor F5). Nessun effetto | lettura `ops_report.py`; `grep -n hypothesis` |

Nessun rilievo HIGH o MEDIUM. Il gate mainnet ha resistito a tutti gli attacchi nel suo modello di minaccia (v. anche "Limiti").

## Verdetto sulle correzioni del 2026-11-14

| Correzione | Verdetto | Evidenza |
|---|---|---|
| A2 (st.rerun dopo le azioni della scheda Paper trading) | **Confermata** | CP062 paper tab: failed su `4e79c23` con streamlit 1.67 (pulsante disegnato disabilitato con lo stato vecchio), passed su `6651afc` (prova 25) |
| R9 (CLI: SessionBusy → JSON + exit 75) | **Confermata** | test failed senza / passed con; probe proprio con processo detentore del lock: `rc=75`, `{"status": "BUSY", …}`, nessun traceback (prove 14, 25) |
| R10 (BUY `FILLS_INCOMPLETE` avvia l'orizzonte) | **Confermata** | test failed senza / passed con; probe proprio: BUY con `hide_fills` mai rilasciato → HALTED → resolve `FILLS_INCOMPLETE` → rebaseline → SELL FILLED a +6h (prove 14, 25) |
| F5 (`fill_ratio` dei MARKET a importo in quote su importo speso) | **Confermata** | test failed senza / passed con; `ops_report._live_orders` calcola `notional / requested_quote` e espone `requested_quote` (prove 25, lettura codice) |
| S08 `validate_card` (commit `55174e7`) | **Confermata** | 6 test failed sul padre `cb406a0`, verdi su `6651afc`; probe proprio con 26 attacchi: scheda scritta a mano, scheda BLOCKED trasformata (passed, decision, soglie, controlli tolti, digest criteri), approvazione con campi diversi, percorsi alternativi (decide/approve/write-once/posizione) tutti rifiutati (prove 19, 20, 26) |

Regressioni: nessuna (suite completa 544 passed su `6651afc`).

## Rilievi aperti (F6, F8, F10, A5, R11)

| ID | Verdetto | Evidenza |
|---|---|---|
| F6 | **Confermato LOW** | `build_adapter`/`open_feed` risolvono i percorsi relativi contro `repo_root`; `FakeExchange(path)` con file assente crea stato nuovo vuoto (lettura `exchange.py:212-225`). Solo fixture/collaudo: nessun percorso verso denaro reale |
| F8 | **Confermato LOW** | Artefatti storici citano digest di motori precedenti (la scheda `fd1153cd` stessa documenta `code_sha256` diverso dall'attuale `a7d396bd…` — comportamento corretto del pinning). Il report S05 rigenerato oggi non cita `engine_sha256` tra le chiavi di primo livello; la deriva resta una nota di provenienza, senza effetto sugli esiti |
| F10 | **Confermato INFO** | Prova 28: l'esempio S03 termina `HALTED_GAP` con cassa 9834,02, ledger `a9d732ee…` identico alle esecuzioni dei revisori precedenti; comportamento previsto non dichiarato nel README dell'esempio |
| A5 | **Confermato INFO** | Prova 29: percorsi locali in artefatti tracciati congelati (snapshot CP08, inventario). Nessuna azione possibile senza violare il congelamento |
| R11 | **Confermato INFO** | Prova 27: sklearn 1.9.1 → `model_sha256 dbddca2e…` ≠ riferimento `85975023…` (1.9.0), identico a quanto osservato dalla review GLM del 14-11 con la stessa versione; esito REJECTED e `spec_sha256` invariati. L'identità registrata dipende dall'ambiente: nessun impatto (nessun candidato promosso) |

## Raccomandazioni al coordinatore

| Step | Raccomandazione | Motivo |
|---|---|---|
| S01 | **VERIFIED** | CP01.1–3 ripetuti da un revisore non Claude (prove 2–5): banner di successo solo con `AUDIT_CURRENT` (anche con input malformati), 9 stati distinti, no-write su fixture. Residuo dichiarato in handoff: nessuna soglia di età dell'audit nella UI (coperta dal controllo FRESHNESS interno al report CP08-07) — non bloccante |
| S04 | **VERIFIED** | CP04.1–3 ripetuti (prove 6–8) **e** percorso dell'app autonoma eseguito per intero dal revisore (prova 9): salvataggio configurazione immutabile, backtest su dati reali, rilettura in sessione nuova con metriche identiche, confronto di due run con segnalazione delle condizioni diverse, catalogo limitato alle fonti autorizzate. La prova manuale dell'operatore del 14-11 resta complementare |
| S07 | **resta IN_REVIEW** | Parte codice chiusa: CP07.1–3 verdi su fixture, R1–R3, R8 (e R9, R10) confermate con probe indipendenti e gate reale (prove 10–14). Manca il collaudo sull'API reale della testnet con chiavi dell'operatore (`scripts/s07_testnet_smoke.py`), inclusa la conferma R6 su `orderId`+`fromId`: è l'oggetto esplicito del checkpoint S07 ("sandbox o ambiente di test") e non è sostituibile con fixture |
| S08 | **VERIFIED** | CP08.1 drill ripetuto dal wheel installato in venv nuovo (15/15, prova 16); CP08.2 scheda ricostruita su commit pulito: BLOCKED con i motivi corretti e ledger identici alla scheda committata (prova 17); CP08.3 decisione NOT_ACTIVATED registrata e confermata dall'operatore, registro append-only coerente (prova 18). Gate mainnet attaccato su tutti i vettori richiesti senza successo (prove 19–20), correzione `validate_card` verificata fail-without/pass-with (prova 26). Residui: nessuna candidata READY (per costruzione), collaudo testnet registrato tra gli open_requirements della decisione stessa; limite dichiarato sui valori osservati non rimisurati (v. Limiti) |
| E02 | **VERIFIED** | CP-E02.0–2 ripetuti da clone pulito (prove 21–24): 35 test, due esecuzioni con 16 ledger byte-identici, REJECTED dal criterio registrato, sha256 della specifica conforme, detector causale verificato con probe indipendente (invarianza sui prefissi, barra corrente esclusa, reset a gap), numeri identici a handoff e backlog. Anomalia griglia g01=g03/g05=g07 già dichiarata in handoff per una eventuale v2 |

Nessuna dichiarazione di readiness per il live: la candidata è BLOCKED e NOT_ACTIVATED, correttamente.

## Limiti di questa review

- Nessuna esecuzione contro API Binance reali (mainnet e testnet): le chiamate sono verificate staticamente contro la documentazione ufficiale citata in `docs/S07-live-adapter.md` (firma HMAC-SHA256 hex sulla query con `recvWindow`/`timestamp`, header `X-MBX-APIKEY`, endpoint `/api/v3/{order,openOrders,myTrades,account,exchangeInfo,klines,time}`, `-2013`/`-2011`/`-1007`/5xx, `LIMIT GTC` con `timeInForce`, paginazione `fromId`) e dal vettore ufficiale della firma nei test (verde).
- `data/forward/` mai aperto: CP01.3 limitato alle fixture (v. sopra).
- **Limite dichiarato del gate S08, confermato:** `validate_card` non rimisura i valori osservati; con scrittura diretta su `releases/` una catena auto-consistente (criteri propri + scheda READY con valori conformi alle soglie + approval coerente) supera `verify_activation` (probe C0/C1). È il limite già dichiarato dal coordinatore; la mitigazione operativa è il tracciamento Git del registro (diff/aggiunte visibili) e la ricostruzione della scheda con `build-card` prima di ogni approvazione seria. Serve comunque `CDL_LIVE_ALLOW_MAINNET=1`, `allow_mainnet`, chiavi e capitale in conto.
- La prova "manuale da browser" di S04 è sostituita dall'avvio reale del server e dalla guida via AppTest (stesso albero di componenti).
- Le esecuzioni dell'esperimento E02 sono state fatte su dati di ricerca del catalogo (`BTCUSDT-1h-raw-v1`), non sull'archivio forward.

## Artefatti creati (nessuno committato)

- Questo report: `REVIEW-INDEP-2026-11-14-glm2.md`.
- `var/review-indep/`: `clone/` (6651afc), `clone-nofix/` (4e79c23 + test di regressione), `clone-nofix-s08/` (cb406a0 + test), `venv/`, `probe_s01.py`, `probe_s04.py`, `probe_s07.py`, `probe_s08_gate.py`, `probe_e02.py`, `pytest-full.log` (544 passed), `rel/` (package, `drill-btcusdt-momentum-v1/drill.json`, scheda `a7d396bd`), `e02-run-b/`, `s02-ds/`, `s05-rerun-gem/`, `s03-f10/`, `attack-releases/`, log vari.

## Comandi principali

```bash
cd var/review-indep && python -m venv venv && ./venv/Scripts/python -m pip install -e ".[dashboard,dev]"   # nel clone
./venv/Scripts/python -m pytest -q -p no:cacheprovider --basetemp ../pytest-tmp                          # 544 passed
./venv/Scripts/python probe_s01.py && ./venv/Scripts/python probe_s04.py && ./venv/Scripts/python probe_s07.py
./venv/Scripts/python probe_s08_gate.py && ./venv/Scripts/python probe_e02.py
# S08 drill dal pacchetto
../venv/Scripts/python -m crypto_decision_lab.release package --out ../rel/package
../rel/package/venv/Scripts/python.exe -m crypto_decision_lab.release drill --candidate btcusdt-momentum-v1 \
  --python ../rel/package/venv/Scripts/python.exe --workdir ../rel/drill-btcusdt-momentum-v1               # 15/15
../venv/Scripts/python -m crypto_decision_lab.release --releases ../rel/releases build-card \
  --candidate btcusdt-momentum-v1 --drill ../rel/drill-btcusdt-momentum-v1/drill.json --package-out ../rel/package
# E02 due volte
../venv/Scripts/python -m crypto_decision_lab.pattern_formation_experiment --spec config/experiments/E02-pattern-formation-v1.json
../venv/Scripts/python -m crypto_decision_lab.pattern_formation_experiment --spec … --output-dir ../e02-run-b
# correzioni: senza/col
git worktree add ../clone-nofix 4e79c23 && cd ../clone-nofix && git checkout 6811579 -- tests/test_live.py tests/test_ops_report.py
../venv/Scripts/python -m pytest tests/test_live.py -k "review_r9 or review_r10" ; … -k f5 ; … test_cp062_paper_tab…
```
