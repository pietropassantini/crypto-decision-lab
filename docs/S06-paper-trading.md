# S06 - Paper trading persistente

## Comandi

```powershell
# feed di laboratorio (fuori da CP08: costruito da uno snapshot di ricerca ammesso)
python -m crypto_decision_lab.paper feed-create --scenario config/lab-feeds/lab-btc-2024-03.json --feed-dir var/lab-feeds/lab-btc-2024-03
python -m crypto_decision_lab.paper feed-advance --feed-dir var/lab-feeds/lab-btc-2024-03 --hours 24

# sessione su feed di laboratorio, oppure replay di uno snapshot (--snapshot <dataset_id> --end ...)
python -m crypto_decision_lab.paper session-create --name lab --config config/simulation.example.json --feed-dir var/lab-feeds/lab-btc-2024-03 --start "2024-03-02 02:00"

# runner indipendente dalla UI
python -m crypto_decision_lab.paper session-run --session-dir var/paper/sessions/<id> [--loop --interval 60]
python -m crypto_decision_lab.paper session-status|session-ack-gap|session-suspend|session-resume|session-finish --session-dir ...
```

In Strategy Studio la scheda **Paper trading** crea sessioni, mostra stato, ultimo evento, orologio
del feed, posizione, equity, P&L, costi, trade, ordini pendenti e storico degli stati, e invia gli
stessi comandi del runner.

## Motore

`simulation.Engine` è il motore S03 trasformato in macchina a stati: `step(event)`, `finish()`,
`acknowledge_gap()`, `state()` serializzabile. `run()` (backtest e paper replay) lo usa, quindi
backtest, replay e sessioni persistenti condividono lo stesso percorso di transizione. Il registro
del CLI S03 sul dataset reale è byte-identico a quello precedente al refactor. `input_digest`
è ora una catena di hash riprendibile.

## Sessione

`var/paper/sessions/<id>/session.sqlite` (WAL, `synchronous=FULL`):

| Tabella | Contenuto |
|---|---|
| `meta` | configurazione normalizzata e digest, sorgente e sua identità, inizio/fine, soglia di obsolescenza, revisione del motore |
| `state` | stato del motore, cursore, stato esplicito, `version` per concorrenza ottimistica |
| `events` | eventi elaborati (record del motore + valori grezzi del feed) |
| `history` | `prev_close` orario per `momentum_24h` (warmup incluso) |
| `ledger` | registro canonico, append-only |
| `status_log` | transizioni di stato con orologio del feed |

Ogni evento è applicato in una sola transazione (evento, registro, storia, stato). Un'interruzione
prima del commit lascia lo stato precedente, dopo il commit quello nuovo; la ripresa riparte dal
cursore e produce lo stesso registro della sessione continua.

Stati: `CREATED`, `RUNNING`, `WAITING`, `STALE`, `PAUSED_GAP`, `DATA_ERROR`, `SUSPENDED`,
`MODEL_ERROR`, `ENGINE_MISMATCH`, `COMPLETED`.

- **Gap**: se manca l'ora successiva ma ne esistono di più recenti, la sessione si ferma in
  `PAUSED_GAP`. Recupero automatico se la barra arriva; recupero controllato con `session-ack-gap`
  (registro `gap_acknowledged`, ordine pendente annullato, posizione valutata solo con prezzi
  successivi).
- **Dati obsoleti**: nessuna barra per `stale_after_hours` dell'orologio del feed → `STALE`.
- **Dati incompleti o revisionati**: barra senza open, revisioni valide in conflitto o modifica di
  una barra già elaborata → `DATA_ERROR`, nessun avanzamento.
- **Versioni fissate**: configurazione, strategia e modello (`model_sha256`) sono letti dal DB;
  un artefatto sostituito porta a `MODEL_ERROR`, un motore diverso a `ENGINE_MISMATCH` finché
  l'operatore non lo accetta esplicitamente.

## Feed di laboratorio

`config/lab-feeds/*.json` descrive fonte (solo catalogo `research`), finestra e guasti (`late`,
`invalid` con correzione, `outage`, `gap`). `bars.jsonl` è append-only con `published_at`
pianificato: stesso scenario e stesso orologio danno byte identici, comunque si avanzi il clock.

## Checkpoint

```powershell
python -m pytest tests/test_paper.py tests/test_simulation.py tests/dashboard/test_strategy_studio.py -q -p no:cacheprovider --basetemp var/s06-verification
```

Limiti: le strategie S05 a decision point non ricevono le feature S02 nelle sessioni (solo
`momentum_24h`); il replay esegue un fsync per evento (~11 ms/evento sul PC di sviluppo).
