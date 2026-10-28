# S03-CP01 · Bitcoin Forecast Explorer — Report milestone

**Repository:** `crypto-decision-lab`  
**Ambito:** dashboard read-only per BTCUSDT, forecast Timestral, baseline statistiche e futuro Decision Model.  
**Stato:** MVP e viste avanzate implementati; test verdi; avvio verificato.

---

## 1. Analisi preliminare

### Struttura trovata
- Package Python: `src/crypto_decision_lab/` con pipeline già esistente (`market.py`, `sync.py`, `freeze.py`, `forecast_smoke.py`, `forecast_batch.py`, `forecast_diagnostics.py`, `residual_dataset.py`, `residual_baselines.py`).
- Dipendenze dichiarate: `requests`, `pandas`, `pyarrow`; test `pytest`.
- Configurazione assente prima di questa milestone; aggiunto YAML di esempio.
- Nessun modulo dashboard preesistente: nessuna duplicazione di codice forecast/training.

### Dati identificati
- **Storico reale**: `data/snapshots/BTCUSDT-1h-raw-v1/raw-manifest.json`.
  - 70 file referenziati: 64 in `data/chunks`, 6 in `data/quarantine`.
  - 50.906 barre orarie, 2021-01-01 → 2026-10-23 15:00 UTC.
  - 14 ore mancanti, 5 close irregolari, 19 anomalie registrate; nessun fill.
- **Timestral**: `data/experiments/S02-CP02-batch-100/`, 100 finestre, 24 target orari ciascuna.
  Colonne: `timestamp`, `actual`, `timestral_p10/p50/p90`, `naive`.
- **Ridge/HistGB**: `data/experiments/S02-CP05-residual-baselines-v1/predictions.parquet`,
  50 finestre, solo target +24h.
- **Diagnostica/feature**: `data/diagnostics/S02-CP03-batch-100-v1/` e
  `data/datasets/S02-CP04-residuals-v1/`.
- **Snapshot**: `forecast-windows.parquet` è solo indice; nessun OHLCV duplicato.

### Semantica temporale verificata
- `cutoff = forecast_start_utc`.
- Prima riga forecast = target al cutoff; 24ª riga = `cutoff + 23h`.
- Ancore = close a `cutoff - 1h`.
- Verificato che CP05 `actual_price_24h` corrisponde alla close di mercato a `cutoff + 23h`
  (errore massimo osservato < 0,004 USDT), mentre `actual_price_24h` **non** corrisponde a
  `cutoff + 24h`.
- Verificato che `timestral_price_24h` CP05 non coincide con il p50 CP02 per una
  incongruenza di scala nell'artefatto CP05 (`forecast_return_24h` decimale vs
  `residual_pct` percentuale). I file non sono stati modificati.

### Incompatibilità e mitigazioni
| Rischio | Mitigazione |
|---|---|
| CP05 copre solo 50/100 finestre e solo h24 | Confronto multi-modello solo su timestamp/sample comuni; conteggio sempre visibile |
| CP05 Timestral reference incoerente | Timestral letto da CP02; CP05R corretto preferito; fallback CP05 originale con warning UI |
| Assenza Decision Model | Adapter opzionale, serie marcata non disponibile, nessun valore inventato |
| Path Windows/Linux e avvio senza install | Bootstrap `sys.path` nell'entrypoint; path `pathlib` |
| Snapshot non modificabile | Solo lettura, verifica SHA-256, nessuna scrittura |

---

## 2. Architettura implementata

```text
src/crypto_decision_lab/dashboard/
    app.py
    config.py
    data_loader.py
    forecast_adapter.py
    indicators.py
    metrics.py
    charts.py
    components/
        common.py
        overview.py
        market_explorer.py
        model_comparison.py
        forecast_inspector.py
tests/dashboard/
config/dashboard.example.yaml
docs/screenshots/
scripts/render_screenshots.py
README-DASHBOARD.md
```

Separazione:
- **accesso dati**: `data_loader.py`, `forecast_adapter.py`;
- **trasformazioni**: resampling OHLCV, filtri, allineamento target;
- **indicatori**: `indicators.py` con registry causale;
- **metriche**: `metrics.py`, inclusi regimi past-only;
- **presentazione**: `charts.py` + `components/`.

Cache: `lru_cache` nel core + `st.cache_data` nell'app; pulsante “Aggiorna cache”.

---

## 3. Checkpoint e verifiche

### CP01.1 — Data adapter e validazione
- [x] Lettura manifest snapshot, verifica SHA-256 chunk.
- [x] Validazione timestamp UTC, duplicati, OHLC, valori finiti/positivi.
- [x] Gap orari riportati ma non riempiti.
- [x] Resampling OHLCV 1h/4h/1D verificato con test sintetici.
- [x] Fallback a `data/chunks/**/*.parquet` se il manifest manca.
- **Esito:** test `test_data_loader.py` verdi.

### CP01.2 — Indicatori tecnici
- [x] SMA/EMA, RSI, MACD, Bollinger, ADX, ROC, Stochastic, ATR, volatilità, volume/volume MA.
- [x] Parametri UI modificabili e preset multipli.
- [x] Calcolo sul timeframe ricampionato.
- [x] Test di assenza di look-ahead su **tutti** gli indicatori registrati.
- [x] Registry estendibile via `register_indicator`.
- **Esito:** test `test_indicators.py` verdi.

### CP01.3 — Allineamento forecast
- [x] Target h1..h24 allineati al timestamp target, non al cutoff.
- [x] h24 = cutoff+23h, coerente con CP05.
- [x] `align_models` usa chiavi `(window_index, target, horizon_hours)`.
- [x] Modelli mancanti esclusi, mai inventati.
- [x] Cross-check actual archiviati vs close di mercato.
- **Esito:** test `test_alignment_metrics.py` verdi.

### CP01.4 — Overview e Market Explorer
- [x] BTCUSDT reale line/candlestick.
- [x] Timestral/naive/Ridge/HistGB attivabili/disattivabili.
- [x] Decision Model opzionale non disponibile.
- [x] Timeframe 1h/4h/1D, view line/candlestick, zoom/pan, legend interattiva.
- [x] Export CSV; export PNG dalla toolbar Plotly.
- [x] Banner read-only con ultima candela ≠ prezzo live.
- **Esito:** AppTest Streamlit senza eccezioni; health endpoint `ok`.

### CP01.5 — Model Comparison
- [x] MAE, RMSE, bias, MAPE, directional accuracy.
- [x] Distribuzione residui, errori nel tempo, scatter actual/predicted.
- [x] MAE per regime volatilità/trend, calcolato solo su dati pre-cutoff.
- [x] Miglioramento % vs naive e numero campioni comuni.
- **Esito:** test metriche/grafici verdi.

### CP01.6 — Forecast Inspector
- [x] Cutoff, target, prezzo al cutoff, actual al target, previsione, naive.
- [x] Residuo, errore assoluto/percentuale, direzione prevista/reale.
- [x] Versione modello, sample id, snapshot, SHA-256, run archive.
- [x] Indicatori tecnici al cutoff separati dagli esiti futuri.
- **Esito:** rendering AppTest senza eccezioni.

---

## 4. Test

```text
$ pytest tests/dashboard -q
27 passed in 13.95s

$ pytest -q
68 passed in 12.52s
```

Copertura dei requisiti di test:
- SMA/EMA con valori attesi;
- RSI/MACD e causalità;
- tutti gli indicatori: perturbazione del futuro → nessun cambiamento nel passato;
- allineamento temporale cutoff/horizon/target;
- timestamp mancanti e gap non riempiti;
- resampling OHLCV 1h→4h/1D;
- metriche, direzioni, miglioramento vs naive;
- regimi calcolati solo su chiusure pre-cutoff;
- lettura dataset e checksum;
- dataset vuoti/duplicati/OHLC invalidi;
- grafici: h24 solo marker, path solo per la finestra selezionata.

Avvio:
```text
$ python -m streamlit run src/crypto_decision_lab/dashboard/app.py --server.port 8502
$ curl -s http://localhost:8502/_stcore/health
ok
```

AppTest:
```text
exceptions: []
tabs: Overview, Market Explorer, Model Comparison, Forecast Inspector
```

---

## 5. Criteri di accettazione

| Criterio | Esito | Evidenza |
|---|---|---|
| Dashboard si avvia | ✅ | health `ok`, AppTest |
| BTCUSDT reale | ✅ | snapshot 50.906 barre |
| Timestral + naive visibili | ✅ | CP02 100 finestre |
| Confronto stesso asse temporale | ✅ | `align_models` + `h24_common` |
| SMA/EMA abilitabili | ✅ | sidebar indicatori |
| Selezione periodo | ✅ | date range UTC |
| MAE/RMSE senza disallineamenti | ✅ | test metriche comuni |
| Test automatici passano | ✅ | 68 passed |
| Snapshot non modificato | ✅ | sola lettura, checksum |
| Read-only | ✅ | nessun writer/ordine/download |

---

## 6. Screenshot

- `docs/screenshots/overview.png` — candlestick BTCUSDT, SMA/EMA, volume, RSI, forecast h24 comuni.
- `docs/screenshots/model_comparison.png` — actual vs predicted sui target +24h comuni.

La generazione statica usa `kaleido` + Chrome locale; in alternativa la toolbar Plotly
esporta PNG dal browser. `scripts/render_screenshots.py` è opzionale e non tocca `data/`.

---

## 7. Limiti e backlog

- Il dataset è statico: l'ultima candela non è un prezzo live (mostrato esplicitamente).
- Nessun Decision Model disponibile: adapter pronto, serie non simulata.
- CP05 resta esplorativo e non è una validazione di trading; l'incongruenza di scala
  nell'artefatto CP05 è documentata in `docs/S02-CP05R-forecast-consistency-audit.md`.
  La dashboard preferisce il benchmark additivo CP05R quando disponibile.
- Il numero di campioni comuni può ridursi molto restringendo il periodo o selezionando
  Ridge/HistGB (solo h24, 50 finestre).
- Aggiornamenti futuri: supporto a più run decisionali, download PDF/HTML del report,
  test end-to-end browser, quantili di regime configurabili per periodo.
