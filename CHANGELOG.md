# Changelog — CoinAgentX v6.0.2 (`Hybrid Adaptive Strategy Specification v1.0`)

## [6.0.2-hybrid-adaptive-v1.0] — 2026-11-17

### Added (Deliverables 1–10 & Acceptance Tests 1–57)
- **Deliverable 1 — M15 Regime Detector (`futures/hybrid_adaptive.py::detect_m15_regime`)**:
  - Klasifikasi deterministik 9 regime M15 dengan urutan prioritas ketat: `HIGH_VOLATILITY` -> `BREAKOUT_UP/DOWN` -> `STRONG_TREND_UP/DOWN` -> `TREND_UP/DOWN` -> `RANGE` -> `TRANSITION`.
  - Penambahan indikator `EMA9`, `EMA21`, `+DI`, `-DI`, `MACD (12,26,9)`, `RVOL20`, `Donchian20`, `bb_width`, `ema50_slope_norm`, `stoch_rsi_k/d` tanpa nilai default netral palsu, serta `confirmed_swing_pivots(left=2, right=2)` di `futures/indicators.py`.
- **Deliverable 2 — Deterministic Strategy Router & Persistent Arming Store (`route_strategy_by_regime`, `ArmingStateStore`)**:
  - Routing regime M15 ke Strategi `A`–`E` dengan penyimpanan status *armed* untuk M15 breakout (`6` candle M5 = 30 menit) dan liquidity sweep (`5` candle M5), serta perlindungan `CONFLICT_WAIT`.
- **Deliverable 3 — Modul Strategi Terisolasi `A`–`E`**:
  - `Strategy A` (*Trend Pullback*), `Strategy B` (*Breakout Retest*), `Strategy C` (*Momentum Scalping*), `Strategy D` (*Mean Reversion* — khusus `RANGE`), dan `Strategy E` (*Liquidity Sweep Reversal* — prioritas di atas `D` pada `RANGE` dan satu-satunya strategi aktif pada `TRANSITION`).
- **Deliverable 4 — Setup Quality Score 100 Poin (`compute_setup_quality_score`, `classify_setup_stage`)**:
  - 8 komponen terukur (`Regime fit` [15], `M15 context` [15], `Strategy pattern` [25], `M5 trigger` [20], `Momentum` [10], `Relative volume` [5], `VWAP / structural location` [5], `Execution quality` [5]) dan pemisahan tahap `EARLY_WATCH` (`55–64`), `SETUP_FORMING` (`65–77`), serta `ENTRY_CANDIDATE / PAPER_ENTRY` (`>= 78`).
- **Deliverable 5 — Hard Gates, Sizing, Entry STOP-LIMIT & Exit Lifecycle (`evaluate_hybrid_adaptive`, `build_hybrid_entry_order`, `compute_hybrid_tp_targets`, `evaluate_hybrid_position_lifecycle`)**:
  - Pemblokiran mutlak `SPOT_REFERENCE` dari pembuatan order `PAPER` Futures Perpetual.
  - Validasi histori `>= 250` candle tertutup & kontinu pada M15 dan M5.
  - Gate likuiditas (`volume 24j >= $5M`, `trades >= 500`, `spread <= 10 bps & <= 0,08 ATR M5`, `depth ±10 bps >= 5x notional`), biaya round-trip `<= 20%` dari 1R, SL struktural + `0,15 ATR` dalam rentang `0,75–2,50 ATR M5`, dan Net R:R `>= 1,50R`.
  - Risiko per posisi `<= 0,25%` ekuitas, total risiko terbuka + pending `<= 0,50%` ekuitas, leverage default `2x` (maks `3x`), batas rugi harian `-2,0%`, dan *cooldown* `60 menit` setelah `3` kerugian beruntun.
  - Order `STOP-LIMIT` (`trigger ± 1 tick`, `limit ± 0,10 ATR M5`, pembatalan `MISSED_NO_CHASE`, kedaluwarsa `2` candle M5).
  - Exit parsial `TP1 = +1R (40%)`, `TP2 = +2R (35%)`, `Runner = 25% (+3R)` dengan *fallback* otomatis lot minimum, geser SL ke *fee-adjusted net break-even* pasca-TP1, *trailing stop* `1,25 ATR M5` aktif setelah `+1,5R` pada candle M5 tertutup, *time stop* `6` bar (`< +0,5R` + pembalikan momentum) dan `12` bar (`< +1,0R`), serta `CONSERVATIVE_INTRABAR_APPROXIMATION` (SL didahulukan bila SL & TP tersentuh pada candle yang sama).
- **Deliverable 6 — Groq Advisor-Only, Telegram Dedup, & Scanner Funnel 50/30/10/5**:
  - `evaluate_hybrid_ai_advisor` (`AI_ADVISORY_OK`, `AI_CAUTION`, `AI_UNAVAILABLE` tanpa memblokir `PAPER_ENTRY` valid).
  - Template & deduplikasi alert `symbol + timeframe + candle_close_ms + strategy + direction + stage`.
  - `HybridRotatingQueue` (50/30/10/5) dan 17 metrik funnel `empty_hybrid_funnel()`.
- **Deliverables 7–10 — Backtest Runner, Walk-Forward Report, & 57 Acceptance Tests**:
  - `backtest_hybrid_adaptive.py`, `config/backtest_manifest.json`, `WALK_FORWARD_REPORT.md`, dan `tests/test_hybrid_adaptive_spec_v1_acceptance_1_57.py` (57/57 pengujian lulus).
