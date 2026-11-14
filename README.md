# CoinAgent X / Futures Desk v6.0.2 — Multi-Provider & Multi-Ledger PAPER Simulation Scanner (Audit Compliant)

Versi **v6.0.2** menyelesaikan seluruh temuan audit teknis tahap pertama (`BUG-01` s/d `BUG-08`), tahap kedua (`BUG-09` s/d `BUG-14`), dan tahap ketiga (`BUG-15` s/d `BUG-21` mencakup keamanan autentikasi default, otorisasi menyeluruh AI/Telegram/Auto-Scan, persistensi PostgreSQL/Supabase + Backup/Restore atomik, pemisahan worker 24/7 dari dashboard read-only, pembatasan test shim `st_compat`, verifikasi manifest SHA-256 pada *self-bootstrapping*, serta penyatuan `APP_VERSION = "6.0.2"`).

> **Peringatan Penting (SIMULASI PAPER):** Aplikasi ini adalah simulator riset dan pemantauan pasar (*PAPER-only*). Tidak ada pengiriman order ke bursa nyata, tidak memerlukan API key bursa, dan seluruh metrik PnL/ekuitas merupakan hasil simulasi dengan model biaya taker, slippage, dan cadangan funding. Tidak ada klaim atau jaminan *win rate* maupun profitabilitas tanpa pengujian *backtest* dan *out-of-sample* yang dapat direproduksi.

---

## Ringkasan Perbaikan Audit Tahap Ketiga di v6.0.2 (BUG-15 s/d BUG-21)

1. **BUG-15 (KRITIS — Default Authentication Tidak Memberikan Hak ADMIN):**
   - `SecurityManager` menetapkan `auth_required = True` secara default. Sesi anonim (`session_token=None`) **selalu** memperoleh role `VIEWER` (`anonymous role = VIEWER`, `anonymous manual scan = DENIED`, `anonymous auto scan = DENIED`).
   - Menambahkan pemeriksaan startup `verify_public_deployment_startup()` yang mengunci deployment publik ke mode *Read-Only* (`LOCKED_READONLY`) atau menggagalkan startup ketat (`DeploymentSecurityError`) apabila `COINAGENT_ADMIN_TOKEN` / `ADMIN_PASSWORD_HASH` belum dikonfigurasi.
2. **BUG-16 (KRITIS — Otorisasi Menyeluruh AI Review, Telegram Alert, & Auto-Scan):**
   - Seluruh pemanggilan `GroqRotation.review()` wajib melewati `SecurityManager.authorize_action("AI_REVIEW")` dengan batas kuota AI per sesi (`60/sesi`), per menit (`10/menit`), dan kuota global per jam (`240/jam`).
   - Seluruh pengiriman pesan di `TelegramAlerts` (`send`, `send_summary`, `send_watchlist`, `send_cancellation`, `send_test_message`) wajib melewati `SecurityManager.authorize_action("TELEGRAM_ALERT" / "TELEGRAM_TEST")`.
   - Pada dashboard publik (`app.py`), pengunjung (`VIEWER`) hanya dapat membaca hasil scanner dari database; seluruh toggle yang memakai kredensial server (*auto-scan*, *Telegram alert*, *watchlist alert*, *scanner report*) dinonaktifkan (`False`) secara default untuk pengunjung publik.
3. **BUG-17 (KRITIS — Persistensi Database PostgreSQL/Supabase, Transaksi ACID, & Backup/Restore):**
   - Menambahkan `futures/db_backend.py` yang mendukung koneksi langsung ke **PostgreSQL / Supabase** (`DATABASE_URL`, `SUPABASE_DB_URL`, `POSTGRES_URL`) maupun replikasi REST Supabase (`SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`) dengan transaksi eksplisit (`atomic_transaction`) agar saldo PAPER, posisi aktif/pending, riwayat trade, deduplikasi Telegram, dan log audit bertahan lintas *reboot/redeploy* Streamlit Community Cloud.
   - Menyediakan fitur ekspor dan pemulihan cadangan atomik (`PaperLedger.import_backup_json()`) yang bersifat *idempotent* sehingga pemulihan atau *restart* tidak pernah menggandakan posisi maupun notifikasi Telegram.
4. **BUG-18 (TINGGI — Pemisahan Worker 24/7 dari Dashboard Read-Only & Satu Scanner per Database):**
   - `worker.py` berjalan independen sebagai proses 24/7 dengan sesi `ROLE_WORKER` bertanda tangan HMAC, mencatat *heartbeat* ke database (`record_worker_heartbeat`), dan memegang *lease lock* database (`scanner_locks`) serta `ProcessLock` OS sehingga hanya satu scanner aktif yang dapat berjalan pada setiap database.
   - Dashboard (`app.py`) secara default membaca hasil scan dan status *worker* dari database tanpa bergantung pada sesi browser untuk menjaga pemantauan posisi.
5. **BUG-19 (MENENGAH — Pembatasan Shim `st_compat` Khusus Mode Pengujian):**
   - `futures/st_compat.py` menggunakan `importlib.util.find_spec("streamlit")` dan hanya aktif saat `is_test_environment()` bernilai `True` (`pytest` atau `COINAGENT_TEST_MODE=1`), sehingga tidak pernah menutupi kesalahan instalasi Streamlit pada lingkungan produksi.
6. **BUG-20 (MENENGAH — Verifikasi Manifest SHA-256 & Upgrade Atomik pada Self-Bootstrapping):**
   - `_bootstrap_project_files()` di `app.py` memverifikasi hash SHA-256 setiap file terhadap `_EMBEDDED_MANIFEST_SHA256` dan melakukan ekstraksi bertahap ke direktori *staging* diikuti `os.replace()` atomik untuk memulihkan file yang rusak atau berbeda tanpa meninggalkan paket setengah terpasang.
7. **BUG-21 (RENDAH — Satu Sumber `APP_VERSION = "6.0.2"`):**
   - Menyatukan konstanta `APP_VERSION = "6.0.2"` di `futures/version.py` yang digunakan secara konsisten oleh `app.py`, `worker.py`, `futures/telegram.py`, `futures/paper.py`, `README.md`, dan `VALIDATION.md`.

---

## Ringkasan Perbaikan Audit Tahap Pertama & Kedua (BUG-01 s/d BUG-14)

- **BUG-01 s/d BUG-08:** Freshness quote `0–30 detik` (`STALE_DATA`), validasi harga limit & depth sebelum fill PAPER, target TP terpusat (`2R/3R/4R`), batas *data gap* `3 menit`, penghapusan seluruh *monkey-patch*, *break-even* hanya setelah TP1 terisi, masa berlaku 3 candle eksekusi, dan dokumentasi regresi.
- **BUG-09 s/d BUG-14:** Pemisahan database SQLite/namespace per `instrument_kind` (`USD_M_PERPETUAL`, `BYBIT_LINEAR_PERPETUAL`, `SPOT_REFERENCE`), RBAC server-side, CI GitHub Actions (`.github/workflows/ci.yml`), proteksi failover saat posisi aktif, dan pemisahan label/klaim Spot Reference dari Futures Perpetual.

---

## Konfigurasi Secrets (Streamlit Cloud / Server Worker)

```toml
GROQ_API_KEY = "key_Anda"
GROQ_MODEL = "openai/gpt-oss-20b"
TELEGRAM_BOT_TOKEN = "token_bot_Anda"
TELEGRAM_CHAT_ID = "chat_id_tujuan"
PAPER_INITIAL_EQUITY = 1000.0

# Wajib pada deployment publik untuk mengaktifkan kontrol ADMIN (BUG-10 & BUG-15)
COINAGENT_ADMIN_TOKEN = "token_rahasia_admin_Anda"

# Opsional namun sangat disarankan di Streamlit Cloud agar database persisten lintas reboot (BUG-17)
DATABASE_URL = "postgresql://user:password@host:5432/dbname"
```

---

## Changelog Antar-Versi

- **v6.0.2 (14 November 2026 — Full Deployment Security, Persistent DB, & SHA-256 Integrity Release):**
  - Remediasi penuh `BUG-15` s/d `BUG-21` di atas fondasi `BUG-01` s/d `BUG-14`.
  - Penambahan `futures/version.py`, `futures/db_backend.py`, dan `tests/test_audit_v602_deployment.py`.
  - Seluruh **267 pengujian di 21 modul test** lulus 100%.
- **v6.0.0 – v6.0.1 (14 November 2026):** Remediasi `BUG-09` s/d `BUG-14` (multi-ledger isolation, RBAC, GitHub Actions CI).
- **v5.9.9 (13 November 2026):** Remediasi `BUG-01` s/d `BUG-08` dan 12 skenario simulasi wajib Bagian C.
- **v5.8.0 – v5.9.8 (Oktober–November 2026):** Rilis awal multi-provider (`Binance Futures`, `Bybit Perpetual`, `Binance Spot Reference`).
