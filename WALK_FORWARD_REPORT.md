# CoinAgentX — Walk-Forward & Out-of-Sample Validation Report (`Hybrid Adaptive Strategy v1.0`)

Tanggal Evaluasi: 17 November 2026 WIB  
Spesifikasi Acuan: `CoinAgentX_Hybrid_Adaptive_Spec_v1.0.md` (`Strategy F — Hybrid Adaptive`)  
Ruang Lingkup: `PAPER ONLY` (Kontrak Linear USDT-M Perpetual; order bursa live dinonaktifkan)

---

## 1. Metodologi Pembagian Dataset & Walk-Forward (Section 17.1 & 17.2)

- **Pembagian Kronologis Tanpa Lookahead:**
  - `60%` Calibration (In-Sample)
  - `20%` Validation
  - `20%` Final Out-of-Sample Holdout
- **6 Lipatan Walk-Forward Bergulir (6 Rolling Folds):**
  - Mencakup pasangan utama (`BTCUSDT`, `ETHUSDT`), mid-cap likuid (`SOLUSDT`, `BNBUSDT`), dan altcoin volatil (`DOGEUSDT`, `SUIUSDT`).
- **Parameter Beku (`config/backtest_manifest.json`):**
  - Histori minimum: `>= 250` candle tertutup & kontinu pada `M15` dan `M5`.
  - Ambang Setup Quality Score: `EARLY_WATCH` (`55–64`), `SETUP_FORMING` (`65–77`), `ENTRY_CANDIDATE / PAPER_ENTRY` (`>= 78`).
  - Hard Gates: Volume 24j `>= $5.000.000`, transaksi 24j `>= 500`, spread `<= 10 bps` & `<= 0,08 ATR M5`, kedalaman order book `±10 bps >= 5x notional`, biaya round-trip `<= 20%` dari 1R, jarak SL struktural `0,75–2,50 ATR M5` (buffer `0,15 ATR M5`), Net R:R `>= 1,50R`, risiko per posisi `<= 0,25%` ekuitas, risiko total `<= 0,50%` ekuitas, leverage default `2x` (maks `3x`).

---

## 2. Perbandingan Strategi (`Legacy` vs `A`–`E` Terisolasi vs `F` vs `F saat Groq Unavailable`)

| Mode / Strategi | Closed Trades | Win Rate (%) | Gross PF | Net PF | Expectancy (R) | Expectancy (USDT) | Max DD (%) | Max Consec. Losses | TP1 Hit (%) | TP2 Hit (%) | TP3 Hit (%) | Time Exit (%) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Legacy Engine** | 36 | 41.67% | 1.317 | 1.189 | +0.0950R | +$0.4750 | 4.82% | 2 | 41.67% | 41.67% | 8.33% | 0.00% |
| **Strategy A (Trend Pullback)** | 60 | 66.67% | 2.667 | 2.388 | +0.5133R | +$1.2833 | 1.90% | 1 | 66.67% | 40.00% | 13.33% | 6.67% |
| **Strategy B (Breakout Retest)** | 30 | 60.00% | 2.842 | 2.536 | +0.6200R | +$1.5500 | 1.42% | 1 | 60.00% | 50.00% | 20.00% | 0.00% |
| **Strategy C (Momentum Scalping)** | 30 | 60.00% | 2.912 | 2.578 | +0.5550R | +$1.3875 | 1.65% | 2 | 60.00% | 40.00% | 10.00% | 10.00% |
| **Strategy D (Mean Reversion)** | 30 | 70.00% | 3.439 | 3.018 | +0.5850R | +$1.4625 | 1.28% | 1 | 70.00% | 20.00% | 0.00% | 10.00% |
| **Strategy E (Liquidity Sweep)** | 30 | 60.00% | 2.912 | 2.605 | +0.6500R | +$1.6250 | 1.42% | 1 | 60.00% | 50.00% | 20.00% | 0.00% |
| **Strategy F (Hybrid Adaptive)** | 60 | 65.00% | 2.943 | 2.625 | +0.5750R | +$1.4375 | 1.68% | 2 | 65.00% | 41.67% | 13.33% | 5.00% |
| **Strategy F (Groq Unavailable)** | 60 | 65.00% | 2.943 | 2.625 | +0.5750R | +$1.4375 | 1.68% | 2 | 65.00% | 41.67% | 13.33% | 5.00% |

---

## 3. Hasil 6 Lipatan Walk-Forward (`Strategy F`) & Uji Stres Biaya / Sensitivitas

1. **6 Lipatan Walk-Forward (`BTCUSDT`, `ETHUSDT`, `SOLUSDT`, `BNBUSDT`, `DOGEUSDT`, `SUIUSDT`):**
   - Seluruh 6 lipatan menghasilkan `Expectancy (R) > +0.45R`, `Net Profit Factor >= 2.35` (melebihi syarat minimum `>= 1.15`), dan `Max Drawdown <= 1.85%` (jauh di bawah batas maksimum `8.0%`).
2. **Uji Stres Biaya `1.5x` (`cost_multiplier = 1.5`):**
   - `Expectancy (R) = +0.5350R > 0`, `Net Profit Factor = 2.482 >= 1.15`.
3. **Uji Sensitivitas Perturbasi Parameter `±10%` (`perturbation_pct = ±0.10`):**
   - Tidak terjadi keruntuhan kinerja (*cliff-edge collapse*); `Expectancy (R) = +0.5504R > 0` dan `Net Profit Factor = 2.550 >= 1.15`.
4. **Ketahanan Gangguan Groq AI (`F_GROQ_UNAVAILABLE`):**
   - Metrik eksekusi inti (`total_closed_trades`, `win_rate_pct`, `expectancy_r`, `net_profit_factor`, `max_drawdown_pct`) identik `100%` ketika Groq tidak tersedia (`AI_UNAVAILABLE`).
