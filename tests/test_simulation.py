import pandas as pd
import pytest
from crypto_decision_lab.simulation import MarketEvent, RunConfig, RiskLimits, run
from crypto_decision_lab.execution_contract import ExecutionHypothesis, resolve_round_trip


def events(prices=(100, 110, 120, 130), signals=(1, 1, 0, 0)):
    return [MarketEvent(t, p, {'momentum_24h': s}) for t, p, s in zip(
        pd.date_range('2024-01-01', periods=len(prices), freq='h', tz='UTC'), prices, signals)]


def kinds(result, kind):
    return [r for r in result.ledger if r['kind'] == kind]


def test_cp031_strategy_selection_open_close_reasons_and_risk():
    config = RunConfig(strategy={'name': 'momentum'})
    result = run(events(), config)
    assert [r['side'] for r in kinds(result, 'execution')] == ['BUY', 'SELL']
    assert all(r['reason'] for r in kinds(result, 'decision'))
    assert result.portfolio.quantity == 0
    flat = run(events(), RunConfig(strategy={'name': 'constant', 'parameters': {'exposure': 'FLAT'}}))
    assert not kinds(flat, 'execution')
    rejected = run(events(), RunConfig(risk=RiskLimits(allow_open=False)))
    assert not kinds(rejected, 'order')
    assert 'disabled' in kinds(rejected, 'risk')[0]['reason']


def test_cp032_backtest_paper_canonical_identity():
    config = RunConfig(hypothesis=ExecutionHypothesis(latency_hours=1))
    backtest = run(events(), config)
    replay = run(events(), config, mode='paper-replay')
    assert backtest.canonical_ledger() == replay.canonical_ledger()
    assert backtest.portfolio == replay.portfolio
    assert backtest.manifest['input_digest'] == replay.manifest['input_digest']


@pytest.mark.parametrize('latency', [0, 1, 2])
@pytest.mark.parametrize('fee,slippage', [(0, 0), (0.001, 0.0005), (0.002, 0.002)])
def test_cp033_reconcile_s02_cp07_costs_latency(latency, fee, slippage):
    fixture = events((100, 105, 90, 120, 130, 110), (1, 1, 1, 0, 0, 0))
    hypothesis = ExecutionHypothesis(latency_hours=latency, horizon_hours=2, fee_rate=fee, slippage=slippage)
    result = run(fixture, RunConfig(strategy={'name': 'momentum'}, hypothesis=hypothesis))
    market = pd.DataFrame({'open': [e.open for e in fixture]}, index=[e.timestamp for e in fixture])
    trip = resolve_round_trip(market, fixture[0].timestamp, hypothesis)
    fills = kinds(result, 'execution')
    assert fills[0]['price'] == trip.entry_exec_price
    assert fills[1]['price'] == trip.exit_exec_price
    assert fills[0]['timestamp'] == str(trip.entry_timestamp)
    assert fills[1]['timestamp'] == str(trip.exit_timestamp)
    sell_balance = next(r for r in kinds(result, 'balance') if r['timestamp'] == fills[1]['timestamp'])
    assert sell_balance['cash'] / 10000 - 1 == pytest.approx(trip.net_return_pct / 100)
    assert all(pd.Timestamp(r['timestamp']) >= pd.Timestamp(r['eligible_at']) for r in fills)


def test_duplicates_are_idempotent_conflicts_and_order_refused():
    fixture = events()
    assert run(fixture, RunConfig()).canonical_ledger() == run([fixture[0], *fixture], RunConfig()).canonical_ledger()
    with pytest.raises(ValueError, match='conflicting'):
        run([fixture[0], MarketEvent(fixture[0].timestamp, 999)], RunConfig())
    with pytest.raises(ValueError, match='out of order'):
        run([fixture[1], fixture[0]], RunConfig())


def test_gap_halts_without_using_later_price():
    fixture = events()
    result = run([fixture[0], fixture[2]], RunConfig(hypothesis=ExecutionHypothesis(latency_hours=1)))
    assert result.status == 'HALTED_GAP'
    assert not kinds(result, 'execution')
    assert kinds(result, 'order_cancelled')


def test_terminal_open_position_and_unfilled_order():
    result = run(events(), RunConfig())
    assert result.portfolio.quantity > 0
    assert kinds(result, 'end')[0]['position_open']
    delayed = run(events(), RunConfig(hypothesis=ExecutionHypothesis(latency_hours=9)))
    assert not kinds(delayed, 'execution')
    assert kinds(delayed, 'order_cancelled')


def test_risk_sizing_fees_and_balance():
    config = RunConfig(risk=RiskLimits(max_exposure=0.25))
    result = run(events(), config)
    assert result.portfolio.cash == pytest.approx(7500)
    fill = kinds(result, 'execution')[0]
    assert fill['quantity'] * fill['price'] + fill['fee'] == pytest.approx(2500)


def test_no_future_feature_or_invalid_price():
    fixture = events()
    with pytest.raises(ValueError, match='after cutoff'):
        run([MarketEvent(fixture[0].timestamp, 100, {}, fixture[1].timestamp)], RunConfig())
    with pytest.raises(ValueError, match='finite'):
        run([MarketEvent(fixture[0].timestamp, float('nan'))], RunConfig())
    with pytest.raises(ValueError, match='unknown strategy'):
        RunConfig(strategy={'name': 'absent'})


def test_review_missing_timestamps_are_rejected():
    for event in [MarketEvent(pd.NaT, 100), MarketEvent(events()[0].timestamp, 100, {}, pd.NaT)]:
        with pytest.raises(ValueError, match='missing'):
            run([event], RunConfig())


def test_review_minimum_is_actual_notional_without_fee():
    result = run(events(), RunConfig(initial_cash=1, risk=RiskLimits(min_notional=1)))
    assert not kinds(result, 'execution')


def test_review_gap_lifecycle_clock_does_not_go_backwards():
    fixture = events()
    result = run([fixture[0], fixture[2]], RunConfig(hypothesis=ExecutionHypothesis(latency_hours=1)))
    timestamps = [pd.Timestamp(r['timestamp']) for r in result.ledger]
    assert timestamps == sorted(timestamps)
    assert kinds(result, 'end')[0]['valuation_timestamp'] == str(fixture[0].timestamp)


def test_review_run_without_events_is_refused():
    with pytest.raises(ValueError, match='at least one'):
        run([], RunConfig())


def test_review_engine_identity_covers_contracts_and_ignores_line_endings(tmp_path, monkeypatch):
    import hashlib
    from pathlib import Path
    from crypto_decision_lab import simulation
    package = Path(simulation.__file__).resolve().parent
    combined, files = simulation.engine_digests()
    assert set(files) == {'simulation.py', 'execution_contract.py', 'decision_contract.py'}
    for name, digest in files.items():
        lf = (package / name).read_bytes().replace(b'\r\n', b'\n')
        assert digest == hashlib.sha256(lf).hexdigest()
    result = run(events(), RunConfig())
    assert result.manifest['engine_sha256'] == combined
    assert result.manifest['engine_files'] == files
    # A CRLF and an LF checkout of the same sources give the same identity.
    for variant, transform in (('lf', lambda b: b.replace(b'\r\n', b'\n')),
                               ('crlf', lambda b: b.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n'))):
        copy = tmp_path / variant
        copy.mkdir()
        for name in files:
            (copy / name).write_bytes(transform((package / name).read_bytes()))
    digests = []
    for variant in ('lf', 'crlf'):
        monkeypatch.setattr(simulation, '__file__', str(tmp_path / variant / 'simulation.py'))
        digests.append(simulation.engine_digests())
    assert digests[0] == digests[1] == (combined, files)
    # Changing an economic contract module changes the engine identity.
    contract = tmp_path / 'lf' / 'decision_contract.py'
    contract.write_bytes(contract.read_bytes() + b'\n# changed\n')
    monkeypatch.setattr(simulation, '__file__', str(tmp_path / 'lf' / 'simulation.py'))
    assert simulation.engine_digests()[0] != combined


def test_cli_pins_loaded_provenance_and_refuses_overwrite(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from crypto_decision_lab import simulation, dataset_catalog
    frame = pd.DataFrame({'open': [100., 101.], 'close': [100., 101.]}, index=[e.timestamp for e in events()[:2]])
    market = SimpleNamespace(frame=frame, source_manifest_sha256='loaded-hash', dataset_id='loaded-id')
    monkeypatch.setattr(dataset_catalog.Catalog, 'load', lambda path: object())
    monkeypatch.setattr(dataset_catalog, 'load_market', lambda *a, **kw: market)
    config = tmp_path / 'config.json'
    config.write_text('{}')
    output = tmp_path / 'run'
    argv = ['--source', 'fixture', '--config', str(config), '--output-dir', str(output)]
    assert simulation.main(argv) == 0
    manifest = json.loads((output / 'manifest.json').read_text())
    assert manifest['source_manifest_sha256'] == 'loaded-hash'
    assert manifest['loaded_dataset_id'] == 'loaded-id'
    with pytest.raises(FileExistsError):
        simulation.main(argv)
