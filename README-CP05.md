# Crypto Decision Lab S02-CP05: Residual Learning Baselines

Patch **additiva**: nessuna modifica ai dati precedenti. Confronta Timestral originale, naïve, Ridge e HistGradientBoosting sul **prezzo finale a 24h** (non MAE sulle 24 ore).

## Sirio

```bash
cd ~/projects/crypto-decision-lab
source .venv/bin/activate
unzip -o ../crypto-decision-lab-s02-cp05-patch.zip
pip install 'scikit-learn>=1.4,<2'
python -m pytest tests/test_residual_baselines.py -q
python -m crypto_decision_lab.residual_baselines
```

Output: `data/experiments/S02-CP05-residual-baselines-v1/predictions.parquet` e `summary.json`.

* 100 finestre ordinate temporalmente; prime 50 per training iniziale, 50 valutate mediante 5 fold espansivi da 10.
* StandardScaler allenato **solo sul training** in ciascun fold, senza leakage.
* Nessuna selezione iperparametri dai risultati di test. I due algoritmi hanno parametri fissati nel sorgente.
* L'output include tutte le previsioni per una revisione indipendente, checksum e metriche MAE/RMSE a prezzo finale 24h.
* Non è una validazione di trading né una valutazione indipendente dal pretraining Timestral. Non usarlo per trading reale.
* Con 100 punti e soli 50 esempi di training iniziale, l'overfitting resta probabile. Il progresso successivo è ampliare lo storico inferito, fissare test temporali intoccati e calibrare i risultati.
