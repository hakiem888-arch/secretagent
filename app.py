
import os, requests, pandas as pd, streamlit as st
import plotly.graph_objects as go
from langchain_groq import ChatGroq

st.set_page_config(page_title="AI Consensus Trading V4.1", page_icon="🧠", layout="wide")
BYBIT="https://api.bybit.com"
BYBIT_CATEGORY="linear"  # public USDT perpetual market data

def secret(k):
    try:
        if k in st.secrets:
            return st.secrets[k]
    except Exception:
        pass
    return os.getenv(k, "")

GROQ_KEY=secret("GROQ_API_KEY")
TG_TOKEN=secret("TELEGRAM_BOT_TOKEN")
TG_CHAT=secret("TELEGRAM_CHAT_ID")

def bget(path, params=None):
    """Bybit V5 public market-data request; no API key required."""
    r=requests.get(
        BYBIT+path,
        params=params,
        timeout=15,
        headers={"User-Agent":"AI-Consensus-Trading-V4/1.0"}
    )
    if r.status_code != 200:
        raise RuntimeError(f"Bybit HTTP {r.status_code}: {r.text[:300]}")
    data=r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(
            f"Bybit {data.get('retCode')}: {data.get('retMsg')}"
        )
    return data["result"]

def ema(s,n): return s.ewm(span=n,adjust=False).mean()
def rsi(s,n=14):
    d=s.diff(); g=d.where(d>0,0).ewm(alpha=1/n,adjust=False).mean()
    l=(-d.where(d<0,0)).ewm(alpha=1/n,adjust=False).mean()
    return 100-100/(1+g/l.replace(0,pd.NA))
def atr(df,n=14):
    tr=pd.concat([df.High-df.Low,(df.High-df.Close.shift()).abs(),
                  (df.Low-df.Close.shift()).abs()],axis=1).max(axis=1)
    return tr.rolling(n).mean()
def macd(s):
    m=ema(s,12)-ema(s,26); sig=ema(m,9)
    return m,sig,m-sig
def bb(s,n=20):
    m=s.rolling(n).mean(); sd=s.rolling(n).std()
    return m,m+2*sd,m-2*sd

def klines(symbol, interval="15m", limit=180):
    x=bget("/api/v3/klines",{"symbol":symbol,"interval":interval,"limit":limit})
    c=["time","Open","High","Low","Close","Volume","close_time","QuoteVolume",
       "Trades","TakerBuyBase","TakerBuyQuote","ignore"]
    d=pd.DataFrame(x,columns=c)
    for z in ["Open","High","Low","Close","Volume","QuoteVolume","Trades","TakerBuyBase","TakerBuyQuote"]:
        d[z]=pd.to_numeric(d[z],errors="coerce")
    d.index=pd.to_datetime(d.time,unit="ms",utc=True)
    d["EMA20"]=ema(d.Close,20); d["EMA50"]=ema(d.Close,50); d["RSI"]=rsi(d.Close)
    d["MACD"],d["MACD_SIGNAL"],d["MACD_HIST"]=macd(d.Close)
    d["ATR"]=atr(d); d["BB_MID"],d["BB_UP"],d["BB_LOW"]=bb(d.Close)
    d["VOL_RATIO"]=d.Volume/d.Volume.rolling(20).mean()
    return d.dropna()

def candle_info(d):
    a=d.iloc[-1]; p=d.iloc[-2]; body=abs(a.Close-a.Open); rng=max(a.High-a.Low,1e-12)
    uw=a.High-max(a.Open,a.Close); lw=min(a.Open,a.Close)-a.Low
    br=body/rng
    if br<.1: name="DOJI"
    elif lw>body*2 and uw<max(body,1e-12): name="HAMMER BULLISH"
    elif uw>body*2 and lw<max(body,1e-12): name="SHOOTING STAR BEARISH"
    elif a.Close>a.Open and p.Close<p.Open and a.Open<=p.Close and a.Close>=p.Open: name="BULLISH ENGULFING"
    elif a.Close<a.Open and p.Close>p.Open and a.Open>=p.Close and a.Close<=p.Open: name="BEARISH ENGULFING"
    elif a.Close>a.Open: name="BULLISH"
    elif a.Close<a.Open: name="BEARISH"
    else: name="NETRAL"
    return name,br,uw/rng,lw/rng

def book(symbol):
    x=bget("/api/v3/depth",{"symbol":symbol,"limit":20})
    bid=sum(float(v) for _,v in x["bids"]); ask=sum(float(v) for _,v in x["asks"])
    total=bid+ask
    imb=(bid-ask)/total if total else 0
    bbid=float(x["bids"][0][0]) if x["bids"] else 0
    bask=float(x["asks"][0][0]) if x["asks"] else 0
    spread=(bask-bbid)/bbid*100 if bbid else 0
    return imb,spread

def flow(d):
    total=float(d.TakerBuyQuote.iloc[-1] / max(d.QuoteVolume.iloc[-1],1e-12))
    return total

def market_data(d,ob):
    a=d.iloc[-1]; candle,br,uw,lw=candle_info(d)
    buy=flow(d); support=float(d.Low.tail(30).min()); resistance=float(d.High.tail(30).max())
    vol=float(a.ATR/a.Close)
    regime="VOLATILE" if vol>=.03 else ("SIDEWAYS" if abs(a.EMA20-a.EMA50)/a.Close<.005 else "TRENDING")
    return dict(price=float(a.Close),ema20=float(a.EMA20),ema50=float(a.EMA50),
                rsi=float(a.RSI),macd=float(a.MACD),hist=float(a.MACD_HIST),
                atr=float(a.ATR),bbup=float(a.BB_UP),bblow=float(a.BB_LOW),
                volratio=float(a.VOL_RATIO),buy=buy,imb=ob[0],spread=ob[1],
                candle=candle,body=br,uw=uw,lw=lw,support=support,
                resistance=resistance,regime=regime)

def scanner_score(x):
    s=50; why=[]
    if x["change"]>=3: s+=12; why.append("harga naik kuat")
    elif x["change"]>=1: s+=6; why.append("momentum positif")
    elif x["change"]<=-3: s-=12; why.append("harga turun kuat")
    elif x["change"]<=-1: s-=6
    if x["vr"]>=3: s+=18; why.append("volume sangat tinggi")
    elif x["vr"]>=2: s+=12; why.append("volume meningkat")
    elif x["vr"]>=1.5: s+=6
    if x["buy"]>=.60: s+=12; why.append("tekanan beli dominan")
    elif x["buy"]>=.55: s+=6
    elif x["buy"]<=.40: s-=12; why.append("tekanan jual dominan")
    if x["imb"]>=.20: s+=8; why.append("bid lebih tebal")
    elif x["imb"]<=-.20: s-=8
    if 50<=x["rsi"]<=68: s+=6
    elif x["rsi"]>78: s-=8
    return max(0,min(100,round(s))),why

def scan(n):
    tick=bget("/api/v3/ticker/24hr"); base=[]
    for t in tick:
        s=t.get("symbol","")
        if not s.endswith("USDT"): continue
        basecoin=s[:-4]
        if basecoin in {"USDC","FDUSD","TUSD","USDP","DAI","BUSD"}: continue
        q=float(t.get("quoteVolume",0))
        if q<2_000_000: continue
        base.append((s,float(t["priceChangePercent"]),q))
    base=sorted(base,key=lambda z:z[2],reverse=True)[:n]
    out=[]; bar=st.progress(0); msg=st.empty()
    for i,(s,ch,q) in enumerate(base):
        msg.write(f"🔎 Memindai {s} ({i+1}/{len(base)})")
        try:
            d=klines(s,limit=80); ob=book(s); a=d.iloc[-1]
            buy=flow(d); x={"symbol":s,"change":ch,"qvol":q,"vr":float(a.VOL_RATIO),
                            "buy":buy,"imb":ob[0],"rsi":float(a.RSI)}
            x["score"],x["why"]=scanner_score(x); out.append(x)
        except Exception: pass
        bar.progress((i+1)/max(len(base),1))
    bar.empty(); msg.empty()
    return sorted(out,key=lambda z:z["score"],reverse=True)

def model():
    if not GROQ_KEY: return None
    return ChatGroq(api_key=GROQ_KEY,model="openai/gpt-oss-120b",temperature=.1)

def ai(role,data,rule):
    m=model()
    if not m: return "WAIT\nGROQ_API_KEY belum dikonfigurasi."
    p=f"""Kamu adalah {role} dalam ruang trading profesional.
DATA:
{data}
ATURAN:
{rule}
Gunakan hanya data di atas. Jangan mengarang. Jangan menjanjikan profit.
Baris pertama WAJIB hanya LONG, SHORT, atau WAIT.
Setelah itu jelaskan singkat dalam Bahasa Indonesia."""
    try: return m.invoke(p).content.strip()
    except Exception as e: return f"WAIT\nAI error: {e}"

def vote(t):
    z=t.strip().upper().splitlines()[0] if t.strip() else ""
    return "LONG" if z.startswith("LONG") else ("SHORT" if z.startswith("SHORT") else "WAIT")

def deep(symbol):
    d=klines(symbol); ob=book(symbol); m=market_data(d,ob)
    text=f"""Symbol: {symbol}
Harga: ${m['price']:,.8f}
EMA20: ${m['ema20']:,.8f} | EMA50: ${m['ema50']:,.8f}
RSI: {m['rsi']:.2f}
MACD Histogram: {m['hist']:.8f}
ATR: ${m['atr']:,.8f}
Bollinger Upper/Lower: ${m['bbup']:,.8f} / ${m['bblow']:,.8f}
Volume Ratio: {m['volratio']:.2f}x
Buy Pressure: {m['buy']:.1%}
Order Book Imbalance: {m['imb']:+.1%}
Spread: {m['spread']:.3f}%
Candlestick: {m['candle']} | Body {m['body']:.1%} | Upper Wick {m['uw']:.1%} | Lower Wick {m['lw']:.1%}
Support: ${m['support']:,.8f}
Resistance: ${m['resistance']:,.8f}
Market Regime: {m['regime']}"""
    specs=[
        ("🧭 AI Trend",25,"Fokus EMA20, EMA50 dan market regime."),
        ("⚡ AI Momentum",25,"Fokus RSI, MACD, volume, buy pressure dan order book."),
        ("📐 AI Oscillator",20,"Fokus RSI, Bollinger, ATR dan risiko volatilitas."),
        ("🕯️ AI Candlestick",30,"Fokus pola candle, wick, rejection, support/resistance dan price action.")
    ]
    agents=[]
    for name,w,rule in specs:
        t=ai(name,text,rule); v=vote(t); agents.append((name,w,v,t))
    score=sum(w if v=="LONG" else -w if v=="SHORT" else 0 for _,w,v,_ in agents)
    warnings=[]
    if m["regime"]=="VOLATILE": warnings.append("Market sangat volatil.")
    if m["spread"]>.20: warnings.append("Spread relatif lebar.")
    if m["volratio"]<.8: warnings.append("Volume belum mendukung.")
    if score>0 and m["price"]>m["resistance"]*.995: warnings.append("Harga dekat resistance.")
    if score<0 and m["price"]<m["support"]*1.005: warnings.append("Harga dekat support.")
    votes="\n".join(f"{n}: {v} (bobot {w})" for n,w,v,_ in agents)
    judge=ai("👨‍⚖️ AI Judge",f"{text}\n\nVOTING:\n{votes}\nSKOR: {score}/100\nRISK: {warnings or ['Tidak ada warning besar.']}",
             "Jangan mengikuti mayoritas secara buta. Jika risiko tinggi atau bukti konflik, pilih WAIT.")
    final=vote(judge)
    if abs(score)<40: final="WAIT"
    return d,m,agents,score,warnings,judge,final

def telegram(msg):
    if not TG_TOKEN or not TG_CHAT: return False,"Telegram belum dikonfigurasi."
    r=requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                    json={"chat_id":TG_CHAT,"text":msg},timeout=12)
    if r.status_code!=200: return False,r.text[:300]
    return bool(r.json().get("ok")),r.json().get("description","OK")

def tg_message(symbol,m,agents,score,final):
    icon="🚀" if final=="LONG" else "🩸"
    sl=m["price"]-m["atr"] if final=="LONG" else m["price"]+m["atr"]
    tp=m["price"]+2*m["atr"] if final=="LONG" else m["price"]-2*m["atr"]
    votes="\n".join(f"{n}: {v}" for n,_,v,_ in agents)
    return f"""🚨 AI TRADING ALERT

{icon} {symbol}
SIGNAL: {final}
SCORE: {score}/100

4 AI:
{votes}

💰 Entry: ${m['price']:,.8f}
🎯 TP: ${tp:,.8f}
🛑 SL: ${sl:,.8f}

📊 Volume: {m['volratio']:.2f}x
🔥 Buy Pressure: {m['buy']:.1%}
📚 Order Imbalance: {m['imb']:+.1%}
🕯 Candle: {m['candle']}
📈 RSI: {m['rsi']:.2f}
🌡 Market: {m['regime']}

⚠️ Analisis eksperimental, bukan jaminan profit."""

if "scan" not in st.session_state: st.session_state.scan=[]
if "sent" not in st.session_state: st.session_state.sent=set()

st.title("🧠 AI Consensus Trading V4")
st.caption("Scanner → Volume & Order Flow → 4 AI → Risk Gate → AI Judge → Telegram")
st.sidebar.header("🔎 Scanner")
scan_n=st.sidebar.slider("Koin yang discan",20,100,60)
top_n=st.sidebar.slider("Kandidat dianalisis AI",1,5,3)
scan_btn=st.sidebar.button("🔍 SCAN MARKET",use_container_width=True)
st.sidebar.divider()
st.sidebar.write("Groq:", "✅ SIAP" if GROQ_KEY else "❌ BELUM ADA")
st.sidebar.header("📱 Telegram")
tg_on=st.sidebar.checkbox("Kirim alert Telegram",bool(TG_TOKEN and TG_CHAT))
min_score=st.sidebar.slider("Minimum skor alert",40,100,75)
if st.sidebar.button("📨 Test Telegram",use_container_width=True):
    ok,msg=telegram("✅ TEST AI CONSENSUS TRADING V4\nTelegram berhasil terhubung.")
    st.sidebar.success(msg) if ok else st.sidebar.error(msg)

if scan_btn:
    with st.spinner("🛰️ Mencari koin dengan volume, momentum dan order flow kuat..."):
        try: st.session_state.scan=scan(scan_n)
        except Exception as e: st.error(f"Scanner gagal: {e}")

if not st.session_state.scan:
    st.info("Klik SCAN MARKET untuk memulai.")
    st.stop()

st.subheader("🔥 Top Kandidat")
rows=[]
for i,x in enumerate(st.session_state.scan[:10],1):
    rows.append({"Rank":i,"Symbol":x["symbol"],"Score":x["score"],
                 "24h":f"{x['change']:+.2f}%","Volume 24h":f"${x['qvol']:,.0f}",
                 "Vol Ratio":f"{x['vr']:.2f}x","Buy Pressure":f"{x['buy']:.1%}",
                 "Order Imbalance":f"{x['imb']:+.1%}","RSI":f"{x['rsi']:.2f}"})
st.dataframe(pd.DataFrame(rows),use_container_width=True,hide_index=True)

st.divider()
st.subheader(f"🧠 Analisis Mendalam Top {top_n}")

for rank,c in enumerate(st.session_state.scan[:top_n],1):
    s=c["symbol"]
    st.markdown(f"## #{rank} {s} — Scanner {c['score']}/100")
    with st.spinner(f"🤖 4 AI + Judge menganalisis {s}..."):
        try: d,m,agents,score,warnings,judge,final=deep(s)
        except Exception as e:
            st.error(f"Gagal menganalisis {s}: {e}"); continue
    a,b,cc,dcol,e=st.columns(5)
    a.metric("Harga",f"${m['price']:,.6f}"); b.metric("RSI",f"{m['rsi']:.2f}")
    cc.metric("Volume",f"{m['volratio']:.2f}x"); dcol.metric("Buy",f"{m['buy']:.1%}")
    e.metric("Order Book",f"{m['imb']:+.1%}")
    ch=d.tail(100)
    fig=go.Figure()
    fig.add_trace(go.Candlestick(x=ch.index,open=ch.Open,high=ch.High,low=ch.Low,close=ch.Close,name="Candle"))
    fig.add_trace(go.Scatter(x=ch.index,y=ch.EMA20,name="EMA20"))
    fig.add_trace(go.Scatter(x=ch.index,y=ch.EMA50,name="EMA50"))
    fig.update_layout(height=450,template="plotly_dark",xaxis_rangeslider_visible=False)
    st.plotly_chart(fig,use_container_width=True)
    cols=st.columns(4)
    for col,(name,w,v,t) in zip(cols,agents):
        with col:
            if v=="LONG": col.success(f"**{name} — LONG**\n\n{t}")
            elif v=="SHORT": col.error(f"**{name} — SHORT**\n\n{t}")
            else: col.warning(f"**{name} — WAIT**\n\n{t}")
    st.write(f"### ⚖️ Skor Konsensus: **{score:+d}/100**")
    if warnings: st.warning("⚠️ Risk Gate:\n\n"+"\n".join("- "+x for x in warnings))
    if final=="LONG": st.success(f"🚀 **AI JUDGE: LONG — {s}**")
    elif final=="SHORT": st.error(f"🩸 **AI JUDGE: SHORT — {s}**")
    else: st.warning(f"⚖️ **AI JUDGE: WAIT — {s}**")
    st.write(judge)
    if final in ("LONG","SHORT"):
        sl=m["price"]-m["atr"] if final=="LONG" else m["price"]+m["atr"]
        tp=m["price"]+2*m["atr"] if final=="LONG" else m["price"]-2*m["atr"]
        st.write(f"Entry **${m['price']:,.8f}** | TP **${tp:,.8f}** | SL **${sl:,.8f}**")
        key=f"{s}|{final}|{score}|{d.index[-1]}"
        if tg_on and abs(score)>=min_score and key not in st.session_state.sent:
            ok,msg=telegram(tg_message(s,m,agents,score,final))
            if ok:
                st.success("📱 Alert Telegram terkirim.")
                st.session_state.sent.add(key)
            else: st.error(f"Telegram gagal: {msg}")
    st.divider()

st.caption("V4 hanya melakukan scanning dan analisis; tidak mengeksekusi order otomatis. Sinyal bersifat eksperimental dan bukan jaminan profit.")
