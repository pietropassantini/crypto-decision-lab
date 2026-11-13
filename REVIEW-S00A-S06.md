# Revisione S00A–S06 · 2026-11-13

**Revisore:** Claude Opus 5.5 (claude-opus-5-5), con mandato dell'utente di portare gli step in `VERIFIED` o di rinviarli.
**Base rivista:** `prd/integration` @ `57cff95`, checkout pulito in un worktree, quindi solo file tracciati.
**Esito del merge:** in `main` entrano gli step verificati. S01 è rinviato e annullato con revert prima del merge.

## Indipendenza

Il PRD (sezione 5) chiede un revisore diverso dall'autore. Questo vale pienamente per S00A, S01 e S02. Per gli altri step il revisore è anche autore di parte del lavoro:

- **S03:** correzioni della revisione, `market_events()` e motore a passi;
- **S04, S05, S06:** l'intero step;
- **S00A:** la correzione del ripristino.

Questa revisione **non è indipendente** per quelle parti. L'utente, che coordina il progetto, l'ha autorizzata in questa forma. Se in futuro sarà disponibile un secondo revisore, deve ripetere le prove determinanti di S03–S06 indicate negli handoff.

## Prove ripetute dal checkout pulito

| Prova | Esito |
|---|---|
| Suite completa (`pytest -q`, basetemp corto) | 429 passed |
| Suite completa con i file dello snapshot ripristinati | 429 passed |
| S02 CP02.1: build reale `BTCUSDT-1h-research-v1` | 2061 righe; digest features `2cb8f741…489b`, targets `8fffb2ec…5f30f`, identici a quelli registrati |
| S03: CLI `simulation` su `BTCUSDT-1h-raw-v1` | `ledger.json` byte-identico a quello revisionato (HALTED_GAP, cash 9834.02390961377) |
| S03: commit `0f017ca` in un worktree isolato | 82 passed |
| S04: commit `aa4f8e9` in un worktree isolato | 106 passed |
| S05: commit `9be9a04` in un worktree isolato | 126 passed |
| S05: esperimento rieseguito | esito REJECTED; report identico a quello registrato tranne `engine_sha256`, che passa da `6ff68d…` (commit S05) a `b3c692…` (motore a passi S06), come atteso |
| S00A CP00A.4: percorsi congelati CP08, `0daf402..57cff95` | diff vuoto; entrambi i protocolli `PROTOCOL_VERIFIED` |
| S00A CP00A.3: segreti tracciati | nessuno |

Nota ambientale: con un basetemp lungo, 8 test della raccolta forward e del monitor falliscono perché il sistema ha `LongPathsEnabled = 0` e i percorsi superano 260 caratteri. Con un percorso corto passano. Non è una regressione degli step. Va però segnalato al responsabile CP08: in quel caso `forward_collection` riporta conteggi errati (`quarantined_chunks` 0) invece di un errore esplicito.

## Esiti

| Step | Esito | Motivazione |
|---|---|---|
| S00A | **VERIFIED** dopo correzione | Rilievo bloccante: `report.json` e `forecast-windows.parquet` dello snapshot di riferimento erano stati tolti dal tracking, pur essendo letti da 6 moduli forecast e non rigenerabili sul posto. Il prompt di S00A chiede di conservare gli snapshot di riferimento. Su decisione dell'utente sono stati ripristinati, byte-identici a `0daf402` (commit `9e3f930`). CP00A.1–4 superati. |
| S01 | **RINVIATO**, poi **VERIFIED** (vedi seguito) | Il banner CP08-07 mostra "Tutti i controlli CP08-07 superati." con `NO_AUDIT` (riprodotto con AppTest), con `AUDIT_READ_ERROR` e con `AUDIT_FAILED` senza elenco di fallimenti. Contraddice CP01.2 e R01. Il commit è annullato da revert (`1842b00`) e il lavoro resta su `prd/integration`. |
| S02 | **VERIFIED** | CP02.1–3 confermati, anche dal checkout pulito. Rilievo LOW aperto sull'unione di `reserved_dataset_ids`, non bloccante. |
| S03 | **VERIFIED** | CP03.1–3 confermati. Le correzioni MEDIUM e LOW sono verificate; CP03.2 è stato ripetuto attraverso sessioni persistenti (S06). |
| S04 | **VERIFIED** | CP04.1–3 confermati; il commit è verde da solo. |
| S05 | **VERIFIED** con esito sperimentale REJECTED | CP05.1–3 confermati; esperimento riprodotto. Anomalia registrata: la specifica è stata committata dopo l'esecuzione e la preregistrazione è garantita solo dal lock. Nessun modello candidato utilizzabile. |
| S06 | **VERIFIED** | CP06.1–3 confermati: ripresa identica dopo interruzioni prima e dopo il commit, anche con kill del processo; ciclo di vita del feed; versioni fissate. |

## Attività aperte

1. **S01:** *(ripresentato il 2026-11-13 su `fix/s01-audit-banner`, commit `f3b0dbb`, in attesa di nuova verifica)* correggere il banner (nessun successo senza un audit `AUDIT_CURRENT` con gate PASS), aggiungere un test UI per i quattro stati e ripresentare. Va riapplicato il codice annullato dal revert `1842b00`.
2. **S00 (VERIFIED):** l'handoff S00A segnala conteggi di test probabilmente attribuiti al branch sbagliato in `S00-BASELINE-REPORT.md`. Da ricontrollare, non bloccante.
3. **S02:** rilievo LOW su `reserved_dataset_ids`.
4. **CP08:** errore silenzioso con percorsi lunghi su Windows (vedi nota ambientale).
5. **S05/S06:** le strategie a decision point non ricevono le feature S02 nelle sessioni paper.

## Seguito · S01 ripresentato e verificato (2026-11-13)

Il banner è stato corretto sul branch `fix/s01-audit-banner` (commit `f3b0dbb`, handoff `9ac6f79`). Dal checkout pulito di `9ac6f79`: CP01.1 passa, CP01.2 passa con 9 stati verificati nella vista renderizzata (i nuovi test falliscono 11/11 contro il codice rinviato), CP01.3 passa, suite completa 440 passed, percorsi congelati CP08 intatti. Sull'archivio forward reale il banner mostra "Audit CP08-07 assente" (info) al posto del falso successo.

**Indipendenza:** la correzione e questa verifica sono della stessa istanza (Claude Sonnet 5.5); il rinvio originale era di Claude Opus 5.5. Revisione non indipendente, eseguita su mandato dell'utente.

**Aperto:** la corrispondenza dell'audit è per versione del manifest (il report CP08-07 non registra hash); una soglia di età dell'audit nella UI va concordata con CP08.
