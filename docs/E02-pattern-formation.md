# E02 - Strategia dinamica su pattern in formazione

```powershell
python -m crypto_decision_lab.pattern_formation_experiment --spec config/experiments/E02-pattern-formation-v1.json
```

Output in `data/experiments/E02-pattern-formation-v1/` (ignorato da Git; la cartella deve essere nuova).

## Componenti

| Modulo | Ruolo |
|---|---|
| `pattern_detector.PatternDetector` | Macchina a stati causale sulle barre orarie chiuse: punti di svolta confermati dopo `pivot_bars` barre, pattern rialzisti attivi, reset a ogni ora mancante |
| `pattern_detector.pattern_features` | Feature per evento: lo stato dopo la chiusura della barra `t-1h` è la feature dell'evento `t` |
| `pattern_detector.PatternStrategy` | Strategia `pattern` in `simulation.STRATEGIES`: entra dentro un pattern in formazione, resta long finché un pattern configurato è vivo, esce a obiettivo, invalidazione o età massima |
| `pattern_formation_experiment` | Runner preregistrato: griglia sullo sviluppo, `selection.json` bloccato, una valutazione su validazione e conferma, esito dai criteri |

## Pattern

| Id | Forma | Livello da rompere | Invalidazione | Obiettivo |
|---|---|---|---|---|
| `dbot` | Doppio minimo: due minimi entro `bottom_tolerance`, massimo intermedio almeno `min_depth` sopra | massimo intermedio | sotto il minimo più basso | altezza proiettata sopra la neckline |
| `atri` | Triangolo ascendente: due massimi entro `top_tolerance`, due minimi crescenti di almeno `min_rise` | resistenza | sotto l'ultimo minimo | resistenza + (resistenza − primo minimo) |
| `flag` | Bandiera: impulso di almeno `min_pole` in al massimo `max_pole_bars`, ritracciamento entro `max_retrace` | cima dell'asta | `max_retrace` dell'asta | cima + asta |
| `rng` | Range: due massimi e due minimi entro `range_tolerance`, altezza almeno `min_depth` | resistenza | sotto il supporto | resistenza + altezza |

Feature per pattern attivo: `pat_<id>_progress`, `_confidence`, `_stop_dist`, `_target_dist`, `_broken`, `_age`. Dopo la rottura l'invalidazione sale alla neckline. Alla fine del pattern l'evento successivo porta `pat_<id>_ended` (1 obiettivo, 2 invalidazione, 3 età) per una sola barra.

## Protocollo

1. Specifica `PREREGISTERED`; il suo sha256 (fine riga LF) è registrato nella riga E02 del backlog. Il runner scrive `preregistration.json` prima di leggere i dati.
2. Solo `BTCUSDT-1h-raw-v1` tramite catalogo, con digest del manifest uguale a quello della specifica.
3. Sviluppo: 8 configurazioni + baseline; `selection.json` scritto prima di validazione e conferma.
4. Validazione e conferma valutate una sola volta per la configurazione scelta e le baseline (momentum, buy and hold).
5. Esito `CANDIDATE`, `INCONCLUSIVE` o `REJECTED` dai criteri registrati.

Ogni modifica a specifica, detector o strategia dopo un risultato richiede una nuova versione preregistrata (`E02-...-v2`).

## Artefatti

| File | Contenuto |
|---|---|
| `preregistration.json` | specifica, sha256, istante del blocco |
| `selection.json` | risultati di sviluppo, configurazione scelta, regola |
| `ledgers/<finestra>-<config>.json` | registri canonici del motore |
| `report.json` | configurazioni, metriche per finestra (rendimento, drawdown, trade, trade/anno, costi, quota costi sul lordo, cause di uscita), criteri, esito |
| `manifest.json` | sha256 di ogni file |

## Test

```powershell
python -m pytest tests/test_pattern_detector.py tests/test_pattern_formation_experiment.py -q
```

Solo dati sintetici: causalità del detector (invarianza sui prefissi, barra corrente e future ignorate, reset ai gap), riconoscimento dei quattro pattern, regole della strategia, runner (blocco della preregistrazione, selezione solo sullo sviluppo, riproducibilità dei digest, rifiuto di output esistenti, dati forward e manifest diverso).
