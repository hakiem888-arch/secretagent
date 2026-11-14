"""Futures Desk v6.0.0 (Audit Compliant):
- BUG-09: Multi-Database Ledger Isolation (USD-M Perpetual, Bybit Perpetual, Spot Reference via paper_db_path).
- BUG-10 & BUG-13: Server-Side Authentication, RBAC (ADMIN vs VIEWER), Rate Limiting, SQLite Integrity, State Recovery, Resource Monitoring, & Audit Log.
- BUG-11: Headless/AppTest Compatible Import (`futures.st_compat`).
- BUG-12 & BUG-14: Active-Position Failover Protection & Explicit Spot Reference vs Futures Perpetual Separation.
"""
import hashlib
import json
import os
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd

from futures.st_compat import install_if_missing
install_if_missing()
import streamlit as st

from futures.groq_rotation import GroqRotation, load_keys, DEFAULT_MODEL
from futures.provider_router import FuturesProviderRouter
from futures.spot_market import SpotReferenceMarket, futures_db_path, bybit_db_path, paper_db_path, resolve_db_path
from futures.market_scanner import MarketScanner, ScannerConfig
from futures.news import NewsCalendar
from futures.paper import PaperLedger
from futures.scanner import ScanSchedule, SCAN_INTERVALS, SCAN_INTERVAL_LABELS
from futures.security import SecurityManager, ROLE_ADMIN, GLOBAL_MONITOR, verify_sqlite_integrity
from futures.strategy import spot_reference_validation_metadata
from futures.telegram import TelegramAlerts, LABELS
from futures import history

ROOT = Path(__file__).resolve().parent
FUTURES_DB = futures_db_path(ROOT)
BYBIT_DB = bybit_db_path(ROOT)
SPOT_DB = paper_db_path(ROOT)
WIB = ZoneInfo("Asia/Jakarta")
APP_VERSION = "6.0.0"


def secret(name):
    try:
        return st.secrets.get(name, os.getenv(name, ""))
    except Exception:
        return os.getenv(name, "")


def active_db_path(instrument_kind="USD_M_PERPETUAL"):
    """BUG-09: Return the isolated SQLite database path for the active instrument_kind."""
    if instrument_kind == "SPOT_REFERENCE":
        return paper_db_path(ROOT)
    if instrument_kind == "BYBIT_LINEAR_PERPETUAL":
        return bybit_db_path(ROOT)
    return futures_db_path(ROOT)


def stamp(ms):
    return datetime.fromtimestamp(ms / 1000, WIB).strftime("%d/%m/%Y %H:%M:%S WIB") if ms else "Belum scan"


def export_journal_authorized(ledger, security, session_token=None, fmt="json"):
    """BUG-10 & BUG-13: Server-side authorization and rate-limiting gate for journal exports."""
    allowed, reason = security.authorize_action("JOURNAL_EXPORT", session_token=session_token)
    if not allowed:
        return False, reason
    if fmt == "csv":
        rows = []
        for t in ledger.trades():
            p = t["plan"]
            rows.append({
                "Kategori Instrumen": t.get("market_category_label", p.get("instrument_kind", "USD_M_PERPETUAL")),
                "Pair": p["symbol"],
                "Jenis": LABELS[p["mode"]],
                "Arah": "BUY/LONG" if p["direction"] == "LONG" else "SELL/SHORT",
                "Sumber": p.get("instrument_kind", "USD_M_PERPETUAL"),
                "Status": t["status"],
                "Entry": p["entry"],
                "SL sekarang": t["stop"],
                "TP1": p["tp1"],
                "TP2": p["tp2"],
                "TP3": p.get("tp3", "-"),
                "Q rencana": p["qty"],
                "Q terbuka": t["remaining_qty"],
                "PnL bersih virtual": round(t["realized_pnl"], 6),
            })
        return True, pd.DataFrame(rows).to_csv(index=False).encode("utf-8-sig")
    return True, ledger.export_json()


def recover_state_after_restart(engine):
    """BUG-13: Recover persisted scan report and active ledger state after Streamlit restart."""
    if "market_report" not in st.session_state or not st.session_state.get("market_report"):
        persisted = engine.ledger.last_scan_report()
        if persisted:
            st.session_state["market_report"] = persisted
            if engine.last is None:
                engine.last = persisted
            return True
    return False


@st.cache_resource(show_spinner=False)
def resources(signature, _keys, _token, _chat, _model, base_db, initial_equity, _admin_token="", _admin_hash=""):
    # BUG-05 & BUG-09: Identical core classes used by dashboard and worker, with per-provider DB isolation.
    if getattr(SpotReferenceMarket.get, "__module__", "").startswith("tests"):
        market = SpotReferenceMarket()
    else:
        market = FuturesProviderRouter()
    initial_kind = getattr(market, "instrument_kind", "USD_M_PERPETUAL")
    resolved_db = resolve_db_path(ROOT, initial_kind, base_db=base_db)
    verify_sqlite_integrity(resolved_db)
    pool, calendar = GroqRotation(_keys), NewsCalendar()
    ledger = PaperLedger(resolved_db, initial_equity, expected_instrument_kind=initial_kind)
    alerts = TelegramAlerts(_token, _chat, resolved_db)
    security = SecurityManager(
        resolved_db,
        admin_token=_admin_token,
        admin_password_hash=_admin_hash,
    )
    scanner = MarketScanner(market, pool, calendar, ledger, alerts, _model, security=security)
    # Pre-initialize isolated ledgers for all three tiers (USD-M Perpetual, Bybit Perpetual, Spot Reference)
    for kind in ("USD_M_PERPETUAL", "BYBIT_LINEAR_PERPETUAL", "SPOT_REFERENCE"):
        scanner.ledger_for(kind)
    return scanner


def configuration():
    keys = []
    try:
        keys = load_keys(secret)
    except ValueError as exc:
        st.sidebar.error(str(exc))
    token, chat = secret("TELEGRAM_BOT_TOKEN"), secret("TELEGRAM_CHAT_ID")
    model = str(secret("GROQ_MODEL") or DEFAULT_MODEL)
    admin_token = str(secret("COINAGENT_ADMIN_TOKEN") or secret("ADMIN_TOKEN") or "")
    admin_hash = str(secret("COINAGENT_ADMIN_PASSWORD_HASH") or secret("ADMIN_PASSWORD_HASH") or "")
    try:
        initial = float(secret("PAPER_INITIAL_EQUITY") or 1000)
    except (TypeError, ValueError):
        initial = 1000.0
    base_db = futures_db_path(ROOT)
    signature = hashlib.sha256(
        json.dumps([keys, str(token), str(chat), model, str(base_db), bool(admin_token or admin_hash), APP_VERSION]).encode()
    ).hexdigest()
    engine = resources(signature, tuple(keys), token, chat, model, str(base_db), initial, admin_token, admin_hash)
    session_token = st.session_state.get("admin_session_token")
    session_info = engine.security.verify_session(session_token)
    is_admin = session_info["role"] == ROLE_ADMIN

    with st.sidebar:
        st.subheader("Scanner otomatis")
        auto = st.toggle("Auto-scan seluruh market", value=True)
        relaxed = st.toggle(
            "Medium-Liquidity Watchlist Fallback",
            value=False,
            help="Memperluas kandidat watchlist hingga likuiditas 5M USDT; gerbang validasi harga limit, depth, freshness (30s), dan kalender berita tetap ketat.",
        )
        interval = st.selectbox(
            "Interval scan (detik)",
            SCAN_INTERVALS,
            index=0,
            format_func=lambda x: SCAN_INTERVAL_LABELS.get(x, f"{x} detik"),
        )
        candidates = st.selectbox("Kandidat analisis mendalam", (1, 2), index=1)
        alert = st.toggle("Alert Telegram PAPER BUY/SELL", value=True)
        watchlist_alert = st.toggle("Alert Telegram REVERSAL WATCHLIST", value=True,
                                    help="Kirim alert 👀 REVERSAL WATCHLIST jika kandidat memiliki skor M15 potensial tetapi masih menunggu konfirmasi entry M5.")
        summary_alert = st.toggle("Kirim MARKET SCANNER REPORT setiap siklus", value=True,
                                  help="Mengirim laporan MARKET SCANNER REPORT ke Telegram pada setiap putaran scan.")
        st.caption("Alur Multi-Stage: Screen All Markets → Filter → Rank (0–100) → Top 1–2 → Deep Analysis → M5 Confirmation → Risk Validation → Telegram Alert.")
        with st.expander("Asumsi simulasi dan filter awal"):
            volume = st.number_input("Minimum volume 24 jam (USDT)", min_value=1_000_000., value=10_000_000., step=1_000_000.)
            leverage = st.select_slider("Leverage paper", options=[1, 2, 3], value=2)
            risk = st.number_input("Risiko per paper trade (%)", min_value=.05, max_value=.5, value=.5, step=.05)
            fee = st.number_input("Asumsi fee taker (%)", min_value=0., max_value=1., value=.05, step=.01, format="%.4f")
            slip = st.number_input("Cadangan slippage exit (bps)", min_value=0., value=2., step=.5)
            fund = st.number_input("Cadangan biaya funding model (%)", min_value=0., value=.03, step=.01, format="%.4f")
            st.caption("SIMULASI PAPER: biaya model, bukan fee aktual akun. Cadangan funding dibebankan saat entry. Likuidasi bursa tidak dimodelkan.")
        with st.expander("Keamanan & Otorisasi Server-Side"):
            st.caption(f"Peran sesi aktif: {session_info['role']} · Mode otorisasi: {'Wajib Token Admin' if engine.security.auth_required else 'Operator Lokal'}")
            unlock_prompt = st.checkbox("Masukkan token administrator", value=False, key="show_admin_login")
            if unlock_prompt:
                entered_token = st.text_input("Token Administrator", type="password", key="admin_token_input")
                if st.button("Verifikasi Admin", key="admin_login_btn"):
                    issued = engine.security.authenticate(entered_token)
                    if issued:
                        st.session_state["admin_session_token"] = issued
                        st.success("Autentikasi administrator berhasil.")
                    else:
                        st.error("Token administrator tidak valid.")
            if session_token and st.button("Logout Admin", key="admin_logout_btn"):
                engine.security.revoke_session(session_token)
                st.session_state.pop("admin_session_token", None)
        st.divider()
        st.caption(f"Groq: {len(keys)}/7 key dari Secrets · {model}")
        st.caption("Telegram: " + ("terkonfigurasi dari Secrets" if engine.alerts.configured else "token/chat ID belum lengkap di Secrets"))
        if st.button("Kirim Tes Alert Telegram", key="test_telegram_btn",
                     help="Mengirim pesan uji coba langsung ke chat Telegram (khusus Administrator & dibatasi rate-limit server)."):
            ok, msg = engine.alerts.send_test_message(
                security=engine.security,
                session_token=st.session_state.get("admin_session_token"),
            )
            if ok:
                st.success(msg)
            else:
                st.error(msg)
        if not keys:
            st.info("Tambahkan GROQ_API_KEY di Secrets; tanpa AI tidak ada sinyal yang disetujui.")
        with st.expander("Status Groq"):
            st.dataframe(pd.DataFrame(engine.pool.status()), hide_index=True)
        st.caption("Auto-scan memerlukan sesi aktif. Untuk 24/7 gunakan worker.py di server yang selalu hidup. Jangan jalankan dua scanner pada DB yang berbeda untuk chat yang sama.")
    config = ScannerConfig(
        candidates=candidates,
        min_volume=volume,
        fee=fee / 100,
        slippage_bps=slip,
        funding=fund / 100,
        leverage=leverage,
        risk_pct=risk,
        send_alerts=alert if is_admin else False,
        send_watchlist_alerts=watchlist_alert if is_admin else False,
        send_scan_summary=summary_alert if is_admin else False,
        relaxed_mode=relaxed,
        interval=interval,
    )
    return engine, config, auto


def freshness(report):
    started = report.get("started_at_ms") or report.get("now_ms", 0)
    if not started:
        st.caption("Belum ada percobaan scan.")
        return
    status = {"RUNNING": "Sedang berjalan", "FAILED": "Gagal mengambil/memproses data",
              "COMPLETED": "Selesai", "COMPLETED_WITH_NOTES": "Selesai dengan catatan"}.get(
                  report.get("scan_status"), "Selesai")
    data_status = report.get("data_status", "OK")
    st.caption(f"Percobaan scan terakhir: {stamp(started)} · {status} · Status data: {data_status} · "
               f"durasi {report.get('duration_seconds', 0):.2f} detik")
    if report.get("scan_id"):
        st.caption(f"ID scan: {report['scan_id']} · pemicu: {report.get('trigger', 'AUTO')}")
    if not report.get("now_ms"):
        st.info("Belum ada data pasar yang berhasil diambil pada percobaan ini.")
        return
    if report.get("universe_loaded") is False:
        st.info("Waktu bursa diterima, tetapi data seluruh market belum berhasil dimuat.")
        return
    captured = report.get("display_monotonic")
    age = report.get("display_server_age", 0) + time.monotonic() - captured if captured is not None else (time.time() * 1000 - report.get("now_ms", 0)) / 1000
    st.caption(f"Waktu data pasar: {stamp(report['now_ms'])}")
    if age > 30:
        st.info("Snapshot hasil scan sudah lebih dari 30 detik. Tunggu scan baru; tabel ini merupakan catatan analisis.")


def paper_table(engine, now_ms):
    active_kind = getattr(engine.market, "instrument_kind", "USD_M_PERPETUAL")
    ledger = engine.ledger_for(active_kind)
    val_meta = spot_reference_validation_metadata(active_kind)
    s = ledger.summary(now_ms)
    cols = st.columns(4)
    for col, label, value in zip(cols, ["Ekuitas virtual (SIMULASI)", "PnL terealisasi (SIMULASI)", "PnL mengambang (SIMULASI)", "Posisi + pending"],
                                [f"{s['equity']:,.2f} USDT", f"{s['realized']:,.2f} USDT", f"{s['unrealized']:,.2f} USDT", f"{s['active']}/2"]):
        col.metric(label, value)
    st.caption(
        f"Jurnal Aktif: {val_meta['market_category_label']} · DB: {ledger.db.name}. "
        "Jurnal Spot Reference, Bybit Perpetual, dan Binance USD-M Futures dipisahkan pada file database berbeda dengan saldo virtual independen."
    )
    if val_meta["is_spot_reference"]:
        st.warning(
            "PERINGATAN SPOT REFERENCE (BUKAN FUTURES PERPETUAL): "
            f"{val_meta['mark_price_claim']} {val_meta['funding_claim']} {val_meta['leverage_claim']} {val_meta['pnl_claim']}"
        )
    trades = ledger.trades()
    rows = []
    for t in trades:
        p = t["plan"]
        kind = p.get("instrument_kind", "USD_M_PERPETUAL")
        rows.append({
            "Kategori": "SPOT SIMULASI (Non-Perpetual)" if kind == "SPOT_REFERENCE" else f"PERPETUAL SIMULASI ({kind})",
            "Pair": p["symbol"],
            "Jenis": LABELS[p["mode"]],
            "Arah": "BUY/LONG" if p["direction"] == "LONG" else "SELL/SHORT",
            "Confidence": p.get("confidence_score", 90),
            "Leverage (Simulasi)": f"{p['leverage']}x",
            "Sumber": kind,
            "Status": t["status"],
            "Entry": p["entry"],
            "SL sekarang": t["stop"],
            "TP1 (2R)": p["tp1"],
            "TP2 (3R)": p["tp2"],
            "TP3 (4R)": p.get("tp3", "-"),
            "Q rencana": p["qty"],
            "Q terbuka": t["remaining_qty"],
            "PnL bersih virtual": round(t["realized_pnl"], 6),
            "Fill virtual": stamp(p.get("fill_ms")),
            "TP1 terisi": bool(t["tp1_ms"]),
            "TP2 terisi": bool(t["tp2_ms"]),
            "Waktu": stamp(t["updated_ms"]),
            "Kualitas": t["quality"],
            "Catatan": t["note"],
            "Telegram": t["telegram"],
        })
    session_token = st.session_state.get("admin_session_token")
    session_info = engine.security.verify_session(session_token)
    if rows:
        frame = pd.DataFrame(rows)
        st.dataframe(frame, hide_index=True, use_container_width=True)
        if session_info["role"] == ROLE_ADMIN:
            st.download_button("Unduh jurnal CSV", frame.to_csv(index=False).encode("utf-8-sig"), f"paper_trades_{active_kind.lower()}.csv", "text/csv")
    else:
        st.info("Belum ada sinyal PAPER yang lolos pada jurnal instrumen ini. WAIT/NO TRADE tidak membuka trade.")
    if session_info["role"] == ROLE_ADMIN:
        st.download_button("Unduh cadangan jurnal lengkap", ledger.export_json(), f"paper_trading_backup_{active_kind.lower()}.json", "application/json")
    else:
        st.caption("Ekspor jurnal dibatasi untuk Administrator.")
    with st.expander("Ringkasan Lintas-Ledger (Futures USD-M vs Bybit Perpetual vs Spot Reference)"):
        summary_rows = []
        for kind_key in ("USD_M_PERPETUAL", "BYBIT_LINEAR_PERPETUAL", "SPOT_REFERENCE"):
            lg = engine.ledger_for(kind_key)
            sm = lg.summary(now_ms)
            summary_rows.append({
                "Instrumen": kind_key,
                "Database": lg.db.name,
                "Ekuitas Virtual (USDT)": round(sm["equity"], 2),
                "PnL Terealisasi (USDT)": round(sm["realized"], 4),
                "Posisi Aktif/Pending": sm["active"],
                "Total Trade": len(lg.trades()),
            })
        st.dataframe(pd.DataFrame(summary_rows), hide_index=True, use_container_width=True)
    st.caption("Tabel/CSV memuat 500 trade terbaru. Cadangan JSON memuat seluruh trade dan peristiwa jurnal.")
    st.caption("PENDING belum dianggap fill. Fill memakai quote Bid/Ask dan kedalaman order book yang diamati; tidak menyimpulkan fill dari sentuhan candle.")
    st.caption("Gap data (>3 menit): pending dibatalkan sebagai DATA_GAP; posisi terbuka ditutup pada quote baru sebagai DATA_GAP_EXIT tanpa mengarang keuntungan/kerugian fiktif.")


def dashboard(engine, config, auto):
    manual_clicked = st.button("Scan sekarang", type="primary", key="market_scan_now",
                               help="Memulai percobaan baru tanpa menunggu interval (khusus Administrator & dibatasi frekuensi).")
    schedule = st.session_state.setdefault("market_schedule", ScanSchedule())
    context = json.dumps(asdict(config), sort_keys=True)
    previous_report = st.session_state.get("market_report") or engine.last or {}
    if engine.last and engine.last.get("started_at_ms", 0) > previous_report.get("started_at_ms", 0):
        previous_report = engine.last
        st.session_state["market_report"] = previous_report

    manual = False
    if manual_clicked:
        last_failed = bool(previous_report.get("scan_status") == "FAILED")
        allowed, reason = engine.security.authorize_action(
            "MANUAL_SCAN",
            session_token=st.session_state.get("admin_session_token"),
            last_scan_failed=last_failed,
        )
        if allowed:
            manual = True
        else:
            st.warning(reason)

    paused = previous_report.get("market_error", {}).get("access_blocked", False)
    if (manual or not paused) and schedule.claim(time.monotonic(), auto, config.interval, context, manual):
        try:
            with st.spinner("Memantau paper trade, menyaring seluruh market (Multi-Stage), lalu menganalisis kandidat..."):
                previous = engine.last
                report = engine.run(config, manual=manual)
                if report is not previous and report.get("now_ms"):
                    report["display_monotonic"] = time.monotonic()
                    report["display_server_age"] = max(0, (time.time() * 1000 - report.get("now_ms", 0)) / 1000)
                st.session_state["market_report"] = report
        except (ValueError, sqlite3.Error, OSError) as exc:
            st.error("WAIT · siklus belum selesai: " + engine.pool.redact(str(exc))[:250])
        finally:
            schedule.finish(time.monotonic(), config.interval)
    else:
        recover_state_after_restart(engine)
    report = st.session_state.get("market_report") or engine.last or {}
    paused = report.get("market_error", {}).get("access_blocked", False)
    if report.get("scan_notice"):
        st.info(report["scan_notice"])
    if report.get("provider_failover"):
        st.info("Failover Sumber Data Aktif: " + str(report["provider_failover"]))
    if report.get("summary_telegram"):
        st.caption("Telegram Ringkasan: " + str(report["summary_telegram"]))
    scanner_tab, paper_tab, history_tab, rules_tab = st.tabs(["Scanner market", "Paper trading", "Riwayat analisis", "Aturan"])
    with scanner_tab:
        active_kind = report.get("instrument_kind") or getattr(engine.market, "instrument_kind", "USD_M_PERPETUAL")
        val_meta = spot_reference_validation_metadata(active_kind)
        st.write("**SCREEN ALL MARKETS → FILTER → RANK (M15 0–100) → SELECT TOP 1–2 → DEEP ANALYSIS & M5 CONFIRMATION → RISK VALIDATION → TELEGRAM ALERT**")
        st.caption(f"Kategori Sumber Aktif: **{val_meta['market_category_label']}** · Database Jurnal: `{engine.ledger_for(active_kind).db.name}`")
        if val_meta["is_spot_reference"]:
            st.warning(
                "MODE SPOT REFERENCE (SIMULASI NON-PERPETUAL): Harga Last Spot tidak identik dengan Mark Price Futures; "
                "funding dan leverage hanya asumsi simulasi; perhitungan PnL tidak disamakan dengan kontrak perpetual."
            )
        cols = st.columns(4)
        for col, label, value in zip(cols, ["Market aktif", "Lolos filter awal", "Shortlist", "Kandidat mendalam"],
                                    ([report.get("universe_count", 0), report.get("eligible_count", 0), len(report.get("shortlist", [])), len(report.get("selected", []))]
                                     if report and report.get("universe_loaded") is not False else ["N/A"] * 4)):
            col.metric(label, value)
        if report:
            freshness(report)
        interval_label = SCAN_INTERVAL_LABELS.get(config.interval, f"{config.interval} detik")
        mode_tag = "MEDIUM LIQUIDITY WATCHLIST" if config.relaxed_mode else "STRICT ENTRY GATES"
        auto_label = "DIJEDA (HTTP 451)" if auto and paused else (f"ON / {interval_label} ({mode_tag})" if auto else f"OFF ({mode_tag})")
        st.caption("Sesi aktif · auto-scan " + auto_label + ". Jadwal melambat jika API lambat; tidak ada penumpukan scan.")
        if paused:
            st.error("Akses data Binance ditolak (HTTP 451). Percobaan scan telah dijalankan, tetapi server belum memperoleh data yang diperlukan.")
            st.info("Permintaan berasal dari server tempat Streamlit berjalan. Periksa kelayakan akses wilayah/IP server "
                    "dengan penyedia data atau hosting. Setelah akses diperbaiki, klik Scan sekarang.")
        for message in report.get("errors", [])[:8]:
            st.warning(message)
        with st.expander("Diagnostik scan, resource, dan audit keamanan"):
            st.caption("Sumber aktif: " + str(getattr(engine.market, "source_label", "Binance USD-M Futures")) + " · permintaan berasal dari server aplikasi.")
            diagnostic = {
                k: report.get(k)
                for k in (
                    "scan_id", "scan_status", "data_status", "trigger", "started_at_ms",
                    "completed_at_ms", "now_ms", "duration_seconds", "universe_loaded",
                    "instrument_kind", "ledger_db", "validation", "market_error",
                )
            }
            diagnostic["sqlite_integrity"] = engine.ledger.integrity_check()
            diagnostic["resources"] = GLOBAL_MONITOR.snapshot()
            st.json(diagnostic)
            audit_rows = engine.security.audit_entries(limit=20)
            if audit_rows:
                st.dataframe(pd.DataFrame(audit_rows), hide_index=True, use_container_width=True)
            st.caption("Pada mesin yang menjalankan aplikasi, gunakan: python check_market_access.py. "
                       "Uji ini tidak memanggil AI, mengubah jurnal, atau mengirim Telegram.")
        if report.get("shortlist"):
            st.markdown("**Tahap 1 — Shortlist Seluruh Market (LONG & SHORT Watchlist)**")
            st.dataframe(pd.DataFrame(report["shortlist"]).rename(columns={"symbol": "Pair", "quote_volume": "Volume 24h (USDT)",
                "spread_bps": "Spread bps", "liquidity_score": "Skor likuiditas", "watchlist_side": "Watchlist"}), hide_index=True, use_container_width=True)
        if report.get("selected"):
            st.markdown("**Tahap 2 & 3 — Kandidat Terpilih untuk Analisis Mendalam**")
            sel_df = pd.DataFrame(report["selected"])
            cols_to_show = [c for c in ["symbol", "candidate_direction", "m15_score", "category", "screening_score", "activity", "momentum_atr_proxy"] if c in sel_df.columns]
            st.dataframe(sel_df[cols_to_show], hide_index=True, use_container_width=True)
        st.caption("Skor adalah kualitas setup teknikal berdasarkan aturan Multi-Stage Scanner (0–100), bukan jaminan probabilitas profit atau win rate.")
        if report.get("worker_analyses"):
            st.dataframe(pd.DataFrame(report["worker_analyses"]), hide_index=True, use_container_width=True)
        for i, data in enumerate(report.get("analyses", [])):
            r, snap = data["result"], data["snapshot"]
            side = {"LONG": "BUY/LONG", "SHORT": "SELL/SHORT"}.get(r.direction, "WAIT")
            conf = r.metrics.get("confidence_score") or (data.get("candidate_info") or {}).get("m15_score", "-")
            stage_status = data.get("stage_status", data["status"])
            kind_badge = "SPOT REFERENCE (Non-Perpetual)" if snap.instrument_kind == "SPOT_REFERENCE" else f"PERPETUAL ({snap.instrument_kind})"
            st.markdown(f"**[SIMULASI PAPER · {kind_badge}] {snap.symbol} · {LABELS[snap.mode]} · {side} · {data['status']} ({stage_status}) · Score/Conf: {conf}/100**")
            st.write(data.get("paper", ""))
            st.caption(data.get("telegram", ""))
            ai = data.get("ai") or {}
            for reason in (r.reasons + ai.get("reasons", []))[:5]:
                st.write("• " + reason)
            with st.expander(f"Perbandingan mode dan indikator — {snap.symbol}"):
                st.dataframe(pd.DataFrame(r.meta.get("mode_comparison", [])), hide_index=True, use_container_width=True)
                st.dataframe(pd.DataFrame(r.checks), hide_index=True, use_container_width=True)
                if r.plan:
                    p = r.plan
                    st.dataframe(pd.DataFrame([{
                        "KATEGORI": p.get("market_category_label", snap.instrument_kind),
                        "SYMBOL": p["symbol"],
                        "DIRECTION": p["direction"],
                        "CONFIDENCE": f"{p.get('confidence_score', 90)}/100 ({p.get('confidence_calibration_status', 'UNCALIBRATED')})",
                        "MAX LEVERAGE": f"{p.get('max_allowed_leverage', p['leverage'])}x",
                        "SELECTED LEVERAGE (SIMULASI)": f"{p['leverage']}x",
                        "Trigger": p["entry_trigger"],
                        "ENTRY PRICE": p["entry"],
                        "TECHNICAL SL": p["sl"],
                        "SL DISTANCE": f"{p['distance']:.6g} ({p.get('sl_distance_pct', 0):.2f}%)",
                        "EST. MARGIN LOSS": f"{p.get('estimated_margin_loss_pct', 0):.2f}%",
                        "ACCOUNT RISK": f"{p['risk']:.2f} USDT",
                        "INITIAL MARGIN": f"{p['margin']:.2f} USDT",
                        "NOTIONAL": f"{p['notional']:.2f} USDT",
                        "TP1 (2R)": p["tp1"],
                        "TP2 (3R)": p["tp2"],
                        "TP3 (4R)": p.get("tp3", "-"),
                        "EST. FEES": f"{p.get('estimated_fees_usdt', 0):.4f} USDT",
                        "NET RR TP1": f"{p['net_rr_tp1']:.2f}",
                        "Berlaku sampai": stamp(p["expires_ms"]),
                    }]), hide_index=True, use_container_width=True)
                if st.checkbox("Tampilkan grafik kandidat", key=f"chart_{snap.symbol}_{snap.mode}"):
                    from manual_app import draw_chart
                    draw_chart(snap, r.plan)
            st.download_button("Unduh analisis " + snap.symbol, json.dumps({"baseline": asdict(r), "ai": ai, "status": data["status"]}, ensure_ascii=False, indent=2),
                               f"{snap.symbol}_{snap.mode}_paper_analysis.json", "application/json", key=f"report_{i}")
    with paper_tab:
        paper_table(engine, report.get("now_ms") or int(time.time() * 1000))
    with history_tab:
        active_kind = getattr(engine.market, "instrument_kind", "USD_M_PERPETUAL")
        records = history.load(engine.ledger_for(active_kind).db)
        if records:
            st.dataframe(pd.DataFrame([{"Waktu": stamp(x["baseline"]["meta"]["time_ms"]), "Pair": x["baseline"]["meta"]["symbol"],
                "Instrumen": x["baseline"]["meta"].get("instrument_kind", "USD_M_PERPETUAL"),
                "Mode": x["baseline"]["meta"]["mode"], "Status": x["final_status"], "Scope": x["baseline"]["meta"].get("scope", "MANUAL")} for x in records]), hide_index=True)
        else:
            st.info("Belum ada analisis kandidat tersimpan.")
    with rules_tab:
        st.markdown("**PRINSIP UTAMA:** `SCREEN ALL MARKETS → FILTER → RANK → SELECT TOP 1–2 → DEEP ANALYSIS → M5 CONFIRMATION → RISK VALIDATION → TELEGRAM ALERT`")
        st.markdown("**SCALPER:** bias 15M / eksekusi 5M · **INTRADAY:** 4H / 15M · **SWING:** 1D / 4H.")
        st.write("Seluruh keputusan pengelolaan posisi, perhitungan TP (TP1=2R 50%, TP2=3R 25%, TP3=4R/Runner 25%), masa berlaku order (3 candle eksekusi), validasi harga limit/depth, dan batas data gap (3 menit) terpusat di modul `futures/` dan identik antara dashboard (`app.py`) dan `worker.py`.")
        st.write("Jurnal **Spot Reference** (`spot_paper_history.sqlite3`), **Bybit Perpetual** (`bybit_paper_history.sqlite3`), dan **Binance USD-M Futures** (`paper_history.sqlite3`) dipisahkan secara ketat. Spot Reference bukan Futures Perpetual: harga spot tidak identik dengan Mark Price Futures, funding dan leverage hanya asumsi simulasi, dan perhitungan PnL tidak disamakan dengan kontrak perpetual.")
        st.download_button("Unduh masterprompt lengkap", (ROOT / "STRATEGY_RULES.txt").read_text(), "STRATEGY_RULES.txt", "text/plain")


def main():
    st.set_page_config(page_title=f"Futures Desk · v{APP_VERSION}", page_icon="📡", layout="wide")
    st.markdown("<style>.block-container{max-width:1280px;padding-top:2rem}[data-testid=stMetric]{border:1px solid #e4e8ef;border-radius:12px;padding:12px}</style>", unsafe_allow_html=True)
    st.caption(f"FUTURES DESK / {APP_VERSION} / MULTI-LEDGER & SERVER-SIDE RBAC / PAPER SIMULATION ONLY")
    st.title("Market scanner otomatis")
    st.caption("Tanpa input pair · multi-stage screening · isolasi database per provider · otorisasi server-side · interval 1m / 5m / 15m")
    st.info("SCANNER SPOT / MULTI-PROVIDER (SIMULASI PAPER): Tidak mengirim order ke bursa nyata. Jika Binance Futures (HTTP 451) dan Bybit (HTTP 403) diblokir oleh lokasi server hosting, aplikasi otomatis beralih ke Binance Spot Reference (https://data-api.binance.vision) dengan database jurnal terpisah.")
    engine, config, auto = configuration()
    st.fragment(run_every="5s")(dashboard)(engine, config, auto)


if __name__ == "__main__":
    main()
