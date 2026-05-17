# app.py — NANO OI Dashboard | Groww Theme | Wilder RSI with multi-fallback
from flask import Flask, render_template, jsonify, request
from flask_cors import CORS
import requests
from datetime import datetime, timedelta
import logging
import os
import time
import threading
from flask_session import Session

app = Flask(__name__)
app.secret_key = os.urandom(24)
app.config['SESSION_TYPE'] = 'filesystem'
Session(app)
CORS(app)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
BASE_URL  = "https://api.dhan.co/v2"
DB_URL    = "https://trading-idea-render-default-rtdb.firebaseio.com"
CLIENT_ID = "1111417630"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# GLOBAL STATE
# ─────────────────────────────────────────────
class TradingState:
    def __init__(self):
        self.algo_running   = False
        self.current_trade  = None
        self.daily_trades   = 0
        self.daily_pnl      = 0.0
        self.emergency_stop = False
        self.candle_closes  = []   # RSI source — list of float close prices
        self.last_candle_ts = 0    # epoch seconds of last successful candle fetch
        self.spot_ring      = []   # rolling spot prices (fallback RSI source)
        self.rsi_source     = "none"  # which strategy succeeded

state = TradingState()

# ─────────────────────────────────────────────
# FIREBASE
# ─────────────────────────────────────────────
class DB:
    @staticmethod
    def _url(p): return f"{DB_URL}/{p}.json"

    @staticmethod
    def get(path):
        try:
            r = requests.get(DB._url(path), timeout=8)
            return r.json() if r.ok else None
        except Exception as e:
            logger.error(f"DB.get {e}"); return None

    @staticmethod
    def push(path, data):
        try:
            r = requests.post(DB._url(path), json=data, timeout=8)
            return r.json().get("name") if r.ok else None
        except Exception as e:
            logger.error(f"DB.push {e}"); return None

# ─────────────────────────────────────────────
# TOKEN MANAGER
# ─────────────────────────────────────────────
class Token:
    _cache  = None
    _expiry = 0

    @staticmethod
    def get():
        now_ms = time.time() * 1000
        if Token._cache and now_ms < Token._expiry - 300_000:
            return Token._cache
        try:
            r = requests.get(f"{DB_URL}/ownerToken.json", timeout=8)
            if not r.ok: return None
            d = r.json()
            if not d: return None
            tok, exp = d.get("token"), d.get("expiresAt", 0)
            if tok and now_ms < exp:
                Token._cache, Token._expiry = tok, exp
                return tok
        except Exception as e:
            logger.error(f"Token.get {e}")
        return None

    @staticmethod
    def headers():
        t = Token.get()
        if not t: return None
        return {
            "access-token":  t,
            "client-id":     CLIENT_ID,
            "Content-Type":  "application/json",
        }

    @staticmethod
    def fund_limit():
        h = Token.headers()
        if not h: return None
        try:
            r = requests.get(f"{BASE_URL}/fundlimit", headers=h, timeout=8)
            if r.ok:
                d = r.json()
                return {
                    "available_balance":    d.get("availabelBalance", 0),
                    "sod_limit":            d.get("sodLimit", 0),
                    "withdrawable_balance": d.get("withdrawableBalance", 0),
                    "utilized_amount":      d.get("utilizedAmount", 0),
                }
        except Exception as e:
            logger.error(f"fund_limit {e}")
        return None

# ─────────────────────────────────────────────
# WILDER RSI
# ─────────────────────────────────────────────
def wilder_rsi(closes, period=14):
    if not closes or len(closes) < period + 1:
        return None
    closes = list(closes)
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0: gains  += d
        else:     losses += abs(d)
    avg_g = gains  / period
    avg_l = losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = d if d > 0 else 0.0
        l = abs(d) if d < 0 else 0.0
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0
    rs  = avg_g / avg_l
    return round(100.0 - 100.0 / (1.0 + rs), 2)

# ─────────────────────────────────────────────
# CANDLE FETCH — 4 strategies
# ─────────────────────────────────────────────
def _extract_closes(obj):
    """Pull close list from various Dhan response shapes."""
    if isinstance(obj, list):
        return [float(x) for x in obj]
    for key in ("close", "c", "Close"):
        v = obj.get(key)
        if v:
            return [float(x) for x in v]
    inner = obj.get("data")
    if isinstance(inner, dict):
        for key in ("close", "c", "Close"):
            v = inner.get(key)
            if v:
                return [float(x) for x in v]
    return []

def _strat_intraday(h, today, interval="5"):
    """Dhan v2 intraday — try 5-min then 1-min."""
    for iv in [interval, "1", "15"]:
        try:
            payload = {
                "securityId":      "13",
                "exchangeSegment": "IDX_I",
                "instrument":      "INDEX",
                "interval":        iv,
                "fromDate":        today,
                "toDate":          today,
            }
            r = requests.post(f"{BASE_URL}/charts/intraday",
                              headers=h, json=payload, timeout=12)
            if r.ok:
                c = _extract_closes(r.json())
                if len(c) >= 5:
                    logger.info(f"RSI: intraday interval={iv} → {len(c)} bars")
                    return c, f"intraday-{iv}min"
        except Exception as e:
            logger.debug(f"_strat_intraday iv={iv}: {e}")
    return None, None

def _strat_historical(h):
    """Dhan historical daily — last 30 sessions."""
    try:
        today     = datetime.now().strftime("%Y-%m-%d")
        from_date = (datetime.now() - timedelta(days=50)).strftime("%Y-%m-%d")
        payload   = {
            "securityId":      "13",
            "exchangeSegment": "IDX_I",
            "instrument":      "INDEX",
            "expiryCode":      0,
            "fromDate":        from_date,
            "toDate":          today,
        }
        r = requests.post(f"{BASE_URL}/charts/historical",
                          headers=h, json=payload, timeout=12)
        if r.ok:
            c = _extract_closes(r.json())
            if len(c) >= 5:
                logger.info(f"RSI: historical daily → {len(c)} bars")
                return c[-30:], "daily"
    except Exception as e:
        logger.debug(f"_strat_historical: {e}")
    return None, None

def _strat_ltp_history(h):
    """
    Poll Dhan LTP every call and accumulate in spot_ring.
    Not a true candle but gives real price changes.
    """
    if len(state.spot_ring) >= 15:
        logger.info(f"RSI: spot-ring fallback → {len(state.spot_ring)} ticks")
        return list(state.spot_ring), "spot-ring"
    return None, None

def refresh_candles():
    """Throttled to once per minute. Tries 4 strategies in order."""
    if time.time() - state.last_candle_ts < 60:
        return

    h = Token.headers()
    if not h:
        logger.warning("refresh_candles: no auth token")
        return

    today = datetime.now().strftime("%Y-%m-%d")

    for strategy_fn in [
        lambda: _strat_intraday(h, today, "5"),
        lambda: _strat_historical(h),
        lambda: _strat_ltp_history(h),
    ]:
        c, src = strategy_fn()
        if c and len(c) >= 2:
            state.candle_closes  = c
            state.last_candle_ts = time.time()
            state.rsi_source     = src
            return

    logger.warning("refresh_candles: all strategies failed")

# ─────────────────────────────────────────────
# DHAN MARKET DATA
# ─────────────────────────────────────────────
def get_nifty_spot():
    h = Token.headers()
    if not h: return 23997.55
    try:
        r = requests.get(f"{BASE_URL}/marketfeed/nse_index/NIFTY 50", headers=h, timeout=5)
        if r.ok:
            return float(r.json().get("ltp", 23997.55))
    except: pass
    return 23997.55

def get_option_chain():
    h = Token.headers()
    if not h: return None, "no token"
    try:
        er = requests.post(
            f"{BASE_URL}/optionchain/expirylist", headers=h,
            json={"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I"}, timeout=10)
        if not er.ok: return None, f"expiry {er.status_code}"
        expiry_list = er.json().get("data", [])
        if not expiry_list: return None, "no expiries"
        expiry = sorted(expiry_list)[0]

        ocr = requests.post(
            f"{BASE_URL}/optionchain", headers=h,
            json={"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I", "Expiry": expiry},
            timeout=10)
        if not ocr.ok: return None, f"oc {ocr.status_code}"
        oc = ocr.json().get("data", {}).get("oc", {})

        rows, tot_c_oi, tot_p_oi, tot_c_ch, tot_p_ch = [], 0, 0, 0, 0
        for sk, item in oc.items():
            try:
                strike = float(sk)
                ce, pe = item.get("ce", {}), item.get("pe", {})
                c_oi   = int(ce.get("oi", 0));          p_oi   = int(pe.get("oi", 0))
                c_prev = int(ce.get("previous_oi", 0)); p_prev = int(pe.get("previous_oi", 0))
                c_ch   = c_oi - c_prev;                 p_ch   = p_oi - p_prev
                c_ltp  = float(ce.get("last_price", ce.get("ltp", 0)))
                p_ltp  = float(pe.get("last_price", pe.get("ltp", 0)))
                tot_c_oi += c_oi; tot_p_oi += p_oi
                tot_c_ch += c_ch; tot_p_ch += p_ch
                if c_oi or p_oi:
                    rows.append({
                        "strike": strike, "call_oi": c_oi, "put_oi": p_oi,
                        "call_change": c_ch, "put_change": p_ch,
                        "call_ltp": round(c_ltp, 2), "put_ltp": round(p_ltp, 2),
                    })
            except: pass
        rows.sort(key=lambda x: x["strike"])
        return {
            "expiry": expiry, "data": rows,
            "total_call_oi": tot_c_oi, "total_put_oi": tot_p_oi,
            "total_call_change": tot_c_ch, "total_put_change": tot_p_ch,
            "timestamp": datetime.now().isoformat(),
        }, None
    except Exception as e:
        return None, str(e)

# ─────────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────────
def calc_cpr(hi, lo, cl):
    pivot = (hi + lo + cl) / 3
    bc    = (hi + lo) / 2
    tc    = pivot + (pivot - bc)
    return {"pivot": round(pivot, 2), "bc": round(bc, 2), "tc": round(tc, 2)}

def cpr_signal(price, cpr, prev):
    if price > cpr["tc"]    and prev <= cpr["tc"]:    return "BULLISH BREAKOUT",  "bullish"
    if price > cpr["pivot"] and prev <= cpr["pivot"]: return "ABOVE PIVOT",       "bullish"
    if price < cpr["bc"]    and prev >= cpr["bc"]:    return "BEARISH BREAKDOWN", "bearish"
    if price < cpr["pivot"] and prev >= cpr["pivot"]: return "BELOW PIVOT",       "bearish"
    return "SIDEWAYS", "neutral"

def rsi_label(rsi):
    if rsi is None:    return "LOADING",          "neutral"
    if rsi >= 70:      return "OVERBOUGHT 🔴",    "bearish"
    if rsi <= 30:      return "OVERSOLD 🟢",      "bullish"
    if rsi >= 60:      return "NEAR OVERBOUGHT",  "mild_bearish"
    if rsi <= 40:      return "NEAR OVERSOLD",    "mild_bullish"
    return "NEUTRAL",  "neutral"

def pcr_label(pcr):
    if pcr > 1.2: return "BULLISH",      "#00b386", "Strong put writing — bulls in control"
    if pcr > 1.0: return "MILD BULLISH", "#44d7a8", "Slight put dominance"
    if pcr > 0.8: return "NEUTRAL",      "#fbbf24", "Balanced market"
    if pcr > 0.6: return "MILD BEARISH", "#f97316", "Call writers active"
    return "BEARISH",  "#ef4444",         "Bears dominating"

def max_pain(data):
    if not data: return 0
    return min(data, key=lambda sr: sum(
        max(0, sr["strike"] - r["strike"]) * r["call_oi"] +
        max(0, r["strike"]  - sr["strike"]) * r["put_oi"]
        for r in data))["strike"]

# ─────────────────────────────────────────────
# FULL ANALYSIS
# ─────────────────────────────────────────────
def full_analysis():
    oc, err = get_option_chain()
    if err or not oc:
        return {"success": False, "error": err or "no data"}

    data = oc["data"]
    spot = get_nifty_spot()

    # Update spot ring (fallback RSI source)
    state.spot_ring.append(spot)
    if len(state.spot_ring) > 60:
        state.spot_ring.pop(0)

    # Refresh candle cache
    refresh_candles()

    # RSI
    rsi       = wilder_rsi(state.candle_closes, 14)
    rl, rt    = rsi_label(rsi)
    n_candles = len(state.candle_closes)
    rsi_note  = (
        f"Wilder(14) · {n_candles} bars · source: {state.rsi_source}"
        if rsi is not None
        else f"Waiting… {n_candles}/15 bars loaded (source: {state.rsi_source})"
    )

    # CPR
    if n_candles >= 3:
        hi, lo, cl  = max(state.candle_closes), min(state.candle_closes), state.candle_closes[-1]
        prev_cl     = state.candle_closes[-2]
    else:
        hi, lo, cl, prev_cl = spot+50, spot-50, spot, spot
    cpr    = calc_cpr(hi, lo, cl)
    cs, ct = cpr_signal(spot, cpr, prev_cl)

    # PCR
    pcr         = round(oc["total_put_oi"] / oc["total_call_oi"], 4) if oc["total_call_oi"] else 1.0
    pl, pc, pn  = pcr_label(pcr)

    # Levels
    atm        = min(data, key=lambda x: abs(x["strike"] - spot))["strike"] if data else 0
    support    = max(data, key=lambda x: x["put_oi"])["strike"] if data else 0
    resistance = max(data, key=lambda x: x["call_oi"])["strike"] if data else 0
    mc_strike  = resistance
    mp_strike  = support
    mp_val     = max_pain(data)

    # Nearby OI for chart/table
    nearby = sorted([x for x in data if abs(x["strike"] - spot) <= 1000],
                    key=lambda x: x["strike"])

    # Best signal
    rsi_v      = rsi if rsi is not None else 50
    best, bscore = None, 0
    for row in nearby:
        for opt_type, ltp_key, oi_key, ch_key in [
            ("CALL", "call_ltp", "call_oi", "call_change"),
            ("PUT",  "put_ltp",  "put_oi",  "put_change"),
        ]:
            ltp = row[ltp_key]
            if not (15 <= ltp <= 40):
                continue
            s = 0
            if opt_type == "CALL":
                if pcr > 1.0: s += 25
                if row["put_change"] > row["call_change"]: s += 20
                if row["put_oi"] > row["call_oi"]: s += 15
                if abs(row["strike"] - support) <= 50: s += 15
                if rsi_v < 40: s += 15
                if ct == "bullish": s += 10
            else:
                if pcr < 1.0: s += 25
                if row["call_change"] > row["put_change"]: s += 20
                if row["call_oi"] > row["put_oi"]: s += 15
                if abs(row["strike"] - resistance) <= 50: s += 15
                if rsi_v > 60: s += 15
                if ct == "bearish": s += 10
            if s > bscore:
                bscore = s
                best = {
                    "type":       opt_type,
                    "strike":     row["strike"],
                    "entry":      ltp,
                    "target":     round(ltp + 3, 2),
                    "stoploss":   round(max(ltp - 2, 1), 2),
                    "confidence": min(s + 20, 100),
                    "oi_change":  round(row[ch_key] / 100000, 2),
                }

    def lakh(v): return round(v / 100000, 2)

    return {
        "success":    True,
        "expiry":     oc["expiry"],
        "spot":       round(spot, 2),
        "timestamp":  oc["timestamp"],
        # PCR
        "pcr": pcr, "pcr_label": pl, "pcr_color": pc, "pcr_note": pn,
        "total_call_oi":     lakh(oc["total_call_oi"]),
        "total_put_oi":      lakh(oc["total_put_oi"]),
        "total_call_change": lakh(oc["total_call_change"]),
        "total_put_change":  lakh(oc["total_put_change"]),
        # RSI
        "rsi": rsi, "rsi_label": rl, "rsi_trend": rt, "rsi_note": rsi_note,
        "rsi_candles": n_candles,
        # CPR
        "cpr": cpr, "cpr_signal": cs, "cpr_trend": ct,
        # Levels
        "atm": atm, "support": support, "resistance": resistance,
        "max_call_strike": mc_strike, "max_put_strike": mp_strike,
        "max_pain": mp_val,
        # Best trade
        "best_option": best,
        # OI table/chart data
        "oi_data": [{
            "strike":      r["strike"],
            "call_oi":     lakh(r["call_oi"]),
            "put_oi":      lakh(r["put_oi"]),
            "call_change": lakh(r["call_change"]),
            "put_change":  lakh(r["put_change"]),
            "call_ltp":    r["call_ltp"],
            "put_ltp":     r["put_ltp"],
        } for r in nearby[-30:]],
    }

# ─────────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/market-data")
def api_market_data():
    return jsonify(full_analysis())

@app.route("/api/fund-limit")
def api_fund_limit():
    d = Token.fund_limit()
    return jsonify({"success": bool(d), "data": d or {}})

@app.route("/api/status")
def api_status():
    return jsonify({
        "algo_running":   state.algo_running,
        "emergency_stop": state.emergency_stop,
        "current_trade":  state.current_trade,
        "daily_trades":   state.daily_trades,
        "daily_pnl":      round(state.daily_pnl, 2),
    })

@app.route("/api/start-algo",     methods=["POST"])
def start_algo():
    if state.emergency_stop:
        return jsonify({"success": False, "msg": "emergency stop active"})
    state.algo_running = True
    return jsonify({"success": True})

@app.route("/api/stop-algo",      methods=["POST"])
def stop_algo():
    state.algo_running = False
    return jsonify({"success": True})

@app.route("/api/emergency-stop", methods=["POST"])
def emg_stop():
    state.algo_running  = False
    state.emergency_stop = True
    state.current_trade = None
    return jsonify({"success": True})

@app.route("/api/reset",          methods=["POST"])
def reset():
    state.algo_running  = False
    state.emergency_stop = False
    state.current_trade = None
    state.daily_trades  = 0
    state.daily_pnl     = 0.0
    return jsonify({"success": True})

@app.route("/api/trades")
def api_trades():
    logs = DB.get("trade_logs") or {}
    out  = sorted(
        [{"id": k, "timestamp": v.get("timestamp", 0),
          "type": v.get("type"), "strike": v.get("strike"),
          "entry": v.get("entry"), "pnl": v.get("pnl", 0),
          "status": v.get("status")}
         for k, v in logs.items()],
        key=lambda x: x["timestamp"], reverse=True)
    return jsonify(out[:20])

@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "candles": len(state.candle_closes),
        "rsi_source": state.rsi_source,
        "timestamp": datetime.now().isoformat(),
    })

# ─────────────────────────────────────────────
# BACKGROUND ALGO THREAD
# ─────────────────────────────────────────────
def bg_loop():
    while True:
        try:
            if state.algo_running and not state.emergency_stop:
                a = full_analysis()
                if a.get("success") and a.get("best_option"):
                    b = a["best_option"]
                    if b["confidence"] >= 55 and not state.current_trade:
                        state.current_trade = b
                        state.daily_trades += 1
                        DB.push("trade_logs", {
                            "timestamp": int(time.time()),
                            "type": b["type"], "strike": b["strike"],
                            "entry": b["entry"], "status": "executed",
                        })
                        logger.info(f"TRADE: {b['type']} {b['strike']} @ ₹{b['entry']}")
        except Exception as e:
            logger.error(f"bg_loop: {e}")
        time.sleep(15)

threading.Thread(target=bg_loop, daemon=True).start()

# ─────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n{'='*55}\n  NANO OI  |  Groww Theme  |  PORT {port}\n{'='*55}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
