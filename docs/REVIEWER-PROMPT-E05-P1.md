# Prompt per il revisore: sessione paper E05-P1 (2026-11-15)

Da incollare a un agente diverso da Claude. È una review **di differenza**: `paper.py`, `public_klines.py`,
`session_fidelity.py` (prima versione) ed E01-E05 sono già stati rivisti il 15-11 su `73e7277`
([REVIEW-INDEP-2026-11-15-glm3.md](../REVIEW-INDEP-2026-11-15-glm3.md)). Qui si rivede quello che è cambiato dopo e la
specifica corretta, perché è il passo che precede la preregistrazione.

```text
Sei il revisore indipendente del progetto Crypto Decision Lab. Il PRD (§5) richiede che le prove determinanti siano ripetute da un revisore diverso dall'autore. Il codice e la specifica che rivedi sono stati scritti da modelli Claude (Opus 5.5, Sonnet 5.5, Fable 5.2). La tua review decide se la specifica della sessione paper su dati nuovi E05-P1 può essere preregistrata.

## Contesto
- Repo: https://github.com/pietropassantini/crypto-decision-lab, branch main, commit 450591f (HEAD). L'ultima revisione indipendente è su 73e7277.
- Leggi per primi: Crypto_Decision_Lab_PRD.md (riga E05-P1, nota della revisione del 2026-11-15), REVIEW-INDEP-2026-11-15-glm3.md (rilievi R16-R26 e "Modifiche richieste a E05-P1"), config/experiments/E05-P1-paper-forward-v1.json, docs/E05-P1-paper-forward.md, E05-HANDOFF.yaml.
- Da rivedere: `git diff 73e7277..450591f -- src tests config docs`. In sintesi:
  - src/crypto_decision_lab/session_report.py (NUOVO, strumento dei checkpoint PRE-7) e tests/test_session_report.py;
  - src/crypto_decision_lab/session_fidelity.py (estratta run_engine, riusata per il buy and hold);
  - config/experiments/E05-P1-paper-forward-v1.json e docs/E05-P1-paper-forward.md (corretti secondo R21-R25, deviazione DEV-1 approvata dal coordinatore il 2026-11-15, criterio sul vantaggio sul netto realizzato dei trade chiusi).
- La specifica è ancora DRAFT: la review precede la preregistrazione.

## Vincoli
- Non leggere e non modificare data/forward/ (perimetro CP08, archivio congelato).
- Nessuna credenziale, nessuna chiamata di rete a exchange o servizi di mercato: i test usano un client HTTP finto. Non eseguire il runner contro il mercato reale.
- Non fare commit, push o modifiche al codice. Se trovi un difetto, lo riproduci e lo documenti; la correzione la decide il coordinatore.
- Non cambiare gli stati del backlog: proponi soltanto.
- Lavora in un clone pulito, in var/review-indep/ (ignorata da Git), con venv nuovo: pip install -e ".[dashboard,dev]".

## Cosa fare
1. Suite completa: pytest -q -p no:cacheprovider. Atteso: 602 passed. Riporta ogni scostamento.
2. Controllo di fine a fine su dati reali (solo snapshot di ricerca, nessuna rete). Crea la candidata congelata in un file, per esempio candidate.json:
   {"strategy": {"name": "daily_trend", "parameters": {"entry_threshold": 0.0, "exit_threshold": 0.0, "regime_min": 0.0}}, "initial_cash": 500, "hypothesis": {"latency_hours": 0, "horizon_hours": 1000000, "fee_rate": 0.001, "slippage": 0.0005}, "risk": {"max_exposure": 0.5, "min_notional": 10, "allow_open": true}}
   Poi:
   python -m crypto_decision_lab.paper session-create --name e05conf --config candidate.json --snapshot BTCUSDT-1h-raw-v1 --start "2025-01-01 00:00" --end "2026-10-01 00:00" --momentum-days 28 --sma-days 100 --root var/review-indep/paper
   python -m crypto_decision_lab.paper session-run --session-dir <cartella indicata dal comando precedente>   (circa 1 minuto)
   python -m crypto_decision_lab.session_fidelity --session-dir <cartella>
   python -m crypto_decision_lab.session_report edge --session-dir <cartella> --as-of "2026-10-01 00:00"
   Atteso (riprodotto dall'autore): sessione COMPLETED con 15.312 eventi; fidelity match true con 61.319 record; il registro coincide con quello dell'esperimento E05 (data/experiments/E05-daily-trend-v1/ledgers/confirmation-g10.json dopo `session_fidelity.normalized`, oppure riesegui E05 daily_experiment per ottenerlo); edge: 17 trade chiusi, netto realizzato -23,93 (-4,79%), netto dell'equity +4,98%, posizione aperta dal 2026-08-20 con +48,84 non realizzati, buy and hold -5,35% (uguale alla baseline di E05), drawdown -9,33%, costi sul lordo non definiti (lordo realizzato <= 0), esito NOT_CONFIRMED. Verifica ogni cifra con un ricalcolo tuo dal registro, senza usare session_report.
3. Revisione di session_report.py contro la specifica E05-P1 (confronta il codice con il testo, criterio per criterio):
   - week12: tempestività (status_log.at della riga "last event <barra>" contro la barra delle 00:00, entro 2 ore, almeno 95% dei giorni), giorni mancanti non spiegati da un gap, fedeltà, drawdown, regola di stop. Il calcolo dei giorni attesi è corretto ai bordi (inizio sessione non a mezzanotte, as_of a ridosso, gap riconosciuti)? Una poll in ritardo che elabora più barre insieme è misurata barra per barra?
   - edge: trade chiusi contro posizione aperta (studio_analytics.trades), costi e lordo dei soli trade chiusi, buy and hold = strategia constant LONG dello stesso motore, con capitale, esposizione, fee e slippage della candidata, sulle barre registrate. La definizione nel codice coincide con quella della specifica? Il drawdown dell'equity include la posizione aperta come da specifica?
   - la valutazione "una sola volta": p1-edge-report.json è scritto una sola volta e una seconda chiamata esce con 2. Si può aggirare (cancellare il file, cambiare --as-of, cartella diversa)? Che cosa fa la specifica per questi casi e basta?
   - il trigger "primo lunedì con almeno 8 trade chiusi": lo strumento lo impone o lo lascia all'operatore? Dipende solo dal numero di trade e mai dal risultato? Sono possibili forme di "optional stopping"?
   - le regole di stop della specifica (drawdown sotto -20%, decisione presa su una barra non chiusa, modifica della candidata) sono tutte realmente controllate dal software, o alcune restano solo testo? Elencale.
   - esiti e codici di uscita coerenti con la specifica; NOT_DUE non scrive nulla.
4. Revisione della specifica E05-P1 corretta (config/experiments/E05-P1-paper-forward-v1.json e la guida):
   - verifica che R21-R25 del review precedente siano davvero chiusi, uno per uno, e che non siano comparse nuove ambiguità;
   - il criterio sul vantaggio sul netto realizzato (equity riportata accanto) è coerente con il resto e non è né troppo permissivo né inutilmente severo? Nota che, applicato alla finestra di conferma di E05 (controllo di fine a fine al punto 2), darebbe NOT_CONFIRMED: giudica se è la conseguenza voluta;
   - ripartenze (al massimo 2, scadenza a 78 settimane) e "valutazione una sola volta": ci sono scappatoie?
   - DEV-1 (prezzi reali pubblici invece del testnet): è descritta in modo completo, con approvazione registrata? Che cosa si perde rispetto al testnet (fill reali, fee reali) e dove è dichiarato?
   - quello che non è coperto: lo specchio facoltativo su testnet (S07) calcola ancora solo momentum_24h.
5. Revisione di session_fidelity.py (modifica): run_engine produce lo stesso registro di prima per replay(); il buy and hold non usa le feature della candidata; le manomissioni già provate (ordine, fill, saldo, barra) sono ancora rilevate.
6. Rilievi ancora aperti dalle review precedenti: R17, R18 (LOW), R13, R14, R19, R20, R26 (INFO): dicci se qualcuno è diventato più rilevante con la specifica corretta.

## Output
1. Scrivi REVIEW-INDEP-<data>-<modello>.md nella root del repo, senza committarlo: revisore (modello e versione) e commit di base; tabella delle prove (comando, atteso, osservato, esito); rilievi nuovi con ID R27, R28, ... con severità HIGH/MEDIUM/LOW/INFO, oggetto, descrizione e riproduzione minima; verdetto su session_report, su session_fidelity (modifica) e sulla specifica E05-P1; l'elenco esatto delle modifiche richieste prima della preregistrazione, oppure "preregistrabile così".
2. Nella risposta finale: un riepilogo breve, i rilievi HIGH/MEDIUM in cima, l'elenco dei file creati.

Ogni affermazione deve poggiare su un comando eseguito o su un file letto. Se una prova non è eseguibile, scrivilo esplicitamente e spiega perché.
```

## Note per il coordinatore

- È una review di differenza: il perimetro è `73e7277..450591f` e la specifica corretta. Il resto è già stato rivisto.
- Il controllo di fine a fine del punto 2 l'ho eseguito io il 2026-11-15 (replay della finestra di conferma di E05): le cifre attese sono quelle riprodotte.
- Conseguenza da pesare: con i criteri di E05-P1 sul netto realizzato, la finestra di conferma di E05 avrebbe dato NOT_CONFIRMED (realizzato −23,9). È voluto (rilievo R16), ma significa che la candidata dovrà fare meglio, sui dati nuovi, di quanto ha fatto in backtest.
- Anche dopo questa review restano la macchina sempre accesa (PRE-3) e la preregistrazione. Il collaudo S07 non serve alla sessione.
- Se riusi lo stesso modello delle altre review (GLM-5.3), dichiaralo nella nota di verifica come per il 14 e 15-11.
