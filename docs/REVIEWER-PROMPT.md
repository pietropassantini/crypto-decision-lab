# Prompt per il revisore indipendente

Da incollare a un agente diverso da Claude (per esempio Codex). Il PRD (§5) richiede che le prove determinanti siano ripetute da un revisore diverso dall'autore.

```text
Sei il revisore indipendente del progetto Crypto Decision Lab. Il PRD (§5) richiede che le prove determinanti siano ripetute da un revisore diverso dall'autore. Gli step S04–S09 e le correzioni di review sono stati scritti da Claude Opus 5.5, quindi la tua review è quella che conta per il §5.

## Contesto
- Repo: C:\Users\ACER2023\Desktop\progetti-python\crypto-decision-lab
- Branch: prd/integration
- Commit da rivedere:
  - 57cff95 S06 (base)
  - 7101740 S07–S09: live adapter, release gate, ops report. Contiene già le correzioni F1–F3 in live.py.
  - 53f15ef Review fixes: F4 in paper.py e il report REVIEW-2026-11-13.md
- Documento di riferimento: Crypto_Decision_Lab_PRD.md (backlog §7, checkpoint CPxx.y, formato handoff, §5 indipendenza).
- Leggi per primi: PRD, S06/S07/S08/S09-HANDOFF.yaml, REVIEW-2026-11-13.md, docs/S07-live-adapter.md, docs/S08-release.md, docs/S09-ops-report.md.

## Vincoli
- Non leggere e non modificare data/forward/ (perimetro CP08, archivio congelato).
- Non usare credenziali e non chiamare la mainnet. Il collaudo testnet reale (scripts/s07_testnet_smoke.py) lo esegue l'operatore con le sue chiavi: tu non lo esegui.
- Non fare commit, push o modifiche al codice. Se trovi un difetto, lo riproduci e lo documenti; la correzione la decide il coordinatore.
- Non cambiare gli stati del backlog: proponi soltanto.
- Lavora in var/review-indep/ (ignorata da git).

## Cosa fare
1. Clone pulito del commit 53f15ef con venv nuovo:
     pip install -e . --no-deps
     pytest -q -p no:cacheprovider
   Atteso: 480 passed.
2. Ripeti i checkpoint degli step IN_REVIEW, senza fidarti degli esiti negli handoff:
   - S06, CP06.1–3: ripresa dopo interruzione identica alla sessione continua; ciclo di vita del feed di laboratorio; pin di modello e motore.
   - S07, CP07.1–3:
     - invio, rifiuto, esecuzione parziale, annullamento, riconciliazione;
     - timeout dopo l'invio e riavvio senza ordini duplicati;
     - guardie su esposizione, feed obsoleto, saldo disallineato, sospensione;
     - mainnet bloccata.
   - S08, CP08.1–3: criteri write-once; drill; scheda di rilascio BLOCKED con le motivazioni giuste; gate verify_activation (scheda READY e invariata, sha della config, exchange/ambiente/simbolo, sha del codice); decisions.jsonl append-only.
   - S09, CP09.1–2: report riproducibile (stesso content_sha256 rigenerando dal registro drill-c); totali riconciliati; ipotesi coerenti con gli scostamenti.
   - S03: il refactor Engine di S06 deve lasciare byte-identico il ledger.json del CLI simulation:
       config/simulation.example.json
       source BTCUSDT-1h-raw-v1
     Esegui due volte e confronta.
3. Verifica le correzioni F1–F4, che sono dello stesso autore:
   - F1: dopo session-finish nessun comando operatore riporta la sessione live in trading.
   - F2: ENGINE_MISMATCH non si aggira con suspend/resume; flatten, cancel, rebaseline, resolve-missing-order e finish sono rifiutati fino ad accept-code-revision.
   - F3: un ordine aperto che l'exchange non conosce più diventa MISSING, poi UNRESOLVED nella tolleranza, poi HALTED oltre la tolleranza. resolve-missing-order lo porta a LOST; poi va fatto il rebaseline.
   - F4: la sessione paper COMPLETED rifiuta suspend; resume ricontrolla i pin.
   Cerca anche percorsi alternativi che riaprano gli stessi difetti: altri comandi, riapertura del processo, stati concorrenti.
4. Review del codice:
   - Concentrati su live.py, exchange.py, binance_spot.py, release.py, ops_report.py e paper.py.
   - Priorità: denaro reale e idempotenza degli ordini; gestione degli esiti incerti; derivazione del portafoglio solo dai fill; trattamento dei segreti; gate mainnet.
   - Controlla le chiamate Binance (firma HMAC, endpoint, codici di errore) contro la documentazione ufficiale Binance Spot.
5. Valuta i rilievi aperti F5–F10 di REVIEW-2026-11-13.md: confermali, ridimensionali o smentiscili.

## Output
1. Scrivi REVIEW-INDEP-<data>.md nella root del repo, senza committarlo. Contenuto:
   - revisore (modello e versione) e commit di base;
   - tabella delle prove ripetute: comando, atteso, osservato, esito;
   - rilievi nuovi con ID R1, R2, … Per ciascuno: severità HIGH/MEDIUM/LOW/INFO, step, descrizione, riproduzione minima;
   - verdetto su F1–F4 (confermata / insufficiente) e su F5–F10;
   - per ogni step S00A–S09, la raccomandazione VERIFIED oppure "resta IN_REVIEW", con il motivo.
2. Nella risposta finale: un riepilogo breve, i rilievi HIGH/MEDIUM in cima, l'elenco dei file creati.

Ogni affermazione deve poggiare su un comando eseguito o su un file letto. Se una prova non è eseguibile, scrivilo esplicitamente e spiega perché.
```
