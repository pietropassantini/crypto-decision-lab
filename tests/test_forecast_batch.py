import pytest
from crypto_decision_lab.forecast_batch import sample_indices, summarize


def test_stratified_sample():
    assert sample_indices(2053, 5) == [0, 513, 1026, 1539, 2052]
    assert len(set(sample_indices(2053, 100))) == 100


def test_selection_validation():
    with pytest.raises(ValueError):
        sample_indices(10, 11)
    with pytest.raises(ValueError):
        sample_indices(10, 2, 7, 7)


def test_aggregate():
    def record(a, b, c, d):
        return {'metrics': {'timestral': {'mae': a, 'direction_correct_24h': d},
                            'naive': {'mae': b}, 'quantile_10_90_empirical_coverage': c}}
    s = summarize([record(2, 4, .75, True), record(3, 2, .5, False)])
    assert s['completed'] == 2
    assert s['timestral_better_windows'] == 1
    assert s['mean_mae_improvement_pct'] == pytest.approx(100*(1-2.5/3))
    assert s['direction_accuracy'] == .5
