from flask import Flask, render_template, jsonify
from flask_cors import CORS
import requests
from datetime import datetime
import logging
import os

app = Flask(__name__)
CORS(app)

# =========================================================
# CONFIG
# =========================================================

BASE_URL = "https://api.dhan.co/v2"
DATABASE_URL = "https://trading-idea-render-default-rtdb.firebaseio.com/ownerToken.json"
CLIENT_ID = "1111417630"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =========================================================
# FETCH TOKEN
# =========================================================

def fetch_token_from_firebase():

    try:

        response = requests.get(
            DATABASE_URL,
            timeout=10
        )

        if response.status_code != 200:
            return None

        token_data = response.json()

        if not token_data:
            return None

        token = token_data.get("token")

        expires_at = token_data.get(
            "expiresAt",
            0
        )

        current_time = datetime.now().timestamp() * 1000

        if current_time > expires_at:
            logger.error("Token Expired")
            return None

        return token

    except Exception as e:

        logger.error(f"Token Error : {e}")

        return None

# =========================================================
# MARKET SIGNAL
# =========================================================

def get_signal(pcr):

    if pcr > 1.2:

        return {
            "signal": "BULLISH 📈",
            "color": "#22c55e",
            "strength": "85%"
        }

    elif pcr < 0.8:

        return {
            "signal": "BEARISH 📉",
            "color": "#ef4444",
            "strength": "85%"
        }

    return {
        "signal": "SIDEWAYS ⚖️",
        "color": "#facc15",
        "strength": "55%"
    }

# =========================================================
# SMART MONEY SIGNAL
# =========================================================

def ai_signal(call_change, put_change):

    if put_change > 0 and call_change < 0:
        return "STRONG BULLISH 🚀"

    elif call_change > 0 and put_change < 0:
        return "STRONG BEARISH 🔻"

    elif put_change > call_change:
        return "BULLISH 📈"

    elif call_change > put_change:
        return "BEARISH 📉"

    return "SIDEWAYS ⚖️"

# =========================================================
# MAX PAIN
# =========================================================

def calculate_max_pain(data):

    pain_data = []

    for strike_row in data:

        strike = strike_row["strike"]

        total_pain = 0

        for row in data:

            call_loss = max(
                0,
                strike - row["strike"]
            ) * row["call_oi"]

            put_loss = max(
                0,
                row["strike"] - strike
            ) * row["put_oi"]

            total_pain += (
                call_loss + put_loss
            )

        pain_data.append({

            "strike": strike,
            "pain": total_pain

        })

    min_pain = min(
        pain_data,
        key=lambda x: x["pain"]
    )

    return min_pain["strike"], pain_data

# =========================================================
# BEST OPTION
# =========================================================

def find_best_option(
    data,
    pcr,
    support,
    resistance
):

    best_trade = None

    highest_score = 0

    for row in data:

        # =====================================================
        # CALL BUY
        # =====================================================

        if 20 <= row["call_ltp"] <= 35:

            score = 0

            reasons = []

            if pcr > 1:
                score += 25
                reasons.append("PCR Bullish")

            if row["put_change"] > row["call_change"]:
                score += 25
                reasons.append("Put Writing")

            if row["put_oi"] > row["call_oi"]:
                score += 25
                reasons.append("Strong Put OI")

            if abs(row["strike"] - support) <= 100:
                score += 25
                reasons.append("Near Support")

            if score > highest_score:

                highest_score = score

                best_trade = {

                    "type": "CALL BUY",

                    "strike": row["strike"],

                    "entry": round(
                        row["call_ltp"],
                        2
                    ),

                    "target": round(
                        row["call_ltp"] + 3,
                        2
                    ),

                    "stoploss": round(
                        max(row["call_ltp"] - 2, 1),
                        2
                    ),

                    "confidence": f"{score}%",

                    "reasons": reasons
                }

        # =====================================================
        # PUT BUY
        # =====================================================

        if 20 <= row["put_ltp"] <= 35:

            score = 0

            reasons = []

            if pcr < 1:
                score += 25
                reasons.append("PCR Bearish")

            if row["call_change"] > row["put_change"]:
                score += 25
                reasons.append("Call Writing")

            if row["call_oi"] > row["put_oi"]:
                score += 25
                reasons.append("Strong Call OI")

            if abs(row["strike"] - resistance) <= 100:
                score += 25
                reasons.append("Near Resistance")

            if score > highest_score:

                highest_score = score

                best_trade = {

                    "type": "PUT BUY",

                    "strike": row["strike"],

                    "entry": round(
                        row["put_ltp"],
                        2
                    ),

                    "target": round(
                        row["put_ltp"] + 3,
                        2
                    ),

                    "stoploss": round(
                        max(row["put_ltp"] - 2, 1),
                        2
                    ),

                    "confidence": f"{score}%",

                    "reasons": reasons
                }

    return best_trade

# =========================================================
# OPTION DATA
# =========================================================

def get_option_data():

    try:

        token = fetch_token_from_firebase()

        if not token:
            return None, "No valid token"

        headers = {

            "access-token": token,

            "client-id": CLIENT_ID,

            "Content-Type": "application/json"
        }

        # =====================================================
        # EXPIRY
        # =====================================================

        expiry_response = requests.post(

            f"{BASE_URL}/optionchain/expirylist",

            headers=headers,

            json={
                "UnderlyingScrip": 13,
                "UnderlyingSeg": "IDX_I"
            },

            timeout=10
        )

        expiry_json = expiry_response.json()

        expiry_list = expiry_json.get(
            "data",
            []
        )

        if not expiry_list:
            return None, "No Expiry Found"

        expiry = sorted(expiry_list)[0]

        # =====================================================
        # OPTION CHAIN
        # =====================================================

        option_response = requests.post(

            f"{BASE_URL}/optionchain",

            headers=headers,

            json={
                "UnderlyingScrip": 13,
                "UnderlyingSeg": "IDX_I",
                "Expiry": expiry
            },

            timeout=10
        )

        option_json = option_response.json()

        oc = option_json.get(
            "data",
            {}
        ).get(
            "oc",
            {}
        )

        processed_data = []

        for strike_str, item in oc.items():

            try:

                strike = float(strike_str)

                ce = item.get("ce", {})
                pe = item.get("pe", {})

                call_oi = int(
                    ce.get("oi", 0)
                )

                put_oi = int(
                    pe.get("oi", 0)
                )

                call_prev = int(
                    ce.get("previous_oi", 0)
                )

                put_prev = int(
                    pe.get("previous_oi", 0)
                )

                call_change = (
                    call_oi - call_prev
                )

                put_change = (
                    put_oi - put_prev
                )

                call_ltp = float(

                    ce.get(
                        "last_price",

                        ce.get(
                            "ltp",
                            0
                        )
                    )
                )

                put_ltp = float(

                    pe.get(
                        "last_price",

                        pe.get(
                            "ltp",
                            0
                        )
                    )
                )

                if (

                    call_oi == 0 and
                    put_oi == 0 and
                    call_ltp == 0 and
                    put_ltp == 0

                ):
                    continue

                processed_data.append({

                    "strike": strike,

                    "call_oi": call_oi,

                    "put_oi": put_oi,

                    "call_change": call_change,

                    "put_change": put_change,

                    "call_ltp": round(call_ltp, 2),

                    "put_ltp": round(put_ltp, 2)

                })

            except Exception as e:

                logger.error(f"Strike Error : {e}")

        processed_data.sort(
            key=lambda x: x["strike"]
        )

        if not processed_data:
            return None, "No Data"

        # =====================================================
        # TOTALS
        # =====================================================

        total_call_oi = sum(
            x["call_oi"]
            for x in processed_data
        )

        total_put_oi = sum(
            x["put_oi"]
            for x in processed_data
        )

        total_call_change = sum(
            x["call_change"]
            for x in processed_data
        )

        total_put_change = sum(
            x["put_change"]
            for x in processed_data
        )

        pcr = round(

            total_put_oi /
            total_call_oi,

            2

        ) if total_call_oi > 0 else 0

        # =====================================================
        # SUPPORT / RESISTANCE
        # =====================================================

        support = max(

            processed_data,

            key=lambda x: x["put_oi"]

        )["strike"]

        resistance = max(

            processed_data,

            key=lambda x: x["call_oi"]

        )["strike"]

        # =====================================================
        # SIGNAL
        # =====================================================

        market_signal = get_signal(pcr)

        smart_money_signal = ai_signal(
            total_call_change,
            total_put_change
        )

        # =====================================================
        # MAX PAIN
        # =====================================================

        max_pain, pain_chart = calculate_max_pain(
            processed_data
        )

        # =====================================================
        # BEST OPTION
        # =====================================================

        best_option = find_best_option(

            processed_data,

            pcr,

            support,

            resistance
        )

        # =====================================================
        # DUMMY VALUES FOR UI
        # =====================================================

        rsi = round(pcr * 50, 2)

        ema = support

        vwap = resistance

        # =====================================================
        # RETURN
        # =====================================================

        return {

            "success": True,

            "expiry": expiry,

            "timestamp": datetime.now().isoformat(),

            "pcr": pcr,

            "support": support,

            "resistance": resistance,

            "signal": market_signal,

            "smart_money_signal": smart_money_signal,

            "rsi": rsi,

            "ema": ema,

            "vwap": vwap,

            "max_pain": max_pain,

            "best_option": best_option,

            "total_call_oi": total_call_oi,

            "total_put_oi": total_put_oi,

            "total_call_change": total_call_change,

            "total_put_change": total_put_change,

            "pain_chart": pain_chart,

            "data": processed_data[-30:]

        }, None

    except Exception as e:

        logger.error(f"Error : {e}")

        return None, str(e)

# =========================================================
# ROUTES
# =========================================================

@app.route("/")
def index():

    return render_template("index.html")

@app.route("/api/market-data")
def market_data():

    data, error = get_option_data()

    if error:

        return jsonify({
            "success": False,
            "error": error
        })

    return jsonify(data)

@app.route("/api/health")
def health():

    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat()
    })

# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    port = int(
        os.environ.get("PORT", 5000)
    )

    print("=" * 60)
    print("🚀 NIFTY OPTION CHAIN SERVER STARTED")
    print(f"📍 PORT : {port}")
    print("=" * 60)

   app.run(host="0.0.0.0", port=port, debug=False)
