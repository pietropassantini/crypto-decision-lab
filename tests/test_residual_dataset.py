import numpy as np
import pandas as pd
import pytest
from crypto_decision_lab.residual_dataset import derive, FEATURES, TARGETS


def test_signed_residuals_and_no_leakage():
    features = pd.DataFrame([{'window_index': 4, 'forecast_start_utc': pd.Timestamp('2025-01-01', tz='UTC'),
      'last_context_close': 100.0, 'volatility_168h': .01, 'trend_168h': .02,
      'momentum_24h': .03, 'forecast_return_24h': .04,
      'interval_width_pct': .08, 'mean_interval_width_pct': .07}])
    f = pd.DataFrame({'timestamp':pd.date_range('2025-01-01', periods=24, freq='h', tz='UTC'),
       'actual':np.full(24, 103.), 'timestral_p10':np.full(24, 99.),
       'timestral_p50':np.full(24, 102.), 'timestral_p90':np.full(24, 105.),
       'naive':np.full(24, 100.)})
    x, y = derive(features, {4:f})
    assert list(x.columns) == list(FEATURES)
    assert list(y.columns) == list(TARGETS)
    assert y.loc[0, 'residual_pct_4h'] == pytest.approx(1.)
    assert y.loc[0, 'residual_pct_24h'] == pytest.approx(1.)
    assert y.loc[0, 'quantile_position_24h'] == pytest.approx(4/6)
    assert y.loc[0, 'timestral_better']
    assert not any('actual' in c or 'residual' in c for c in x.columns)


def test_anchor_rejection():
    f = pd.DataFrame({'actual':[99]*24,'timestral_p50':[100]*24,'timestral_p10':[98]*24,
                      'timestral_p90':[102]*24,'naive':[100]*24})
    x = pd.DataFrame([{k:(1 if k != 'window_index' else 0) for k in FEATURES}])
    with pytest.raises(ValueError, match='anchor'):
        derive(x,{0:f})
