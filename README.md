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
  - Seluruh **300 pengujian di 24 modul test** lulus 100%.
- **v6.0.0 – v6.0.1 (14 November 2026):** Remediasi `BUG-09` s/d `BUG-14` (multi-ledger isolation, RBAC, GitHub Actions CI).
- **v5.9.9 (13 November 2026):** Remediasi `BUG-01` s/d `BUG-08` dan 12 skenario simulasi wajib Bagian C.
- **v5.8.0 – v5.9.8 (Oktober–November 2026):** Rilis awal multi-provider (`Binance Futures`, `Bybit Perpetual`, `Binance Spot Reference`).


### Remediasi Audit Regresi v6.0.2 (BUG-22 s/d BUG-26)
- **BUG-22 (PostgreSQL False Positive & Konfigurasi Terpusat)**: `resolve_database_config` meneruskan `DATABASE_URL` ke seluruh instance `PaperLedger`, `SecurityManager`, `TelegramAlerts`, dan `worker.py`; `requirements.txt` menyertakan `psycopg[binary]>=3.1.18,<4.0.0`; `connect_database` menolak fallback diam-diam ke SQLite (`DatabaseConfigurationError`); dan `persistence_status` hanya melaporkan `POSTGRESQL` (`persistent=True`) bila koneksi dan probe baca/tulis berhasil.
- **BUG-23 (Sinkronisasi Aktif Supabase REST)**: `PaperLedger` otomatis memanggil `SupabaseStateSync.push_backup` setelah setiap transaksi penting dan otomatis memulihkan state saat SQLite lokal kosong pasca-restart, dengan `namespace` terpisah per instrumen, `schema_version=2`, `checksum_sha256`, penolakan backup kedaluwarsa (`stale guard`), dan retry jaringan.
- **BUG-24 (Health Check Nyata SQLite vs PostgreSQL)**: Menghapus translasi palsu `PRAGMA quick_check -> SELECT 'ok'` pada adapter PostgreSQL dan memisahkan `verify_sqlite_integrity`, `verify_postgres_health`, serta `verify_database_health`.
- **BUG-25 (Kuota AI Global & Per-Sesi Persisten Lintas Proses)**: Kuota AI disimpan secara atomik pada tabel bersama `security_quota_events` sehingga konsisten antara `worker.py`, `app.py`, dan bertahan melintasi restart proses.
- **BUG-26 (Fingerprint Cache Resource & Rotasi Token Admin)**: `compute_auth_config_fingerprint` memastikan `@st.cache_resource` diperbarui saat token dirotasi tanpa menyimpan token asli di log/memori, dan otomatis membatalkan seluruh sesi administrator sebelumnya (`TOKEN_ROTATION`).


### Remediasi Audit Regresi Terbaru v6.0.2 (BUG-27 s/d BUG-32, LOGIC-01, & OPS-01..03)
- **Total Pengujian Terverifikasi**: **327 unit & integration test lulus (0 gagal) di 26 modul pengujian** (`OPS-03`).
- **BUG-27 (Dynamic Leverage pada Plan)**: `select_dynamic_leverage()` selalu mengembalikan `max_allowed_leverage` dan `selected_leverage` pada semua jalur return (tanpa `KeyError`), menolak sinyal dengan `confidence < 85`, dan menerapkan `effective_leverage = selected_lev` pada `plan['leverage']`.
- **BUG-28 (Canonical Checksum & Pemulihan Audit Log)**: `compute_backup_checksum()` menghitung `trades`, `events`, `meta`, `telegram_alerts`, `security_audit_log`, `security_quota_events`, `instrument_kind`, dan `namespace`; mewajibkan `checksum_sha256` pada seluruh backup schema v2+; menolak backup lintas instrumen; serta memulihkan `security_audit_log` secara idempoten saat restore.
- **BUG-29 (Perpindahan Scanner Lock Atomik saat Failover)**: `MarketScanner` menyimpan `lock_owner_ledger` dan `lock_owner_path`, mengakuisisi lock database tujuan secara atomik sebelum melepas lock sumber saat failover, dan membatalkan scan jika database tujuan sedang dikunci scanner lain.
- **BUG-30 (Klasifikasi Error Eksplisit untuk Fallback Spot)**: `classify_provider_error()` memisahkan `REGIONAL_RESTRICTION`, `RATE_LIMIT`, `NETWORK_ERROR`, dan `INVALID_RESPONSE`; fallback ke `SPOT_REFERENCE` hanya terjadi pada `REGIONAL_RESTRICTION` (HTTP 403/451 regional), tidak pernah pada `http_status=None`, timeout jaringan, JSON/retCode invalid, atau rate limit.
- **BUG-31 (Otorisasi Tunggal Telegram)**: Menghapus otorisasi ganda pada `send_summary()`, `send_watchlist()`, dan `send_cancellation()` sehingga 1 pengiriman hanya menggunakan 1 kuota `TELEGRAM_ALERT` dan 1 catatan audit.
- **BUG-32 (Diagnostik Database Sesuai Backend Aktual)**: Dashboard menggunakan `ledger.integrity_check()` (`database_health`) sesuai backend aktif (`POSTGRESQL` vs `SQLITE`).
- **LOGIC-01 (Eksekusi Otomatis TP3 4R & Runner)**: `PaperLedger.advance()` mengeksekusi `TP1` (2R, 50%), `TP2` (3R, 25%), dan `TP3` (4R, 25% runner) secara otomatis pada quote order-book valid sembari mengawal runner dengan trailing stop 2 ATR setelah `TP2`.
- **OPS-01 (SQL Setup Supabase & PostgreSQL)**: Disediakan pada `supabase/migrations/001_coinagent_schema.sql` (mencakup tabel `public.coinagent_backups` dengan `UNIQUE (namespace)` dan kolom `payload JSONB` + RLS policy `service_role`). Jalankan di Supabase SQL Editor lalu verifikasi via panel diagnostik dashboard (`verified_rw: true`).
- **OPS-02 (Guard Keamanan Legacy `manual_app.py`)**: `manual_app.verify_legacy_entrypoint_security()` memblokir penggunaan tanpa autentikasi pada deployment publik (`COINAGENT_PUBLIC_DEPLOYMENT=true`); gunakan `app.py` sebagai entrypoint produksi.


### Remediasi Audit Supabase Integration & Security v6.0.2 (16 November 2026 — SUPABASE-01..03, SEC-03, BUG-35, OPS-03, QA-03, DOC-03)
- **Status Pengujian & CI Terverifikasi (`DOC-03`)**: **327 unit & integration test lulus (0 gagal) di 26 modul pengujian** pada tanggal **16 November 2026** (GitHub Actions CI: `https://github.com/hakiem888-arch/secretagent/actions/workflows/ci.yml`).
- **SUPABASE-01 (Explicit GRANT/REVOKE & Live RW Probe)**: `supabase/migrations/001_coinagent_schema.sql` memberikan `GRANT USAGE ON SCHEMA public TO service_role`, `REVOKE ALL ... FROM PUBLIC, anon, authenticated`, serta `GRANT SELECT, INSERT, UPDATE, DELETE` khusus `service_role`; `SupabaseStateSync.verify_rw_probe()` memverifikasi upsert, select, update, dan pull nyata.
- **SUPABASE-02 (Timestamp Checksum & HMAC Envelope Auth)**: `backup_at_ms`, `state_updated_ms`, dan `state_seq` tercakup di dalam `compute_backup_checksum()` dan `compute_backup_hmac()`; ketidaksesuaian timestamp antara envelope dan payload langsung ditolak.
- **SUPABASE-03 (Single-Writer REST Guard & Non-ACID Labeling)**: `SupabaseStateSync` menolak konflik multi-writer (`SUPABASE-03 MULTI-WRITER CONFLICT`) serta snapshot kedaluwarsa (`STALE SNAPSHOT REJECTED`), dan tidak pernah mengklaim REST Sync sebagai database ACID bersama (`acid_transactional: false`, `shared_multi_writer_safe: false`).
- **SEC-03 (Proteksi Tabel Internal, `token_hash`, & Audit Failure Monitoring)**: Seluruh 12 tabel internal menerapkan `ENABLE/FORCE ROW LEVEL SECURITY` dan `REVOKE ALL FROM PUBLIC, anon, authenticated`; `security_sessions` hanya menyimpan digest SHA-256 `token_hash` (bukan bearer token mentah); kegagalan penulisan `security_audit_log` dipantau aktif melalui `audit_health_status()`.
- **BUG-35 (Klasifikasi Spesifik HTTP 403 Bybit)**: Memisahkan `REGIONAL_RESTRICTION`, `RATE_LIMIT`, `AUTH_ERROR`, dan `UNKNOWN_403` berdasarkan isi respons dan `retCode`; `UNKNOWN_403` dan `AUTH_ERROR` tidak memicu fallback ke `SPOT_REFERENCE`.
- **OPS-03 & QA-03 (Monitoring Heartbeat Worker 24/7 & 10-Stage Integration Test)**: `PaperLedger.worker_health_status()` mendeteksi `WORKER_ALIVE`, `WORKER_DEAD_OR_DISCONNECTED`, dan `DB_CONNECTION_LOST`, diverifikasi melalui 10 skenario integrasi penuh pada `tests/test_audit_v602_supabase_security_qa03.py`.


### Remediasi Audit Arsitektur v6.0.2 (16 November 2026 — BUG-36 s/d BUG-44)
- **Status Pengujian Terverifikasi**: **327 unit & integration test lulus (0 gagal) di 26 modul pengujian** (`tests/test_audit_v602_architecture_bug36_44.py`).
- **BUG-36 (Scanner Independen dari Sesi Admin & Status `WORKER_OFFLINE`)**: Eksekusi scanner pada Worker (`ROLE_WORKER`) berjalan independen dari masa berlaku token `ADMIN` (3.600 detik); menampilkan `WORKER_OFFLINE` secara eksplisit saat worker tidak aktif dan mencegah dua scheduler berjalan bersamaan.
- **BUG-37 (Pemisahan Refresh UI 60s dari Interval Scanner)**: Refresh tampilan pasif diatur menjadi `60s` (bukan `15s`) tanpa memicu request API market baru, dilengkapi informasi `last_scan_at` dan `next_scan_at`.
- **BUG-38 (Tabel Global `user_preferences`, Default `OFF`, & Stabilitas Toggle)**: Konfigurasi sidebar disimpan secara transaksional di tabel global `user_preferences` (terpisah dari ledger trading `paper_meta`), default fitur otomatis berisiko ditetapkan `OFF`, indikator `SAVED` / `SAVE_FAILED` ditampilkan secara eksplisit, dan pergantian role (`ADMIN` <-> `VIEWER`) maupun failover provider tidak pernah mengubah nilai toggle.
- **BUG-39 (Diagnostik Filter 7 Kategori Penolakan)**: `shortlist()` dan dashboard menampilkan rincian `rejected_low_volume`, `rejected_wide_spread`, `rejected_low_trades`, `rejected_stale_quote`, `rejected_missing_candle`, `rejected_invalid_symbol`, dan `rejected_intraday_range` beserta metrik ringkasan.
- **BUG-40 (Sinkronisasi Interval Global Admin, Viewer, & Worker)**: Satu sumber konfigurasi interval di `user_preferences` dibaca secara identik oleh Admin, Viewer (tanpa paksaan `300s`), dan `worker.py` secara dinamis setiap siklus tanpa restart.
- **BUG-41 (Tabel Global `scanner_runs` Lintas Provider)**: Setiap hasil scan disimpan ke tabel global `scanner_runs` sehingga dashboard selalu menampilkan laporan terbaru berdasarkan timestamp lintas `Binance`, `Bybit`, dan `Spot Reference`.
- **BUG-42 (Kelanjutan Technical Scan Saat News API Gagal)**: Kegagalan News API menetapkan `news_status = 'NEWS_UNAVAILABLE'` namun tetap menjalankan screening teknikal Tahap 1, Tahap 2 (M15), dan Tahap 3.
- **BUG-43 (Klasifikasi Spesifik Error M15)**: Error kandidat M15 diklasifikasikan secara spesifik ke dalam `HTTP_TIMEOUT`, `RATE_LIMIT`, `STALE_CANDLE`, `CANDLE_NOT_CLOSED`, `EMPTY_RESPONSE`, `INVALID_SYMBOL`, `INVALID_OHLC`, dan `CONNECTION_ERROR` dengan redaksi kredensial otomatis.
- **BUG-44 (Anggaran Waktu Per Tahap & `PARTIAL_SCAN` + Antrean Kandidat)**: Memisahkan anggaran waktu per tahap dan menandai siklus yang terpotong batas waktu sebagai `PARTIAL_SCAN` (bukan `COMPLETED`) beserta antrean `pending_candidate_queue` untuk siklus berikutnya.


### Remediasi Market Scanner & Aturan Buy/Sell v6.0.2 (17 November 2026 — BUG-45 s/d BUG-53 + Panel Diagnostik 19 Metrik)
- **Status Pengujian Terverifikasi**: **327 unit & integration test lulus (0 gagal) di 26 modul pengujian** (`tests/test_audit_v602_scanner_rules_bug45_53.py`).
- **BUG-45 (Shortlist 30, Balanced Long/Short, Rotating Queue 5 Kandidat/Siklus)**: Shortlist dinaikkan menjadi 30 kandidat dengan antrean rotasi 5 kandidat per siklus, keseimbangan LONG/SHORT, filter skor minimum M15 (`min_m15_deep_score=55`), serta pengiriman alert watchlist bagi kandidat memenuhi syarat di luar top 5.
- **BUG-46 (Pemisahan Tahap Alert `EARLY WATCH`, `SETUP FORMING`, `PAPER ENTRY`)**: Alert dibagi per tahap (`EARLY WATCH` ≥55, `SETUP FORMING` ≥65 + ≥3/5 konfirmasi M5, `PAPER ENTRY`) dengan deduplikasi `symbol + timeframe + candle_close + direction + stage` yang mengizinkan promosi tahap tanpa spam duplikat.
- **BUG-47 & BUG-48 (Hard Risk Gates vs Konfluensi Berbobot 3/5 & Profil Strategi)**: Seluruh Hard Risk Gates dipertahankan ketat sementara indikator lunak memakai sistem konfluensi berbobot (minimal 3 dari 5 konfirmasi inti M5) pada profil `CONSERVATIVE` (85, risiko ≤0.50%), `BALANCED` (78, risiko ≤0.35%), dan `ACTIVE` (72, risiko ≤0.25%) yang dilengkapi validasi *walk-forward*.
- **BUG-49 (Audit Komponen Confidence Score & Kalibrasi PAPER)**: Seluruh 10 komponen `compute_confidence` memberikan tepat 0 poin bila kondisi tidak terjadi, tidak pernah menyebut skor sebagai *win probability*, dan dikalibrasi terhadap metrik historis PAPER (`win_rate_pct`, `expectancy_r`, `profit_factor`, `max_drawdown_pct`).
- **BUG-50 (Pemisahan `RULE_BASED_VALID`, `AI_CONFIRMED`, & `AI_UNAVAILABLE`)**: AI berperan sebagai *contextual reviewer* tanpa dapat menciptakan sinyal ketika rule engine gagal, dan memisahkan status `AI_UNAVAILABLE` secara eksplisit saat Groq mengalami gangguan/cooldown.
- **BUG-51 (Kebutuhan Candle Berdasarkan Indikator + `HISTORY_INSUFFICIENT`)**: Kebutuhan histori disesuaikan dengan indikator aktual (`200` candle bias EMA200, `120` candle eksekusi M5) dengan tetap menjaga pemeriksaan kontinuitas candle dan status `HISTORY_INSUFFICIENT`.
- **BUG-52 (`ENTRY_BLOCKED_POSITION_LIMIT` Tanpa Mematikan Discovery)**: Saat 2 posisi/pending aktif, scanner tetap menyaring market, memperbarui watchlist, dan menampilkan kandidat terbaik dengan status `ENTRY_BLOCKED_POSITION_LIMIT`.
- **BUG-53 & Panel Diagnostik Wajib (19 Metrik Funnel)**: Evaluasi M15, M5, dan AI di-cache berdasarkan `symbol + timeframe + candle_close_ms`, serta setiap siklus scan menghasilkan 19 metrik funnel lengkap pada Panel Diagnostik Wajib.
