"""Offline test: replaces HTTP with simulated Kalshi / Open-Meteo / NWS data."""
import datetime as dt
import json
import os
import random
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import weather_bot as wb  # noqa: E402

tmp = tempfile.mkdtemp()
wb.RESULTS = tmp
wb.CFG["known_series"] = []
random.seed(1)

NOW = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
TOMORROW = (NOW - dt.timedelta(hours=5)).date() + dt.timedelta(days=1)
TK = TOMORROW.strftime("%y%b%d").upper()
SETTLE_RESULT = {}


def brackets(series, center_label_lo, prices):
    lows = [center_label_lo - 4 + 2 * i for i in range(6)]
    out = []
    for i, (lo, yp) in enumerate(zip(lows, prices)):
        if i == 0:
            sub, st, fl, cp = f"{lo + 1}° or below", "less", None, lo + 2
        elif i == 5:
            sub, st, fl, cp = f"{lo}° or above", "greater", lo - 1, None
        else:
            sub, st, fl, cp = f"{lo}° to {lo + 1}°", "between", lo, lo + 1
        out.append({"ticker": f"{series}-{TK}-B{i}", "event_ticker": f"{series}-{TK}",
                    "yes_sub_title": sub, "strike_type": st, "floor_strike": fl, "cap_strike": cp,
                    "yes_bid": yp - 1, "yes_ask": yp + 1, "volume": 500})
    return out


MARKETS = {
    # Market centred near 70-71, model (NWS 74 + ensemble ~74) says warmer -> edge on 74-75 / NO on 70-71
    "KXHIGHNY": brackets("KXHIGHNY", 70, [8, 22, 45, 18, 5, 2]),
    # Market agrees with model -> no pick
    "KXHIGHMIA": brackets("KXHIGHMIA", 84, [3, 15, 40, 30, 10, 2]),
}


def fake_ensemble(params):
    lats = params["latitude"].split(",")
    n_hours = 24 * (1 + params["forecast_days"])
    start = dt.datetime.combine((NOW - dt.timedelta(days=1)).date(), dt.time(0))
    times = [(start + dt.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(n_hours)]
    locs = []
    for lat in lats:
        base = 74 if lat.startswith("40") else 85
        hourly = {"time": times}
        for k in range(20):
            peak = base + random.gauss(0, 1.5)
            hourly[f"temperature_2m_member{k:02d}"] = [
                round(peak - 12 + 12 * max(0, 1 - abs(((h - 5) % 24) - 14) / 8), 1) for h in range(n_hours)]
        locs.append({"hourly": hourly})
    return locs


def fake_get_json(url, params=None, headers=None, retries=3):
    params = params or {}
    if url.endswith("/series"):
        return {"series": [
            {"ticker": "KXHIGHNY", "settlement_sources": [{"url": "https://forecast.weather.gov/product.php?site=OKX&product=CLI&issuedby=NYC"}]},
            {"ticker": "KXHIGHMIA", "settlement_sources": [{"url": "https://forecast.weather.gov/product.php?site=MFL&product=CLI&issuedby=MIA"}]},
            {"ticker": "KXRAINNYC"},
        ]}
    if url.endswith("/markets"):
        return {"markets": MARKETS.get(params["series_ticker"], []), "cursor": ""}
    if "/markets/" in url:
        t = url.rsplit("/", 1)[1]
        return {"market": {"ticker": t, "result": SETTLE_RESULT.get(t, "")}}
    if url.startswith(wb.OPEN_METEO):
        return fake_ensemble(params)
    if "/points/" in url:
        return {"properties": {"forecast": "https://api.weather.gov/gridpoints/X/1,1/forecast?" + url[-12:]}}
    if "/gridpoints/" in url:
        hi = 74 if "73.9" in url or "-73" in url else 85
        return {"properties": {"periods": [
            {"startTime": f"{TOMORROW}T06:00:00-05:00", "isDaytime": True, "temperature": hi},
            {"startTime": f"{TOMORROW}T18:00:00-05:00", "isDaytime": False, "temperature": hi - 12},
        ]}}
    if "/observations" in url:
        return {"features": []}
    raise AssertionError(url)


wb.get_json = fake_get_json

# --- unit checks
assert wb.bracket_from_text("70° to 71°") == (70, 71)
assert wb.bracket_from_text("69° or below") == (None, 69)
assert wb.bracket_from_text("78° or above") == (78, None)
assert wb.bracket_from_strikes({"strike_type": "greater", "floor_strike": 77}) == (78, None)
assert wb.bracket_from_strikes({"strike_type": "less", "cap_strike": 70}) == (None, 69)
assert abs(wb.fee_per_contract(0.5) - 0.0175) < 1e-9
mem = [(70.0, 1.0)]
ps = [wb.prob_bracket(mem, 1.5, lo, hi) for lo, hi in [(None, 67), (68, 69), (70, 71), (72, 73), (74, None)]]
assert abs(sum(ps) - 1) < 1e-6, sum(ps)
# truncation: observed 72 so far -> P(<=70) ~ 0
assert wb.prob_bracket(mem, 1.5, None, 69, floor_obs=72) == 0.0
print("unit checks ok; probs", [round(p, 3) for p in ps])

# --- full run
wb.main()
out = json.load(open(os.path.join(tmp, "latest.json")))
print("picks:")
for p in out["picks"]:
    print(f"  {p['city']} {p['date']} {p['side']} {p['bracket']}: model {p['model_prob']} vs price "
          f"{p['market_price']} edge {p['edge_taker']} limit {p['limit_price']} stake ${p['suggested_stake_usd']} conf {p['confidence']}")
for c in out["markets"]:
    print(" market", c["city"], "center", c["center"], "sd", c["sd"], "nws", c["nws"], "ens", c["ensemble_mean"],
          "| probs", [(b["label"], b["model_p"], b["yes_ask"]) for b in c["brackets"]])
assert out["picks"], "expected at least one pick for NYC"
assert all(p["city"].startswith("New York") for p in out["picks"]), "Miami should have no pick"

# --- settlement: make every pick win, rerun
for p in out["picks"]:
    SETTLE_RESULT[p["ticker"]] = "yes" if p["side"] == "YES" else "no"
wb.main()
out2 = json.load(open(os.path.join(tmp, "latest.json")))
print("track record:", out2["track_record"])
assert out2["track_record"]["settled"] == len(out["picks"])
assert out2["track_record"]["wins"] == len(out["picks"])
shutil.rmtree(tmp)
print("ALL TESTS PASSED")
