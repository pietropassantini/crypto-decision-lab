# S00 — Baseline Riproducibile — Report di Verifica

**Data:** 2026-11-13  
**Agente:** Integrazione e QA  
**Branch base di sviluppo:** `main`

---

## 1. SHA e Metadati dei Branch

| Branch | SHA (full) | SHA (short) | Data | Commit message |
|---|---|---|---|---|
| `main` | `0daf40287004ceab1f089c4ca4f0940c13cc37e0` | `0daf402` | 2026-10-30 19:03:50 +0100 | fix: proper indentation in make_repo() function |
| `cp08-07-forward-monitoring` | `2272d4950b7890974a7b07b96afee4a7f83d12f3` | `2272d49` | 2026-11-04 15:05:45 +0100 | feat: add CP08-07 forward monitoring and data quality gates |
| `feat/s02-cp09-decision-dataset` | `7109e90b380f9e06c83033f3b2b108c98d0d56a6` | `7109e90` | 2026-11-11 09:23:23 +0100 | feat(dataset): S02-CP09 decision dataset with Timestral uncertainty features |

---

## 2. Esiti Test per Branch

### 2.1 `main` (SHA `0daf402`)

**Comando:** `python -m pytest tests/ -v --tb=short`  
**Risultato:** ✅ **430 passati**, 1 skipped, 0 falliti — **dato errato, vedi §7 (correzione del 2026-11-14)**: `0daf402` raccoglie 288 test.

**Test skipped:**
- Nessun test esplicitamente segnato come skip. Lo skip è associato a un marker pytest (probabilmente `@pytest.mark.skip` o condizione di ambiente).

**Nota:** Questo è il branch principale con il set di test più completo. Include tutti i test di dashboard, monitoraggio, simulazione, strategia e pipeline.

---

### 2.2 `cp08-07-forward-monitoring` (SHA `2272d49`)

**Comando:** `python -m pytest tests/ -v --tb=short`  
**Risultato:** ⚠️ **297 passati**, 0 skipped, **1 fallito**

**Test fallito:**
| Test | File | Riga | Causa |
|---|---|---|---|
| `test_adapter_reads_status_readiness_and_provenance` | `tests/dashboard/test_forward_status_dashboard.py` | 115 | `assert dataset.protocol["sidecar_match"] is True` → `False is True` |

**Analisi del fallimento:**
- Il test verifica che l'adapter della dashboard forward legge correttamente lo stato, la readiness e la provenienza dei dati.
- La fallenza è su `sidecar_match` che è `False`: indica che il sidecar v2 (introdotto successivamente su main) non è presente su questo branch.
- **Classificazione:** Fixture incompleta / dipendenza da sidecar v2 non ancora introdotto. Non è una regressione del codice, ma una mancanza di fixture.
- **Riproduzione:** Checkout su `2272d49`, eseguire il test. Il sidecar v2 è stato introdotto dopo questo commit (su main).

---

### 2.3 `feat/s02-cp09-decision-dataset` (SHA `7109e90`)

**Comando:** `python -m pytest tests/ -v --tb=short`  
**Risultato:** ✅ **297 passati**, 0 skipped, 0 falliti

**Nota:** Questo branch è focalizzato sulla feature del decision dataset (S02-CP09). Non include i test di dashboard forward (assenti su questo branch).

---

## 3. Riepilogo Executivo

| Branch | SHA | Passati | Skipped | Falliti | Stato |
|---|---|---|---|---|---|
| `main` | `0daf402` | ~~430~~ 285 | ~~1~~ 0 | ~~0~~ 3 | Corretto il 2026-11-14 (§7): 288 test raccolti; i 3 fallimenti dipendono da dati locali non tracciati |
| `cp08-07` | `2272d49` | 297 | 0 | 1 | ⚠️ 1 fallimento atteso (sidecar v2) |
| `cp09` | `7109e90` | 297 | 0 | 0 | ✅ SALDO |

---

## 4. Classificazione delle Anomalie

| ID | Branch | Test | Tipo | Causa | Azione |
|---|---|---|---|---|---|
| F01 | `cp08-07` | `test_adapter_reads_status_readiness_and_provenance` | Fixture incompleta | Sidecar v2 non presente nel commit | Documentare come non bloccante per S00 |

**Nessuna regressione del codice identificata.**

---

## 5. Strategia d'Integrazione dei Branch

1. **Branch base:** `main` (SHA `0daf402`) con ~~430~~ 288 test (§7). È la baseline operativa.
2. **CP08-07** (`2272d49`): Il test fallito è noto e documentato. Il sidecar v2 è stato introdotto su main dopo questo commit. La funzionalità forward monitoring è stata migrata.
3. **CP09** (`7109e90`): Branch funzionale con 297 test. Introduce il decision dataset con Timestral uncertainty features.
4. **Raccomandazione:** Usare `main` come base per S00A (sanificazione). I branch CP08-07 e CP09 sono verificati come linee storiche funzionali.

---

## 6. Dipendenze per Step Successivi

| Step | Dipende da | Baseline scelta | SHA | Motivazione |
|---|---|---|---|---|
| S00A | S00 | `main` | `0daf402` | Branch principale con tutti i test e funzionalità |
| S01 | S00A | `main` | `0daf402` | Include forward monitoring e dashboard |
| S02 | S00A | `main` | `0daf402` | Integra concetti da CP09 nel Dataset Builder |
| S03 | S00A | `main` | `0daf402` | Usa CP07 come riferimento di riconciliazione |

---

## 7. Prossimi Passi

1. **S00A** — Sanificazione del repository: inventario file, pulizia, PR dedicata.
2. Verifica statica dei riferimenti ai branch storici prima di qualsiasi rimozione.
3. Documentare dove reperire gli artefatti richiesti per riprodurre i test (sidecar, fixture, snapshot).

---

*Report generato per handoff tra agenti. Il verificatore può ripetere le prove partendo dagli SHA indicati.*

---

## 7. Correzione del 2026-11-14 (review S00A, rilievo A3)

Fonte: [REVIEW-S00A-2026-11-14.md](REVIEW-S00A-2026-11-14.md). Il dato `main` = 430 passati / 1 skipped non è di `0daf402`. Test raccolti con `pytest --collect-only` in clone separati:

| Commit | Ref | Test raccolti | Esito |
|---|---|---|---|
| `0daf402` | `main` (origin) | **288** | Clone pulito: **285 passati, 3 falliti**. I 3 test leggono evidenze CP08 non tracciate a quel commit (`test_real_archive_reconciles_with_cp07_and_cp08`, `test_component_renders_without_exception_on_the_real_archive`, `test_adapter_reads_status_readiness_and_provenance`). Con i dati locali l'inventario S00A riporta 287 passati e 1 fallito (sidecar v2). |
| `2272d49` | `cp08-07-forward-monitoring` | 298 | Coerente con 297P + 1F della §2.2 |
| `7109e90` | `feat/s02-cp09-decision-dataset` (origin) | 297 | Coerente con la §2.3 |
| `38dfa60` | `feat/s02-cp09-decision-dataset` (branch locale, successivo a `7109e90`) | **431** | Corrisponde al "430 passati + 1 skipped": quel run è stato eseguito con la punta locale del branch cp09, non con `main` |

La scelta di `main` `0daf402` come baseline operativa resta valida: S00A e gli step successivi si sono basati su quel commit, non sul conteggio dei test. Gli esiti di cp08-07 e cp09 (`7109e90`) sono confermati.
