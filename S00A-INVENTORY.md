# S00A — Inventario della sanificazione (CP00A.1)

Commit analizzato: `6414abd` (386 file: 385 rimossi dal tracking, 1 modificato `.gitignore`). Baseline: `0daf402`.
Verifica dei riferimenti: `git grep -F <percorso o nome>` su HEAD (`f93e4e8`), escludendo handoff, report S00 e `.gitignore`. Nessun file è stato cancellato dal disco di lavoro: sono solo usciti dal tracking.
**Recupero di qualsiasi elemento rimosso:** `git show 0daf402:<percorso>` (la cronologia Git è intatta).

| # | Gruppo | File | Utilizzo noto (riferimenti trovati) | Decisione | Evidenza |
|---|---|---|---|---|---|
| 1 | `data/chunks/BTCUSDT/1h/` | 128 (64 coppie parquet+json) | Fissati per SHA-256 da `raw-manifest.json`; letti da `dashboard/data_loader.py`, test su archivio reale, README | **RIPRISTINATO** (`775c57f`) | clone pulito: 342 passati |
| 2 | `data/quarantine/BTCUSDT/1h/` | 12 (6 coppie) | Fissati da `raw-manifest.json` (6 dei 70 chunk) | **RIPRISTINATO** (`775c57f`) | idem |
| 3 | `data/snapshots/BTCUSDT-1h-raw-v1/` `report.json`, `forecast-windows.parquet` | 2 (93 KB la cartella) | **Lettura hard-coded**: `forecast_batch.py:46-47`, `forecast_expansion.py:83,94`, `forecast_smoke.py:20`, `decision_labeling.py:135`, `forecast_consistency_audit.py:98`, `dashboard/forecast_adapter.py:545`; lettura opzionale in `dashboard/data_loader.py:132`; README righe 147-148 | **APERTO, proposta: RIPRISTINARE.** Non rigenerabili sul posto: `cdl freeze` rifiuta una cartella di output esistente (README riga 150). I test passano in clone pulito solo perché usano fixture | `git grep` sopra |
| 4 | `data/experiments/S02-CP02-batch-100/` | 202 (1,3 MB) | Output Timestral. Percorso di default in `forecast_batch.py:140`, `dashboard/config.py:115`, `config/dashboard.example.yaml:10`, README riga 181, docs/S03-CP01-report.md | **RIMOSSO**, rigenerabile solo con extra `forecast` (torch + timestral). `forecast_adapter.py:204` controlla `is_dir()`; il comportamento della UI a cartella assente **non è stato verificato** | `git grep` |
| 5 | `data/experiments/S02-CP02-pilot-5/`, `S02-CP01-window-0000..0003/` | 12 + 8 | Output Timestral di prova; default in `forecast_smoke.py:115`, README righe 165, 179 | **RIMOSSO**, rigenerabile con i comandi README (extra `forecast`) | `git grep` |
| 6 | `data/experiments/S02-CP05-residual-baselines-v1/` | 2 | Predizioni Ridge/HistGB. `dashboard/config.py:142`, `config/dashboard.example.yaml:13`, README-CP05, README-DASHBOARD, docs | **RIMOSSO**, dipende dal punto 4 per essere rigenerato | `git grep` |
| 7 | `data/diagnostics/S02-CP03-batch-100-v1/` | 3 | Diagnostica opzionale (README-DASHBOARD riga 58); default in `forecast_diagnostics.py:118`, `dashboard/config.py:150` | **RIMOSSO**, dipende dal punto 4 | `git grep` |
| 8 | `data/manifests/BTCUSDT_1h_1609459200000_1792771200000.json` | 1 | Citato da README righe 82, 138 (comando `freeze --manifest`); `raw-manifest.json:4` lo cita con **percorso assoluto** `/home/sirio/...` | **RIMOSSO**, rigenerabile con `cdl ingest`. Il percorso assoluto non è portabile (anomalia preesistente, non toccata) | `git grep` |
| 9 | `data/reports/BTCUSDT_1h_2024-01-0*.json` | 3 | Solo esempi di output nel README (riga 47). L'evidenza CP08-05R `data/reports/S02-CP08-05R-protocol-integrity-evidence.json` **è rimasta tracciata** | **RIMOSSO**, nessuna dipendenza di codice | `git grep` |
| 10 | `docs/screenshots/*.png` | 12 (1,1 MB) | Elencati in README-DASHBOARD righe 250-261; prodotti da `scripts/render_screenshots.py` (tracciato, sola scrittura su `docs/screenshots/`) | **RIMOSSO**, rigenerabile con lo script. Script **non eseguito** in questa verifica | `scripts/render_screenshots.py:5-7` |
| 11 | `.gitignore` | 1 (modificato) | Regole di esclusione; estese poi da `775c57f` e `ad5f09b` | **MANTENUTO** | `git show 6414abd -- .gitignore` |

Verifiche incluse: `deploy/` (solo unit systemd forward, nessun riferimento ai percorsi rimossi); `scripts/` (solo `render_screenshots.py`); `config/` (`dashboard.example.yaml` cita i percorsi dei punti 4, 6, 7 come default modificabili).

## Esito
- Ogni gruppo ha riferimenti verificati e una decisione. Il punto 3 ha una decisione **proposta ma non ancora confermata**.
- I punti 4-7 lasciano la vista Forecast/Timestral non utilizzabile da un clone pulito senza rigenerare i dati. Il test suite non lo copre.

## Differenza 342 vs 430 test
Il numero 430 nel baseline S00 **non è riproducibile per `main`**. Raccolta (`pytest --collect-only tests`), eseguita nei worktree dei tre commit:

| Revisione | Test raccolti | Esito osservato | Riportato in S00 |
|---|---|---|---|
| `main` `0daf402` | 288 | con dati locali: 287 passati, 1 fallito (sidecar v2) | 430 passati, 1 saltato, 0 falliti |
| `cp08-07` `2272d49` | 298 | non eseguito | 297 passati, 1 fallito |
| `cp09` `7109e90` | 431 | non eseguito | 297 passati, 0 falliti |

- 431 raccolti su `cp09` corrisponde a "430 passati + 1 saltato": i numeri di `main` e `cp09` nella tabella S00 sembrano **scambiati o attribuiti al branch sbagliato**. È un'inferenza dai conteggi; i test di `cp09` non sono stati eseguiti.
- Il fallimento di `main` (287P/1F) è lo stesso già corretto in `4fa9f25`.
- Il branch attuale: 342 = 288 (main) + 6 (S01) + 48 (S02: 22+11+15). Nessun test di `main` è stato rimosso: tutti gli ID di `main` sono presenti.
- **Nessuna regressione di test** da S00A. S00 (`VERIFIED`) contiene però un dato errato: va ricontrollato da un revisore.
