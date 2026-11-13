# S04 - Strategy Studio

Avvio dalla radice del repository (extra `dashboard`):

```powershell
streamlit run src/crypto_decision_lab/dashboard/studio_app.py
```

Lo Studio è anche la scheda **Strategy Studio** della dashboard principale
(`streamlit run src/crypto_decision_lab/dashboard/app.py`). L'entry point autonomo
non dipende dallo snapshot della sidebar né dagli archivi forecast.

## Viste

| Vista | Funzione |
|---|---|
| Strategie | Crea una configurazione (strategia registrata in `simulation.STRATEGIES`, parametri, capitale, rischio, latenza, orizzonte, fee e slippage per lato) e la salva come file immutabile. Elenco e dettaglio con SHA-256. |
| Nuovo backtest | Sceglie configurazione, dataset **ammesso** dal catalogo S02, periodo UTC e modalità (`backtest` o `paper-replay`). Mostra i segmenti contigui e avvisa se il periodo contiene gap (il motore S03 si arresta con `HALTED_GAP`). |
| Risultati | P&L netto, drawdown mark-to-market, costi, esposizione, trade, win rate; equity, drawdown, prezzo con ingressi/uscite e motivazioni; registro operazioni (CSV), risultati per finestra (mese, trimestre, settimana, anno), riconciliazione con il registro, decisioni con esito del rischio, manifest. |
| Confronto | Due o più run: verdetto di confrontabilità, tabella delle condizioni (dataset, manifest, periodo, digest degli eventi, latenza, orizzonte, fee, slippage, capitale, revisione del motore) separata dall'oggetto del confronto (strategia, limiti di rischio, modalità, esito), metriche ed equity sovrapposte. |

## Contratti

- **Configurazioni** — `var/studio/configs/<nome>-<sha12>.json`. Il digest è calcolato sulla
  `RunConfig` S03 normalizzata (0 e 0.0 sono lo stesso valore). I file sono creati in modo
  esclusivo e verificati a ogni lettura; un file alterato non viene più proposto.
- **Fonti** — solo `Studio.authorized_sources()`, cioè le voci del catalogo che superano
  `assert_research_admissible` (ruolo, identità, provenienza, alias). Il dataset forward
  CP08 e le sue copie non sono mai selezionabili né caricabili. Lo storage dello Studio
  non può stare sotto `data/forward`.
- **Eventi** — `simulation.market_events(frame, start, end_exclusive)`, condiviso con la CLI S03.
  `momentum_24h` = close(t-1h)/close(t-25h)-1 sul calendario orario: le barre prima
  dell'inizio fanno solo da warmup; un'ora mancante non produce la feature.
- **Run** — `var/studio/runs/<UTC>-<fingerprint>/` con `manifest.json` (manifest del motore,
  `source_manifest_sha256`, blocco `studio` con configurazione, dataset, periodo richiesto
  ed effettivo, dichiarazione dell'ipotesi di esecuzione, `ledger_sha256`), `ledger.json`
  canonico e `summary.json`. Un registro alterato viene rifiutato in lettura.
- **Metriche** — `studio_analytics` ricava tutto dal registro: equity = saldo `balance` a
  ogni evento (cassa + quantità × open), drawdown = `min(equity/picco - 1)` partendo dal
  capitale iniziale (stessa definizione di `charts.make_drawdown_figure`), costi = fee +
  slippage delle esecuzioni, trade = BUY e SELL successiva (posizione finale valorizzata
  all'ultimo open). `reconcile()` verifica P&L, cassa, finestre, costi e conteggi.

`var/` è ignorata da Git: configurazioni e run dello Studio sono artefatti locali
rigenerabili; la riproducibilità è data da configurazione, digest degli input e hash del motore.

## Checkpoint

```powershell
python -m pytest tests/test_studio.py tests/dashboard/test_strategy_studio.py tests/test_simulation.py -q -p no:cacheprovider --basetemp var/s04-verification
```

- CP04.1 — `tests/dashboard/test_strategy_studio.py::test_cp041_save_config_run_open_trade_and_reread_in_new_session`
  e `tests/test_studio.py::TestSourcesAndRuns`.
- CP04.2 — `tests/test_studio.py::TestCP042Reconciliation` (fixture calcolato a mano, finestre mensili).
- CP04.3 — `tests/test_studio.py::TestCP043Comparison`, `TestSourcesAndRuns::test_studio_exposes_only_authorized_sources`,
  `tests/dashboard/test_strategy_studio.py::test_cp043_comparison_view_flags_period_change`.

Fuori ambito: Decision Model (S05), sessioni paper persistenti su feed (S06).
