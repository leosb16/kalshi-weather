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


def member_extremes(model_series, times, day, std_offset, kind, bias=None):
    """One value per member (weighted equally within a model, models equal)."""
    start, end = cli_window_utc(day, std_offset)
    idx = [i for i, t in enumerate(times) if start <= t < end]
    pts = []
    for model, series in model_series.items():
        vals = []
        for s in series:
            hrs = [s[i] for i in idx if i < len(s) and s[i] is not None]
            if len(hrs) >= 18:
                vals.append((max(hrs) if kind == "high" else min(hrs)) + (bias or {}).get(model, 0.0))
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



# ---------------------------------------------------------------- calibration
PREV_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"
DET_API = "https://api.open-meteo.com/v1/forecast"
DET_MODELS = ["gfs_seamless", "ecmwf_ifs025", "icon_seamless", "gem_seamless"]


def _model_key(keys, var, model):
    for k in keys:
        if k == f"{var}_{model}":
            return k
    return var if (len(DET_MODELS) == 1 and var in keys) else None


def det_forecasts(stations, days_ahead):
    """Current deterministic (high-resolution) model runs: {cli: {model: hourly}}, times."""
    out, times = {c: {} for c in stations}, []
    for i in range(0, len(stations), 10):
        chunk = stations[i:i + 10]
        try:
            d = get_json(DET_API, {
                "latitude": ",".join(str(STATIONS[c][1]) for c in chunk),
                "longitude": ",".join(str(STATIONS[c][2]) for c in chunk),
                "hourly": "temperature_2m", "models": ",".join(DET_MODELS),
                "temperature_unit": "fahrenheit", "timezone": "GMT",
                "past_days": 1, "forecast_days": days_ahead + 2,
            })
        except Exception as e:  # noqa: BLE001
            log(f"deterministic forecasts failed: {e}")
            continue
        locs = d if isinstance(d, list) else [d]
        for c, loc in zip(chunk, locs):
            h = loc.get("hourly", {})
            times = times or [dt.datetime.fromisoformat(t) for t in h.get("time", [])]
            for m in DET_MODELS:
                k = _model_key(h.keys(), "temperature_2m", m)
                if k and h.get(k):
                    out[c][m] = h[k]
    return out, times


def daily_extreme(arr, times, day, off, kind, min_hours=20):
    s, e = cli_window_utc(day, off)
    hrs = [arr[i] for i, t in enumerate(times) if s <= t < e and i < len(arr) and arr[i] is not None]
    if len(hrs) < min_hours:
        return None
    return max(hrs) if kind == "high" else min(hrs)


def settled_actuals(series_ticker, days):
    """{date: settled temperature} from Kalshi's settled 2-degree brackets."""
    out, cursor = {}, None
    cutoff = dt.date.today() - dt.timedelta(days=days)
    for _ in range(6):
        p = {"series_ticker": series_ticker, "status": "settled", "limit": 1000}
        if cursor:
            p["cursor"] = cursor
        d = get_json(f"{KALSHI}/markets", p)
        for m in d.get("markets", []) or []:
            if (m.get("result") or "").lower() != "yes":
                continue
            day = event_date(m.get("event_ticker"))
            if not day or day < cutoff:
                continue
            br = bracket_from_text(m.get("yes_sub_title") or m.get("subtitle")) or bracket_from_strikes(m)
            if br and br[0] is not None and br[1] is not None:
                out[day] = (br[0] + br[1]) / 2.0
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


def calibrate(series_info):
    """For each station/kind and lead (0 = same day, 1 = day ahead): each model's
    bias (actual - forecast) and the RMSE of the bias-corrected model average,
    measured against the last N days of settled Kalshi results."""
    path = os.path.join(RESULTS, "calibration.json")
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    if os.path.exists(path):
        cal = json.load(open(path))
        if cal.get("date") == today and cal.get("version") == 3:
            return cal["stations"]
    days = CFG.get("calibration_days", 60)
    actual = {}
    for st, (cli, kind) in series_info.items():
        try:
            a = settled_actuals(st, days)
        except Exception as e:  # noqa: BLE001
            log(f"calibration actuals {st} failed: {e}")
            continue
        if a:
            actual.setdefault(f"{cli}|{kind}", {}).update(a)
    clis = sorted({k.split("|")[0] for k in actual})
    end = dt.date.today()
    start = end - dt.timedelta(days=days + 1)
    hist = {}
    for i in range(0, len(clis), 8):
        chunk = clis[i:i + 8]
        try:
            d = get_json(PREV_RUNS, {
                "latitude": ",".join(str(STATIONS[c][1]) for c in chunk),
                "longitude": ",".join(str(STATIONS[c][2]) for c in chunk),
                "hourly": "temperature_2m_previous_day0,temperature_2m_previous_day1",
                "models": ",".join(DET_MODELS), "temperature_unit": "fahrenheit",
                "timezone": "GMT", "start_date": start.isoformat(), "end_date": end.isoformat(),
            })
        except Exception as e:  # noqa: BLE001
            log(f"calibration history failed: {e}")
            continue
        locs = d if isinstance(d, list) else [d]
        for c, loc in zip(chunk, locs):
            h = loc.get("hourly", {})
            times = [dt.datetime.fromisoformat(t) for t in h.get("time", [])]
            per = {}
            for lead in (0, 1):
                var = f"temperature_2m_previous_day{lead}"
                per[lead] = {m: h.get(_model_key(h.keys(), var, m) or "") for m in DET_MODELS}
            hist[c] = (times, per)
    stations = {}
    for key, acts in actual.items():
        cli, kind = key.split("|")
        if cli not in hist:
            continue
        times, per = hist[cli]
        off = STATIONS[cli][3]
        entry = {}
        for lead in (0, 1):
            errs, rows = {m: [] for m in DET_MODELS}, []
            for day, act in sorted(acts.items()):
                vals = {}
                for m, arr in per[lead].items():
                    if arr:
                        v = daily_extreme(arr, times, day, off, kind)
                        if v is not None:
                            vals[m] = v
                            errs[m].append(act - v)
                if vals:
                    rows.append((act, vals))
            bias = {m: round(sum(v) / len(v), 2) for m, v in errs.items() if len(v) >= 10}
            resid = []
            for act, vals in rows:
                corr = [vals[m] + bias[m] for m in vals if m in bias]
                if corr:
                    resid.append(act - sum(corr) / len(corr))
            if len(resid) >= 10 and bias:
                rmse = math.sqrt(sum(r * r for r in resid) / len(resid))
                mean_res = sum(resid) / len(resid)
                entry[str(lead)] = {"bias": bias, "rmse": round(rmse, 2), "n": len(resid),
                                    "resid_mean": round(mean_res, 2)}
        if entry:
            stations[key] = entry
    json.dump({"date": today, "version": 3, "days": days, "stations": stations},
              open(path, "w"), indent=1)
    log(f"calibrated {len(stations)} station/kind pairs")
    return stations


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
    det, det_times = det_forecasts(stations, CFG["days_ahead"])
    series_info = {}
    for m in markets:
        series_info[m["_series"]] = (m["_cli"], m["_kind"])
    try:
        calib = calibrate(series_info)
    except Exception as e:  # noqa: BLE001
        log(f"calibration failed: {e}")
        calib = {}

    groups = {}
    for m in markets:
        groups.setdefault((m["_series"], m["_date"]), []).append(m)

    floors = CFG["sd_floor_by_lead_days"]
    candidates, cities, snaps, suspicious = [], [], [], []
    for (st, day), ms in sorted(groups.items()):
        cli, kind = ms[0]["_cli"], ms[0]["_kind"]
        icao, _, _, off, name = STATIONS[cli]
        local_today = (now + dt.timedelta(hours=off)).date()
        lead = (day - local_today).days
        if lead < 0 or lead > CFG["days_ahead"]:
            continue
        # Same-day markets: once much of the CLI day has passed, traders see the
        # station's 5-minute readings and the models can't compete. Skip them.
        day_start, _ = cli_window_utc(day, off)
        frac_day = max(0.0, (now - day_start).total_seconds() / 86400)
        if lead == 0 and frac_day > (CFG["same_day_cutoff_low"] if kind == "low" else CFG["same_day_cutoff_high"]):
            continue
        calk = calib.get(f"{cli}|{kind}") or {}
        cal = calk.get(str(min(lead, 1)))
        # Center: high-resolution models, each corrected by its own measured bias.
        det_vals = {}
        for mdl, arr in det.get(cli, {}).items():
            v = daily_extreme(arr, det_times, day, off, kind)
            if v is not None:
                det_vals[mdl] = v + ((cal or {}).get("bias", {}).get(mdl, 0.0))
        members, _ = member_extremes(ens.get(cli, {}), times, day, off, kind)
        if not det_vals and len(members) < 10:
            log(f"{st} {day}: no model data")
            continue
        ens_mean = sum(v * w for v, w in members) if members else None
        # Spread within each ensemble (ignores the offsets between models, which the
        # bias correction already handles), averaged across models.
        sds = []
        for mdl, series_list in ens.get(cli, {}).items():
            vals = [x for x in (daily_extreme(sr, times, day, off, kind, 18) for sr in series_list) if x is not None]
            if len(vals) >= 5:
                mu = sum(vals) / len(vals)
                sds.append(math.sqrt(sum((x - mu) ** 2 for x in vals) / (len(vals) - 1)))
        ens_sd = sum(sds) / len(sds) if sds else 0.0
        model_center = (sum(det_vals.values()) / len(det_vals)) if det_vals else ens_mean
        model_spread = (max(det_vals.values()) - min(det_vals.values())) if len(det_vals) > 1 else 0.0
        periods = nws_forecast(cli, cache)
        nws = nws_value(periods, day, kind)
        center = model_center if nws is None else (CFG["nws_weight"] * nws + (1 - CFG["nws_weight"]) * model_center)
        obs_hi = obs_lo = None
        frac = 0.0
        if lead == 0:
            obs_hi, obs_lo, frac = observed_so_far(cli, day, now)
        if cal:
            sd = cal["rmse"] * (1.1 if lead <= 1 else 1.4)
        else:
            sd = floors[min(lead, len(floors) - 1)]
        # Widen when the ensembles or the models disagree more than usual.
        sd = max(1.0, sd, 0.8 * ens_sd, 0.5 * model_spread)
        target_sd = sd
        shifted, kernel = [(center, 1.0)], sd
        model_means = {k: round(v, 1) for k, v in det_vals.items()}
        if ens_mean is not None:
            model_means["ensemble_raw"] = round(ens_mean, 1)
        # Observed value bounds the outcome (minus 1F for C->F rounding of ASOS obs).
        f_obs = (round(obs_hi) - 1) if (kind == "high" and obs_hi is not None) else None
        c_obs = (round(obs_lo) + 1) if (kind == "low" and obs_lo is not None) else None

        disagreement = None if nws is None else round(nws - model_center, 1)
        conf = "high" if cal else "medium"
        if disagreement is not None and abs(disagreement) >= 3:
            conf = "low"
        elif model_spread > 5 or (disagreement is not None and abs(disagreement) >= 2):
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
                if abs(pw - px) > CFG["max_model_market_gap"]:
                    # A gap this big in a liquid market usually means the model is
                    # missing something (a front, a station quirk), not free money.
                    suspicious.append({"city": name, "date": day.isoformat(), "kind": kind,
                                       "bracket": label, "side": side, "model_prob": round(pw, 3),
                                       "market_price": px})
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
                    "model_center": round(model_center, 1), "sd": round(target_sd, 2),
                    "observed_high": None if obs_hi is None else round(obs_hi, 1),
                    "observed_low": None if obs_lo is None else round(obs_lo, 1),
                })
        fav = max(rows, key=lambda r: r["yes_ask"] or 0) if rows else None
        top = max(rows, key=lambda r: r["model_p"]) if rows else None
        cities.append({
            "city": name, "station": icao, "series": st, "date": day.isoformat(), "kind": kind,
            "lead_days": lead, "nws": nws, "model_center": round(model_center, 1),
            "model_means": model_means, "center": round(center, 1), "sd": round(target_sd, 2),
            "observed_high": None if obs_hi is None else round(obs_hi, 1),
            "observed_low": None if obs_lo is None else round(obs_lo, 1),
            "confidence": conf,
            "calibration": cal,
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
    return now, picks, candidates, cities, snaps, suspicious


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
    now, picks, candidates, cities, snaps, suspicious = analyze()
    log_picks(now, picks)
    append_csv(os.path.join(RESULTS, "snapshots.csv"),
               ["run_utc", "series", "date", "lead", "ticker", "bracket", "model_p",
                "yes_bid", "yes_ask", "mid", "volume"], snaps)
    out = {
        "run_utc": now.isoformat() + "Z", "picks": picks,
        "other_candidates": candidates[len(picks):len(picks) + 15],
        "track_record": track_record(), "markets": cities,
        "model_vs_market_too_far_apart": suspicious[:20],
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
