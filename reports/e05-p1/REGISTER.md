# Registro ufficiale E05-P1

Tenuto dal coordinatore (procedure.official_register della specifica). Il software non sa quale sessione sia quella
ufficiale né quante ripartenze ci siano state: lo dice questo registro. Ogni rapporto stampa `spec_sha256`, che deve
coincidere con quello qui sotto prima di accettare qualunque esito.

## Preregistrazione

| Voce | Valore |
|---|---|
| Specifica | `config/experiments/E05-P1-paper-forward-v1.json` |
| Stato | `PREREGISTERED` |
| Data di preregistrazione | 2026-11-16 |
| sha256 della specifica (a-capo LF) | `fe830f818d9f04f612efa47a9e6355f5f9cb4a51b0c5d3badc4f733d68b390a2` |
| Commit del runner | `77ae4223ca0dc911ebea2c1e51268d1f9e45519b` |
| Candidata (config_sha256) | `7775ccd417e3354c981248cf1469f6cab447844638b88301756fb59c099c5536` |
| Deviazione | DEV-1 approvata il 2026-11-15 (klines pubbliche del mercato reale, esecuzione simulata) |
| Review | PRE-4: REVIEW-INDEP-2026-11-15-glm3.md; PRE-7: REVIEW-INDEP-2026-11-15-deepseek.md, REVIEW-INDEP-2026-11-15-codex.md, REVIEW-INDEP-2026-11-16-gpt-6-astra.md, REVIEW-INDEP-2026-11-16-glm-5.3.md |

Calcolo dello sha256 (stesso valore stampato dai rapporti):

```powershell
python -c "import hashlib,pathlib;print(hashlib.sha256(pathlib.Path(r'config/experiments/E05-P1-paper-forward-v1.json').read_bytes().replace(b'\r\n', b'\n')).hexdigest())"
```

## Sessione ufficiale

| Voce | Valore |
|---|---|
| Identificativo della sessione attiva | _da compilare all'avvio_ |
| Cartella e macchina (PRE-3) | _da compilare all'avvio_ |
| Ora di inizio (UTC) | _da compilare all'avvio_ |
| Settimana 12 (inizio + 12 settimane) | _da compilare all'avvio_ |
| Scadenza del vantaggio (inizio + 52 settimane, sul lunedì di trigger) | _da compilare all'avvio_ |
| Scadenza assoluta (78 settimane dall'inizio della prima sessione) | _da compilare all'avvio_ |

## Ripartenze (al massimo 2)

| # | Data | Sessione abbandonata | Motivo | Approvazione | Nuova sessione |
|---|---|---|---|---|---|

## Decisioni ed evidenze

| Data | Checkpoint | Esito | spec_sha256 del rapporto | File |
|---|---|---|---|---|
