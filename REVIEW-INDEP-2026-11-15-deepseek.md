# Revisione indipendente — 2026-11-15 (DeepSeek v4 Pro, commit 450591f)

**Revisore:** DeepSeek v4 Pro (provider deepseek), agente pi, sessione `01a1e042-2a0c-7433-a743-ef5f3fd27b9e`.
**Base:** commit `450591f` su `main` (checkout detached nel clone). Perimetro: `git diff 73e7277..450591f -- src tests config docs`.
**Clone:** clone locale pulito in `var/review-indep/review-e05p1`, venv nuovo `var/review-indep/venv-e05p1` (Python 3.14.0), `pip install -e ".[dashboard,dev]"`.
**Indipendenza (PRD §5):** autori del codice rivisto = modelli Claude (Opus 5.5, Sonnet 5.5, Fable 5.2). Questo revisore è DeepSeek v4 Pro, modello diverso dagli autori. **Trasparenza:** DeepSeek v4 Pro è lo stesso modello della review del 2026-11-13 (`REVIEW-INDEP-2026-11-13-deepseek.md`), non delle review GLM-5.3 del 14/15-11. Indipendenza *modello-vs-autore* sussiste; occhi nuovi rispetto alle review GLM, non un modello nuovo. Il coordinatore lo pesi in fase di accettazione.

## Vincoli rispettati

- `data/forward/` mai letto né modificato. Il controllo di fine a fine usa il solo snapshot di ricerca `BTCUSDT-1h-raw-v1` (ruolo `research`). Output in `var/review-indep/` (ignorato da Git).
- Nessuna credenziale, nessuna chiamata di rete: `public_klines` non è stato eseguito contro il mercato reale (il runner paper è stato usato solo in modalità snapshot/lab-feed).
- Nessun commit, push o modifica a codice/backlog. Questo report non è committato.
- Collaudo S07 non eseguito (compito dell'operatore).

## 1. Tabella delle prove

| # | Comando / verifica | Atteso | Osservato | Esito |
|---|---|---|---|---|
| 1 | `pytest -q -p no:cacheprovider` (clone, venv nuovo) | 602 passed | 602 passed in 426 s (85.870 warnings, tutte deprecation) | PASS |
| 2 | `python -m crypto_decision_lab.paper session-create --name e05conf --config candidate.json --snapshot BTCUSDT-1h-raw-v1 --start "2025-01-01 00:00" --end "2026-10-01 00:00" --momentum-days 28 --sma-days 100 --root var/review-indep/paper` | sessione creata, `config_sha256` della candidata | `e05conf-17ba1f7f38`, `config_sha256 7775ccd4…`, `engine_sha256 2f02331f…` | PASS |
| 3 | `python -m crypto_decision_lab.paper session-run --session-dir <cartella>` | COMPLETED, 15.312 eventi | COMPLETED, `events` 15.312, `ledger_records` 61.319, equity 524,91 | PASS |
| 4 | `python -m crypto_decision_lab.session_fidelity --session-dir <cartella>` | match true, 61.319 record | `match: true`, `events: 15312`, `session_records: 61319`, `replay_records: 61319`, exit 0 | PASS |
| 5 | `python -m crypto_decision_lab.session_report edge --session-dir <cartella> --as-of "2026-10-01 00:00"` | 17 trade chiusi, realizzato −23,93 (−4,79%), equity +4,98%, posizione aperta 2026-08-20 con +48,84, buy&hold −5,35%, drawdown −9,33%, costi sul lordo non definiti, esito NOT_CONFIRMED | tutti i valori coincidono; exit 1 (NOT_CONFIRMED) | PASS |
| 6 | Seconda chiamata di `session_report edge` sulla stessa sessione | exit 2, rifiutata | exit 2, messaggio "the edge was already evaluated" | PASS |
| 7 | Ricalcolo indipendente dal registro (walk cash/quantità/equity, pairing esecuzioni, drawdown, buy&hold) senza `session_report`/`studio_analytics` | cifre identiche al punto 5 | eventi 15.312; equity finale 524,9125; net +4,9825%; drawdown −9,3294%; 17 chiusi; realizzato −23,9264; costi 12,4033; lordo −11,5231; aperto 2026-08-20 +48,8390; buy&hold −5,3515% | PASS |
| 8 | `daily_experiment all --spec config/experiments/E05-daily-trend-v1.json --output-dir var/review-indep/e05-recheck` | CANDIDATE riprodotto, ledger di conferma g10 | CANDIDATE; `confirmation-g10.json` rigenerato | PASS |
| 9 | Confronto `session_fidelity.normalized` del registro della sessione con `var/review-indep/e05-recheck/ledgers/confirmation-g10.json` | uguali | 61.319 record entrambi, `normalized` identici | PASS |
| 10 | Manomissioni `session_fidelity` (ordine, fill fee, saldo equity, barra `events.raw`) | tutte rilevate | fill→first_diff 196; order→195; balance→3; bar→0; tutte `match:false` | PASS |
| 11 | `edge --as-of "2027-01-01 00:00"` su una sessione con <8 trade chiusi (gapm) | (per la specifica: INCONCLUSIVE solo dopo 52 settimane reali) | scrive `p1-edge-report.json` con `INCONCLUSIVE` e blocca la sessione | vedi R29 |
| 12 | Cancellazione di `p1-edge-report.json` e nuova chiamata `edge` su una copia della sessione | (per la specifica: una sola valutazione) | nuova valutazione, nuovo report scritto, exit 1 | vedi R30 |
| 13 | `edge` eseguito di giovedì (2026-10-01) | (trigger = primo lunedì 00:00 UTC) | decide lo stesso (`NOT_CONFIRMED`), nessun controllo del giorno | vedi R29 |
| 14 | Lab-feed con gap a un 00:00 (2025-01-05), commit tutti puntuali (+5 min), `timeliness` | (criterio 1 sul runner, criterio 2 sui gap) | `on_time_share` 0,889 (8/9) < 95%, `unexplained_missing_days` vuoto | vedi R27 |

## 2. Controllo di fine a fine (punto 2 del prompt)

Riprodotto per intero nel clone `450591f`. La sessione `e05conf-17ba1f7f38` è COMPLETED con 15.312 eventi e 61.319 record; `session_fidelity` esce 0 con `match:true`. Il registro, dopo `session_fidelity.normalized`, è **identico** a `confirmation-g10.json` rigenerato rieseguendo `daily_experiment all` (E05 → CANDIDATE). Il report `edge` produce esattamente le cifre attese: 17 trade chiusi, realizzato −23,93 (−4,79%), equity +4,98%, posizione aperta dal 2026-08-20 con +48,84 non realizzati, buy&hold −5,35%, drawdown −9,33%, costi sul lordo non definiti (lordo realizzato −11,52 ≤ 0), esito NOT_CONFIRMED (exit 1). Ho ricalcolato ogni cifra con un walk indipendente del registro (senza `session_report` né `studio_analytics`): tutti i valori coincidono (prova 7). La seconda valutazione `edge` esce 2.

Confermo quindi la conseguenza dichiarata dal coordinatore: con i criteri E05-P1 sul netto realizzato, la finestra di conferma di E05 avrebbe dato NOT_CONFIRMED. È la conseguenza voluta del rilievo R16, non un difetto del calcolo.

## 3. Revisione di `session_report.py` contro la specifica E05-P1

### 3.1 Criteri `edge` (favorevole, con note)

- **Trade chiusi vs posizione aperta:** usa `studio_analytics.trades`, filtra `CLOSED`/`OPEN`; il netto realizzato è la somma dei `pnl` dei chiusi su capitale iniziale, i costi sono `fees+slippage` dei soli chiusi, il lordo è `realizzato+costi`. Il mio ricalcolo indipendente (pairing BUY/SELL dal registro) coincide esattamente.
- **Buy and hold:** `run_engine(session, hold, with_features=False)` con `hold` = copia della config della sessione con strategia `constant` LONG. Verificato che non usa le feature della candidata (`with_features=False` → `spec=None` → nessuna feature giornaliera) e che conserva capitale, `max_exposure 0.5`, fee e slippage. Il valore −5,35% coincide con la baseline buy&hold di E05 e col mio ricalcolo.
- **Drawdown dell'equity:** `analytics.summarize` calcola il drawdown sulla curva equity dei `balance` (che include la posizione aperta mark-to-market). Il mio ricalcolo coincide (−9,33%).
- **Criteri:** i 4 criteri nel codice coincidono letteralmente con la specifica. Il caso `gross <= 0` dà `realized_costs_share_of_gross: null` e criterio 3 non superato: coerente con la matematica e con la nota dell'autore, ma la specifica non lo esplicita (vedi R34).

### 3.2 `week12` e `timeliness`

- **Tempestività barra-per-barra:** ogni barra processata scrive una riga `status_log` con `detail = "last event <barra>"` e `at` = ora reale (verificato in `paper._commit_step`). Una poll che processa più barre insieme produce una riga per barra con lo stesso `at`: ogni 00:00 è misurato individualmente. Corretto.
- **Bordi:** `expected = pd.date_range(start.ceil("D"), as_of.floor("D"), freq="1D")` filtrato con `ts + 2h <= as_of`: esclude correttamente i giorni la cui finestra di 2 ore non è ancora chiusa e parte dal primo 00:00 ≥ start. Corretto per gli usi previsti (start all'ora, as_of a ridosso).
- **Gap:** i giorni mancanti coperti da un `gap_acknowledged` finiscono in `missing_days` ma non in `unexplained_missing_days`. **Tuttavia** entrano comunque nel denominatore di `on_time_share`: un solo gap a un 00:00 abbassa la quota sotto il 95% anche se ogni commit è puntuale (prova 14). È il rilievo R27.

### 3.3 Valutazione "una sola volta"

- `edge` scrive `p1-edge-report.json` con `open("x")` e rifiuta (exit 2) se il file esiste. `NOT_DUE` non scrive nulla. Corretto rispetto al testo.
- Scappatoie: cancellare il file riabilita la valutazione (prova 12); copiare la cartella sessione in un nuovo percorso dà un report nuovo; `--as-of` futuro può scrivere e bloccare `INCONCLUSIVE`/decisione (prova 11). Vedi R29/R30.

### 3.4 Trigger "primo lunedì con almeno 8 trade chiusi"

- Lo strumento **non** impone il lunedì né l'orario 00:00 UTC: decide appena `closed_trades >= 8`, in qualsiasi giorno/ora (prova 13: ha deciso di giovedì). Il lunedì è lasciato all'operatore (runbook "ogni lunedì 00:00 UTC").
- Il trigger nel codice dipende solo dal numero di trade chiusi e dalle settimane trascorse, **mai** dal risultato: nessun percorso legge il P&L per decidere se scattare. Questo punto della specifica è rispettato.
- **Optional stopping possibile:** poiché il lunedì non è imposto e il blocco scatta solo alla prima decisione scritta, l'operatore può ritardare la prima esecuzione in attesa di una cifra migliore (o, al contrario, eseguire in anticipo e bloccare un risultato prematuro). Il "nobody may wait for a better figure" è solo testo. Vedi R29.

### 3.5 Regole di stop della specifica (richiesta esplicita: elencarle)

| Regola di stop | Controllata dal software? | Dove |
|---|---|---|
| `max_drawdown_mtm_pct < -20` → FAILED (any time) | **Parzialmente** | solo `session_report week12` emette `FAILED`; `edge` non lo emette (dà al più NOT_CONFIRMED sul criterio 2); il runner `paper.py` non chiude né blocca la sessione sul drawdown. |
| Decisione su una barra non chiusa → INVALID | **No** | nessun esito `INVALID` esiste in `session_report`; il runner per costruzione usa solo barre chiuse (`prev_close`), ma non c'è alcun controllo/diagnosi che produca `INVALID`. |
| Modifica della candidata congelata → INVALID | **No** | nessun esito `INVALID`; la config è immutabile via API (`meta`), ma `session_report` non confronta `config_sha256` con la candidata preregistrata (la specifica non ne fissa il digest) e `session_fidelity` ri-esegue la config della sessione stessa, quindi una config alterata a mano non è smascherata come `INVALID` (al più come mismatch di fedeltà, solo se `week12` gira). |

Rilievo R28.

## 4. Revisione della specifica E05-P1 corretta (config + guida)

- **R21 chiuso:** DEV-1 è scritta in `origin.deviation_note` e in `deviations[0]` con `from`/`to`/`why`/`what_is_kept`/`requires`/`approval`; la guida ha la sezione dedicata.
- **R22 chiuso:** "at least 8 CLOSED trades" e INCONCLUSIVE dopo 52 settimane senza 8 trade (prima "fewer than 5").
- **R23 chiuso:** buy&hold definito operativamente (constant LONG, stesso motore, capitale/esposizione/fee/slippage della candidata, un ingresso al primo evento, valorizzato all'ultimo evento, barre registrate).
- **R24 chiuso:** fonte della prova di tempestività dichiarata (`status_log.at`, riga `last event <barra>`).
- **R25 chiuso:** restart al massimo 2 approvati e registrati; orologi che ripartono; trade delle sessioni abbandonate esclusi; scadenza 78 settimane dall'inizio della prima sessione.
- **DEV-1:** descritta in modo completo; approvazione registrata inline (`deviations[0].approval`, 2026-11-15). Nota: il testo `requires` dice "recorded in approvals" ma nel repo non esiste un artefatto `approvals/` separato e la nota dice "approved in chat" (R33). Cosa si perde rispetto al testnet (fill reali, fee reali) è dichiarato in `data.why_not_testnet_prices`, nella sezione threats e nella guida.
- **Criterio sul netto realizzato:** coerente con il resto e non troppo permissivo; applicato alla finestra di conferma di E05 dà NOT_CONFIRMED, che è la conseguenza voluta (nota del coordinatore e R16). Il criterio 3 non esplicita il caso `gross <= 0` (R34).
- **Ripartenze e "una sola volta":** le ripartenze e la scadenza a 78 settimane non sono imposte dal software (nessun concetto di "prima sessione" nel tool); la "una sola volta" ha le scappatoie di R29/R30.
- **S07 facoltativo:** la guida dichiara che calcola ancora solo `momentum_24h` e non è coperto. Coerente con PRE-1.

## 5. Revisione di `session_fidelity.py` (modifica)

- `run_engine` estrae il corpo di `replay`; `replay()` ora delega a `run_engine(session)` con gli stessi parametri effettivi. La prova 4/9 conferma che il replay produce lo stesso registro di prima (match true e identità col ledger E05).
- Buy and hold: `run_engine(..., with_features=False)` non usa le feature della candidata (verificato per lettura e col ricalcolo −5,35%).
- Manomissioni già provate (ordine, fill, saldo, barra) ancora rilevate: prova 10 (fill→196, order→195, balance→3, bar→0).

## 6. Rilievi precedenti ancora aperti

- **R17 (LOW):** resta valido; ora è documentato esplicitamente nella sezione threats della specifica. Non è diventato più rilevante.
- **R18 (LOW):** resta valido; ora il criterio week12 della specifica dichiara "certifica la coerenza interna, non l'autenticità delle barre". Non è diventato più rilevante.
- **R19 (INFO), R20 (INFO), R26 (INFO):** invariati, nessun impatto nuovo dalla specifica corretta.
- **R13, R14 (INFO, S08/S07):** invariati, non toccati da E05-P1.

## 7. Rilievi nuovi

| ID | Severità | Oggetto | Descrizione e riproduzione minima |
|---|---|---|---|
| R27 | MEDIUM | `timeliness` penalizza i giorni di gap | Un giorno con barra 00:00 mancante per un gap di mercato è contato come `missing` e riduce `on_time_share`, anche se il runner ha committato tutto in orario. Con un solo gap a un 00:00 e tutti i commit a barra+5 min, `timeliness` dà `on_time_share 0,889` (< 95%) e `unexplained_missing_days` vuoto: il criterio 1 fallirebbe per un gap, che invece il criterio 2 tratta separatamente. Riproduzione: lab-feed `BTCUSDT-1h-raw-v1` con fault `{"type":"gap","at":"2025-01-05 00:00"}`, sessione `daily_trend` 2025-01-01→2025-01-10, `session_report.timeliness(..., as_of 2025-01-10 00:00)`. |
| R28 | MEDIUM | Regole di stop solo parzialmente controllate | Vedi §3.5. Il drawdown < −20% produce `FAILED` solo in `week12` (non in `edge`, non nel runner); le due regole `INVALID` (barra non chiusa, modifica candidata) non hanno alcun esito software. La specifica le elenca come "stop_rules_any_time", ma il software non le applica tutte. Inoltre la candidata non è incrociata col preregistrato: la specifica non fissa `config_sha256` (la candidata g10 è `7775ccd4…`) e `session_report` non la verifica. |
| R29 | MEDIUM | Trigger "primo lunedì" e "nessuno attende una cifra migliore" non imposti dal software; `--as-of` può bloccare la sessione | `edge` decide in qualunque giorno/ora appena `closed_trades >= 8` (prova 13: giovedì). Il lunedì e il divieto di attendere sono solo runbook. Inoltre `--as-of` (documentato "solo per provare") scrive il report reale: con <8 trade e `--as-of` futuro produce `INCONCLUSIVE` e scrive `p1-edge-report.json`, bloccando la sessione (prova 11 su `gapm-3d92ef1470`). |
| R30 | LOW | "Una sola volta" aggirabile cancellando il file o copiando la cartella | Il blocco è solo `target.exists()`. Cancellato `p1-edge-report.json` da una copia della sessione, `edge` rivaluta e riscrive (exit 1). Copiare la cartella in un nuovo percorso dà un report nuovo. Il guardiano reale è il report committato in `reports/e05-p1/` + approvazione del coordinatore, non il tool. |
| R31 | LOW | `p1-week12-report.json` write-once ma esito non "una sola volta" | `week12` usa `open("x")`: un `OPERATIONAL_PASS` scritto alla settimana 12 non viene sovrascritto se una corsa successiva dà `FAILED` (drawdown) o `OPERATIONAL_FAIL`. L'output stampato può quindi divergere dal file archiviato. |
| R32 | INFO | Conteggio test nel backlog | La riga E05-P1 del PRD dice "11 test nuovi in tests/test_paper_daily.py"; il file ha 10 test (9 funzioni, una parametrizzata ×2). `session_report` ha 5 test come dichiarato. |
| R33 | INFO | Approvazione DEV-1 inline, nessun artefatto `approvals/` | `requires` dice "recorded in approvals"; nel repo non c'è una directory/file `approvals/` e la nota dell'approvazione dice "approved in chat". Se l'approvazione inline nella DRAFT basta, allineare il testo di `requires` prima della preregistrazione. |
| R34 | INFO | Caso `gross <= 0` non esplicitato nella specifica | Il criterio 3 ("costi ≤ 50% del lordo") con lordo realizzato ≤ 0 dà share `null` e criterio non superato. Il codice è coerente e la guida lo annota nel controllo di fine a fine, ma il testo della specifica non lo dice. |

## 8. Verdetti

| Oggetto | Verdetto |
|---|---|
| `session_report.py` (PRE-7) | **Non approvato così** per la preregistrazione: i calcoli di `edge` sono corretti (prove 5-9) e la scrittura una-volta dell'edge c'è, ma R27 (metrica tempestività), R28 (stop rules), R29 (trigger/`--as-of`), R30/R31 (scrittura) vanno corretti o esplicitati. |
| `session_fidelity.py` (modifica) | **Approvato**: `run_engine` conserva `replay` (prove 4, 9), il buy&hold non usa le feature della candidata (prova 7), le manomissioni restano rilevate (prova 10). |
| Specifica E05-P1 corretta | **Non preregistrabile così**: R21-R25 sono chiusi e DEV-1 è completa, ma restano da allineare software e testo su R27-R29 (e da chiudere i LOW R30/R31 e gli INFO R32-R34). |

## 9. Modifiche richieste prima della preregistrazione

1. **R27 (MEDIUM):** in `session_report.timeliness`, escludere i giorni coperti da un gap dal denominatore di `on_time_share` (i gap restano al criterio 2), oppure, se si intende che i giorni di gap contino nel 95%, scriverlo esplicitamente nel criterio 1 della specifica. Oggi codice e testo divergono.
2. **R28 (MEDIUM):** allineare le regole di stop. Al minimo: (a) far emettere `FAILED` per drawdown < −20% anche a `edge` (o dichiarare che il FAILED è rilevato solo dal run settimanale di `week12`); (b) decidere se le due regole `INVALID` sono controlli software o solo testo e, se software, implementare un esito `INVALID`; (c) fissare `config_sha256` della candidata (`7775ccd4…`) nella specifica e farlo verificare (da `session_report` o dalla procedura di creazione sessione).
3. **R29 (MEDIUM):** o imporre il lunedì 00:00 UTC nel tool (rifiutando esecuzioni fuori orario in produzione), o dichiarare nella specifica che il lunedì è un vincolo operativo del runbook e non del software; in ogni caso rendere `--as-of` incapace di scrivere/bloccare il report reale (es. richiedere un flag esplicito `--test` che non scrive, o rifiutare la scrittura quando `--as-of` è presente).
4. **R30 (LOW):** registrare la decisione `edge` anche dentro la sessione (es. in `meta` o in una tabella) e far verificare `target.exists()` contro quella registrazione, così la cancellazione del file non riabilita la valutazione; in alternativa dichiarare che il guardiano è il report committato + approvazione del coordinatore.
5. **R31 (LOW):** per `week12`, consentire la sovrascrittura (o versionare i report) quando l'esito cambia, oppure documentare che il file è il primo snapshot non-NOT_DUE.
6. **R32-R34 (INFO):** correggere il conteggio "11 test" → 10; allineare `requires` di DEV-1 all'approvazione inline effettiva; esplicitare il caso `gross <= 0` nel criterio 3.

## 10. Nota finale sull'indipendenza

Review eseguita da DeepSeek v4 Pro, stesso modello della review del 13-11 (dichiarato). Tutte le prove sono state eseguite in questa sessione su un clone pulito del commit `450591f`; il ricalcolo indipendente del punto 2 e i probe di manomissione sono stati scritti da zero, senza usare `session_report` né `studio_analytics` per le cifre del registro.
