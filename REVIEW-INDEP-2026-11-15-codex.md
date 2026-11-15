# Revisione indipendente E05-P1 R2 · 15 novembre 2026

**Verdetto: specifica E05-P1 non preregistrabile al commit `524a61d`.** Il calcolo storico al lunedì di trigger si riproduce. Una chiamata in produzione può però confermare una sessione fuori perimetro o fissare un esito prima che la barra del lunedì sia disponibile. Un gap riconosciuto può inoltre invalidare una sessione regolare.

**Revisore.** OpenAI Codex, famiglia GPT‑6. L'identificativo esatto della variante e della build non è esposto in questa sessione. Gli autori della revisione tecnica da verificare hanno usato Claude Opus 5.5. Il revisore è distinto anche da DeepSeek v4 Pro e GLM‑5.3.

**Base.** `524a61d381f46377ecdf306f504456a6fc620d10` su `main`, checkout detached in un clone pulito. Prompt in `docs/REVIEWER-PROMPT-E05-P1-R2.md` a `21e364d`. Perimetro `450591f..524a61d`. Ho letto la revisione DeepSeek, la specifica JSON, la guida, la riga E05-P1 del PRD e il diff. I test e i dati sono rimasti fuori da CP08. Il checkout ha escluso `data/forward/` prima delle prove. Le prove in modalità produzione hanno usato copie, orologio simulato e client HTTP finto. Le prove sono locali e il runner non ha interrogato il mercato. Il report è l'unico file di lavoro non ignorato da Git, senza commit o push.

## Prove

`$P` indica `var/review-indep/venv/bin/python`, con `PYTHONPATH=src:var/review-indep`. I log locali delle prove sono in `var/review-indep/`. La lettura Parquet alternativa è circoscritta al runner di prova. Il codice versionato di caricamento, verifica dei checksum, sessione, motore e report è quello della base dichiarata.

| Comando o prova | Atteso | Osservato | Esito |
|---|---|---|---|
| `$P -m pip install --no-index --no-build-isolation -e '.[dashboard,dev]'` | Ambiente nuovo con le dipendenze | Installazione offline si ferma a `requests>=2.31`, assente nel runtime Python 3.12 | Bloccata |
| `$P -m pytest -q -p no:cacheprovider` | 608 passed | `No module named pytest`. Nessun conteggio di suite verificato | Non eseguibile |
| `$P -m crypto_decision_lab.paper session-create … --snapshot BTCUSDT-1h-raw-v1` | Creazione per via standard | Lettura del primo chunk rifiutata per assenza di PyArrow e Fastparquet | Bloccata |
| `$P var/review-indep/offline_cli.py crypto_decision_lab.paper session-create …`, seguito da `session-run` con lo stesso wrapper | Config candidata, sessione COMPLETED | `e05conf-17ba1f7f38`, digest atteso, cursor 2026-09-30 23:00 UTC, equity 524,912532. Il wrapper usa un lettore locale per le sole colonne Parquet numeriche richieste | PASS con adattamento |
| `session_fidelity --session-dir <e05conf>` | Fedeltà del replay | `match=true`, 15.312 eventi e 61.319 record su entrambi i registri, exit 0 | PASS |
| `session_report edge --as-of "2025-07-13 23:00"` | NOT_DUE, exit 0 | NOT_DUE per lunedì, exit 0. Il payload espone già le cifre di 17 trade sull'intero replay, dato oltre l'istante richiesto | Esito PASS, R43 sul payload |
| `session_report edge --as-of "2025-07-14 00:00"` e `"2026-10-01 00:00"` | NOT_CONFIRMED, metriche identiche | Exit 1 in entrambi i casi, cifre e criteri identici. Ritardi di 0 e 444 giorni | PASS |
| `session_report week12 --as-of "2026-10-01 00:00"` | OPERATIONAL_FAIL per la puntualità del replay | Exit 1, 0/638 giorni puntuali, 0 giorni mancanti, fedeltà positiva | PASS |
| `$P var/review-indep/recalc.py` | Ricalcolo indipendente delle metriche | Valori riconciliati con tolleranza assoluta 1e−8. Somma di cassa e quota riconciliata su ogni saldo | PASS |
| `$P var/review-indep/probes.py` · gap singolo, multiplo, iniziale, finale, prima/dopo riconoscimento | Gap riconosciuti esclusi dal denominatore, giorni mai elaborati penalizzati | 8/8 e 6/6 dopo la ripresa, 0/1 al primo giorno mancante, 4/5 con coda mai elaborata. Il replay confronta male i gap riconosciuti | PASS per puntualità, R41 per fedeltà |
| Stesso script · digest, feature, fonte, mode, spec alternativa | Solo la candidata corretta sulla fonte ammessa è valida | Feature errate → INVALID. ETHUSDT o mode `paper-replay` con kind `public_klines` → nessuna ragione INVALID. Una spec alternativa coerente permette CONFIRMED | FAIL, R35 |
| Stesso script · hash di tutti i file prima/dopo `edge(as_of=…)` | Byte e nomi identici | Sessione sana chiusa: `session.sqlite` e `session.json` identici. SQLite aperto in WAL: cambia `-shm`. Digest motore discordante: cambia lo stato in ENGINE_MISMATCH, il database cambia e durante la prova scompaiono o cambiano i sidecar; l'esito può restare CONFIRMED | FAIL, R38 |
| Stesso script · decisioni su copie e interleaving delle scritture | Una sola decisione edge e una riga per esito week12 | Cancellare solo il JSON o copiare dopo la decisione dà 2. Copia anteriore e cancellazione di JSON+riga riaprono. Due letture prima degli INSERT consentono due diversi esiti edge. Week12 conserva PASS e FAILED in file distinti | Parziale, R40 |
| `$P var/review-indep/more.py` · inserimento dell'evento del lunedì | Una decisione solo su dati completi al confine | Stessa sessione: CONFIRMED sulle 23:00 di domenica, NOT_CONFIRMED dopo il commit delle 00:00 | FAIL, R36 |
| Stesso script · caduta dopo il lunedì e controllo scadenza in `probes.py` | FAILED a ogni violazione successiva. INCONCLUSIVE oltre il termine se il trigger è tardivo | Drawdown −45,96% con fedeltà positiva: week12 FAILED, edge CONFIRMED. A 53 settimane, trigger tardivo → CONFIRMED | FAIL, R37/R39 |
| Stesso script · tariffa della candidata modificata e digest simulato diverso | Errore esplicito prima del calcolo | `ReportError` espone il digest fissato e quello appena calcolato in entrambi i casi | PASS |
| Conteggio statico con `ast.parse` e `git diff --check 450591f..524a61d` | Backlog allineato, patch ben formata | 9 funzioni in `test_paper_daily.py`, una parametrizzata ×2, quindi 10 casi. 11 funzioni in `test_session_report.py`. Diff check exit 0 | PASS |

### Replay di ricerca e ricalcolo

Configurazione generata prendendo `strategy`, `initial_cash`, `hypothesis` e `risk` dalla candidata della specifica. Digest calcolato `7775ccd417e3354c981248cf1469f6cab447844638b88301756fb59c099c5536`, feature 28/100. Nel wrapper locale è stato sostituito solo `pandas.read_parquet`, con lettura delle pagine numeriche del formato Parquet e decompressione Snappy/Zstandard. La normale procedura del repository ha verificato i checksum dei chunk del manifest prima di leggerli. Resta da ripetere il percorso canonico con PyArrow.

| Misura | Fino a lun 14 luglio 2025, 00:00 UTC incluso | Intero replay di conferma |
|---|---:|---:|
| Eventi | 4.657 | 15.312 |
| Trade chiusi | 8 | 17 |
| Netto realizzato | −10,7478 (−2,1496%) | −23,9264 (−4,7853%) |
| Costi e lordo sui chiusi | 5,8403 e −4,9075 | 12,4033 e −11,5231 |
| Quota costi sul lordo | Indefinita | Indefinita |
| Equity e rendimento | 506,1294 (+1,2259%) | 524,9125 (+4,9825%) |
| Posizione aperta | Dal 10 luglio 2025, +16,8772 | Dal 20 agosto 2026, +48,8390 |
| Buy and hold | +13,5356% | −5,3515% |
| Drawdown | −7,2843% | −9,3294% |

Il ricalcolo legge `ledger` in ordine di sequenza con SQLite e usa il prezzo open di ogni market event. Abbina BUY/SELL dal registro, sottrae le fee alla cassa, valorizza la quota all'ultimo open e calcola il minimo equity/picco cumulato − 1 partendo da 500. Buy and hold investe 250 al primo open con gli stessi slippage e fee. La quantità è `250 / (open_iniziale × 1,0005 × 1,001)`, la cassa residua è 250. Non importa `session_report` o `studio_analytics`. L'ottavo SELL chiude l'8 luglio a mezzanotte. Al lunedì falliscono netto realizzato, costi e confronto con il benchmark. La soglia del benchmark è +3,5356%, sopra il +1,2259% osservato.

## Rilievi nuovi

| ID | Severità | Oggetto | Evidenza e riproduzione minima |
|---|---|---|---|
| R35 | HIGH | Il report può confermare una sessione fuori perimetro | `invalid_reasons` confronta digest dichiarato, feature e `source.kind` con la spec passata. Non confronta il config effettivo con il digest, il simbolo con BTCUSDT, `mode` con `paper`, o avvio con preregistrazione. Non impedisce che una spec DRAFT o sostitutiva ridefinisca il filtro. Su una copia sintetica coerente, impostare `meta.source` a `public_klines` con simbolo ETHUSDT e identità corrispondente, lasciare `mode=paper-replay`, usare un client finto e invocare `edge` in modalità produzione. Risultato CONFIRMED privo di motivi INVALID. Un secondo caso usa una spec auto-coerente con `source_kind=lab_feed` e passa il CLI `--spec`. Week12 può essere OPERATIONAL_FAIL mentre edge è CONFIRMED. I due strumenti non verificano ordinamento e autorizzazione della sessione attiva. |
| R36 | HIGH | Decisione definitiva prima che l'evento di trigger sia registrato | Dopo l'ottavo trade, arrestare il runner a domenica 23:00. Chiamare edge al lunedì 00:00. Restituisce CONFIRMED con `last_event` domenica 23:00. Far registrare la barra di lunedì 00:00 con open sintetico 240 e rilanciare la prova. Restituisce NOT_CONFIRMED, con buy and hold che passa da −4,17% a +69,82%. Il controllo d'ora in `edge` ammette il report senza verificare il cursore. In produzione il primo esito avrebbe già occupato il checkpoint. |
| R37 | MEDIUM | Il termine di 52 settimane dipende da quando si apre il report | In `edge` i due rami per `fired` precedono il controllo 52 settimane. Su un clone sintetico con start impostato 53 settimane prima del lunedì, ottavo trade dopo la scadenza e report invocato tardi, esce CONFIRMED. Serve confrontare con il termine l'istante di trigger, indipendentemente dall'istante della prima esecuzione. Va esplicitato il caso in cui l'ottavo trade cada prima della scadenza e il lunedì dopo. |
| R38 | MEDIUM | `--as-of` può modificare SQLite e contattare la fonte | Su una copia di sessione attiva con `meta.engine_sha256` discordante, `edge(as_of=…)` cambia `state` e `status_log` via `PaperSession._load_pins`. Varia l'hash del database. Su una copia sana con connessione WAL già aperta varia l'hash di `-shm`, con DB e WAL uguali nella prova. Con fonte pubblica finta ho contato richieste `/api/v3/time`: `check → run_engine → session.snapshot → _feed_clock`. Sono inutili al rapporto e introducono attesa e dipendenza dall'exchange. |
| R39 | MEDIUM | Lo stop sul drawdown usa due orizzonti incompatibili | Far cadere l'open a 10 in un evento dopo il trigger con posizione aperta. Il replay resta fedele e week12 osserva −45,96% → FAILED. Edge successivo usa solo il −1,81% al trigger → CONFIRMED. Il drawdown del criterio edge deve restare troncato, mentre la regola di stop dichiarata «in qualsiasi momento» deve osservare i dati disponibili fino all'istante del rapporto. |
| R40 | MEDIUM | L'unicità di edge è per coppia checkpoint/esito | Intercalare `_decided(A)=False`, `_decided(B)=False`, `_record(CONFIRMED)`, `_record(FAILED)`. Entrambi gli INSERT riescono e rimangono due righe edge, con il JSON che contiene solo CONFIRMED. Con lo stesso esito il secondo INSERT può essere ignorato senza exit 2. La copia dopo decisione e la cancellazione del solo JSON danno 2, ma una copia anteriore o la rimozione di JSON+riga riaprono. Un crash tra INSERT e file lascia la decisione nel DB ma il CLI non ripristina il JSON. |
| R41 | MEDIUM | Gap regolari invalidano edge, mentre la fedeltà non prova la chiusura di una barra | Nel lab-feed togliere la barra 2025-01-05 00:00, fare poll, acknowledge e poll. Il live paper registra per primo `gap_acknowledged` all'indice 388, il replay registra prima `gap`. Succede anche con 51 ore mancanti. La regola di R28 ora rende il mismatch INVALID per edge. In senso opposto, pubblicare un `prev_close` sintetico non disponibile all'istante di decisione, con un `published_at` posteriore nel JSON del feed, fa registrare quel numero e restituisce ancora `match=true`. Il replay può distinguere un registro incoerente da uno coerente, mentre il formato persistito non prova il momento in cui il close era disponibile. |
| R42 | LOW | I due checkpoint non garantiscono un quadro coerente durante un poll | Registro, eventi, storia e fedeltà sono letti in query separate e spesso con connessioni diverse. Una poll tra le letture può cambiare il perimetro del confronto. L'acknowledge senza la poll successiva è uno stato intermedio osservato con `match=false`. Serve una vista coerente per esiti irrevocabili. Il gap finale resta non riconoscibile finché non appare un evento successivo. |
| R43 | LOW | Il contenuto della prova con `--as-of` può includere dati futuri | Prima del lunedì di trigger `until=None` e la prova edge sul replay completo espone 17 trade fino al 30 settembre 2026, sebbene l'istante richiesto sia 13 luglio 2025. Week12 applica il clock alla griglia dei giorni e legge comunque tutti i record per alcuni controlli. Il risultato va definito per l'istante richiesto anche quando il checkpoint non è dovuto. |

## Chiusura dei rilievi precedenti

| ID | Stato | Prova e osservazione |
|---|---|---|
| R27 | **Chiuso** per il calcolo di puntualità | Caso del 5 gennaio con commit barra +5 min: 8/8 dopo il riconoscimento. Gap 5–7 gennaio: 6/6 con tre esclusi. PAUSED_GAP: zero esclusi. Prima barra mancante: il runner rifiuta l'acknowledge e il giorno è non spiegato. Coda senza nuove barre: STALE, 4/5. La discordanza di fedeltà dopo ripresa è R41. |
| R28 | **Parziale** | Digest reale e feature pin corretti e provati. Divergenza nelle feature dà INVALID nei due report. Lo stop scatta quando la violazione è visibile nel loro orizzonte. La fonte e la registrazione sono verifiche incomplete, il drawdown successivo al trigger è ignorato da edge e la chiusura effettiva della barra non è attestata. |
| R29 | **Parziale** | Stesso lunedì incluso e stesse metriche con ledger già completo. La chiamata può essere prematura e la prova può scrivere SQLite. L'uscita dopo la scadenza è incoerente con la specifica. |
| R30 | **Parziale** | Cancellare solo il file e copiare dopo la decisione conserva l'uscita 2. I casi copia precedente, cancellazione della riga, race e guasto DB/JSON sono riprodotti o mostrati dall'interleaving in R40. |
| R31 | **Chiuso** per la registrazione distinta di ogni esito | Il CLI registra `p1-week12-OPERATIONAL_PASS.json` e successivamente `p1-week12-FAILED.json`. Ripetere PASS lascia la prima prova. Una riga già presente continua a bloccare la scrittura dell'esito anche se il file è perso. |
| R32 | **Chiuso** | Conteggio statico 10/11 per i due moduli, allineato alla riga del PRD. |
| R33 | **Chiuso** | `deviations[0].requires` indica esattamente dove registrare l'approvazione. La nota approvata il 15 novembre è nello stesso oggetto. Il titolo della guida può riflettere che l'approvazione è già registrata. |
| R34 | **Chiuso** | JSON e guida dichiarano che con lordo realizzato ≤ 0 la quota è indefinita e il criterio fallisce. Riprodotto con lordo −4,9075 nel replay al trigger. |

## Trigger e corrispondenza codice–specifica

- `first_monday_at_or_after` normalizza in UTC nel percorso effettivo, perché `analytics.trades` fornisce timestamp UTC. Lunedì alle 00:00 resta lo stesso. Martedì punta al prossimo lunedì. Lunedì dopo le 00:00 punta al successivo. Il test su registro minimo produce il medesimo comportamento con nove chiusure nello stesso giorno.
- `trigger` ordina le chiusure per timestamp e prende l'ottava. Il replay iniziato di mercoledì e l'ottavo trade di martedì riproducono il 14 luglio. Il minimo è 8, quindi eventuali altre chiusure fra l'ottava e la mezzanotte di lunedì entrano legittimamente nelle cifre.
- `_until` include tutti i record con timestamp ≤ confine. Nella prova con ottava SELL esattamente lunedì 00:00, il trigger resta allo stesso istante e il taglio mantiene la SELL e il saldo delle 00:00. Esclude l'evento delle 01:00. `edge_figures` usa gli stessi record per chiusi, costi, drawdown e posizione aperta. Sul replay sono valorizzati alla barra delle 00:00 di lunedì.
- `run_engine(until=…)` seleziona le righe events con ≤, usa l'intervallo fino a ultimo evento + 1 ora e non chiama `finish`. Il buy and hold usa constant LONG, senza le feature giornaliere, e arriva allo stesso ultimo evento se il confine è già registrato. Il ricalcolo indipendente conferma il benchmark.
- La fedeltà integrale è un controllo sensato sulla continuazione della sessione, ma può cambiare a posteriori l'esito che certifica il lunedì. Il rapporto deve rendere distinguibili fedeltà al cutoff e incidenti successivi. Anche per il cutoff occorre leggere una vista SQLite coerente. Valutare un gap riconosciuto come errore permanente confonde la continuità tecnica con la qualità della fonte.
- I quattro confronti edge coincidono con il testo: netto realizzato strettamente positivo, drawdown ≥ −20%, quota costi definita e ≤ 50%, equity ≥ benchmark −10 punti. Il drawdown di stop è strettamente sotto −20%, e viene valutato dopo INVALID. Week12 mette a confronto puntualità ≥ 95%, zero giorni senza decisione per colpa del runner, fedeltà positiva e drawdown ≥ −20%.
- I codici provati sono 0 per NOT_DUE, OPERATIONAL_PASS e CONFIRMED, 1 per NOT_CONFIRMED, OPERATIONAL_FAIL e FAILED, 2 per un edge già registrato senza race. Il ramo finale emette 1 anche per INVALID e INCONCLUSIVE. Una spec illeggibile o con digest incompatibile genera un'eccezione prima del report. Se cambiano `normalize_config`, `config_digest` o il relativo schema, la divergenza è rumorosa, con traceback e valore atteso e osservato. Il coordinatore deve conservare la versione della normalizzazione insieme al runner.
- La riga di puntualità legge l'orologio di commit effettivo ed esclude entrambi i giorni futuri e i giorni che hanno ancora le due ore di tolleranza. Un gap è escluso su intervallo [mancante, ripresa). Un timestamp di commit precedente all'evento risulterebbe puntuale. Il confronto usa solo il limite superiore, rilevante per replay/importazioni e orologi fuori sincrono.

## Controlli software e responsabilità operative

| Regola | Cosa succede oggi | Accettabilità per la preregistrazione |
|---|---|---|
| Stop per drawdown | I report calcolano e registrano un esito quando invocati. Week12 vede l'intera sessione, edge può fermarsi al trigger. Il runner non sospende automaticamente la sessione | La chiusura può essere operativa se la guida stabilisce chi la esegue e quando. Il segnale di stop deve essere coerente tra i report |
| Stop per candidata diversa / barra non chiusa | I confronti con la spec sono parziali. Il replay verifica la coerenza con dati propri. Il feed standard usa open corrente e close precedente, ma il report non ha evidenza persistita del tempo di disponibilità | Vincolare sessione, spec e fonte. Dichiarare il limite della fedeltà e prevedere un controllo dei timestamp sorgente per lo stop |
| Al massimo due ripartenze, con approvazione | I contatori sono locali alla sessione. Il CLI non ha registro della sequenza, delle approvazioni o della sessione attiva | Accettabile come procedura esplicita e verificabile se il coordinatore registra gli ID, quale sessione è valida e perché ogni ripartenza è ammessa |
| 78 settimane dalla prima sessione | Solo testo. Il CLI non legge la data iniziale globale | Accettabile operativamente solo se il termine e il relativo esito sono fissati e annotati nel registro prima dell'avvio |
| Divieto di attendere un risultato migliore | Il report tronca le cifre al giorno giusto solo se il ledger a quel confine è completo. La data di invocazione e il ritardo sono stampati | Accettabile dopo controllo di prontezza e scadenza. L'istante di trigger non deve dipendere da quando si esegue il comando |
| Esecuzione del lunedì | Il comando parte anche in un altro giorno. Il runbook pianifica lunedì 00:00, la riga del report attesta eventuale ritardo | La pianificazione è operativa. Il report deve attendere il commit della barra delle 00:00 o un gap esplicitamente riconosciuto prima di registrare un esito |
| Archiviazione una sola volta | JSON e decisione locale si separano in caso di errore. Un backup precedente può ripetere l'esito | Il registro ufficiale deve identificare la sessione approvata. Il DB deve imporre un solo esito edge e consentire di esportarlo di nuovo |

## Soglia degli otto trade e rilievi residui

Fissare la valutazione alla prima finestra con otto chiusure è coerente con l'intento di eliminare la scelta a posteriori della data. Nel replay il negativo realizzato si vede sia a otto trade (−10,75) sia a diciassette (−23,93). L'esclusione del plusvalore aperto è deliberata e spiega parte della differenza rispetto a E05. Il 14 luglio il risultato fallisce anche il confronto con buy and hold e il criterio costi. Il risultato storico fornisce un collaudo del protocollo. Una singola finestra già nota con otto chiusure non stima la probabilità di successo del candidato su dati nuovi. Otto trade concentrano molto peso sul singolo round trip e sul regime del periodo. Il criterio è severo verso trend ancora aperti e potenzialmente permissivo per pochi trade fortunati. È coerente come soglia decisionale operativa dichiarata, con l'incertezza già prevista in specifica. Questa review non propone di modificarla alla luce del replay.

- **R17 LOW.** Il warmup rimane quello memorizzato alla prima lettura e il replay lo condivide. Rilevanza invariata.
- **R18 LOW.** La coerenza interna acquista maggiore rilevanza perché ora un mismatch blocca edge e il testo attribuisce alla fedeltà una prova temporale che non può fornire. R41 concretizza entrambi i casi.
- **R19 INFO.** La base equity dei balance è quella dichiarata e riconciliata. La differenza con un'altra convenzione rimane non decisiva per le prove riprodotte.
- **R20 INFO.** Le politiche di retry della fonte sono invariate. R38 identifica chiamate al clock superflue anche nei report.
- **R26 INFO.** Il comportamento di `acknowledge_gap` con fonte indisponibile resta invariato. Il nuovo uso del replay come stop aumenta il peso del flusso di ripresa, come in R41.
- **R13 e R14 INFO.** Restano nel perimetro S08/S07. La modalità paper osservata e la deviazione DEV-1 non li rendono più rilevanti.

## Verdetti e modifiche richieste

| Oggetto | Verdetto |
|---|---|
| `session_report.py` / PRE-7 | **Da correggere.** Metrica e replay di riferimento riprodotti. Bloccanti l'ammissione in produzione, la prontezza del trigger, l'irreversibilità e gli stop incoerenti |
| `session_fidelity.py` | **Taglio temporale riprodotto e corretto sul replay senza gap.** Da correggere prima di usarlo come stop in produzione per i gap riconosciuti e per la lettura operativa con effetti collaterali. La provenienza temporale richiede evidenza propria |
| Specifica E05-P1 | **Non preregistrabile così.** Il runner automatico PRE-3 resta di competenza operatore come da specifica |

1. Vincolare esecuzioni in produzione a specifica preregistrata e identificata da digest e a una sessione autorizzata. Ricalcolare il digest effettivo. Verificare feature, mode, coppia, identità fonte e partenza dopo la registrazione. Rendere verificabile l'esito operativo e quale sessione è attiva prima di pubblicare un esito edge favorevole.
2. Impedire la scrittura terminale finché il runner non abbia raggiunto il confine incluso. Definire cosa vale al cutoff se l'evento è in gap. Applicare il termine di 52 settimane alla data di trigger definita, anche quando il report parte tardi, e formalizzare il conflitto fra ottava chiusura e lunedì a cavallo della scadenza.
3. Fare leggere i report e il replay da una vista SQLite coerente, in sola lettura e senza aprire la fonte per il clock. Verificare hash di database, WAL e SHM con sessione sana, attiva e con digest divergente. Definire l'orizzonte dei payload di prova prima del trigger.
4. Separare le cifre del vantaggio al trigger dal controllo di drawdown di stop all'istante del rapporto. Allineare JSON e guida su precedenza, causalità della barra e limiti della fedeltà.
5. Imporre una sola decisione edge tramite vincolo atomico per checkpoint. Dare exit 2 anche al concorrente tardivo. Consentire l'esportazione del JSON a partire dalla decisione già persistita, anche dopo un crash. Usare il registro operativo per collegare backup, copie, ripartenze e scadenza globale alla sessione ufficiale.
6. Riprodurre la sequenza del gap del runner nel controllo di fedeltà, anche quando è tra acknowledge e ripresa. Distinguere la coerenza dei valori dalla prova temporale delle barre. Prima della preregistrazione, rieseguire con dipendenze complete il replay canonico e la suite, includendo prove di regressione sui rilievi bloccanti.
