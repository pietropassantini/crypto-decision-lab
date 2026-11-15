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

## Checkpoint e criteri

**In qualsiasi momento:** drawdown sotto −20% chiude la sessione (FAILED); una decisione presa su una
barra non chiusa o una modifica alla candidata rende la sessione INVALID.

**Settimana 12 (operativo e fedeltà):**
- decisioni giornaliere elaborate entro 2 ore dalle 00:00 UTC in almeno il 95% dei giorni;
- nessun giorno senza decisione per colpa del runner;
- un backtest sulle barre registrate dalla sessione riproduce il registro della sessione;
- drawdown ≥ −20%.

**Checkpoint sul vantaggio (8 trade chiusi o 52 settimane):**
- rendimento netto > 0;
- drawdown ≥ −20%;
- costi ≤ 50% del lordo;
- rendimento netto non inferiore a quello di buy and hold alla stessa esposizione meno 10 punti.

Esiti: `OPERATIONAL_PASS` / `OPERATIONAL_FAIL` alla settimana 12; `CONFIRMED` (si possono registrare
criteri S08 per questa candidata), `NOT_CONFIRMED` o `INCONCLUSIVE` (meno di 5 trade dopo 52 settimane).

## Prerequisiti

| Id | Cosa serve | Stato |
|---|---|---|
| PRE-1 | Feature giornaliere nel runner paper | **realizzato**, da rivedere: `PaperSession.create(features={momentum_days, sma_days})` e `daily_trend.daily_features_at`, che danno gli stessi valori di `daily_features` sull'intera serie |
| PRE-2 | Fonte di dati in tempo reale | **realizzata**, da rivedere: sorgente `public_klines` (klines pubbliche in sola lettura, storia di 2.600 ore con paginazione, apertura della barra in corso, tollera ore mancanti, riprova sul throttling) |
| PRE-3 | Runner pianificato su una macchina sempre accesa, con backup | operatore |
| PRE-4 | Review non Claude di E05 e del codice di PRE-1, PRE-2 e PRE-6 | revisore |
| PRE-5 | Solo per lo specchio su testnet: collaudo S07 con le chiavi | operatore |
| PRE-6 | Strumento di fedeltà per il criterio della settimana 12 | **realizzato**, da rivedere: `session_fidelity` |

Lo specchio su testnet (S07, facoltativo) calcola ancora solo `momentum_24h` e non è coperto.

La specifica va preregistrata solo dopo PRE-4, citando il commit del runner.

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

Se la fonte non risponde, la poll segna `DATA_ERROR` senza toccare lo stato e la poll successiva riprova.

## Limiti dichiarati

- Anche con 8 trade la conferma resta debole; il confronto con buy and hold e il limite sul drawdown
  riducono il danno di un `CONFIRMED` sbagliato.
- Un solo mercato: 12–52 settimane possono cadere tutte in una fase rialzista o ribassista.
- I fill simulati usano l'open e lo slippage ipotizzato; quelli reali si vedono solo nello specchio su
  testnet e, dopo, con capitale minimo sotto S08.
