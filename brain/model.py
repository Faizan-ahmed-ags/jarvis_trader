"""Model training with purge-embargoed walk-forward CV + registry."""
import json

import numpy as np

from . import util
from .data_feed import get_prices
from .features import add_features
from .store import connect, set_memory

EMBARGO = 10  # sessions between train and test blocks (label horizon 5 + buffer)


def _make_model(params):
    from sklearn.ensemble import HistGradientBoostingClassifier

    p = {"learning_rate": 0.06, "max_iter": 300, "max_leaf_nodes": 15,
         "min_samples_leaf": 40, "l2_regularization": 0.1}
    p.update(params or {})
    return HistGradientBoostingClassifier(
        learning_rate=p["learning_rate"], max_iter=p["max_iter"],
        max_leaf_nodes=p["max_leaf_nodes"], min_samples_leaf=p["min_samples_leaf"],
        l2_regularization=p["l2_regularization"], random_state=7)


def _blocks(n, n_blocks=5):
    """Yield (train_slice_end, test_slice) for expanding-window walk-forward."""
    size = n // (n_blocks + 1)
    for b in range(n_blocks):
        train_end = size * (b + 1)
        test_end = min(train_end + size, n)
        if test_end - train_end < 30:
            continue
        yield train_end, test_end


def train_model(symbol, params=None):
    """Train on all labeled data; walk-forward CV decides status champion/candidate.

    Returns dict(model, cv_metrics, n_train, features, params, symbol, status).
    """
    df = get_prices(symbol, period="5y")
    if df is None:
        raise RuntimeError(f"no price data for {symbol}")
    X, y, feats, d = add_features(df)
    if X is None or len(X) < 250:
        raise RuntimeError(f"not enough clean rows for {symbol} ({0 if X is None else len(X)})")

    n = len(X)
    aucs, accs = [], []
    for train_end, test_end in _blocks(n):
        Xtr = X.iloc[:max(0, train_end - EMBARGO)]
        ytr = y.iloc[:max(0, train_end - EMBARGO)]
        Xte = X.iloc[train_end:test_end]
        yte = y.iloc[train_end:test_end]
        if len(Xtr) < 120 or len(Xte) < 30:
            continue
        m = _make_model(params)
        m.fit(Xtr, ytr)
        from sklearn.metrics import roc_auc_score, accuracy_score

        p = m.predict_proba(Xte)[:, 1]
        aucs.append(roc_auc_score(yte, p))
        accs.append(accuracy_score(yte, (p > 0.5).astype(int)))

    if not aucs:
        raise RuntimeError("walk-forward produced no valid blocks")

    final = _make_model(params)
    final.fit(X, y)
    cv = {"auc_mean": round(sum(aucs) / len(aucs), 4),
          "auc_blocks": [round(a, 3) for a in aucs],
          "acc_mean": round(sum(accs) / len(accs), 4)}
    status = "champion" if cv["auc_mean"] >= 0.53 else "candidate"
    meta = {"symbol": symbol, "params": params or {}, "features": feats,
            "cv": cv, "status": status, "created_at": util.iso(),
            "train_rows": n, "train_end": str(d.index[-1].date())}
    _persist(symbol, meta, final)
    return {"model": final, "meta": meta}


def _persist(symbol, meta, model):
    import joblib

    util.ensure_dirs()
    path = util.MODELS_DIR / f"model_{symbol}.joblib"
    joblib.dump({"model": model, "meta": meta}, path)
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO models(symbol,created_at,train_start,train_end,metrics,status,notes) "
            "VALUES(?,?,?,?,?,?,?)",
            (symbol, meta["created_at"], str(meta["train_rows"]), meta["train_end"],
             json.dumps(meta["cv"]), meta["status"], json.dumps(meta["params"])))
        conn.commit()
    finally:
        conn.close()
    set_memory(f"champion:{symbol}", meta)
    util.log_event("MODEL", f"{symbol}: {meta['status']} AUC={meta['cv']['auc_mean']} rows={meta['train_rows']}")

