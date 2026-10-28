import numpy as np
import pandas as pd
import pytest
from crypto_decision_lab.residual_baselines import prepare, walk_forward, summarize


def fixtures(n=65):
    i=np.arange(n)
    dates=pd.date_range('2021-01-01',periods=n,freq='D',tz='UTC')
    features=pd.DataFrame({
        'window_index':i, 'forecast_start_utc':dates,
        'last_context_close':np.full(n,100.0),
        'volatility_168h':.02+i*.0001, 'trend_168h':np.sin(i),
        'momentum_24h':np.cos(i), 'forecast_return_24h':np.sin(i)*.5,
        'interval_width_pct':np.ones(n)*2, 'mean_interval_width_pct':np.ones(n)*3,
    })
    targets=pd.DataFrame({'window_index':i,'forecast_start_utc':dates,
                          'residual_pct_24h':np.sin(i/5),
                          'actual_return_pct_24h':np.sin(i)*.5+np.sin(i/5)})
    return features, targets


def test_walk_forward_no_future_targets():
    f,t=fixtures()
    df=prepare(f,t)
    a=walk_forward(df,50,10)
    changed=t.copy()
    changed.loc[changed.window_index>=50,'residual_pct_24h']=1e6
    changed.loc[changed.window_index>=50,'actual_return_pct_24h']=1e6
    b=walk_forward(prepare(f,changed),50,10)
    for key in ['ridge_predicted_residual_pct','histgb_predicted_residual_pct']:
        assert np.allclose(a.loc[a.fold==0,key],b.loc[b.fold==0,key])
    assert len(a)==15
    assert summarize(a)['evaluation_rows']==15


def test_prevent_target_mismatch():
    f,t=fixtures()
    t.loc[0,'forecast_start_utc']=pd.Timestamp('2020-01-01',tz='UTC')
    with pytest.raises(ValueError,match='mismatch'):
        prepare(f,t)


def test_prevent_duplicate():
    f,t=fixtures()
    f.loc[0,'window_index']=1
    with pytest.raises(ValueError,match='Duplicate'):
        prepare(f,t)
