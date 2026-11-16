# Revisione indipendente E05-P1 R3 · 16 novembre 2026

**La specifica richiede ulteriori correzioni prima della preregistrazione.** I due scenari HIGH di R2 sono corretti nel percorso CLI ordinario. La scadenza, il drawdown successivo al trigger, l'unicità della decisione nel database e la lettura concorrente superano le prove descritte sotto. Rimangono quattro rilievi MEDIUM, R44–R47, e tre LOW, R48–R50. Il problema operativo più diretto è un INVALID irrevocabile se il report parte fra il riconoscimento del gap e la poll che riprende.

Revisore OpenAI Codex, modello **gpt-6-astra**, versione interna/build non esposta. La revisione precedente è stata svolta da Codex, dichiarato famiglia GPT-6 con variante non esposta. Questa revisione mantiene l'indipendenza rispetto all'autore Claude Opus 5.5, mentre la continuità con la precedente famiglia Codex è dichiarata.

Base esatta **7bf5b496d2655ccc55b71791b38ef70a4783bd1f**. Prompt R3 letto da main a **03ff751**. Perimetro del diff **524a61d..7bf5b49**. Clone locale pulito in `var/review-indep/r3/`, detached alla base, con sparse checkout che esclude `data/forward/` prima del checkout. File di codice, configurazione e backlog versionati conservati. Report creato nella root del clone e copiato nella root del repository di lavoro, senza commit.

Ho letto il report R2, la specifica E05-P1, la guida, la riga del PRD, i tre moduli della revisione e i test dell'autore. Le prove usano snapshot di ricerca o fixture sintetiche. Le chiamate di rete dei processi di prova sono bloccate, il client pubblico è finto dove necessario. Le verifiche in produzione usano copie sintetiche con orologio simulato.

## Ambiente e riproducibilità

Venv nuovo Python 3.12.14 con accesso ai pacchetti già installati nel runtime. Il tentativo offline `pip install --no-index --no-build-isolation -e '.[dashboard,dev]'` si ferma a `requests>=2.31`. `python -m pytest -q -p no:cacheprovider` restituisce `No module named pytest`. **Il risultato atteso di 615 passed rimane non verificato da questo revisore.**

Il percorso canonico `paper session-create --snapshot BTCUSDT-1h-raw-v1` fallisce sulla lettura Parquet per assenza di PyArrow/Fastparquet. Ho ripetuto la creazione e il run con lo stesso adattatore locale di lettura Parquet usato in R2, che sostituisce `pandas.read_parquet` nel solo processo di prova. La verifica dei checksum e il caricamento del catalogo rimangono quelli del repository. Il lettore supporta le pagine numeriche effettivamente incontrate, con Snappy/Zstandard. Questo replay fornisce evidenza dei calcoli, con il limite esplicito sul percorso canonico.

Le prove di concorrenza avviano processi Python reali. I guasti sono inseriti in wrapper di prova, mediante barriere e interruzione forzata del processo. Il codice versionato non è modificato. Sistema operativo verificato Linux, UID 1000. **Windows non è disponibile e resta non verificato.**

Nei comandi seguenti `$P` è `var/review-indep/venv/bin/python`, con `PYTHONPATH=src:var/review-indep`, dalla root del clone R3. Script e log sono nel suo `var/review-indep/`. Le fixture vengono generate dagli script.

| Prova e comando | Atteso | Osservato | Esito |
|---|---|---|---|
| Installazione offline e `$P -m pytest -q -p no:cacheprovider` | Ambiente completo, 615 passed | Dipendenze mancanti, pytest assente | Bloccata |
| `$P -m crypto_decision_lab.paper session-create …` | Percorso canonico con PyArrow | Primo chunk Parquet illeggibile per engine assente | Bloccata |
| `$P var/review-indep/offline_cli.py crypto_decision_lab.paper session-create …`, poi `session-run` | COMPLETED, candidata fissata | COMPLETED, equity 524,912532, ultimo evento 2026-09-30 23:00 UTC | PASS con adattatore dichiarato |
| `$P -m crypto_decision_lab.session_fidelity --session-dir <e05conf>` | match true, 15.312 eventi e 61.319 record | Tutti i valori attesi, exit 0 | PASS |
| `session_report edge --as-of "2025-07-13 23:00"` | NOT_DUE, otto chiusi, valori fermati alle 23:00 | Exit 0, +0,9770% equity, +13,2333% benchmark | PASS |
| Stesso comando, `2025-07-14 00:00` e `2026-10-01 00:00` | NOT_CONFIRMED, cifre edge uguali | Exit 1 in entrambi, ritardi 0 e 444 giorni, fedeltà al cutoff e alla data true | PASS |
| `session_report week12 --as-of "2026-10-01 00:00"` | OPERATIONAL_FAIL per puntualità replay | Exit 1, puntualità 0%, fedeltà true | PASS |
| `$P var/review-indep/recalc.py` | Ricalcolo indipendente dei saldi e delle metriche | Coincidenza entro 1e−8, incluse domenica 23:00, lunedì e intera finestra | PASS |
| `$P var/review-indep/adversarial.py` · ammissione | Bloccare simbolo, host, fonte, modo, data e candidata discordanti | Casi ordinari bloccati. Rimangono NaT, identità incoerenti e default API | Parziale, R49 |
| Stesso script · prontezza e scadenza | Attesa senza barra, termine applicato al lunedì | NOT_DUE fino al commit. Scadenza lunedì esatto inclusiva, differenza di 1 ns discriminata | PASS nel percorso ordinario |
| Stesso script · gap | Riconoscimento seguito da ripresa fedele | Fedeltà true per un'ora, 51 ore, posizione long, ordine pendente e due gap intervallati da una barra. Stato dopo ack e prima di poll non fedele | Parziale, R44 |
| Stesso script · crollo dopo trigger | Stop FAILED, cifre edge mantenute al trigger | Entrambi FAILED, stop −45,9607%, drawdown al cutoff −1,8070% | PASS |
| Stesso script · alterazioni ordine, fill, saldo, open, prev_close | Tutte rilevate | match false, prime differenze agli indici 3, 4, 5, 98, 98 | PASS |
| `$P var/review-indep/concurrency.py` · due processi simultanei | Una riga edge, uscite 0 e 2 | Una riga, un CONFIRMED exit 0, un rifiuto exit 2 | PASS |
| Stesso script · crash dopo INSERT, prima di aprire JSON | Esportazione dal DB al retry | Processo interrotto con 73. Retry exit 2, JSON completo ricostruito | PASS |
| Stesso script · crash durante JSON e doppia esportazione | JSON recuperabile, exit 2 al secondo processo | JSON troncato resta tale. Due esportazioni simultanee causano FileExistsError in una | FAIL, R46 |
| Stesso script · cancellazione DB, copia precedente, week12 | Limiti espliciti e recupero artefatti | Cancellare riga+JSON o usare copia precedente riapre la valutazione. Il JSON week12 eliminato non è ricreato | Limite operativo dichiarato e R46 |
| `$P var/review-indep/snapshot_probes.py` · sola lettura | Tabelle identiche, nessuna fonte aperta | Tabelle e byte identici nei casi con sidecar già presenti, anche con digest motore discordante. Apertura PaperSession e clock fonte interdette senza errori | PASS nel caso provato |
| Stesso script · runner durante SELECT | Un'unica versione coerente | Il runner passa da 2.400 a 2.401 eventi durante la lettura. La lettura in corso conserva 2.400 eventi e ledger/cursor precedenti. La successiva ne vede 2.401, entrambi fedeli | PASS |
| Stesso script · ack prima del primo trigger | Stato regolare senza decisione negativa terminale | match false, edge INVALID exit 1 persistito. Seconda chiamata exit 2. Dopo poll il registro torna fedele | FAIL, R44 |
| `$P var/review-indep/final_probes.py` · DB WAL senza sidecar | Chiarire limiti della sola lettura | Cartella scrivibile crea WAL/SHM. Cartella 0555 e DB 0444 fallisce con OperationalError. DB principale invariato | R48 |
| Stesso script · file spec sostituito durante il calcolo | Hash della specifica realmente usata | CONFIRMED calcolato con spec A, pubblicato con SHA di spec B che avrebbe dato INVALID | FAIL, R45 |
| Stesso script · taglio dentro gap, sessione vuota | Replay valido al confine e senza eventi | Tagli prima/dentro/dopo il gap fedeli quando esiste ripresa. Zero eventi dà match true e NOT_DUE nei due rapporti | PASS con riserve R44/R50 |
| `git diff --check 524a61d..7bf5b49`, `git diff --exit-code` | Diff ben formato, codice di base intatto | Exit 0, nessuna modifica tracciata prima del report | PASS |

## Riproduzione storica e riconciliazione

La candidata conserva digest `7775ccd417e3354c981248cf1469f6cab447844638b88301756fb59c099c5536`, feature 28/100, capitale 500, esposizione 0,5, fee 0,001, slippage 0,0005 e orizzonte 1.000.000 ore. Snapshot ammesso `BTCUSDT-1h-raw-v1`, intervallo 2025-01-01 incluso → 2026-10-01 escluso.

| Misura | 13 luglio 2025, 23:00 UTC | 14 luglio 2025, 00:00 UTC | 1 ottobre 2026, rapporto tardivo |
|---|---:|---:|---:|
| Esito | NOT_DUE | NOT_CONFIRMED | NOT_CONFIRMED |
| Trade chiusi conteggiati | 8 | 8 | 8 |
| Netto realizzato | −10,74778976 | −10,74778976 | −10,74778976 |
| Netto realizzato % | −2,14955795% | −2,14955795% | −2,14955795% |
| Equity netta % | +0,97702079% | +1,22587861% | +1,22587861% |
| Buy and hold % | +13,23332755% | +13,53564386% | +13,53564386% |
| Drawdown del vantaggio | −7,28433523% | −7,28433523% | −7,28433523% |
| Drawdown di stop alla data | −7,28433523% | −7,28433523% | −9,32943929% |
| Plusvalore aperto dal 10 luglio | +15,63289371 | +16,87718283 | +16,87718283 |
| Costi / lordo sui chiusi | 5,84030956 / −4,90748020 | Uguale | Uguale |
| Quota costi sul lordo | Indefinita | Indefinita | Indefinita |
| Ritardo rispetto al lunedì | −0,04 giorni | 0 giorni | 444 giorni |
| Fedeltà al cutoff / alla data | true / true | true / true | true / true |

Ottavo trade chiuso l'8 luglio. Lunedi di trigger il 14 luglio. Al trigger falliscono netto realizzato, quota costi e confronto con buy and hold, mentre passa il drawdown. I 17 trade chiusi sull'intero replay realizzano −23,92643786, con +48,83897003 non realizzati sulla posizione finale ed equity +4,98250643%.

`recalc.py` legge SQLite in sola lettura e ricostruisce cassa, quota, entrate, uscite, costi e picchi di equity direttamente da market/execution/balance. Ogni saldo viene confrontato con la ricostruzione. Per il benchmark investe 250 al primo open con le due maggiorazioni previste per fee e slippage, mantiene la quota e la valorizza all'ultimo open selezionato. Il ricalcolo non importa `session_report` o `studio_analytics`.

## Nuovi rilievi

**R44 · MEDIUM · Un riconoscimento di gap regolare può consumare definitivamente la decisione edge.**

Riferimenti `session_fidelity.py:72`, `session_snapshot.py:44`, `session_report.py:327`. Il nuovo replay riconosce un gap solo quando incontra l'evento successivo. Il runner invece persiste già `gap_acknowledged`, ed eventualmente `order_cancelled`, nel comando di riconoscimento. Nel periodo prima della poll successiva esiste quindi uno stato persistito legittimo che il replay non riproduce.

Riproduzione minima eseguita con lab-feed sintetico e candidata daily_trend con feature accelerate 2/3. Togliere la barra 2025-01-05 00:00, eseguire `poll`, `acknowledge_gap`, chiudere la connessione senza riprendere la poll. Su copia con metadati pubblici e specifica preregistrata di prova, invocare `edge` in produzione. La prima differenza è il record 388, un ack presente solo nel registro. Il CLI restituisce INVALID con exit 1 e lo registra come unica decisione. La seconda chiamata dà 2. Dopo aver fatto riprendere il runner originale, il controllo torna true con 239 eventi/963 record. È possibile incorrere nel difetto prima di otto trade.

Il caso con ordine pendente produce lo stesso problema, includendo la cancellazione. Con `finish` immediatamente dopo l'ack, il replay resta falso anche in stato COMPLETED. Al trigger coperto da gap ho osservato inoltre una differenza fra simulazione e produzione sulla stessa fase operativa. Prima dell'ack entrambi attendono. Dopo ack e prima di ripresa, la prova tagliata elimina l'ack e resta NOT_DUE, mentre produzione può decidere CONFIRMED con `fidelity_to_date=false`. Dopo la ripresa entrambi sono coerenti. Serve rappresentare e rigiocare esplicitamente la fase di ack, evitando che una transizione lecita diventi INVALID irrevocabile.

**R45 · MEDIUM · Lo SHA della specifica può descrivere byte diversi da quelli usati nel calcolo.**

Riferimenti `session_report.py:86,90,399`. `load_spec` legge il file prima di invocare il rapporto. `spec_sha256` lo rilegge dopo il calcolo. Il registro ufficiale si affida proprio a questo hash.

Prova con sostituzione controllata nel wrapper durante `edge`. Spec A usa feature 2/3, spec B modifica SMA a 4. Il rapporto è CONFIRMED secondo A, ma stampa e registra lo SHA di B. Ricalcolare l'ammissione con B restituisce INVALID per feature discordanti. La verifica documentale del digest non può identificare correttamente la specifica usata in questo caso. Leggere una sola sequenza di byte, normalizzare gli a-capo, ricavarne sia JSON sia hash e conservarli nel rapporto. L'operatore continua a confrontare lo SHA con il registro ufficiale.

**R46 · MEDIUM · La decisione è atomica nel DB, ma l'esportazione dei report resta incompleta in caso di guasto.**

Riferimenti `session_report.py:161,176,399`. Il vincolo univoco e la transazione risolvono la doppia decisione. Ho ottenuto una riga e uscite 0/2 con due processi reali fermati su una barriera prima di `record_decision`. Il crash fra commit e apertura del JSON è recuperato.

Restano tre casi riprodotti.

- Interruzione dopo apertura del file con `"x"` e scrittura del prefisso `{"checkpoint":`, prima della chiusura. Il retry vede il file esistente, restituisce 2 e lascia il JSON illeggibile. La decisione corretta è nel DB.
- Due processi tentano contemporaneamente di riesportare un edge già deciso il cui file manca. Dopo una barriera prima dell'apertura esclusiva, uno dà 2 e l'altro termina con FileExistsError, exit 1. L'unicità nel DB resta preservata.
- Dopo OPERATIONAL_PASS week12, eliminare il relativo JSON e ripetere il rapporto. La riga esistente impedisce l'inserimento e il file resta assente, sebbene PRE-7 dica genericamente che un file mancante viene ricreato.

Il recupero dovrebbe derivare dal report già persistito, con pubblicazione atomica del file completo e gestione idempotente dei concorrenti. Va recuperato anche un file esistente incompleto o difforme. Per week12 va implementata la riesportazione per esito, oppure il contratto va ristretto esplicitamente all'edge e va indicata la procedura di recupero.

**R47 · MEDIUM · Specifica e guida continuano ad attribuire alla fedeltà una verifica temporale che il controllo non effettua.**

Riferimenti `criteria.stop_rules_any_time[2]`, `threats`, guida `docs/E05-P1-paper-forward.md` nella sezione regole di stop. La nuova sezione threats dichiara correttamente che il controllo non conosce quando un close fosse disponibile. Il modulo spiega lo stesso limite. La regola di stop e la guida affermano ancora che una feature calcolata da una barra non chiusa venga rilevata dal replay.

Il codice legge i soli valori memorizzati di history/events e confronta i registri. La limitazione già dimostrata in R2 resta intrinseca a questi input. Le prove R3 dimostrano la rilevazione di alterazioni incoerenti di cinque tipi, ma nessuna evidenza di disponibilità temporale viene introdotta. La formulazione nelle minacce è onesta, mentre la regola operativa la contraddice. È sufficiente allineare i testi: lo stop automatico deriva da mismatch del registro con il replay, la causalità dell'ingresso è una proprietà richiesta al feed/runner e ai relativi test. Qualunque violazione temporale accertata esternamente può avere una procedura operatore esplicita. Non occorre inventare una prova che i dati persistiti non contengono.

**R48 · LOW · La sola lettura delle tabelle non garantisce assenza di file ausiliari o funzionamento in tutte le cartelle read-only.**

Riferimento `session_snapshot.py:63`. Su Linux UID 1000, DB impostato WAL e connessione scrivibile finale chiusa dopo checkpoint, WAL e SHM sono assenti. La lettura `mode=ro` riesce in cartella scrivibile ma crea `session.sqlite-wal` e `session.sqlite-shm`. Lo stesso DB in cartella 0555 e file 0444 fallisce con `OperationalError: attempt to write a readonly database`. Con sidecar già presenti, anche in cartella protetta, i casi provati passano. Il DB principale e le tabelle rimangono invariati.

Il comportamento non riproduce la mutazione di stato di R2, che è risolta. Richiede però un contratto preciso sui file ausiliari. Per copie archiviate in sola lettura serve una modalità compatibile con il loro stato WAL. Non usare un'opzione che ignori il WAL su sessioni attive. Compatibilità Windows non verificata.

**R49 · LOW · L'ammissione ha casi residui su default API e metadati incoerenti.**

Riferimento `session_report.py:109`. La CLI memorizza esplicitamente history_hours e supera il caso ordinario. Una sessione legittima creata via `PaperSession.create(..., source={"kind":"public_klines","symbol":"BTCUSDT"})` usa il default 2600 e lo registra nell'identità, ma non nell'oggetto source. Il report legge 0 per il campo omesso e produce `warmup of None hours, fewer than 2400`. Prova tramite API effettiva, con HTTP finto per l'orologio.

Sono inoltre accettati `registered="NaT"`, `identity.kind="snapshot"` con source.kind pubblico e una history_hours nell'identità di 600 con source.history_hours 2600. Si tratta di metadati incoerenti o data non finita. Il registro operatore riduce il rischio di ammetterli, mentre il supporto dei default documentati evita falsi INVALID per chiamanti legittimi. Verificare data UTC finita e coerenza dei valori effettivi, applicando i default del costruttore.

**R50 · LOW · Il taglio temporale conserva alcuni metadati futuri e rappresenta un replay per timestamp di mercato.**

Riferimento `session_snapshot.py:44`. Ledger, eventi e storia sono tagliati sul timestamp di mercato. Status_log è tagliato su `at`. Il campo cursor e lo stato restano quelli letti dall'intera sessione. Nella prova con taglio 21 gennaio, cursor rimane 10 aprile 23:00 e lo stato STALE. Una riga status_log inclusa può inoltre contenere un clock della fonte successivo al taglio. I report correnti non espongono quel cursor e i loro numeri storici richiesti sono corretti.

Una barra committata in ritardo dopo l'istante richiesto resta negli eventi se il suo timestamp è precedente, mentre la corrispondente riga di commit scompare dal log. Il risultato è un replay retrospettivo per tempo di mercato, con misura della puntualità limitata al wall clock. È una semantica utilizzabile, ma la docstring «as it was at instant» e «every piece cut there» è troppo ampia. Ricostruire o qualificare stato/cursor e documentare esplicitamente questa differenza. Le righe di ack eliminate hanno anche l'effetto operativo trattato in R44.

## Chiusura di R35–R43

| Rilievo | Stato | Motivazione |
|---|---|---|
| R35 | **Parziale** | Simbolo, host, modo, kind, preregistrazione, avvio, configurazione effettiva, digest memorizzato e feature coprono i casi ordinari di R2. La flessibilità di `--spec` è affidata al registro e dichiarata. R45 rende necessario fissare lo SHA agli stessi byte usati. R49 descrive i casi residui |
| R36 | **Chiuso nel percorso con barra, parziale per il gap** | Domenica 23:00 → lunedì senza evento dà NOT_DUE anche se COMPLETED. Dopo la barra sintetica delle 00:00 il risultato diventa NOT_CONFIRMED. La variante con ack ha la differenza fra prova e produzione di R44 |
| R37 | **Chiuso** | Trigger confrontato con start + 52 settimane. Ottavo trade prima del termine e lunedì successivo → INCONCLUSIVE al termine. Lunedì esattamente uguale ammesso, un nanosecondo prima della scadenza distingue correttamente i casi. Attesa prima della scadenza coerente |
| R38 | **Chiuso per stato e rete, parziale per il contratto sui file** | Nessun PaperSession, fonte o orologio dell'exchange durante la lettura. Tabelle invariate anche con digest del motore discordante. Rimane R48 sui sidecar e sulle cartelle protette |
| R39 | **Chiuso** | Crollo successivo al trigger causa FAILED in entrambi i rapporti. Cifre edge conservate al cutoff. Prova su percorso reale del motore, senza falsificare un saldo |
| R40 | **Parziale** | Indice univoco parziale e transazione risolvono due decisioni concorrenti, incluse uscite 0/2. Crash prima dell'apertura JSON recuperato. R46 riguarda esportazione concorrente, file troncato e week12 |
| R41 | **Parziale** | Tutti i gap ripresi provati diventano fedeli e le alterazioni restano rilevate. Ack senza ripresa e ack seguito da finish rimangono problematici. Limitazione temporale dichiarata ma contraddetta in R47 |
| R42 | **Chiuso** | Un vero runner commette fra SELECT metadati e SELECT stato. La transazione di lettura conserva una versione interamente precedente. La nuova lettura vede interamente quella successiva. Entrambe sono fedeli |
| R43 | **Chiuso per i numeri dei report richiesti, parziale per il contratto generale** | Domenica 23:00 espone otto trade e l'ultimo evento corretto, week12 usa eventi e status_log tagliati. R50 riguarda metadati e interpretazione del wall clock. La griglia giorni resta limitata alle scadenze a +2 ore maturate |

## Taglio e fedeltà, caso per caso

`SessionSnapshot.until` mantiene i record con timestamp ≤ cutoff. Calcola l'indice dell'ultimo market event e rimuove dalla coda ack/cancellazioni finché sono gli ultimi record. Sui gap completati questa scelta rende coerenti i tagli prima, durante e dopo il gap, anche quando il pending viene cancellato. Il replay riproduce il riconoscimento prima della prima barra successiva. Le prove sui gap di una e 51 ore, su due gap separati da una barra e con posizione long superano il controllo.

Ci sono due limiti. Il semplice `check` sulla sessione attiva non applica questo taglio, quindi osserva l'ack in coda e lo giudica falso. Viceversa il taglio a un istante successivo all'ack ma precedente alla poll lo cancella, perdendo un'informazione già presente. Se dopo l'ack il runner viene terminato, il record end interrompe la rimozione dalla coda e il replay non sa riprodurre quella sequenza. R44 richiede un modello coerente della transizione, evitando di nascondere indiscriminatamente una cancellazione solo perché in coda.

Il run usa i record della stessa istantanea e termina se questa contiene end. Buy and hold passa `with_features=False`, conserva capitale e limiti della candidata e usa gli stessi eventi, con valutazione allo stesso ultimo open. Il replay storico e il ricalcolo indipendente confermano l'allineamento. Il valore iniziale delle feature e le barre di warmup non producono trade prima di start.

## Criteri, precedenze e codici di uscita

| Regola | Codice e risultato della verifica |
|---|---|
| Netto realizzato > 0 | Somma dei soli CLOSED divisa per capitale. Sul replay −2,1496%, criterio fallito |
| Drawdown del vantaggio ≥ −20% | Equity fino al trigger incluso. Sul replay −7,2843%, criterio superato |
| Costi ≤ 50% del lordo realizzato | Fee e slippage dei CLOSED. Lordo ≤ 0 produce null e fallimento. Riprodotto |
| Equity ≥ buy and hold −10 punti | Confronto sullo stesso periodo e stessi costi. Al trigger soglia +3,5356%, osservato +1,2259% |
| Stop sul drawdown | Tutta l'istantanea alla data del rapporto. Stop strettamente sotto −20%. INVALID ha precedenza sul drawdown se entrambe le condizioni si presentano |
| Fedeltà | Al cutoff può dare INVALID edge. Quella alla data è informativa. Week12 usa la fedeltà della propria istantanea |
| Scadenza | Il lunedì deve essere ≤ start + 52 settimane. I branch di scadenza sono dopo gli stop e prima della valutazione favorevole |
| Puntualità | Prima registrazione di last event entro due ore, ≥95% dei giorni conteggiati. Gap riconosciuti esclusi. Giorni senza evento e senza giustificazione fanno fallire il criterio separato |
| NOT_DUE | Exit 0 e nessuna decisione registrata |
| OPERATIONAL_PASS, CONFIRMED | Exit 0. Provati tramite CLI sulle fixture |
| NOT_CONFIRMED, OPERATIONAL_FAIL, INVALID | Exit 1. Provati tramite CLI sul replay e sul caso ack |
| FAILED | Exit 1 nel dispatcher. Esito riprodotto con crollo effettivo nei due report |
| INCONCLUSIVE | Esito provato ai bordi e sul caso isolato con meno di otto trade oltre 52 settimane. Exit 1 verificato nel dispatcher isolato |
| Edge già deciso | Exit 2 sia sequenziale sia nel concorrente che perde l'inserimento. Eccezione sull'esportazione simultanea in R46 |

Dopo una decisione edge già registrata il CLI esce prima di ricalcolare gli stop. Il controllo operativo successivo passa quindi da week12 e dalla gestione del coordinatore. Questo è compatibile con il checkpoint unico, ma la guida deve evitare che «entrambi in qualsiasi momento» sia interpretato come riesame continuo dell'edge già chiuso.

Una spec incoerente rispetto al proprio blocco candidata viene rifiutata da `candidate_pin`. Le routine di normalizzazione/digest non sono cambiate nel diff R3 e resta applicabile la prova R2 del guasto esplicito quando cambiano. Lo SHA della specifica normalizza CRLF in LF. Deve essere quello dello stesso contenuto analizzato, come richiesto in R45.

## Regole affidate al coordinatore

| Regola | Software | Evidenza e compito operatore | Accettabilità |
|---|---|---|---|
| Spec e sessione ufficiale | Valida contenuti di ammissione, stampa spec_sha256 | Registro con session_id, SHA preregistrato e data. Confronto prima di accettare il risultato | Accettabile dopo R45 |
| Spec alternativa passata con `--spec` | Può ammettere una candidata o un simbolo diversi se spec e sessione concordano | Lo SHA diverso la rende estranea all'esperimento registrato | Accettabile con registro effettivo. Il tool non possiede quel registro |
| Due ripartenze approvate | Nessun contatore globale | Registrare motivo, approvazione, sessioni abbandonate e sessione corrente | Accettabile come procedura già dichiarata |
| Termine 78 settimane | Non calcolato dal tool | Fissare la data a partire dalla prima sessione e registrare INCONCLUSIVE al termine | Accettabile se data e responsabilità sono registrate prima dell'avvio |
| Copia precedente alla decisione | Può produrre un'altra decisione locale | Il registro identifica l'unica copia/sessione autorizzata | Limite dichiarato e accettabile |
| Cancellazione manuale della riga e del JSON | Riapre la valutazione, riprodotto | Decisione e relativa evidenza nel registro ufficiale impediscono di accettare un secondo esito | Accettabile come limite dell'accesso in scrittura al DB. Esplicitarlo accanto alle copie |
| Lunedì e ora del report | Calcola trigger e ritardo, permette esecuzione tardiva | Pianificazione settimanale, coordinamento con poll e controllo degli esiti | Accettabile. R44 deve evitare falsi stop durante la ripresa |
| Attendere cifre migliori | La cifra del vantaggio è fissata al trigger completo. Stop considera anche il seguito | Ritardo stampato e controllo delle esecuzioni settimanali | Corretto nel percorso ordinario. Il risultato storico è stabile |
| Sospensione effettiva dopo stop | Report emette FAILED/INVALID, runner non sospeso da questo modulo | Operatore esegue la chiusura prevista e la registra | Accettabile per una sessione paper, con responsabilità dichiarata |
| Fedeltà e disponibilità dei close | Controllo interno più test del feed causale | Gestire eventuale evidenza esterna di violazioni | Accettabile dopo l'allineamento testuale R47 |

Il registro ufficiale è una scelta procedurale esplicita nel JSON. Può restare operativo. Il confronto dello SHA e l'ID della sessione devono precedere l'accettazione di un CONFIRMED, e un risultato favorevole dell'edge non sostituisce le durate minime e le verifiche previste dagli altri gate. In particolare il minimo dichiarato per E05-P1 prima di S08 è **12 settimane**, con esito operativo da verificare. La procedura weekly_run invoca week12 ed edge, mentre il codice di edge non dipende dall'esito week12.

## Rilievi delle revisioni più vecchie

R17 LOW conserva lo stesso limite delle barre di warmup congelate. R18 LOW ora è dichiarato correttamente nelle minacce e nel modulo, ma resta la contraddizione R47 nelle regole di stop. R19 INFO conserva la convenzione equity da balance, riconciliata nel ricalcolo. R20 INFO sui retry HTTP rimane nel feed, mentre i report non lo attivano più. R26 INFO sulla gestione dell'errore del feed durante acknowledge rimane invariato, e R44 riguarda una fase successiva con feed disponibile. R13/R14 INFO su S08/S07 rimangono fuori dal percorso modificato. Le definizioni sono state verificate nei report GLM del 14 e 15 novembre e il diff R3 non modifica i relativi moduli.

## Verdetti

| Oggetto | Verdetto |
|---|---|
| session_snapshot | Lettura concorrente approvata nelle prove Linux. Taglio da precisare/correggere per ack, metadati e modalità read-only d'archivio |
| session_fidelity | Replay storico e gap dopo ripresa approvati. Ancora da correggere ack persistito prima della ripresa e ack seguito da finish |
| session_report | Correzioni principali riprodotte. Restano R44–R46 sul percorso operativo e R49 sull'ammissione API |
| Specifica E05-P1 | **Non preregistrabile così.** Correggere i MEDIUM, allineare i contratti residui e completare le prove canoniche richieste dal protocollo |

Modifiche richieste prima della preregistrazione, con perimetro concreto.

1. **R44** — Gestire come stato lecito l'ack già persistito prima della poll, incluso pending cancellato e finish. La verifica non deve consumare l'unica decisione edge su quella transizione. Rendere coerenti prova e produzione per il gap al trigger.
2. **R45** — Calcolare spec e SHA dalla stessa lettura di byte, poi usare quell'hash sia in output sia nella decisione persistita.
3. **R46** — Pubblicare il JSON completo in modo atomico, recuperarlo dal DB anche se parziale/difforme e gestire due esportatori senza eccezioni. Allineare l'esportazione week12 alla promessa del PRE-7 o documentarne precisamente il recupero.
4. **R47** — Correggere il testo della regola sulla barra non chiusa e la guida, allineandoli al limite correttamente dichiarato nelle minacce.
5. **R48–R50** — Precisare il contratto dei sidecar/read-only e del taglio storico. Correggere almeno il falso INVALID del default API e rifiutare date di preregistrazione non finite. I campi di identità vanno trattati coerentemente. Questi rilievi LOW possono essere chiusi con una combinazione esplicita di correzioni e documentazione, senza ampliare il protocollo economico.
6. Ripetere la suite canonica da ambiente con le dipendenze complete e il replay con PyArrow, includendo regressioni sui MEDIUM. Il presente report non attesta 615 passed. Il numero comunicato dall'autore rimane evidenza dell'autore.

Gli script di prova creati nel clone sono `helpers.py`, `adversarial.py`, `process_worker.py`, `concurrency.py`, `snapshot_probes.py`, `final_probes.py`, `recalc.py`, `offline_cli.py` e `parquet_offline.py`, tutti sotto `var/review-indep/`, insieme a candidate.json, log e fixture SQLite generate. Il file destinato alla revisione è **REVIEW-INDEP-2026-11-16-gpt-6-astra.md**.
