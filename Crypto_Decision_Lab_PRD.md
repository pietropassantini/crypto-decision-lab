# Crypto Decision Lab — PRD e backlog di esecuzione

**Versione 1.1 · 12 novembre 2026**

La versione 1.1 aggiunge lo Step S00A per sanificare il repository dopo la verifica della baseline.

La visione di prodotto è approvata. I checkpoint e l'ordine di esecuzione qui definiti sono la proposta operativa iniziale. Il backlog parte dall'analisi tecnica già svolta.

## 1. Obiettivo di prodotto

Consentire di raccogliere dati, costruire dataset, studiare pattern di mercato, addestrare modelli, confrontare strategie e portare una versione verificata da backtest a paper trading e infine a trading automatico reale. I risultati osservati in esercizio alimentano nuove ipotesi e nuove versioni candidate.

Il primo caso d'uso è BTCUSDT spot 1h, con una strategia deterministica e una variante che usa un Decision Model per filtrare gli ingressi. L'obiettivo del confronto è misurare il contributo del filtro dopo i costi. **Il completamento della piattaforma e la promozione di una strategia sono due esiti distinti.** Una strategia che fallisce i criteri torna in ricerca. L'attivazione con capitale reale è una decisione operativa esplicita sulla versione e sui limiti da applicare.

### Requisiti utente

| ID | L'utente deve poter |
|---|---|
| R01 | Controllare freschezza, copertura, gap, quarantena, versione del manifest, esito dell'audit e readiness dal monitor di raccolta. |
| R02 | Creare un dataset di ricerca versionato e ricostruire la provenienza di snapshot, feature e target. |
| R03 | Formulare un'ipotesi, addestrare un modello e valutare se riconosce un pattern utile su finestre temporali fuori campione. |
| R04 | Selezionare e configurare strategie intercambiabili da Strategy Studio, lanciare backtest, confrontare run e ispezionare il motivo di ogni operazione. |
| R05 | Avviare paper trading su una versione precisa, vedere posizioni e ordini simulati e riprendere la sessione dopo un riavvio. |
| R06 | Attivare una versione promossa in live con limiti espliciti, osservare gli ordini reali e sospendere le nuove aperture. |
| R07 | Confrontare le esecuzioni live con le ipotesi di backtest e paper e usare gli scostamenti per pianificare il prossimo esperimento. |
| R08 | Passare uno step a un altro agente o LLM usando prompt, revisione del codice, evidenze e decisioni registrate. |

## 2. Baseline e confini

| Baseline GitHub analizzata | Implicazione |
|---|---|
| [main · 0daf402](https://github.com/pietropassantini/crypto-decision-lab/commit/0daf402) | Streamlit ospita già la vista Forward Data Status e l'analisi CP07. La navigazione raggiunge la vista forward dopo il caricamento dello storico. CP07 simula operazioni complete di ingresso, uscita e rendimento. |
| [CP08-07 · 2272d49](https://github.com/pietropassantini/crypto-decision-lab/commit/2272d49) | Monitor qualità con report `latest-audit.json`, da collegare alla vista operativa. |
| [CP09 · 7109e90](https://github.com/pietropassantini/crypto-decision-lab/commit/7109e90) | Dataset con feature e target in artefatti separati. L'ipotesi di ingresso all'open del cutoff va riconciliata con l'esecuzione del nuovo motore. |

Nell'analisi precedente i test mirati hanno dato **97 passati e 2 falliti** su main, **60 passati e 1 fallito** su CP08-07 e **17 passati** su CP09. Sono evidenze storiche da aggiornare allo Step 00. I fallimenti riguardavano il fixture forward senza sidecar v2 e, su main, lo smoke test CP08-03 in un checkout con archivio incompleto. La revisione effettivamente distribuita su `trade-vector` era da verificare.

Il dataset **`BTCUSDT-1h-forward-v1`** è riservato alla validazione indipendente CP08. Il monitor può mostrarne qualità, provenienza e readiness. Ricerca, training, backtest esplorativi e paper di sviluppo usano dati ammessi dal relativo catalogo. Durante la raccolta, il paper in tempo reale usa un feed di laboratorio fuori dal perimetro CP08, anche quando esistono copie degli stessi dati sotto altri percorsi.

## 3. Architettura e contratti

- **Dati.** La pipeline di ricerca legge snapshot e manifest identificati per hash. Il Dataset Builder produce feature e target separati con timestamp di disponibilità, versione dello schema, ipotesi di esecuzione e riferimenti agli input.
- **Decisione.** Una strategia legge feature disponibili al cutoff e stato del portafoglio. Restituisce esposizione desiderata e motivazione. Il Decision Model, quando presente, fornisce uno score o un regime. La strategia traduce l'output in una proposta `LONG` o `FLAT`. Il Risk Engine controlla la proposta e dimensiona l'ordine.
- **Esecuzione.** Il motore registra separatamente evento di mercato, decisione, esito del rischio, ordine ed esecuzione. Backtest e paper replay condividono motore, clock degli eventi e broker simulato. Il paper su feed corrente usa un clock operativo. Il live usa un adattatore exchange e riconcilia gli esiti effettivi.
- **Versioni.** Un run identifica dataset o feed, revisione del motore, strategia, modello e hash dei relativi artefatti, parametri, capitale, costi e limiti di rischio. Una versione attiva rimane fissata fino a una nuova attivazione.
- **Interfaccia.** Streamlit e Plotly restano la base proposta. La UI configura e avvia i run e legge gli artefatti. Il runner possiede lo stato delle sessioni e può operare anche a UI chiusa. Parquet e JSON sono il formato iniziale per risultati e manifest. La persistenza transazionale delle sessioni viene introdotta nello Step 06.

## 4. Roadmap

La difficoltà esprime complessità tecnica, non durata. **Bassa** indica un intervento circoscritto. **Media** coinvolge più componenti o richiede test d'integrazione. **Alta** richiede gestione di stato, riproducibilità o verifica statistica. **Molto alta** coinvolge esecuzione reale e recupero da errori esterni.

| Step | Incarico | Dipendenze | Difficoltà | Risultato |
|---|---|---|---|---|
| S00 | Baseline e avvio | Analisi già svolta | Bassa | Revisioni e test riproducibili |
| S00A | Sanificazione del repository | S00 | Media | Checkout pulito, riproducibile e con artefatti censiti |
| S01 | Monitor raccolta | S00A | Media | Vista operativa indipendente dallo storico |
| S02 | Dataset Builder | S00A | Alta | Dataset versionato con semantica temporale verificata |
| S03 | Motore di simulazione | S00A | Alta | Strategia e rischio condivisi, backtest e replay coerenti |
| S04 | Strategy Studio | S02, S03 | Alta | Configurazione, backtest e confronto nella UI |
| S05 | Pattern e Decision Model | S02, S03, S04 | Alta | Esperimento fuori campione e modello candidato versionato |
| S06 | Paper trading persistente | S03, S04 | Alta | Sessione riprendibile su feed di laboratorio |
| S07 | Adattatore live e collaudo | S06 | Molto alta | Runtime verificato in ambiente di collaudo |
| S08 | Rilascio operativo | S01, S05, S07 | Molto alta | Piattaforma pronta e decisione sulla prima attivazione |
| S09 | Misura e ciclo successivo | S08 | Media | Report operativo e nuovi esperimenti tracciati |

S01, S02 e S03 possono avanzare in parallelo dopo S00A. S05 e S06 possono avanzare in parallelo dopo S04. Un esito negativo del Decision Model chiude comunque l'esperimento S05 con evidenza e consente di valutare una candidata deterministica in S08, se supera i propri criteri.

## 5. Protocollo per agenti intercambiabili

**Input per l'agente assegnato.** Passare questo PRD, l'ID dello step, il prompt dello step, la revisione base esatta, le righe di backlog delle dipendenze e gli artefatti verificati. Il modello è un dato del run. La fonte di verità è il backlog con evidenze consultabili.

**Prompt comune da anteporre a ogni incarico.**

> Lavora esclusivamente sullo step assegnato nel repository Crypto Decision Lab. Leggi le istruzioni del repository, verifica il commit base e confrontalo con il backlog. Controlla le evidenze dei checkpoint dipendenti. Riutilizza i moduli esistenti quando rispettano i contratti. Mantieni la raccolta CP08 isolata dalla ricerca. Esegui i checkpoint indicati e restituisci per ciascuno comando o procedura, risultato atteso, risultato osservato e riferimento all'evidenza. Registra branch, commit o diff, file toccati, decisioni, anomalie e prossimo passo nel formato di handoff. Se un checkpoint è bloccato, registra la dipendenza mancante e il lavoro completato. Il verificatore può ripetere le prove partendo dal commit indicato.

**Verifica e cambio agente.** Il responsabile dello step può essere sostituito in qualsiasi momento. Il successore parte dall'ultimo commit verificato e dall'ultimo handoff. Per checkpoint che cambiano contratti, guardie sui dati o percorso live, un revisore diverso dall'autore ripete le prove determinanti. Il coordinatore aggiorna la riga di backlog a `VERIFIED` quando tutti i checkpoint dello step hanno evidenza. I cambiamenti al PRD si registrano con data, motivazione, step coinvolti e decisione.

## 6. Incarichi, prompt e checkpoint

### S00 — Baseline riproducibile

**Agente:** integrazione e QA · **Difficoltà:** Bassa.

> Verifica gli head correnti di main, CP08-07 e CP09. Prepara un checkout di analisi e ripeti i test mirati del monitor, della dashboard, di CP07 e di CP09. Distingui regressioni del codice, fixture incomplete e dipendenze locali. Se il checkout distribuito è accessibile in lettura, rilevane SHA e configurazione operativa pertinente. Prepara il backlog con riferimenti esatti e una strategia d'integrazione dei branch.

- **CP00.1** — Tabella con SHA, data, comandi di test ed esiti per branch. Ogni fallimento ha una riproduzione o una causa confermata.
- **CP00.2** — Baseline scelta per ciascuno step dipendente. SHA distribuito e stato operativo risultano verificati oppure registrati come non osservati con la verifica necessaria.

### S00A — Sanificazione del repository

**Agente:** manutenzione del repository e QA · **Difficoltà:** Media.

> Parti dagli SHA e dai test di S00. Censisci file tracciati, duplicati, script obsoleti, configurazioni locali, artefatti generati, fixture e dipendenze mancanti. Controlla anche i riferimenti da import, test, documentazione e procedure di deploy prima di rimuovere o spostare un file. Applica le correzioni in una PR dedicata. Conserva codice attivo, fixture necessarie, snapshot di riferimento, manifest, sidecar, report di validazione e compatibilità con i servizi distribuiti. Documenta dove reperire gli artefatti richiesti per riprodurre i test. Se trovi credenziali o dati sensibili tracciati, registra percorso e tipo senza riportarne il contenuto e apri un'attività di bonifica dedicata. Mantieni l'intervento nella normale cronologia Git.

- **CP00A.1** — Inventario verificabile con percorso, utilizzo noto e decisione per ogni elemento candidato alla pulizia. Ogni rimozione o spostamento ha una verifica dei riferimenti e un'evidenza nel diff.
- **CP00A.2** — Installazione da checkout pulito con i comandi documentati e test mirati di dashboard, raccolta, simulatore e dataset disponibili alla revisione. Le dipendenze dei test sono dichiarate. I fixture o artefatti richiesti sono disponibili o hanno una procedura di recupero verificabile. Gli esiti sono confrontati con S00, con motivazione per le eventuali anomalie residue.
- **CP00A.3** — Controlli su indice Git e regole di esclusione verificano che output rigenerabili, configurazioni locali e credenziali non entrino nei nuovi commit. Fixture, esempi e artefatti di riferimento attesi rimangono tracciabili.
- **CP00A.4** — Diff e verifica degli hash confermano che il dataset forward, i manifest, i sidecar e gli artefatti congelati CP08 sono intatti. I riferimenti ai percorsi operativi sono verificati staticamente o in collaudo e il backlog riporta commit, test e attività residue.

### S01 — Monitor della raccolta

**Agente:** Streamlit e operazioni dati · **Difficoltà:** Media.

> Rendi Forward Data Status raggiungibile anche quando storico e forecast di ricerca sono indisponibili. Riusa l'adattatore forward e integra il report CP08-07. Mostra manifest auditato, manifest corrente, timestamp, lag e fallimenti. Mantieni la readiness CP08 separata dalla qualità operativa. Verifica i fixture e preserva i significati del protocollo.

- **CP01.1** — AppTest della vista senza snapshot storico. La vista carica e mostra i dati del monitor disponibili.
- **CP01.2** — Casi verificati per audit corrente, audit su manifest precedente, audit assente e audit fallito. La UI distingue i quattro stati.
- **CP01.3** — Test o confronto di hash dimostra che il caricamento della vista lascia intatti archivio e manifest forward.

### S02 — Catalogo e Dataset Builder

**Agente:** data engineering · **Difficoltà:** Alta.

> Integra i concetti di CP09 nel Dataset Builder di ricerca. Identifica gli input con manifest e hash, registra il tempo di disponibilità delle feature e mantieni target e feature separati. Dichiara come cutoff, latenza, orizzonte e costi determinano i target. Con S03 concorda lo stesso contratto di esecuzione. Costruisci il primo dataset versionato e applica le guardie di provenienza anche a copie o alias delle fonti riservate.

- **CP02.1** — Due build con stessi input e parametri producono gli stessi contenuti di feature e target, verificati per hash o digest canonico. Gli eventuali timestamp di build sono metadati separati.
- **CP02.2** — I test respingono input forward per identità o provenienza, feature disponibili oltre il cutoff e finestre prive dei dati richiesti.
- **CP02.3** — Un fixture con prezzi e tempi noti ricostruisce il target con lo stesso esito economico del motore S03, comprese latenza e commissioni.

### S03 — Motore, strategie e broker simulato

**Agente:** backend del motore di trading · **Difficoltà:** Alta.

> Realizza un motore a eventi per una posizione spot LONG o FLAT. Aggiungi il contratto Strategy, un registro per selezione da configurazione, strategie deterministiche di riferimento, Risk Engine e broker simulato. Separa disponibilità dell'evento, decisione, eleggibilità dell'ordine ed esecuzione. Riusa le formule CP07 compatibili e usa CP07 come riferimento di riconciliazione. Rendi il motore richiamabile da CLI e test.

- **CP03.1** — Un fixture sostituisce la strategia da configurazione e verifica apertura, chiusura, rifiuto del rischio e registrazione della motivazione.
- **CP03.2** — Backtest e paper replay su stessi eventi, configurazione e clock producono lo stesso registro canonico di decisioni, ordini, esecuzioni e saldi.
- **CP03.3** — Test di costi, latenza, eventi duplicati, gap, fine del run con posizione aperta e assenza di esecuzione su un prezzo precedente all'eleggibilità. Le differenze rispetto a CP07 sono spiegate con un caso riproducibile.

### S04 — Strategy Studio

**Agente:** applicazione Streamlit · **Difficoltà:** Alta.

> Implementa le viste Strategie, Nuovo backtest e Risultati. Consenti di salvare una configurazione immutabile e di avviare un run da un dataset ammesso. Mostra grafici, registro operazioni, motivazioni e costi. Aggiungi il confronto tra run, indicando le differenze di dataset, periodo e ipotesi di esecuzione. Riusa i grafici esistenti dove le definizioni delle metriche coincidono.

- **CP04.1** — Test UI salva una configurazione, avvia un run, apre un trade e rilegge i risultati in una nuova sessione.
- **CP04.2** — Su un fixture, P&L netto, drawdown mark-to-market, costi, esposizione e conteggio trade si riconciliano con il registro. La UI espone risultati aggregati e per finestra.
- **CP04.3** — Due run a parità di condizioni risultano confrontabili. Un cambio di periodo o modello di costi è visibile. Il catalogo dello Studio espone solo fonti autorizzate.

### S05 — Ricerca dei pattern e Decision Model

**Agente:** ML e validazione quantitativa · **Difficoltà:** Alta.

> Studia se un modello riconosce ingressi della strategia deterministica da filtrare. Registra prima dell'esperimento ipotesi, target, baseline, finestre temporali e criteri di decisione. Addestra su finestre cronologiche e seleziona soglie sullo sviluppo. Confronta baseline e variante filtrata fuori campione con gli stessi prezzi di esecuzione, costi e limiti. Registra il candidato e rendi lo score consumabile dallo Strategy Layer.

- **CP05.1** — Test che verifica split temporali e maturazione dei target prima del confine di training. Le feature d'inferenza escludono i target. Configurazione e seed rendono l'esperimento riproducibile entro la tolleranza dichiarata.
- **CP05.2** — Report per finestra con conteggi, trade filtrati, rendimento netto, drawdown e variabilità. Se l'output è una probabilità, il report include anche calibrazione e numerosità.
- **CP05.3** — Modello, schema feature, soglie e dataset sono identificabili. L'esito `CANDIDATE`, `INCONCLUSIVE` o `REJECTED` è motivato sui criteri registrati. L'adattatore del modello supera i test di contratto.

### S06 — Paper trading persistente

**Agente:** runtime e persistenza · **Difficoltà:** Alta.

> Esegui sessioni paper da configurazioni versionate, prima in replay e poi su feed di laboratorio fuori dal perimetro CP08. Persiste insieme lo stato del portafoglio, l'identità degli eventi e il cursore. Esponi stato della sessione, ultimo evento, posizioni, ordini, costi e P&L a Strategy Studio. Gestisci dati obsoleti o incompleti con uno stato esplicito.

- **CP06.1** — Arresto simulato prima e dopo la scrittura dello stato. La ripresa produce lo stesso saldo e registro della sessione continua, senza duplicati.
- **CP06.2** — Un feed di laboratorio riproducibile mostra avvio, aggiornamento, pausa su gap, recupero controllato e stato visibile dalla UI.
- **CP06.3** — Una sessione mantiene la strategia e il modello selezionati all'avvio. Una seconda sessione può usare una versione diversa con stato indipendente.

### S07 — Adattatore live e collaudo

**Agente:** integrazione exchange e affidabilità · **Difficoltà:** Molto alta.

> Implementa un adattatore exchange dietro il contratto di esecuzione. Dopo la scelta dell'exchange, verifica l'API ufficiale e le regole operative applicabili all'account. Collauda con sandbox o ambiente di test e fixture di risposta. Traccia ordini con un identificatore locale persistente, riconcilia esiti incerti e gestisci rifiuti, cancellazioni ed esecuzioni parziali. Configura limiti e sospensione dei nuovi ordini. Usa un ambiente di collaudo per le prove di questo step.

- **CP07.1** — Prove riproducibili di invio, conferma, rifiuto, annullamento ed esecuzione parziale. Quantità, prezzi, costi e stato locale si riconciliano con gli eventi restituiti.
- **CP07.2** — Timeout dopo l'invio e riavvio con ordine aperto. Il runner riconcilia lo stato prima di inviare ulteriori ordini e il fixture non mostra duplicati.
- **CP07.3** — Limite di esposizione, feed obsoleto, disallineamento del saldo e sospensione manuale bloccano le nuove aperture previste dalla configurazione. Il runbook descrive anche il trattamento degli ordini aperti e delle posizioni.

### S08 — Rilascio e attivazione controllata

**Agente:** release e operazioni · **Difficoltà:** Molto alta.

> Prepara il pacchetto rilasciabile, la configurazione live e il runbook di avvio, monitoraggio, sospensione e ripristino. Registra prima della decisione i criteri paper e operativi della versione candidata. Verifica che versione, limiti ed exchange configurati coincidano con quelli approvati per l'eventuale attivazione. Collauda il percorso completo. Registra l'esito della decisione sulla prima operatività con capitale reale.

- **CP08.1** — Runbook ripetuto in collaudo. Evidenze per avvio da installazione pulita, ripresa con ordini aperti, riconciliazione e sospensione.
- **CP08.2** — Scheda di rilascio con versione esatta, evidenze di backtest e paper, criteri osservati, configurazione dei limiti e decisione `READY` o `BLOCKED`, con motivazione.
- **CP08.3** — Decisione operativa registrata come `ACTIVATED` o `NOT_ACTIVATED`. In caso di attivazione autorizzata, verificare la prima riconciliazione live e il tracciamento dei primi ordini quando la strategia li genera. In caso di mancata attivazione, registrare il requisito ancora aperto o la decisione di operare in paper.

### S09 — Misura operativa e ciclo successivo

**Agente:** osservabilità e ricerca quantitativa · **Difficoltà:** Media.

> Collega eventi operativi a versione, decisioni ed esecuzioni. Produci un report che confronti costi, latenza, segnali ed esiti osservati con le ipotesi di simulazione. Se il live non è attivo, collauda il report con dati paper o fixture live etichettati. Trasforma gli scostamenti in nuove ipotesi di ricerca con una metrica e un checkpoint.

- **CP09.1** — Report riproducibile da un registro noto. Evidenzia feed, modalità, versione, fill mancanti, slippage osservato, costi e latenza. I totali si riconciliano con il registro.
- **CP09.2** — Almeno un nuovo esperimento nel backlog ha ipotesi, input, baseline, criterio di accettazione e prompt. È riprendibile da un agente diverso.

## 7. Backlog iniziale

Questa tabella è la coda iniziale. Durante l'implementazione va mantenuta con il codice o con un registro versionato collegato ai commit, così rimane disponibile a ogni agente. Gli ID `CPxx.y` sono checkpoint del presente PRD. Le sigle storiche CP07, CP08 e CP09 indicano gli esperimenti e i branch già esistenti.

| ID | Stato | Dipendenze | Revisione o evidenza | Prossima azione |
|---|---|---|---|---|
| A00 | DONE | — | Analisi di main, CP08-07 e CP09, con SHA ed esiti nella sezione 2 | Usare come punto di partenza per S00 |
| S00 | VERIFIED | A00 | Baseline storica disponibile | main=~~430P/1S/0F~~ 288 test (clone pulito 285P/3F; correzione del 2026-11-14, §7 del report: il 430 era della punta locale `38dfa60` di cp09), cp08-07=297P/1F, cp09=297P/0F. SHA main=0daf402 scelto come baseline. [S00-BASELINE-REPORT.md](S00-BASELINE-REPORT.md) |
| S00A | VERIFIED | S00 | Commit 6414abd (sanificazione, -385 file). [S00A-HANDOFF.yaml](S00A-HANDOFF.yaml): CP00A.1–4 PASS, inventario in [S00A-INVENTORY.md](S00A-INVENTORY.md); 342 test = 288 main + 54 nuovi. Anomalia: i 430 test di S00 non sono riproducibili per main. Rilievo F7 (output rigenerabili non esclusi) chiuso il 2026-11-13 con `.gitignore`. Review [REVIEW-S00A-2026-11-14.md](REVIEW-S00A-2026-11-14.md): CP00A.1, .3, .4 PASS; CP00A.2 FAIL per A1 (PyYAML non dichiarato nel nucleo) e A3 (dato `main` di S00 errato). Corretti il 2026-11-14 A1, A3, A4 e F9 (snapshot ritracciato) | Verificato dal coordinatore il 2026-11-14 dopo la revisione indipendente [GLM-5.3 del 2026-11-14](REVIEW-INDEP-2026-11-14-glm.md): CP00A.1–4 ripetuti da clone pulito in venv isolato, A1, A3, A4, A6, F7 e F9 chiusi. Restano A2 (su S06) e A5 (INFO) |
| S01 | VERIFIED | S00A | Commit 8949237 + correzione fixture (sidecar v2). 342 test verdi. Dopo la review del 2026-11-13 il monitor CP08-07 è stato corretto (commit f3b0dbb): banner di successo solo con AUDIT_CURRENT, 9 stati verificati in UI, checkout pulito con 440 test verdi; verificato da Claude Opus 5.5 con revisione non indipendente ([REVIEW-S00A-S06.md](REVIEW-S00A-S06.md)). [S01-HANDOFF.yaml](S01-HANDOFF.yaml): CP01.1–3 PASS | Verificato dal coordinatore il 2026-11-14 dopo la revisione indipendente [GLM-5.3 del 2026-11-14 su 6651afc](REVIEW-INDEP-2026-11-14-glm2.md): CP01.1–3 ripetuti (banner di successo solo con AUDIT_CURRENT, 9 stati, no-write su fixture); CP01.3 sull'archivio forward reale non eseguito dal revisore. Aperto: soglia di età dell'audit nella UI, da concordare con CP08 |
| S02 | VERIFIED | S00A | Commit 62c38c9 su feat/s02-dataset-builder: execution_contract, dataset_catalog, dataset_builder; dataset BTCUSDT-1h-research-v1 (2061 finestre); CP02.1–CP02.3 PASS in [S02-HANDOFF.yaml](S02-HANDOFF.yaml) | Verificato dal coordinatore il 2026-11-14 dopo la revisione indipendente [GLM-5.3 del 2026-11-14](REVIEW-INDEP-2026-11-14-glm.md): CP02.1 ripetuto con due build (2061 finestre, digest uguali ai valori fissati), CP02.2–3 verdi. Il rilievo LOW del 13-11 resta un suggerimento non bloccante |
| S03 | VERIFIED | S00A | Motore eventi, Strategy, Risk Engine e broker; CP03.1-3 in [S03-HANDOFF.yaml](S03-HANDOFF.yaml). Revisione indipendente GLM-5.3 ([REVIEW-INDEP-2026-11-13-glm.md](REVIEW-INDEP-2026-11-13-glm.md)), diverso sia dall'autore (GPT-6) sia da Claude: CP03.1–3 verdi, ledger CLI byte-identico tra due esecuzioni e al riferimento precedente al refactor S06 | Verificato dal coordinatore il 2026-11-13. La dipendenza S00A è VERIFIED dal 2026-11-14 |
| S04 | VERIFIED | S02, S03 | Strategy Studio (commit aa4f8e9): studio.py, studio_analytics.py, vista Streamlit e app autonoma; CP04.1–3 PASS in [S04-HANDOFF.yaml](S04-HANDOFF.yaml), 24 test nuovi. **Prova manuale dell'app completata dall'operatore il 2026-11-14** (app autonoma in venv, percorso CP04.1–3, nessun difetto segnalato; run e metriche non trascritti) | Verificato dal coordinatore il 2026-11-14 dopo la revisione indipendente [GLM-5.3 del 2026-11-14 su 6651afc](REVIEW-INDEP-2026-11-14-glm2.md): CP04.1–3 ripetuti e app autonoma avviata con percorso completo su dati reali; prova manuale dell'operatore complementare |
| S05 | VERIFIED | S02, S03, S04 | Esperimento preregistrato S05-momentum-filter-v1 (spec sha ba6754d9…) su BTCUSDT-1h-research-v1; esito **REJECTED** (0/3 fold migliorati, AUC fuori campione 0,47). CP05.1–3 PASS in [S05-HANDOFF.yaml](S05-HANDOFF.yaml), 20 test nuovi | Verificato dal coordinatore il 2026-11-14 dopo la revisione indipendente [GLM-5.3 del 2026-11-14](REVIEW-INDEP-2026-11-14-glm.md): esperimento rigenerato, esito REJECTED, `spec_sha256` identico, 421/421 valori dei fold identici. Aperti F8 LOW e R11 INFO (`model_sha256` dipende dalla versione di scikit-learn), senza effetto sull'esito |
| S06 | VERIFIED | S03, S04 | Sessioni SQLite transazionali, Engine a passi condiviso con il backtest, feed di laboratorio riproducibile (config/lab-feeds), scheda Paper trading; CP06.1–3 PASS in [S06-HANDOFF.yaml](S06-HANDOFF.yaml). Revisioni indipendenti Codex GPT-6 e GLM-5.3: CP06.1–3 e correzione F4 confermati | Verificato dal coordinatore il 2026-11-13. La dipendenza S04 resta IN_REVIEW (prova manuale dell'app) |
| S07 | IN_REVIEW | S06 | Binance Spot, collaudo su Spot Testnet (mainnet bloccata fino a S08). Contratto di esecuzione, adattatore, runner live con ordini write-ahead e riconciliazione, guardie, runbook in [docs/S07-live-adapter.md](docs/S07-live-adapter.md). CP07.1–3 PASS su fixture in [S07-HANDOFF.yaml](S07-HANDOFF.yaml), 29 test nuovi, 458 test verdi (diff non committato su 57cff95) | Correzioni R1–R3 confermate da DeepSeek v4 Pro; R8 da confermare. L'operatore esegue `scripts/s07_testnet_smoke.py` con chiavi testnet, anche per R6 (paginazione `myTrades`) |
| S08 | VERIFIED | S01, S05, S07 | Criteri preregistrati, pacchetto in venv pulito, drill del runbook 15/15 PASS, gate mainnet su approvazione. Candidata momentum: scheda **BLOCKED** (backtest -96%, paper -91% dopo i costi; requisiti operativi aperti) e decisione **NOT_ACTIVATED**, operatività in paper/testnet. Evidenze in [releases/btcusdt-momentum-v1/fd1153cd](releases/btcusdt-momentum-v1/fd1153cd/release-card.md) e [S08-HANDOFF.yaml](S08-HANDOFF.yaml), 10 test nuovi, 468 test verdi. Il 2026-11-14 è stato corretto il rilievo emerso dopo la revisione GLM: `verify_activation` accettava una scheda scritta a mano che conteneva solo `decision: READY`. Ora `validate_card` lega la scheda a `criteria.json` controllando digest, configurazione, destinazione, insieme completo dei controlli con soglie registrate e decisione coerente; lo usano `approve`, `verify_activation` e `record_decision`. 6 test nuovi, suite 530 verde | Verificato dal coordinatore il 2026-11-14 dopo la revisione indipendente [GLM-5.3 del 2026-11-14 su 6651afc](REVIEW-INDEP-2026-11-14-glm2.md): drill dal wheel 15/15, scheda BLOCKED riprodotta con ledger identici, gate mainnet respinge 26/26 attacchi; limite dichiarato: valori osservati non rimisurati. La dipendenza S07 resta IN_REVIEW (collaudo testnet): requisito live aperto in `decisions.jsonl`. Una nuova scheda serve solo con una candidata da E01. Aperti R12 LOW (setuptools per `release package`), R13, R15 INFO |
| S09 | VERIFIED | S08 | `ops_report`: collega versione, decisioni, ordini ed esecuzioni e li confronta con l'ipotesi di simulazione (costi, slippage, latenza, fill mancanti, segnali, esito), con riconciliazione e hash del contenuto. Live non attivo (S08 NOT_ACTIVATED confermato dall'operatore il 2026-11-13): collaudo su fixture live etichettata, drill S08 e paper S08 in [reports/s09](reports/s09). CP09.1–2 PASS in [S09-HANDOFF.yaml](S09-HANDOFF.yaml), 6 test nuovi, 474 test verdi. Revisioni indipendenti Codex GPT-6 e GLM-5.3: `content_sha256` identico rigenerando dal registro drill-c | Verificato dal coordinatore il 2026-11-13, valido per fixture e paper (nessuna esecuzione live reale). F5 LOW aperto. Poi E01 |
| E01 | IN_REVIEW | S09 | **Esito REJECTED** (2026-11-14, [E01-HANDOFF.yaml](E01-HANDOFF.yaml), [reports/e01](reports/e01/report.json)). Selezionata sullo sviluppo g11 (soglia 0,03, orizzonte 48 h): sviluppo -29,6% contro -95,4% della baseline; validazione -20,3% netto con lordo +36,1 e costi 137,5 (criteri rendimento > 0 e costi <= 50% del lordo falliti; turnover 14,6% della baseline, drawdown -24,3% e 206 trade superati); conferma -28,2%, drawdown -32,4%. Turnover ridotto di circa 7 volte ma vantaggio lordo troppo piccolo per i costi; quasi tutte le uscite sono del segnale, l'orizzonte non vincola. Due esecuzioni con 17 `ledger_sha256` identici; runner [turnover_experiment.py](src/crypto_decision_lab/turnover_experiment.py) committato prima dei dati reali (94cd1d7), selezione committata prima della validazione (ca74636). Esperimento preregistrato [S09-E01-turnover-reduction-v1](config/experiments/S09-E01-turnover-reduction-v1.json) (sha256 d5527a6a…): ipotesi `H-turnover` da S08 (−96% netto contro +40,5% lordo), griglia soglia × orizzonte, selezione solo sullo sviluppo, validazione e conferma una volta, baseline btcusdt-momentum-v1 | Un revisore non Claude ripete CP-E01.1–2 da clone pulito. Nuova ipotesi (isteresi o filtro di regime) solo con una specifica nuova preregistrata |
| E02 | VERIFIED | S03, S09 | **Esito REJECTED** (2026-11-14, [E02-HANDOFF.yaml](E02-HANDOFF.yaml)). Selezionata g05 (pivot_bars 5, rr 1,5, rottura richiesta): sviluppo -35,9%, validazione +1,4% netto con 55 trade ma costi pari all'86% del lordo (criterio <= 50% fallito), conferma -6,3%. Turnover circa 20 volte inferiore a momentum, ma vantaggio lordo quasi nullo: la maggior parte delle uscite è per invalidazione. Due esecuzioni con 16 `ledger_sha256` identici. Esperimento preregistrato il 2026-11-14 dal coordinatore: [E02-pattern-formation-v1](config/experiments/E02-pattern-formation-v1.json) (sha256 LF `b0b757a02dd14d12d26f36e5daf726070eb2f3e71aec9a3bb8ff8844ce0900d4`). Strategia dinamica su pattern rialzisti in formazione (doppio minimo, triangolo ascendente, bandiera, range): detector a regole causale e `PatternStrategy` in [pattern_detector.py](src/crypto_decision_lab/pattern_detector.py), registrata come `pattern` in `simulation.STRATEGIES` (cambia l'`engine_sha256`); runner [pattern_formation_experiment.py](src/crypto_decision_lab/pattern_formation_experiment.py), guida in [docs/E02-pattern-formation.md](docs/E02-pattern-formation.md). 35 test su dati sintetici; nessuna esecuzione su dati reali prima della registrazione | Verificato dal coordinatore il 2026-11-14 dopo la revisione indipendente [GLM-5.3 del 2026-11-14 su 6651afc](REVIEW-INDEP-2026-11-14-glm2.md): 35 test, due esecuzioni con 16 `ledger_sha256` identici, REJECTED, detector causale. Nuova ipotesi solo con una specifica v2 preregistrata |
| E03 | IN_REVIEW | E01 | **Esito REJECTED** (2026-11-15, [E03-HANDOFF.yaml](E03-HANDOFF.yaml), [reports/e03](reports/e03/report.json)). Selezionata sullo sviluppo g09 (ingresso 0,05, uscita 0, SMA 720 h): sviluppo +4,1% (unica positiva su 12); validazione -4,65% con lordo +7,1 e costi 30,4 (criteri rendimento > 0 e costi <= 50% del lordo falliti; drawdown -10,2%, 41 trade e turnover 2,9% della baseline superati); conferma +0,6%. Rischio molto più basso di E01, ma in mercato solo il 3-8% del tempo: con BTC +103% in validazione il lordo resta minimo. Due esecuzioni con 23 `ledger_sha256` identici; specifica preregistrata (4476553) e selezione committata (afc21f4) prima della validazione. Esperimento preregistrato dal coordinatore il 2026-11-15: [E03-regime-momentum-v1](config/experiments/E03-regime-momentum-v1.json) (sha256 LF `41ddad98e6ee209a3b458bb4262c56910fb722b6f85838ea5ecec5be0af65b92`): momentum con isteresi (ingresso sopra `entry_threshold`, uscita solo sotto `exit_threshold`) e filtro di regime (`trend_ratio` = close/SMA_n − 1 > 0), griglia 3 × 2 × 2 = 12, stesse finestre e stessi criteri di E01, baseline momentum_v1, E01 g11 e buy and hold. Strategia [regime_momentum.py](src/crypto_decision_lab/regime_momentum.py) registrata come `regime_momentum` (cambia l'`engine_sha256`), runner a due fasi [regime_experiment.py](src/crypto_decision_lab/regime_experiment.py), guida [docs/E03-regime-momentum.md](docs/E03-regime-momentum.md); 9 test su dati sintetici, nessuna esecuzione su dati reali. Minaccia dichiarata: finestre già viste da S08, E01, E02 e griglia orientata dai risultati di E01; un CANDIDATE porta solo a una sessione paper su testnet di almeno 8 settimane, non a S08 | Un revisore non Claude ripete CP-E03.0–2. Nessuna nuova variante di momentum orario sullo stesso snapshot senza un'ipotesi diversa |
| E04 | IN_REVIEW | E03 | **Esito REJECTED** (2026-11-15, [E04-HANDOFF.yaml](E04-HANDOFF.yaml), [reports/e04](reports/e04/report.json)). Selezionata sullo sviluppo g05 (entry_z 2,0, finestra 24 h, orizzonte 120 h): sviluppo -6,3% (tutte le 12 negative); validazione -7,2% con lordo +61,4 e costi 97,5 (criteri rendimento > 0 e costi <= 50% del lordo falliti; drawdown -13,4%, 136 trade e turnover 9,6% della baseline superati); conferma -19,6%. Il prezzo torna alla media nel 59-62% dei trade, ma il lordo vale 0,39-0,45 per trade contro circa 0,72 di costo. Due esecuzioni con 20 `ledger_sha256` identici; specifica preregistrata (f11dfe3) e selezione committata (b32db6a) prima della validazione. Esperimento preregistrato dal coordinatore il 2026-11-15: [E04-mean-reversion-v1](config/experiments/E04-mean-reversion-v1.json) (sha256 LF `9aecd5c0a95c4cd358b970baeee759299712a783017646cb1ebcc6d7eb792ddc`): altra famiglia di segnale dopo i REJECTED di momentum (E01, E03) e pattern (E02). Mean reversion long-only in trend rialzista: ingresso quando lo z-score del close attraversa `-entry_z` con close sopra la media a 720 h, uscita al ritorno alla media o a orizzonte. Griglia 3 × 2 × 2 = 12, stesse finestre e stessi criteri di E01, baseline momentum_v1 e buy and hold. Strategia [mean_reversion.py](src/crypto_decision_lab/mean_reversion.py) registrata come `mean_reversion` (cambia l'`engine_sha256`); protocollo a due fasi condiviso [experiment_protocol.py](src/crypto_decision_lab/experiment_protocol.py) e runner [reversion_experiment.py](src/crypto_decision_lab/reversion_experiment.py); guida [docs/E04-mean-reversion.md](docs/E04-mean-reversion.md); 10 test su dati sintetici, nessuna esecuzione su dati reali. Minacce dichiarate: quarto esperimento sullo stesso snapshot; payoff asimmetrico senza stop sul prezzo; un CANDIDATE porta solo a una sessione paper su testnet | Un revisore non Claude ripete CP-E04.0–2. Prima di un E05 il coordinatore sceglie la direzione: orizzonte più lungo, costi più bassi o altro mercato |
| E05 | IN_REVIEW | E04 | **Esito CANDIDATE** (2026-11-15, [E05-HANDOFF.yaml](E05-HANDOFF.yaml), [reports/e05](reports/e05/report.json)): non è un rilascio. Selezionata sullo sviluppo g10 (rendimento a 28 giorni, media a 100 giorni, soglia 0): sviluppo +28,2% (8 positive su 12); validazione +70,9%, drawdown -13,1%, 17 trade, lordo 21,9 per trade, 5 criteri su 5 (buy and hold +103,2% con drawdown -22,8%); conferma +5,0%, drawdown -9,3%, 17 trade, 2 criteri su 2 (buy and hold -5,4%). Fragilità: in conferma il netto dipende da un solo trade (senza il migliore -23,9); 16–17 trade per finestra; quinto esperimento sullo stesso snapshot. Controllo indipendente su 102 ordini: tutti alle 00:00, feature ricalcolate identiche, nessun dato futuro. Due esecuzioni con 20 `ledger_sha256` identici; specifica preregistrata (3b7a42c) e selezione committata (32d137b) prima della validazione. Esperimento preregistrato dal coordinatore il 2026-11-15, minimi di trade 12 e 8 approvati: [E05-daily-trend-v1](config/experiments/E05-daily-trend-v1.json) (sha256 LF `7054becb66c96ea0bebd77d6815ef407834bcf8020a62680a5c514a0a9ffce83`): dopo quattro REJECTED su decisioni orarie (lordo per trade 0,2–0,9 contro circa 0,75 di costo), stesso costo e orizzonte più lungo. Una decisione al giorno alle 00:00 UTC su chiusure giornaliere: LONG se il rendimento a n giorni supera la soglia e il close è sopra la media a m giorni, uscita se il rendimento diventa negativo o il close scende sotto la media. Griglia 3 × 2 × 2 = 12, stesse finestre, criteri di E01 con minimi di trade ridotti (12 in selezione, 8 in validazione). Strategia [daily_trend.py](src/crypto_decision_lab/daily_trend.py) registrata come `daily_trend` (cambia l'`engine_sha256`), runner [daily_experiment.py](src/crypto_decision_lab/daily_experiment.py) sul protocollo condiviso (ora con minimi di trade per esperimento), guida [docs/E05-daily-trend.md](docs/E05-daily-trend.md); 11 test su dati sintetici, nessuna esecuzione su dati reali. Minacce dichiarate: pochi trade, validazione nota come favorevole al long (buy and hold +103%), quinto esperimento sullo stesso snapshot | Un revisore non Claude ripete CP-E05.0–2. Poi CP-E05.3: specifica preregistrata di una sessione paper su testnet di almeno 12 settimane (servono le chiavi dell'operatore e il collaudo S07). Nessun passo S08 prima |


Review del 2026-11-13 sugli step IN_REVIEW: [REVIEW-2026-11-13.md](REVIEW-2026-11-13.md).
- **Prove:** clone pulito di 57cff95 con 429 test verdi; build S02, CLI S03, esperimento S05, evidenze S08 e report S09 riprodotti.
- **Modifiche richieste:** S06 (F4) e S07 (F1 HIGH, F2, F3), corrette lo stesso giorno con 6 test di regressione; un revisore diverso deve confermarle.
- **Indipendenza:** per S04–S09 la review non è indipendente, perché revisore e autore sono lo stesso modello.
- **Stati:** invariati, la decisione resta al coordinatore.

Revisioni indipendenti del 2026-11-13 su `53f15ef`: [Codex GPT-6](REVIEW-INDEP-2026-11-13.md) e [GLM-5.3](REVIEW-INDEP-2026-11-13-glm.md).
- **Esito:** 480 test verdi su clone pulito; F3, F4 confermate; F1, F2 insufficienti.
- **Rilievi nuovi:** R1 HIGH (cambio codice senza nuova approvazione S08), R3 HIGH (ordine dopo un `session-finish` concorrente), R2 MEDIUM (fill incompleti persi). Corretti lo stesso giorno da Claude Opus 5.5 con 8 test di regressione: serve un nuovo revisore non Claude per S07 e S08.
- **Stati:** S03, S06 e S09 VERIFIED per decisione del coordinatore; gli altri restano IN_REVIEW.
- **Terza revisione indipendente** [DeepSeek v4 Pro](REVIEW-INDEP-2026-11-13-deepseek.md), sulle correzioni R1–R3: 488 test verdi, R1–R3 e F1–F4 confermate. Rilievi nuovi: R6 LOW (ordine dei trade nella paginazione di `myTrades`, da confermare sul testnet), R7 INFO (lock solo sulla stessa macchina, documentato), R8 LOW (trade oltre `executedQty`, corretto con lo stato `FILLS_EXCESS`; la correzione va confermata da un revisore non Claude).

Revisione indipendente del 2026-11-14 su `b212033`: [GLM-5.3 del 2026-11-14](REVIEW-INDEP-2026-11-14-glm.md).
- **Esito:** clone pulito in venv isolato, 488 test verdi e 1 fallito (A2, streamlit 1.67); 51/51 prove proprie; R1–R3, R8 e F1–F3 confermate; A1, A3, A4, A6, F7 e F9 chiusi.
- **Rilievi nuovi:** R9 INFO (i comandi CLI non gestiscono `SessionBusy`), R10 INFO (BUY `FILLS_INCOMPLETE` senza fill non avvia l'orizzonte), R11 INFO (`model_sha256` S05 dipende dalla versione di scikit-learn).
- **Stati:** S00A, S02 e S05 VERIFIED per decisione del coordinatore il 2026-11-14; S01, S04, S07 e S08 restano IN_REVIEW.

Revisione indipendente del 2026-11-14 su `6651afc`: [GLM-5.3, seconda sessione](REVIEW-INDEP-2026-11-14-glm2.md).
- **Esito:** clone pulito in venv nuovo, 544 test verdi; correzioni A2, R9, R10, F5 e `validate_card` confermate (test falliscono senza, passano con); gate mainnet respinge 26/26 attacchi; due esecuzioni di E02 con 16 ledger identici.
- **Rilievi nuovi:** R12 LOW (`release package` richiede `setuptools` nell'ambiente), R13, R14, R15 INFO. Nessun HIGH o MEDIUM.
- **Stati:** S01, S04, S08 ed E02 VERIFIED per decisione del coordinatore il 2026-11-14; S07 resta IN_REVIEW (collaudo testnet con le chiavi dell'operatore). Lo stesso modello aveva firmato la review precedente in un'altra sessione: la scelta di considerarla indipendente (PRD §5) è del coordinatore.
- **Correzioni:** R12 chiuso il 2026-11-14 (`setuptools` e `wheel` nell'extra `dev`, messaggio d'errore esplicito in `release package`) e R15 (parametro inutilizzato di `_live_orders` rimosso); R13 e R14 restano INFO documentati.

Correzioni del 2026-11-14 ai rilievi A2, R9, R10 e F5 (branch `fix/review-findings-a2-r9-r10-f5`, suite verde): A2 `st.rerun()` dopo ogni azione della scheda Paper trading; R9 i comandi `live` rispondono a una sessione occupata con una riga JSON `{"status":"BUSY"}` e codice di uscita 75; R10 un BUY eseguito con trade non importati (`FILLS_INCOMPLETE`) avvia l'orizzonte di detenzione; F5 il `fill_ratio` degli ordini MARKET a importo in quote si calcola sull'importo speso. Un revisore non Claude conferma le correzioni; i report già committati in `reports/s09` precedono F5 e non sono rigenerati.

Stati operativi: `TODO`, `READY`, `IN_PROGRESS`, `IN_REVIEW`, `VERIFIED`, `BLOCKED`. `DONE` nella tabella identifica l'analisi storica già svolta.

### Scheda di handoff da compilare a ogni cambio agente o consegna

```yaml
step_id: S00
status: IN_REVIEW
updated_at: <timestamp ISO 8601>
agent_role: <ruolo>
agent_model: <modello/versione se nota>
base_commit: <SHA>
head_commit: <SHA o riferimento al diff>
review_ref: <PR o riferimento alla revisione>
inputs:
  - <ID e riferimenti alle evidenze delle dipendenze>
changed_paths:
  - <percorso e scopo>
checkpoints:
  - id: CP00.1
    result: <PASS | FAIL | BLOCKED | NOT_RUN>
    verification: <comando o procedura ripetibile>
    expected: <risultato atteso>
    observed: <risultato effettivo>
    evidence: <link o percorso con SHA se applicabile>
decisions:
  - <decisione, motivazione e impatto>
blockers:
  - <blocco, azione necessaria e chi la può sbloccare>
next_action: <singola azione concreta per l'agente successivo>
```

## 8. Criterio di chiusura della roadmap

La piattaforma è consegnabile quando un operatore può seguire da Strategy Studio un dataset fino al backtest e al paper, un revisore può riprodurre le evidenze da un commit e l'adattatore live supera le prove di esecuzione e recupero. Il rilascio riporta l'esito dell'attivazione reale, l'eventuale evidenza live ancora pendente e il primo esperimento del ciclo successivo. La capacità di trading reale è quindi verificabile anche quando l'attivazione resta sospesa per decisione operativa o perché nessuna candidata soddisfa i criteri dichiarati.
