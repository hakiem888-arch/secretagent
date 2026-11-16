# Laporan Validasi & Regresi Teknis — CoinAgent X / Futures Desk v6.0.2

Tanggal laporan: 14 November 2026 WIB.
Status: **LULUS REMEDIASI AUDIT TAHAP 1 (`BUG-01`..`BUG-08`), TAHAP 2 (`BUG-09`..`BUG-14`), & TAHAP 3 (`BUG-15`..`BUG-21`)**.

---

## 1. Hasil Eksekusi Kompilasi & Pengujian Lengkap (v6.0.2)

- **Kompilasi Statis (`compileall`):** Seluruh **51 file Python** (`app.py`, `manual_app.py`, `worker.py`, `check_market_access.py`, paket `futures/`, dan `tests/`) berhasil dikompilasi tanpa error.
- **Total Pengujian Lulus:** **267 passed, 0 failed** di seluruh **25 modul pengujian** (termasuk `test_audit_v602_deployment.py` [BUG-15..BUG-21], `test_audit_v6_security_failover.py` [BUG-09..BUG-14], `test_audit_remediation.py` [BUG-01..BUG-08 & C01–C12], `test_dashboard.py`, dan `test_ui.py`).

---

## 2. Matriks Remediasi Audit Tahap Ketiga (BUG-15 s/d BUG-21)

| Kode Temuan | Prioritas | Permasalahan Sebelumnya | Tindakan Perbaikan pada v6.0.2 | Pengujian Verifikasi |
| :--- | :--- | :--- | :--- | :--- |
| **BUG-15** | KRITIS | Tanpa token admin/env flag, `auth_required` bernilai `False` dan sesi anonim mendapat role `ADMIN`. | Menetapkan `auth_required = True` secara default di `SecurityManager`. Sesi anonim selalu mendapat role `VIEWER` dan ditolak saat memicu scan. Menambahkan `verify_public_deployment_startup()`. | `test_bug15_default_auth_is_strict_and_anonymous_is_always_viewer`, `test_public_viewer_defaults_off_and_cannot_trigger_server_actions` |
| **BUG-16** | KRITIS | Otorisasi `TELEGRAM_ALERT` dan `AI_REVIEW` belum dipanggil di seluruh jalur otomatis, dan toggle dashboard aktif default untuk publik. | Memasang gerbang `SecurityManager.authorize_action()` di dalam `GroqRotation.review()`, seluruh metode `TelegramAlerts`, dan `MarketScanner.run()`. Menerapkan kuota AI global per jam & per sesi. Mengatur default toggle pengunjung publik ke `False`. | `test_bug16_ai_review_telegram_and_autoscan_enforce_authorization_and_global_quota`, `test_public_viewer_defaults_off_and_cannot_trigger_server_actions` |
| **BUG-17** | KRITIS | SQLite lokal tidak menjamin persistensi saat reboot/redeploy di Streamlit Community Cloud. | Menambahkan `futures/db_backend.py` dengan dukungan PostgreSQL/Supabase (`DATABASE_URL` / `SUPABASE_URL`), transaksi database atomik (`atomic_transaction`), serta ekspor & pemulihan backup JSON bebas duplikasi (`import_backup_json`). | `test_bug17_backup_and_restore_preserves_state_without_duplicating_positions_or_alerts` |
| **BUG-18** | TINGGI | Worker 24/7 tidak boleh bergantung pada sesi browser dashboard yang dapat tidur setelah 12 jam. | Memisahkan scheduler `worker.py` (dengan token `ROLE_WORKER` dan pencatatan `worker_heartbeat`) dari dashboard *read-only*, serta menerapkan *lease lock* database `scanner_locks` + `ProcessLock`. | `test_bug18_single_scanner_db_lease_lock_and_worker_heartbeat` |
| **BUG-19** | MENENGAH | Shim `st_compat` memakai `except Exception` yang dapat menutupi kerusakan instalasi Streamlit produksi. | Membatasi `install_if_missing()` hanya pada mode pengujian (`is_test_environment()`) dan menggunakan `importlib.util.find_spec("streamlit")` secara spesifik. | `test_bug19_st_compat_never_activates_outside_test_mode` |
| **BUG-20** | MENENGAH | `_bootstrap_project_files()` hanya memeriksa keberadaan file/marker tanpa memverifikasi integritas konten. | Menambahkan manifest hash `_EMBEDDED_MANIFEST_SHA256` dan `_EMBEDDED_BUNDLE_SHA256`, membandingkan hash SHA-256 setiap file aktual, dan memulihkan file rusak secara atomik melalui direktori *staging* + `os.replace()`. | `test_bug20_self_bootstrapping_verifies_sha256_manifest_and_repairs_tampered_files` |
| **BUG-21** | RENDAH | Dokumentasi dan beberapa pesan notifikasi masih mencantumkan versi `6.0.0` / `5.9.9`. | Menyatukan konstanta `APP_VERSION = "6.0.2"` di `futures/version.py` dan menggunakannya di seluruh kode, notifikasi Telegram, `README.md`, dan `VALIDATION.md`. | `test_bug21_unified_app_version_6_0_2` |

---

## 3. Ringkasan Kepatuhan Kriteria Release v6.0.2

1. **Sesi anonim tidak bisa memperoleh ADMIN pada deployment publik:** Terpenuhi (`BUG-15`).
2. **AI dan Telegram tidak dapat dipanggil tanpa otorisasi yang benar:** Terpenuhi (`BUG-16`).
3. **Data jurnal tetap tersedia setelah reboot dan redeploy (PostgreSQL/Supabase & Backup/Restore Idempotent):** Terpenuhi (`BUG-17`).
4. **Worker dapat berjalan independen dari sesi browser dengan satu scanner per database:** Terpenuhi (`BUG-18`).
5. **Shim pengujian tidak menutupi Streamlit produksi & seluruh CI otomatis terkonfigurasi:** Terpenuhi (`BUG-11`, `BUG-19`).
6. **Failover tidak mencampur posisi antar-instrumen:** Terpenuhi (`BUG-09`, `BUG-12`).
7. **Self-Bootstrapping memverifikasi integritas SHA-256 seluruh file secara atomik:** Terpenuhi (`BUG-20`).
8. **Hasil PAPER tetap realistis dan tidak mengklaim profitabilitas tanpa bukti backtest:** Terpenuhi (`BUG-01`..`BUG-08`, `BUG-14`).


## 4. Riwayat Audit Sebelumnya
- **v5.9.9 (13 November 2026):** Remediasi penuh `BUG-01` s/d `BUG-08` dan 12 skenario simulasi wajib Bagian C.


### Remediasi Audit Regresi v6.0.2 (BUG-22 s/d BUG-26)
- **BUG-22 (PostgreSQL False Positive & Konfigurasi Terpusat)**: `resolve_database_config` meneruskan `DATABASE_URL` ke seluruh instance `PaperLedger`, `SecurityManager`, `TelegramAlerts`, dan `worker.py`; `requirements.txt` menyertakan `psycopg[binary]>=3.1.18,<4.0.0`; `connect_database` menolak fallback diam-diam ke SQLite (`DatabaseConfigurationError`); dan `persistence_status` hanya melaporkan `POSTGRESQL` (`persistent=True`) bila koneksi dan probe baca/tulis berhasil.
- **BUG-23 (Sinkronisasi Aktif Supabase REST)**: `PaperLedger` otomatis memanggil `SupabaseStateSync.push_backup` setelah setiap transaksi penting dan otomatis memulihkan state saat SQLite lokal kosong pasca-restart, dengan `namespace` terpisah per instrumen, `schema_version=2`, `checksum_sha256`, penolakan backup kedaluwarsa (`stale guard`), dan retry jaringan.
- **BUG-24 (Health Check Nyata SQLite vs PostgreSQL)**: Menghapus translasi palsu `PRAGMA quick_check -> SELECT 'ok'` pada adapter PostgreSQL dan memisahkan `verify_sqlite_integrity`, `verify_postgres_health`, serta `verify_database_health`.
- **BUG-25 (Kuota AI Global & Per-Sesi Persisten Lintas Proses)**: Kuota AI disimpan secara atomik pada tabel bersama `security_quota_events` sehingga konsisten antara `worker.py`, `app.py`, dan bertahan melintasi restart proses.
- **BUG-26 (Fingerprint Cache Resource & Rotasi Token Admin)**: `compute_auth_config_fingerprint` memastikan `@st.cache_resource` diperbarui saat token dirotasi tanpa menyimpan token asli di log/memori, dan otomatis membatalkan seluruh sesi administrator sebelumnya (`TOKEN_ROTATION`).


### Remediasi Audit Regresi Terbaru v6.0.2 (BUG-27 s/d BUG-32, LOGIC-01, & OPS-01..03)
- **Total Pengujian Terverifikasi**: **317 unit & integration test lulus (0 gagal) di 25 modul pengujian** (`OPS-03`).
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
- **Status Pengujian & CI Terverifikasi (`DOC-03`)**: **317 unit & integration test lulus (0 gagal) di 25 modul pengujian** pada tanggal **16 November 2026** (GitHub Actions CI: `https://github.com/hakiem888-arch/secretagent/actions/workflows/ci.yml`).
- **SUPABASE-01 (Explicit GRANT/REVOKE & Live RW Probe)**: `supabase/migrations/001_coinagent_schema.sql` memberikan `GRANT USAGE ON SCHEMA public TO service_role`, `REVOKE ALL ... FROM PUBLIC, anon, authenticated`, serta `GRANT SELECT, INSERT, UPDATE, DELETE` khusus `service_role`; `SupabaseStateSync.verify_rw_probe()` memverifikasi upsert, select, update, dan pull nyata.
- **SUPABASE-02 (Timestamp Checksum & HMAC Envelope Auth)**: `backup_at_ms`, `state_updated_ms`, dan `state_seq` tercakup di dalam `compute_backup_checksum()` dan `compute_backup_hmac()`; ketidaksesuaian timestamp antara envelope dan payload langsung ditolak.
- **SUPABASE-03 (Single-Writer REST Guard & Non-ACID Labeling)**: `SupabaseStateSync` menolak konflik multi-writer (`SUPABASE-03 MULTI-WRITER CONFLICT`) serta snapshot kedaluwarsa (`STALE SNAPSHOT REJECTED`), dan tidak pernah mengklaim REST Sync sebagai database ACID bersama (`acid_transactional: false`, `shared_multi_writer_safe: false`).
- **SEC-03 (Proteksi Tabel Internal, `token_hash`, & Audit Failure Monitoring)**: Seluruh 12 tabel internal menerapkan `ENABLE/FORCE ROW LEVEL SECURITY` dan `REVOKE ALL FROM PUBLIC, anon, authenticated`; `security_sessions` hanya menyimpan digest SHA-256 `token_hash` (bukan bearer token mentah); kegagalan penulisan `security_audit_log` dipantau aktif melalui `audit_health_status()`.
- **BUG-35 (Klasifikasi Spesifik HTTP 403 Bybit)**: Memisahkan `REGIONAL_RESTRICTION`, `RATE_LIMIT`, `AUTH_ERROR`, dan `UNKNOWN_403` berdasarkan isi respons dan `retCode`; `UNKNOWN_403` dan `AUTH_ERROR` tidak memicu fallback ke `SPOT_REFERENCE`.
- **OPS-03 & QA-03 (Monitoring Heartbeat Worker 24/7 & 10-Stage Integration Test)**: `PaperLedger.worker_health_status()` mendeteksi `WORKER_ALIVE`, `WORKER_DEAD_OR_DISCONNECTED`, dan `DB_CONNECTION_LOST`, diverifikasi melalui 10 skenario integrasi penuh pada `tests/test_audit_v602_supabase_security_qa03.py`.


### Remediasi Audit Arsitektur v6.0.2 (16 November 2026 — BUG-36 s/d BUG-44)
- **Status Pengujian Terverifikasi**: **317 unit & integration test lulus (0 gagal) di 25 modul pengujian** (`tests/test_audit_v602_architecture_bug36_44.py`).
- **BUG-36 (Scanner Independen dari Sesi Admin & Status `WORKER_OFFLINE`)**: Eksekusi scanner pada Worker (`ROLE_WORKER`) berjalan independen dari masa berlaku token `ADMIN` (3.600 detik); menampilkan `WORKER_OFFLINE` secara eksplisit saat worker tidak aktif dan mencegah dua scheduler berjalan bersamaan.
- **BUG-37 (Pemisahan Refresh UI 60s dari Interval Scanner)**: Refresh tampilan pasif diatur menjadi `60s` (bukan `15s`) tanpa memicu request API market baru, dilengkapi informasi `last_scan_at` dan `next_scan_at`.
- **BUG-38 (Tabel Global `user_preferences`, Default `OFF`, & Stabilitas Toggle)**: Konfigurasi sidebar disimpan secara transaksional di tabel global `user_preferences` (terpisah dari ledger trading `paper_meta`), default fitur otomatis berisiko ditetapkan `OFF`, indikator `SAVED` / `SAVE_FAILED` ditampilkan secara eksplisit, dan pergantian role (`ADMIN` <-> `VIEWER`) maupun failover provider tidak pernah mengubah nilai toggle.
- **BUG-39 (Diagnostik Filter 7 Kategori Penolakan)**: `shortlist()` dan dashboard menampilkan rincian `rejected_low_volume`, `rejected_wide_spread`, `rejected_low_trades`, `rejected_stale_quote`, `rejected_missing_candle`, `rejected_invalid_symbol`, dan `rejected_intraday_range` beserta metrik ringkasan.
- **BUG-40 (Sinkronisasi Interval Global Admin, Viewer, & Worker)**: Satu sumber konfigurasi interval di `user_preferences` dibaca secara identik oleh Admin, Viewer (tanpa paksaan `300s`), dan `worker.py` secara dinamis setiap siklus tanpa restart.
- **BUG-41 (Tabel Global `scanner_runs` Lintas Provider)**: Setiap hasil scan disimpan ke tabel global `scanner_runs` sehingga dashboard selalu menampilkan laporan terbaru berdasarkan timestamp lintas `Binance`, `Bybit`, dan `Spot Reference`.
- **BUG-42 (Kelanjutan Technical Scan Saat News API Gagal)**: Kegagalan News API menetapkan `news_status = 'NEWS_UNAVAILABLE'` namun tetap menjalankan screening teknikal Tahap 1, Tahap 2 (M15), dan Tahap 3.
- **BUG-43 (Klasifikasi Spesifik Error M15)**: Error kandidat M15 diklasifikasikan secara spesifik ke dalam `HTTP_TIMEOUT`, `RATE_LIMIT`, `STALE_CANDLE`, `CANDLE_NOT_CLOSED`, `EMPTY_RESPONSE`, `INVALID_SYMBOL`, `INVALID_OHLC`, dan `CONNECTION_ERROR` dengan redaksi kredensial otomatis.
- **BUG-44 (Anggaran Waktu Per Tahap & `PARTIAL_SCAN` + Antrean Kandidat)**: Memisahkan anggaran waktu per tahap dan menandai siklus yang terpotong batas waktu sebagai `PARTIAL_SCAN` (bukan `COMPLETED`) beserta antrean `pending_candidate_queue` untuk siklus berikutnya.
