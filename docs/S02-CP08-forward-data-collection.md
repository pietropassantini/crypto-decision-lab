# S02-CP08-05 · Forward Data Collection & Independent Validation Isolation

> **Dichiarazione di metodo.** Questa milestone costruisce il *rubinetto* dell'evidenza
> indipendente, non l'evidenza. Non addestra, non inferisce, non seleziona soglie, non
> calcola P&L, non emette verdetti. Il gate strategico resta **NO-GO** e la validazione
> indipendente resta **NOT_EVALUATED** finché non esiste un dataset forward completo,
> registrato e consumato **una sola volta** secondo il protocollo v1.0.0.

- Milestone: **S02-CP08-05**
- Dataset: **`BTCUSDT-1h-forward-v1`**
- Boundary congelato: **`2026-10-23T16:00:00Z`** (autorità: protocollo v1.0.0)
- Codice: `src/crypto_decision_lab/forward_collection.py`, `src/crypto_decision_lab/forward_isolation.py`
- Test: `tests/test_forward_collection.py` (43), `tests/dashboard/test_forward_status_dashboard.py` (8)

---

## 1. CP08-05-01 · Analisi del repository (cosa è stato riutilizzato)

| Componente esistente | Uso in CP08-05 | Note |
|---|---|---|
| `market.fetch_klines` | **adapter di rete unico** | endpoint pubblico `data-api.binance.vision`, paginazione `limit=1000`, retry con backoff su 429/418/5xx, rifiuto esplicito delle candele non chiuse |
| `market.normalize_klines` | schema OHLCV canonico | 12 colonne Binance + `timestamp` UTC derivato da `open_time` |
| `quality.check_quality` | nucleo del quality gate | continuità oraria, duplicati, allineamento, `close_time`, NaN, OHLC, volumi |
| `sync.digest` | hashing dei file | SHA-256 a blocchi da 1 MiB |
| `sync.sync` (pattern) | struttura chunk + manifest + quarantine + `os.replace` atomico | **non riusato come motore**: chunk mensili, semantica "exception audit" e sovrascrittura del manifest non sono compatibili con un archivio forward immutabile |
| `independent_validation` | **autorità** del protocollo | `load_protocol`, `validate_protocol`, `_sidecar_for`, `ValidationRegistry`, costanti di verdetto |

### Incompatibilità documentate (e come sono state risolte)

1. **Chunking mensile vs boundary.** `sync.sync` parte da date utente e produce chunk mensili;
   il forward deve partire *esattamente* dal boundary e restare allineato a una griglia
   deterministica. → Griglia propria (`grid_cells`) con `chunk_hours` configurabile e coda
   finale immutabile.
2. **Eccezioni di sorgente.** `assess_source_quality` accetta eventi storici noti (2021) con
   placeholder audit-only. Per il forward **nessuna eccezione è ammessa**: un buco è un buco.
   → `assess_chunk` usa solo `check_quality` + controlli aggiuntivi.
3. **Manifest sovrascritto.** `sync.sync` riscrive il manifest con `os.replace`. Il forward
   richiede manifest immutabili e versionati. → `dataset-manifest-NNNN.json` write-once +
   puntatore `latest.json` (additivo, non sostituisce dati).
4. **Metadati nell'hash.** Un `ingestion_timestamp` dentro il Parquet renderebbe il digest
   non riproducibile. → l'ingestion timestamp vive **solo** nel metadata JSON; il payload
   contiene provenienza deterministica + `record_checksum` per riga (vedi §3).
5. **`fcntl` non disponibile su Windows.** Il modulo usa `fcntl` su Linux e `msvcrt` come
   fallback, così i test girano su entrambe le piattaforme senza cambiare semantica.
6. **Fine riga nei sidecar.** I file `.sha256` erano CRLF (rompevano `sha256sum -c` su POSIX):
   corretti in LF con `.gitattributes` limitato a `config/protocols/*` (nessun impatto sugli hash
   dei dati).

---

## 2. CP08-05-02 · Contratto del dataset `BTCUSDT-1h-forward-v1`

**Regole di boundary (applicate dal gate, non solo dichiarate).**

| Regola | Implementazione |
|---|---|
| Prima osservazione ammissibile | `open_time >= 2026-10-23T16:00:00Z` (uguale al boundary) |
| Candela che attraversa il confine | **esclusa**: `open_time < boundary` ⇒ `BOUNDARY_VIOLATION` (INVALID/quarantena) |
| Candela parziale | rifiutata: `close_time != open_time + 3 599 999 ms` ⇒ `PARTIAL_CANDLE` |
| Candela non chiusa | mai richiesta: `end <= floor(now, 1h)`; l'adapter rifiuta un `end` nel futuro |
| Interpolazione / gap filling | vietati: nessun placeholder, nessuna riga sintetica |
| Sovrascrittura | vietata: chunk write-once, contenuto diverso ⇒ conflitto |
| Dati precedenti al boundary | vietati e verificati anche in `audit` (`FWD.3`) |

**Colonne del payload (deterministiche):**

`timestamp, open_time, open, high, low, close, volume, close_time, quote_volume, trades,
taker_buy_base, taker_buy_quote, symbol, schema_version, source_id, data_source, record_checksum`

- `timestamp` = UTC derivato da `open_time`; `record_checksum` = SHA-256 della riga canonica
  (schema + valori OHLCV + provenienza).
- **`ingestion_timestamp`** è nel metadata del chunk (`ingested_utc`) e nel manifest: è un
  metadato operativo, escluso dall'hash di contenuto *per scelta*, così che una ri-ingestione
  identica produca byte identici (idempotenza reale, non apparente).
- Schema: `cdl.cp08.forward_dataset.v1` (payload), `cdl.cp08.forward_chunk.v1` (metadata),
  `cdl.cp08.forward_manifest.v1` (manifest), `cdl.cp08.forward_event.v1` (registry).

---

## 3. CP08-05-03 · Acquisition engine

```bash
python -m crypto_decision_lab.forward_collection status      # offline, read-only
python -m crypto_decision_lab.forward_collection sync        # rete, incrementale
python -m crypto_decision_lab.forward_collection audit       # offline, hash/boundary/isolation
python -m crypto_decision_lab.forward_collection readiness   # offline, sola disponibilità
```

Flag utili: `--config`, `--repo-root`, `--now` (diagnostica/test), `--recheck-source`,
`--lock-timeout`, `--log-file`.

**Algoritmo di `sync`**

1. `closed_boundary = floor(now, 1h)`; nessuna richiesta oltre l'ultima ora chiusa.
2. Griglia di celle da `boundary` a `closed_boundary` (`chunk_hours`, default 24) + eventuale
   coda incompleta, salvata come chunk `tail-*` immutabile.
3. Per ogni cella: se esiste già un chunk VALID che la copre → **riuso, zero rete**.
4. Altrimenti fetch tramite `market.fetch_klines` (retry/backoff), normalizzazione, quality gate.
5. VALID → `chunks/` (write-once, Parquet zstd, `os.replace` atomico + `fsync`).
   Non VALID → `quarantine/` con stato, issue e gap elencati; **mai promosso**.
6. Eventi append-only (`CHUNK_PROMOTED`, `CHUNK_QUARANTINED`, `SOURCE_DISCREPANCY`,
   `QUARANTINE_RESOLVED`, `SYNC_INTERRUPTED`, `SYNC_COMPLETED`, `SYNC_NO_DATA`).
7. Manifest versionato; `latest.json` puntatore con hash del manifest puntato.
8. Lock esclusivo non bloccante (`registry/sync.lock`); seconda istanza ⇒ exit code **75**.

**Idempotenza e resume.** Il contenuto è indirizzato per hash: se la sorgente restituisce gli
stessi dati, il chunk esiste già e viene riusato (nessuna riscrittura). Un'interruzione a metà
run lascia i chunk già promossi e un evento `SYNC_INTERRUPTED`; il run successivo riprende
dalle celle mancanti, senza temporanei residui (`.tmp` sempre rimossi).

**Conflitti di sorgente.** Con `--recheck-source` (o quando una cella viene riesaminata) il
payload nuovo viene confrontato riga-per-riga sull'overlap con l'oggetto esistente:
- prefisso identico e sola estensione ⇒ nuovo `tail` promosso;
- qualunque differenza ⇒ evento `SOURCE_DISCREPANCY` con `promotion_blocked: true`, **nessuna
  sovrascrittura**, l'oggetto esistente resta autorevole.

**Sicurezza.** Nessuna credenziale: solo endpoint pubblici; il caricamento rifiuta `api_key`/
`secret`/`token` nel YAML.

---

## 4. CP08-05-04 · Dataset quality gate

Classificazione deterministica:

| Stato | Significato | Promozione |
|---|---|---|
| `VALID` | tutte le verifiche superate | sì |
| `REQUIRES_REVIEW` | barre strutturalmente valide ma sospette (volume totale nullo, OHLC piatto) | **no** |
| `QUARANTINED` | dati presenti ma difettosi: gap, duplicati, disallineamento, `close_time` errato, OHLC/volume incoerenti, barre fuori range | no |
| `INVALID` | payload inutilizzabile: schema/parse, cella che tocca il boundary, intervallo non chiuso | no |

**Gap vs non ancora acquisito.** `readiness` distingue:
- *non ancora acquisito*: ore attese > ore archiviate, senza oggetti in quarantena
  (`status.hours_not_yet_acquired`, `status.pending_hours`);
- *gap in un periodo dichiarato completo*: celle chiuse senza chunk VALID, con evidenza di
  quarantena (`status.gaps`, `status.quarantined_chunks`, eventi `CHUNK_QUARANTINED`).
Nessun dato problematico viene cancellato: la quarantena è immutabile e content-addressed.

---

## 5. CP08-05-05 · Archivio immutabile

```text
data/forward/
  BTCUSDT-1h-forward-v1/
    chunks/        chunk-YYYYMMDDTHHMM_YYYYMMDDTHHMM.{parquet,json}   # write-once
                   tail-YYYYMMDDTHHMM_YYYYMMDDTHHMM.{parquet,json}    # coda immutabile
    quarantine/    q-<cella>-<hash12>.{parquet,json}                   # mai promosso
    manifests/     dataset-manifest-0001.json, ... , latest.json       # versionati
    registry/      events.jsonl (append-only), sync.lock
```

Ogni chunk dichiara: `chunk_id` deterministico, intervallo esatto (`start_ms`/`end_ms`/UTC),
`cell_start_ms`, `kind` (`grid`/`tail`), righe/righe attese, `sha256` del Parquet,
`data_sha256` del contenuto, `record_chain_sha256`, `ingested_utc`, `source`, `quality`,
`schema_version`, `protocol` (id/versione/hash).

**Catenella crittografica.** `dataset_sha256 = SHA-256(dataset_id, schema, [[chunk_id, data_sha256]...])`
sull'ordine cronologico dei chunk attivi: è il pin da registrare nel registro di validazione
al momento della valutazione (non ora, vedi §6). Ogni manifest porta anche
`chunk_chain_sha256`.

**Versionamento in git.** Il `.gitignore` globale esclude `*.parquet`; per questo archivio è
stata aggiunta un'eccezione esplicita (`!data/forward/**`), perché il dataset forward è
evidenza e deve essere preservato. Restano ignorati solo i file operativi (`registry/sync.lock`,
`var/log/`).

**Revisioni.** Una correzione non sostituisce nulla: nasce un nuovo oggetto versionato
(nuovo `tail`, nuovo manifest, nuovo evento). Un manifest con lo stesso dataset hash non viene
riscritto (nessun churn).

---

## 6. CP08-05-06 · Validation isolation

1. **Hash del protocollo.** `protocol_authority()` carica il YAML, esegue `validate_protocol`,
   verifica il sidecar `.sha256` e confronta `config_sha256`/`canonical_sha256` con il manifest
   dell'archivio CP08-04. Divergenza ⇒ `readiness.isolation.protocol_hash_ok = false` e
   `audit` FWD.7 FAIL.
2. **Boundary.** Il boundary *non è configurabile*: viene letto dal protocollo
   (`independent_validation.start_utc`). Se il YAML prova a dichiararne uno diverso,
   `load_config` fallisce (`diverges from the frozen protocol`).
3. **Divieto di riuso della ricerca.** `audit` FWD.3 ricalcola le barre archiviate (nessuna
   può precedere il boundary); FWD.9 ricalcola gli hash dei 17 artefatti congelati e rifiuta
   qualunque artefatto che viva in `data/forward`.
4. **Separazione fisica.** `guard_archive_layout` impedisce di collocare l'archivio forward
   dentro `data/chunks|snapshots|datasets|experiments|processed`.
5. **Registro dei dataset.** `readiness`/`audit` **leggono** `ValidationRegistry` e verificano
   che non esistano dataset forward registrati né valutazioni. La registrazione è
   deliberatamente **rinviata**: registrare un dataset parziale ne fisserebbe l'hash e
   bloccherebbe quello definitivo (`register_dataset` rifiuta hash diversi per lo stesso id).
6. **Registrazione (rinviata e pronta).** `readiness.registration_candidate` espone il record
   immutabile (`dataset_id`, `sha256`, ruolo `independent`, copertura, protocollo) che la
   valutazione futura potrà passare a `ValidationRegistry.register_dataset`. Nessuna scrittura
   avviene in CP08-05: fissare ora l'hash di un dataset parziale impedirebbe la registrazione
   di quello definitivo.
7. **Isolamento CP09.** Nuovo modulo `forward_isolation.py`:
   - `is_forward_path` riconosce qualunque percorso con segmenti `data/forward` (anche in un
     altro checkout);
   - `guard_research_path` / `guard_research_output` sollevano `ForwardIsolationViolation`;
   - i punti di ingresso della ricerca sono stati **irrobustiti**: `sync.sync`, `freeze.freeze`,
     `cli.ingest` rifiutano un output/input dentro l'archivio forward;
   - `guard_metric_payload` rifiuta payload con metriche di strategia prodotte dal forward.
8. **Nessuna selezione di soglia.** Il modulo non implementa `select_threshold`,
   `evaluate_frame`, label, decisioni, equity o curve; `readiness` non importa codice di
   metrica e i test verificano che `evaluate_independent` **non venga mai chiamato**.

---

## 7. CP08-05-07 · Readiness

`readiness` è offline e read-only. Mostra **solo** disponibilità:

- ultima candela completa acquisita (`last_acquired_bar_utc`) e ultima ora chiusa dalla
  sorgente (`last_closed_bar_utc`);
- ore disponibili / attese, copertura, intervalli validi, quarantene, gap, ore non ancora acquisite;
- finestre potenzialmente eleggibili: contratto CP07 con
  `cutoff = boundary + k·stride`, `lookback = 168h` (warm-up ammesso dallo snapshot congelato),
  `latenza = 1h`, `holding = 24h`, e **contiguità completa** di `[cutoff-168h, cutoff+25h]`;
  una finestra che attraversa un gap non esiste (`gap_crossing_allowed: false`);
- fold possibili = `min(folds_max, finestre // 60)`; requisiti minimi dal protocollo
  (`300 finestre`, `5 fold`, `180 giorni`, `100 trade`);
- stato del protocollo e hash; isolamento.

Stati: `NO_DATA` (archivio vuoto), `NOT_READY`, `DATA_READY_EVALUATION_PENDING`.
Il requisito `min_trades` è dichiarato **non misurabile** prima della valutazione finale: la
raccolta non lo stima. `evaluation_executed` è sempre `false`, `final_evaluation_gate`
sempre `NOT_ACTIVATED`.

---

## 8. CP08-05-08 · Operational automation (systemd)

Frequenza e percorsi stanno **fuori dal codice**: `deploy/systemd/crypto-decision-forward.{service,timer}`,
config in `/etc/crypto-decision/forward-collection.yaml`, log in `var/log/`.

```bash
# installazione (mai automatica)
sudo install -D -m 0644 config/forward-collection.example.yaml /etc/crypto-decision/forward-collection.yaml
sudo cp deploy/systemd/crypto-decision-forward.service /etc/systemd/system/
sudo cp deploy/systemd/crypto-decision-forward.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start crypto-decision-forward.service     # prova singola
systemctl status crypto-decision-forward.service --no-pager
sudo systemctl enable --now crypto-decision-forward.timer

# verifica
systemctl list-timers crypto-decision-forward.timer
journalctl -u crypto-decision-forward.service -n 100 --no-pager
python -m crypto_decision_lab.forward_collection status

# rollback (l'archivio resta valido)
sudo systemctl disable --now crypto-decision-forward.timer
sudo systemctl stop crypto-decision-forward.service || true
sudo rm /etc/systemd/system/crypto-decision-forward.{service,timer}
sudo systemctl daemon-reload
```

Garanzie operative: `Type=oneshot` + flock esclusivo ⇒ **una sola istanza**; exit code 75 è
trattato come esito normale (overlap del timer) e non come errore; retry controllati
nell'adapter; log JSON per riga (journald + file); nessuna valutazione strategica;
`ReadWritePaths` limita la scrittura a `data/forward` e `var/log`.

---

## 9. CP08-05-09 · Testing

`tests/test_forward_collection.py` — 43 test, nessuna rete:

primo sync · sync incrementale · idempotenza (zero fetch) · resume dopo interruzione ·
coda → chunk completo · rate limit con retry · esaurimento retry · conflitto di sorgente ·
gap · candela parziale · timestamp errati · boundary · volume nullo → review ·
promozione write-once · atomicità (nessun `.tmp`) · manifest versionato · chunk corrotto ·
hash di protocollo · audit · registry intatto · isolamento CP09 (guard + pipeline di ricerca) ·
payload metriche rifiutato · archivio dentro la ricerca rifiutato · nessuna registrazione di
dataset · eligibility (contratto CP07, gap non attraversabili) · readiness insufficiente ·
nessuna metrica di strategia nei payload · nessuna chiamata all'evaluator · comandi offline
senza scritture · CLI status/readiness/audit · lock → exit 75 · adapter reale senza rete.

`tests/dashboard/test_forward_status_dashboard.py` — 8 test: adapter, assenza di metriche,
archivio mancante, config mancante, nessuna scrittura, grafici (pieno/vuoto/gap), rendering
Streamlit read-only (4 tab, 6 metriche, 2 grafici).

---

## 10. CP08-05-10 · Dashboard «Forward Data Status»

Nuovo tab read-only (`dashboard/components/forward_status.py`, `dashboard/forward_adapter.py`,
`charts.make_forward_coverage_figure`, `charts.make_forward_readiness_figure`):

- stato acquisizione, ore acquisite/attese, copertura;
- timeline: boundary, intervallo atteso, copertura acquisita, gap, quarantena, prima finestra
  eleggibile;
- ultima candela completa, chunk con SHA-256 e provenienza, dataset SHA-256, versione manifest;
- quarantena con cause, gap, eventi del registry;
- readiness, requisiti minimi mancanti, blocco isolamento (hash protocollo, registro, gate).

La scheda **non** mostra P&L/MAE/segnali/verdetti, **non** scarica e **non** scrive: i comandi
`status`/`readiness`/`audit` sono testati per lasciare l'albero dei file immutato.

---

## 11. Stato dopo CP08-05 (verificato in questo repository)

| Elemento | Valore |
|---|---|
| Dataset forward | `BTCUSDT-1h-forward-v1`, archivio additivo in `data/forward/` |
| Boundary | `2026-10-23T16:00:00Z` |
| Protocollo | `momentum-independent-validation` v1.0.0 · `config_sha256`/`canonical_sha256` verificati |
| Snapshot e artefatti CP07/CP08 | invariati (17 hash ricalcolati) |
| Validazione indipendente | **NOT_EVALUATED** — nessuna valutazione eseguita |
| Gate strategico | **NO-GO** |
| Momentum Confirmation Engine | `SPECIFICATION_ONLY`, 0 regole attive |
| Intern-Decision-0.8B | non avviato |

### 11.1 Stato dell'archivio al primo sync (2026-10-30T15:10Z)

| Campo | Valore |
|---|---|
| Celle acquisite | 7 (6 chunk `grid` da 24h + 1 `tail` da 23h) |
| Barre | **167** (2026-10-23 16:00Z → 2026-10-30 14:00Z) |
| Copertura | 167/167 ore attese (100%), 0 gap, 0 quarantena, 0 conflitti |
| `dataset_sha256` | `d3bc59b041e15ecc4a598c34d51f85ccc1611386a60db35ac607242b6e30e310` |
| Manifest | `dataset-manifest-0001.json` (immutabile) |
| Finestre eleggibili | **6** (cutoff 2026-10-23T16:00Z → 2026-10-28T16:00Z) |
| Fold possibili | **0** su 5 richiesti |
| Readiness | **NOT_READY** (mancano 294 finestre, 5 fold, 174 giorni) |
| Sync successivi | idempotenti (`UP_TO_DATE`, zero rete, nessun nuovo manifest) |
| Audit | 11/11 PASS, gate tecnico PASS |

Il prossimo passo legittimo resta uno solo: lasciare che il timer accumuli dati nuovi, poi
registrare il dataset con il suo `dataset_sha256` e consumare **una** valutazione finale
secondo il protocollo. Nessun backtest sul passato può sostituire questo passaggio.
