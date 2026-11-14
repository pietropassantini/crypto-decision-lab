# S03 - Motore di simulazione

Avvio dalla radice (dipendenze base del progetto):

```powershell
python -m crypto_decision_lab.simulation --source BTCUSDT-1h-raw-v1 --config config/simulation.example.json --output-dir var/my-backtest
python -m crypto_decision_lab.simulation --source BTCUSDT-1h-raw-v1 --config config/simulation.example.json --mode paper-replay --output-dir var/my-replay
```

Le directory devono essere nuove. Il catalogo S02 ammette e verifica gli input prima del caricamento; fonti forward e copie identificate dal catalogo sono respinte. Output: `manifest.json` (configurazione, digest input/config, hash del motore e del manifest sorgente) e `ledger.json` canonico. La modalita compare nel manifest, non nel registro economico. `engine_sha256` identifica la revisione del motore su `simulation.py`, `execution_contract.py` e `decision_contract.py` con fine riga normalizzati a LF (stesso valore su checkout Windows e Linux); `engine_files` riporta il digest di ciascun modulo. Un run senza eventi e rifiutato.

API: `run(events, RunConfig(...), mode=...)`. Gli eventi in memoria sono un confine fidato per fixture o adattatori che abbiano gia verificato la provenienza. `MarketEvent` espone open al timestamp e feature disponibili entro quel cutoff. Lo Strategy Protocol riceve solo cutoff, feature e portafoglio; non riceve prezzi futuri o target. Registrare una factory in `STRATEGIES` per estendere la selezione. Le strategie incluse sono `constant` LONG/FLAT e `momentum` (valori frazionari: 0.05 = 5%). Il CLI calcola momentum sulle chiusure precedenti.

Il Risk Engine autorizza LONG/FLAT e dimensiona la spesa, comprensiva della commissione, entro `max_exposure` del capitale disponibile. Nessuna leva, short o piramidazione. Gli ordini pendenti restano fissati fino al fill; nuovi segnali sono registrati ma non li sostituiscono. `allow_open=false` blocca aperture e permette uscite.

Il clock avanza con gli eventi orari. Ordine eleggibile a decisione+latenza; fill all'open di quell'evento con slippage sfavorevole e fee sul nozionale. Uscita programmata a fill+orizzonte; non aggiunge una seconda latenza. Prevale su una chiusura strategica ancora pendente e impedisce riaperture nello stesso evento. Le chiusure strategiche anticipate applicano la latenza configurata.

Differenze riproducibili da CP07: solo LONG/FLAT spot; CP07 comprende SHORT e decisioni su finestre gia risolte. Qui saldi e fee sono contabilizzati a ogni fill; una posizione terminale resta aperta e valorizzata all'ultimo open, gli ordini residui sono cancellati. Non viene inventata una chiusura. Un gap arresta il run con `HALTED_GAP`, senza usare il prezzo oltre il gap. Duplicati identici sono idempotenti; duplicati conflittuali e disordine temporale sono errori. A parita di ingresso/uscita LONG, costi e tempi, il rendimento coincide con `execution_contract.resolve_round_trip` e quindi con CP07. Il test parametrico copre latenze 0/1/2 e tre modelli di costo; il fixture a latenza 2 entra a 90 ed esce a 130, mentre a latenza 0 entra a 100 ed esce a 90.

Checkpoint ripetibili:

```powershell
python -m pytest tests/test_simulation.py tests/test_execution_contract.py tests/test_dataset_builder.py tests/test_trading_simulator.py -q --basetemp var/s03-verification -p no:cacheprovider
```

CP03.1: strategie selezionate da config, apertura/chiusura, rifiuto e motivi.
CP03.2: uguaglianza esatta del registro canonico e saldi backtest/replay.
CP03.3: costi, latenza, duplicati, gap, posizione finale e prezzi eleggibili; riconciliazione S02/CP07.

Non sono incluse persistenza e ripresa (S06) o UI (S04). La dipendenza S00A resta IN_REVIEW nel backlog.
