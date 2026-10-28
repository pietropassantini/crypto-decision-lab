# Crypto Decision Lab · Dashboard S03-CP01

Dashboard Streamlit **read-only** per analizzare BTCUSDT storico, forecast Timestral-209M,
baseline statistiche e il futuro Decision Model. Non esegue training, non scarica dati,
non invia ordini e non modifica alcun file sotto `data/`.

## Avvio rapido

Dalla radice del repository:

```bash
cd ~/projects/crypto-decision-lab
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e '.[dashboard,dev]'

streamlit run src/crypto_decision_lab/dashboard/app.py
```

In alternativa, se il package è già installato:

```bash
cd ~/projects/crypto-decision-lab
streamlit run src/crypto_decision_lab/dashboard/app.py
# oppure: python -m streamlit run src/crypto_decision_lab/dashboard/app.py
```

L'entrypoint aggiunge automaticamente `src/` a `sys.path`, quindi il comando documentato
funziona anche senza `pip install -e .`.

Verifica rapida:

```bash
pytest tests/dashboard -q
```

## Prerequisiti dati

La dashboard legge gli artefatti già prodotti:

| Dato | Percorso |
|---|---|
| Storico BTCUSDT 1h | `data/snapshots/BTCUSDT-1h-raw-v1/raw-manifest.json` e chunk referenziati |
| Timestral S02-CP02 | `data/experiments/S02-CP02-batch-100/window-NNNN/` |
| Ridge / HistGB S02-CP05 | `data/experiments/S02-CP05-residual-baselines-v1/predictions.parquet` |
| Diagnostica S02-CP03 | `data/diagnostics/S02-CP03-batch-100-v1/` (opzionale) |
| Decision Model S03 | `data/experiments/S03-*/predictions.parquet` (opzionale, non presente) |

Non serve un server Timestral attivo: la dashboard legge i forecast archiviati.
Se un archivio manca, la dashboard mostra BTCUSDT reale e segnala la serie assente;
**non inventa previsioni**.

## Configurazione

Senza configurazione usa i default del repository. Per personalizzare:

```bash
cp config/dashboard.example.yaml config/dashboard.yaml
```

Oppure:

```bash
export CDL_DASHBOARD_CONFIG=/percorso/dashboard.yaml
export CDL_DATA_ROOT=/percorso/data
```

`config/dashboard.yaml` e `config/local.yaml` sono ignorati da git; l'esempio è versionato.
I percorsi relativi sono risolti rispetto alla radice del repository.

## Semantica temporale dei forecast (`cutoff`, orizzonte, target)

Per ogni finestra:

- `cutoff = provenance.forecast_start_utc`;
- il primo target orario è la barra al cutoff;
- il target `h24` è la **24ª barra oraria**, cioè `cutoff + 23h`;
- il prezzo di ancoraggio è l'ultima close nota **prima** del cutoff (`cutoff - 1h`);
- CP05 `actual_price_24h` segue la stessa convenzione.

Le previsioni di finestre diverse sono indipendenti. Per questo:

- la modalità predefinita `h24_common` confronta i modelli **sugli stessi timestamp target**
  e disegna solo marker, senza collegare finestre diverse;
- la modalità `cutoff_path` disegna il percorso 1–24h di **una sola** finestra selezionata;
- modelli disponibili solo a +24h (Ridge/HistGB) compaiono come singolo marker.

## Viste

### Overview
Ultima candela del dataset (non prezzo live), close, campioni forecast, MAE Timestral h24,
MAE naive h24, miglioramento percentuale e grafico principale.

### Market Explorer
Candlestick/line, zoom/pan, timeframe 1h / 4h / 1D, volume, indicatori tecnici calcolati
sul timeframe ricampionato. Export CSV di OHLCV+indicatori e dei forecast visibili.
Il PNG si esporta dalla toolbar Plotly.

### Model Comparison
Metriche su campioni comuni: MAE, RMSE, bias, MAPE, directional accuracy, miglioramento
vs naive, distribuzione residui, errori nel tempo, scatter actual/predicted e MAE per
regime di volatilità/trend. I regimi usano solo chiusure precedenti al cutoff.
Il numero di campioni comuni è sempre mostrato.

### Forecast Inspector
Per una singola previsione: cutoff, target, prezzo al cutoff, actual al target, previsione,
naive, residuo, errore assoluto/percentuale, direzioni, versione modello, sample id,
snapshot, SHA-256 e indicatori osservabili al cutoff. La sezione “conosciuto a posteriori”
è separata da quella “osservabile al cutoff”.

## Indicatori disponibili

- **Medie mobili**: SMA 7/20/50/100/200, EMA 9/12/26/50/200, periodi modificabili.
- **Trend**: MACD (12, 26, 9), Bollinger Bands (20, 2), ADX (14).
- **Momentum**: RSI (14), ROC, Stochastic Oscillator.
- **Volatilità**: ATR (14), Rolling Volatility.
- **Volumi**: Volume, Volume MA.

Indicatori overlay sul prezzo; oscillatori in pannelli separati; volume in pannello dedicato.
Il calcolo è causale e avviene sulla serie temporale selezionata (1h/4h/1D), non solo sulla
sua rappresentazione grafica.

### Aggiungere un indicatore via Python

```python
import pandas as pd
from crypto_decision_lab.dashboard.indicators import (
    IndicatorSpec, register_indicator,
)

def my_indicator(frame: pd.DataFrame, window: int = 10) -> pd.DataFrame:
    value = frame["close"].rolling(int(window), min_periods=int(window)).mean()
    return pd.DataFrame({"my_ma": value}, index=frame.index)

register_indicator(
    IndicatorSpec(
        key="my_ma",
        label="My MA",
        kind="overlay",              # overlay | oscillator | volume
        default_params={"window": 10},
        outputs=("my_ma",),
        description="Causal custom moving average.",
    ),
    my_indicator,
)
```

La funzione deve restituire un DataFrame con lo stesso indice dell'input e usare solo
righe presenti/ passate. Non deve fare shift negativi, fill o interpolazioni.

## Architettura

```text
src/crypto_decision_lab/dashboard/
    app.py                  entrypoint Streamlit, sidebar, tabs
    config.py               YAML, path, colori, preset, regimi
    data_loader.py          manifest/chunk, validazione OHLCV, resampling, gap
    forecast_adapter.py     archivio Timestral/CP05/Decision, allineamento target
    indicators.py           registry causale indicatori
    metrics.py              metriche, direzione, regimi, campioni comuni
    charts.py               figure Plotly dark
    components/
        common.py
        overview.py
        market_explorer.py
        model_comparison.py
        forecast_inspector.py
tests/dashboard/            26 test su fixture sintetiche
scripts/render_screenshots.py
```

## Test

```bash
pytest tests/dashboard -q     # 26 passed
pytest -q                    # 52 passed (suite completa)
```

I test dashboard usano fixture sintetiche, non i dati reali. Coprono SMA/EMA, RSI/MACD,
assenza di look-ahead, allineamento cutoff-target, timestamp mancanti, resampling OHLCV,
checksum, metriche comuni, regimi e grafici senza false traiettorie.

## Screenshot

- `docs/screenshots/overview.png`
- `docs/screenshots/model_comparison.png`

Per rigenerarli (richiede Chrome/Chromium e `kaleido`):

```bash
python scripts/render_screenshots.py
```

La toolbar Plotly consente comunque l'export PNG direttamente dal browser.

## Nota su S02-CP05

L'archivio CP05 contiene `timestral_price_24h` con una scala non coerente rispetto a
`forecast_return_24h` (decimale vs percentuale). La dashboard **non modifica** quei file:
usa il p50 CP02 per Timestral, mostra Ridge/HistGB così come archiviati e segnala
l'incongruenza negli avvisi. Non usare questi output come valutazione definitiva o per
decisioni di trading.

## Read-only e limiti

- Nessuna scrittura in `data/`, nessuna cancellazione, nessun fill.
- Nessun download exchange, nessuna chiamata Timestral, nessun training.
- Lo snapshot storico S01 e i file CP02/CP03/CP04/CP05 non vengono alterati.
- Ultima candela del dataset ≠ prezzo di mercato in tempo reale.
- Il Decision Model è solo un adapter opzionale: finché non esiste un archivio compatibile,
  la serie è marcata non disponibile.
