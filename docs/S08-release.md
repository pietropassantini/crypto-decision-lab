# S08 — Rilascio e attivazione controllata

Lo step S08 trasforma una versione candidata in una decisione esplicita sulla prima operatività con capitale reale. Il tooling **non attiva nulla**: produce evidenze, blocca ciò che non coincide con quanto approvato e registra la decisione dell'operatore.

## Percorso

```text
criteri (write-once) ─► pacchetto + drill del runbook ─► scheda READY|BLOCKED ─► approvazione ─► decisione
config/release/…          var/release/package             releases/<cand>/<code8>/   approval.json   decisions.jsonl
```

| Passo | Comando (`python -m crypto_decision_lab.release …`) | Artefatto |
|---|---|---|
| 1. Criteri **prima** di ogni valutazione | `register-criteria --criteria config/release/criteria-<cand>.json` | `releases/<cand>/criteria.json`, con timestamp e digest della configurazione live candidata |
| 2. Pacchetto | `package --out var/release/package` | Wheel e venv pulito: il pacchetto è installato, non letto da `src/` |
| 3. Drill CP08.1 | `drill --candidate <cand> --python var/release/package/venv/Scripts/python.exe --workdir var/release/drill-<cand>` | `drill.json` (step, atteso, osservato, esito) |
| 4. Scheda CP08.2 | `build-card --candidate <cand> --drill …/drill.json --package-out var/release/package` | `release-card.json` / `.md`, write-once per revisione del codice |
| 5. Approvazione | `approve --card … --operator <nome> --note <motivo>` | `approval.json`, solo per schede READY |
| 6. Verifica pre-avvio | `verify --approval … --config config/live/<cand>.json` | Esito della verifica; un errore elenca ogni differenza |
| 7. Decisione CP08.3 | `decide --release-dir … --decision ACTIVATED\|NOT_ACTIVATED --decided-by … --reason …` | `decisions.jsonl` append-only |

### Scheda di rilascio

La scheda riporta:
- **versione**: commit, percorsi non committati, versione e sha256 del wheel, digest del codice motore + live (indipendente dal checkout);
- **criteri**: sha256 e data di registrazione, che deve precedere la scheda;
- **destinazione**: exchange, ambiente, simbolo, capitale e limiti;
- **evidenze**:
  - backtest sulla finestra registrata: i gap dello snapshot sono riconosciuti con la regola S06 e contati, con i controlli `backtest.completed` e `backtest.window_covered`;
  - paper: sessione S06 persistente in replay sulla finestra successiva, che deve riprodurre il ledger del backtest;
  - collaudo testnet S07, drill e stato del backlog PRD.

Ogni controllo riporta osservato, soglia ed esito. Basta un controllo fallito per rendere la scheda `BLOCKED`, con i motivi.

### Gate di attivazione

`LiveSession.create` su Binance **mainnet** richiede `exchange.approval`. `verify_activation` rifiuta la sessione se:
- la scheda non è READY, oppure è stata modificata dopo l'approvazione (sha256);
- la configurazione live normalizzata differisce da quella approvata (strategia, capitale, limiti, tipo d'ordine);
- exchange, ambiente o simbolo differiscono;
- il digest del codice differisce da quello approvato.

L'adattatore richiede inoltre `CDL_LIVE_ALLOW_MAINNET=1` e le chiavi `CDL_BINANCE_API_KEY`/`CDL_BINANCE_API_SECRET` nell'ambiente.

```powershell
python -m crypto_decision_lab.live session-create --name prod-<cand> --config config/live/<cand>.json `
  --exchange binance-mainnet --approval releases/<cand>/<code8>/approval.json --start "<ora UTC>"
```

## Runbook operativo

Il runbook di avvio, monitoraggio, sospensione e ripristino è quello di S07 ([docs/S07-live-adapter.md#runbook](S07-live-adapter.md#runbook)). Il drill CP08.1 lo ripete in collaudo con i CLI del pacchetto installato. Per la produzione valgono in più queste regole:

1. **Avvio.**
   - Usare un account dedicato e capitale pari a quello approvato.
   - Eseguire `release verify`, poi `session-create` con `--approval`.
   - Avviare `session-run --loop` sotto un supervisore che lo riavvia: alla ripartenza il runner riconcilia prima di ogni invio.
2. **Prima riconciliazione (CP08.3).** Subito dopo l'avvio e dopo il primo ordine:
   - `session-status` deve risultare `WAITING` e mostrare `halted: null` e `open_orders: []`;
   - ogni `execution` deve avere un `trade_id` riscontrabile nello storico ordini Binance.
3. **Monitoraggio.** Controllare almeno ogni ora `session-status` e la tabella degli stati di S07. `STALE`, `UNRESOLVED` oltre la finestra di tolleranza, `HALTED` ed `ENGINE_MISMATCH` richiedono intervento.
4. **Sospensione.** Usare `suspend-openings`, poi decidere su ordini (`cancel-open-orders`) e posizione (`flatten`, oppure mantenerla fino all'uscita a orizzonte).
5. **Ripristino.**
   - Dopo un crash basta rilanciare `session-run`.
   - Dopo `HALTED`, spiegare la differenza di saldo prima di `rebaseline`.
   - Un cambio di codice o di limiti richiede una **nuova scheda e una nuova approvazione**: il gate rifiuta la vecchia. Su una sessione già attiva il cambio di codice passa da `live accept-code-revision --approval <nuova approval.json>`, che riesegue `verify_activation` sul nuovo digest (revisione indipendente, R1).
6. **Disattivazione.** `suspend-openings`, `cancel-open-orders`, `flatten`, `session-finish` e infine una nuova voce `decide` con lo stato operativo risultante.

## Esito della prima esecuzione (2026-11-13)

Candidata `btcusdt-momentum-v1`: strategia momentum con soglia 0, orizzonte 6h, capitale 500, ordine massimo 100, posizione massima 200. È la candidata deterministica ammessa dal PRD dopo il rifiuto del Decision Model in S05.

- Criteri registrati alle 17:05:48Z, prima di ogni valutazione.
- Drill CP08.1: 15/15 step superati, eseguito dal wheel installato nel venv pulito.
- Scheda `releases/btcusdt-momentum-v1/fd1153cd/release-card.md`: **BLOCKED**.
  - Backtest 2021-02 → 2024-12: -96,0%, drawdown -96,4%, 2214 trade, costi 535 su 500 di capitale.
  - Paper 2025-01 → 2026-09: -91,1%, 1556 trade; riproduce esattamente il ledger del backtest.
  - Collaudo testnet assente, step S00A–S07 non VERIFIED, rilascio da working tree con modifiche non committate.
- Decisione **NOT_ACTIVATED**, con requisiti aperti e modalità operativa paper/testnet.
- Diagnostica fuori criteri: senza costi la stessa candidata fa +40,5% (3559 trade, drawdown -29%). Il segnale ha un contenuto, ma il turnover a soglia 0 lo azzera. È un'ipotesi per S09.
- Una prima scheda è stata ritirata in `superseded-1/`: il backtest si fermava al primo gap dello snapshot. I criteri sono rimasti invariati.
