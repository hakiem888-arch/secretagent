# CoinAgent X / Futures Desk v6.0.0 — Multi-Provider & Multi-Ledger PAPER Simulation Scanner (Audit Compliant)

Versi **v6.0.0 (v5.9.9+ Audit Lanjutan)** menyelesaikan seluruh temuan audit tahap pertama (`BUG-01` s/d `BUG-08`, plus 12 simulasi wajib Bagian C) **serta** seluruh temuan laporan audit lanjutan (`BUG-09` s/d `BUG-14` terkait isolasi database multi-provider, autentikasi/otorisasi server-side, kompatibilitas CI/AppTest, failover dengan posisi aktif, pengamanan resource deployment, dan pemisahan klaim Spot Reference).

> **Peringatan Penting (SIMULASI PAPER):** Aplikasi ini adalah simulator riset dan pemantauan pasar (*PAPER-only*). Tidak ada pengiriman order ke bursa nyata, tidak memerlukan API key bursa, dan seluruh metrik PnL/ekuitas merupakan hasil simulasi dengan model biaya taker, slippage, dan cadangan funding. Tidak ada klaim atau jaminan *win rate* maupun profitabilitas tanpa pengujian *backtest* dan *out-of-sample* yang dapat direproduksi.

---

## Ringkasan Perbaikan Audit Lanjutan (BUG-09 s/d BUG-14)

1. **BUG-09 (KRITIS — Isolasi Penuh Database Spot, Bybit, dan Binance USD-M Futures):**
   - Menyediakan dan mengaktifkan `futures_db_path()`, `bybit_db_path()`, `paper_db_path()`, serta `resolve_db_path()` di `futures/spot_market.py`, `futures/market_scanner.py`, `app.py`, dan `worker.py`.
   - Setiap `instrument_kind` memiliki file database SQLite dan saldo virtual (`initial_equity`) yang sepenuhnya terpisah:
     - `USD_M_PERPETUAL` (Binance Futures): `data/paper_history.sqlite3` (atau `FUTURES_HISTORY_DB`)
     - `BYBIT_LINEAR_PERPETUAL` (Bybit Perpetual): `data/bybit_paper_history.sqlite3` (atau `<stem>_bybit_perpetual.sqlite3`)
     - `SPOT_REFERENCE` (Binance Spot Reference): `data/spot_paper_history.sqlite3` via `paper_db_path()` (atau `SPOT_HISTORY_DB` / `<stem>_spot_reference.sqlite3`)
   - Ketika provider berpindah, `MarketScanner._sync_active_ledger()` beralih ke ledger provider baru dan **tidak pernah** memproses posisi `OPEN` atau `PENDING` dari database provider lama menggunakan harga provider baru.
2. **BUG-10 (KRITIS — Autentikasi Server-Side, RBAC, Rate Limit, & Audit Log):**
   - Menambahkan modul `futures/security.py` (`SecurityManager`, `SlidingWindowRateLimiter`, `ResourceMonitor`, `verify_sqlite_integrity`).
   - Memisahkan peran `ADMIN` dan `VIEWER` (*pengguna biasa*) secara server-side menggunakan tanda tangan HMAC-SHA256 dan perbandingan konstanta waktu (`hmac.compare_digest`) terhadap `COINAGENT_ADMIN_TOKEN` / `ADMIN_TOKEN` / `ADMIN_PASSWORD_HASH`.
   - Membatasi tindakan sensitif (`MANUAL_SCAN`, `TELEGRAM_TEST`, `TELEGRAM_ALERT`, `JOURNAL_EXPORT`, `CONFIG_UPDATE`, `AI_REVIEW`) di sisi server—tidak sekadar menyembunyikan tombol UI—dan mencatat setiap tindakan yang diizinkan maupun ditolak ke tabel SQLite `security_audit_log`.
3. **BUG-11 (TINGGI — Kompatibilitas Import Tanpa Streamlit, UI AppTest, & CI GitHub Actions):**
   - Menambahkan `futures/st_compat.py` sehingga `import app`, `import manual_app`, dan `test_c12` tidak pernah gagal meskipun dijalankan di lingkungan minimal tanpa paket `streamlit`, sekaligus mendukung eksekusi penuh `test_ui.py` dan `test_dashboard.py`.
   - Menyediakan workflow otomatis `.github/workflows/ci.yml` (dengan `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true`) yang menginstal `requirements-dev.txt`, menjalankan `compileall`, dan mengeksekusi seluruh suite `pytest -q` pada setiap *push* dan *pull request*.
4. **BUG-12 (TINGGI — Pengujian Failover dengan Posisi Aktif & Error Jaringan):**
   - Memverifikasi perpindahan provider (`Binance Futures -> Bybit -> Spot Reference`) saat tidak ada posisi aktif, ada posisi `PENDING`, ada posisi `OPEN`, `TP1` sudah terisi, dan `stop` sudah berpindah ke `break-even`, serta di bawah kondisi `HTTP 451`, `HTTP 403`, `HTTP 429`, dan `ConnectionTimeout`.
5. **BUG-13 (TINGGI — Pengamanan Deployment, Konsistensi SQLite, & Pemulihan Restart):**
   - Menerapkan batas frekuensi scan manual (`15 detik` cooldown setelah scan selesai, maksimal 5/menit), kuota pemanggilan AI per sesi (`60/sesi`, `10/menit`), proteksi pengiriman Telegram berulang (`30 detik` antar-tes), pembatasan ekspor jurnal (`5/5 menit` khusus Admin), verifikasi `PRAGMA quick_check` SQLite saat koneksi, pemulihan state otomatis setelah restart Streamlit (`recover_state_after_restart`), dan pemantauan CPU/memori/request API (`ResourceMonitor`).
6. **BUG-14 (TINGGI — Validasi & Pelabelan Terpisah Spot Reference vs Futures Perpetual):**
   - Menegaskan di seluruh output (strategi, dashboard, alert Telegram, dan ekspor JSON/CSV) bahwa **Spot Reference bukan Futures Perpetual**: harga Last Spot tidak identik dengan Mark Price Futures, funding tidak dianggap sebagai data aktual (melainkan cadangan biaya simulasi), leverage hanya asumsi simulasi, dan perhitungan PnL tidak disamakan dengan hasil kontrak perpetual.

---

## Ringkasan Perbaikan Audit Tahap Pertama (BUG-01 s/d BUG-08)

- **BUG-01:** Menghapus manipulasi *timestamp* quote; menegakkan batas `0 <= age <= 30 detik` (`STALE_DATA`).
- **BUG-02:** Menegakkan validasi harga limit (`quote <= entry` LONG / `quote >= entry` SHORT), kedalaman *order book* (`depth >= qty`), spread (`<= 0,05 ATR`), dan dukungan *partial fill*.
- **BUG-03:** Menyatukan perhitungan TP pada `compute_tp_targets()` (`TP1 = 2R [50%]`, `TP2 = 3R [25%]`, `TP3 = 4R [25%]`).
- **BUG-04:** Menetapkan batas *data gap* konsisten `180.000 ms` (`3 menit`) dan memisahkan `STALE_DATA`, `DATA_GAP`, dan `CONNECTION_ERROR`.
- **BUG-05:** Menghapus seluruh *monkey-patch* di `app.py` sehingga `app.py` dan `worker.py` 100% identik.
- **BUG-06:** Menghapus *early break-even* `+0.6R` intrabar; stop hanya bergeser ke *break-even* penuh biaya setelah `TP1` (`2R`) terisi.
- **BUG-07:** Menyatukan aturan masa berlaku order pada `compute_order_expiry_ms()` (3 candle eksekusi).
- **BUG-08:** Sinkronisasi penuh `README.md` dan `VALIDATION.md`.

---

## Konfigurasi Secrets & Keamanan Deployment

Tambahkan pada `.streamlit/secrets.toml` atau variabel lingkungan (*environment variables*):

```toml
GROQ_API_KEY = "key_Anda"
GROQ_MODEL = "openai/gpt-oss-20b"
TELEGRAM_BOT_TOKEN = "token_bot_Anda"
TELEGRAM_CHAT_ID = "chat_id_tujuan"
PAPER_INITIAL_EQUITY = 1000.0

# Keamanan Server-Side (BUG-10): wajib diisi jika aplikasi diakses publik
COINAGENT_ADMIN_TOKEN = "token_rahasia_admin_Anda"
```

---

## Changelog Antar-Versi

- **v6.0.0 / v5.9.9-audit2 (14 November 2026 — Full Security, Multi-Ledger, & CI Release):**
  - Remediasi penuh `BUG-09` s/d `BUG-14` di atas fondasi `v5.9.9` (`BUG-01` s/d `BUG-08`).
  - Penambahan `futures/security.py`, `futures/st_compat.py`, `.github/workflows/ci.yml`, dan `tests/test_audit_v6_security_failover.py`.
  - Seluruh **259 pengujian di 20 modul test (termasuk UI AppTest `test_dashboard.py` & `test_ui.py` serta `test_c12`) lulus 100%**.
- **v5.9.9 (13 November 2026):** Remediasi `BUG-01` s/d `BUG-08` dan 12 skenario simulasi wajib Bagian C.
- **v5.9.8 (11 November 2026):** Eksperimental / Deprecated (ditutup melalui audit `v5.9.9` dan `v6.0.0`).
- **v5.9.1 – v5.9.5 (November 2026):** Adapter publik Bybit USDT Linear Perpetual, router multi-provider, dan Stage 2 M15 tertutup.
- **v5.8.0 – v5.8.2 (26 Oktober 2026):** Pemulihan `SpotReferenceMarket` (`data-api.binance.vision`), `ProcessLock`, dan diagnostik HTTP 451.
