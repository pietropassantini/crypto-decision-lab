import numpy as np
import pytest
from crypto_decision_lab.forecast_smoke import evaluate

def test_naive_perfect():
    result = evaluate([3,3], [3,3], 3)
    assert result['mae'] == 0
    assert result['rmse'] == 0
    assert result['direction_correct_24h'] is None

def test_direction_and_error():
    result = evaluate([102,104], [101,105], 100)
    assert result['mae'] == 1
    assert result['direction_correct_24h'] is True

def test_reject_nonfinite():
    with pytest.raises(ValueError):
        evaluate([1], [np.nan], 1)
