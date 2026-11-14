# E03 - Momentum con isteresi e filtro di regime

Specifica: [config/experiments/E03-regime-momentum-v1.json](../config/experiments/E03-regime-momentum-v1.json).
Lo stato è `DRAFT`: il runner rifiuta di partire finché il coordinatore non la porta a
`PREREGISTERED` e ne registra lo sha256 (fine riga LF) nella riga E03 del backlog.

## Perché

E01 (REJECTED) ha ridotto il turnover di circa 7 volte, ma quasi tutte le uscite erano del
segnale che ripassava sotto la soglia d'ingresso: l'orizzonte di 48 h non vincolava mai.
E03 prova due correzioni insieme:

| Idea | Regola |
|---|---|
| Isteresi | entra se `momentum_24h > entry_threshold`; resta long finché `momentum_24h >= exit_threshold` (`exit_threshold <= entry_threshold`) |
| Filtro di regime | mai long se `trend_ratio <= 0`, con `trend_ratio = close(t-1h) / SMA_n(t-1h) - 1` |

Feature mancante (inizio serie, ore dopo un gap) significa FLAT, anche in posizione.

## Esecuzione (dopo la preregistrazione)

```powershell
python -m crypto_decision_lab.regime_experiment develop --spec config/experiments/E03-regime-momentum-v1.json
```

Poi committare `data/experiments/E03-regime-momentum-v1/selection.json` (copia in `reports/e03/`)
**prima** della fase 2:

```powershell
python -m crypto_decision_lab.regime_experiment evaluate --spec config/experiments/E03-regime-momentum-v1.json
```

Riproducibilità (CP-E03.1): `... all --output-dir var/e03-run-b` e confronto dei `ledger_sha256`.

## Griglia e criteri

- `entry_threshold` 0,02 / 0,03 / 0,05 × `exit_threshold` 0 / −0,02 × `sma_hours` 168 / 720 = 12 configurazioni;
  orizzonte massimo 720 h, fee 0,10% e slippage 0,05% per lato, capitale 500, esposizione 0,5.
- Selezione sullo sviluppo: almeno 30 trade chiusi, rendimento netto più alto.
- Validazione e conferma: gli stessi criteri di E01 (rendimento > 0, drawdown ≥ −25%, almeno 15 trade,
  trade/anno ≤ 30% della baseline momentum_v1, costi ≤ 50% del lordo; in conferma rendimento > 0 e drawdown ≥ −25%).
- Baseline: momentum_v1 (soglia 0, 6 h) per il criterio di turnover; E01 g11 (soglia 0,03, 48 h) e buy and hold come contesto.

## Limiti dichiarati

Le tre finestre sono già state viste per varianti di momentum (S08, E01) e i risultati di E01 hanno
orientato la griglia. Un esito CANDIDATE è quindi un indizio debole: per la specifica porta solo a una
sessione paper preregistrata su dati nuovi (testnet, almeno 8 settimane), non a una scheda S08.

## Test

```powershell
python -m pytest tests/test_regime_momentum.py -q
```

Solo dati sintetici: causalità di `trend_ratio` (barre chiuse prima dell'evento, invarianza sui prefissi,
indefinita dopo un gap finché la finestra non si riempie), regole di isteresi e regime, fail closed,
runner (selezione bloccata, riproducibilità, rifiuti).
