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
| S00 | VERIFIED | A00 | Baseline storica disponibile | main=430P/1S/0F, cp08-07=297P/1F, cp09=297P/0F. SHA main=0daf402 scelto come baseline. [S00-BASELINE-REPORT.md](S00-BASELINE-REPORT.md) |
| S00A | IN_REVIEW | S00 | Commit 6414abd (sanificazione, -385 file). Handoff dedicato da produrre | Completare handoff S00A e verificare CP00A.1–4 |
| S01 | IN_PROGRESS | S00A | Modifiche forward_status/forward_adapter nell'albero di lavoro (1 test fallente pre-esistente) | Completare CP01.1–3 e registrare handoff |
| S02 | IN_REVIEW | S00A | Commit 62c38c9 su feat/s02-dataset-builder: execution_contract, dataset_catalog, dataset_builder; dataset BTCUSDT-1h-research-v1 (2061 finestre); CP02.1–CP02.3 PASS in [S02-HANDOFF.yaml](S02-HANDOFF.yaml) | Revisione CP02.3 congiunta con S03 sul contratto condiviso |
| S03 | TODO | S00A | Simulatore CP07 | Definire eventi e fixture comune |
| S04 | TODO | S02, S03 | Viste Streamlit esistenti | Collegare configurazione, runner e risultati |
| S05 | TODO | S02, S03, S04 | Dataset CP09 come base | Registrare l'esperimento fuori campione |
| S06 | TODO | S03, S04 | Feed di laboratorio da identificare | Implementare checkpoint transazionale |
| S07 | TODO | S06 | Exchange e ambiente di collaudo da scegliere | Definire contratto di riconciliazione |
| S08 | TODO | S01, S05, S07 | Decisione operativa da registrare | Preparare scheda di rilascio |
| S09 | TODO | S08 | Report da collaudare | Aprire la prima ipotesi successiva |

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
