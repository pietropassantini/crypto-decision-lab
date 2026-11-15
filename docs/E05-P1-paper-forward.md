# E05-P1 - Sessione paper su dati nuovi (bozza)

Specifica: [config/experiments/E05-P1-paper-forward-v1.json](../config/experiments/E05-P1-paper-forward-v1.json), stato `DRAFT`.
È il checkpoint CP-E05.3: dopo l'esito CANDIDATE di E05 serve una prova su dati prodotti dopo la
registrazione, prima di qualsiasi passo S08.

## Cosa si prova

La candidata è congelata: `daily_trend` con rendimento a 28 giorni, media a 100 giorni, soglie 0
(E05 g10), stessi costi, capitale 500, esposizione 0,5. Nessun parametro può cambiare durante la sessione.

## Scelte della bozza

| Tema | Scelta | Motivo |
|---|---|---|
| Prezzi | Klines orarie pubbliche del mercato reale, in sola lettura, senza chiavi | I prezzi del testnet sono quelli del mercato di prova e non sono stati confrontati con il mercato reale |
| Esecuzione | Simulata dal motore S06 con il modello di costo del backtest | Stesso motore, confronto diretto con il backtest |
| Testnet | Specchio facoltativo degli ordini tramite S07 | Serve a osservare fill, fee e guasti reali; non entra nei criteri sul vantaggio |
| Durata | 12 settimane per i criteri operativi; fino a 8 trade chiusi o 52 settimane per i criteri sul vantaggio | La candidata fa circa 7–11 trade l'anno: in 12 settimane sono 2–3 |

## Deviazione da E05 (DEV-1, da approvare prima della preregistrazione)

E05 prevede, per un esito CANDIDATE, una sessione paper su dati nuovi con le klines del **testnet**, con il runner
S06/S07 e le chiavi dell'operatore. La bozza usa invece le klines pubbliche del **mercato reale**, senza chiavi, con
l'esecuzione simulata dallo stesso motore del backtest; il testnet resta solo uno specchio facoltativo che non entra
in nessun criterio. Motivi: il segnale va giudicato su prezzi reali (quelli del testnet non sono stati confrontati con il
mercato reale), non servono chiavi per l'evidenza e quindi nemmeno il collaudo S07, e motore, feature e costi sono quelli
del backtest, così il controllo di fedeltà è diretto. E05 non cambia: il suo testo resta quello preregistrato. Serve
l'approvazione esplicita del coordinatore, registrata nella specifica (`deviations[0].approval`).

## Checkpoint e criteri

**In qualsiasi momento:** drawdown sotto −20% chiude la sessione (FAILED); una decisione presa su una barra non
chiusa o una modifica alla candidata rende la sessione INVALID.

**Settimana 12 (operativo e fedeltà):**
- **tempestività:** per ogni giorno, l'evento delle 00:00 UTC risulta registrato entro 2 ore dalla barra, in almeno il
  95% dei giorni. La prova è l'ora reale (`status_log.at`) della riga con dettaglio `last event <barra>`; una poll in
  ritardo che elabora più barre insieme si misura barra per barra;
- nessun giorno senza decisione per colpa del runner;
- fedeltà: `session_fidelity` esce con 0 (decisioni, ordini, fill e saldi uguali a quelli del motore sulle barre registrate
  dalla sessione). Certifica la coerenza interna della sessione, non l'autenticità delle barre;
- drawdown ≥ −20%.

**Checkpoint sul vantaggio (P1.edge)**, valutato **una volta sola**: il primo lunedì alle 00:00 UTC in cui la sessione ha
almeno **8 trade chiusi** (controllo settimanale). Se dopo 52 settimane non c'è, non si valuta e l'esito è INCONCLUSIVE. Il
momento dipende solo dal numero di trade chiusi, mai dal risultato: un fallimento chiude la candidata, un successo non si
ripete né si prolunga.

Si riportano sempre affiancati il netto con la posizione aperta (equity) e il netto dei soli trade chiusi, la posizione
aperta e il suo risultato non realizzato (rilievo R16 della review: la conferma di E05 passa solo per un plusvalore non
realizzato). I criteri sono:
- netto **realizzato** (trade chiusi) > 0;
- drawdown dell'equity ≥ −20%;
- costi ≤ 50% del lordo, sui soli trade chiusi;
- netto dell'equity non inferiore a quello di buy and hold meno 10 punti.

Buy and hold = la strategia `constant` LONG dello stesso motore, con capitale, esposizione 0,5, fee e slippage della
candidata, un solo ingresso al primo evento della sessione, valutata allo stesso ultimo evento, sulle barre registrate
dalla sessione.

**Esiti:** `OPERATIONAL_PASS` / `OPERATIONAL_FAIL` alla settimana 12. A P1.edge: `CONFIRMED` (si possono registrare criteri
S08 per questa candidata), `NOT_CONFIRMED` (candidata chiusa come REJECTED sui dati nuovi), `INCONCLUSIVE` (meno di 8 trade
chiusi dopo 52 settimane, o scadenza delle ripartenze).

**Ripartenze dopo OPERATIONAL_FAIL:** al massimo 2, ciascuna approvata dal coordinatore e registrata; si corregge il
runner, mai la candidata né i criteri. Le settimane e il conteggio dei trade ripartono con la nuova sessione e i trade
chiusi di una sessione abbandonata non contano. Entro **78 settimane dall'inizio della prima sessione** P1.edge deve essere
avvenuto, altrimenti INCONCLUSIVE.

## Prerequisiti

| Id | Cosa serve | Stato |
|---|---|---|
| PRE-1 | Feature giornaliere nel runner paper | **realizzato**, da rivedere: `PaperSession.create(features={momentum_days, sma_days})` e `daily_trend.daily_features_at`, che danno gli stessi valori di `daily_features` sull'intera serie |
| PRE-2 | Fonte di dati in tempo reale | **realizzata**, da rivedere: sorgente `public_klines` (klines pubbliche in sola lettura, storia di 2.600 ore con paginazione, apertura della barra in corso, tollera ore mancanti, riprova sul throttling) |
| PRE-3 | Runner pianificato su una macchina sempre accesa, con backup | operatore |
| PRE-4 | Review non Claude di E05 e del codice di PRE-1, PRE-2 e PRE-6 | revisore |
| PRE-5 | Solo per lo specchio su testnet: collaudo S07 con le chiavi | operatore |
| PRE-6 | Strumento di fedeltà per il criterio della settimana 12 | **realizzato**, da rivedere: `session_fidelity` |
| PRE-7 | Strumento di rapporto per i checkpoint: `session_report week12|edge` | **realizzato**, da rivedere: tempestività da `status_log`, cifre di P1.edge (netto equity e realizzato, lordo e costi dei trade chiusi, drawdown, posizione aperta, buy and hold) e l'esito dai criteri |

Lo specchio su testnet (S07, facoltativo) calcola ancora solo `momentum_24h` e non è coperto.

La specifica va preregistrata solo dopo PRE-4 e PRE-7, con DEV-1 approvata (fatto il 2026-11-15), citando il commit del runner.

## Come si lancia (dopo la preregistrazione)

Creazione, all'ora in cui parte la sessione (la partenza deve essere l'ora corrente o successiva):

```powershell
python -m crypto_decision_lab.paper session-create --name e05-p1 --config <config della candidata> --public-klines BTCUSDT --start "2026-12-01 00:00" --momentum-days 28 --sma-days 100
```

Poll orario (da pianificare, una volta all'ora, o in ciclo):

```powershell
python -m crypto_decision_lab.paper session-run --session-dir <cartella della sessione>
```

Controllo di fedeltà (settimana 12 e ogni volta che serve):

```powershell
python -m crypto_decision_lab.session_fidelity --session-dir <cartella della sessione>
```

Il controllo esce con codice 0 se il registro coincide con quello del motore sulle barre registrate dalla
sessione; i valori delle feature si confrontano a 12 cifre significative, perché la somma delle chiusure
giornaliere ha un ordine diverso e l'ultima cifra può cambiare.

Rapporti dei checkpoint (da lanciare ogni lunedì alle 00:00 UTC; `--as-of` serve solo per provare):

```powershell
python -m crypto_decision_lab.session_report week12 --session-dir <cartella della sessione>
python -m crypto_decision_lab.session_report edge --session-dir <cartella della sessione>
```

`week12` dà `NOT_DUE` prima delle 12 settimane, poi `OPERATIONAL_PASS` o `OPERATIONAL_FAIL` (e `FAILED` se il drawdown
supera −20% in qualsiasi momento). `edge` dà `NOT_DUE` finché i trade chiusi sono meno di 8 (e non sono passate 52
settimane), poi `CONFIRMED` o `NOT_CONFIRMED`; dopo 52 settimane senza 8 trade chiusi dà `INCONCLUSIVE`. Quando
decide scrive `p1-edge-report.json` **una volta sola**: una seconda valutazione è rifiutata (codice di uscita 2).

Se la fonte non risponde, la poll segna `DATA_ERROR` senza toccare lo stato e la poll successiva riprova.

## Limiti dichiarati

- Anche con 8 trade chiusi la conferma resta debole; il confronto con buy and hold e il limite sul drawdown
  riducono il danno di un `CONFIRMED` sbagliato.
- Un solo mercato: 12–52 settimane possono cadere tutte in una fase rialzista o ribassista.
- I fill simulati usano l'open e lo slippage ipotizzato; quelli reali si vedono solo nello specchio su
  testnet e, dopo, con capitale minimo sotto S08.
