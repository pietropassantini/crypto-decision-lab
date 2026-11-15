# Prompt per il revisore: correzioni R27-R34 della sessione paper E05-P1 (2026-11-15)

Da incollare a un agente diverso da Claude. È la seconda revisione di differenza su E05-P1: dopo la review di DeepSeek
v4 Pro ([REVIEW-INDEP-2026-11-15-deepseek.md](../REVIEW-INDEP-2026-11-15-deepseek.md), su `450591f`) sono state
corrette `session_report`, la specifica e i testi. Qui si rivede solo quello che è cambiato (`450591f..524a61d`) e si
decide se la specifica può essere preregistrata.

```text
Sei il revisore indipendente del progetto Crypto Decision Lab. Il PRD (§5) richiede che le prove determinanti siano ripetute da un revisore diverso dall'autore. Le correzioni che rivedi sono state scritte da un modello Claude (Opus 5.5) in risposta alla review di DeepSeek v4 Pro. La tua review decide se la specifica della sessione paper su dati nuovi E05-P1 può essere preregistrata.

## Contesto
- Repo: https://github.com/pietropassantini/crypto-decision-lab, branch main, commit 524a61d (HEAD). Ultima revisione indipendente su 450591f.
- Leggi per primi: REVIEW-INDEP-2026-11-15-deepseek.md (rilievi R27-R34 e "Modifiche richieste prima della preregistrazione"), config/experiments/E05-P1-paper-forward-v1.json, docs/E05-P1-paper-forward.md, la riga E05-P1 del Crypto_Decision_Lab_PRD.md.
- Da rivedere: `git diff 450591f..524a61d -- src tests config docs`. In sintesi:
  - src/crypto_decision_lab/session_report.py (riscritto: esclusione dei giorni di gap, stop rule FAILED e INVALID, prova con --as-of, valutazione fino al lunedì di trigger, decisioni registrate nel database della sessione);
  - src/crypto_decision_lab/session_fidelity.py (run_engine con `until`);
  - tests/test_session_report.py;
  - config/experiments/E05-P1-paper-forward-v1.json (candidata fissata per digest, testi dei criteri, regole di stop) e docs/E05-P1-paper-forward.md.
- La specifica è ancora DRAFT: la review precede la preregistrazione.

## Vincoli
- Non leggere e non modificare data/forward/ (perimetro CP08, archivio congelato).
- Nessuna credenziale, nessuna chiamata di rete: i test usano un client HTTP finto. Non eseguire il runner contro il mercato reale.
- Non fare commit, push o modifiche al codice. Se trovi un difetto, lo riproduci e lo documenti; la correzione la decide il coordinatore.
- Non cambiare gli stati del backlog: proponi soltanto.
- Lavora in un clone pulito, in var/review-indep/ (ignorata da Git), con venv nuovo: pip install -e ".[dashboard,dev]".
- Le prove in "produzione" (senza --as-of) scrivono nel database della sessione: eseguile solo su una COPIA della cartella della sessione.

## Cosa fare
1. Suite completa: pytest -q -p no:cacheprovider. Atteso: 608 passed. Riporta ogni scostamento.
2. Chiusura dei rilievi, uno per uno, con la prova. Per ciascuno dì se è chiuso, parzialmente chiuso o aperto, e perché:
   - R27: un giorno con la barra delle 00:00 mancante per un gap riconosciuto non conta contro la puntualità; un giorno mai elaborato sì. Riproduci il caso del revisore precedente (gap a un 00:00, commit tutti puntuali) e prova casi di bordo: gap che copre più giorni, gap non ancora riconosciuto (PAUSED_GAP), gap alla barra 00:00 del primo giorno, gap dopo l'ultimo evento.
   - R28: candidata fissata nella specifica con config_sha256 (7775ccd417e3354c981248cf1469f6cab447844638b88301756fb59c099c5536) e con le feature; INVALID e FAILED nei due rapporti. Prova a produrre un falso INVALID (sessione legittima giudicata non valida) e un falso positivo (sessione diversa giudicata valida). La regola "barra non chiusa" è davvero coperta dal controllo di fedeltà? Se no, dimostra con una prova come si potrebbe violarla senza che nulla se ne accorga.
   - R29: --as-of è una prova che non scrive niente. Verificalo davvero: confronta byte per byte la cartella della sessione (compresi session.sqlite e i file -wal e -shm) prima e dopo una prova, e dì se un rapporto in prova può comunque modificare la sessione (per esempio `PaperSession.__init__` può cambiare lo stato quando il motore o il modello non coincidono). Verifica che il vantaggio sia calcolato fino al lunedì di trigger e che lanciarlo più tardi non cambi le cifre.
   - R30, R31: la decisione sul vantaggio è registrata nel database della sessione; cancellare il file o copiare la cartella non riapre la valutazione; gli esiti della settimana 12 si registrano uno per esito. Cerca altre scappatoie (per esempio cancellare la riga del database, usare un'altra cartella, un altro --spec, aprire la sessione con un altro percorso).
   - R32, R33, R34: conteggio dei test nel backlog, testo di DEV-1 e caso "lordo realizzato non positivo" nella specifica.
3. Il calcolo del lunedì di trigger, riga per riga: `first_monday_at_or_after`, `trigger`, `_until`, `edge_figures(until=...)`, `run_engine(until=...)`. Casi di bordo: l'ottavo trade chiude esattamente di lunedì alle 00:00; chiude di martedì; sessione che parte a metà settimana; più trade chiusi nello stesso giorno; posizione aperta al momento del trigger; il confine `<=` è inclusivo per i record dell'evento delle 00:00 del lunedì? Il buy and hold è troncato allo stesso istante e valutato allo stesso ultimo evento? Il controllo di fedeltà del rapporto usa l'intera sessione mentre le cifre sono troncate: è coerente?
4. Controllo di fine a fine su dati reali (solo snapshot di ricerca, nessuna rete). Crea la candidata congelata in un file, per esempio candidate.json:
   {"strategy": {"name": "daily_trend", "parameters": {"entry_threshold": 0.0, "exit_threshold": 0.0, "regime_min": 0.0}}, "initial_cash": 500, "hypothesis": {"latency_hours": 0, "horizon_hours": 1000000, "fee_rate": 0.001, "slippage": 0.0005}, "risk": {"max_exposure": 0.5, "min_notional": 10, "allow_open": true}}
   python -m crypto_decision_lab.paper session-create --name e05conf --config candidate.json --snapshot BTCUSDT-1h-raw-v1 --start "2025-01-01 00:00" --end "2026-10-01 00:00" --momentum-days 28 --sma-days 100 --root var/review-indep/paper
   python -m crypto_decision_lab.paper session-run --session-dir <cartella>   (circa 1 minuto)
   python -m crypto_decision_lab.session_fidelity --session-dir <cartella>   (atteso: match true, 15.312 eventi, 61.319 record)
   python -m crypto_decision_lab.session_report edge --session-dir <cartella> --as-of "2026-10-01 00:00"   (prova, non scrive)
   Valori attesi, riprodotti dall'autore con la versione corretta: trigger = ottavo trade chiuso il 2025-07-08, lunedì di trigger 2025-07-14 00:00 UTC, ritardo 444 giorni; cifre fino a quel lunedì: 8 trade chiusi, netto realizzato -10,75 (-2,15%), netto dell'equity +1,23%, posizione aperta dal 2025-07-10 con +16,88 non realizzati, buy and hold +13,54%, drawdown -7,28%, costi sul lordo non definiti; esito NOT_CONFIRMED con tre criteri su quattro falliti (realizzato > 0, costi sul lordo, equity contro buy and hold meno 10 punti), solo il drawdown superato. Con --as-of "2025-07-13 23:00" l'esito atteso è NOT_DUE ("due at the Monday"); con --as-of "2025-07-14 00:00" le stesse cifre con ritardo 0. Il rapporto week12 con --as-of "2026-10-01 00:00" dà OPERATIONAL_FAIL con tempestività 0% perché è un replay (l'orologio reale non c'è): è atteso. Ricalcola ogni cifra dal registro senza usare session_report né studio_analytics.
5. Revisione di session_report.py e della specifica, criterio per criterio (confronta il codice con il testo): i quattro criteri del vantaggio, le due regole di stop, il trigger, la puntualità, i codici di uscita (0 per NOT_DUE, OPERATIONAL_PASS, CONFIRMED; 1 per gli altri esiti; 2 per una seconda valutazione sul vantaggio), `load_spec` che rifiuta una specifica incoerente col proprio blocco candidata. Che cosa succede se studio.config_digest o normalize_config cambiano in futuro (il digest nella specifica non corrisponde più)? Il guasto è rumoroso o silenzioso?
6. Che cosa resta solo testo? Elenca, tra le regole della specifica (stop, ripartenze, scadenza a 78 settimane, divieto di attendere una cifra migliore, lunedì obbligatorio), quelle che il software impone, quelle che il software rende verificabili a posteriori, e quelle che restano affidate all'operatore. Per ciascuna dì se è accettabile.
7. Valuta il fatto che il vantaggio sia ora giudicato all'ottavo trade chiuso: nel controllo del punto 4 la candidata, giudicata al 14-07-2025, risulta NOT_CONFIRMED, mentre sull'intera finestra di conferma i 17 trade chiusi valgono -23,9 realizzati. Il criterio è coerente con l'intento dichiarato? È troppo severo, troppo permissivo, troppo sensibile al caso con soli 8 trade?
8. Rilievi ancora aperti dalle review precedenti: R17, R18 (LOW), R13, R14, R19, R20, R26 (INFO): dicci se qualcuno è diventato più rilevante.

## Output
1. Scrivi REVIEW-INDEP-<data>-<modello>.md nella root del repo, senza committarlo: revisore (modello e versione) e commit di base; tabella delle prove (comando, atteso, osservato, esito); rilievi nuovi con ID R35, R36, ... con severità HIGH/MEDIUM/LOW/INFO, oggetto, descrizione e riproduzione minima; per ciascuno di R27-R34 lo stato (chiuso, parziale, aperto); verdetto su session_report, su session_fidelity e sulla specifica E05-P1; l'elenco esatto delle modifiche richieste prima della preregistrazione, oppure "preregistrabile così".
2. Nella risposta finale: un riepilogo breve, i rilievi HIGH/MEDIUM in cima, l'elenco dei file creati.

Ogni affermazione deve poggiare su un comando eseguito o su un file letto. Se una prova non è eseguibile, scrivilo esplicitamente e spiega perché.
```

## Note per il coordinatore

- Il revisore deve dichiarare modello e versione e il commit di base: sono richiesti dal prompt, e senza non posso scrivere la nota di indipendenza del PRD §5. L'ultimo report ricevuto senza queste informazioni non è stato accettato.
- Il controllo del punto 4 lo ho rieseguito io il 2026-11-15 sulla versione corretta; le cifre attese sono quelle riprodotte.
- Conseguenza da pesare (punto 7 del prompt): con la valutazione all'ottavo trade chiuso la candidata, applicata alla finestra di conferma di E05, risulta NOT_CONFIRMED già al 14-07-2025, e non solo sull'intera finestra. È voluto dal rilievo R29 (nessuna attesa di una cifra migliore), ma significa che il vantaggio si giudica presto e su pochi trade.
- Restano anche la macchina sempre accesa (PRE-3) e, dopo la review, la preregistrazione. Il collaudo S07 non serve alla sessione.
- Se riusi uno dei modelli già usati (GLM-5.3 o DeepSeek v4 Pro), dichiaralo nella nota di verifica, come per le review precedenti.
