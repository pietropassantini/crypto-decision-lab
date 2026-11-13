# Report operativo release-btcusdt-momentum-v1-81028b51fd

**Dati: paper-replay S08 (finestra 2025-01 -> 2026-09, BTCUSDT-1h-raw-v1)** · modalità `paper-replay` · registro `324dea26ddff` (69295 record) · contenuto `469e78b39a66`

## Versione e feed

- Versione: `{"config_sha256": "b29ee5b0fb33c75d03d4dc4383b37cd73174a44aa7faecaf628284a92720b2ee", "engine_sha256": "b3c692525b654cce0456471a4e186e90bd98d51f1bce8453b3fade0786015e0c"}`
- Feed: `{"dataset_id": "BTCUSDT-1h-raw-v1", "kind": "snapshot", "source_manifest_sha256": "75663276f48df572ef79e4bda113438d6256c98c714eeb8245f8d6ef4f2fd4d8"}`
- Periodo: 2025-01-01 00:00:00+00:00 → 2026-09-30 23:00:00+00:00 (15312 barre)

## Osservato e ipotesi

| Voce | Osservato | Ipotesi |
|---|---|---|
| Commissioni (bps del nozionale) | 10.00 | 10.00 |
| Slippage medio (bps, + = avverso) | 5.00 | 5.00 |
| Latenza mediana (s) | 0 | 0 |
| Fill ratio | 1.000 | 1.000 |

## Esito

- P&L netto osservato -455.47; costi osservati 426.22; lordo -29.25

## Riconciliazione

- OK executions = orders filled or cancelled: 3113 / 3113
- OK fees: report = register: 284.147845617451 / 284.147845617451

## Ipotesi dagli scostamenti

- **H-turnover**: costs 426.22 vs gross result -29.25. turnover consumes the gross edge; fewer, longer trades keep a positive net result. Metrica: net_return_pct and costs / gross on the same window. Checkpoint: preregistered turnover experiment (S09-E01)
