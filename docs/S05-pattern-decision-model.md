# S05 - Ricerca dei pattern e Decision Model

```powershell
python -m crypto_decision_lab.pattern_experiment --spec config/experiments/S05-momentum-filter-v1.json
```

Output in `data/experiments/<experiment_id>/` (ignorato da Git, rigenerabile; la directory deve essere nuova).

## Protocollo

1. **Preregistrazione** — la specifica versionata `config/experiments/S05-momentum-filter-v1.json`
   fissa ipotesi, dataset (digest di feature e target S02), target, baseline, feature, seed, fold,
   griglia delle soglie, regola di selezione e criteri di decisione. Il runner scrive
   `preregistration.json` con lo SHA-256 della specifica (fine riga normalizzati) **prima** di leggere
   i dati o addestrare; ogni artefatto successivo cita quel digest.
2. **Fold cronologici con purge** — training: cutoff con target maturo (`cutoff + latenza + orizzonte`)
   entro `dev_start`; sviluppo: target maturo entro `test_start`; test: `[test_start, test_end)`.
3. **Training e soglia** — regressione logistica standardizzata sulle sole entrate della baseline
   del training; soglia scelta sullo sviluppo dalla griglia preregistrata (`0` = nessun filtro),
   obiettivo = somma dei rendimenti netti target delle entrate mantenute, con numerosità minima.
4. **Confronto fuori campione** — baseline (`scheduled_momentum`) e variante (`model_filtered_momentum`)
   nel motore S03 con gli stessi eventi orari, prezzi, latenza, costi e limiti di rischio. Le
   strategie decidono solo ai cutoff S02 e mantengono l'esposizione tra un cutoff e il successivo:
   ingressi e uscite cadono sugli stessi open dei target. Un decision point `close_only` chiude la
   posizione a fine finestra. I fold di test devono stare in un segmento senza gap.
5. **Esito** — `CANDIDATE`, `INCONCLUSIVE` o `REJECTED` solo dai criteri registrati, ciascuno con
   soglia e valore osservato.

## Artefatti

| File | Contenuto |
|---|---|
| `preregistration.json` | specifica, SHA-256, istante del lock |
| `models/<fold>.json` | modello portabile (schema feature, standardizzazione, coefficienti, soglia, provenienza, `model_sha256`) |
| `ledgers/<fold>-{baseline,filtered}.json` | registri canonici del motore |
| `predictions.parquet` | score, segnale, decisione e target per ogni cutoff di test |
| `report.json` | per fold: confini e purge, conteggi, soglie, vista target, calibrazione (bin, Brier, AUC), metriche motore (rendimento, drawdown MTM, costi, variabilità mensile e per trade, riconciliazione); aggregato; criteri ed esito |
| `candidate.json` | candidato registrato: modello dell'ultimo fold, schema, soglia, dataset, configurazione per lo Strategy Layer, `usable_by_strategy_layer` |
| `manifest.json` | SHA-256 di ogni file |

## Strategy Layer

`DecisionModelAdapter` carica un artefatto verificandone il digest (e quello atteso dalla
configurazione), rifiuta schemi che contengono colonne target, restituisce una probabilità in
`[0, 1]` oppure `None` se una feature manca: la strategia allora non apre (fail closed).
`model_filtered_momentum` e `scheduled_momentum` sono registrate in `simulation.STRATEGIES` e
selezionabili da configurazione. Lo Studio (S04) le elenca ma i suoi eventi orari non hanno decision
point: l'uso operativo richiede eventi con le feature S02, previsto con S06.

## Checkpoint

```powershell
python -m pytest tests/test_pattern_experiment.py -q -p no:cacheprovider --basetemp var/s05-verification
```

CP05.1 `TestCP051Splits` · CP05.2 `TestCP052Report` · CP05.3 `TestCP053Identity`, `test_cp053_outcome_rules`, `TestAdapterContract`.
