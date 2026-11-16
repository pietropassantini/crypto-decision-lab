# Prompt per il revisore: correzioni R35-R43 della sessione paper E05-P1 (2026-11-16)

Da incollare a un agente diverso da Claude. È la terza revisione di differenza su E05-P1: dopo la review di Codex
([REVIEW-INDEP-2026-11-15-codex.md](../REVIEW-INDEP-2026-11-15-codex.md), su `524a61d`) sono stati riscritti il
modo in cui i rapporti leggono la sessione, il controllo di fedeltà e le regole di ammissione e di decisione. Qui si
rivede solo quello che è cambiato (`524a61d..7bf5b49`) e si decide se la specifica può essere preregistrata.

```text
Sei il revisore indipendente del progetto Crypto Decision Lab. Il PRD (§5) richiede che le prove determinanti siano ripetute da un revisore diverso dall'autore. Le correzioni che rivedi sono state scritte da un modello Claude (Opus 5.5) in risposta alla review di OpenAI Codex. La tua review decide se la specifica della sessione paper su dati nuovi E05-P1 può essere preregistrata.

## Contesto
- Repo: https://github.com/pietropassantini/crypto-decision-lab, branch main, commit 7bf5b49 (HEAD). Ultima revisione indipendente su 524a61d.
- Leggi per primi: REVIEW-INDEP-2026-11-15-codex.md (rilievi R35-R43, "Trigger e corrispondenza codice-specifica", "Controlli software e responsabilità operative", "Modifiche richieste"), config/experiments/E05-P1-paper-forward-v1.json, docs/E05-P1-paper-forward.md, la riga E05-P1 del Crypto_Decision_Lab_PRD.md.
- Da rivedere: `git diff 524a61d..7bf5b49 -- src tests config docs`. In sintesi:
  - src/crypto_decision_lab/session_snapshot.py (NUOVO: lettura consistente della sessione in sola lettura, taglio a un istante);
  - src/crypto_decision_lab/session_fidelity.py (riscritto sopra l'istantanea; il replay riconosce i gap come il runner);
  - src/crypto_decision_lab/session_report.py (riscritto: ammissione, prontezza, scadenza, stop, decisione atomica);
  - tests/test_session_report.py;
  - config/experiments/E05-P1-paper-forward-v1.json (dati di ammissione, regole di stop, definizioni del vantaggio, procedura) e docs/E05-P1-paper-forward.md.
- La specifica è ancora DRAFT: la review precede la preregistrazione.

## Vincoli
- Non leggere e non modificare data/forward/ (perimetro CP08, archivio congelato).
- Nessuna credenziale, nessuna chiamata di rete: i test usano un client HTTP finto. Non eseguire il runner contro il mercato reale.
- Non fare commit, push o modifiche al codice. Se trovi un difetto, lo riproduci e lo documenti; la correzione la decide il coordinatore.
- Non cambiare gli stati del backlog: proponi soltanto.
- Lavora in un clone pulito, in var/review-indep/ (ignorata da Git), con venv nuovo: pip install -e ".[dashboard,dev]". Se l'ambiente non permette di installare le dipendenze (per esempio PyArrow o pytest), dillo esplicitamente, descrivi come hai ripiegato e che cosa resta non verificato: la volta scorsa la suite non è stata eseguita e il percorso canonico non è stato ripetuto.
- Le prove in "produzione" (senza --as-of) scrivono nel database della sessione: eseguile solo su una COPIA della cartella della sessione.
- Dichiara nel report modello, versione (o che non è esposta) e commit di base.

## Cosa fare
1. Suite completa: pytest -q -p no:cacheprovider. Atteso: 615 passed. Riporta ogni scostamento.
2. Chiusura dei rilievi di Codex, uno per uno, con la prova. Per ciascuno dì se è chiuso, parzialmente chiuso o aperto, e perché. Cerca di violare ogni correzione, non limitarti a rieseguire i test dell'autore:
   - R35 (ammissione in produzione): simbolo, host, profondità del warmup, modalità, tipo di fonte, specifica preregistrata con data, avvio dopo la preregistrazione, digest ricalcolato dalla configurazione memorizzata. Rimane qualcosa di non controllato? Che cosa impedisce di passare `--spec` con una specifica diversa in produzione (il rapporto stampa `spec_sha256`, il confronto col registro è dell'operatore)? Dì se è accettabile.
   - R36 (prontezza): nessuna decisione finché la barra del lunedì non è registrata. Riproduci la sequenza del revisore precedente (arresto a domenica 23:00, rapporto al lunedì 00:00, poi la barra registrata). Che cosa succede se il lunedì è coperto da un gap riconosciuto, da un gap non riconosciuto, se la sessione è COMPLETED prima del lunedì?
   - R37 (scadenza): applicata al trigger, anche con ottavo trade prima della scadenza e lunedì dopo. Prova i bordi (scadenza esattamente sul lunedì, valori frazionari).
   - R38 (nessuna scrittura, nessuna rete): i rapporti non costruiscono PaperSession, non aprono la fonte, non chiedono l'orologio. Verifica che una prova (--as-of) lasci identiche tutte le tabelle e controlla anche i file -wal e -shm, con sessione chiusa, aperta in WAL e con digest del motore diverso. La connessione in sola lettura funziona con sessioni in WAL, in cartelle senza permesso di scrittura, su Windows e su Linux?
   - R39 (stop sul drawdown): FAILED su tutto ciò che si vede fino al rapporto, criterio del vantaggio sulle cifre del trigger. Riproduci un crollo dell'equity dopo il trigger.
   - R40 (decisione atomica): indice univoco, inserimento atomico, file ricreato dal database. Prova due processi che valutano insieme (anche con sleep forzati), un crash tra inserimento e file, la cancellazione della riga del database, la copia anteriore alla decisione. Che cosa resta aggirabile e dove la specifica lo dichiara (registro ufficiale)?
   - R41 (gap): il replay riconosce il gap prima di far girare la barra successiva. Riproduci: gap di 1 ora e di più ore, gap con ordine pendente, gap mentre si è long, gap all'ultima barra, due gap consecutivi, taglio dentro un gap (`SessionSnapshot.until`). Il controllo continua a rilevare le manomissioni già provate (ordine, fill, saldo, barra, prev_close)? La parte "il formato persistito non prova il momento in cui il close era disponibile" è dichiarata come limite nella specifica: giudica se la formulazione è onesta e sufficiente.
   - R42 (lettura coerente): una sola transazione di lettura. Fai scrivere il runner (o un processo che inserisce eventi) mentre i rapporti leggono, e verifica che le parti restino coerenti.
   - R43 (prova per un istante passato): nessun dato successivo all'istante richiesto, in edge e in week12 (compresa la griglia dei giorni, la puntualità, le righe di status_log).
3. Il taglio all'istante: `SessionSnapshot.until`, compresa la regola che toglie in coda i record di ripresa dopo un gap quando il taglio cade dentro il gap. È corretta? Può togliere record che non dovrebbe o lasciarne che non dovrebbe?
4. Controllo di fine a fine su dati reali (solo snapshot di ricerca, nessuna rete). Crea la candidata congelata in un file, per esempio candidate.json:
   {"strategy": {"name": "daily_trend", "parameters": {"entry_threshold": 0.0, "exit_threshold": 0.0, "regime_min": 0.0}}, "initial_cash": 500, "hypothesis": {"latency_hours": 0, "horizon_hours": 1000000, "fee_rate": 0.001, "slippage": 0.0005}, "risk": {"max_exposure": 0.5, "min_notional": 10, "allow_open": true}}
   python -m crypto_decision_lab.paper session-create --name e05conf --config candidate.json --snapshot BTCUSDT-1h-raw-v1 --start "2025-01-01 00:00" --end "2026-10-01 00:00" --momentum-days 28 --sma-days 100 --root var/review-indep/paper
   python -m crypto_decision_lab.paper session-run --session-dir <cartella>   (circa 1 minuto)
   python -m crypto_decision_lab.session_fidelity --session-dir <cartella>   (atteso: match true, 15.312 eventi, 61.319 record)
   python -m crypto_decision_lab.session_report edge --session-dir <cartella> --as-of "<istante>"   (prova, non scrive)
   Valori attesi, riprodotti dall'autore con la versione corretta:
   - --as-of "2025-07-13 23:00": NOT_DUE ("due at the Monday"), 8 trade chiusi, equity +0,98%, buy and hold +13,23%, ritardo -0,04 giorni (cifre fino a quell'istante, non oltre);
   - --as-of "2025-07-14 00:00": NOT_CONFIRMED, 8 trade chiusi, netto realizzato -10,75 (-2,15%), equity +1,23%, buy and hold +13,54%, drawdown -7,28%, ritardo 0, fedeltà al cutoff e alla data true;
   - --as-of "2026-10-01 00:00": le stesse cifre del 14 luglio, ritardo 444 giorni, drawdown di stop -9,33%.
   Il rapporto week12 con --as-of "2026-10-01 00:00" dà OPERATIONAL_FAIL con tempestività 0% perché è un replay (nessun orologio reale): atteso. Ricalcola le cifre dal registro senza usare session_report né studio_analytics.
5. Revisione di session_report.py e della specifica, criterio per criterio (confronta codice e testo): i quattro criteri del vantaggio, le regole di stop, il trigger, la puntualità, i codici di uscita (0 per NOT_DUE, OPERATIONAL_PASS, CONFIRMED; 1 per gli altri esiti; 2 per una seconda valutazione sul vantaggio, anche in concorrenza). Il testo della specifica e quello della guida dicono la stessa cosa del codice?
6. Che cosa resta solo testo o affidato all'operatore? Elenca, tra le regole della specifica (ripartenze, scadenza a 78 settimane, divieto di attendere una cifra migliore, lunedì obbligatorio, registro ufficiale della sessione, copie della cartella prima della decisione), quelle che il software impone, quelle che rende verificabili a posteriori e quelle affidate all'operatore. Per ciascuna dì se è accettabile per iniziare la sessione.
7. Rilievi ancora aperti dalle review precedenti: R17, R18 (LOW), R13, R14, R19, R20, R26 (INFO): dicci se qualcuno è diventato più rilevante.

## Output
1. Scrivi REVIEW-INDEP-<data>-<modello>.md nella root del repo, senza committarlo: revisore (modello e versione) e commit di base; tabella delle prove (comando, atteso, osservato, esito); rilievi nuovi con ID R44, R45, ... con severità HIGH/MEDIUM/LOW/INFO, oggetto, descrizione e riproduzione minima; per ciascuno di R35-R43 lo stato (chiuso, parziale, aperto); verdetto su session_snapshot, session_report, session_fidelity e sulla specifica E05-P1; l'elenco esatto delle modifiche richieste prima della preregistrazione, oppure "preregistrabile così".
2. Nella risposta finale: un riepilogo breve, i rilievi HIGH/MEDIUM in cima, l'elenco dei file creati.

Ogni affermazione deve poggiare su un comando eseguito o su un file letto. Se una prova non è eseguibile, scrivilo esplicitamente e spiega perché.
```

## Note per il coordinatore

- È il terzo giro: la specifica è cambiata molto, quindi il revisore ha un perimetro ampio. Se vuoi limitarlo, la parte che conta davvero è il punto 2 (R35, R36, R41 sono quelli ad alto impatto) e il punto 4.
- Il controllo del punto 4 lo ho rieseguito io il 2026-11-16 con la versione corretta: le cifre attese sono quelle riprodotte. La prova delle 23:00 della domenica ora si ferma a quell'istante (prima mostrava 17 trade).
- Il report precedente di Codex dichiarava limiti di ambiente (niente suite, niente PyArrow). Nel prompt ho chiesto di descrivere esplicitamente cosa resta non verificato: la suite l'ho eseguita io (615 passati).
- Restano la macchina sempre accesa (PRE-3), il registro ufficiale della sessione tenuto da te (identificativo della sessione, sha256 della specifica, ripartenze, scadenza a 78 settimane) e, dopo la review, la preregistrazione.
- Se riusi uno dei modelli già usati (GLM-5.3, DeepSeek v4 Pro, Codex), dichiaralo nella nota di verifica come per le altre review.
