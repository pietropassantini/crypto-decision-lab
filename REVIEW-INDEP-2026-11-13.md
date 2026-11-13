# Revisione indipendente — 2026-11-13

Revisore: Codex, GPT-6 (identità fornita dall'ambiente; identificatore di build non esposto).
Base: `53f15efd2f4e3a0020313dea08049d7c6cfa9741`, branch sorgente prd/integration.

## Esito e rilievi nuovi

### R1 — HIGH — S07/S08: cambio codice senza nuova approvazione

`live.py:832` (`accept_code_revision`) sostituisce il digest e imposta RUNNING senza verificare l'approvazione S08. Il costruttore controlla il pin, ma non richiama `verify_activation`. Il runbook S08 richiede invece una nuova scheda e approvazione a ogni cambio codice.
Riproduzione offline: `var/review-indep/reproduce-2.py`, comando `var/review-indep/venv/Scripts/python.exe var/review-indep/reproduce-2.py` (usare una nuova directory repro-data a ogni esecuzione). Su FakeExchange, impostare ENGINE_MISMATCH e cambiare la funzione digest in memoria; accept_code_revision produce RUNNING/new-code conservando il riferimento di approvazione precedente. Il riferimento sintetico serve a mostrare che il metodo non lo consulta: non è una prova di attivazione mainnet reale. L'applicabilità alla mainnet deriva dall'assenza di qualsiasi condizione sull'ambiente nel metodo e nel percorso di riapertura.
Correzione proposta: richiedere e verificare una nuova approvazione per il cambio di codice di sessioni mainnet.

### R2 — MEDIUM — S07: fill incompleti diventano terminali

`live.py:423–458` registra lo stato FILLED anche quando la somma dei fill differisce da executedQty. Scrive soltanto «fills pending». L'ordine terminale esce da unresolved_orders e non viene più interrogato. `binance_spot.py:trades` effettua una singola chiamata myTrades senza paginazione. Binance documenta limite predefinito 500 e massimo 1000, con paginazione fromId: un ordine con molti fill può quindi perdere parte dello storico locale. La guardia saldi può fermare il trading, ma non recupera i fill mancanti.
Riproduzione nello stesso script: applicare un report FILLED con executedQty positivo e fills vuoto a un registro locale privo di fill. Osservato: `FILLED 0 0 fills 0 differ from executedQty 4.21798: fills pending` (stato, numero fill, numero ordini irrisolti). È una prova del caso limite nel contratto, non una misura della frequenza su Binance.
Correzione proposta: paginare myTrades e mantenere riconciliabile ogni ordine con fill incompleti, anche terminale lato exchange.

### R3 — HIGH — S07: ordine inviato dopo finish concorrente (F1 insufficiente)

`live.py:647–696` verifica COMPLETED solo all'ingresso di poll. Non ricontrolla lo stato prima della decisione o dell'invio. Un altro processo può completare la sessione dopo il controllo iniziale.
Riproduzione deterministica nello stesso script: due LiveSession sullo stesso SQLite; la prima lettura feed durante poll fa eseguire finish sulla seconda connessione, poi restituisce la barra. Osservato: stato COMPLETED, un ordine, registro `end, market, decision, risk, order, balance, execution, order_update`. Non sono necessari thread o rete: l'interleaving riproduce la sequenza concorrente.
Correzione proposta: serializzare runner e comandi operatore con un protocollo che protegga anche il tratto fra commit dell'intento e invio; ricontrollare lo stato sotto tale protezione.

## Metodo e prove

Letti PRD, handoff S06–S09, review precedente, documentazione S07–S09 e i percorsi rilevanti di live.py, exchange.py, binance_spot.py, release.py, ops_report.py, paper.py e test.
Nessun commit, push, modifica al codice di prodotto o stato del backlog. Nessuna credenziale reale o richiesta agli endpoint exchange. Consultata soltanto la documentazione pubblica.
Clone locale senza checkout iniziale; sparse checkout con `/data/forward/` esclusa prima del checkout. Non ispezionato l'archivio forward originale. I test costruiscono propri fixture sintetici, anche con directory denominate data/forward, sotto var/review-indep.
Venv nuovo **con system-site-packages**, come nella procedura della review precedente: non è una ricostruzione indipendente delle dipendenze. Installazione editable con `--no-deps --no-build-isolation`; il backend disponibile è stato riutilizzato. Git, ensurepip e pip hanno richiesto esecuzione autorizzata fuori sandbox per WinError 5/Permission denied.

| Prova/comando | Atteso | Osservato | Esito |
|---|---|---|---|
| git clone --no-checkout --local . var/review-indep/clone; sparse-checkout; checkout 53f15ef | Base esatta, forward escluso | HEAD 53f15ef; status clone pulito dopo installazione | PASS |
| python -m venv --system-site-packages var/review-indep/venv; pip install -e . --no-deps --no-build-isolation | Pacchetto installabile | Installazione completata | PASS con limite dipendenze condivise |
| pytest -q -p no:cacheprovider --basetemp ../pytest-clean dal clone | 480 passed | Vedere esito conclusivo sotto; log pytest-clean.log | Esito sotto |
| CP06.1–3, CP07.1–3, F1–F4: test_paper.py e test_live.py inclusi nella suite | Resume, feed, pin, ordini, timeout, guardie | Regressioni aggiuntive R1/R2/R3 separate dalla suite | Esito suite sotto |
| CP08.1–3: test_release.py incluso nella suite | Drill CLI, criteri write-once, BLOCKED, gate, decisioni append-only | Controllo del codice del gate confermato; R1 riguarda percorso successivo all'avvio | Esito suite sotto |
| CP09.1–2: test_ops_report.py incluso nella suite | Totali riconciliati e ipotesi coerenti | Inoltre rigenerato due volte il registro storico drill-c | Esito suite sotto |
| simulation --source BTCUSDT-1h-raw-v1 --config config/simulation.example.json --output-dir ../sim-a, poi ../sim-b | ledger byte-identico | Entrambi SHA256 A9D732EECBD4F2BFF035D6C488E20B59FE528143C0D9189333BB3735F763B5EA; HALTED_GAP, cash 9834.02390961377 | PASS ripetibilità, non confronto pre-refactor |
| python var/review-indep/report-check.py | Hash drill-c storico identico | Due rigenerazioni e riferimento: 0fae4fc93e7a52eebbee0f77868ca5a0af5fa0dc70dda651dd949f94e0d80540 | PASS |
| python var/review-indep/reproduce-2.py | Nessun trading dopo finish; fill incompleti recuperabili; nuova approvazione | R1/R2/R3 riprodotti | FAIL |

I tentativi pytest in sandbox (`pytest.log`, `focused.log`) hanno incontrato errori di accesso alle directory temporanee: non sono evidenza di regressioni del prodotto. Il run fuori sandbox è la prova di riferimento.

## Verdetto F1–F10

| ID | Verdetto | Evidenza/limite |
|---|---|---|
| F1 | Insufficiente | Il percorso sequenziale è protetto da _require; R3 riproduce ordine dopo finish concorrente |
| F2 | Insufficiente come garanzia S08 | Suspend/resume preservano ENGINE_MISMATCH; R1 aggira il requisito di nuova approvazione tramite accept-code-revision |
| F3 | Confermata nel percorso coperto | MISSING, tolleranza su updated_at, HALTED, LOST e rebaseline presenti; regressioni nella suite. Non è una prova di ogni interleaving concorrente |
| F4 | Confermata nel percorso coperto | COMPLETED rifiuta suspend; resume richiama _load_pins; regressioni nella suite |
| F5 | Confermato LOW | ops_report.py:190–202 usa quote_quantity/reference_open come quantità richiesta: slippage può ridurre il rapporto anche su FILLED |
| F6 | Confermato LOW staticamente | build_adapter/open_feed risolvono rispetto a repo_root; FakeExchange crea stato nuovo se manca il file. Non ripetuto il cambio cwd |
| F7 | Confermato LOW | git status iniziale espone stackdump, reports, manifests, diagnostics non tracciati; .gitignore non li esclude |
| F8 | Confermato mismatch; ridimensionato | Report S05 contiene 6ff68d76…, motore attuale b3c69252…; corretto considerarlo risultato storico. Esperimento S05 non ripetuto, equivalenza economica non riconfermata |
| F9 | Confermata assenza in Git | git ls-files sui due file non restituisce risultati. Decisione di packaging resta aperta; UI Forecast non provata manualmente |
| F10 | Confermato INFO | Due CLI terminano HALTED_GAP; comportamento coerente con la semantica di gap |

## Raccomandazioni al coordinatore

| Step | Raccomandazione | Motivo |
|---|---|---|
| S00A | resta IN_REVIEW | F7/F9 e CP00A.4 non ripetuto per vincolo forward; inventario non ripercorso voce per voce |
| S01 | resta IN_REVIEW | Prove UI automatiche nella suite, ma nessuna nuova verifica sull'archivio forward reale |
| S02 | resta IN_REVIEW | Suite disponibile; doppia build separata non ripetuta e dipendenza S00A aperta |
| S03 | resta IN_REVIEW | Doppio CLI identico; revisore GPT-6 coincide con autore dichiarato del codice originario, indipendenza valida solo per modifiche Claude |
| S04 | resta IN_REVIEW | Prova manuale dell'app autonoma non eseguita |
| S05 | resta IN_REVIEW | Esperimento completo non rigenerato; F8 documentato |
| S06 | VERIFIED | CP06 e F4 ripetuti nei test; raccomandazione tecnica, dipendenze aperte da risolvere dal coordinatore |
| S07 | resta IN_REVIEW | R1–R3; collaudo testnet reale esplicitamente riservato all'operatore |
| S08 | resta IN_REVIEW | R1 e dipendenza S07; candidata storica BLOCKED, nessuna attivazione proposta |
| S09 | VERIFIED | Hash storico identico, test riconciliazione/ipotesi; F5 LOW resta aperto. Valido per fixture/paper, non esecuzioni reali |

Non ripetuti: package wheel installato separatamente, valutazione completa della candidata storica S08, esperimento S05, UI manuale, testnet reale e audit forward. Il drill della suite usa il CLI del Python di test; non equivale a un nuovo collaudo del wheel installato. Nessuna dichiarazione di readiness live.

## Documentazione Binance consultata

- [Account e myTrades](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/account): endpoint, orderId, fromId, limite 500/1000; fondamento della paginazione R2.
- [Trade](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/trade): endpoint ordini e campi di richiesta.
- [General REST](https://developers.binance.com/en/docs/products/spot/rest-api): timeout -1007 con esito incerto, interrogare lo stato.
- [Errori](https://developers.binance.com/en/docs/products/spot/errors): -1006/-1007 indicano esito incerto, coerente con la classificazione dell'adattatore.
Firma HMAC-SHA256 e vettore ufficiale presenti nel test_signature_matches_the_official_example della suite. Non eseguite chiamate autenticate.

## Artefatti creati

- Questo report (non committato).
- var/review-indep/: clone sparse, venv, log pytest, directory di test, sim-a/sim-b, reproduce.py/reproduce-2.py e log, report-check.py e log, ops-regenerated.json, fixture repro-data/repro-data-2.

## Esito conclusivo della suite

Run autorizzato fuori sandbox completato: **480 passed in 351.05s (0:05:51)**, exit code 0. Evidenza: var/review-indep/pytest-clean.log. Confermati quindi i checkpoint automatici S06–S09 e i sei test di regressione F1–F4 inclusi nella suite; i nuovi casi R1–R3 non sono coperti da quei test e restano riprodotti separatamente. Raccomandazioni S06 e S09 VERIFIED nei limiti dichiarati; S07/S08 restano IN_REVIEW.

