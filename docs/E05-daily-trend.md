# E05 - Trend su barre giornaliere

Specifica: [config/experiments/E05-daily-trend-v1.json](../config/experiments/E05-daily-trend-v1.json).
Lo stato è `DRAFT`: il runner rifiuta di partire finché il coordinatore non la porta a
`PREREGISTERED` e ne registra lo sha256 (fine riga LF) nella riga E05 del backlog.

## Perché

E01–E04 (momentum, pattern, mean reversion con decisioni orarie) sono stati bocciati dallo stesso
criterio: il lordo per trade restava tra 0,2 e 0,9 su una posizione di 250, contro circa 0,75 di costo
e circa 1,5 per superare il criterio. E05 non cambia il costo: allunga l'orizzonte a una decisione al
giorno, così ogni trade punta a un movimento di più giorni.

## Come funziona

Il motore resta orario (gap, esecuzioni e costi invariati). La strategia `daily_trend` decide solo
sull'evento delle 00:00 UTC, con feature costruite dalle chiusure giornaliere:

| Feature | Definizione |
|---|---|
| chiusura del giorno D−1 | close della barra delle 23:00, l'ultima chiusa prima della decisione |
| `d_momentum` | `close(D−1) / close(D−1−n) − 1` su `momentum_days` |
| `d_trend` | `close(D−1) / SMA_m − 1` su `sma_days` |

| Situazione | Decisione |
|---|---|
| evento non alle 00:00 | mantiene l'esposizione |
| feature mancante alle 00:00 | FLAT |
| `d_trend <= 0` | FLAT |
| flat e `d_momentum > entry_threshold` | LONG |
| long e `d_momentum >= 0` | resta LONG, altrimenti FLAT |

Un giorno senza la barra delle 23:00 non ha chiusura giornaliera: le finestre che lo contengono sono indefinite.

## Esecuzione (dopo la preregistrazione)

```powershell
python -m crypto_decision_lab.daily_experiment develop --spec config/experiments/E05-daily-trend-v1.json
```

Poi committare `selection.json` (copia in `reports/e05/`) **prima** della fase 2:

```powershell
python -m crypto_decision_lab.daily_experiment evaluate --spec config/experiments/E05-daily-trend-v1.json
```

Riproducibilità (CP-E05.1): `... all --output-dir var/e05-run-b` e confronto dei `ledger_sha256`.

## Griglia e criteri

- `momentum_days` 7 / 14 / 28 × `sma_days` 50 / 100 × `entry_threshold` 0 / 0,05 = 12 configurazioni;
  nessun orizzonte massimo, fee 0,10% e slippage 0,05% per lato, capitale 500, esposizione 0,5.
- Criteri di E01, **con minimi di trade ridotti**: 12 trade chiusi per la selezione (erano 30) e 8 in validazione (erano 15).
- Baseline: momentum_v1 per il criterio di turnover, buy and hold come contesto.

## Limiti dichiarati

- **Pochi trade.** Con decisioni giornaliere i trade sono 5–20 l'anno: l'evidenza statistica è più debole
  e uno o due trade grandi possono decidere l'esito.
- **Finestra di validazione favorevole.** È noto che buy and hold fa +103% in validazione: una regola
  long-only di trend ne beneficia per costruzione. La prova più dura è la conferma (buy and hold −5,4%).
- **Quinto esperimento sullo stesso snapshot.** Un CANDIDATE porta solo a una sessione paper su dati
  nuovi (testnet, almeno 12 settimane).
- **Nessuno stop sul prezzo.** L'uscita è controllata una volta al giorno; un crollo intraday resta in
  portafoglio fino alle 00:00 successive.

## Test

```powershell
python -m pytest tests/test_daily_trend.py -q
```

Solo dati sintetici: causalità delle chiusure giornaliere, giorni mancanti, mantenimento tra le decisioni,
regole di isteresi e regime, minimi di trade del protocollo, ordini solo alle 00:00.
