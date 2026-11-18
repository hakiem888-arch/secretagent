# Changelog — CoinAgentX / Futures Desk

## v6.0.4 — Hybrid Adaptive v1.1 (Futures Primary / 50-Candidate Pipeline) (18 November 2026)
- **Market Source — Futures Primary (Section 2)**:
  - Prioritas provider: (1) `Binance USDⓈ-M Futures Perpetual` (`PRIMARY`), (2) `Bybit USDT Linear Perpetual` (`SECONDARY FAILOVER`), (3) `Binance Spot Reference` (`REFERENCE ONLY`).
  - Menambahkan status `FUTURES_DATA_UNAVAILABLE` (`futures_available=False`) ketika seluruh provider Futures gagal/terblokir dan menahan pembuatan `PAPER_ENTRY` Futures (`status = HOLD`).
  - Memisahkan secara mutlak `last`, `mark`, `index_price`, `funding_rate`, `open_interest`, `bid`/`ask`, `data_timestamp`, dan `received_timestamp` antara Futures dan Spot Reference.
- **Candidate Pipeline — 50 Kandidat / Target 20 Dianalisis / Maksimal 5 Groq (Section 3 & Section 7)**:
  - Meningkatkan kapasitas pipeline menjadi **50 kandidat** (`STAGE1_MAX_CANDIDATES = 50`, `STAGE2_MAX_CANDIDATES = 50`, `shortlist_limit = 50`).
  - Menganalisis minimal **20 kandidat per siklus** melalui Rule Engine (`evaluate_hybrid_adaptive`) bila kandidat valid dan anggaran waktu tersedia, serta membatasi panggilan Groq AI maksimal **5 kandidat terbaik per siklus** (`max_groq_candidates = 5`).
  - Menyimpan kandidat yang belum selesai dianalisis pada antrean rotasi persisten (`scanner_candidate_queue`) dengan pembaruan data sebelum melanjutkan analisis dan penghapusan kandidat kedaluwarsa (`expired_candidates`).
  - Rekonsiliasi matematis 10 metrik pipeline (`universe_total == stage1_passed + stage1_rejected` dan `queued_count == analyzed_count + deferred_count + expired_count`).
- **Symbol & Contract Validation (Section 4)**:
  - Memvalidasi `exchangeInfo` / `instruments-info` (`PERPETUAL`, `TRADING`, `USDT`, `PRICE_FILTER`, `LOT_SIZE`, `MIN_NOTIONAL`, `tickSize`, `stepSize`, `contractSize`) dan memisahkan `filtered_non_target_instrument` dari `rejected_contract_spec` dan `rejected_invalid_symbol`.
- **Liquidation Safety, Contract Multiplier, & PAPER Lifecycle (Section 6 & Section 8)**:
  - Menambahkan `verify_liquidation_safety()` untuk memastikan SL berada aman sebelum harga likuidasi Isolated Margin.
  - Memperhitungkan `contract_multiplier` (`contractSize`) pada position sizing, margin, biaya, dan PnL, serta memastikan proteksi SL aktif segera pada partial fill (`sl_active_for_partial_fill = True`).
- **Independent Worker 24/7 & Read-Only Dashboard (Section 9 & Section 11)**:
  - Heartbeat worker setiap **30 detik** (batas stale **> 90 detik**) dengan status `ONLINE`, `STALE`, `OFFLINE`, `NO_WORKER` dan 7 field bukti runtime (`worker_id`, `last_heartbeat_ms`, `last_scan_ms`, `next_scan_ms`, `effective_strategy_profile`, `effective_strategy_engine`, `database_name`).
  - Instrumentasi telemetri langsung pada adapter HTTP (`market_api_requests`, `groq_requests`, `telegram_requests`, `manual_scans`, `auto_scans`, `worker_scans`, `dashboard_refreshes`) dan pelaporan `NOT_INSTRUMENTED` bila instrumentasi nonaktif.
- **26 Acceptance Tests v1.1 & Full Regression Suite (Section 13 & Section 14)**:
  - Menambahkan `tests/test_hybrid_adaptive_v1_1_acceptance_v604.py` (26 pengujian penerimaan v1.1); seluruh **419 pengujian di 29 modul** lulus (`419 passed, 0 failed`), mencakup P0 Futures-only enforcement (`futures_primary_only=True`, `FuturesProviderRouter` default di `worker.py`), P0 Queue persistence safety (`QUEUE_PERSISTENCE_FAILED` & `verified_tx`), P1 Pipeline `50/20/5`, P1 Hybrid lifecycle `1R/2R/3R` (`40/35/25`) tanpa legacy `2R/3R/4R`, dan Market-Aligned Auto Scanner (`current_market_candle_boundary_ms` / `next_market_aligned_scan_ms`).


v6.0.3 (`P0 Hybrid Adaptive Production Integration & Audit Remediation`)

## [6.0.3] — 2026-11-18

### Fixed & Integrated (P0-1 through P0-8 Production Remediation)
- **P0-1 — Integrasi Penuh Hybrid Adaptive (`Strategy F`) ke Jalur Produksi (`futures/market_scanner.py`, `futures/screener.py`, `futures/history.py`, `futures/paper.py`, `app.py`)**:
  - `MarketScanner._analyse()` dan `MarketScanner.monitor()` kini memanggil `evaluate_hybrid_adaptive()` (`Strategy F`) secara langsung sebagai satu-satunya evaluator produksi tanpa fallback diam-diam ke mesin legacy `futures.strategy.evaluate()`.
  - Menggunakan `M15` untuk klasifikasi regime (`detect_m15_regime`) dan `M5` untuk routing pemicu `Strategy A`–`E` (`route_strategy_by_regime`).
  - Menyimpan `strategy_id` (`A`–`E` / `F`) dan `regime` (`m15_regime`) pada setiap hasil scan (`report["analyses"]`, `report["ranked"]`, `report["selected"]`, `worker_analyses`, `paper_trades`, dan tabel `analyses` di `futures/history.py`).
- **P0-2 — Groq Murni sebagai Contextual Advisor (`Advisor-Only`, `futures/groq_rotation.py`, `futures/telegram.py`, `futures/market_scanner.py`, `app.py`)**:
  - `evaluate_contextual_ai()` menjadikan Rule Engine (`Strategy F`) sebagai satu-satunya penentu validitas teknikal dan risiko (`rule_status == "RULE_BASED_VALID"` -> `final_status = "VALID"` dan `paper_entry_allowed = True`).
  - Groq hanya menghasilkan catatan penasihat (`AI_ADVISORY_OK`, `AI_CAUTION`, `AI_UNAVAILABLE`). Kondisi `AI_UNAVAILABLE` maupun respons `WAIT`/`NO_TRADE` dari Groq tidak pernah memveto atau menggagalkan kandidat yang telah lolos seluruh hard gates.
  - AI tidak dapat mengubah `direction`, `entry`, `sl`, `tp1/tp2/tp3`, `qty`, `leverage`, atau `setup_quality_score`.
  - Memperbarui pesan sidebar di `app.py` sehingga menjelaskan secara akurat bahwa Rule Engine (`Strategy F`) memvalidasi sinyal secara independen dan Groq berperan sebagai `Advisor-Only`.
- **P0-3 — Sinkronisasi Konfigurasi Worker & Profil `BALANCED` Eksplisit (`worker.py`, `futures/db_backend.py`, `futures/paper.py`, `futures/market_scanner.py`, `app.py`)**:
  - `ScannerConfig`, `DEFAULT_USER_PREFERENCES`, dan `create_worker_engine()` menjadikan `BALANCED` (`strategy_engine="HYBRID_ADAPTIVE_V1"`) sebagai profil eksplisit.
  - `sync_worker_config_from_preferences()` menyinkronkan `strategy_profile`, `relaxed_mode`, `shortlist_limit`, `interval`, dan seluruh parameter risiko lintas Admin, Viewer, dan Worker tanpa mereset preferensi saat restart.
  - `PaperLedger.record_worker_heartbeat()` dan `worker_health_status()` mencatat serta menampilkan `effective_strategy_profile` dan `strategy_engine` yang benar-benar dipakai oleh Worker di dashboard.
- **P0-4 — Penyelarasan Candle History Minimal 250 Candle (`config/scanner.json`, `futures/screener.py`, `futures/hybrid_adaptive.py`)**:
  - Memperbarui `config/scanner.json` dan `futures/screener.py` menjadi `"m15_candle_limit": 260`, `"m5_candle_limit": 260`, `"min_history_candles": 250`, dan `"stage2_max_candidates": 30`.
  - Menampilkan status `HISTORY_INSUFFICIENT` secara terpisah di `report["data_status"]` dan `scanner_diagnostics` tanpa melonggarkan validasi demi memaksakan sinyal.
- **P0-5 — Penyatuan Lifecycle PAPER Strategy F (`futures/lifecycle.py`, `futures/paper.py`, `futures/telegram.py`, `app.py`)**:
  - Menyatukan `after_fill()`, `manage_position()`, dan `PaperLedger.advance()` agar seluruh rencana `Strategy F` menjalankan target `TP1 = +1R (40%)`, `TP2 = +2R (35%)`, `TP3 = +3R Runner (25%)` beserta *fallback* pembagian lot otomatis (`60%/40%` dan `100%`), *fee-adjusted net break-even* pasca-TP1, *trailing stop* `+1,5R` (`1,25 * M5_ATR14` pada candle M5 tertutup), *time exit* `6` bar (`< +0,5R` + pembalikan momentum) dan `12` bar (`< +1,0R`), kedaluwarsa `2` candle M5 untuk order `PENDING`, serta aturan intrabar konservatif (`CONSERVATIVE_INTRABAR_APPROXIMATION`).
- **P0-6 — Backtest Historis OHLCV Bar-by-Bar Aktual (`backtest_hybrid_adaptive.py`, `WALK_FORWARD_REPORT.md`)**:
  - Mengganti daftar outcome manual dengan mesin simulasi OHLCV bar-by-bar yang memanggil evaluator produksi `detect_m15_regime()`, `route_strategy_by_regime()`, `evaluate_strategy_a()`..`evaluate_strategy_e()`, `compute_setup_quality_score()`, `build_hybrid_entry_order()`, `compute_hybrid_tp_targets()`, dan `evaluate_hybrid_position_lifecycle()` secara langsung di atas `15.600` candle M15 dan `16.560` candle M5 (`SHA-256` terverifikasi) dengan pembagian kronologis `60% / 20% / 20%` dan `6` fold Walk-Forward (`OOS Net Expectancy = +0,238R`, `OOS Profit Factor = 1,55`, `OOS Max DD = 0,87%`).
- **P0-7 — Diagnostik Scanner Wajib (`futures/market_scanner.py`, `app.py`)**:
  - Mencatat dan menampilkan `scanner_diagnostics` pada setiap siklus scan: `universe_total`, `lolos_stage_1`, `stage_1_rejection_reasons`, `lolos_candle_history`, `rejected_by_candle_history` (`HISTORY_INSUFFICIENT`), `m15_regime_distribution`, `strategy_candidate_counts` (`A`–`E`), `m5_trigger_count`, `setup_quality_scores`, `hard_gate_rejection_reasons`, `groq_status_counts`, `telegram_status_counts`, `worker_heartbeat` (beserta `effective_strategy_profile`), dan `persistence_status`.
- **P0-8 — Uji Penerimaan Produksi End-to-End (`tests/test_production_e2e_hybrid_v603.py`)**:
  - Menambahkan suite pengujian integrasi produksi end-to-end (`389 passed, 0 failed` di 28 modul pengujian).

---

## [6.0.2-hybrid-adaptive-v1.0] — 2026-11-17

### Added (Deliverables 1–10 & Acceptance Tests 1–57)
- Implementasi awal spesifikasi `futures/hybrid_adaptive.py` dan 57 Acceptance Tests (`tests/test_hybrid_adaptive_spec_v1_acceptance_1_57.py`).
