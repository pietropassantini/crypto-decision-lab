# S09 — Misura operativa e ciclo successivo

Il modulo `crypto_decision_lab.ops_report` legge un registro noto e produce un report che confronta l'osservato con le ipotesi di simulazione della stessa configurazione. Il registro può essere una sessione live S07 (`live.sqlite`) o una sessione paper S06 (`session.sqlite`). Gli scostamenti oltre soglia diventano ipotesi di ricerca, ciascuna con metrica e checkpoint.

```powershell
python -m crypto_decision_lab.ops_report report --session-dir <sessione> --label "<cosa sono i dati>" --out reports/s09/<nome>
python -m crypto_decision_lab.ops_report fixture --config config/live/<cand>.json --workdir var/s09-fixture
```

## Contenuto del report

| Sezione | Contenuto | Confronto |
|---|---|---|
| Versione | Digest di configurazione e di run_config, digest del codice (motore + live) o del motore, attivazione S08, exchange | — |
| Feed e modalità | Identità del feed (`feed_id`, scenario, manifest della fonte); `live`, `live-fixture`, `paper` o `paper-replay`; periodo | — |
| Ordini | Ordine ↔ barra ↔ motivo della decisione ↔ fill: quantità richiesta ed eseguita, VWAP, commissioni, slippage, latenze | — |
| Costi | Commissioni in quote e in bps del nozionale | `fee_rate` |
| Slippage | VWAP rispetto all'open della barra di decisione, con segno (+ = avverso); costo in quote | `slippage` |
| Latenza | Live: barra → intento, intento → primo fill. Paper: ordine → fill | `latency_hours` |
| Fill mancanti | Ordini rifiutati, non piazzati, scaduti, annullati o parziali; fill ratio | Fill completo |
| Segnali | Il motore S03 rigioca gli stessi record di mercato con la stessa configurazione: accordo dei segnali di strategia e della posizione per barra; guardie scattate | Simulazione |
| Esito | P&L netto, costi e lordo osservati; P&L e costi simulati sugli stessi eventi | Simulazione |
| Riconciliazione | Fill = `executedQty` per ordine, record di esecuzione = fill, nozionale e commissioni del report = registro, `trade_id` unici | Registro |

Il contenuto è deterministico: `content_sha256` esclude l'ora di generazione. Ogni report dichiara in `data_label` di che dati si tratta.

## Soglie che aprono un'ipotesi

| Ipotesi | Condizione |
|---|---|
| `H-slippage` | Slippage medio avverso > 1,5 × quello ipotizzato |
| `H-fills` | Solo live: fill ratio < 0,99 o almeno un fill mancante |
| `H-latency` | Latenza mediana > latenza ipotizzata + 300 s |
| `H-signals` | Accordo dei segnali < 0,99 |
| `H-turnover` | Costi > 50% del risultato lordo, oppure lordo ≤ 0 con costi positivi |

## Collaudo (live non attivo)

Lo step S08 ha deciso **NOT_ACTIVATED**, confermato dall'operatore il 2026-11-13, quindi non esiste un registro live reale. Il report è collaudato su tre registri in `reports/s09/`:

| Report | Dati | Esito |
|---|---|---|
| `fixture-live-btcusdt-momentum-v1` | Fixture live **etichettata**: FakeExchange con slippage avverso di 10 bps, 1 rifiuto, 1 timeout, 1 MARKET parziale; lab feed 2024-03 con una barra in ritardo; config candidata | Ritrova gli scostamenti iniettati: slippage 10,00 contro 5,00 bps, fill ratio 0,813, 2 fill mancanti, accordo dei segnali 1,000, accordo della posizione 0,933. Ipotesi `H-slippage`, `H-fills`, `H-turnover`. Riconciliazione completa. |
| `drill-c-btcusdt-momentum-v1` | Parte C del drill S08: fixture senza guasti, config candidata | Solo `H-turnover`. Riconciliazione completa. |
| `paper-btcusdt-momentum-v1` | Sessione paper-replay S08, 2025-01 → 2026-09 | Commissioni 10 bps e slippage 5 bps, uguali alle ipotesi per costruzione; 3113 esecuzioni; costi 426 contro lordo -29. Solo `H-turnover`. |

Le ipotesi `H-slippage` e `H-fills` della fixture derivano da guasti iniettati. Dimostrano che il rilevamento funziona, **non** sono osservazioni di mercato. Diventeranno esperimenti solo con fill reali, cioè da testnet o live.

## Ciclo successivo

`H-turnover` è supportata da evidenza reale: backtest e paper S08, più la diagnostica senza costi a +40,5%. Diventa l'esperimento preregistrato **S09-E01** ([config/experiments/S09-E01-turnover-reduction-v1.json](../config/experiments/S09-E01-turnover-reduction-v1.json)), riga `E01` del backlog. La specifica contiene:
- ipotesi, input (dataset con hash del manifest, motore e parametri fissi), griglia di 12 configurazioni;
- tre finestre cronologiche (selezione, validazione, conferma), con regola di selezione solo sullo sviluppo;
- baseline, metriche, criteri di accettazione ed esiti `CANDIDATE`, `INCONCLUSIVE`, `REJECTED`;
- checkpoint CP-E01.1–3, rischi e prompt completo per un agente diverso.
