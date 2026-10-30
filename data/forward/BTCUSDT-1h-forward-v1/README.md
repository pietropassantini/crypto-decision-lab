# BTCUSDT-1h-forward-v1 · forward (independent) dataset

Archivio **additivo e immutabile** prodotto da `crypto_decision_lab.forward_collection`
(S02-CP08-05). Contiene esclusivamente candele BTCUSDT 1h **successive** al boundary
congelato `2026-10-23T16:00:00Z`, destinate alla futura validazione indipendente della
policy Momentum congelata.

- Ruolo: `independent_forward_collection`.
- Uso consentito: sola valutazione finale del protocollo, una volta, su dataset registrato.
- Uso vietato: ricerca, training, feature build, label build, selezione di soglie, dashboard
  con metriche economiche.
- Nessuna metrica di strategia è presente qui: solo OHLCV, provenienza, hash ed eventi.

```text
chunks/       chunk immutabili VALID (write-once, SHA-256)
quarantine/   anomalie conservate e mai promosse
manifests/    dataset-manifest-NNNN.json (immutabili) + latest.json (puntatore)
registry/     events.jsonl append-only + sync.lock
```

Comandi: `status`, `sync`, `audit`, `readiness` (vedi
`docs/S02-CP08-forward-data-collection.md`). Non cancellare né modificare file: una revisione
produce un nuovo oggetto versionato.
