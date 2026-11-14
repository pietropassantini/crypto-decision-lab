# Scheda di rilascio btcusdt-momentum-v1-fd1153cd

**Decisione: BLOCKED** · creata 2026-11-13T17:23:46Z

## Versione

- Commit `57cff955dcb3b81371b658e1a1564bd63c74ff09`; percorsi non committati: 6
- Pacchetto 0.2.0, wheel `crypto_decision_lab-0.2.0-py3-none-any.whl` sha256 `0c99810f4875679d…`
- Codice (motore + live) sha256 `fd1153cd96c397d30f3d43feae86e26f5a1088cb3b68a05cf827a08cc2053e2e`
- Criteri `releases\btcusdt-momentum-v1\criteria.json` sha256 `c32dab77553cf988…`, registrati 2026-11-13T17:05:48Z

## Destinazione e limiti

- binance-spot mainnet BTCUSDT
- Capitale 500; limiti {"balance_abs_tolerance": 1e-08, "balance_rel_tolerance": 1e-06, "max_order_notional": 100, "max_position_notional": 200, "max_signal_age_hours": 1, "order_timeout_seconds": 300, "stale_after_hours": 2, "unknown_grace_seconds": 60}

## Evidenze

- **backtest** 2021-02-01 00:00 → 2025-01-01 00:00: rendimento netto -96.01%, drawdown -96.43%, trade chiusi 2214, costi 534.81, ledger `8450c9d155e3`
- **paper** 2025-01-01 00:00 → 2026-10-01 00:00: rendimento netto -91.09%, drawdown -91.13%, trade chiusi 1556, costi 426.22, ledger `324dea26ddff`
- **testnet**: 0 esecuzioni, superato: False
- **drill CP08.1**: True

## Controlli

| Controllo | Osservato | Richiesto | Esito |
|---|---|---|---|
| `backtest.net_return` | -96.01 | > 0 | FAIL |
| `backtest.drawdown` | -96.43 | >= -40 | FAIL |
| `backtest.trades` | 2214 | >= 50 | PASS |
| `paper.net_return` | -91.09 | > 0 | FAIL |
| `paper.drawdown` | -91.13 | >= -25 | FAIL |
| `paper.trades` | 1556 | >= 20 | PASS |
| `backtest.completed` | COMPLETED | == COMPLETED | PASS |
| `backtest.window_covered` | 2024-12-31 23:00:00+00:00 | == 2024-12-31 23:00:00+00:00 | PASS |
| `paper.matches_backtest` | True | == True | PASS |
| `paper.completed` | COMPLETED | == COMPLETED | PASS |
| `ops.candidate_config_unchanged` | 838e1b3accd18fe30ffc5bc9c7611a75b5762de47e190a0d76a4a8fd6e2ebbc4 | == 838e1b3accd18fe30ffc5bc9c7611a75b5762de47e190a0d76a4a8fd6e2ebbc4 | PASS |
| `ops.capital` | 500 | <= 500 | PASS |
| `ops.max_order_notional` | 100 | <= 100 | PASS |
| `ops.max_position_notional` | 200 | <= 200 | PASS |
| `ops.git_commit` | True | == True | PASS |
| `ops.git_clean` | 6 | == 0 | FAIL |
| `ops.testnet_collaudo` | False | == True | FAIL |
| `ops.runbook_drill` | True | == True | PASS |
| `ops.step_S00A_verified` | IN_REVIEW | == VERIFIED | FAIL |
| `ops.step_S01_verified` | IN_REVIEW | == VERIFIED | FAIL |
| `ops.step_S02_verified` | IN_REVIEW | == VERIFIED | FAIL |
| `ops.step_S03_verified` | IN_REVIEW | == VERIFIED | FAIL |
| `ops.step_S04_verified` | IN_REVIEW | == VERIFIED | FAIL |
| `ops.step_S05_verified` | IN_REVIEW | == VERIFIED | FAIL |
| `ops.step_S06_verified` | IN_REVIEW | == VERIFIED | FAIL |
| `ops.step_S07_verified` | IN_REVIEW | == VERIFIED | FAIL |

## Motivi del blocco

- backtest.net_return: observed -96.00726168842135, required > 0
- backtest.drawdown: observed -96.43323967259205, required >= -40
- paper.net_return: observed -91.09484384541496, required > 0
- paper.drawdown: observed -91.13164024757234, required >= -25
- ops.git_clean: observed 6, required == 0
- ops.testnet_collaudo: observed False, required == True
- ops.step_S00A_verified: observed 'IN_REVIEW', required == 'VERIFIED'
- ops.step_S01_verified: observed 'IN_REVIEW', required == 'VERIFIED'
- ops.step_S02_verified: observed 'IN_REVIEW', required == 'VERIFIED'
- ops.step_S03_verified: observed 'IN_REVIEW', required == 'VERIFIED'
- ops.step_S04_verified: observed 'IN_REVIEW', required == 'VERIFIED'
- ops.step_S05_verified: observed 'IN_REVIEW', required == 'VERIFIED'
- ops.step_S06_verified: observed 'IN_REVIEW', required == 'VERIFIED'
- ops.step_S07_verified: observed 'IN_REVIEW', required == 'VERIFIED'
