# Report operativo s09-fixture-b442b2d4

**Dati: fixture-live etichettata: FakeExchange (slippage avverso 10 bps, 1 rifiuto, 1 timeout, 1 MARKET parziale) + lab feed 2024-03 con barra in ritardo; non operativo** · modalità `live-fixture` · registro `9f29b99866e7` (206 record) · contenuto `a31f8a79e6d5`

## Versione e feed

- Versione: `{"config_sha256": "838e1b3accd18fe30ffc5bc9c7611a75b5762de47e190a0d76a4a8fd6e2ebbc4", "run_config_sha256": "b29ee5b0fb33c75d03d4dc4383b37cd73174a44aa7faecaf628284a92720b2ee", "code_sha256": "ca5a033610a145de970d9f091011c7925deec860dfe283ff937a48a9ed8ef99d", "activation": null, "exchange": {"environment": "fixture", "exchange": "fake", "symbol_rules": {"base_asset": "BTC", "max_qty": "9000", "min_notional": "5", "min_qty": "0.00001", "quote_asset": "USDT", "quote_precision": 8, "step_size": "0.00001", "symbol": "BTCUSDT", "tick_size": "0.01"}}}`
- Feed: `{"feed_id": "s09-fixture", "kind": "lab_feed", "scenario_sha256": "d97bb0d550d6fd31f31689942d98810fc33d0b03e423c9e341c32f6fcf238d4f", "source": {"catalog_role": "research", "dataset_id": "BTCUSDT-1h-raw-v1", "source_manifest_sha256": "75663276f48df572ef79e4bda113438d6256c98c714eeb8245f8d6ef4f2fd4d8"}}`
- Periodo: 2024-03-02 02:00:00+00:00 → 2024-03-03 23:00:00+00:00 (45 barre)

## Osservato e ipotesi

| Voce | Osservato | Ipotesi |
|---|---|---|
| Commissioni (bps del nozionale) | 10.00 | 10.00 |
| Slippage medio (bps, + = avverso) | 10.00 | 5.00 |
| Latenza mediana (s) | 0 | 0 |
| Fill ratio | 0.813 | 1.000 |
| Accordo segnali | 1.000 | 1.000 |
| Accordo posizione | 0.933 | 1.000 |

## Esito

- P&L netto osservato -0.30; costi osservati 1.24; lordo 0.94
- P&L simulato sugli stessi eventi -0.84 (costi 3.37); differenza 0.54

## Fill mancanti

- `{"client_order_id": "cdl-7a205e71-000000", "side": "BUY", "status": "REJECTED", "requested_quantity": 0.00161212290631583, "executed_quantity": 0.0, "detail": "-2010: Account has insufficient balance for requested action."}`
- `{"client_order_id": "cdl-7a205e71-000003", "side": "BUY", "status": "EXPIRED", "requested_quantity": 0.0016188771435754618, "executed_quantity": 0.00096, "detail": ""}`

## Riconciliazione

- OK fills = executedQty (cdl-7a205e71-000000): 0 / 0
- OK fills = executedQty (cdl-7a205e71-000001): 0.00161 / 0.00161
- OK fills = executedQty (cdl-7a205e71-000002): 0.00160 / 0.00160
- OK fills = executedQty (cdl-7a205e71-000003): 0.00096 / 0.00096
- OK fills = executedQty (cdl-7a205e71-000004): 0.00096 / 0.00096
- OK fills = executedQty (cdl-7a205e71-000005): 0.00161 / 0.00161
- OK fills = executedQty (cdl-7a205e71-000006): 0.00161 / 0.00161
- OK fills = executedQty (cdl-7a205e71-000007): 0.00159 / 0.00159
- OK execution records = fills: 7 / 7
- OK notional: report = register: 617.9484342 / 617.9484342
- OK fees: report = register: 0.6179484342 / 0.6179484342
- OK trade ids unique: 7 / 7

## Ipotesi dagli scostamenti

- **H-slippage**: mean adverse slippage 10.00 bps vs assumed 5.00 bps. the simulation underestimates execution cost; a slippage model calibrated on observed fills changes the ranking of candidates. Metrica: net_return_pct with calibrated slippage vs assumed slippage. Checkpoint: re-run the candidate backtest with slippage = observed p75; accept the model if it reproduces observed live/paper cost within 10%
- **H-fills**: 2 orders with missing quantity; fill ratio 0.813393961771307. unfilled or rejected orders bias the outcome against the simulation, which fills every eligible order. Metrica: share of decisions executed in full; P&L difference attributable to missing fills. Checkpoint: classify every missing fill by cause (reject, timeout, expiry, cancel) and add the dominant cause to the simulated broker
- **H-turnover**: costs 1.24 vs gross result 0.94. turnover consumes the gross edge; fewer, longer trades keep a positive net result. Metrica: net_return_pct and costs / gross on the same window. Checkpoint: preregistered turnover experiment (S09-E01)
