import os
import json
import math
import sqlite3
from datetime import datetime, timezone

import requests
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from langchain_groq import ChatGroq

# ============================================================
# AI CONSENSUS TRADING V5
# TradingAgents-style multi-agent crypto research system
# - Binance public market data
# - 4 specialist analysts
# - Bull vs Bear research debate
# - Research Manager
# - Trader
# - 3 Risk agents
# - Portfolio Manager / Final Judge
# - Persistent SQLite memory
# - Paper trading + performance tracking
# - Optional Telegram
#
# NOTE:
# This is an original implementation inspired by the
# multi-agent research structure discussed from TradingAgents.
# It is NOT a copy of the TradingAgents source code.
# ============================================================

st.set_page_config(
    page_title="AI Consensus Trading V5",
    page_icon="🧠",
    layout="wide",
)

BINANCE = "https://data-api.binance.vision"
DB_FILE = "trading_v5.db"

# -------------------------
# Secrets
# -------------------------
def secret(key):
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key, "")

GROQ_KEY = secret("GROQ_API_KEY")
TG_TOKEN = secret("TELEGRAM_BOT_TOKEN")
TG_CHAT = secret("TELEGRAM_CHAT_ID")
GROQ_MODEL = secret("GROQ_MODEL") or "openai/gpt-oss-120b"

# -------------------------
# Database
# -------------------------
def db():
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        scanner_score REAL,
        final_decision TEXT NOT NULL,
        confidence REAL,
        entry REAL,
        stop_loss REAL,
        take_profit REAL,
        risk_reward REAL,
        market_regime TEXT,
        score_consensus REAL,
        reasoning TEXT,
        closed_at TEXT,
        result TEXT,
        r_multiple REAL,
        exit_price REAL
    );

    CREATE TABLE IF NOT EXISTS agent_votes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id INTEGER NOT NULL,
        agent TEXT NOT NULL,
        decision TEXT NOT NULL,
        confidence REAL,
        reasoning TEXT,
        FOREIGN KEY(signal_id) REFERENCES signals(id)
    );

    CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        symbol TEXT,
        memory_type TEXT NOT NULL,
        content TEXT NOT NULL,
        signal_id INTEGER
    );
    """)
    con.commit()
    con.close()

init_db()

# -------------------------
# Binance public API
# -------------------------
def bget(path, params=None):
    r = requests.get(
        BINANCE + path,
        params=params,
        timeout=15,
        headers={
            "User-Agent": "AI-Consensus-Trading-V5",
            "Accept": "application/json",
        },
    )
    if r.status_code != 200:
        detail = r.text[:500].replace("\n", " ")
        raise RuntimeError(f"Binance HTTP {r.status_code}: {detail}")
    return r.json()

def binance_health():
    try:
        bget("/api/v3/ping")
        return True, "Binance public data API ONLINE"
    except Exception as e:
        return False, str(e)

# -------------------------
# Indicators
# -------------------------
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def rsi(s, n=14):
    d = s.diff()
    gain = d.where(d > 0, 0).ewm(alpha=1/n, adjust=False).mean()
    loss = (-d.where(d < 0, 0)).ewm(alpha=1/n, adjust=False).mean()
    rs = gain / loss.replace(0, pd.NA)
    return 100 - 100 / (1 + rs)

def atr(df, n=14):
    tr = pd.concat([
        df.High - df.Low,
        (df.High - df.Close.shift()).abs(),
        (df.Low - df.Close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(n).mean()

def macd(s):
    line = ema(s, 12) - ema(s, 26)
    signal = ema(line, 9)
    return line, signal, line - signal

def bollinger(s, n=20):
    mid = s.rolling(n).mean()
    sd = s.rolling(n).std()
    return mid, mid + 2 * sd, mid - 2 * sd

def klines(symbol, interval="15m", limit=180):
    x = bget(
        "/api/v3/klines",
        {"symbol": symbol, "interval": interval, "limit": limit},
    )

    cols = [
        "time", "Open", "High", "Low", "Close", "Volume",
        "close_time", "QuoteVolume", "Trades",
        "TakerBuyBase", "TakerBuyQuote", "ignore"
    ]

    d = pd.DataFrame(x, columns=cols)

    numeric = [
        "Open", "High", "Low", "Close", "Volume",
        "QuoteVolume", "Trades", "TakerBuyBase", "TakerBuyQuote"
    ]
    for c in numeric:
        d[c] = pd.to_numeric(d[c], errors="coerce")

    d.index = pd.to_datetime(d.time, unit="ms", utc=True)

    d["EMA20"] = ema(d.Close, 20)
    d["EMA50"] = ema(d.Close, 50)
    d["EMA200"] = ema(d.Close, 200)
    d["RSI"] = rsi(d.Close)
    d["MACD"], d["MACD_SIGNAL"], d["MACD_HIST"] = macd(d.Close)
    d["ATR"] = atr(d)
    d["BB_MID"], d["BB_UP"], d["BB_LOW"] = bollinger(d.Close)
    d["VOL_RATIO"] = d.Volume / d.Volume.rolling(20).mean()
    d["BUY_PRESSURE"] = (
        d.TakerBuyQuote /
        d.QuoteVolume.replace(0, pd.NA)
    )

    return d.dropna()

def order_book(symbol, limit=20):
    x = bget("/api/v3/depth", {"symbol": symbol, "limit": limit})

    bid = sum(float(q) for _, q in x.get("bids", []))
    ask = sum(float(q) for _, q in x.get("asks", []))
    total = bid + ask

    imbalance = (bid - ask) / total if total else 0

    best_bid = float(x["bids"][0][0]) if x.get("bids") else 0
    best_ask = float(x["asks"][0][0]) if x.get("asks") else 0

    spread = (
        (best_ask - best_bid) / best_bid * 100
        if best_bid else 0
    )

    return {
        "imbalance": imbalance,
        "spread": spread,
        "bid_depth": bid,
        "ask_depth": ask,
    }

def candle_info(d):
    a = d.iloc[-1]
    p = d.iloc[-2]

    body = abs(a.Close - a.Open)
    rng = max(a.High - a.Low, 1e-12)

    upper = a.High - max(a.Open, a.Close)
    lower = min(a.Open, a.Close) - a.Low

    body_ratio = body / rng

    if body_ratio < 0.10:
        name = "DOJI"
    elif lower > body * 2 and upper < max(body, 1e-12):
        name = "HAMMER BULLISH"
    elif upper > body * 2 and lower < max(body, 1e-12):
        name = "SHOOTING STAR BEARISH"
    elif (
        a.Close > a.Open and
        p.Close < p.Open and
        a.Open <= p.Close and
        a.Close >= p.Open
    ):
        name = "BULLISH ENGULFING"
    elif (
        a.Close < a.Open and
        p.Close > p.Open and
        a.Open >= p.Close and
        a.Close <= p.Open
    ):
        name = "BEARISH ENGULFING"
    elif a.Close > a.Open:
        name = "BULLISH"
    elif a.Close < a.Open:
        name = "BEARISH"
    else:
        name = "NETRAL"

    return {
        "name": name,
        "body": body_ratio,
        "upper_wick": upper / rng,
        "lower_wick": lower / rng,
    }

def market_regime(d):
    a = d.iloc[-1]
    vol = float(a.ATR / a.Close)
    ema_gap = abs(a.EMA20 - a.EMA50) / a.Close

    if vol >= 0.03:
        return "VOLATILE"
    if ema_gap < 0.005:
        return "SIDEWAYS"
    return "TRENDING"

# -------------------------
# Scanner
# -------------------------
def scanner_score(symbol, change, qvol, d, ob):
    a = d.iloc[-1]
    score = 50
    why = []

    if change >= 3:
        score += 12
        why.append("harga naik kuat")
    elif change >= 1:
        score += 6
        why.append("momentum positif")
    elif change <= -3:
        score -= 12
        why.append("harga turun kuat")
    elif change <= -1:
        score -= 6
        why.append("momentum negatif")

    vr = float(a.VOL_RATIO)
    if vr >= 3:
        score += 18
        why.append("volume sangat tinggi")
    elif vr >= 2:
        score += 12
        why.append("volume meningkat")
    elif vr >= 1.5:
        score += 6

    buy = float(a.BUY_PRESSURE)
    if buy >= 0.60:
        score += 12
        why.append("taker-buy dominan")
    elif buy >= 0.55:
        score += 6
    elif buy <= 0.40:
        score -= 12
        why.append("taker-sell dominan")

    if ob["imbalance"] >= 0.20:
        score += 8
        why.append("bid depth lebih tebal")
    elif ob["imbalance"] <= -0.20:
        score -= 8
        why.append("ask depth lebih tebal")

    r = float(a.RSI)
    if 50 <= r <= 68:
        score += 6
    elif r > 78:
        score -= 8

    return max(0, min(100, round(score))), why

def scan_markets(n=60):
    tickers = bget("/api/v3/ticker/24hr")
    candidates = []

    stable_like = {
        "USDC", "FDUSD", "TUSD", "USDP",
        "DAI", "BUSD", "USDE"
    }

    for t in tickers:
        symbol = t.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue

        base = symbol[:-4]
        if base in stable_like:
            continue

        qvol = float(t.get("quoteVolume", 0) or 0)
        if qvol < 2_000_000:
            continue

        change = float(t.get("priceChangePercent", 0) or 0)

        candidates.append((symbol, change, qvol))

    candidates.sort(key=lambda x: x[2], reverse=True)
    candidates = candidates[:n]

    out = []
    progress = st.progress(0)
    status = st.empty()

    for i, (symbol, change, qvol) in enumerate(candidates):
        status.write(f"🔎 Memindai {symbol} ({i+1}/{len(candidates)})")

        try:
            d = klines(symbol, "15m", 100)
            ob = order_book(symbol)

            score, why = scanner_score(
                symbol, change, qvol, d, ob
            )

            a = d.iloc[-1]

            out.append({
                "symbol": symbol,
                "change": change,
                "qvol": qvol,
                "vr": float(a.VOL_RATIO),
                "buy": float(a.BUY_PRESSURE),
                "imb": float(ob["imbalance"]),
                "spread": float(ob["spread"]),
                "rsi": float(a.RSI),
                "score": score,
                "why": why,
            })

        except Exception:
            pass

        progress.progress((i + 1) / max(len(candidates), 1))

    progress.empty()
    status.empty()

    return sorted(
        out,
        key=lambda x: x["score"],
        reverse=True,
    )

# -------------------------
# AI engine
# -------------------------
def model():
    if not GROQ_KEY:
        return None

    return ChatGroq(
        api_key=GROQ_KEY,
        model=GROQ_MODEL,
        temperature=0.1,
    )

def call_ai(role, data, instructions):
    m = model()

    if not m:
        return {
            "decision": "WAIT",
            "confidence": 0,
            "reasoning": "GROQ_API_KEY belum dikonfigurasi.",
        }

    prompt = f"""
Kamu adalah {role} dalam sebuah ruang riset trading profesional.

ATURAN KERAS:
- Gunakan HANYA data yang diberikan.
- Jangan mengarang data, berita, harga, volume, atau indikator.
- Jangan menjanjikan profit.
- Jika bukti tidak cukup, pilih WAIT.
- Jawaban wajib Bahasa Indonesia.
- Kamu adalah satu agen, bukan hakim final.

DATA MARKET:
{data}

TUGAS KHUSUS:
{instructions}

Balas dalam format JSON SAJA:
{{
  "decision": "LONG atau SHORT atau WAIT",
  "confidence": 0-100,
  "reasoning": "alasan singkat",
  "key_risk": "risiko utama"
}}
"""

    try:
        raw = m.invoke(prompt).content.strip()

        # Cari blok JSON jika model menambahkan markdown.
        if "{" in raw and "}" in raw:
            raw = raw[raw.find("{"):raw.rfind("}") + 1]

        obj = json.loads(raw)

        decision = str(obj.get("decision", "WAIT")).upper()
        if decision not in {"LONG", "SHORT", "WAIT"}:
            decision = "WAIT"

        return {
            "decision": decision,
            "confidence": float(obj.get("confidence", 0) or 0),
            "reasoning": str(obj.get("reasoning", "")),
            "key_risk": str(obj.get("key_risk", "")),
        }

    except Exception as e:
        return {
            "decision": "WAIT",
            "confidence": 0,
            "reasoning": f"AI tidak dapat menghasilkan output terstruktur: {e}",
            "key_risk": "Output AI gagal diproses.",
        }

# -------------------------
# Market packet
# -------------------------
def market_packet(symbol):
    frames = {}

    for tf, limit in [
        ("5m", 160),
        ("15m", 180),
        ("1h", 180),
    ]:
        frames[tf] = klines(symbol, tf, limit)

    ob = order_book(symbol)
    d = frames["15m"]

    c = candle_info(d)
    a = d.iloc[-1]

    support = float(d.Low.tail(30).min())
    resistance = float(d.High.tail(30).max())

    packet = {
        "symbol": symbol,
        "price": float(a.Close),
        "market_regime": market_regime(d),
        "order_book": ob,
        "support": support,
        "resistance": resistance,
        "candlestick": c,
        "timeframes": {},
    }

    for tf, df in frames.items():
        x = df.iloc[-1]

        packet["timeframes"][tf] = {
            "price": float(x.Close),
            "ema20": float(x.EMA20),
            "ema50": float(x.EMA50),
            "ema200": float(x.EMA200),
            "rsi": float(x.RSI),
            "macd_hist": float(x.MACD_HIST),
            "atr": float(x.ATR),
            "vol_ratio": float(x.VOL_RATIO),
            "buy_pressure": float(x.BUY_PRESSURE),
            "bb_upper": float(x.BB_UP),
            "bb_lower": float(x.BB_LOW),
        }

    return frames, packet

def packet_text(packet):
    lines = [
        f"Symbol: {packet['symbol']}",
        f"Harga: ${packet['price']:.8f}",
        f"Market regime: {packet['market_regime']}",
        f"Support 30 candle: ${packet['support']:.8f}",
        f"Resistance 30 candle: ${packet['resistance']:.8f}",
        f"Order book imbalance: {packet['order_book']['imbalance']:+.2%}",
        f"Spread: {packet['order_book']['spread']:.3f}%",
        f"Candlestick: {packet['candlestick']['name']}",
        f"Body: {packet['candlestick']['body']:.1%}",
        f"Upper wick: {packet['candlestick']['upper_wick']:.1%}",
        f"Lower wick: {packet['candlestick']['lower_wick']:.1%}",
        "",
    ]

    for tf, x in packet["timeframes"].items():
        lines.extend([
            f"[TIMEFRAME {tf}]",
            f"EMA20={x['ema20']:.8f}",
            f"EMA50={x['ema50']:.8f}",
            f"EMA200={x['ema200']:.8f}",
            f"RSI={x['rsi']:.2f}",
            f"MACD_HIST={x['macd_hist']:.8f}",
            f"ATR={x['atr']:.8f}",
            f"VOL_RATIO={x['vol_ratio']:.2f}x",
            f"TAKER_BUY_PRESSURE={x['buy_pressure']:.1%}",
            f"BB_UP={x['bb_upper']:.8f}",
            f"BB_LOW={x['bb_lower']:.8f}",
            "",
        ])

    return "\n".join(lines)

# -------------------------
# TradingAgents-style orchestration
# -------------------------
ANALYSTS = [
    (
        "🧭 Technical Analyst",
        "Fokus trend multi-timeframe, EMA20/50/200, MACD, support/resistance, dan market regime."
    ),
    (
        "⚡ Momentum Analyst",
        "Fokus momentum, RSI, volume ratio, taker-buy pressure, acceleration, dan konfirmasi 5m/15m/1h."
    ),
    (
        "📚 Order Flow Analyst",
        "Fokus order-book imbalance, spread, liquidity, taker-buy pressure, dan apakah pergerakan didukung flow."
    ),
    (
        "🕯️ Price Action Analyst",
        "Fokus candlestick, wick, rejection, breakout, support/resistance, dan struktur harga."
    ),
]

def run_multi_agent(symbol, scanner_score_value):
    frames, packet = market_packet(symbol)
    data = packet_text(packet)

    analyst_results = []

    for name, rule in ANALYSTS:
        result = call_ai(
            name,
            data,
            rule + (
                "\nJangan membuat keputusan final portfolio. "
                "Berikan thesis yang bisa diserang oleh Bull dan Bear researcher."
            ),
        )
        analyst_results.append({
            "agent": name,
            **result,
        })

    analyst_summary = "\n\n".join(
        f"{x['agent']}\n"
        f"Decision={x['decision']}\n"
        f"Confidence={x['confidence']:.0f}\n"
        f"Reasoning={x['reasoning']}\n"
        f"Risk={x['key_risk']}"
        for x in analyst_results
    )

    bull = call_ai(
        "🐂 Bull Researcher",
        data + "\n\nHASIL ANALYST:\n" + analyst_summary,
        """
Bangun kasus paling kuat untuk LONG.
Kamu harus menyerang kelemahan argumen bearish dan menunjukkan bukti
yang mendukung kenaikan. Jangan mengabaikan risiko.
Jika data tidak cukup, kamu boleh WAIT.
""",
    )

    bear = call_ai(
        "🐻 Bear Researcher",
        data + "\n\nHASIL ANALYST:\n" + analyst_summary,
        """
Bangun kasus paling kuat untuk SHORT atau WAIT.
Cari overextension, resistance, volume yang tidak sehat,
order-flow yang berlawanan, dan risiko false breakout.
Serang argumen Bull secara spesifik.
""",
    )

    debate = (
        f"BULL RESEARCHER:\n{json.dumps(bull, ensure_ascii=False)}\n\n"
        f"BEAR RESEARCHER:\n{json.dumps(bear, ensure_ascii=False)}"
    )

    research_manager = call_ai(
        "🧠 Research Manager",
        data + "\n\nANALYST TEAM:\n" + analyst_summary + "\n\nDEBAT:\n" + debate,
        """
Nilai kualitas bukti dari seluruh analyst dan debat Bull vs Bear.
Tentukan thesis riset yang paling masuk akal.
Jangan menjadi portfolio manager. Fokus pada kualitas bukti,
kontradiksi, dan kondisi yang harus dipenuhi sebelum entry.
""",
    )

    trader = call_ai(
        "💹 Trader",
        data
        + "\n\nRESEARCH MANAGER:\n"
        + json.dumps(research_manager, ensure_ascii=False)
        + "\n\nBULL:\n"
        + json.dumps(bull, ensure_ascii=False)
        + "\n\nBEAR:\n"
        + json.dumps(bear, ensure_ascii=False),
        """
Ubah thesis menjadi rencana paper-trade.
Pilih LONG, SHORT, atau WAIT.
Jelaskan entry logic, invalidation, dan risk/reward.
Belum boleh memutuskan portfolio final.
""",
    )

    risk_agents = []

    risk_specs = [
        (
            "🟢 Aggressive Risk Manager",
            "Cari peluang dengan toleransi risiko lebih tinggi, tetapi tetap cek invalidation."
        ),
        (
            "🟡 Neutral Risk Manager",
            "Cari keseimbangan peluang vs risiko dan validasi risk/reward."
        ),
        (
            "🔴 Conservative Risk Manager",
            "Prioritaskan perlindungan modal, false breakout, spread, volatilitas, dan invalidation."
        ),
    ]

    risk_input = (
        data
        + "\n\nRESEARCH:\n"
        + json.dumps(research_manager, ensure_ascii=False)
        + "\n\nTRADER PLAN:\n"
        + json.dumps(trader, ensure_ascii=False)
    )

    for name, rule in risk_specs:
        result = call_ai(
            name,
            risk_input,
            rule + "\nKamu boleh menolak rencana Trader jika risikonya tidak layak.",
        )
        risk_agents.append({
            "agent": name,
            **result,
        })

    risk_summary = "\n\n".join(
        f"{x['agent']}: {x['decision']} | confidence={x['confidence']:.0f}\n"
        f"{x['reasoning']}\nRisk: {x['key_risk']}"
        for x in risk_agents
    )

    portfolio = call_ai(
        "👨‍⚖️ Portfolio Manager / Final Judge",
        data
        + "\n\nANALYSTS:\n"
        + analyst_summary
        + "\n\nBULL vs BEAR:\n"
        + debate
        + "\n\nRESEARCH MANAGER:\n"
        + json.dumps(research_manager, ensure_ascii=False)
        + "\n\nTRADER:\n"
        + json.dumps(trader, ensure_ascii=False)
        + "\n\nRISK TEAM:\n"
        + risk_summary,
        """
Ini keputusan final.

Jangan memakai voting mayoritas secara buta.
Pertimbangkan:
1. kualitas bukti,
2. konflik antar timeframe,
3. Bull vs Bear,
4. rencana Trader,
5. tiga perspektif Risk,
6. spread dan order book,
7. market regime,
8. jarak ke support/resistance.

Pilih LONG, SHORT, atau WAIT.

Jika data konflik atau risk/reward buruk, pilih WAIT.
Confidence harus mencerminkan kekuatan bukti, bukan rasa percaya diri.
""",
    )

    final = portfolio["decision"]

    # Hard safety gate: AI tidak boleh memaksa posisi ketika consensus lemah.
    analyst_direction = sum(
        1 if x["decision"] == "LONG"
        else -1 if x["decision"] == "SHORT"
        else 0
        for x in analyst_results
    )

    risk_direction = sum(
        1 if x["decision"] == "LONG"
        else -1 if x["decision"] == "SHORT"
        else 0
        for x in risk_agents
    )

    if abs(analyst_direction) <= 1 and abs(risk_direction) <= 1:
        final = "WAIT"

    # Entry / SL / TP are deterministic from current price + ATR,
    # not invented by the LLM.
    current = packet["price"]
    atr15 = packet["timeframes"]["15m"]["atr"]

    if final == "LONG":
        sl = current - 1.0 * atr15
        tp = current + 2.0 * atr15
    elif final == "SHORT":
        sl = current + 1.0 * atr15
        tp = current - 2.0 * atr15
    else:
        sl = None
        tp = None

    rr = 2.0 if final in {"LONG", "SHORT"} else 0.0

    return {
        "symbol": symbol,
        "frames": frames,
        "packet": packet,
        "analysts": analyst_results,
        "bull": bull,
        "bear": bear,
        "research_manager": research_manager,
        "trader": trader,
        "risk_agents": risk_agents,
        "portfolio": portfolio,
        "final": final,
        "scanner_score": scanner_score_value,
        "entry": current if final != "WAIT" else None,
        "sl": sl,
        "tp": tp,
        "rr": rr,
    }

# -------------------------
# Memory / paper trading
# -------------------------
def save_signal(result):
    con = db()
    now = datetime.now(timezone.utc).isoformat()

    cur = con.execute(
        """
        INSERT INTO signals
        (created_at,symbol,timeframe,scanner_score,final_decision,
         confidence,entry,stop_loss,take_profit,risk_reward,
         market_regime,score_consensus,reasoning)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            now,
            result["symbol"],
            "5m/15m/1h",
            result["scanner_score"],
            result["final"],
            result["portfolio"]["confidence"],
            result["entry"],
            result["sl"],
            result["tp"],
            result["rr"],
            result["packet"]["market_regime"],
            result["scanner_score"],
            result["portfolio"]["reasoning"],
        ),
    )

    signal_id = cur.lastrowid

    for group in [
        result["analysts"],
        [
            {"agent": "🐂 Bull Researcher", **result["bull"]},
            {"agent": "🐻 Bear Researcher", **result["bear"]},
        ],
        [
            {"agent": "🧠 Research Manager", **result["research_manager"]},
            {"agent": "💹 Trader", **result["trader"]},
        ],
        result["risk_agents"],
        [{"agent": "👨‍⚖️ Portfolio Manager", **result["portfolio"]}],
    ]:
        for x in group:
            con.execute(
                """
                INSERT INTO agent_votes
                (signal_id,agent,decision,confidence,reasoning)
                VALUES (?,?,?,?,?)
                """,
                (
                    signal_id,
                    x["agent"],
                    x["decision"],
                    x["confidence"],
                    x["reasoning"],
                ),
            )

    con.execute(
        """
        INSERT INTO memories
        (created_at,symbol,memory_type,content,signal_id)
        VALUES (?,?,?,?,?)
        """,
        (
            now,
            result["symbol"],
            "SIGNAL_THESIS",
            result["portfolio"]["reasoning"],
            signal_id,
        ),
    )

    con.commit()
    con.close()

    return signal_id

def current_price(symbol):
    data = bget("/api/v3/ticker/price", {"symbol": symbol})
    return float(data["price"])

def update_paper_trades():
    con = db()
    rows = con.execute(
        """
        SELECT * FROM signals
        WHERE result IS NULL
        AND final_decision IN ('LONG','SHORT')
        ORDER BY id ASC
        """
    ).fetchall()

    updated = []

    for row in rows:
        try:
            price = current_price(row["symbol"])
        except Exception:
            continue

        decision = row["final_decision"]
        result = None
        r_mult = None

        if decision == "LONG":
            if price >= row["take_profit"]:
                result = "TP"
                r_mult = 2.0
            elif price <= row["stop_loss"]:
                result = "SL"
                r_mult = -1.0

        elif decision == "SHORT":
            if price <= row["take_profit"]:
                result = "TP"
                r_mult = 2.0
            elif price >= row["stop_loss"]:
                result = "SL"
                r_mult = -1.0

        if result:
            now = datetime.now(timezone.utc).isoformat()

            con.execute(
                """
                UPDATE signals
                SET closed_at=?, result=?, r_multiple=?, exit_price=?
                WHERE id=?
                """,
                (now, result, r_mult, price, row["id"]),
            )

            memory = (
                f"{row['symbol']} {decision} selesai {result}. "
                f"Entry={row['entry']}, Exit={price}, R={r_mult:+.1f}. "
                "Gunakan hasil ini sebagai histori performa, bukan jaminan."
            )

            con.execute(
                """
                INSERT INTO memories
                (created_at,symbol,memory_type,content,signal_id)
                VALUES (?,?,?,?,?)
                """,
                (
                    now,
                    row["symbol"],
                    "TRADE_OUTCOME",
                    memory,
                    row["id"],
                ),
            )

            updated.append(
                (row["symbol"], result, r_mult)
            )

    con.commit()
    con.close()
    return updated

def performance():
    con = db()

    closed = con.execute(
        """
        SELECT * FROM signals
        WHERE result IN ('TP','SL')
        ORDER BY id DESC
        """
    ).fetchall()

    open_count = con.execute(
        """
        SELECT COUNT(*) FROM signals
        WHERE result IS NULL
        AND final_decision IN ('LONG','SHORT')
        """
    ).fetchone()[0]

    con.close()

    if not closed:
        return {
            "closed": 0,
            "open": open_count,
            "wins": 0,
            "losses": 0,
            "win_rate": 0,
            "avg_r": 0,
            "profit_factor": 0,
            "rows": [],
        }

    wins = sum(1 for x in closed if x["result"] == "TP")
    losses = sum(1 for x in closed if x["result"] == "SL")

    positive = sum(
        max(float(x["r_multiple"]), 0)
        for x in closed
    )
    negative = abs(sum(
        min(float(x["r_multiple"]), 0)
        for x in closed
    ))

    return {
        "closed": len(closed),
        "open": open_count,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(closed) * 100,
        "avg_r": sum(float(x["r_multiple"]) for x in closed) / len(closed),
        "profit_factor": (
            positive / negative if negative else float("inf")
        ),
        "rows": closed,
    }

# -------------------------
# Telegram
# -------------------------
def telegram(message):
    if not TG_TOKEN or not TG_CHAT:
        return False, "Telegram belum dikonfigurasi."

    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": message},
        timeout=12,
    )

    if r.status_code != 200:
        return False, r.text[:300]

    data = r.json()
    return bool(data.get("ok")), data.get("description", "OK")

def telegram_message(result):
    final = result["final"]
    icon = "🚀" if final == "LONG" else "🩸"

    return f"""🚨 AI TRADING V5

{icon} {result['symbol']}
SIGNAL: {final}
CONFIDENCE: {result['portfolio']['confidence']:.0f}/100
SCANNER: {result['scanner_score']}/100

💰 Entry: ${result['entry']:,.8f}
🎯 TP: ${result['tp']:,.8f}
🛑 SL: ${result['sl']:,.8f}
⚖️ R:R: 1:{result['rr']:.1f}

🧠 Market: {result['packet']['market_regime']}
📚 Order Imbalance: {result['packet']['order_book']['imbalance']:+.1%}
🕯 Candle: {result['packet']['candlestick']['name']}

👨‍⚖️ Final Judge:
{result['portfolio']['reasoning']}

⚠️ Paper-analysis only. Tidak ada order otomatis."""

# -------------------------
# UI
# -------------------------
if "scan" not in st.session_state:
    st.session_state.scan = []

if "last_result" not in st.session_state:
    st.session_state.last_result = None

st.title("🧠 AI Consensus Trading V5")
st.caption(
    "Crypto Scanner → Analyst Team → Bull ↔ Bear → Research Manager → "
    "Trader → Risk Debate → Portfolio Manager → Memory → Paper Trading"
)

st.sidebar.header("🔎 Market Scanner")
scan_n = st.sidebar.slider("Koin yang discan", 20, 100, 60)
top_n = st.sidebar.slider("Kandidat dianalisis AI", 1, 3, 1)

if st.sidebar.button("🔍 SCAN MARKET", use_container_width=True):
    ok, status = binance_health()

    if not ok:
        st.error(
            "❌ Binance Public Market Data tidak dapat diakses. "
            f"Detail: {status}"
        )
    else:
        with st.spinner("🛰️ Mencari market dengan volume, flow dan momentum terbaik..."):
            try:
                st.session_state.scan = scan_markets(scan_n)
            except Exception as e:
                st.error(f"Scanner gagal: {e}")

st.sidebar.divider()
st.sidebar.header("🤖 AI")
st.sidebar.write(
    "Groq:",
    "🟢 SIAP" if GROQ_KEY else "🔴 BELUM ADA"
)
st.sidebar.caption(f"Model: {GROQ_MODEL}")
st.sidebar.caption(
    "Satu kandidat dapat memakai beberapa panggilan AI karena setiap agen memiliki peran berbeda."
)

st.sidebar.divider()
st.sidebar.header("📱 Telegram")
tg_on = st.sidebar.checkbox(
    "Kirim alert Telegram",
    bool(TG_TOKEN and TG_CHAT),
)

if not TG_TOKEN or not TG_CHAT:
    st.sidebar.caption(
        "Telegram belum dikonfigurasi — V5 tetap bisa digunakan."
    )

if st.sidebar.button("📨 Test Telegram", use_container_width=True):
    ok, msg = telegram("✅ TEST AI CONSENSUS TRADING V5\nTelegram berhasil terhubung.")
    st.sidebar.success(msg) if ok else st.sidebar.error(msg)

st.sidebar.divider()
st.sidebar.header("🧪 Paper Trading")

if st.sidebar.button(
    "🔄 UPDATE PAPER TRADES",
    use_container_width=True,
):
    with st.spinner("Memeriksa TP/SL paper trade yang masih terbuka..."):
        updates = update_paper_trades()

    if updates:
        for symbol, result, r in updates:
            st.sidebar.success(
                f"{symbol}: {result} ({r:+.1f}R)"
            )
    else:
        st.sidebar.info("Belum ada trade yang mencapai TP/SL.")

if st.sidebar.button(
    "🧹 RESET DATABASE V5",
    use_container_width=True,
):
    st.session_state.confirm_reset = True

if st.session_state.get("confirm_reset", False):
    st.sidebar.warning(
        "Ini akan menghapus histori signal, agent votes, dan memory."
    )
    if st.sidebar.button("⚠️ YA, HAPUS SEMUA"):
        con = db()
        con.executescript("""
        DELETE FROM agent_votes;
        DELETE FROM memories;
        DELETE FROM signals;
        """)
        con.commit()
        con.close()
        st.session_state.confirm_reset = False
        st.rerun()

# -------------------------
# Performance dashboard
# -------------------------
perf = performance()

st.subheader("📊 V5 Performance Memory")

p1, p2, p3, p4, p5 = st.columns(5)
p1.metric("Signal Closed", perf["closed"])
p2.metric("Open Paper Trade", perf["open"])
p3.metric("Win Rate", f"{perf['win_rate']:.1f}%")
p4.metric("Average R", f"{perf['avg_r']:+.2f}R")
pf = "∞" if math.isinf(perf["profit_factor"]) else f"{perf['profit_factor']:.2f}"
p5.metric("Profit Factor", pf)

st.divider()

# -------------------------
# Scanner results
# -------------------------
if not st.session_state.scan:
    st.info(
        "Klik **SCAN MARKET** untuk mencari kandidat crypto. "
        "V5 tidak mengeksekusi order."
    )
else:
    st.subheader("🔥 Top Kandidat")

    rows = []

    for i, x in enumerate(st.session_state.scan[:10], 1):
        rows.append({
            "Rank": i,
            "Symbol": x["symbol"],
            "Score": x["score"],
            "24h": f"{x['change']:+.2f}%",
            "Volume 24h": f"${x['qvol']:,.0f}",
            "Vol Ratio": f"{x['vr']:.2f}x",
            "Buy Pressure": f"{x['buy']:.1%}",
            "Order Imbalance": f"{x['imb']:+.1%}",
            "RSI": f"{x['rsi']:.2f}",
        })

    st.dataframe(
        pd.DataFrame(rows),
        use_container_width=True,
        hide_index=True,
    )

    st.divider()
    st.subheader(f"🧠 TradingAgents-style Research — Top {top_n}")

    for rank, candidate in enumerate(
        st.session_state.scan[:top_n],
        1,
    ):
        symbol = candidate["symbol"]

        st.markdown(
            f"## #{rank} {symbol} — Scanner {candidate['score']}/100"
        )

        with st.spinner(
            f"🧠 Menjalankan analyst team + bull/bear + risk team untuk {symbol}..."
        ):
            try:
                result = run_multi_agent(
                    symbol,
                    candidate["score"],
                )
                st.session_state.last_result = result
            except Exception as e:
                st.error(f"Gagal menganalisis {symbol}: {e}")
                continue

        packet = result["packet"]

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Harga", f"${packet['price']:,.6f}")
        c2.metric("Regime", packet["market_regime"])
        c3.metric(
            "Buy Pressure",
            f"{packet['timeframes']['15m']['buy_pressure']:.1%}",
        )
        c4.metric(
            "Order Book",
            f"{packet['order_book']['imbalance']:+.1%}",
        )
        c5.metric(
            "RSI 15m",
            f"{packet['timeframes']['15m']['rsi']:.2f}",
        )

        # Candlestick chart
        ch = result["frames"]["15m"].tail(100)

        fig = go.Figure()

        fig.add_trace(
            go.Candlestick(
                x=ch.index,
                open=ch.Open,
                high=ch.High,
                low=ch.Low,
                close=ch.Close,
                name="Candlestick",
            )
        )

        fig.add_trace(
            go.Scatter(
                x=ch.index,
                y=ch.EMA20,
                name="EMA20",
            )
        )

        fig.add_trace(
            go.Scatter(
                x=ch.index,
                y=ch.EMA50,
                name="EMA50",
            )
        )

        fig.add_trace(
            go.Scatter(
                x=ch.index,
                y=ch.EMA200,
                name="EMA200",
            )
        )

        fig.update_layout(
            height=500,
            template="plotly_dark",
            xaxis_rangeslider_visible=False,
        )

        st.plotly_chart(
            fig,
            use_container_width=True,
        )

        # Analyst team
        st.markdown("### 🧩 Analyst Team")

        cols = st.columns(4)

        for col, agent in zip(
            cols,
            result["analysts"],
        ):
            with col:
                if agent["decision"] == "LONG":
                    col.success(
                        f"**{agent['agent']} — LONG**\n\n"
                        f"Confidence: {agent['confidence']:.0f}\n\n"
                        f"{agent['reasoning']}"
                    )
                elif agent["decision"] == "SHORT":
                    col.error(
                        f"**{agent['agent']} — SHORT**\n\n"
                        f"Confidence: {agent['confidence']:.0f}\n\n"
                        f"{agent['reasoning']}"
                    )
                else:
                    col.warning(
                        f"**{agent['agent']} — WAIT**\n\n"
                        f"Confidence: {agent['confidence']:.0f}\n\n"
                        f"{agent['reasoning']}"
                    )

        # Bull vs Bear
        st.markdown("### 🐂 Bull vs 🐻 Bear Debate")

        b1, b2 = st.columns(2)

        with b1:
            st.markdown("#### 🐂 Bull Researcher")
            st.write(
                f"**{result['bull']['decision']}** — "
                f"confidence {result['bull']['confidence']:.0f}"
            )
            st.write(result["bull"]["reasoning"])
            st.caption("Risiko: " + result["bull"]["key_risk"])

        with b2:
            st.markdown("#### 🐻 Bear Researcher")
            st.write(
                f"**{result['bear']['decision']}** — "
                f"confidence {result['bear']['confidence']:.0f}"
            )
            st.write(result["bear"]["reasoning"])
            st.caption("Risiko: " + result["bear"]["key_risk"])

        # Research manager + trader
        st.markdown("### 🧠 Research Manager")

        st.info(
            f"**{result['research_manager']['decision']}** — "
            f"confidence {result['research_manager']['confidence']:.0f}\n\n"
            f"{result['research_manager']['reasoning']}"
        )

        st.markdown("### 💹 Trader Plan")

        st.write(
            f"**{result['trader']['decision']}** — "
            f"confidence {result['trader']['confidence']:.0f}"
        )
        st.write(result["trader"]["reasoning"])
        st.caption(
            "Risiko Trader: "
            + result["trader"]["key_risk"]
        )

        # Risk team
        st.markdown("### 🛡️ Risk Debate")

        rcols = st.columns(3)

        for col, risk in zip(
            rcols,
            result["risk_agents"],
        ):
            with col:
                if risk["decision"] == "LONG":
                    col.success(
                        f"**{risk['agent']} — LONG**\n\n"
                        f"{risk['reasoning']}"
                    )
                elif risk["decision"] == "SHORT":
                    col.error(
                        f"**{risk['agent']} — SHORT**\n\n"
                        f"{risk['reasoning']}"
                    )
                else:
                    col.warning(
                        f"**{risk['agent']} — WAIT**\n\n"
                        f"{risk['reasoning']}"
                    )

        # Final
        st.markdown("### 👨‍⚖️ Portfolio Manager — Final Decision")

        final = result["final"]

        if final == "LONG":
            st.success(
                f"🚀 **LONG {symbol}** — "
                f"confidence {result['portfolio']['confidence']:.0f}/100"
            )
        elif final == "SHORT":
            st.error(
                f"🩸 **SHORT {symbol}** — "
                f"confidence {result['portfolio']['confidence']:.0f}/100"
            )
        else:
            st.warning(
                f"⚖️ **WAIT {symbol}** — "
                f"confidence {result['portfolio']['confidence']:.0f}/100"
            )

        st.write(result["portfolio"]["reasoning"])
        st.caption(
            "Risiko final: "
            + result["portfolio"]["key_risk"]
        )

        if final in {"LONG", "SHORT"}:
            e1, e2, e3, e4 = st.columns(4)

            e1.metric(
                "Paper Entry",
                f"${result['entry']:,.8f}",
            )
            e2.metric(
                "Stop Loss",
                f"${result['sl']:,.8f}",
            )
            e3.metric(
                "Take Profit",
                f"${result['tp']:,.8f}",
            )
            e4.metric(
                "Risk / Reward",
                f"1:{result['rr']:.1f}",
            )

            st.warning(
                "⚠️ Ini PAPER TRADE. Tidak ada order Binance yang dieksekusi."
            )

            if st.button(
                f"💾 Simpan Signal {symbol}",
                key=f"save_{symbol}_{rank}",
            ):
                signal_id = save_signal(result)
                st.success(
                    f"Signal tersimpan ke memory V5. ID: {signal_id}"
                )

                if tg_on:
                    ok, msg = telegram(
                        telegram_message(result)
                    )
                    if ok:
                        st.success("📱 Alert Telegram terkirim.")
                    else:
                        st.error(f"Telegram gagal: {msg}")

        else:
            if st.button(
                f"💾 Simpan WAIT {symbol}",
                key=f"save_wait_{symbol}_{rank}",
            ):
                signal_id = save_signal(result)
                st.success(
                    f"WAIT tersimpan ke memory V5. ID: {signal_id}"
                )

        with st.expander("🔍 Lihat data multi-timeframe"):
            st.json(packet)

        st.divider()

# -------------------------
# History
# -------------------------
st.subheader("🧠 Memory & Signal History")

con = db()
history = con.execute(
    """
    SELECT id, created_at, symbol, final_decision,
           confidence, entry, stop_loss, take_profit,
           result, r_multiple, market_regime
    FROM signals
    ORDER BY id DESC
    LIMIT 100
    """
).fetchall()
con.close()

if history:
    hist_rows = []

    for x in history:
        hist_rows.append({
            "ID": x["id"],
            "Waktu": x["created_at"][:19].replace("T", " "),
            "Symbol": x["symbol"],
            "Decision": x["final_decision"],
            "Confidence": round(float(x["confidence"] or 0)),
            "Entry": x["entry"],
            "SL": x["stop_loss"],
            "TP": x["take_profit"],
            "Result": x["result"] or "OPEN",
            "R": x["r_multiple"],
            "Regime": x["market_regime"],
        })

    st.dataframe(
        pd.DataFrame(hist_rows),
        use_container_width=True,
        hide_index=True,
    )
else:
    st.info(
        "Belum ada signal tersimpan. "
        "Gunakan tombol Simpan Signal setelah analisis."
    )

st.caption(
    "V5 adalah sistem riset/paper trading. Tidak mengeksekusi order otomatis. "
    "Hasil historis tidak menjamin hasil masa depan."
)
