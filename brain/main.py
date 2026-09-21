"""JARVIS brain CLI.

Usage (from repo root, with .venv activated or via runtime/python.exe):
  python -m brain.main init-secrets
  python -m brain.main screen
  python -m brain.main train
  python -m brain.main backtest
  python -m brain.main run-once
  python -m brain.main status
  python -m brain.main evolve
  python -m brain.main daemon
"""
import argparse
import json
import time

from . import util
from . import config as cfgmod
from .brokers import get_broker, secrets_template
from .halal import screen_universe
from .store import recent_decisions, get_memory


def _load_model(symbol):
    try:
        import joblib
        path = util.MODELS_DIR / f"model_{symbol}.joblib"
        if path.exists():
            return joblib.load(path)
    except Exception as e:
        util.log_event("MODEL", f"load failed for {symbol}: {e}")
    return None


def cmd_screen(_args):
    cfg = cfgmod.load()
    results = screen_universe(cfg["universe"])
    for sym, r in results.items():
        flag = "PASS" if r["compliant"] else "FAIL"
        print(f"{sym:6} {flag}  {r['reason']}")
        if r.get("ratios"):
            print(f"        debt/mcap={r['ratios']['debt']:.2f} "
                  f"cash+int/mcap={r['ratios']['cash_int']:.2f} "
                  f"recv/mcap={r['ratios']['receivables']:.2f}")


def cmd_train(_args):
    cfg = cfgmod.load()
    for sym in cfg["universe"]:
        print(f"training {sym} ...")
        from .model import train_model
        out = train_model(sym)
        m = out["meta"]
        print(f"  {m['status']}  AUC={m['cv']['auc_mean']}  "
              f"acc={m['cv']['acc_mean']}  blocks={m['cv']['auc_blocks']}  rows={m['train_rows']}")


def cmd_backtest(args):
    cfg = cfgmod.load()
    champs = util.load_json(util.DATA_DIR / "champion_params.json", {}) or {}
    for sym in ([args.symbol] if args.symbol else cfg["universe"]):
        from .strategy import backtest
        r = backtest(sym, params=champs.get(sym))
        if not r.get("ok"):
            print(f"{sym}: backtest failed — {r.get('error')}")
            continue
        print(f"{sym}: OOS return {r['return_pct']}% (buy&hold {r['buy_hold_pct']}%) "
              f"sharpe={r['sharpe']} sortino={r['sortino']} maxDD={r['max_drawdown_pct']}% "
              f"over {r['oos_days']} sessions")
        print(f"  params: threshold={r['params']['threshold']} TP={r['params']['take_profit']} "
              f"SL={r['params']['stop_loss']} pos={r['params']['max_position_frac']}")


def cmd_run_once(_args):
    cfg = cfgmod.load()
    broker = get_broker(cfg)
    print(f"broker: {broker.name}")

    # kill-switch: daily loss guard
    day = util.utcnow().strftime("%Y-%m-%d")
    guard = get_memory("day_guard", {}) or {}
    prices = {}
    equity = broker.equity(prices)
    if guard.get("date") != day:
        guard = {"date": day, "start_equity": equity}
    loss_pct = (equity / guard["start_equity"] - 1) * 100 if guard["start_equity"] else 0
    blocked = loss_pct <= -float(cfg["max_daily_loss_pct"])

    screened = screen_universe(cfg["universe"])
    for sym, comp in screened.items():
        if not comp["compliant"]:
            print(f"{sym}: SKIP — {comp['reason']}")
            continue
        df = None
        try:
            from .data_feed import get_prices, github_signal
            df = get_prices(sym)
            gh_raw, gh_z = github_signal(sym)
        except Exception as e:
            gh_z, gh_raw = None, None
            util.log_event("DATA", f"{sym}: run-once data error {e}")
        bundle = _load_model(sym)
        if df is None or bundle is None:
            print(f"{sym}: WAIT — no data/model yet (run `train`)")
            continue
        if bundle.get("meta", {}).get("status") != "champion":
            print(f"{sym}: OBSERVE — model AUC below edge gate "
                  f"({bundle.get('meta', {}).get('cv', {}).get('auc_mean')}); no orders placed")
            continue
        from .strategy import decide
        champs = util.load_json(util.DATA_DIR / "champion_params.json", {}) or {}
        act = decide(bundle, df, gh_z=gh_z, params=champs.get(sym),
                     equity=equity, current_position=broker.position(sym))
        prob = f"{act['prob']:.2f}" if act.get("prob") is not None else "n/a"
        if act["action"] == "buy" and blocked:
            act = dict(act, action="hold",
                       reason=f"daily loss guard {loss_pct:.1f}% <= -{cfg['max_daily_loss_pct']}%")
        print(f"{sym}: p={prob} -> {act['action'].upper()} ({act['reason']})")
        if act["action"] == "buy":
            px = float(df["Close"].iloc[-1])
            res = broker.buy(sym, act["qty"], px, act["reason"])
            print(f"   buy {res.get('filled_qty', 0)} @ {px}")
            util.log_event("TRADE", f"{sym} BUY {res.get('filled_qty')} @ {px}: {act['reason']}")
        elif act["action"] == "sell":
            res = broker.sell(sym, act["qty"], float(df["Close"].iloc[-1]), act["reason"])
            util.log_event("TRADE", f"{sym} SELL: {act['reason']}")
            print(f"   sold.")
    guard["last_equity"] = equity
    from .store import set_memory
    set_memory("day_guard", guard)
    print(f"equity: {broker.equity(prices)}  mode={broker.name}")


def cmd_status(_args):
    cfg = cfgmod.load()
    broker = get_broker(cfg)
    print(f"mode: {'PAPER (alpaca)' if broker.name == 'alpaca-paper' else 'SIM (no keys needed)'}")
    print(f"equity: {broker.equity()}")
    print(f"positions: {json.dumps(broker.positions())}")
    print("\nrecent decisions:")
    for d in recent_decisions(10):
        print(f"  {d['ts']}  {d['symbol']:5} {d['action']:4} qty={d['qty']} @ {d['price']}  [{d['mode']}] {d['reason'][:60]}")
    print("\nchampions:")
    for sym in cfg["universe"]:
        m = get_memory(f"champion:{sym}")
        if m:
            print(f"  {sym}: {m['status']} AUC={m['cv']['auc_mean']} trained to {m['train_end']}")
    evo = get_memory("last_evolution")
    if evo:
        print(f"\nlast evolution: {evo['at']}")
        for sym, r in evo["results"].items():
            print(f"  {sym}: {r['status']} {r.get('why', '')}")


def cmd_evolve(_args):
    cfg = cfgmod.load()
    print("running evolution pass (LLM proposes, backtest disposes)...")
    results = _evolve(cfg)
    for sym, r in results.items():
        line = f"  {sym}: {r['status']}"
        if r.get("challenger_return") is not None:
            line += f" (challenger {r['challenger_return']}% vs incumbent {r['incumbent_return']}%)"
        if r.get("why"):
            line += f" — {r['why']}"
        print(line)


def _evolve(cfg):
    from .agent import review_and_evolve
    return review_and_evolve(cfg, cfg["universe"])


def cmd_daemon(_args):
    cfg = cfgmod.load()
    print(f"daemon started. daily run at {cfg['daily_run_time']} UTC, "
          f"evolve on {cfg['evolve_day']} at {cfg['evolve_time']} UTC. Ctrl+C to stop.")
    last_run_date, last_evo_date = None, None
    # MT5 attach (logs in if credentials are set; harmless when not)
    try:
        from . import mt5_bridge
        mt5_bridge.connect()
    except Exception as e:
        util.log_event("DAEMON", f"mt5 attach skipped: {e}")
    # daily learn->predict->trade cycle thread
    from .daily import schedule_loop
    import threading
    stop = threading.Event()
    threading.Thread(target=schedule_loop, args=(stop,), daemon=True).start()
    while True:
        now = util.utcnow()
        hm = now.strftime("%H:%M")
        day = now.strftime("%Y-%m-%d")
        wd = now.strftime("%A").lower()
        if hm == cfg["daily_run_time"] and last_run_date != day:
            print(f"[{util.iso()}] scheduled run-once")
            cmd_run_once(None)
            last_run_date = day
        if wd == cfg["evolve_day"] and hm == cfg["evolve_time"] and last_evo_date != day:
            print(f"[{util.iso()}] scheduled evolve")
            cmd_evolve(None)
            last_evo_date = day
        time.sleep(30)


def cmd_init_secrets(_args):
    p = util.ROOT / "secrets.json"
    if p.exists():
        print("secrets.json already exists — not overwriting.")
        return
    util.save_json(p, secrets_template())
    print("created secrets.json — fill in keys you want (all optional):")
    print("  groq_api_key  -> enables the self-evolution agent")
    print("  alpaca_key_id/alpaca_secret_key -> enables Alpaca PAPER trading")
    print("  github_token  -> higher GitHub API rate limits")


def cmd_cycle(_args):
    """Full daily cycle (same brain as the local console): learn -> predict ->
    trade -> journal -> lessons. Used by the GitHub Actions cloud runner."""
    import json
    from .daily import run_cycle
    res = run_cycle(trigger="cloud")
    print(json.dumps(res, indent=1, default=str)[:4000])


def main():
    ap = argparse.ArgumentParser(prog="jarvis-brain")
    sub = ap.add_subparsers(dest="cmd", required=True)
    simple = ("init-secrets", "screen", "train", "run-once", "status", "evolve", "daemon", "cycle")
    for name in simple:
        sub.add_parser(name).set_defaults(func=globals()[f"cmd_{name.replace('-', '_')}"])
    bt = sub.add_parser("backtest")
    bt.add_argument("--symbol")
    bt.set_defaults(func=cmd_backtest)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
