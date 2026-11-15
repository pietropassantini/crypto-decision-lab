# Prompt per il revisore indipendente (2026-11-15)

Da incollare a un agente diverso da Claude (Codex, GLM, DeepSeek). Sostituisce nei fatti
[REVIEWER-PROMPT.md](REVIEWER-PROMPT.md), che è fermo al commit 53f15ef. Il PRD (§5) richiede che le
prove determinanti siano ripetute da un revisore diverso dall'autore.

```text
Sei il revisore indipendente del progetto Crypto Decision Lab. Il PRD (§5) richiede che le prove determinanti siano ripetute da un revisore diverso dall'autore. Gli esperimenti E01, E03, E04, E05, il protocollo condiviso e il codice della sessione paper (E05-P1) sono stati scritti ed eseguiti da modelli Claude (Opus 5.5, Sonnet 5.5, Fable 5.2): la tua review è quella che conta per il §5. E05 ha dato esito CANDIDATE, quindi la tua review decide se può essere usato come base di una sessione paper su dati nuovi.

## Contesto
- Repo: https://github.com/pietropassantini/crypto-decision-lab, branch main, commit 73e7277 (HEAD).
- Leggi per primi: Crypto_Decision_Lab_PRD.md (backlog, §5 indipendenza, righe E01-E05 ed E05-P1), E01/E03/E04/E05-HANDOFF.yaml, docs/E03-regime-momentum.md, docs/E04-mean-reversion.md, docs/E05-daily-trend.md, docs/E05-P1-paper-forward.md, REVIEW-INDEP-2026-11-14-glm2.md (review precedente, per il contesto).
- Da rivedere (stato oggi IN_REVIEW): E01, E03, E04, E05.
- Da rivedere come codice nuovo: src/crypto_decision_lab/experiment_protocol.py (protocollo a due fasi condiviso da E04 in poi, con minimi di trade per esperimento), daily_trend.py, mean_reversion.py, regime_momentum.py, turnover_experiment.py, regime_experiment.py, reversion_experiment.py, daily_experiment.py.
- Da rivedere come modifica a uno step già VERIFIED (S06): src/crypto_decision_lab/paper.py (parametro features, sorgente public_klines, DATA_ERROR su errori della fonte, _feed_clock), più public_klines.py e session_fidelity.py.
- Bozza (non preregistrata) da valutare nel merito: config/experiments/E05-P1-paper-forward-v1.json.

## Vincoli
- Non leggere e non modificare data/forward/ (perimetro CP08, archivio congelato).
- Nessuna credenziale, nessuna chiamata di rete a exchange o a servizi di mercato: i test usano un client HTTP finto. Non eseguire il runner contro il mercato reale; verifica le chiamate Binance (endpoint, parametri, limiti) leggendo il codice e la documentazione ufficiale.
- Non fare commit, push o modifiche al codice. Se trovi un difetto, lo riproduci e lo documenti; la correzione la decide il coordinatore.
- Non cambiare gli stati del backlog: proponi soltanto.
- Lavora in un clone pulito, in var/review-indep/ (ignorata da Git), con venv nuovo: pip install -e ".[dashboard,dev]".

## Cosa fare
1. Suite completa: pytest -q -p no:cacheprovider. Atteso: 597 passed. Riporta ogni scostamento.
2. Ripeti gli esperimenti senza fidarti degli handoff. Per ciascuno: verifica lo sha256 della specifica (fine riga LF) contro la riga del backlog, esegui le fasi develop, evaluate e poi la fase all in una cartella diversa (--output-dir) e confronta i ledger_sha256.
   - E01: python -m crypto_decision_lab.turnover_experiment {develop,evaluate,all} --spec config/experiments/S09-E01-turnover-reduction-v1.json. Spec d5527a6a9ab79a2acc22ae19358b8dcd66d1c4aa0cc32c0d63048f9d01ecc47f. Atteso: selezionata g11, REJECTED, 17 ledger_sha256 identici.
   - E03: python -m crypto_decision_lab.regime_experiment ... --spec config/experiments/E03-regime-momentum-v1.json. Spec 41ddad98e6ee209a3b458bb4262c56910fb722b6f85838ea5ecec5be0af65b92. Atteso: g09, REJECTED, 23 ledger.
   - E04: python -m crypto_decision_lab.reversion_experiment ... --spec config/experiments/E04-mean-reversion-v1.json. Spec 9aecd5c0a95c4cd358b970baeee759299712a783017646cb1ebcc6d7eb792ddc. Atteso: g05, REJECTED, 20 ledger.
   - E05: python -m crypto_decision_lab.daily_experiment ... --spec config/experiments/E05-daily-trend-v1.json. Spec 7054becb66c96ea0bebd77d6815ef407834bcf8020a62680a5c514a0a9ffce83. Atteso: g10, CANDIDATE (validazione +70,87% con 17 trade, drawdown -13,08%; conferma +4,98% con 17 trade), 20 ledger.
   Nota: i ledger_sha256 dipendono dall'engine_sha256; sullo stesso commit devono coincidere con reports/e0X/report.json, tranne engine_sha256 (cambia a ogni strategia registrata in simulation.py).
3. Ordine di preregistrazione (git log): per ogni esperimento la specifica PREREGISTERED deve precedere ogni lettura di dati, e selection.json (reports/eNN/selection.json, copia byte per byte di quello scritto dal runner) deve essere nel repository PRIMA della fase evaluate. Ricostruisci la sequenza dai commit (E01: 94cd1d7, ca74636, c1c87a0; E03: 4476553, afc21f4, e318fc3; E04: f11dfe3, b32db6a, 69a39f5; E05: 3b7a42c, 32d137b, 1ef1a1e) e segnala ogni violazione.
4. Caccia a data leakage e a errori di protocollo, la parte più importante:
   - le feature (momentum_24h, trend_ratio, zscore, d_momentum, d_trend) usano solo barre chiuse prima dell'evento? Prova a violarle: altera la barra corrente e quelle future e verifica che le feature non cambino; cerca confronti con barre dopo un gap.
   - E05: le chiusure giornaliere sono quelle delle 23:00 del giorno D-1; la strategia decide solo alle 00:00 e mantiene la posizione negli altri eventi. Verifica che nessun ordine avvenga fuori dalle 00:00 (tranne la chiusura a fine finestra) e che le finestre di sviluppo, validazione e conferma non si sovrappongano.
   - la selezione usa davvero solo lo sviluppo? Il minimo di trade (30 in E01, E03, E04; 12 e 8 in E05) è applicato come dichiarato nella specifica? Controlla experiment_protocol._select, _validation_criteria, _decide.
   - i criteri di accettazione nel codice coincidono letteralmente con il testo della specifica (soprattutto "closed_trades_per_year <= 30% della baseline momentum_v1" e "costi <= 50% del lordo")?
   - le metriche (net_return_pct, costs_share_of_gross, closed_trades_per_year) sono calcolate correttamente dal registro? Ricalcolane qualcuna a mano da un ledger.
5. Valuta la robustezza del CANDIDATE di E05 (non è un criterio registrato, serve a giudicare l'uso dell'esito):
   - in conferma il netto (+24,9 su 500) dipende da un solo trade (+48,8): senza di esso sarebbe -23,9; in validazione resta positivo senza i due migliori trade. Conferma queste cifre dai ledger.
   - tutte le finestre sono state viste da più esperimenti (S08, E01-E04); E05 è il quinto sullo stesso snapshot, con 12 configurazioni e minimi di trade abbassati. Esprimi un giudizio motivato: l'esito giustifica una sessione paper su dati nuovi? Che cosa cambieresti prima?
6. Review del codice nuovo di paper.py (modifica a S06): le sessioni di altre strategie hanno lo stesso session_id e gli stessi metadati di prima? I test S06 sono ancora verdi e significativi? _check_features, FEATURE_PARAMETERS, _features, _history_close, _feed_clock, il blocco try/except FeedError in poll, la regola "la sessione sul mercato reale parte dall'ora corrente". Cerca percorsi che lascino la sessione in uno stato incoerente dopo un errore della fonte.
7. Review di public_klines.py contro la documentazione ufficiale Binance Spot (market data only): host data-api.binance.vision, /api/v3/klines (parametri startTime, endTime, limit 1000), /api/v3/time, ritardi o differenze dei dati; la barra in corso contribuisce solo con l'open; gestione di 418/429/5xx, ore mancanti, righe malformate. Può leggere dati che cambiano retroattivamente (revisioni di barre già chiuse)? Come reagisce la sessione (_revisions)?
8. Review di session_fidelity.py: il confronto ammette differenze solo nelle feature (12 cifre significative) e nella nota dei gap? Può dare un falso "match"? Prova a manometterlo (ordine, fill, saldo, barra registrata) e verifica che il controllo se ne accorga.
9. Valuta la bozza E05-P1 (config/experiments/E05-P1-paper-forward-v1.json): i criteri sono sufficienti e non aggirabili? 12 settimane per il checkpoint operativo e 8 trade chiusi o 52 settimane per quello sul vantaggio; stop a drawdown -20%; confronto con buy and hold (netto non inferiore a buy and hold meno 10 punti). Che cosa manca? Che cosa è troppo permissivo o troppo severo?
10. Rilievi ancora aperti: R13, R14 (INFO) e S07 (collaudo testnet con le chiavi dell'operatore, che tu NON esegui).

## Output
1. Scrivi REVIEW-INDEP-<data>-<modello>.md nella root del repo, senza committarlo: revisore (modello e versione) e commit di base; tabella delle prove (comando, atteso, osservato, esito); rilievi nuovi con ID R16, R17, ... con severità HIGH/MEDIUM/LOW/INFO, step, descrizione e riproduzione minima; verdetto separato per E01, E03, E04, E05, per il protocollo condiviso, per le modifiche a paper.py (S06), per public_klines e session_fidelity; per E05-P1 le modifiche richieste prima della preregistrazione; per ogni step la raccomandazione VERIFIED oppure "resta IN_REVIEW", con il motivo.
2. Nella risposta finale: un riepilogo breve, i rilievi HIGH/MEDIUM in cima, l'elenco dei file creati.

Ogni affermazione deve poggiare su un comando eseguito o su un file letto. Se una prova non è eseguibile, scrivilo esplicitamente e spiega perché.
```

## Note per il coordinatore

- Il revisore non può eseguire il runner sul mercato reale né il collaudo S07: le due cose restano a te.
- Indipendenza: il PRD §5 chiede un revisore diverso dall'autore. Il revisore precedente (GLM-5.3) aveva già
  firmato altre review; se riusi lo stesso modello dichiaralo nella nota di verifica, come per le review del 14-11.
- Il prompt è lungo apposta: la parte critica è il punto 4 (leakage e protocollo), perché l'unico esito positivo
  finora (E05) poggia su 17 trade per finestra.
