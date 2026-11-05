#!/usr/bin/env python3
"""Kalshi daily-temperature analyzer.

Each run:
  1. Discovers every open Kalshi daily high/low temperature market (KXHIGH*/KXLOW*).
  2. Builds a probability for each temperature bracket from ~140 ensemble
     forecast members (GFS, ECMWF, ICON, GEM via Open-Meteo), blended with the
     official NWS point forecast and, for same-day markets, the high/low
     already observed at the settlement station.
  3. Compares those probabilities with Kalshi's prices after fees and ranks
     the brackets where the gap is big enough to be worth trading.
  4. Logs every pick and later grades it against the settled result, so the
     track record shows whether the edge is real.

Standard library only. Writes results/latest.json, results/runs/<stamp>.json,
results/picks.csv and results/snapshots.csv.
"""
import csv
import datetime as dt
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request

from stations import STATIONS, TICKER_HINTS

ROOT = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(ROOT, "results")
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
NWS = "https://api.weather.gov"
OPEN_METEO = "https://ensemble-api.open-meteo.com/v1/ensemble"
UA = "kalshi-weather-research (github actions)"

CFG = json.load(open(os.path.join(ROOT, "config.json")))
LOG = []


def log(msg):
    LOG.append(msg)
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------- HTTP
def get_json(url, params=None, headers=None, retries=3):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    h = {"User-Agent": UA, "Accept": "application/geo+json, application/json"}
    h.update(headers or {})
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


# ---------------------------------------------------------------- math
def norm_cdf(x, mu, sd):
    return 0.5 * (1 + math.erf((x - mu) / (sd * math.sqrt(2))))


def fee_per_contract(price, rate=0.07):
    """Kalshi taker fee per contract: rate * P * (1-P) (rounded up per order)."""
    return rate * price * (1 - price)


def bracket_from_text(sub):
    """'70° to 71°' -> (70, 71); '69° or below' -> (None, 69); '78° or above' -> (78, None)."""
    s = (sub or "").replace("°", "").lower()
    nums = [int(n) for n in re.findall(r"-?\d+", s)]
    if "below" in s or "or less" in s or "lower" in s:
        return (None, nums[0]) if nums else None
    if "above" in s or "or more" in s or "higher" in s:
        return (nums[0], None) if nums else None
    if len(nums) >= 2:
        return (min(nums[:2]), max(nums[:2]))
    return None


def bracket_from_strikes(m):
    st = (m.get("strike_type") or "").lower()
    fl, cp = m.get("floor_strike"), m.get("cap_strike")
    if st == "between" and fl is not None and cp is not None:
        return (int(round(fl)), int(round(cp)))
    if st in ("greater", "greater_or_equal") and fl is not None:
        return (int(math.floor(fl)) + (1 if st == "greater" else 0), None)
    if st in ("less", "less_or_equal") and cp is not None:
        return (None, int(math.ceil(cp)) - (1 if st == "less" else 0))
    return None


def prob_bracket(members, kernel_sd, lo, hi, floor_obs=None, ceil_obs=None):
    """P(integer settlement temp in [lo, hi]) from a mixture of normals centred
    on each (weighted) member. floor_obs/ceil_obs truncate the distribution with
    what has already been observed today."""
    a = -1e9 if lo is None else lo - 0.5
    b = 1e9 if hi is None else hi + 0.5
    if floor_obs is not None:
        a = max(a, floor_obs - 0.5)
    if ceil_obs is not None:
        b = min(b, ceil_obs + 0.5)
    lo_t = -1e9 if floor_obs is None else floor_obs - 0.5
    hi_t = 1e9 if ceil_obs is None else ceil_obs + 0.5
    num = den = 0.0
    for v, w in members:
        den += w * (norm_cdf(hi_t, v, kernel_sd) - norm_cdf(lo_t, v, kernel_sd))
        if b > a:
            num += w * (norm_cdf(b, v, kernel_sd) - norm_cdf(a, v, kernel_sd))
    if den <= 1e-12:
        return 1.0 if (b > a) else 0.0
    return max(0.0, min(1.0, num / den))


# ---------------------------------------------------------------- Kalshi
def price(m, key):
    v = m.get(key)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v / 100.0
    v = m.get(key + "_dollars")
    if v not in (None, ""):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def discover_series():
    found = {}
    try:
        data = get_json(f"{KALSHI}/series", {"category": "Climate and Weather"})
        for s in data.get("series", []) or []:
            t = s.get("ticker", "")
            if t.startswith("KXHIGH") or t.startswith("KXLOW"):
                found[t] = s
    except Exception as e:  # noqa: BLE001
        log(f"series discovery failed: {e}")
    for t in CFG["known_series"]:
        found.setdefault(t, {"ticker": t})
    return found


def cli_code_for(series):
    for src in series.get("settlement_sources", []) or []:
        url = src.get("url", "") or ""
        m = re.search(r"issuedby=([A-Za-z]{3})", url)
        if m and m.group(1).upper() in STATIONS:
            return m.group(1).upper()
        m = re.search(r"/(K[A-Z]{3})\b", url)
        if m and m.group(1)[1:] in STATIONS:
            return m.group(1)[1:]
    t = series["ticker"]
    frag = re.sub(r"^KX(HIGH|LOW)T?", "", t)
    for cand in (frag, re.sub(r"^KX(HIGH|LOW)", "", t)):
        if cand in TICKER_HINTS:
            return TICKER_HINTS[cand]
    return None


def event_date(event_ticker):
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker or "")
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(0)[1:], "%y%b%d").date()
    except ValueError:
        return None


def open_markets(series_ticker):
    out, cursor = [], None
    for _ in range(5):
        p = {"series_ticker": series_ticker, "status": "open", "limit": 200}
        if cursor:
            p["cursor"] = cursor
        d = get_json(f"{KALSHI}/markets", p)
        out += d.get("markets", []) or []
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


# ---------------------------------------------------------------- weather
def cli_window_utc(day, std_offset):
    start = dt.datetime.combine(day, dt.time(0)) - dt.timedelta(hours=std_offset)
    return start, start + dt.timedelta(hours=24)


def ensemble_members(stations, days_ahead):
    """Returns {cli: {model: [hourly series per member]}} plus the hourly times (UTC)."""
    lats = ",".join(str(STATIONS[c][1]) for c in stations)
    lons = ",".join(str(STATIONS[c][2]) for c in stations)
    result = {c: {} for c in stations}
    times = None
    for model in CFG["ensemble_models"]:
        try:
            d = get_json(OPEN_METEO, {
                "latitude": lats, "longitude": lons, "hourly": "temperature_2m",
                "models": model, "temperature_unit": "fahrenheit", "timezone": "GMT",
                "past_days": 1, "forecast_days": days_ahead + 2,
            })
        except Exception as e:  # noqa: BLE001
            log(f"ensemble {model} failed: {e}")
            continue
        locs = d if isinstance(d, list) else [d]
        for c, loc in zip(stations, locs):
            hourly = loc.get("hourly", {})
            times = times or [dt.datetime.fromisoformat(t) for t in hourly.get("time", [])]
            series = [v for k, v in hourly.items()
                      if k.startswith("temperature_2m") and isinstance(v, list)]
            if series:
                result[c][model] = series
    return result, times or []


def member_extremes(model_series, times, day, std_offset, kind):
    """One value per member (weighted equally within a model, models equal)."""
    start, end = cli_window_utc(day, std_offset)
    idx = [i for i, t in enumerate(times) if start <= t < end]
    pts = []
    for model, series in model_series.items():
        vals = []
        for s in series:
            hrs = [s[i] for i in idx if i < len(s) and s[i] is not None]
            if len(hrs) >= 18:
                vals.append(max(hrs) if kind == "high" else min(hrs))
        if vals:
            pts.append((model, vals))
    members = []
    for model, vals in pts:
        w = 1.0 / (len(pts) * len(vals))
        members += [(v, w) for v in vals]
    return members, {m: round(sum(v) / len(v), 1) for m, v in pts}


def nws_forecast(cli, cache):
    icao, lat, lon = STATIONS[cli][:3]
    try:
        if cli not in cache:
            p = get_json(f"{NWS}/points/{lat:.4f},{lon:.4f}")
            cache[cli] = p["properties"]["forecast"]
        return get_json(cache[cli]).get("properties", {}).get("periods", [])
    except Exception as e:  # noqa: BLE001
        log(f"NWS forecast {cli} failed: {e}")
        return []


def nws_value(periods, day, kind):
    for p in periods:
        st = dt.datetime.fromisoformat(p["startTime"])
        if kind == "high" and p.get("isDaytime") and st.date() == day:
            return p.get("temperature")
        if kind == "low" and not p.get("isDaytime") and st.date() == day - dt.timedelta(days=1):
            return p.get("temperature")
    return None


def observed_so_far(cli, day, now_utc):
    icao, _, _, off, _ = STATIONS[cli]
    start, end = cli_window_utc(day, off)
    if now_utc <= start:
        return None, None, 0.0
    try:
        d = get_json(f"{NWS}/stations/{icao}/observations",
                     {"start": start.strftime("%Y-%m-%dT%H:%M:%SZ")})
    except Exception as e:  # noqa: BLE001
        log(f"obs {icao} failed: {e}")
        return None, None, 0.0
    temps = []
    for f in d.get("features", []):
        pr = f.get("properties", {})
        t = (pr.get("temperature") or {}).get("value")
        ts = pr.get("timestamp")
        if t is None or not ts:
            continue
        when = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
        if start <= when < end:
            temps.append(t * 9 / 5 + 32)
    if not temps:
        return None, None, 0.0
    frac = min(1.0, (now_utc - start).total_seconds() / 86400)
    return max(temps), min(temps), frac


# ---------------------------------------------------------------- core
def analyze():
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).replace(microsecond=0)
    os.makedirs(os.path.join(RESULTS, "runs"), exist_ok=True)
    cache_path = os.path.join(RESULTS, "nws_points_cache.json")
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}

    series = discover_series()
    by_cli, markets = {}, []
    for t, s in sorted(series.items()):
        cli = cli_code_for(s)
        if not cli:
            log(f"skip {t}: unknown settlement station")
            continue
        try:
            ms = open_markets(t)
        except Exception as e:  # noqa: BLE001
            log(f"markets {t} failed: {e}")
            continue
        kind = "low" if t.startswith("KXLOW") else "high"
        for m in ms:
            d = event_date(m.get("event_ticker"))
            if not d:
                continue
            m["_cli"], m["_kind"], m["_date"], m["_series"] = cli, kind, d, t
            markets.append(m)
        if ms:
            by_cli.setdefault(cli, set()).add(kind)
    log(f"{len(markets)} open brackets across {len(by_cli)} stations")

    stations = sorted(by_cli)
    ens, times = ensemble_members(stations, CFG["days_ahead"])

    groups = {}
    for m in markets:
        groups.setdefault((m["_series"], m["_date"]), []).append(m)

    floors = CFG["sd_floor_by_lead_days"]
    candidates, cities, snaps = [], [], []
    for (st, day), ms in sorted(groups.items()):
        cli, kind = ms[0]["_cli"], ms[0]["_kind"]
        icao, _, _, off, name = STATIONS[cli]
        local_today = (now + dt.timedelta(hours=off)).date()
        lead = (day - local_today).days
        if lead < 0 or lead > CFG["days_ahead"]:
            continue
        members, model_means = member_extremes(ens.get(cli, {}), times, day, off, kind)
        if len(members) < 10:
            log(f"{st} {day}: not enough ensemble data")
            continue
        ens_mean = sum(v * w for v, w in members)
        ens_sd = math.sqrt(max(0.0, sum(w * (v - ens_mean) ** 2 for v, w in members)))
        periods = nws_forecast(cli, cache)
        nws = nws_value(periods, day, kind)
        center = ens_mean if nws is None else (CFG["nws_weight"] * nws + (1 - CFG["nws_weight"]) * ens_mean)
        obs_hi = obs_lo = None
        frac = 0.0
        if lead == 0:
            obs_hi, obs_lo, frac = observed_so_far(cli, day, now)
        floor = floors[min(lead, len(floors) - 1)]
        if lead == 0 and frac > 0.5:
            floor = max(0.9, floor * (1.0 - frac) * 1.6)
        target_sd = max(floor, ens_sd * 1.15)
        kernel = math.sqrt(max(target_sd ** 2 - ens_sd ** 2, 0.8 ** 2))
        shifted = [(v + (center - ens_mean), w) for v, w in members]
        # Observed value bounds the outcome (minus 1F for C->F rounding of ASOS obs).
        f_obs = (round(obs_hi) - 1) if (kind == "high" and obs_hi is not None) else None
        c_obs = (round(obs_lo) + 1) if (kind == "low" and obs_lo is not None) else None

        disagreement = None if nws is None else round(nws - ens_mean, 1)
        conf = "high"
        if disagreement is not None and abs(disagreement) >= 3:
            conf = "low"
        elif ens_sd > 3.5 or (disagreement is not None and abs(disagreement) >= 2):
            conf = "medium"

        rows = []
        for m in ms:
            br = bracket_from_text(m.get("yes_sub_title") or m.get("subtitle")) or bracket_from_strikes(m)
            if not br:
                continue
            p = prob_bracket(shifted, kernel, br[0], br[1], f_obs, c_obs)
            ya, yb = price(m, "yes_ask"), price(m, "yes_bid")
            na = price(m, "no_ask")
            if na is None and yb is not None:
                na = round(1 - yb, 2)
            vol = m.get("volume") or m.get("volume_fp") or 0
            try:
                vol = float(vol)
            except (TypeError, ValueError):
                vol = 0.0
            label = (m.get("yes_sub_title") or m.get("subtitle") or "").strip()
            rows.append({"ticker": m["ticker"], "label": label, "lo": br[0], "hi": br[1],
                         "model_p": round(p, 3), "yes_bid": yb, "yes_ask": ya, "no_ask": na,
                         "volume": vol})
            mid = None if (ya is None or yb is None) else round((ya + yb) / 2, 3)
            snaps.append([now.isoformat(), st, day.isoformat(), lead, m["ticker"], label,
                          round(p, 4), yb, ya, mid, vol])
            for side, px, pw in (("YES", ya, p), ("NO", na, 1 - p)):
                if px is None or not (CFG["min_price"] <= px <= CFG["max_price"]):
                    continue
                if vol < CFG["min_volume_contracts"]:
                    continue
                taker_edge = pw - px - fee_per_contract(px)
                bid = yb if side == "YES" else (None if ya is None else round(1 - ya, 2))
                maker_px = None if bid is None else round(min(bid + 0.01, px - 0.01), 2)
                maker_edge = None if maker_px is None or maker_px <= 0 else pw - maker_px
                ok = taker_edge >= CFG["min_edge_taker"] or (maker_edge or 0) >= CFG["min_edge_maker"]
                if not ok:
                    continue
                kelly = max(0.0, (pw - px) / (1 - px)) * CFG["kelly_multiplier"]
                stake = min(kelly, CFG["max_bet_fraction"]) * CFG["bankroll_usd"]
                candidates.append({
                    "city": name, "station": icao, "series": st, "date": day.isoformat(),
                    "lead_days": lead, "kind": kind, "ticker": m["ticker"], "bracket": label,
                    "side": side, "model_prob": round(pw, 3), "market_price": px,
                    "fee": round(fee_per_contract(px), 4), "edge_taker": round(taker_edge, 3),
                    "limit_price": maker_px,
                    "edge_maker": None if maker_edge is None else round(maker_edge, 3),
                    "roi_taker": round(taker_edge / px, 3), "confidence": conf,
                    "suggested_stake_usd": round(stake, 2),
                    "contracts": int(stake // px) if px else 0, "volume": vol,
                    "forecast_center": round(center, 1), "nws": nws,
                    "ensemble_mean": round(ens_mean, 1), "sd": round(target_sd, 2),
                    "observed_high": None if obs_hi is None else round(obs_hi, 1),
                    "observed_low": None if obs_lo is None else round(obs_lo, 1),
                })
        fav = max(rows, key=lambda r: r["yes_ask"] or 0) if rows else None
        top = max(rows, key=lambda r: r["model_p"]) if rows else None
        cities.append({
            "city": name, "station": icao, "series": st, "date": day.isoformat(), "kind": kind,
            "lead_days": lead, "nws": nws, "ensemble_mean": round(ens_mean, 1),
            "model_means": model_means, "center": round(center, 1), "sd": round(target_sd, 2),
            "observed_high": None if obs_hi is None else round(obs_hi, 1),
            "observed_low": None if obs_lo is None else round(obs_lo, 1),
            "confidence": conf,
            "market_favorite": fav and {"bracket": fav["label"], "yes_ask": fav["yes_ask"]},
            "model_favorite": top and {"bracket": top["label"], "prob": top["model_p"]},
            "brackets": rows,
        })

    rank = {"high": 0, "medium": 1, "low": 2}
    candidates.sort(key=lambda c: (rank[c["confidence"]], -max(c["edge_taker"], c["edge_maker"] or -1)))
    seen, picks = set(), []
    for c in candidates:
        key = (c["series"], c["date"])
        if key in seen or c["confidence"] == "low":
            continue
        seen.add(key)
        picks.append(c)
        if len(picks) >= CFG["max_picks"]:
            break

    json.dump(cache, open(cache_path, "w"), indent=1)
    return now, picks, candidates, cities, snaps


# ---------------------------------------------------------------- track record
PICK_COLS = ["run_utc", "date", "city", "ticker", "bracket", "side", "model_prob",
             "market_price", "limit_price", "edge_taker", "confidence", "status", "result",
             "pnl_per_contract"]


def append_csv(path, cols, rows):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(cols)
        w.writerows(rows)


def log_picks(now, picks):
    path = os.path.join(RESULTS, "picks.csv")
    existing = set()
    if os.path.exists(path):
        for r in csv.DictReader(open(path)):
            existing.add((r["ticker"], r["side"]))
    rows = []
    for p in picks:
        if (p["ticker"], p["side"]) in existing:
            continue
        rows.append([now.isoformat(), p["date"], p["city"], p["ticker"], p["bracket"], p["side"],
                     p["model_prob"], p["market_price"], p["limit_price"], p["edge_taker"],
                     p["confidence"], "open", "", ""])
    append_csv(path, PICK_COLS, rows)


def settle_picks():
    path = os.path.join(RESULTS, "picks.csv")
    if not os.path.exists(path):
        return
    rows = list(csv.DictReader(open(path)))
    changed = False
    for r in rows:
        if r["status"] != "open":
            continue
        try:
            m = get_json(f"{KALSHI}/markets/{r['ticker']}").get("market", {})
        except Exception as e:  # noqa: BLE001
            log(f"settle {r['ticker']} failed: {e}")
            continue
        res = (m.get("result") or "").lower()
        if res not in ("yes", "no"):
            continue
        px = float(r["market_price"])
        won = (res == "yes") == (r["side"] == "YES")
        pnl = (1 - px if won else -px) - fee_per_contract(px)
        r.update(status="settled", result="WIN" if won else "LOSS", pnl_per_contract=f"{pnl:.3f}")
        changed = True
    if changed:
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=PICK_COLS)
            w.writeheader()
            w.writerows(rows)


def track_record():
    path = os.path.join(RESULTS, "picks.csv")
    if not os.path.exists(path):
        return {"settled": 0}
    rows = [r for r in csv.DictReader(open(path)) if r["status"] == "settled"]
    if not rows:
        return {"settled": 0, "open": sum(1 for r in csv.DictReader(open(path)) if r["status"] == "open")}
    pnl = [float(r["pnl_per_contract"]) for r in rows]
    cost = sum(float(r["market_price"]) for r in rows)
    wins = sum(1 for r in rows if r["result"] == "WIN")
    exp = sum(float(r["model_prob"]) for r in rows)
    brier = sum((float(r["model_prob"]) - (1 if r["result"] == "WIN" else 0)) ** 2 for r in rows) / len(rows)
    mkt_brier = sum((float(r["market_price"]) - (1 if r["result"] == "WIN" else 0)) ** 2 for r in rows) / len(rows)
    return {
        "settled": len(rows), "wins": wins, "expected_wins": round(exp, 1),
        "win_rate": round(wins / len(rows), 3), "pnl_per_contract_total": round(sum(pnl), 2),
        "roi": round(sum(pnl) / cost, 3) if cost else None,
        "brier_model": round(brier, 4), "brier_market": round(mkt_brier, 4),
        "open": sum(1 for r in csv.DictReader(open(path)) if r["status"] == "open"),
    }


def main():
    settle_picks()
    now, picks, candidates, cities, snaps = analyze()
    log_picks(now, picks)
    append_csv(os.path.join(RESULTS, "snapshots.csv"),
               ["run_utc", "series", "date", "lead", "ticker", "bracket", "model_p",
                "yes_bid", "yes_ask", "mid", "volume"], snaps)
    out = {
        "run_utc": now.isoformat() + "Z", "picks": picks,
        "other_candidates": candidates[len(picks):len(picks) + 15],
        "track_record": track_record(), "markets": cities,
        "notes": LOG[-40:], "config": {k: CFG[k] for k in CFG if k != "known_series"},
    }
    json.dump(out, open(os.path.join(RESULTS, "latest.json"), "w"), indent=1, default=str)
    stamp = now.strftime("%Y%m%dT%H%MZ")
    slim = dict(out)
    slim["markets"] = [{k: v for k, v in c.items() if k != "brackets"} for c in cities]
    json.dump(slim, open(os.path.join(RESULTS, "runs", f"{stamp}.json"), "w"), indent=1, default=str)
    print(f"{len(picks)} picks, {len(cities)} markets analyzed")


if __name__ == "__main__":
    main()
