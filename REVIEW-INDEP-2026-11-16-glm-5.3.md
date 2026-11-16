# Revisione indipendente E05-P1 R4 · 16 novembre 2026

**La suite canonica (620 passed) e il replay con PyArrow sono stati eseguiti per la prima volta da un revisore indipendente dopo due giri in cui non era possibile.** Tutte le correzioni R44–R50 sono chiuse e riprodotte con prove avversariali che vanno oltre i test dell'autore. Rimane un rilievo nuovo MEDIUM (R51: il confronto di fedeltà a 12 cifre significative può fallire su un valore di feature vicino a zero, per l'amplificazione della cancellazione `x − 1` su errori di ulp nell'ordine di somma) e quattro rilievi INFO. A parere di questo revisore la specifica è preregistrabile dopo che il coordinatore ha deciso se accettare, correggere o documentare R51: nessun rilievo tocca l'esito del replay ufficiale, che ho ricalcolato dal registro in modo indipendente (tutte le cifre coincidono a 8 decimali).

Revisore: modello **GLM-5.3** (esposto da `PI_MODEL` nell'agente `pi`), versione interna/build non esposta. **Dichiarazione di indipendenza**: diverso dall'autore delle correzioni (Claude Opus 5.5); però GLM-5.3 è già stato usato su E05-P1 per la seconda review (R2, 15-11-2026). Il diff qui riveduto (`7bf5b49..88a79e9`) risponde alla review di gpt-6-astra, non alla mia: la decisione sulla validità ai fini del PRD §5 spetta al coordinatore (nota del prompt: «se ne riusi uno, dichiaralo»).

Base esatta **88a79e961b7abf5ec7785f511ac315982eac7de4**. Prompt R4 letto nel repo di lavoro a `accd67b`. Clone locale pulito in `var/review-indep/r4/` (gitignored), nessuna modifica al codice versionato (`git status` pulito a fine lavoro), nessun commit, `data/forward/` mai letto né modificato (il suo contenuto non è stato aperto; è stato al massimo elencato il nome della cartella per confermarne l'esistenza). Report scritto nella root del repo di lavoro, non committato.

## Ambiente e riproducibilità

Windows 11 (10.0.26300), venv nuovo Python 3.14.0, `pip install -e ".[dashboard,dev]"` da rete, SQLite 3.50.4, pandas 2.3.3, **PyArrow 23.0.1** (il percorso canonico Parquet è stato eseguito, senza adattatori), pytest 9.2.0. **La suite è stata eseguita per intero** (vedi sotto) e anche il replay fine a fine su snapshot reale. Linux non è disponibile: le prove di concorrenza e di sola lettura con ACL sono state eseguite solo su Windows (dichiarato dove conta). Le prove «in produzione» (senza `--as-of`) sono state eseguite solo su copie della cartella di sessione; la sessione ufficiale del probe non è mai stata scritta (verificato a fine probe).

Script di prova in `var/review-indep/r4/var/probe-r4/`: `probe_r44.py`, `probe_r45_46.py`, `probe_r48_49_50.py`, `probe_r51.py`, `probe_prod_vs_dry.py`, `recalc_e2e.py`, `candidate.json` (tutti fuori da Git). Ogni prova usa cataloghi sintetici o lo snapshot di ricerca `BTCUSDT-1h-raw-v1`; nessuna chiamata di rete (il client HTTP pubblico è finto dove serve).

## Prove eseguite

| Prova e comando | Atteso | Osservato | Esito |
|---|---|---|---|
| `python -m pytest -q -p no:cacheprovider` (clone, venv nuovo) | 620 passed (~9 min) | **620 passed, 184.569 warning, 574,45 s (9:34)** | PASS |
| `paper session-create … --snapshot BTCUSDT-1h-raw-v1` + `session-run` (percorso canonico, PyArrow) | COMPLETED in ~1 min | COMPLETED, equity 524,9125321610413, ultimo evento 2026-09-30 23:00 UTC, 18 trade, 46 s | PASS |
| `python -m crypto_decision_lab.session_fidelity --session-dir <e05conf>` | match true, 15.312 eventi, 61.319 record | `match: true, events: 15312, session_records: 61319, replay_records: 61319`, exit 0 | PASS |
| `session_report edge --as-of "2025-07-13 23:00"` | NOT_DUE, 8 chiusi, equity +0,98%, bh +13,23% | NOT_DUE exit 0; 8 chiusi; −10,74778976 (−2,14955795%); equity +0,97702079%; bh +13,23332755%; f_cut/f_date true | PASS |
| idem `--as-of "2025-07-14 00:00"` | NOT_CONFIRMED, 8 chiusi, netto −10,75 (−2,15%), equity +1,23%, bh +13,54%, dd −7,28%, ritardo 0 | NOT_CONFIRMED; tutte le cifre come atteso, criteri [F,V,F,F], ritardo 0,0 giorni | PASS |
| idem `--as-of "2026-10-01 00:00"` | stesse cifre, ritardo 444 giorni, stop dd −9,33% | NOT_CONFIRMED; cifre identiche al 14 luglio, ritardo 444,0, `stop_drawdown_pct` −9,32943929 | PASS |
| `spec_sha256` in tutti i rapporti | lo stesso valore | `f7cb790cb2210ed5d70514d36ff8338159ffd8e004c54cd69e07e61833b49f5c` in tutti e tre | PASS |
| `recalc_e2e.py` (ricalcolo indipendente dal solo registro SQLite) | coincidenza con i rapporti | tutte le cifre coincidono a 8 decimali (netto, costi 5,84030956, lordo −4,90748020, equity, dd, buy and hold, 8° trade 2025-07-08); intera sessione: 17 chiusi, −23,92643786, equity +4,98250643%, dd −9,32943929% | PASS |
| `probe_r44.py` (4 fasi del gap riconosciuto + varianti + manomissioni) | vedi sezione R44 | 29/29 PASS | PASS |
| `probe_r45_46.py` (sha specifica, esportazione, concorrenza) | vedi sezioni R45/R46 | 16/16 PASS | PASS |
| `probe_r48_49_50.py` (ACL Windows, ammissione, taglio) | vedi sezioni R48/R49/R50 | 24/24 PASS | PASS |
| `probe_r51.py` (straddle a 12 cifre vicino a zero) | — | riproduzione del falso mismatch (vedi R51) | RIPRODOTTO |
| `probe_prod_vs_dry.py` (coerenza prova/produzione sul replay reale) | stesso esito a parte la scrittura | 10/10 PASS (differenze documentate, vedi punto 3) | PASS |
| `git status --porcelain` nel clone a fine lavoro | pulito | nessun file tracciato modificato | PASS |

## Chiusura di R44–R50

### R44 — riconoscimento di gap prima della ripresa — **CHIUSO**

Le quattro fasi richieste dal prompt, tutte su sessioni sintetiche con catalogo e orologio finti:

1. **ack senza poll**: il replay riproduce l'ack in coda dal suo `resumed_at` (fedeltà true); il rapporto **edge in produzione** con specifica preregistrata dà **NOT_DUE** (exit 0) e **non scrive niente** (nessun file, nessuna riga `p1_decisions`, ledger invariato); week12 in produzione ha fedeltà true.
2. **ack, poi poll e ripresa**: fedeltà true dopo la ripresa; un solo record `gap_acknowledged`, nessun record `gap` intermedio.
3. **ack seguito da finish**: ledger `[…, gap_acknowledged, end]`, fedeltà true (il replay riordina ack→end come il runner); `--as-of` su quella sessione è un dry run pulito.
4. **taglio `--as-of` dentro il gap e subito dopo l'ack**: il taglio in tempo di mercato **conserva** l'ack (timestamp = prima ora mancante) e il replay lo riproduce: fedeltà true in tutti e tre gli istanti provati (dentro il gap, subito dopo l'ack, dopo la barra di ripresa).

Varianti richieste: **ordine pendente cancellato** (latenza 1 h, barra di fill mancante: coda `[order_cancelled, gap_acknowledged]`, fedeltà true), **posizione long attraverso l'ack** (quantità conservata, fedeltà true), **due gap col secondo riconosciuto e non ripreso** (2 ack, fedeltà true), **ack sul lunedì del trigger** in entrambi i sensi: (a) gap che **copre** il lunedì → `ready`, `--as-of` e produzione danno **lo stesso esito e le stesse figure** con `fidelity_to_the_cutoff` e `fidelity_to_date` entrambe true (prima della correzione la produzione poteva dare CONFIRMED con `fidelity_to_date=false`); (b) `resumed_at` **uguale** al lunedì → la barra va committata: NOT_DUE, niente scritto, coerenza prova/produzione.

Manomissioni provate sul replay della coda: `resumed_at` falsificato «ingenuo» è **rilevato** (il campo ridondante `missing_hours` non torna); `resumed_at` + `missing_hours` falsificati in modo coerente **prima** della ripresa sono indistinguibili (match true: è un record dell'operatore, il controllo certifica coerenza interna, non autenticità — stessa classe di fiducia del testo `note`, escluso dal confronto); la stessa falsificazione **contraddetta da barre successive** è rilevata (il replay ri-ack al bar reale); un ack inserito a mano con gap di lunghezza zero è **rilevato**; un ack inserito a mano in coda a una sessione RUNNING con gap plausibile è indistinguibile (inherent, vedi R54 per l'effetto sulla tempestività); un ack inserito **dopo** il record `end` è **rilevato** (l'ordine differisce).

### R45 — sha256 della specifica dagli stessi byte — **CHIUSO**

`read_spec` fa una sola lettura di byte, ne ricava JSON e digest, e `main` usa quel digest per l'output e per la decisione registrata. Prova con file sostituito durante il calcolo (swap dentro `week12`): il rapporto esce con il digest dei byte **usati**, la riga del database e il file esportato portano lo stesso digest, il file riletto dopo dà un digest diverso. CRLF e LF danno **lo stesso** digest (normalizzazione `\r\n → \n`); un BOM cambia il digest (l'hash copre il byte BOM) ma in modo deterministico: conta hashare il file preregistrato così com'è. Comando esatto per il coordinatore, verificato riprodurre il digest dello strumento sul file reale `config/experiments/E05-P1-paper-forward-v1.json`:

```bash
python -c "import hashlib,pathlib;print(hashlib.sha256(pathlib.Path(r'config/experiments/E05-P1-paper-forward-v1.json').read_bytes().replace(b'\r\n', b'\n')).hexdigest())"
```

(la normalizzazione rende il valore stabile anche se il checkout di Git converte gli a-capo).

### R46 — esportazione atomica e ripristino — **CHIUSO** (Linux non verificato)

File troncato a metà scrittura → riparato dalla riga del database al retry (exit 2, file identico all'originale); file modificato a mano (`FAILED`→`CONFIRMED`) → ripristinato; file cancellato → ricreato da `export_decision` sia per edge sia per week12 (la riga week12 non viene sovrascritta: l'esito già registrato viene riesportato com'era). Il file è **sempre identico alla riga del database** (confronto JSON completo). Due esportatori contemporanei (processi reali, `os.replace` rallentato di 0,4 s per forzare la collisione): entrambi completano, file corretto, **nessun file temporaneo residuo**. Due valutazioni edge complete in gara (processi reali con replace rallentato): una decide e una esce 2 senza traceback, una sola riga, file presente. Il caso «crash fra scrittura del temporaneo e rename» (processo ucciso con `os._exit`) lascia un orphan `.tmp`: **minore e dichiarato** (R53); la pubblicazione successiva è corretta e l'orphan non interferisce. Su Linux non ho potuto provare (ambiente Windows soltanto): la gestione `PermissionError` con retry è specifica di Windows e qui è stata esercitata davvero; il comportamento su Linux resta non verificato da questo revisore.

### R47 — testo della regola sulla barra non chiusa — **CHIUSO**

La regola di stop della specifica (`criteria.stop_rules_any_time[2]`), la guida e il docstring del modulo ora dicono la stessa cosa del codice: il controllo di fedeltà confronta **valori memorizzati** e non può stabilire **quando** un close sia diventato disponibile; la causalità degli ingressi è una proprietà richiesta a fonte e runner e verificata dai loro test; una violazione accertata all'esterno la dichiara il coordinatore nel registro. La sezione threats della specifica e la voce fedeltà di week12 sono coerenti con questo (e ora citano anche il gap riconosciuto non ancora ripreso). Non ho trovato altre affermazioni, nella specifica o nella guida, che attribuiscano al controllo un potere che non ha.

### R48 — sola lettura e file ausiliari — **CHIUSO** (su Windows; Linux non verificato)

Il contratto sui file ausiliari è documentato nel docstring, nella specifica (`procedure.reading`) e nella guida, e il codice lo rispetta: in cartella scrivibile la lettura `mode=ro` può creare `-wal`/`-shm` accanto al database (verificato: appaiono) ma il **file principale non cambia** (hash prima/dopo identico); in cartella in sola lettura **senza** `-wal` (simulata con `icacls /deny *S-1-1-0:(W)` sulla sola cartella: impossibile creare file, db leggibile) la lettura `mode=ro` fallisce e il fallback `immutable=1` **funziona** (tabelle identiche alla lettura normale, nessun sidecar creato); con un `-wal` presente il fallback è **rifiutato** (correttamente: ignorare un WAL vivo non è sicuro), ma il messaggio è quello generico di SQLite (`unable to open database file`), che non menziona il `-wal`: la procedura è scritta nella guida, il messaggio no (R54/INFO). Una cartella scrivibile con `-wal` vuoto si legge normalmente. Su Linux non ho potuto ripetere le prove di ACL.

### R49 — ammissione sui valori in effetto — **CHIUSO**

Sessione creata **via API** con fonte `public_klines` e warmup di default (client HTTP finto): l'identità memorizzata riporta `history_hours: 2600` senza che il campo sia scritto nel source, e l'ammissione **passa** (prima dava il falso INVALID «warmup of None»); valori scritti che contraddicono l'identità (`history_hours`, `symbol`) sono rifiutati con «disagrees with the data source in effect»; `identity.kind` diverso è rifiutato; `registered` = `"NaT"`, `"not a date"`, vuoto sono rifiutati come data non valida; una data con fuso è accettata e convertita in UTC (una sessione che inizia prima di essa, in UTC, è comunque rifiutata). Campo ancora controllato sul valore dichiarato anziché su quello in effetto: **nessuno che permetta di passare** — `kind` ricade sul valore scritto solo se l'identità non lo riporta, ma in quel caso simbolo, host e warmup sull'identità mancano e la sessione è rifiutata comunque (provato: source senza identità → tre motivi di rifiuto). L'identità reale di `PublicKlineSource` include sempre `kind`.

### R50 — taglio in tempo di mercato — **CHIUSO**

Il taglio taglia eventi, registro e storia sull'orario della **barra** e `status_log` su **`at` e `clock`** (verificato su ogni riga); `cursor` = ultima barra contenuta, `status` = `None` (documentato: lo stato del runner a quell'istante non è memorizzato); `read_snapshot(as_of)` è uguale a `snapshot.until(as_of)`. Una barra committata in ritardo (riga di commit spostata avanti) resta nel taglio mentre la sua riga di log sparisce: è il replay retrospettivo dichiarato, con la tempestività misurata solo sull'orologio reale (coerente col testo nuovo). week12 con `--as-of` conteggia solo i giorni scaduti entro l'istante (8 giorni a T0+8: verificato). Coerenza col cursore/stato «che il runner aveva a quell'istante»: il cursore sì (ultima barra), lo stato no per progetto; entrambi i punti sono ora scritti nel docstring, nella specifica e nella guida.

## Punto 3 — coerenza prova/produzione

Sul replay reale (copia della sessione e05conf), per lo stesso istante: **edge** `--as-of` e produzione (orologio patchato all'istante, ammissione allineata alla sessione replay) danno **esito, figure, criteri, trigger e flag di fedeltà identici** prima del lunedì (NOT_DUE, niente scritto), al lunedì (NOT_CONFIRMED registrato una volta, file speculare al report, seconda chiamata exit 2) e molto dopo (444 giorni di ritardo, figure congelate al trigger: non si può aspettare una cifra migliore). **week12**: esiti e verditi dei criteri coincidono; **differiscono** (per progetto, e ora documentato) i valori osservati `fidelity.events`, `max_delay_hours` e il drawdown quando la sessione fisicamente contiene dati oltre l'istante — cosa che **in produzione dal vivo non può accadere** (il runner non ha barre future) e che invece accade su replay e copie d'archivio: è esattamente la semantica «replay in tempo di mercato» dichiarata. L'ho caratterizzato con la sequenza: dry a 2025-06-02 (dd −7,28, 3.649 eventi) vs produzione allo stesso istante (dd −9,33, 15.312 eventi), stesso esito OPERATIONAL_FAIL. Nessuna sequenza in cui l'**esito** differisca per gli stessi dati e lo stesso istante, tranne il caso per progetto dell'ammissione (solo produzione): su una sessione replay una produzione con la specifica reale dà INVALID mentre `--as-of` non valuta l'ammissione — comportamento documentato e voluto.

## Punto 5 — specifica, guida e codice

Riletto il diff criterio per criterio: i testi nuovi della specifica (`admission`, `stop_rules_any_time[2]`, fedeltà week12, `outcomes.week12.reports`, `outcomes.edge.recorded`, `procedure.reading`) e della guida dicono ciò che il codice fa, e l'ho provato sopra. In particolare: **«dopo la decisione sul vantaggio il comando edge non ricalcola più niente»** è vero nel codice (`edge_decided` è valutato prima di qualsiasi calcolo e fa uscire subito con 2; una seconda valutazione non ricalcola nemmeno gli stop) e **«gli stop li segue week12»** è vero (week12 ricalcola sempre drawdown e ammissione: un FAILED successivo si affianca a un OPERATIONAL_PASS senza sovrascrivere, provato in R46). Le prove `--as-of` non riparano né creano file (dry run non chiama `edge_decided`): coerente con «non scrive niente».

## Punto 6 — regole affidate al coordinatore

La tabella della review R3 resta esatta nelle righe che il diff tocca, con due aggiornamenti: «Spec e sessione ufficiale» è ora pienamente accettabile (R45 chiuso: SHA degli stessi byte, comando del coordinatore verificato) e «Fedeltà e disponibilità dei close» ha ora testi allineati (R47 chiuso). Contenuto minimo del registro ufficiale prima dell'avvio, come già da specifica/guida più una lacuna da colmare: **identificativo della sessione attiva, sha256 della specifica preregistrata (confrontato prima di accettare qualunque esito), data di preregistrazione, orario di inizio, il commit del runner** (citato dalla guida e dalla riga PRD; il campo `official_register` della specifica **non** lo elenca — vedi R55), ogni ripartenza con approvazione e motivo, la scadenza a 78 settimane dalla prima sessione, e in corsa le decisioni prese con la loro evidenza (`evidence_to_record`). Le copie della cartella e la cancellazione manuale di riga/file restano fuori dalla portata dello strumento: è il registro a renderle nulle (invariato).

## Punto 7 — rilievi vecchi

R17 LOW (barre di warmup congelate): invariato, non più rilevante. R18 LOW: ora i testi sono ovunque allineati (era la contraddizione risolta da R47). R19 INFO (equity da balance): il mio ricalcolo indipendente riconcilia di nuovo le cifre. R20 INFO (retry HTTP del feed): invariato, fuori dal diff. R26 INFO (errore del feed durante l'acknowledge): invariato. R13/R14 INFO (S08/S07): fuori dal percorso modificato, invariati.

## Nuovi rilievi

**R51 · MEDIUM · Il confronto di fedeltà a 12 cifre significative può dare un falso mismatch quando una feature è vicina a zero.**

Oggetto: `session_fidelity.normalized` / `_round_features` (12g), `daily_trend.daily_features_at` (somma Python, dalla più recente) contro `daily_trend.daily_features` (`rolling().mean()` di pandas, dalla più antica). Meccanismo: i due ordini di somma differiscono di qualche ulp sulla SMA; `d_trend = close/SMA − 1` e `d_momentum = close/close − 1` sottraggono 1 e **amplificano** l'errore relativo di un fattore ≈ 1/|valore|: quando |feature| ≲ 1e-4 la differenza supera 1e-12 relativo e le due rappresentazioni a 12 cifre differiscono → `match` false senza alcuna divergenza reale.

Riproduzione minima (script `probe_r51.py`, dati sintetici seed 17): sessione 90 giorni, feature accelerate `momentum_days=2, sma_days=3`; al 2024-06-30 la sessione memorizza `d_trend = -3.17369617132e-05`, il replay produce `-3.1736961713e-05` (SMA 162,28171366542776 contro 162,28171366542773); **tutti i 2.160 record decisione sono identici** — è solo la stringa della feature a differire — eppure `check` dà `match: false` (→ OPERATIONAL_FAIL a week12, INVALID all'edge). Sulla stessa serie con 7/20 il match è true. Il replay ufficiale (28/100, dati reali) è **non** toccato: l'ho verificato per intero (61.319 record, match true) e l'aveva verificato l'autore; ma durante una sessione dal vivo di 12–52 settimane ogni giorno di decisione con |d_trend| (o |d_momentum|) entro ~1e-4 — tipico vicino agli incroci con la media, cioè vicino alle soglie 0 della candidata — tira i dadi. Correzioni possibili (decide il coordinatore): confrontare con tolleranza mista assoluta/relativa, ridurre le cifre, o far sommare il replay nello stesso ordine del runner. Fino ad allora il rischio operativo è un falso stop che consuma una ripartenza o chiude la candidata: va almeno dichiarato nelle minacce.

**R52 · INFO · Un file `p1-edge-report.json` senza riga nel database blocca per sempre la valutazione edge (exit 2).**

`edge_decided` restituisce vero anche se esiste solo il file (nessuna riga `p1_decisions`): un file spurio o contraffatto nella cartella della sessione fa uscire edge con 2 «already evaluated» senza calcolare e senza riparare il file (il dry run lo ignora e calcola regolarmente). Riprodotto sul replay reale: file contraffatto `{"outcome":"CONFIRMED"}`, produzione → exit 2, nessuna riga creata, file lasciato com'è. Stessa classe di fiducia della cancellazione manuale (accesso in scrittura alla cartella): il registro ufficiale rende nullo l'esito; conviene documentare che il file senza riga va rimosso a mano.

**R53 · INFO · Un crash fra la scrittura del temporaneo e la rename lascia un file `.tmp` orfano.**

`_publish` ripulisce il proprio temporaneo in ogni percorso di guasto gestito, ma un processo ucciso tra la scrittura e la `os.replace` lascia `.p1-edge-report.json.<uuid>.tmp` nella cartella (riprodotto con `os._exit(9)` durante il replace). Nessun impatto sulla correttezza (il file pubblicato successivamente è quello giusto); «nessun temporaneo lasciato» vale per il funzionamento senza crash. Facoltativo: ripulire gli orfani all'avvio dell'esportazione.

**R54 · INFO · Il rifiuto in cartella di sola lettura con `-wal` presente ha un messaggio generico.**

Con ACL di sola lettura e un `-wal` accanto al database, `read_snapshot` rilancia `OperationalError: unable to open database file`, che non menziona il `-wal` né la procedura (copiare in cartella scrivibile). La procedura è documentata in specifica e guida; un messaggio dedicato eviterebbe di doverla cercare.

**R55 · INFO · Il campo `official_register` della specifica non elenca il commit del runner.**

Guida e riga PRD chiedono che la preregistrazione citi il commit del runner (reproducibilità di ciò che gira); `procedure.official_register` elenca id, sha256, inizio, ripartenze e scadenza ma non il commit. Un rigo di testo chiude la lacuna.

## Limiti di questa revisione

- Windows only: concorrenza (`PermissionError`/retry), ACL di sola lettura e comportamento di `os.replace` non sono stati provati su Linux; le corrispondenti prove Linux delle review precedenti restano l'unica evidenza.
- Le prove «in produzione» simulano l'orologio e i costanti LIVE della fonte per adattarli a sessioni replay/synthetic, come fanno i test dell'autore; nessuna ammissione è stata bypassata (le differenze sono dichiarate nei risultati).
- `data/forward/` non è stato letto (perimetro CP08): nessuna prova lo tocca.
- La probabilità quantitativa di R51 sulla candidata reale non è stata stimata oltre la riproduzione del meccanismo e il conteggio dei giorni a rischio sul dato sintetico.

## Verdetti

| Oggetto | Verdetto |
|---|---|
| `session_snapshot` | Approvato: contratto sui file ausiliari onesto e verificato su Windows, taglio in tempo di mercato corretto e documentato, fallback `immutable` sicuro (rifiuto con `-wal`) |
| `session_fidelity` | Approvato con R51: il replay del gap riconosciuto non ripreso (anche con `finish`, ordine pendente, posizione, due gap) è corretto e robusto alle manomissioni rilevabili; resta il rischio del falso mismatch a 12 cifre vicino a zero |
| `session_report` | Approvato: digest sugli stessi byte, ammissione sui valori in effetto, decisione atomica, esportazione dal database con pubblicazione atomica e ripristino, concorrenza esercitata su Windows |
| Specifica E05-P1 | **Preregistrabile a giudizio di questo revisore**, dopo una decisione esplicita del coordinatore su R51 (correggere, o dichiararlo nelle minacce e accettarne il rischio operativo) e i quattro INFO (R52–R55, tutti documentali). Nessun rilievo tocca l'esito del replay ufficiale, riprodotto e ricalcolato per intero |

Modifiche richieste prima della preregistrazione: nessuna obbligatoria oltre la decisione su R51. Raccomandate: la tolleranza (o l'ordine di somma) di R51; il rigo sul commit del runner in `official_register` (R55); i messaggi/contratti INFO restano a scelta.

## Note per il coordinatore

- La suite (620 passed) e il replay canonico con PyArrow sono stati eseguiti da questo revisore: non serve rieseguirli per completare la verifica di questo giro (resta tua discrezione).
- GLM-5.3 è stato già usato su E05-P1 (R2): se per il PRD §5 serve una famiglia mai vista su questo esperimento, questa review va considerata complementare, non sostitutiva.
- Dopo la preregistrazione: registro ufficiale (con il commit del runner), avvio su macchina sempre accesa (PRE-3) e backup della cartella della sessione.
