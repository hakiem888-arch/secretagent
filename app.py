import os
import re
import json
import math
import sqlite3
import time
from datetime import datetime, timezone

import requests
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from groq import Groq

# ============================================================
# AI CONSENSUS TRADING V5.5 (DYNAMIC TP/SL EDITION)
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
# ============================================================

st.set_page_config(
    page_title="AI Consensus Trading V5.5",
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

def _parse_secret_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        raw_items = value
    else:
        raw_items = re.split(r"[\s,]+", str(value))
    return [str(x).strip() for x in raw_items if str(x).strip()]

_GROQ_KEYS = []
_GROQ_KEYS.extend(_parse_secret_list(secret("GROQ_API_KEY")))
_GROQ_KEYS.extend(_parse_secret_list(secret("GROQ_API_KEYS")))
for _i in range(1, 21):
    _GROQ_KEYS.extend(_parse_secret_list(secret(f"GROQ_API_KEY_{_i}")))

GROQ_KEYS = list(dict.fromkeys(k for k in _GROQ_KEYS if k))
GROQ_KEY = GROQ_KEYS[0] if GROQ_KEYS else ""

TG_TOKEN = secret("TELEGRAM_BOT_TOKEN")
TG_CHAT = secret("TELEGRAM_CHAT_ID")

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
# AI engine — V5.5 Hierarchical Multi-Model Safe Mode
# -------------------------
ANALYST_MODEL = secret("ANALYST_MODEL") or "openai/gpt-oss-20b"
DEBATE_MODEL = secret("DEBATE_MODEL") or "qwen/qwen3.6-27b"
RESEARCH_MODEL = secret("RESEARCH_MODEL") or "openai/gpt-oss-120b"
JUDGE_MODEL = secret("JUDGE_MODEL") or "openai/gpt-oss-120b"

ANALYST_MAX_TOKENS = int(secret("ANALYST_MAX_TOKENS") or 450)
DEBATE_MAX_TOKENS = int(secret("DEBATE_MAX_TOKENS") or 550)
RESEARCH_MAX_TOKENS = int(secret("RESEARCH_MAX_TOKENS") or 700)
JUDGE_MAX_TOKENS = int(secret("JUDGE_MAX_TOKENS") or 750)

MAX_AI_CALLS_PER_CANDIDATE = int(secret("MAX_AI_CALLS_PER_CANDIDATE") or 12)
GROQ_MIN_DELAY = float(secret("GROQ_MIN_DELAY") or 1.0)
GROQ_RETRY_SECONDS = float(secret("GROQ_RETRY_SECONDS") or 30.0)

MODEL_TIERS = {
    "analyst": {
        "model": ANALYST_MODEL,
        "max_tokens": ANALYST_MAX_TOKENS,
        "reasoning_effort": "low",
    },
    "debate": {
        "model": DEBATE_MODEL,
        "max_tokens": DEBATE_MAX_TOKENS,
        "reasoning_effort": "low",
    },
    "research": {
        "model": RESEARCH_MODEL,
        "max_tokens": RESEARCH_MAX_TOKENS,
        "reasoning_effort": "low",
    },
    "judge": {
        "model": JUDGE_MODEL,
        "max_tokens": JUDGE_MAX_TOKENS,
        "reasoning_effort": "low",
    },
}

AI_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["LONG", "SHORT", "WAIT"]},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
        "key_risk": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
        },
        "tp_price": {"type": "number"},
        "sl_price": {"type": "number"},
    },
    "required": ["decision", "confidence", "reasoning", "key_risk", "evidence", "tp_price", "sl_price"],
    "additionalProperties": False,
}

def compact_json(obj, max_chars=1400):
    if isinstance(obj, dict):
        evidence = obj.get("evidence", [])
        if not isinstance(evidence, list):
            evidence = [str(evidence)] if evidence else []
        compacted = {
            "decision": obj.get("decision", "WAIT"),
            "confidence": obj.get("confidence", 0),
            "reasoning": obj.get("reasoning", ""),
            "key_risk": obj.get("key_risk", ""),
            "evidence": evidence[:3],
        }
        if "tp_price" in obj: compacted["tp_price"] = obj["tp_price"]
        if "sl_price" in obj: compacted["sl_price"] = obj["sl_price"]
        obj = compacted
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))[:max_chars]

def _provider_state(name):
    key = f"{name}_key_state"
    if key not in st.session_state:
        keys = GROQ_KEYS if name == "groq" else []
        st.session_state[key] = {
            i: {"disabled_until": 0.0, "reason": "", "failures": 0}
            for i in range(len(keys))
        }
    return st.session_state[key]

def _key_mask(key):
    if not key:
        return "-"
    return f"{key[:5]}...{key[-4:]}" if len(key) > 12 else "***"

def _key_disabled(provider, index):
    return float(_provider_state(provider).get(index, {}).get("disabled_until", 0)) > time.time()

def _disable_key(provider, index, seconds, reason):
    state = _provider_state(provider).setdefault(
        index, {"disabled_until": 0.0, "reason": "", "failures": 0}
    )
    state["disabled_until"] = time.time() + seconds
    state["reason"] = str(reason)[:180]
    state["failures"] = int(state.get("failures", 0)) + 1

def _is_quota_error(message):
    msg = str(message).lower()
    return any(x in msg for x in (
        "tokens per day", "requests per day", "daily limit", "daily quota",
        "quota exceeded", "quota has been exceeded", "insufficient_quota",
        "billing", "resource_exhausted"
    ))

def _is_rate_limit_error(message):
    msg = str(message).lower()
    return "429" in msg or "rate_limit" in msg or "rate limit" in msg or "too many requests" in msg

def _is_auth_error(message):
    msg = str(message).lower()
    return any(x in msg for x in ("401", "403", "invalid api key", "authentication", "unauthorized"))

def _is_model_not_found(message):
    msg = str(message).lower()
    return "model_not_found" in msg or "does not exist" in msg or "do not have access" in msg

def _build_prompt(role, data, instructions):
    return f"""
Kamu adalah {role} dalam sistem riset trading multi-agent.

TUGAS:
{instructions}

ATURAN WAJIB:
1. Gunakan hanya data yang diberikan. Jangan mengarang fakta.
2. Jika bukti tidak cukup, pilih WAIT.
3. Jawab dalam Bahasa Indonesia.
4. Jawaban akhir WAJIB berupa SATU OBJECT JSON VALID.
5. JSON harus memiliki tepat struktur berikut:
{{"decision":"LONG|SHORT|WAIT","confidence":0,"reasoning":"alasan singkat","key_risk":"risiko utama","evidence":["bukti 1","bukti 2"],"tp_price":0.0,"sl_price":0.0}}
6. decision hanya LONG, SHORT, atau WAIT.
7. confidence adalah angka 0 sampai 100.
8. evidence adalah array string, maksimal 3 item.
9. tp_price dan sl_price adalah angka harga spesifik. Jika WAIT, isi dengan 0.0.
10. Jangan gunakan markdown, jangan gunakan ```json, dan jangan menulis teks sebelum/sesudah JSON.
11. Kata JSON di atas berarti output akhir harus benar-benar JSON yang dapat diparse Python.

DATA:
{data}
"""

def _safe_json_from_text(raw):
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Respons AI kosong.")

    cleaned = re.sub(r"```(?:json)?\s*", "", raw, flags=re.I).replace("```", "").strip()

    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    starts = [m.start() for m in re.finditer(r"\{", cleaned)]
    for start_pos in starts:
        depth = 0
        in_string = False
        escape = False
        for i in range(start_pos, len(cleaned)):
            ch = cleaned[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = cleaned[start_pos:i+1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict):
                            return obj
                    except Exception:
                        break
    raise ValueError("Respons AI bukan JSON valid.")

def _normalize_ai(obj, tier, provider="unknown", model=None):
    if not isinstance(obj, dict):
        raise ValueError("Output AI bukan object JSON.")

    decision = str(obj.get("decision", "WAIT")).upper().strip()
    if decision not in {"LONG", "SHORT", "WAIT"}:
        decision = "WAIT"

    try:
        confidence = float(obj.get("confidence", 0) or 0)
    except Exception:
        confidence = 0.0

    evidence = obj.get("evidence", [])
    if not isinstance(evidence, list):
        evidence = [str(evidence)] if evidence else []

    return {
        "decision": decision,
        "confidence": max(0.0, min(100.0, confidence)),
        "reasoning": str(obj.get("reasoning", ""))[:700],
        "key_risk": str(obj.get("key_risk", ""))[:400],
        "evidence": [str(x)[:180] for x in evidence[:3]],
        "tp_price": float(obj.get("tp_price", 0.0) or 0.0),
        "sl_price": float(obj.get("sl_price", 0.0) or 0.0),
        "tier": tier,
        "provider": provider,
        "model": model or MODEL_TIERS[tier]["model"],
    }

def _groq_client(key):
    return Groq(api_key=key)

def _request_groq(model, prompt, key, max_tokens, tier):
    kwargs = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_completion_tokens": max(384, int(max_tokens)),
    }

    if model in {"openai/gpt-oss-20b", "openai/gpt-oss-120b"}:
        kwargs["reasoning_effort"] = MODEL_TIERS[tier].get("reasoning_effort", "low")
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "trading_agent_decision",
                "strict": True,
                "schema": AI_SCHEMA,
            },
        }
    elif model.startswith("qwen/"):
        kwargs["reasoning_effort"] = "none"
        kwargs["response_format"] = {"type": "json_object"}
    else:
        kwargs["response_format"] = {"type": "json_object"}

    return _groq_client(key).chat.completions.create(**kwargs)

def _ordered_key_indices(provider):
    if provider != "groq" or not GROQ_KEYS:
        return []
    active = int(st.session_state.get("_active_groq_key", 0)) % len(GROQ_KEYS)
    return list(range(active, len(GROQ_KEYS))) + list(range(0, active))

def _provider_models(provider, tier):
    if provider != "groq":
        return []
    return [MODEL_TIERS[tier]["model"]]

def call_ai(role, data, instructions, tier="analyst", budget=None):
    if tier not in MODEL_TIERS:
        tier = "analyst"

    if not st.session_state.get("_ai_calls_current_candidate", 0):
        st.session_state["_ai_calls_current_candidate"] = 0

    used = int(st.session_state["_ai_calls_current_candidate"])
    if used >= MAX_AI_CALLS_PER_CANDIDATE:
        return {
            "decision": "WAIT", "confidence": 0,
            "reasoning": "Batas panggilan AI per kandidat tercapai.",
            "key_risk": "Quota perlu dihemat.", "evidence": [],
            "tier": tier, "provider": "none", "model": ""
        }
    st.session_state["_ai_calls_current_candidate"] = used + 1

    cfg = MODEL_TIERS[tier]
    max_tokens = int(budget or cfg["max_tokens"])
    prompt = _build_prompt(role, data, instructions)
    errors = []

    providers = ["groq"] if GROQ_KEYS else []

    for provider in providers:
        keys = GROQ_KEYS
        if not keys:
            continue

        for model in _provider_models(provider, tier):
            model_access_failed = True

            for key_index in _ordered_key_indices(provider):
                if _key_disabled(provider, key_index):
                    continue

                key = keys[key_index]
                try:
                    last = float(st.session_state.get("_last_ai_call", 0))
                    wait = GROQ_MIN_DELAY - (time.time() - last)
                    if wait > 0:
                        time.sleep(wait)
                    raw = _request_groq(model, prompt, key, max_tokens, tier)
                    st.session_state["_last_ai_call"] = time.time()
                    st.session_state["_active_groq_key"] = key_index
                    message = raw.choices[0].message
                    content = message.content or ""
                    if not content.strip():
                        reasoning = getattr(message, "reasoning", None) or ""
                        if reasoning:
                            raise RuntimeError(
                                "Respons AI kosong (reasoning ada tetapi content JSON tidak keluar)."
                            )
                        raise RuntimeError("Respons AI kosong.")

                    obj = _safe_json_from_text(content)
                    return _normalize_ai(obj, tier, provider, model)

                except Exception as e:
                    msg = str(e)
                    errors.append(f"{provider} {_key_mask(key)} / {model}: {msg[:240]}")

                    if _is_rate_limit_error(msg):
                        _disable_key(provider, key_index, GROQ_RETRY_SECONDS, "Rate limit")
                        continue
                    if _is_quota_error(msg):
                        _disable_key(provider, key_index, 24 * 60 * 60, "Quota harian")
                        continue
                    if _is_auth_error(msg):
                        _disable_key(provider, key_index, 24 * 60 * 60, "API key/balance")
                        continue
                    if _is_model_not_found(msg):
                        continue
                    if "timeout" in msg.lower() or "connection" in msg.lower():
                        _disable_key(provider, key_index, 60, "Network/timeout")
                        continue

                    model_access_failed = False
                    continue
            continue

    summary = " | ".join(errors[-5:]) if errors else "Tidak ada provider API yang tersedia."
    return {
        "decision": "WAIT",
        "confidence": 0,
        "reasoning": f"AI gagal setelah mencoba provider yang tersedia: {summary}",
        "key_risk": "Periksa API key, quota, akses model, atau koneksi provider.",
        "evidence": [],
        "tier": tier,
        "provider": "none",
        "model": "",
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


def specialist_data(packet, focus):
    tf = packet["timeframes"]
    lines = [
        f"Symbol: {packet['symbol']}",
        f"Harga: {packet['price']:.8f}",
        f"Regime: {packet['market_regime']}",
        f"Support: {packet['support']:.8f}",
        f"Resistance: {packet['resistance']:.8f}",
    ]
    if focus == "technical":
        for name in ("5m", "15m", "1h"):
            x = tf[name]
            lines += [
                f"[{name}] EMA20={x['ema20']:.8f} EMA50={x['ema50']:.8f} EMA200={x['ema200']:.8f}",
                f"[{name}] MACD_HIST={x['macd_hist']:.8f} RSI={x['rsi']:.2f}",
            ]
    elif focus == "momentum":
        for name in ("5m", "15m", "1h"):
            x = tf[name]
            lines += [
                f"[{name}] RSI={x['rsi']:.2f} VOL_RATIO={x['vol_ratio']:.2f}x BUY_PRESSURE={x['buy_pressure']:.1%}",
                f"[{name}] MACD_HIST={x['macd_hist']:.8f}",
            ]
    elif focus == "orderflow":
        ob = packet["order_book"]
        x = tf["15m"]
        lines += [
            f"Order imbalance={ob['imbalance']:+.2%}",
            f"Spread={ob['spread']:.3f}% BidDepth={ob['bid_depth']:.4f} AskDepth={ob['ask_depth']:.4f}",
            f"15m BUY_PRESSURE={x['buy_pressure']:.1%} VOL_RATIO={x['vol_ratio']:.2f}x",
        ]
    else:  # price action
        c = packet["candlestick"]
        x = tf["15m"]
        lines += [
            f"Candle={c['name']} Body={c['body']:.1%} UpperWick={c['upper_wick']:.1%} LowerWick={c['lower_wick']:.1%}",
            f"15m RSI={x['rsi']:.2f} EMA20={x['ema20']:.8f} EMA50={x['ema50']:.8f}",
            f"15m VOL_RATIO={x['vol_ratio']:.2f}x MACD_HIST={x['macd_hist']:.8f}",
        ]
    return "\n".join(lines)


def memory_context(symbol, limit=5):
    try:
        con = db()
        rows = con.execute(
            """
            SELECT final_decision, confidence, result, r_multiple, market_regime
            FROM signals
            WHERE symbol=? AND result IN ('TP','SL')
            ORDER BY id DESC LIMIT ?
            """,
            (symbol, int(limit)),
        ).fetchall()
        con.close()
    except Exception:
        return "Tidak ada memory historis yang tersedia."
    if not rows:
        return "Belum ada paper-trade historis tertutup untuk simbol ini."
    return "\n".join(
        f"{i+1}. {r['final_decision']} C={float(r['confidence'] or 0):.0f} "
        f"hasil={r['result']} R={float(r['r_multiple'] or 0):+.1f} regime={r['market_regime']}"
        for i, r in enumerate(rows)
    )

# -------------------------
# V5.5 Hierarchical orchestration
# -------------------------
ANALYSTS = [
    ("🧭 Technical Analyst", "Fokus trend multi-timeframe, EMA, MACD, support/resistance, dan regime."),
    ("⚡ Momentum Analyst", "Fokus RSI, volume ratio, taker-buy pressure, momentum, dan konfirmasi timeframe."),
    ("📚 Order Flow Analyst", "Fokus order-book imbalance, spread, liquidity, dan tekanan beli/jual."),
    ("🕯️ Price Action Analyst", "Fokus candle, wick, rejection, breakout, struktur harga, support/resistance."),
]


def analyst_summary_text(items):
    lines = []
    for x in items:
        lines.append(
            f"{x['agent']} | {x['decision']} | C={x['confidence']:.0f} | "
            f"Bukti={'; '.join(x.get('evidence', []))} | Risiko={x['key_risk']}"
        )
    return "\n".join(lines)


def debate_summary_text(bull, bear):
    return (
        f"BULL: {bull['decision']} C={bull['confidence']:.0f}; "
        f"{bull['reasoning']}; bukti={'; '.join(bull.get('evidence', []))}; risiko={bull['key_risk']}\n"
        f"BEAR: {bear['decision']} C={bear['confidence']:.0f}; "
        f"{bear['reasoning']}; bukti={'; '.join(bear.get('evidence', []))}; risiko={bear['key_risk']}"
    )


def risk_summary_text(items):
    return "\n".join(
        f"{x['agent']} | {x['decision']} | C={x['confidence']:.0f} | "
        f"{x['reasoning']} | risiko={x['key_risk']}"
        for x in items
    )


def run_multi_agent(symbol, scanner_score_value):
    st.session_state["_ai_calls_current_candidate"] = 0
    frames, packet = market_packet(symbol)
    data = packet_text(packet)

    analyst_results = []
    focus_map = {
        "🧭 Technical Analyst": "technical",
        "⚡ Momentum Analyst": "momentum",
        "📚 Order Flow Analyst": "orderflow",
        "🕯️ Price Action Analyst": "priceaction",
    }
    for name, rule in ANALYSTS:
        analyst_results.append({
            "agent": name,
            **call_ai(
                name,
                specialist_data(packet, focus_map[name]),
                rule + " Berikan thesis mandiri untuk diteruskan ke Bull dan Bear.",
                tier="analyst",
            ),
        })

    analyst_summary = analyst_summary_text(analyst_results)

    debate_data = data + "\n\nRINGKASAN ANALYST:\n" + analyst_summary
    bull = call_ai(
        "🐂 Bull Researcher",
        debate_data,
        "Bangun kasus LONG paling kuat. Tunjukkan bukti utama, kondisi yang membatalkan thesis, dan risiko terbesar. Jangan menjadi final judge.",
        tier="debate",
    )
    bear_data = debate_data + "\n\nTHESIS BULL YANG HARUS DISERANG:\n" + compact_json(bull, 1100)
    bear = call_ai(
        "🐻 Bear Researcher",
        bear_data,
        "Serang thesis Bull secara spesifik. Cari bukti yang bertentangan, resistance, overextension, flow berlawanan, false breakout, konflik timeframe, dan alasan WAIT/SHORT. Jangan sekadar berbeda pendapat tanpa bukti.",
        tier="debate",
    )
    debate = debate_summary_text(bull, bear)

    research_input = (
        data
        + "\n\nANALYST SUMMARY:\n" + analyst_summary
        + "\n\nBULL vs BEAR:\n" + debate
        + "\n\nMEMORY HISTORIS PAPER TRADE:\n" + memory_context(symbol)
    )
    research_manager = call_ai(
        "🧠 Research Manager",
        research_input,
        "Sintesis bukti terbaik. Nilai konflik timeframe, kualitas Bull vs Bear, dan tetapkan thesis riset serta kondisi yang wajib terpenuhi sebelum entry.",
        tier="research",
    )

    trader_input = (
        f"Market: {packet['symbol']} price={packet['price']:.8f}, ATR15={packet['timeframes']['15m']['atr']:.8f}, "
        f"support={packet['support']:.8f}, resistance={packet['resistance']:.8f}, regime={packet['market_regime']}\n"
        f"Research: {compact_json(research_manager)}\n"
        f"Bull/Bear: {debate}"
    )
    trader = call_ai(
        "💹 Trader",
        trader_input,
        "Ubah thesis menjadi rencana paper-trade LONG/SHORT/WAIT. Fokus entry condition, invalidation, dan apakah setup layak secara risk/reward. Jangan menjadi final judge.",
        tier="analyst",
        budget=240,
    )

    risk_specs = [
        ("🟢 Aggressive Risk Manager", "Toleransi risiko lebih tinggi; cari peluang tetapi tetap cek invalidation."),
        ("🟡 Neutral Risk Manager", "Seimbangkan peluang vs risiko dan validasi setup."),
        ("🔴 Conservative Risk Manager", "Prioritaskan perlindungan modal, spread, volatilitas, dan false breakout."),
    ]
    risk_input = (
        f"Symbol={symbol}; price={packet['price']:.8f}; regime={packet['market_regime']}; "
        f"spread={packet['order_book']['spread']:.3f}%; imbalance={packet['order_book']['imbalance']:+.2%}; "
        f"support={packet['support']:.8f}; resistance={packet['resistance']:.8f}; "
        f"Research={compact_json(research_manager)}; Trader={compact_json(trader)}"
    )
    risk_agents = []
    for name, rule in risk_specs:
        risk_agents.append({
            "agent": name,
            **call_ai(
                name,
                risk_input,
                rule + " Boleh menolak rencana Trader jika risiko tidak layak.",
                tier="analyst",
                budget=220,
            ),
        })
    risk_summary = risk_summary_text(risk_agents)

    judge_input = (
        f"MARKET: {symbol} price={packet['price']:.8f} regime={packet['market_regime']} "
        f"support={packet['support']:.8f} resistance={packet['resistance']:.8f} "
        f"spread={packet['order_book']['spread']:.3f}% imbalance={packet['order_book']['imbalance']:+.2%}\n"
        f"ANALYSTS:\n{analyst_summary}\n"
        f"BULL/BEAR:\n{debate}\n"
        f"RESEARCH:\n{compact_json(research_manager)}\n"
        f"TRADER:\n{compact_json(trader)}\n"
        f"RISK:\n{risk_summary}\n"
        f"MEMORY: {memory_context(symbol, 3)}"
    )
    portfolio = call_ai(
        "👨‍⚖️ Final Judge",
        judge_input,
        "Ambil keputusan final LONG/SHORT/WAIT. Jika LONG/SHORT, tentukan angka pasti 'tp_price' dan 'sl_price' menggunakan support/resistance atau ATR yang ada di data. Jangan asal menebak mayoritas buta. Prioritaskan kualitas bukti, konflik timeframe, Bull vs Bear, research, trader plan, risk team, spread, regime, dan jarak support/resistance. Jika konflik berat atau risk/reward buruk, WAIT.",
        tier="judge",
    )

    final = portfolio["decision"]

    analyst_direction = sum(
        1 if x["decision"] == "LONG" else -1 if x["decision"] == "SHORT" else 0
        for x in analyst_results
    )
    risk_direction = sum(
        1 if x["decision"] == "LONG" else -1 if x["decision"] == "SHORT" else 0
        for x in risk_agents
    )
    judge_conf = portfolio.get("confidence", 0)

    if judge_conf < 55:
        final = "WAIT"
    else:
        support_dir = analyst_direction + risk_direction
        if final == "LONG" and support_dir <= -3:
            final = "WAIT"
        elif final == "SHORT" and support_dir >= 3:
            final = "WAIT"

    current = packet["price"]
    atr15 = packet["timeframes"]["15m"]["atr"]
    
    if final == "LONG" or final == "SHORT":
        ai_tp = portfolio.get("tp_price", 0.0)
        ai_sl = portfolio.get("sl_price", 0.0)
        
        # Menggunakan harga AI jika valid, jika AI mengembalikan 0 maka menggunakan ATR sebagai cadangan
        if ai_tp > 0 and ai_sl > 0 and ai_tp != current and ai_sl != current:
            tp = ai_tp
            sl = ai_sl
        else:
            if final == "LONG":
                sl = current - 1.0 * atr15
                tp = current + 2.0 * atr15
            else:
                sl = current + 1.0 * atr15
                tp = current - 2.0 * atr15
                
        # Menghitung Risk Reward (RR) dinamis dari keputusan AI
        risk = abs(current - sl)
        reward = abs(tp - current)
        rr = reward / risk if risk > 0 else 0.0
    else:
        sl = None
        tp = None
        rr = 0.0

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
        "model_tiers": MODEL_TIERS,
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
# V5.5 Performance Lab / Trade Journal
# -------------------------
def journal_rows(limit=500):
    con = db()
    rows = con.execute(
        """
        SELECT * FROM signals
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    con.close()
    return rows


def performance_advanced():
    con = db()
    closed = con.execute(
        """
        SELECT * FROM signals
        WHERE result IN ('TP','SL')
        ORDER BY id ASC
        """
    ).fetchall()
    open_rows = con.execute(
        """
        SELECT * FROM signals
        WHERE result IS NULL AND final_decision IN ('LONG','SHORT')
        ORDER BY id ASC
        """
    ).fetchall()
    con.close()

    rs = [float(r['r_multiple'] or 0) for r in closed]
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x < 0]
    total_r = sum(rs)
    expectancy = total_r / len(rs) if rs else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_profit / gross_loss if gross_loss else (float('inf') if gross_profit else 0.0)

    equity = []
    cur = 0.0
    peak = 0.0
    max_dd = 0.0
    for x in rs:
        cur += x
        peak = max(peak, cur)
        max_dd = min(max_dd, cur - peak)
        equity.append(cur)

    durations = []
    for r in closed:
        try:
            if r['created_at'] and r['closed_at']:
                a = datetime.fromisoformat(r['created_at'].replace('Z', '+00:00'))
                b = datetime.fromisoformat(r['closed_at'].replace('Z', '+00:00'))
                durations.append(max(0, (b-a).total_seconds()/3600))
        except Exception:
            pass

    by_side = {}
    for side in ('LONG','SHORT'):
        vals = [float(r['r_multiple'] or 0) for r in closed if r['final_decision'] == side]
        by_side[side] = {
            'count': len(vals),
            'win_rate': (sum(x > 0 for x in vals)/len(vals)*100) if vals else 0,
            'avg_r': (sum(vals)/len(vals)) if vals else 0,
            'total_r': sum(vals),
        }

    buckets = []
    for lo, hi in ((50,59),(60,69),(70,79),(80,89),(90,100)):
        vals = [float(r['r_multiple'] or 0) for r in closed if lo <= float(r['confidence'] or 0) <= hi]
        buckets.append({
            'bucket': f'{lo}-{hi}',
            'count': len(vals),
            'win_rate': (sum(x > 0 for x in vals)/len(vals)*100) if vals else 0,
            'avg_r': (sum(vals)/len(vals)) if vals else 0,
        })

    return {
        'closed': len(closed),
        'open': len(open_rows),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': (len(wins)/len(rs)*100) if rs else 0,
        'avg_r': expectancy,
        'expectancy_r': expectancy,
        'profit_factor': pf,
        'total_r': total_r,
        'max_drawdown_r': abs(max_dd),
        'avg_duration_h': sum(durations)/len(durations) if durations else 0,
        'equity': equity,
        'closed_rows': closed,
        'open_rows': open_rows,
        'by_side': by_side,
        'confidence_buckets': buckets,
    }


def agent_performance():
    con = db()
    rows = con.execute(
        """
        SELECT av.agent, av.decision, s.result, s.r_multiple
        FROM agent_votes av
        JOIN signals s ON s.id = av.signal_id
        WHERE s.result IN ('TP','SL')
        ORDER BY av.id ASC
        """
    ).fetchall()
    con.close()

    grouped = {}
    for r in rows:
        agent = r['agent']
        grouped.setdefault(agent, []).append(r)

    out = []
    for agent, vals in grouped.items():
        considered = [v for v in vals if v['decision'] in ('LONG','SHORT')]
        signal_correct = 0
        directional = 0
        total_r = 0.0
        for v in vals:
            if v['decision'] == 'WAIT':
                continue
            directional += 1
            trade_won = v['result'] == 'TP'
            signal_side = v['decision']
        out.append((agent, len(vals)))

    con = db()
    rows = con.execute(
        """
        SELECT av.agent, av.decision, s.final_decision, s.result, s.r_multiple
        FROM agent_votes av
        JOIN signals s ON s.id = av.signal_id
        WHERE s.result IN ('TP','SL')
        """
    ).fetchall()
    con.close()
    grouped = {}
    for r in rows:
        grouped.setdefault(r['agent'], []).append(r)

    result = []
    for agent, vals in grouped.items():
        non_wait = [v for v in vals if v['decision'] in ('LONG','SHORT')]
        aligned = [v for v in non_wait if v['decision'] == v['final_decision']]
        correct = 0
        for v in non_wait:
            actual_direction = v['final_decision'] if v['result'] == 'TP' else (
                'SHORT' if v['final_decision'] == 'LONG' else 'LONG'
            )
            if v['decision'] == actual_direction:
                correct += 1
        avg_r = sum(float(v['r_multiple'] or 0) for v in vals) / len(vals) if vals else 0
        result.append({
            'agent': agent,
            'votes': len(vals),
            'directional_votes': len(non_wait),
            'wait_rate': (sum(v['decision']=='WAIT' for v in vals)/len(vals)*100) if vals else 0,
            'aligned_final': (len(aligned)/len(non_wait)*100) if non_wait else 0,
            'outcome_accuracy': (correct/len(non_wait)*100) if non_wait else 0,
            'avg_signal_r': avg_r,
        })
    return sorted(result, key=lambda x: x['outcome_accuracy'], reverse=True)


def render_performance_lab():
    st.subheader('🧪 V5.5 Performance Lab')
    st.caption('Dashboard ini mengevaluasi PAPER TRADE yang sudah benar-benar ditutup. Belum ada klaim bahwa sistem memiliki edge.')
    adv = performance_advanced()

    if adv['closed'] == 0:
        st.info('Belum ada paper trade tertutup. Jalankan analisis, simpan signal LONG/SHORT, lalu gunakan UPDATE PAPER TRADES setelah harga bergerak.')
        if adv['open_rows']:
            st.markdown('#### 📂 Open Paper Trades')
            st.dataframe(pd.DataFrame([{
                'ID': r['id'], 'Symbol': r['symbol'], 'Side': r['final_decision'],
                'Confidence': round(float(r['confidence'] or 0)), 'Entry': r['entry'],
                'SL': r['stop_loss'], 'TP': r['take_profit'], 'Regime': r['market_regime']
            } for r in adv['open_rows']]), use_container_width=True, hide_index=True)
        return

    c = st.columns(7)
    c[0].metric('Closed', adv['closed'])
    c[1].metric('Win Rate', f"{adv['win_rate']:.1f}%")
    c[2].metric('Expectancy', f"{adv['expectancy_r']:+.2f}R")
    pf = '∞' if math.isinf(adv['profit_factor']) else f"{adv['profit_factor']:.2f}"
    c[3].metric('Profit Factor', pf)
    c[4].metric('Total R', f"{adv['total_r']:+.1f}R")
    c[5].metric('Max DD', f"-{adv['max_drawdown_r']:.1f}R")
    c[6].metric('Open', adv['open'])

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        y=adv['equity'],
        mode='lines+markers',
        name='Cumulative R',
    ))
    fig.add_hline(y=0, line_dash='dot')
    fig.update_layout(height=320, title='Paper Equity Curve (R)', xaxis_title='Closed Trade', yaxis_title='Cumulative R')
    st.plotly_chart(fig, use_container_width=True)

    left, right = st.columns(2)
    with left:
        st.markdown('#### 📈 LONG vs SHORT')
        st.dataframe(pd.DataFrame([
            {'Side': side, **vals} for side, vals in adv['by_side'].items()
        ]), use_container_width=True, hide_index=True)
    with right:
        st.markdown('#### 🎯 Confidence Calibration')
        st.dataframe(pd.DataFrame(adv['confidence_buckets']), use_container_width=True, hide_index=True)

    st.markdown('#### 🧠 Agent Performance')
    agents = agent_performance()
    if agents:
        st.dataframe(pd.DataFrame(agents), use_container_width=True, hide_index=True)
    else:
        st.info('Belum cukup histori untuk mengukur performa agent.')

    st.markdown('#### 📓 Trade Journal')
    rows = []
    for r in adv['closed_rows']:
        rows.append({
            'ID': r['id'],
            'Waktu': r['created_at'][:19].replace('T',' '),
            'Symbol': r['symbol'],
            'Side': r['final_decision'],
            'Confidence': round(float(r['confidence'] or 0)),
            'Regime': r['market_regime'],
            'Entry': r['entry'],
            'Exit': r['exit_price'],
            'Result': r['result'],
            'R': float(r['r_multiple'] or 0),
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

# -------------------------
# Telegram
# -------------------------
def telegram(message):
    if not TG_TOKEN or not TG_CHAT:
        return False, "Telegram belum dikonfigurasi."

    r = requests.post(
        f"[https://api.telegram.org/bot](https://api.telegram.org/bot){TG_TOKEN}/sendMessage",
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

    return f"""🚨 AI TRADING V5.5

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

st.title("🧠 AI Consensus Trading V5.5")
st.caption(
    "Crypto Scanner → ⚡ Analyst → 🐂 Bull ↔ 🐻 Bear → 🧠 Research → 💹 Trader → "
    "🛡️ Risk Team → 👨‍⚖️ Final Judge → Memory → Paper Trading"
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
    "🟢 SIAP" if GROQ_KEYS else "🔴 BELUM ADA"
)
if GROQ_KEYS:
    active_key = int(st.session_state.get("_active_groq_key", 0)) % len(GROQ_KEYS)
    available = sum(not _key_disabled("groq", i) for i in range(len(GROQ_KEYS)))
    st.sidebar.caption(
        f"🔑 API keys: {len(GROQ_KEYS)} | tersedia sekarang: {available} | aktif: #{active_key + 1}"
    )
else:
    st.sidebar.caption("Tambahkan GROQ_API_KEY atau GROQ_API_KEYS di Streamlit Secrets.")
st.sidebar.caption("Arsitektur model: ringan → menengah → kuat → terbaik")
st.sidebar.caption(f"⚡ Analyst: {ANALYST_MODEL} | reasoning low")
st.sidebar.caption(f"🧠 Bull/Bear: {DEBATE_MODEL} | structured debate")
st.sidebar.caption(f"🧠🧠 Research: {RESEARCH_MODEL} | reasoning low + strict JSON")
st.sidebar.caption(f"👨‍⚖️ Judge: {JUDGE_MODEL} | reasoning low + strict JSON")
st.sidebar.caption("Output agent dipadatkan agar hemat token dan mengurangi TPM rate-limit.")
st.sidebar.caption(f"⏱️ Jeda AI: {GROQ_MIN_DELAY:.1f}s | Retry 429: aktif")

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
    ok, msg = telegram("✅ TEST AI CONSENSUS TRADING V5.5\nTelegram berhasil terhubung.")
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

st.subheader("📊 V5.5 Performance Lab")

p1, p2, p3, p4, p5 = st.columns(5)
p1.metric("Signal Closed", perf["closed"])
p2.metric("Open Paper Trade", perf["open"])
p3.metric("Win Rate", f"{perf['win_rate']:.1f}%")
p4.metric("Average R", f"{perf['avg_r']:+.2f}R")
pf = "∞" if math.isinf(perf["profit_factor"]) else f"{perf['profit_factor']:.2f}"
p5.metric("Profit Factor", pf)

with st.expander("🧪 Buka V5.5 Performance Lab", expanded=True):
    render_performance_lab()

st.divider()

# -------------------------
# Scanner results
# -------------------------
if not st.session_state.scan:
    st.info(
        "Klik **SCAN MARKET** untuk mencari kandidat crypto. "
        "V5.5 tidak mengeksekusi order."
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
                    f"Signal tersimpan ke Trade Journal V5.5. ID: {signal_id}"
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
                    f"WAIT tersimpan ke Trade Journal V5.5. ID: {signal_id}"
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
    "V5.5 adalah sistem riset/paper trading. Tidak mengeksekusi order otomatis. "
    "Hasil historis tidak menjamin hasil masa depan."
)
