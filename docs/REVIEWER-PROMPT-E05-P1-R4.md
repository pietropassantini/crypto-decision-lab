# Prompt per il revisore: correzioni R44-R50 della sessione paper E05-P1 (2026-11-16)

Da incollare a un agente diverso da Claude. È la quarta revisione di differenza su E05-P1: dopo la review di OpenAI
Codex gpt-6-astra ([REVIEW-INDEP-2026-11-16-gpt-6-astra.md](../REVIEW-INDEP-2026-11-16-gpt-6-astra.md), su
`7bf5b49`) sono stati corretti il replay dei gap riconosciuti, la lettura della specifica, l'esportazione dei rapporti,
l'ammissione e il taglio a un istante. Qui si rivede solo quello che è cambiato (`7bf5b49..88a79e9`) e si decide se la
specifica può essere preregistrata.

```text
Sei il revisore indipendente del progetto Crypto Decision Lab. Il PRD (§5) richiede che le prove determinanti siano ripetute da un revisore diverso dall'autore. Le correzioni che rivedi sono state scritte da un modello Claude (Opus 5.5) in risposta alla review di OpenAI Codex gpt-6-astra. La tua review decide se la specifica della sessione paper su dati nuovi E05-P1 può essere preregistrata.

## Contesto
- Repo: https://github.com/pietropassantini/crypto-decision-lab, branch main, commit 88a79e9 (HEAD). Ultima revisione indipendente su 7bf5b49.
- Leggi per primi: REVIEW-INDEP-2026-11-16-gpt-6-astra.md (rilievi R44-R50, "Chiusura di R35-R43", "Taglio e fedeltà, caso per caso", "Regole affidate al coordinatore", "Verdetti"), config/experiments/E05-P1-paper-forward-v1.json, docs/E05-P1-paper-forward.md, la riga E05-P1 del Crypto_Decision_Lab_PRD.md.
- Da rivedere: `git diff 7bf5b49..88a79e9 -- src tests config docs`. In sintesi:
  - src/crypto_decision_lab/session_snapshot.py: `until()` non toglie più il riconoscimento di un gap in coda, ricalcola il cursore, porta lo stato a None e taglia status_log su `at` e su `clock`; la lettura riprova in modalità `immutable` solo se manca il file -wal;
  - src/crypto_decision_lab/session_fidelity.py: dopo l'ultima barra, il replay riproduce un riconoscimento di gap non ancora seguito dalla ripresa usando `resumed_at` del record (anche se segue `end`);
  - src/crypto_decision_lab/session_report.py: `read_spec` (specifica e sha256 da una sola lettura), ammissione sui valori in effetto della fonte (`identity`) e date di preregistrazione non valide, `_publish` (file temporaneo e rename), `export_decision` (file sempre ricostruito dalla riga del database, anche per week12);
  - tests/test_session_report.py (5 test nuovi, 3 aggiornati);
  - config/experiments/E05-P1-paper-forward-v1.json e docs/E05-P1-paper-forward.md (regola di stop sulla fedeltà riscritta per R47, contratto di lettura e file ausiliari per R48, ammissione per R49, taglio in tempo di mercato per R50, esportazione per R46).
- La specifica è ancora DRAFT: la review precede la preregistrazione.

## Vincoli
- Non leggere e non modificare data/forward/ (perimetro CP08, archivio congelato).
- Nessuna credenziale, nessuna chiamata di rete: i test usano un client HTTP finto. Non eseguire il runner contro il mercato reale.
- Non fare commit, push o modifiche al codice. Se trovi un difetto, lo riproduci e lo documenti; la correzione la decide il coordinatore.
- Non cambiare gli stati del backlog: proponi soltanto.
- Lavora in un clone pulito, in var/review-indep/ (ignorata da Git), con venv nuovo: pip install -e ".[dashboard,dev]". Le ultime due review NON hanno potuto eseguire la suite né il replay con PyArrow: questa volta è il punto più importante. Se l'ambiente non lo permette, dillo nella prima riga del report, descrivi come hai ripiegato e che cosa resta non verificato.
- Le prove in "produzione" (senza --as-of) scrivono nel database della sessione: eseguile solo su una COPIA della cartella della sessione.
- Dichiara nel report modello, versione (o che non è esposta) e commit di base.

## Cosa fare
1. Suite completa: pytest -q -p no:cacheprovider. Atteso: 620 passed (circa 9 minuti). Riporta ogni scostamento, con l'output.
2. Chiusura dei rilievi di gpt-6-astra, uno per uno, con la prova. Per ciascuno dì se è chiuso, parzialmente chiuso o aperto, e perché. Cerca di violare ogni correzione, non limitarti a rieseguire i test dell'autore:
   - R44 (riconoscimento prima della ripresa): riproduci le quattro fasi (ack senza poll; ack, poi poll e ripresa; ack seguito da finish; taglio --as-of dentro il gap e subito dopo l'ack). Il rapporto edge in produzione su una sessione in quello stato deve dare NOT_DUE e non scrivere niente. Prova anche: ack con ordine pendente cancellato, ack mentre si è long, due gap con il secondo riconosciuto e non ripreso, ack sul lunedì del trigger. Il replay della coda può nascondere una manomissione (per esempio un `resumed_at` falsificato, un ack inserito a mano senza gap reale, un ack dopo `end`)?
   - R45 (sha256 della specifica): stessi byte per calcolo, output e decisione registrata. Cambia il file durante il rapporto; prova CRLF/LF e BOM: lo sha resta stabile e coerente con quello che il coordinatore calcolerebbe sul file del commit di preregistrazione? Dì quale comando deve usare il coordinatore per ottenere lo stesso valore.
   - R46 (esportazione): file troncato, modificato a mano, cancellato, per edge e per week12; due esportatori contemporanei (anche con sleep forzati) su Windows e su Linux; nessun file temporaneo lasciato. Il file è sempre identico alla riga del database?
   - R47 (testo della regola di stop): la specifica e la guida ora dicono che la fedeltà confronta valori memorizzati e non prova quando un close era disponibile. È onesto e sufficiente? È coerente con la sezione delle minacce?
   - R48 (sola lettura): cartella senza permesso di scrittura con e senza -wal, su Linux e su Windows se possibile. La modalità `immutable` è usata solo quando è sicura? Il messaggio d'errore con il -wal presente è chiaro?
   - R49 (ammissione): sessione creata via API con warmup di default, valori scritti che contraddicono la fonte, `identity` con kind diverso, date "NaT", "not a date", vuote, con fuso. Rimane un campo controllato sul valore dichiarato invece che su quello in effetto?
   - R50 (taglio in tempo di mercato): eventi, registro e storia sull'orario della barra, status_log su `at` e `clock`. Il cursore e lo stato del taglio sono coerenti con quello che il runner aveva a quell'istante? Week12 con --as-of usa solo righe ≤ istante?
3. Coerenza fra prova e produzione: per gli stessi dati, un rapporto --as-of e un rapporto di produzione lanciato a quell'istante danno lo stesso esito (a parte la scrittura)? Cerca una sequenza in cui differiscono.
4. Controllo di fine a fine su dati reali (solo snapshot di ricerca, nessuna rete). Crea la candidata congelata in un file, per esempio candidate.json:
   {"strategy": {"name": "daily_trend", "parameters": {"entry_threshold": 0.0, "exit_threshold": 0.0, "regime_min": 0.0}}, "initial_cash": 500, "hypothesis": {"latency_hours": 0, "horizon_hours": 1000000, "fee_rate": 0.001, "slippage": 0.0005}, "risk": {"max_exposure": 0.5, "min_notional": 10, "allow_open": true}}
   python -m crypto_decision_lab.paper session-create --name e05conf --config candidate.json --snapshot BTCUSDT-1h-raw-v1 --start "2025-01-01 00:00" --end "2026-10-01 00:00" --momentum-days 28 --sma-days 100 --root var/review-indep/paper
   python -m crypto_decision_lab.paper session-run --session-dir <cartella>   (circa 1 minuto)
   python -m crypto_decision_lab.session_fidelity --session-dir <cartella>   (atteso: match true, 15.312 eventi, 61.319 record)
   python -m crypto_decision_lab.session_report edge --session-dir <cartella> --as-of "<istante>"   (prova, non scrive)
   Valori attesi, riprodotti dall'autore con 88a79e9 (invariati rispetto alla review precedente):
   - --as-of "2025-07-13 23:00": NOT_DUE, 8 trade chiusi, equity +0,98%, buy and hold +13,23%;
   - --as-of "2025-07-14 00:00": NOT_CONFIRMED, 8 trade chiusi, netto realizzato -10,75 (-2,15%), equity +1,23%, buy and hold +13,54%, drawdown -7,28%, ritardo 0, fedeltà al cutoff e alla data true;
   - --as-of "2026-10-01 00:00": le stesse cifre del 14 luglio, ritardo 444 giorni, drawdown di stop -9,33%.
   Lo spec_sha256 stampato deve essere lo stesso in tutti i rapporti. Ricalcola le cifre dal registro senza usare session_report né studio_analytics.
5. Revisione di session_report.py e della specifica, criterio per criterio, solo dove il diff li tocca: il testo della specifica e quello della guida dicono la stessa cosa del codice? In particolare: "dopo la decisione sul vantaggio il comando edge non ricalcola più niente, gli stop li segue week12".
6. Regole affidate al coordinatore: la tabella della review precedente è ancora esatta dopo le correzioni? Che cosa deve contenere al minimo il registro ufficiale della sessione prima dell'avvio (identificativo, sha256 della specifica, data di preregistrazione, commit del runner, termine a 78 settimane, ripartenze)?
7. Rilievi ancora aperti dalle review precedenti: R17, R18 (LOW), R13, R14, R19, R20, R26 (INFO): dicci se qualcuno è diventato più rilevante.

## Output
1. Scrivi REVIEW-INDEP-<data>-<modello>.md nella root del repo, senza committarlo: revisore (modello e versione) e commit di base; ambiente (sistema operativo, Python, PyArrow, se la suite è stata eseguita); tabella delle prove (comando, atteso, osservato, esito); rilievi nuovi con ID R51, R52, ... con severità HIGH/MEDIUM/LOW/INFO, oggetto, descrizione e riproduzione minima; per ciascuno di R44-R50 lo stato (chiuso, parziale, aperto); verdetto su session_snapshot, session_report, session_fidelity e sulla specifica E05-P1; l'elenco esatto delle modifiche richieste prima della preregistrazione, oppure "preregistrabile così".
2. Nella risposta finale: un riepilogo breve, i rilievi HIGH/MEDIUM in cima, l'elenco dei file creati.

Ogni affermazione deve poggiare su un comando eseguito o su un file letto. Se una prova non è eseguibile, scrivilo esplicitamente e spiega perché.
```

## Note per il coordinatore

- Il diff è piccolo rispetto al giro precedente: le parti che contano sono il punto 1 (la suite, mai eseguita da un
  revisore negli ultimi due giri), il punto 2 su R44 e R46 e il punto 4.
- Se il revisore non riesce di nuovo a eseguire la suite, conviene che la esegua tu nel tuo terminale e alleghi
  l'output alla nota di verifica: `python -m pytest -q -p no:cacheprovider` (atteso 620 passed).
- Il controllo del punto 4 l'ho rieseguito io il 2026-11-16 su `88a79e9`: cifre identiche alla versione precedente.
- Se possibile usa un modello non ancora usato su E05-P1 (finora: GLM-5.3, DeepSeek v4 Pro, Codex, gpt-6-astra);
  se ne riusi uno, dichiaralo nella nota di verifica.
- Dopo una review senza rilievi HIGH/MEDIUM restano: il registro ufficiale della sessione, la preregistrazione con il
  commit del runner e la macchina sempre accesa (PRE-3).
