# Report operativo drill-c-4345b936

**Dati: fixture-live etichettata: drill S08 parte C (FakeExchange senza guasti, lab feed 2024-03, config candidata); non operativo** · modalità `live-fixture` · registro `d49943c53771` (114 record) · contenuto `0fae4fc93e7a`

## Versione e feed

- Versione: `{"config_sha256": "838e1b3accd18fe30ffc5bc9c7611a75b5762de47e190a0d76a4a8fd6e2ebbc4", "run_config_sha256": "b29ee5b0fb33c75d03d4dc4383b37cd73174a44aa7faecaf628284a92720b2ee", "code_sha256": "fd1153cd96c397d30f3d43feae86e26f5a1088cb3b68a05cf827a08cc2053e2e", "activation": null, "exchange": {"environment": "fixture", "exchange": "fake", "symbol_rules": {"base_asset": "BTC", "max_qty": "9000", "min_notional": "5", "min_qty": "0.00001", "quote_asset": "USDT", "quote_precision": 8, "step_size": "0.00001", "symbol": "BTCUSDT", "tick_size": "0.01"}}}`
- Feed: `{"feed_id": "drill-c", "kind": "lab_feed", "scenario_sha256": "f4c76c86a789d66358832c5d527eb8f3e08fe1562154dbb2cf8b92b0a1ceb49c", "source": {"catalog_role": "research", "dataset_id": "BTCUSDT-1h-raw-v1", "source_manifest_sha256": "75663276f48df572ef79e4bda113438d6256c98c714eeb8245f8d6ef4f2fd4d8"}}`
- Periodo: 2024-03-02 02:00:00+00:00 → 2024-03-03 01:00:00+00:00 (24 barre)

## Osservato e ipotesi

| Voce | Osservato | Ipotesi |
|---|---|---|
| Commissioni (bps del nozionale) | 10.00 | 10.00 |
| Slippage medio (bps, + = avverso) | 0.00 | 5.00 |
| Latenza mediana (s) | 0 | 0 |
| Fill ratio | 0.998 | 1.000 |
| Accordo segnali | 1.000 | 1.000 |
| Accordo posizione | 1.000 | 1.000 |

## Esito

- P&L netto osservato -0.80; costi osservati 0.60; lordo -0.20
- P&L simulato sugli stessi eventi -2.74 (costi 2.24); differenza 1.94

## Riconciliazione

- OK fills = executedQty (cdl-143c8cb2-000000): 0.00161 / 0.00161
- OK fills = executedQty (cdl-143c8cb2-000001): 0.00160 / 0.00160
- OK fills = executedQty (cdl-143c8cb2-000002): 0.00161 / 0.00161
- OK fills = executedQty (cdl-143c8cb2-000003): 0.00161 / 0.00161
- OK fills = executedQty (cdl-143c8cb2-000004): 0.00161 / 0.00161
- OK fills = executedQty (cdl-143c8cb2-000005): 0.00161 / 0.00161
- OK execution records = fills: 6 / 6
- OK notional: report = register: 597.2644744 / 597.2644744
- OK fees: report = register: 0.5972644744 / 0.5972644744
- OK trade ids unique: 6 / 6

## Ipotesi dagli scostamenti

- **H-turnover**: costs 0.60 vs gross result -0.20. turnover consumes the gross edge; fewer, longer trades keep a positive net result. Metrica: net_return_pct and costs / gross on the same window. Checkpoint: preregistered turnover experiment (S09-E01)
