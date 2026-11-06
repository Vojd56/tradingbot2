"""Testy stopy „Stará metoda" (krypto, jen papír). Bez sítě a bez Gemini."""

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import crypto_old_paper as co


def test_trigger():
    assert co.old_trigger({"price_move_pct": 0.5, "volume_ratio": 1.0}) == []
    assert co.old_trigger({"price_move_pct": -3.2, "volume_ratio": 1.0}) == ["1h pohyb -3.20 %"]
    r = co.old_trigger({"price_move_pct": 0.1, "volume_ratio": 2.5, "sfp_detected": True})
    assert r[0] == "SFP 15M" and "objem 2.50x" in r


def test_decide_and_levels():
    resp = {"chart": {"stop_loss": 90, "take_profit": 200}, "skeptik": {"verdict": "KÚPIŤ"},
            "executor": {"should_execute": True, "reasoning": "ok"}}
    d = co.decide(resp, 100.0)
    assert d["buy"]
    assert d["chart"]["stop_loss"] == 98.0          # SL max 2 % pod entry
    assert d["chart"]["take_profit"] == 106.0       # TP max 3R
    assert not co.decide(dict(resp, executor={"should_execute": False}), 100.0)["buy"]
    assert not co.decide(dict(resp, skeptik={"verdict": "POČKAŤ"}), 100.0)["buy"]


def test_trade_lifecycle():
    now = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    chart = {"entry": 100.0, "stop_loss": 98.0, "take_profit": 104.0, "rrr": "1:2.0"}
    t = co.new_trade("BTCUSDT", chart, now, ["objem 2.1x"], {}, "lite")
    assert abs(t["qty"] - 5.0) < 1e-9 and t["status"] == "OPEN"
    ms = t["opened_ms"]
    # svíčka před otevřením se ignoruje, pak TP
    bars = [[0, 0, 200, 50, 0, 0, ms - 1], [0, 0, 104.5, 99.0, 104.2, 0, ms + 60000]]
    hit = co.exit_for_bars(t, bars, now + timedelta(minutes=2))
    assert hit == (104.0, "Take-Profit")
    co.close_trade(t, *hit, now + timedelta(minutes=2))
    assert t["pnl_usd"] > 19 and t["r"] > 1.8
    # SL i TP v jedné svíčce -> SL
    t2 = co.new_trade("ETHUSDT", chart, now, [], {}, "lite")
    assert co.exit_for_bars(t2, [[0, 0, 105, 97, 100, 0, ms + 60000]], now)[1] == "Stop-Loss"
    s = co.summary([t, t2])
    assert s["closed"] == 1 and s["open"] == 1 and s["wins"] == 1


def test_run_pass_offline():
    tmp = Path(tempfile.mkdtemp())
    co.TRADES_FILE, co.STATE_FILE = tmp / "trades.json", tmp / "state.json"
    co.KILL_SWITCH_FILE = tmp / "STOP"
    co.CRYPTO_WATCHLIST = ["BTCUSDT", "ETHUSDT"]

    async def fake_gemini(prompt, ticker):
        assert "5 rolí" in prompt and ticker in prompt
        return {"chart": {}, "skeptik": {"verdict": "KÚPIŤ"}, "executor": {"should_execute": True},
                "model_used": "fake"}
    co.ask_gemini = fake_gemini
    co.manage_open = lambda now=None, **kw: []

    def fetch(sym):
        return {"price": 100.0, "price_move_pct": 3.5 if sym == "BTCUSDT" else 0.2, "volume_ratio": 1.0,
                "headlines": [], "sfp_detected": False}
    now = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    ev = asyncio.run(co.run_pass(fetch, now))
    assert [e["trade"]["ticker"] for e in ev] == ["BTCUSDT"]
    # druhý průchod: BTC už otevřený, ETH bez signálu -> nic
    assert asyncio.run(co.run_pass(fetch, now + timedelta(minutes=5))) == []
    st = co.load_state(now)
    assert st["gemini_calls"] == 1
    assert "STARÁ METODA" in co.format_event(ev[0])


if __name__ == "__main__":
    test_trigger()
    test_decide_and_levels()
    test_trade_lifecycle()
    test_run_pass_offline()
    print("test_crypto_old: OK")
