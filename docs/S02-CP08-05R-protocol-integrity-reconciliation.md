# S02-CP08-05R · Frozen Protocol Integrity Reconciliation

**ID:** S02-CP08-05R · **Data:** 2026-10-30 · **Ambito:** Crypto Decision Lab — supply chain del protocollo congelato CP08-04
**Ruoli:** Senior Python Engineer / Software Supply Chain Engineer
**Stato finale:** `PROTOCOL_VERIFIED` · `ARCHIVE_VERIFIED` · validazione indipendente `NOT_EVALUATED` · gate strategico `NO-GO`

> Nessuna dichiarazione di integrità *completa* è emessa: la riconciliazione
> riguarda esclusivamente la catena di custodia del protocollo congelato e del
> suo archivio. La validazione indipendente resta **NOT_EVALUATED** e il gate
> strategico resta **NO-GO**.

---

## 1. Incidente

Sull'ambiente Sirio (checkout Linux, `eol=lf`) il controllo CP08-04.2 della
readiness falliva: l'SHA-256 del file YAML del protocollo non corrispondeva al
sidecar.

```
SHA-256 YAML (LF)  : cda9eab6c2943b9515f8fbabf6089851398ec2c578a697f90399ea46ba569665
SHA-256 YAML (CRLF): b5a7d248bcfde6b02b879292097ca8f758f66e7b7528dc3d6db45237f246b80e
Sidecar v1 (originale) dichiara: b5a7d248…  (la forma CRLF)
```

Problema gemello: l'archivio CP08-04
(`data/experiments/S02-CP08-independent-validation-protocol-v1/`) non era
presente su Sirio perché `data/experiments/` è ignorato da Git
(pattern `experiments/` in `.gitignore`).

## 2. Causa radice (dimostrata, non ipotizzata)

1. Il freeze CP08-04 è avvenuto su working tree Windows: il YAML usava **CRLF**
   (445 coppie CRLF, 0 CR isolati) e il sidecar v1 ha pinnato l'hash di quei byte.
2. `.gitattributes` dichiara `config/protocols/*.yaml text eol=lf`: Git
   normalizza a LF in check-in (`git ls-files --eol` → `i/lf`) e materializza LF
   su checkout Linux. Il blob in Git **è** la forma LF (`cda9eab6…`).
3. Su Sirio il working tree contiene quindi byte diversi da quelli pinnati →
   mismatch. ** Nessuna modifica locale del YAML** (working tree pulito,
   `git status` clean su entrambe le macchine).
4. Il verificatore v1 (`_sidecar_for`) accettava un solo hash: per costruzione
   non poteva verificare un checkout LF.

## 3. Evidenze di equivalenza byte-level LF/CRLF

Generate programmaticamente da `protocol_integrity.representation_evidence`
(evidenza macchina completa in
`data/reports/S02-CP08-05R-protocol-integrity-evidence.json`):

| Evidence | Valore |
|---|---|
| `lf_sha256` | `cda9eab6c2943b9515f8fbabf6089851398ec2c578a697f90399ea46ba569665` |
| `crlf_sha256` | `b5a7d248bcfde6b02b879292097ca8f758f66e7b7528dc3d6db45237f246b80e` |
| profilo EOL | 445 coppie CRLF, **0 CR isolati**, 0 LF isolati |
| aritmetica byte | 19 459 byte (CRLF) − 19 014 byte (LF) = 445 = numero di righe |
| `only_line_endings` | `true` — il file è *esattamente* una delle due forme pure |
| round-trip | LF→CRLF→LF e CRLF→LF→CRLF riproducono i byte di partenza |

Il file è una delle due forme pure della stessa sequenza di righe: la
differenza fra i due hash è **esclusivamente** la codifica dei fine riga.

## 4. Identità semantica della configurazione v1.0.0

`protocol_integrity.semantic_equivalence` (PyYAML, hash canonico JSON):

| Rappresentazione | SHA-256 canonico del documento parsato |
|---|---|
| LF | `7b32d50693906f8d818f2cb839ad70b489b5f124497635f203b0c98ebd792eb4` |
| CRLF | `7b32d50693906f8d818f2cb839ad70b489b5f124497635f203b0c98ebd792eb4` |

Identico al `canonical_sha256` pinnato nel **manifest autentico** dell'archivio
CP08-04 → nessuna regola, soglia o valore del protocollo v1.0.0 è cambiato.

## 5. Decisione di riconciliazione

- **Rappresentazione canonica per Git/Linux: LF** (`cda9eab6…`) — è ciò che Git
  memorizza e ciò che ogni checkout `eol=lf` materializza.
- **Hash CRLF originale preservato** (`b5a7d248…`) — resta l'ancora
  dell'integrità: è l'hash pinnato dal sidecar v1 (intatto) e dal manifest
  dell'archivio congelato.
- **Nessuna correzione silenziosa**: il YAML, il sidecar v1 e l'archivio
  originale non sono stati riscritti. La migrazione è *esplicita e versionata*.

## 6. Migrazione versionata (schema `cdl.protocol_integrity.sidecar.v2`)

Nuovo modulo `crypto_decision_lab.protocol_integrity` + sidecar v2 accanto ai
v1 (`*.sha256.v2.json`, per protocollo e confirmation-engine):

```
python -m crypto_decision_lab.protocol_integrity migrate    # esplicita, una tantum
python -m crypto_decision_lab.protocol_integrity verify
python -m crypto_decision_lab.protocol_integrity reconcile
```

Il sidecar v2 registra **entrambe** le rappresentazioni con origine e motivo;
la migrazione rifiuta contenuti non equivalenti e rifiuta di sovrascrivere.
Il verificatore della readiness (CP08-04.2) ora accetta una qualsiasi delle due
forme pure **solo se** l'altra è riproducibile variando esclusivamente i fine
riga e **solo se** l'ancora v1 (CRLF) continua a combaciare. Qualunque altra
differenza è `PROTOCOL_TAMPERED`.

Stati possibili del protocollo: `PROTOCOL_VERIFIED` · `SIDECAR_V2_MISSING` ·
`SIDECAR_V2_INCONSISTENT` · `PROTOCOL_TAMPERED`.

## 7. Recupero del manifest CP08-04

- Il manifest autentico **era presente** sul repository locale
  (`data/experiments/S02-CP08-independent-validation-protocol-v1/manifest.json`,
  creato `2026-10-30T14:36:02Z`, SHA-256 `eabe2d11b0441fd2771ec41099006bf74477103f92da24a08f4aa409ec20c87a`).
- Autenticità verificata **senza inventare nulla**: tutti i 10 output elencati
  nel manifest combaciano con i file presenti (10/10), e il manifest pinnava
  già la copia YAML in forma CRLF (`b5a7d248…`) e l'hash canonico
  (`7b32d506…`).
- L'archivio è ora **tracciato da Git**: `.gitignore` re-includ esattamente
  `data/experiments/S02-CP08-independent-validation-protocol-v1/**` e
  `.gitattributes` lo dichiara byte-stable (`-text -eol`, ultima regola del
  file). Simulazione di checkout dai blob staged: `ARCHIVE_VERIFIED (10/10)`,
  byte identici (JSON CRLF e copie YAML CRLF preservate).

Stati possibili dell'archivio: `ARCHIVE_VERIFIED` · `ARCHIVE_MISSING` ·
`ARCHIVE_CORRUPT`. Se il manifest non fosse recuperabile: `ARCHIVE_MISSING` e
gate di archivio bloccato — mai hash inventati, mai evidenze fabbricate.

## 8. Controlli separati e gate

| Controllo | Stato | Gate |
|---|---|---|
| Integrità del protocollo (CP08-04.2, v2) | `PROTOCOL_VERIFIED` | `protocol_integrity_gate = PASS` |
| Presenza/integrità archivio (CP08-04.16) | `ARCHIVE_VERIFIED` | `archive_gate = PASS` |
| Validazione indipendente | `NOT_EVALUATED` | — (nessuna valutazione eseguita) |
| Gate strategico | — | **`NO-GO`** (invariato) |

La presenza dell'archivio non invalida l'integrità del protocollo e viceversa:
sono due controlli distinti con gate distinti. `ARCHIVE_MISSING` produce un
WARN e blocca solo `archive_gate`; `ARCHIVE_CORRUPT` è FAIL.

Readiness post-riconciliazione: 16/16 check (15 PASS + CP08-04.13 WARN
strutturale), `technical_gate = PASS`, verdetto `NOT_EVALUATED`, banner
"NESSUN DATO INDIPENDENTE · NON VALUTATO · NO-GO" invariato. Verificato sia su
working tree CRLF (Windows) sia su albero LF simulato (Sirio: readiness
completa PASS con `config_sha256 = cda9eab6…`).

## 9. Limitazioni residue (dichiarate)

- ~~Gli archivi di ricerca pinnati dal protocollo restano **local-only**~~ —
  risolto nell'addendum §12: le 17 evidenze pinnate sono ora tracciate in Git
  byte-stable.
- Il verdetto di validazione indipendente e il gate strategico **non** sono
  stati toccati: nessuna valutazione, nessun training, nessun uso del dataset
  forward per ricerca.

## 10. Vincoli rispettati

- Regole del protocollo v1.0.0 non modificate (hash canonico identico).
- Archivio originale CP08-04 non riscritto (10/10 hash combacianti; solo
  aggiunto al tracciamento Git).
- Nessun sidecar corretto silenziosamente: v1 intatto (SHA-256 del file sidecar
  `285f79fb939c1c214a83e28d24554d79fe209408df951e693cf0d2d9ae7271cc`), v2 è un
  nuovo file esplicito e versionato.
- Nessuna valutazione indipendente eseguita; dataset forward non usato.
- Nessun training, nessun trading.

## 11. Artefatti

| Artefatto | Ruolo |
|---|---|
| `src/crypto_decision_lab/protocol_integrity.py` | verificatore versionato v2 + CLI migrate/verify/reconcile |
| `config/protocols/*.yaml.sha256.v2.json` | sidecar v2 (LF+CRLF, motivazione, provenienza) |
| `src/crypto_decision_lab/independent_validation.py` | CP08-04.2 migrato a v2; nuovo CP08-04.16; `archive_gate` |
| `tests/test_protocol_integrity.py` | 16 test: normalizzazione, tamper reale, archivio assente/corrotto, freeze preservato |
| `data/reports/S02-CP08-05R-protocol-integrity-evidence.json` | evidenza macchina della riconciliazione |
| `data/experiments/S02-CP08-independent-validation-protocol-v1/` | archivio autentico, ora tracciato byte-stable |

---

## 12. Addendum — verifica su clone pulito (Sirio)

La prima verifica su Sirio (`git pull` + readiness) ha prodotto un **crash**
(`ProtocolViolation: CP08-03 scenario metrics not found`): gli archivi di
ricerca pinnati dal protocollo erano assenti sul clone perché gitignored, e il
verificatore non degradava. Due correzioni, nello spirito 05R:

1. **Readiness resiliente (fail closed, mai crash).** Le evidenze di ricerca
   assenti degradano a controlli `BLOCKED` (nuovo stato, distinto da `FAIL`:
   assenza ≠ manomissione), elencati in `checks_blocked`; il gate tecnico resta
   chiuso. Nessun traceback, nessuna dichiarazione implicita di integrità.
2. **Evidenze pinnate tracciate in Git.** Tutti i **17 artefatti**
   `frozen_artifacts` (0,46 MB totali: snapshot, dataset CP06B/CP07-labels,
   archivi CP07 e CP08-01/02/03) sono ora tracciati con regole byte-stable
   (`-text -eol`). Per i due JSON dello snapshot — già tracciati dal primo
   commit ma memorizzati **LF-normalizzati** — il blob è stato riallineato con
   `git add --renormalize` ai **byte esatti pinnati** (CRLF): il contenuto non
   è cambiato, è la codifica di storage a combaciare ora con l'hash pinnato
   (verificato 17/17: `digest(blob staged) == sha256 pinnato`).

Esito su clone locale simulato (= Sirio post-pull): readiness completa
`technical_gate = PASS` con tutti i 17 artefatti verificati, protocollo
`PROTOCOL_VERIFIED`, archivio `ARCHIVE_VERIFIED`, `NOT_EVALUATED`, `NO-GO`.
