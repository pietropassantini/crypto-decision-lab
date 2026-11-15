# E04 - Mean reversion in trend rialzista

Specifica: [config/experiments/E04-mean-reversion-v1.json](../config/experiments/E04-mean-reversion-v1.json).
Lo stato è `DRAFT`: il runner rifiuta di partire finché il coordinatore non la porta a
`PREREGISTERED` e ne registra lo sha256 (fine riga LF) nella riga E04 del backlog.

## Perché

Momentum orario (S08, E01, E03) e i pattern di breakout (E02) sono stati bocciati dagli stessi
criteri. E04 cambia famiglia di segnale: invece di seguire la forza compra la debolezza di breve
periodo, ma solo quando il trend di fondo è rialzista.

| Regola | Definizione |
|---|---|
| z-score | `(close(t-1h) - SMA_w) / STD_w` sulle ultime `window_hours` barre chiuse |
| Ingresso | lo z-score **attraversa** `-entry_z` verso il basso, con `trend_ratio > 0` (close sopra la media a 720 h) |
| Uscita | z-score `>= 0` (prezzo tornato alla media) oppure orizzonte massimo |
| Regime | controllato solo all'ingresso: un ribasso può portare il prezzo sotto la media lunga senza forzare l'uscita |

Un ingresso per ribasso: restare in ipervenduto non riattiva il segnale, quindi dopo un'uscita a
orizzonte non c'è rientro immediato. Feature mancante significa FLAT.

## Esecuzione (dopo la preregistrazione)

```powershell
python -m crypto_decision_lab.reversion_experiment develop --spec config/experiments/E04-mean-reversion-v1.json
```

Poi committare `selection.json` (copia in `reports/e04/`) **prima** della fase 2:

```powershell
python -m crypto_decision_lab.reversion_experiment evaluate --spec config/experiments/E04-mean-reversion-v1.json
```

Riproducibilità (CP-E04.1): `... all --output-dir var/e04-run-b` e confronto dei `ledger_sha256`.

## Griglia e criteri

- `entry_z` 1,5 / 2,0 / 2,5 × `window_hours` 24 / 72 × `horizon_hours` 48 / 120 = 12 configurazioni;
  `exit_z` 0, media di regime 720 h, fee 0,10% e slippage 0,05% per lato, capitale 500, esposizione 0,5.
- Selezione, criteri di validazione e conferma, baseline momentum_v1 per il turnover: gli stessi di E01 ed E03.

## Protocollo condiviso

Da E04 gli esperimenti usano [experiment_protocol.py](../src/crypto_decision_lab/experiment_protocol.py):
due fasi (sviluppo e selezione bloccata, poi validazione e conferma una sola volta), criteri di E01.
Un nuovo esperimento dichiara solo un `Adapter` (griglia, feature, classificazione delle uscite).
I runner di E01 ed E03 restano nei loro moduli.

## Limiti dichiarati

- Quarto esperimento sullo stesso snapshot: finestre già viste, filtro di regime scelto dai risultati di sviluppo di E03.
  Un CANDIDATE porta solo a una sessione paper su dati nuovi (testnet, almeno 8 settimane).
- La mean reversion ha molte piccole vincite e poche perdite grandi quando il ribasso continua. Non c'è
  stop sul prezzo: il limite è l'orizzonte, con esposizione massima 0,5. Il criterio sul drawdown è la guardia registrata.

## Test

```powershell
python -m pytest tests/test_mean_reversion.py -q
```

Solo dati sintetici: causalità dello z-score, regole di ingresso e uscita, fail closed, protocollo condiviso
(blocchi di preregistrazione e selezione, riproducibilità, rifiuti, controllo del motore).
