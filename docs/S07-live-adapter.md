# S07 — Adattatore live e collaudo

Step S07 del PRD. Il runner live usa la stessa catena di decisione di S03/S06
(feed orario, `momentum_24h` su barre chiuse, `Strategy`, `RiskEngine`) e cambia
solo l'esecuzione: gli ordini passano da un adattatore exchange e il portafoglio
è la somma delle **esecuzioni riportate dall'exchange**.

## Scelte

| Tema | Decisione | Motivazione |
|---|---|---|
| Exchange | Binance Spot, BTCUSDT | È la fonte di tutti i dati del repository (`binance-spot`). Strategia e dati restano coerenti. |
| Ambiente di collaudo | Binance Spot Testnet (`https://testnet.binance.vision`), fondi virtuali | Stessa API di produzione, filtri e limiti "generalmente uguali". Si azzera circa ogni mese. Solo endpoint `/api`. |
| Fixture di risposta | `FakeExchange` (semantica Binance, guasti programmabili, stato su file) e JSON in `tests/fixtures/binance_spot/` | Prove deterministiche di timeout, rifiuti ed esecuzioni parziali, non ottenibili a comando sulla testnet. |
| Mainnet | Bloccata in S07 | Servono `allow_mainnet` nella specifica **e** `CDL_LIVE_ALLOW_MAINNET=1`. L'attivazione è una decisione di S08. |
| Credenziali | Solo variabili d'ambiente | Le variabili sono `CDL_BINANCE_TESTNET_API_KEY` e `CDL_BINANCE_TESTNET_API_SECRET`. Una specifica di sessione con chiavi viene rifiutata. |

### API ufficiale verificata (developers.binance.com, pagine aggiornate al 2026-11-12)

- Firma HMAC-SHA256 della query in esadecimale e header `X-MBX-APIKEY`. `timestamp` è in ms, `recvWindow` vale 5000 (massimo 60000). `-1021` indica un timestamp fuori finestra: l'ordine viene rifiutato e non creato.
- `POST /api/v3/order`: MARKET con `quantity` oppure `quoteOrderQty`, LIMIT GTC con `price`. `newOrderRespType=FULL` restituisce `fills` (`price`, `qty`, `commission`, `commissionAsset`, `tradeId`).
- `newClientOrderId` è unico solo tra gli ordini aperti: dopo il fill lo stesso id può essere riaccettato. **Il runner quindi non reinvia mai un id: prima interroga l'ordine.** La documentazione letta non specifica regex e lunghezza. Usiamo `cdl-<prefisso8>-<seq6>` (`[a-z0-9-]`, 19 caratteri).
- `GET /api/v3/order?origClientOrderId=` interroga l'ordine e restituisce `-2013` se l'ordine non esiste. `DELETE /api/v3/order` lo annulla e restituisce `-2011` se l'annullamento viene rifiutato. `GET /api/v3/myTrades?orderId=` restituisce i fill di un ordine interrogato: al massimo `limit` trade (predefinito 500, massimo 1000). L'adattatore chiede pagine da 1000 e prosegue con `fromId` finché una pagina non è piena. La combinazione `orderId` + `fromId` e l'ordine crescente dei trade nella pagina vanno confermati sul testnet reale (R6): con meno di 1000 trade per ordine non vengono usati. Se una pagina mancasse, l'ordine resterebbe `FILLS_PENDING` fino a `HALTED`: nessuna perdita silenziosa.
- Un HTTP 5xx o `-1007` (timeout) **non indicano un fallimento**: lo stato di esecuzione è sconosciuto e va verificato.
- Stati: `NEW`, `PENDING_NEW`, `PARTIALLY_FILLED`, `FILLED`, `CANCELED`, `PENDING_CANCEL`, `REJECTED`, `EXPIRED`, `EXPIRED_IN_MATCH`. Un MARKET senza liquidità sufficiente termina `EXPIRED` con esecuzione parziale.
- Filtri: `LOT_SIZE`/`MARKET_LOT_SIZE` (`minQty`, `maxQty`, `stepSize`), `PRICE_FILTER` (`tickSize`), `NOTIONAL` o `MIN_NOTIONAL` (`minNotional`).

## Componenti

- `src/crypto_decision_lab/exchange.py`: contratto di esecuzione (`ExecutionAdapter`, `OrderRequest`, `OrderReport`, `Fill`, `SymbolRules`) ed errori. `ExchangeRejected` è terminale. `ExchangeUnknown` indica un esito incerto. `OrderNotFound` indica un ordine assente. Contiene anche `FakeExchange`.
- `src/crypto_decision_lab/binance_spot.py`: `BinanceSpotAdapter` con trasporto iniettabile e `BinanceKlineFeed`, cioè le barre orarie dello stesso ambiente.
- `src/crypto_decision_lab/live.py`: `LiveSession` su SQLite (`var/live/sessions/<id>/live.sqlite`) e la CLI.
- `config/live/testnet.example.json`: configurazione con limiti.
- `scripts/s07_testnet_smoke.py`: collaudo sull'API reale della testnet.

## Ciclo di un ordine (write-ahead)

1. Nella stessa transazione SQLite vengono registrati decisione, rischio, intento d'ordine (`INTENT`) e client id. **Il commit avviene prima dell'invio.**
2. Invio. Un report viene applicato. Un rifiuto porta a `REJECTED`. Un timeout o un 5xx portano a `UNKNOWN`.
3. Ogni `poll` comincia con `reconcile()`:
   - gli ordini `INTENT`, `UNKNOWN` e aperti vengono interrogati per client id. I fill vengono importati una sola volta, con chiave primaria `trade_id`;
   - un ordine non trovato dopo `unknown_grace_seconds` diventa `NOT_PLACED`. La barra successiva decide di nuovo con un **nuovo** client id;
   - gli ordini aperti oltre `order_timeout_seconds` vengono annullati;
   - un ordine aperto sull'exchange con il prefisso della sessione ma assente in locale porta a `HALTED`;
   - i saldi (free + locked) vengono confrontati con baseline, fill e rettifiche. Una differenza porta a `HALTED`.
   - un ordine terminale sull'exchange i cui trade non sommano a `executedQty` resta `FILLS_PENDING` e viene interrogato di nuovo; nel frattempo il controllo dei saldi è sospeso, perché la differenza è attesa;
   - un ordine terminale con trade importati **oltre** `executedQty` diventa `FILLS_EXCESS` e la sessione passa `HALTED` alla riconciliazione successiva, senza tolleranza (dati dell'exchange incoerenti).
4. **Finché un ordine è `INTENT`, `UNKNOWN`, `FILLS_PENDING` o `FILLS_EXCESS`, il runner non elabora barre e non invia ordini.**

**Un attore alla volta.** Runner e comandi operatore tengono il lock esclusivo `session.lock` per tutta l'azione: decisione, commit dell'intento e invio sono un tratto unico. Un secondo processo riceve `session busy` invece di intercalarsi: per esempio un `session-finish` lanciato durante un `poll` viene rifiutato e va ripetuto. Il sistema operativo rilascia il lock alla morte del processo, quindi un runner ucciso non lascia blocchi. `session-run --loop` su lock occupato attende l'intervallo e riprova. `session-status` legge senza lock. Il lock (`msvcrt`/`fcntl`) coordina solo processi **sulla stessa macchina**: runner e comandi operatore devono girare sullo stesso host, mai da host diversi su un filesystem condiviso.

Cassa e posizione derivano solo dalle tabelle `fills` e dalle rettifiche dell'operatore. Le commissioni nell'asset base riducono la posizione e quelle nell'asset quote riducono la cassa. Il residuo sotto `minQty` o `minNotional` è polvere: la posizione risulta chiusa.

## Guardie (CP07.3)

| Guardia | Effetto | Configurazione |
|---|---|---|
| Sospensione manuale | Blocca le aperture. Le uscite restano consentite se `allow_exits_when_suspended` vale `true` (predefinito). | `suspend-openings` / `resume-openings` |
| Feed obsoleto | Una barra più vecchia di `max_signal_age_hours` rispetto all'orologio non può aprire. Senza barre per `stale_after_hours` lo stato diventa `STALE`. | `limits` |
| Limite di esposizione | Il budget è il minimo tra rischio S03, `max_order_notional` e spazio residuo sotto `max_position_notional`. Sotto il minimo dell'exchange l'apertura viene bloccata. | `limits` |
| Saldo disallineato | `HALTED`: blocca **tutti** gli ordini, uscite comprese, perché lo stato non è affidabile. | `balance_*_tolerance` |
| Ordine pendente o irrisolto | Nessun nuovo ordine. | — |
| Regole del simbolo cambiate | `HALTED` alla riapertura della sessione. | — |
| Revisione del codice diversa | `ENGINE_MISMATCH`: nulla viene elaborato fino ad `accept-code-revision`. Con capitale reale (mainnet o sessione creata dal gate S08) serve `--approval` di una nuova scheda READY per **questo** codice. | — |

Ogni blocco produce un record `guard` nel registro, con barra e motivo.

## Runbook

Variabili: `$S` è la directory della sessione e `python -m crypto_decision_lab.live` è abbreviato in `live`.

**Avvio su testnet**

```powershell
$env:CDL_BINANCE_TESTNET_API_KEY = "..."; $env:CDL_BINANCE_TESTNET_API_SECRET = "..."
python scripts/s07_testnet_smoke.py                  # collaudo API: tutti gli step PASS
python -m crypto_decision_lab.live session-create --name testnet-momentum `
  --config config/live/testnet.example.json --exchange binance-testnet --start "2026-11-13 12:00"
python -m crypto_decision_lab.live session-run --session-dir $S --loop --interval 60
```

La creazione fotografa i saldi come baseline. L'account testnet deve essere dedicato alla sessione, oppure ogni movimento esterno richiede un `rebaseline`. `initial_cash` è il capitale assegnato e non può superare il saldo quote.

**Monitoraggio.** Il comando `live session-status --session-dir $S` mostra `status`, `detail`, `cursor`, `position`, `cash`, `fees_quote`, `open_orders` e `halted`. Lo storico degli stati è nella tabella `status_log` e ogni ordine ha il proprio storico nel registro (`order`, `order_update`, `execution`, `order_cancelled`).

| Stato | Significato | Azione |
|---|---|---|
| `WAITING` | Sessione aggiornata | Nessuna |
| `STALE` | Nessuna barra recente | Verificare connettività e feed. Le aperture riprendono da sole con barre fresche. |
| `UNRESOLVED` | Ordine con esito ignoto, `MISSING` o `FILLS_PENDING` | Il runner riconcilia da solo. Se lo stato persiste oltre `unknown_grace_seconds`, verificare la connettività con `session-reconcile`. **Non inviare ordini a mano.** |
| `HALTED` | Saldo disallineato, ordine estraneo, ordine aperto sconosciuto all'exchange (`MISSING`), trade incompleti oltre la tolleranza (`FILLS_PENDING`) o regole cambiate | Vedi "Sospensione e ripristino". |
| `OPENINGS_SUSPENDED` | Sospensione manuale | `resume-openings` quando opportuno |
| `ENGINE_MISMATCH` | Codice diverso da quello fissato | Rivedere il diff, poi `accept-code-revision --note`, oppure tornare al commit fissato. Con capitale reale: nuova scheda S08 e `accept-code-revision --approval <approval.json> --note`. |

**Sospensione e ripristino**

1. Fermare le nuove aperture con `live suspend-openings --session-dir $S --note "motivo"`. Il runner continua a riconciliare e a gestire le uscite.
2. Ordini aperti: `live cancel-open-orders --session-dir $S --note "motivo"`. Il runner riconcilia, poi annulla. I fill parziali restano nella posizione.
3. Posizione: per chiuderla si usa `live flatten --session-dir $S --note "motivo"`, cioè una SELL MARKET dell'intera posizione negoziabile. Il comando viene rifiutato se ci sono ordini aperti o irrisolti oppure se la sessione è `HALTED`. Per mantenerla basta lasciare la sessione sospesa: l'uscita a orizzonte resta attiva.
4. `HALTED` per saldo: confrontare il record `guard` con i movimenti dell'account. Se la differenza è spiegata, eseguire `live rebaseline --session-dir $S --note "spiegazione"`. La rettifica entra nella posizione o nella cassa della sessione, così le uscite vendono ciò che esiste davvero. Se non è spiegata, la sessione resta `HALTED` e si apre un'indagine.
5. Riavvio dopo un crash: rilanciare `session-run`. La prima azione è la riconciliazione e nessun ordine viene inviato finché tutto non è risolto.
6. Ordine aperto sconosciuto all'exchange (`MISSING`). Succede per esempio dopo l'azzeramento mensile della testnet o con un ordine archiviato.
   - Durante `unknown_grace_seconds` lo stato è `UNRESOLVED`. Se l'ordine ricompare, torna allo stato reale.
   - Oltre la tolleranza la sessione passa `HALTED` e nessun ordine viene inviato.
   - Controllare lo storico ordini e i trade sull'exchange.
   - Eseguire `live resolve-missing-order --session-dir $S --client-order-id <id> --note "evidenza"`: l'ordine diventa `LOST`, terminale.
   - Eseguire `rebaseline` con nota: le esecuzioni non riportate emergono come differenza di saldo.
7. Ordine eseguito con trade incompleti (`FILLS_PENDING`): l'exchange lo dà per chiuso ma `myTrades` non restituisce tutti i trade.
   - Durante `unknown_grace_seconds` lo stato è `UNRESOLVED`. Se i trade arrivano, l'ordine prende lo stato dell'exchange.
   - Oltre la tolleranza la sessione passa `HALTED`. Il blocco resta anche se i trade arrivano dopo: va tolto con `rebaseline`.
   - Controllare i trade dell'ordine sull'exchange, poi `live resolve-missing-order --client-order-id <id> --note "evidenza"`: l'ordine diventa `FILLS_INCOMPLETE`, terminale.
   - Eseguire `rebaseline` con nota: l'esecuzione non importata entra come rettifica di posizione e cassa.
   - Stesso percorso per `FILLS_EXCESS` (trade oltre `executedQty`): dopo la verifica sull'exchange `resolve-missing-order` lo porta a `FILLS_EXCESS_ACCEPTED`, poi `rebaseline`.
8. Comandi ammessi per stato:
   - `COMPLETED` rifiuta ogni comando; serve una nuova sessione.
   - `ENGINE_MISMATCH` ammette solo `session-status`, `suspend-openings`/`resume-openings` (lo stato non cambia) e `accept-code-revision`. Ogni azione sull'exchange va fatta direttamente sull'exchange, poi va eseguito `rebaseline` dopo l'accettazione.
9. Chiusura: `live session-finish --note "..."` viene rifiutato se ci sono ordini aperti o se il runner sta elaborando (`session busy`: ripetere). Il record `end` riporta l'eventuale posizione ancora aperta. Dopo la chiusura la sessione non accetta più comandi.

## Checkpoint

| CP | Prova | Esito |
|---|---|---|
| CP07.1 | `pytest tests/test_live.py -k cp071` copre invio e fill MARKET con uscita a orizzonte, rifiuto `-2010` con nuovo id, LIMIT parziale seguito da fill e annullamento per timeout, MARKET parziale `EXPIRED` e LIMIT in attesa annullato. `assert_reconciled` confronta fill, `executedQty`, stato e saldi. Il parsing Binance è coperto dalle fixture, la classificazione degli errori da 8 casi e il percorso adattatore + runner dallo shim. | PASS su fixture. Testnet reale: `scripts/s07_testnet_smoke.py`, da eseguire con le chiavi. |
| CP07.2 | `-k cp072` copre il timeout dopo l'invio, anche con interrogazione fallita, il timeout prima dell'invio (`NOT_PLACED`), il crash prima e dopo l'invio con riapertura, il riavvio con ordine in attesa poi parzialmente eseguito e il kill del processo CLI (`os._exit(97)`). Ogni caso prevede un solo `submit` per decisione. | PASS |
| CP07.3 | `-k cp073` copre il limite di esposizione (cap e blocco), il feed obsoleto (`STALE` e barre tardive), il saldo disallineato (`HALTED` con uscite bloccate e `rebaseline`), la sospensione manuale (uscite consentite) e `flatten`/`finish`. | PASS |
