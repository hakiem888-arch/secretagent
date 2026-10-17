import copy
import os
import re
import sys
import json
import math
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone

import requests
import pandas as pd

try:
    import streamlit as st
except ImportError:  # Offline test fallback when streamlit is not installed
    class _DummySessionState(dict):
        def __getattr__(self, name):
            try:
                return self[name]
            except KeyError as e:
                raise AttributeError(name) from e

        def __setattr__(self, name, value):
            self[name] = value

    class _DummyStreamlit:
        def __init__(self):
            self.session_state = _DummySessionState()
            self.secrets = {}

        def set_page_config(self, **kwargs):
            pass

    st = _DummyStreamlit()

try:
    import plotly.graph_objects as go
except ImportError:  # Offline test fallback when plotly is not installed
    class _DummyFigure:
        def __init__(self, *args, **kwargs):
            self.traces = []
            self.hrects = []
            self.layout = {}

        def add_trace(self, trace):
            self.traces.append(trace)

        def add_hrect(self, **kwargs):
            self.hrects.append(kwargs)

        def update_layout(self, **kwargs):
            self.layout.update(kwargs)

    class _DummyPlotlyGO:
        Figure = _DummyFigure

        @staticmethod
        def Candlestick(**kwargs):
            return {"type": "Candlestick", **kwargs}

        @staticmethod
        def Scatter(**kwargs):
            return {"type": "Scatter", **kwargs}

    go = _DummyPlotlyGO()

try:
    from groq import Groq
except ImportError:  # Offline test fallback when groq is not installed
    Groq = None


def _is_streamlit_runtime():
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        return get_script_run_ctx() is not None
    except Exception:
        return False

# -------------------------------------------------------------
# Modular Domain & Config Integration (PH02-01 / F34, F35)
# -------------------------------------------------------------
_here = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
_src_dir = os.path.join(_here, "src")
if os.path.isdir(_src_dir) and _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)
if _here not in sys.path:
    sys.path.insert(0, _here)

try:
    import src.ai_consensus as _pkg_root
    import src.ai_consensus.config as _cfg_mod
    import src.ai_consensus.domain as _dom_mod
    import src.ai_consensus.storage as _stg_mod
    import src.ai_consensus.services as _svc_mod
    import src.ai_consensus.market as _mkt_mod
    import src.ai_consensus.features as _feat_mod
    import src.ai_consensus.smc as _smc_mod
    import src.ai_consensus.scanner as _scan_mod
    import src.ai_consensus.agents as _agt_mod
    import src.ai_consensus.providers as _prov_mod
    import src.ai_consensus.risk as _risk_mod
    import src.ai_consensus.paper as _paper_mod
    import src.ai_consensus.monitor as _mon_mod
except ImportError:
    try:
        import ai_consensus as _pkg_root
        import ai_consensus.config as _cfg_mod
        import ai_consensus.domain as _dom_mod
        import ai_consensus.storage as _stg_mod
        import ai_consensus.services as _svc_mod
        import ai_consensus.market as _mkt_mod
        import ai_consensus.features as _feat_mod
        import ai_consensus.smc as _smc_mod
        import ai_consensus.scanner as _scan_mod
        import ai_consensus.agents as _agt_mod
        import ai_consensus.providers as _prov_mod
        import ai_consensus.risk as _risk_mod
        import ai_consensus.paper as _paper_mod
        import ai_consensus.monitor as _mon_mod
    except ImportError:
        _cfg_mod = None
        _dom_mod = None
        _stg_mod = None
        _svc_mod = None
        _mkt_mod = None
        _feat_mod = None
        _smc_mod = None
        _scan_mod = None
        _agt_mod = None
        _prov_mod = None
        _risk_mod = None
        _paper_mod = None
        _mon_mod = None

import importlib.util as _ilu
for _mname, _mvar in (("paper", "_paper_mod"), ("monitor", "_mon_mod")):
    if globals().get(_mvar) is None:
        for _cand_dir in (
            os.path.join(_src_dir, "ai_consensus"),
            os.path.abspath(os.path.join(_here, "..", "..", "..", "..", "src", "ai_consensus")),
        ):
            _mpath = os.path.join(_cand_dir, f"{_mname}.py")
            if os.path.exists(_mpath):
                _mspec = _ilu.spec_from_file_location(f"src.ai_consensus.{_mname}", _mpath)
                if _mspec and _mspec.loader:
                    _mobj = _ilu.module_from_spec(_mspec)
                    sys.modules[f"src.ai_consensus.{_mname}"] = _mobj
                    sys.modules[f"ai_consensus.{_mname}"] = _mobj
                    _mspec.loader.exec_module(_mobj)
                    globals()[_mvar] = _mobj
                    break


import types
if "src" not in sys.modules:
    _src_pkg = types.ModuleType("src")
    if os.path.isdir(_src_dir):
        _src_pkg.__path__ = [_src_dir]
    sys.modules["src"] = _src_pkg
if _cfg_mod is not None:
    sys.modules["src.ai_consensus.config"] = _cfg_mod
    sys.modules["ai_consensus.config"] = _cfg_mod
if _dom_mod is not None:
    sys.modules["src.ai_consensus.domain"] = _dom_mod
    sys.modules["ai_consensus.domain"] = _dom_mod
if _stg_mod is not None:
    sys.modules["src.ai_consensus.storage"] = _stg_mod
    sys.modules["ai_consensus.storage"] = _stg_mod
if _svc_mod is not None:
    sys.modules["src.ai_consensus.services"] = _svc_mod
    sys.modules["ai_consensus.services"] = _svc_mod
if _mkt_mod is not None:
    sys.modules["src.ai_consensus.market"] = _mkt_mod
    sys.modules["ai_consensus.market"] = _mkt_mod
if _feat_mod is not None:
    sys.modules["src.ai_consensus.features"] = _feat_mod
    sys.modules["ai_consensus.features"] = _feat_mod
if _smc_mod is not None:
    sys.modules["src.ai_consensus.smc"] = _smc_mod
    sys.modules["ai_consensus.smc"] = _smc_mod
if _scan_mod is not None:
    sys.modules["src.ai_consensus.scanner"] = _scan_mod
    sys.modules["ai_consensus.scanner"] = _scan_mod
if _agt_mod is not None:
    sys.modules["src.ai_consensus.agents"] = _agt_mod
    sys.modules["ai_consensus.agents"] = _agt_mod
if _prov_mod is not None:
    sys.modules["src.ai_consensus.providers"] = _prov_mod
    sys.modules["ai_consensus.providers"] = _prov_mod
if _risk_mod is not None:
    sys.modules["src.ai_consensus.risk"] = _risk_mod
    sys.modules["ai_consensus.risk"] = _risk_mod

if _dom_mod is not None:
    DecisionAction = _dom_mod.DecisionAction
    DirectionalBias = _dom_mod.DirectionalBias
    MarketRegime = _dom_mod.MarketRegime
    TimingStatus = _dom_mod.TimingStatus
    AgentStatus = _dom_mod.AgentStatus
    DataStatus = _dom_mod.DataStatus
    TradingMode = _dom_mod.TradingMode
    TradePlan = _dom_mod.TradePlan
    GateReport = _dom_mod.GateReport
    ModeDecisionData = _dom_mod.ModeDecisionData
    CORE_MARKET_TIMEFRAMES = _dom_mod.CORE_MARKET_TIMEFRAMES
    HIGHER_MARKET_TIMEFRAMES = _dom_mod.HIGHER_MARKET_TIMEFRAMES
    ALL_MARKET_TIMEFRAMES = _dom_mod.ALL_MARKET_TIMEFRAMES
    is_timeframe_warmed_up = _dom_mod.is_timeframe_warmed_up
if _cfg_mod is not None:
    ModelTierConfig = _cfg_mod.ModelTierConfig
    RiskConfig = _cfg_mod.RiskConfig
    StorageConfig = _cfg_mod.StorageConfig
    AppConfig = _cfg_mod.AppConfig
    mask_secret = _cfg_mod.mask_secret
    redact_secrets = _cfg_mod.redact_secrets
    get_groq_keys = _cfg_mod.get_groq_keys
    get_gemini_keys = _cfg_mod.get_gemini_keys
    is_gemini_enabled = _cfg_mod.is_gemini_enabled
    DEFAULT_GEMINI_ANALYST_MODEL = _cfg_mod.DEFAULT_GEMINI_ANALYST_MODEL
    DEFAULT_GEMINI_DEBATE_MODEL = _cfg_mod.DEFAULT_GEMINI_DEBATE_MODEL
    DEFAULT_GEMINI_RESEARCH_MODEL = _cfg_mod.DEFAULT_GEMINI_RESEARCH_MODEL
    DEFAULT_GEMINI_JUDGE_MODEL = _cfg_mod.DEFAULT_GEMINI_JUDGE_MODEL
    STRATEGY_VERSION = _cfg_mod.STRATEGY_VERSION
    PROMPT_VERSION = _cfg_mod.PROMPT_VERSION
    SCHEMA_VERSION_AI = _cfg_mod.SCHEMA_VERSION_AI
    DEPLOYMENT_MODE = _cfg_mod.DEPLOYMENT_MODE
    compute_model_config_hash = _cfg_mod.compute_model_config_hash
    validate_deployment_security = _cfg_mod.validate_deployment_security
if _stg_mod is not None:
    connect_db = _stg_mod.connect_db
    run_migrations = _stg_mod.run_migrations
    backup_database = _stg_mod.backup_database
    restore_database = _stg_mod.restore_database
    verify_database_integrity = _stg_mod.verify_database_integrity
    compute_consensus_score = _stg_mod.compute_consensus_score
    get_signal_provenance = _stg_mod.get_signal_provenance
    update_pattern_event_workflow = _stg_mod.update_pattern_event_workflow
    get_pattern_event_workflow = _stg_mod.get_pattern_event_workflow
    CURRENT_SCHEMA_VERSION = _stg_mod.CURRENT_SCHEMA_VERSION
    QuotaScopeRecord = _stg_mod.QuotaScopeRecord
    BudgetReservationRecord = _stg_mod.BudgetReservationRecord
    DurableBudgetManager = _stg_mod.DurableBudgetManager
    GLOBAL_DURABLE_BUDGET_MANAGER = _stg_mod.GLOBAL_DURABLE_BUDGET_MANAGER
    DAGCheckpointRecord = _stg_mod.DAGCheckpointRecord
    save_dag_checkpoint = _stg_mod.save_dag_checkpoint
    load_dag_checkpoints = _stg_mod.load_dag_checkpoints
    clear_dag_checkpoints = _stg_mod.clear_dag_checkpoints
if _svc_mod is not None:
    sanitize_ui_error = _svc_mod.sanitize_ui_error
    ensure_session_state = _svc_mod.ensure_session_state
    store_analysis_snapshot = _svc_mod.store_analysis_snapshot
    get_rendered_analysis_snapshot = _svc_mod.get_rendered_analysis_snapshot
    run_and_store_analysis_service = _svc_mod.run_and_store_analysis_service
    analyze_candidates_batch_service = _svc_mod.analyze_candidates_batch_service
    send_telegram_notification = _svc_mod.send_telegram_notification
    save_rendered_analysis_service = _svc_mod.save_rendered_analysis_service
    update_paper_trades_service = _svc_mod.update_paper_trades_service
    compute_performance_service = _svc_mod.compute_performance_service
    reset_trading_state_service = _svc_mod.reset_trading_state_service
if _mkt_mod is not None:
    INTERVAL_SECONDS = _mkt_mod.INTERVAL_SECONDS
    DEFAULT_CACHE_TTL_SECONDS = _mkt_mod.DEFAULT_CACHE_TTL_SECONDS
    BarWatermark = _mkt_mod.BarWatermark
    LiveBarQuote = _mkt_mod.LiveBarQuote
    ValidationReport = _mkt_mod.ValidationReport
    TimeframeSnapshot = _mkt_mod.TimeframeSnapshot
    MarketSnapshotCache = _mkt_mod.MarketSnapshotCache
    GLOBAL_MARKET_CACHE = _mkt_mod.GLOBAL_MARKET_CACHE
    normalize_as_of_ms = _mkt_mod.normalize_as_of_ms
    ms_to_iso = _mkt_mod.ms_to_iso
    annotate_bar_watermarks = _mkt_mod.annotate_bar_watermarks
    extract_closed_df = _mkt_mod.extract_closed_df
    validate_ohlcv_dataframe = _mkt_mod.validate_ohlcv_dataframe
    build_timeframe_snapshot = _mkt_mod.build_timeframe_snapshot
    filter_bars_since = _mkt_mod.filter_bars_since
    get_monitor_runtime_status = _mkt_mod.get_monitor_runtime_status
if _feat_mod is not None:
    INDICATOR_VERSION = _feat_mod.INDICATOR_VERSION
    DEFAULT_KLINES_LIMIT = _feat_mod.DEFAULT_KLINES_LIMIT
    MIN_BARS_CORE_INDICATORS = _feat_mod.MIN_BARS_CORE_INDICATORS
    MIN_BARS_EMA50 = _feat_mod.MIN_BARS_EMA50
    MIN_BARS_EMA200 = _feat_mod.MIN_BARS_EMA200
    RECOMMENDED_BARS_EMA200 = _feat_mod.RECOMMENDED_BARS_EMA200
    WARMUP_REQUIREMENTS = _feat_mod.WARMUP_REQUIREMENTS
    WarmupAndQualityReport = _feat_mod.WarmupAndQualityReport
    evaluate_warmup_and_quality = _feat_mod.evaluate_warmup_and_quality
    compute_indicators_frame = _feat_mod.compute_indicators_frame
    safe_candle_info = _feat_mod.safe_candle_info
    safe_market_regime = _feat_mod.safe_market_regime
    extract_timeframe_features = _feat_mod.extract_timeframe_features
if _smc_mod is not None:
    SMC_ENGINE_VERSION = _smc_mod.SMC_ENGINE_VERSION
    compute_zone_boundary_distance_pct = _smc_mod.compute_zone_boundary_distance_pct
    compute_zone_overlap = _smc_mod.compute_zone_overlap
    find_pivots = _smc_mod.find_pivots
    detect_structure = _smc_mod.detect_structure
    detect_liquidity = _smc_mod.detect_liquidity
    detect_fvgs = _smc_mod.detect_fvgs
    detect_order_blocks = _smc_mod.detect_order_blocks
    compute_zone_quality = _smc_mod.compute_zone_quality
    build_entry_context = _smc_mod.build_entry_context
if _scan_mod is not None:
    SCANNER_VERSION = _scan_mod.SCANNER_VERSION
    BinanceRateLimitError = _scan_mod.BinanceRateLimitError
    BinanceRequestBudget = _scan_mod.BinanceRequestBudget
    GLOBAL_REQUEST_BUDGET = _scan_mod.GLOBAL_REQUEST_BUDGET
    ScanDiagnosticsReport = _scan_mod.ScanDiagnosticsReport
    scanner_score_bidirectional = _scan_mod.scanner_score_bidirectional
    filter_liquid_universe = _scan_mod.filter_liquid_universe
    scan_markets_service = _scan_mod.scan_markets_service
    extract_retry_after_seconds = _scan_mod.extract_retry_after_seconds
    is_http_429_error = _scan_mod.is_http_429_error
if _agt_mod is not None:
    AGENTS_ENGINE_VERSION = _agt_mod.AGENTS_ENGINE_VERSION
    AI_BASE_SCHEMA = _agt_mod.AI_BASE_SCHEMA
    AI_JUDGE_SCHEMA = _agt_mod.AI_JUDGE_SCHEMA
    NEUTRAL_ANALYSTS = _agt_mod.NEUTRAL_ANALYSTS
    is_judge_role = _agt_mod.is_judge_role
    get_schema_for_role = _agt_mod.get_schema_for_role
    detect_prompt_injection = _agt_mod.detect_prompt_injection
    sanitize_untrusted_text = _agt_mod.sanitize_untrusted_text
    sanitize_agent_output_for_downstream = _agt_mod.sanitize_agent_output_for_downstream
    build_bull_instructions = _agt_mod.build_bull_instructions
    build_bear_instructions = _agt_mod.build_bear_instructions
    build_agent_prompt = _agt_mod.build_agent_prompt
    format_debate_transcript = _agt_mod.format_debate_transcript
    run_debate_rounds = _agt_mod.run_debate_rounds
    compute_directional_consensus = _agt_mod.compute_directional_consensus
    classify_provider_error = _agt_mod.classify_provider_error
    WorkflowStageStatus = _agt_mod.WorkflowStageStatus
    build_workflow_stage_status = _agt_mod.build_workflow_stage_status
    DAGValidationError = _agt_mod.DAGValidationError
    DAGNodeSpec = _agt_mod.DAGNodeSpec
    validate_and_toposort_dag = _agt_mod.validate_and_toposort_dag
    ANALYST_NODE_IDS = _agt_mod.ANALYST_NODE_IDS
    RISK_NODE_IDS = _agt_mod.RISK_NODE_IDS
    build_standard_council_dag = _agt_mod.build_standard_council_dag
    AnalysisDAGOrchestrator = _agt_mod.AnalysisDAGOrchestrator
if _prov_mod is not None:
    PROVIDERS_ENGINE_VERSION = _prov_mod.PROVIDERS_ENGINE_VERSION
    ProviderError = _prov_mod.ProviderError
    ProviderAuthError = _prov_mod.ProviderAuthError
    ProviderModelUnsupportedError = _prov_mod.ProviderModelUnsupportedError
    ProviderRateLimitError = _prov_mod.ProviderRateLimitError
    ProviderDailyQuotaError = _prov_mod.ProviderDailyQuotaError
    ProviderNetworkTimeoutError = _prov_mod.ProviderNetworkTimeoutError
    ProviderMalformedOutputError = _prov_mod.ProviderMalformedOutputError
    ModelCapability = _prov_mod.ModelCapability
    DEFAULT_CAPABILITY_REGISTRY = _prov_mod.DEFAULT_CAPABILITY_REGISTRY
    register_model_capability = _prov_mod.register_model_capability
    get_model_capability = _prov_mod.get_model_capability
    parse_duration_header_seconds = _prov_mod.parse_duration_header_seconds
    parse_rate_limit_headers = _prov_mod.parse_rate_limit_headers
    coerce_to_provider_error = _prov_mod.coerce_to_provider_error
    build_provider_request_payload = _prov_mod.build_provider_request_payload
    verify_prompt_schema_alignment = _prov_mod.verify_prompt_schema_alignment
    BaseProviderAdapter = _prov_mod.BaseProviderAdapter
    GroqProviderAdapter = _prov_mod.GroqProviderAdapter
    OptionalGeminiProviderAdapter = _prov_mod.OptionalGeminiProviderAdapter
    BudgetContext = _prov_mod.BudgetContext
    create_budget_context = _prov_mod.create_budget_context
    preflight_reserve_judge_budget = _prov_mod.preflight_reserve_judge_budget
    GEMINI_DEFAULT_TIER_MODELS = _prov_mod.GEMINI_DEFAULT_TIER_MODELS
    should_fallback_to_secondary_provider = _prov_mod.should_fallback_to_secondary_provider
    build_gemini_http_payload = _prov_mod.build_gemini_http_payload
if _risk_mod is not None:
    RISK_ENGINE_VERSION = _risk_mod.RISK_ENGINE_VERSION
    AccountRiskConfig = _risk_mod.AccountRiskConfig
    InstrumentSemantics = _risk_mod.InstrumentSemantics
    PositionSizingResult = _risk_mod.PositionSizingResult
    PaperPortfolioLedger = _risk_mod.PaperPortfolioLedger
    GLOBAL_RISK_LEDGER = _risk_mod.GLOBAL_RISK_LEDGER
    resolve_instrument_semantics = _risk_mod.resolve_instrument_semantics
    compute_position_sizing = _risk_mod.compute_position_sizing
    build_trade_plan_and_sizing = _risk_mod.build_trade_plan_and_sizing
if _paper_mod is not None:
    sys.modules["src.ai_consensus.paper"] = _paper_mod
    sys.modules["ai_consensus.paper"] = _paper_mod
    PAPER_ENGINE_VERSION = _paper_mod.PAPER_ENGINE_VERSION
    REALISTIC_ACCOUNTING_MODE = _paper_mod.REALISTIC_ACCOUNTING_MODE
    REALISTIC_ACCOUNTING_QUALITY = _paper_mod.REALISTIC_ACCOUNTING_QUALITY
    PaperPositionState = _paper_mod.PaperPositionState
    IntrabarTieBreaker = _paper_mod.IntrabarTieBreaker
    RealizedTradeAccounting = _paper_mod.RealizedTradeAccounting
    validate_position_transition = _paper_mod.validate_position_transition
    compute_realized_trade_accounting = _paper_mod.compute_realized_trade_accounting
    resolve_intrabar_conflict = _paper_mod.resolve_intrabar_conflict
    evaluate_position_on_bars = _paper_mod.evaluate_position_on_bars
    compute_expectancy_and_drawdown_analytics = _paper_mod.compute_expectancy_and_drawdown_analytics
    compute_confidence_calibration_report = _paper_mod.compute_confidence_calibration_report
    run_walk_forward_replay_backtest = _paper_mod.run_walk_forward_replay_backtest
    run_walk_forward_folds = _paper_mod.run_walk_forward_folds
if _stg_mod is not None:
    reconcile_paper_positions = _stg_mod.reconcile_paper_positions
    compute_dual_performance_metrics = _stg_mod.compute_dual_performance_metrics
    record_worker_heartbeat = _stg_mod.record_worker_heartbeat
    get_worker_heartbeat_state = _stg_mod.get_worker_heartbeat_state
    clear_worker_heartbeats = _stg_mod.clear_worker_heartbeats
if _mon_mod is not None:
    sys.modules["src.ai_consensus.monitor"] = _mon_mod
    sys.modules["ai_consensus.monitor"] = _mon_mod
    MONITOR_ENGINE_VERSION = _mon_mod.MONITOR_ENGINE_VERSION
    MonitorCycleReport = _mon_mod.MonitorCycleReport
    get_durable_monitor_runtime_status = _mon_mod.get_durable_monitor_runtime_status
    run_monitor_cycle = _mon_mod.run_monitor_cycle
    BackgroundMonitorWorker = _mon_mod.BackgroundMonitorWorker
    parse_cli_args = _mon_mod.parse_cli_args


# ============================================================
# AI CONSENSUS TRADING V5.5
# TradingAgents-style multi-agent crypto research system
# ============================================================

def _configure_page():
    st.set_page_config(
        page_title="AI Consensus Trading V5.6",
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
    """Accept a single string, comma/newline separated keys, or a Streamlit list."""
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

_GEMINI_KEYS = []
_GEMINI_KEYS.extend(_parse_secret_list(secret("GEMINI_API_KEY")))
_GEMINI_KEYS.extend(_parse_secret_list(secret("GEMINI_API_KEYS")))
_GEMINI_KEYS.extend(_parse_secret_list(secret("GOOGLE_API_KEY")))
for _i in range(1, 21):
    _GEMINI_KEYS.extend(_parse_secret_list(secret(f"GEMINI_API_KEY_{_i}")))
GEMINI_KEYS = list(dict.fromkeys(k for k in _GEMINI_KEYS if k))
GEMINI_KEY = GEMINI_KEYS[0] if GEMINI_KEYS else ""
GEMINI_ENABLED = str(secret("GEMINI_ENABLED") or secret("ENABLE_GEMINI_FALLBACK") or "").strip().lower() in {"1", "true", "yes", "on"}

TG_TOKEN = secret("TELEGRAM_BOT_TOKEN")
TG_CHAT = secret("TELEGRAM_CHAT_ID")

# -------------------------
# Database
# -------------------------
def db():
    if _stg_mod is not None:
        return _stg_mod.connect_db(DB_FILE)
    con = sqlite3.connect(DB_FILE, timeout=10.0)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 5000")
    except Exception:
        pass
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

    CREATE TABLE IF NOT EXISTS pattern_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        symbol TEXT NOT NULL,
        mode TEXT NOT NULL,
        state TEXT NOT NULL,
        direction TEXT NOT NULL,
        score REAL NOT NULL,
        fingerprint TEXT NOT NULL,
        notified INTEGER DEFAULT 0
    );
    """)
    con.commit()
    existing_cols = {row[1] for row in con.execute("PRAGMA table_info(signals)").fetchall()}
    for col, ddl in [
        ("smc_score", "ALTER TABLE signals ADD COLUMN smc_score REAL"),
        ("entry_status", "ALTER TABLE signals ADD COLUMN entry_status TEXT"),
        ("entry_zone_low", "ALTER TABLE signals ADD COLUMN entry_zone_low REAL"),
        ("entry_zone_high", "ALTER TABLE signals ADD COLUMN entry_zone_high REAL"),
        ("smc_bias", "ALTER TABLE signals ADD COLUMN smc_bias TEXT"),
        ("analysis_id", "ALTER TABLE signals ADD COLUMN analysis_id TEXT"),
        ("idempotency_key", "ALTER TABLE signals ADD COLUMN idempotency_key TEXT"),
        ("raw_model_decision", "ALTER TABLE signals ADD COLUMN raw_model_decision TEXT"),
        ("veto_reasons", "ALTER TABLE signals ADD COLUMN veto_reasons TEXT"),
        ("accounting_mode", "ALTER TABLE signals ADD COLUMN accounting_mode TEXT"),
    ]:
        if col not in existing_cols:
            con.execute(ddl)
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_analysis_id "
        "ON signals(analysis_id) WHERE analysis_id IS NOT NULL"
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_idempotency_key "
        "ON signals(idempotency_key) WHERE idempotency_key IS NOT NULL"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_pattern_events_fingerprint "
        "ON pattern_events(fingerprint)"
    )
    con.commit()
    con.close()
    if _stg_mod is not None:
        _stg_mod.run_migrations(DB_FILE)

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
    if r.status_code in (429, 418):
        hdrs = getattr(r, "headers", {}) or {}
        ra_raw = hdrs.get("Retry-After") or hdrs.get("retry-after") or 30.0
        try:
            ra_sec = max(1.0, float(ra_raw))
        except (TypeError, ValueError):
            ra_sec = 30.0
        detail = (r.text or "")[:500].replace("\n", " ")
        if _scan_mod is not None:
            GLOBAL_REQUEST_BUDGET.record_rate_limit(ra_sec)
            raise BinanceRateLimitError(
                f"Binance HTTP {r.status_code} (Retry-After={ra_sec}s): {detail}",
                status_code=r.status_code,
                retry_after=ra_sec,
            )
        raise RuntimeError(f"Binance HTTP {r.status_code} (Retry-After={ra_sec}s): {detail}")
    if r.status_code != 200:
        detail = (r.text or "")[:500].replace("\n", " ")
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
    if _feat_mod is not None:
        return _feat_mod.ema(s, n)
    return s.ewm(span=n, adjust=False).mean()


def rsi(s, n=14, min_periods=None, flat_value=50.0):
    if _feat_mod is not None:
        return _feat_mod.rsi(s, n=n, min_periods=min_periods, flat_value=flat_value)
    d = s.diff()
    gain = d.where(d > 0, 0.0).fillna(0.0).ewm(alpha=1/n, adjust=False).mean()
    loss = (-d.where(d < 0, 0.0)).fillna(0.0).ewm(alpha=1/n, adjust=False).mean()
    rs = gain / loss.replace(0, pd.NA)
    return (100 - 100 / (1 + rs)).fillna(flat_value)


def atr(df, n=14):
    if _feat_mod is not None:
        return _feat_mod.atr(df, n=n)
    tr = pd.concat([
        df.High - df.Low,
        (df.High - df.Close.shift()).abs(),
        (df.Low - df.Close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def macd(s):
    if _feat_mod is not None:
        return _feat_mod.macd(s)
    line = ema(s, 12) - ema(s, 26)
    signal = ema(line, 9)
    return line, signal, line - signal


def bollinger(s, n=20):
    if _feat_mod is not None:
        return _feat_mod.bollinger(s, n=n)
    mid = s.rolling(n).mean()
    sd = s.rolling(n).std().fillna(0.0)
    return mid, mid + 2 * sd, mid - 2 * sd


def klines(symbol, interval="15m", limit=260, drop_unwarmed=True):
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
    if len(d) == 0:
        return _feat_mod.compute_indicators_frame(d, interval=interval, drop_unwarmed=drop_unwarmed) if _feat_mod is not None else d

    numeric = [
        "Open", "High", "Low", "Close", "Volume",
        "QuoteVolume", "Trades", "TakerBuyBase", "TakerBuyQuote"
    ]
    for c in numeric:
        d[c] = pd.to_numeric(d[c], errors="coerce")

    d.index = pd.to_datetime(d.time, unit="ms", utc=True)

    if _feat_mod is not None:
        return _feat_mod.compute_indicators_frame(d, interval=interval, drop_unwarmed=drop_unwarmed)

    d["EMA20"] = ema(d.Close, 20)
    d["EMA50"] = ema(d.Close, 50)
    d["EMA200"] = ema(d.Close, 200)
    d["RSI"] = rsi(d.Close)
    d["MACD"], d["MACD_SIGNAL"], d["MACD_HIST"] = macd(d.Close)
    d["ATR"] = atr(d)
    d["BB_MID"], d["BB_UP"], d["BB_LOW"] = bollinger(d.Close)
    d["VOL_RATIO"] = (d.Volume / d.Volume.rolling(20).mean().replace(0, pd.NA)).fillna(0.0)
    d["BUY_PRESSURE"] = (d.TakerBuyQuote / d.QuoteVolume.replace(0, pd.NA)).fillna(0.5)
    return d.dropna(subset=["Open", "High", "Low", "Close", "Volume", "EMA20", "RSI", "ATR", "BB_MID"])

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
    if _feat_mod is not None:
        return _feat_mod.safe_candle_info(d)
    if d is None or len(d) == 0:
        return {"name": "INSUFFICIENT_DATA", "body": 0.0, "upper_wick": 0.0, "lower_wick": 0.0}
    a = d.iloc[-1]
    p = d.iloc[-2] if len(d) >= 2 else a

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


# -------------------------
# Smart Money / Price Structure Engine V5.5
# -------------------------
def _closed_df(df, as_of=None, interval="15m"):
    """Idempotently extract only fully closed candles without double-dropping bars (PH03-01 / F08 / T06)."""
    if _mkt_mod is not None:
        return _mkt_mod.extract_closed_df(df, as_of=as_of, interval=interval)
    if df is None or len(df) == 0:
        out = pd.DataFrame() if df is None else df.copy()
        out.attrs["_is_closed_df"] = True
        return out
    if df.attrs.get("_is_closed_df", False):
        out = df.copy()
        out.attrs["_is_closed_df"] = True
        return out
    out = df.iloc[:-1].copy() if len(df) > 8 else df.copy()
    out.attrs["_is_closed_df"] = True
    return out


def kline_snapshot(symbol, interval="15m", limit=180, as_of=None, use_cache=False, cache=None, ttl_seconds=None):
    """Build or retrieve a cached immutable TimeframeSnapshot separating closed_df from live_bar (PH03-01)."""
    active_cache = cache if cache is not None else (GLOBAL_MARKET_CACHE if _mkt_mod is not None else None)
    if use_cache and active_cache is not None:
        cached = active_cache.get_kline_snapshot(symbol, interval, limit, now_ms=as_of)
        if cached is not None:
            return cached
    raw_df = klines(symbol, interval=interval, limit=limit)
    snap = build_timeframe_snapshot(
        raw_df,
        symbol=symbol,
        interval=interval,
        as_of=as_of,
        ttl_seconds=ttl_seconds,
        limit=limit,
    )
    if use_cache and active_cache is not None:
        active_cache.put_kline_snapshot(snap, limit=limit, fetched_at_ms=snap.as_of_ms, ttl_seconds=ttl_seconds)
    return snap

def _pct_distance(a, b):
    return abs(a - b) / max(abs(b), 1e-12) * 100.0

def _find_pivots(df, left=2, right=2, lookback=100, as_of=None, interval="15m"):
    if _smc_mod is not None:
        return _smc_mod.find_pivots(df, left=left, right=right, lookback=lookback, as_of=as_of, interval=interval)
    return [], []


def _detect_structure(df, as_of=None, interval="15m"):
    if _smc_mod is not None:
        return _smc_mod.detect_structure(df, as_of=as_of, interval=interval)
    return {
        "bias": "RANGE",
        "bos": "NONE",
        "choch": "NONE",
        "last_swing_high": 0.0,
        "last_swing_low": 0.0,
        "previous_swing_high": 0.0,
        "previous_swing_low": 0.0,
    }


def _detect_liquidity(df, as_of=None, interval="15m"):
    if _smc_mod is not None:
        return _smc_mod.detect_liquidity(df, as_of=as_of, interval=interval)
    return {"sweep_high": False, "sweep_low": False, "equal_high": None, "equal_low": None}


def _detect_fvgs(df, max_age=80, as_of=None, interval="15m"):
    if _smc_mod is not None:
        return _smc_mod.detect_fvgs(df, max_age=max_age, as_of=as_of, interval=interval)
    return []


def _detect_order_blocks(df, max_age=80, as_of=None, interval="15m"):
    if _smc_mod is not None:
        return _smc_mod.detect_order_blocks(df, max_age=max_age, as_of=as_of, interval=interval)
    return []


def _zone_quality(zone, fvg=None, structure=None, liquidity=None):
    if _smc_mod is not None:
        return _smc_mod.compute_zone_quality(zone, fvg=fvg, structure=structure, liquidity=liquidity)
    return 0


def _build_entry_context(df, structure, liquidity, obs, fvgs, as_of=None, interval="15m"):
    if _smc_mod is not None:
        return _smc_mod.build_entry_context(df, structure, liquidity, obs, fvgs, as_of=as_of, interval=interval)
    return {}


def smc_analysis(df, as_of=None, interval="15m"):
    if _smc_mod is not None:
        return _smc_mod.smc_analysis(df, as_of=as_of, interval=interval)
    structure = _detect_structure(df, as_of=as_of, interval=interval)
    liquidity = _detect_liquidity(df, as_of=as_of, interval=interval)
    fvgs = _detect_fvgs(df, as_of=as_of, interval=interval)
    obs = _detect_order_blocks(df, as_of=as_of, interval=interval)
    return _build_entry_context(df, structure, liquidity, obs, fvgs, as_of=as_of, interval=interval)


def smc_text(smc):
    stc = smc["structure"]
    liq = smc["liquidity"]
    b = smc["bullish_entry"]
    s = smc["bearish_entry"]
    return "\n".join([
        f"STRUCTURE bias={stc['bias']} BOS={stc['bos']} CHOCH={stc['choch']}",
        f"SWING high={stc['last_swing_high']:.8f} low={stc['last_swing_low']:.8f}",
        f"LIQUIDITY sweep_high={liq['sweep_high']} sweep_low={liq['sweep_low']} "
        f"equal_high={liq['equal_high']} equal_low={liq['equal_low']}",
        f"BULL ENTRY zone={b['zone_low']}..{b['zone_high']} quality={b['quality']} "
        f"confluence={b['confluence']} status={b['status']}",
        f"BEAR ENTRY zone={s['zone_low']}..{s['zone_high']} quality={s['quality']} "
        f"confluence={s['confluence']} status={s['status']}",
        f"TIMING={smc['timing']} SMC_SCORE={smc['smc_score']}",
        f"DISPLACEMENT body_ratio={smc['displacement']['body_ratio']:.2f} "
        f"vol_ratio={smc['displacement']['volume_ratio']:.2f}",
    ])


def market_regime(d):
    if _feat_mod is not None:
        return _feat_mod.safe_market_regime(d)
    if d is None or len(d) == 0:
        return "SIDEWAYS"
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
    if _scan_mod is not None:
        bi = _scan_mod.scanner_score_bidirectional(symbol, change, qvol, d, ob)
        return bi["score"], bi["why"]
    d_closed = _closed_df(d)
    if d_closed is None or len(d_closed) == 0:
        return 0, ["data tidak cukup"]
    return 50, []


def scan_markets(
    n=60,
    use_cache=True,
    cache=None,
    budget=None,
    as_of=None,
    return_report=False,
    min_quote_volume=2_000_000.0,
):
    progress = st.progress(0) if hasattr(st, "progress") else None
    status = st.empty() if hasattr(st, "empty") else None

    def _on_progress(curr, total, sym):
        if status is not None and hasattr(status, "write"):
            status.write(f"🔎 Memindai {sym} ({curr}/{max(total, 1)})")
        if progress is not None and hasattr(progress, "progress"):
            progress.progress(curr / max(total, 1))

    try:
        res = _scan_mod.scan_markets_service(
            n=n,
            tickers_fetcher=lambda: bget("/api/v3/ticker/24hr"),
            kline_fetcher=lambda sym, tf, lim: klines(sym, tf, lim),
            order_book_fetcher=lambda sym: order_book(sym),
            cache=cache if cache is not None else GLOBAL_MARKET_CACHE,
            budget=budget if budget is not None else GLOBAL_REQUEST_BUDGET,
            as_of=as_of,
            use_cache=use_cache,
            kline_limit=260,
            min_quote_volume=min_quote_volume,
            progress_callback=_on_progress,
        )
    finally:
        if progress is not None and hasattr(progress, "empty"):
            progress.empty()
        if status is not None and hasattr(status, "empty"):
            status.empty()

    if hasattr(st, "session_state") and isinstance(st.session_state, dict):
        st.session_state["last_scan_diagnostics"] = res["diagnostics"]
        st.session_state["scan_long_shortlist"] = res["long_shortlist"]
        st.session_state["scan_short_shortlist"] = res["short_shortlist"]

    if return_report:
        return res
    return res["candidates"]


# -------------------------
# AI engine — V5.3.1 Hierarchical Multi-Model Safe Mode
# -------------------------
ANALYST_MODEL = secret("ANALYST_MODEL") or "openai/gpt-oss-20b"
DEBATE_MODEL = secret("DEBATE_MODEL") or "qwen/qwen3.6-27b"
RESEARCH_MODEL = secret("RESEARCH_MODEL") or "openai/gpt-oss-120b"
JUDGE_MODEL = secret("JUDGE_MODEL") or "openai/gpt-oss-120b"

GEMINI_ANALYST_MODEL = secret("GEMINI_ANALYST_MODEL") or "gemini-2.5-flash"
GEMINI_DEBATE_MODEL = secret("GEMINI_DEBATE_MODEL") or "gemini-2.5-flash"
GEMINI_RESEARCH_MODEL = secret("GEMINI_RESEARCH_MODEL") or "gemini-2.5-pro"
GEMINI_JUDGE_MODEL = secret("GEMINI_JUDGE_MODEL") or "gemini-2.5-pro"

GEMINI_MODEL_TIERS = {
    "analyst": GEMINI_ANALYST_MODEL,
    "debate": GEMINI_DEBATE_MODEL,
    "research": GEMINI_RESEARCH_MODEL,
    "judge": GEMINI_JUDGE_MODEL,
}

ANALYST_MAX_TOKENS = int(secret("ANALYST_MAX_TOKENS") or 450)
DEBATE_MAX_TOKENS = int(secret("DEBATE_MAX_TOKENS") or 550)
RESEARCH_MAX_TOKENS = int(secret("RESEARCH_MAX_TOKENS") or 700)
JUDGE_MAX_TOKENS = int(secret("JUDGE_MAX_TOKENS") or 750)
TRADER_MAX_TOKENS = int(secret("TRADER_MAX_TOKENS") or 240)
RISK_MAX_TOKENS = int(secret("RISK_MAX_TOKENS") or 220)
MIN_COMPLETION_TOKENS = int(secret("MIN_COMPLETION_TOKENS") or 128)

MAX_AI_CALLS_PER_CANDIDATE = int(secret("MAX_AI_CALLS_PER_CANDIDATE") or 14)
MAX_PHYSICAL_AI_ATTEMPTS_PER_CANDIDATE = int(
    secret("MAX_PHYSICAL_AI_ATTEMPTS_PER_CANDIDATE") or MAX_AI_CALLS_PER_CANDIDATE
)
MAX_PHYSICAL_ATTEMPTS_PER_CALL = int(secret("MAX_PHYSICAL_ATTEMPTS_PER_CALL") or 2)
DEFAULT_DEBATE_ROUNDS = int(secret("DEFAULT_DEBATE_ROUNDS") or 1)
AI_HTTP_TIMEOUT_SECONDS = float(secret("AI_HTTP_TIMEOUT_SECONDS") or 20.0)
AI_CANDIDATE_DEADLINE_SECONDS = float(secret("AI_CANDIDATE_DEADLINE_SECONDS") or 90.0)
GROQ_MIN_DELAY = float(secret("GROQ_MIN_DELAY") or 1.0)
GROQ_RETRY_SECONDS = float(secret("GROQ_RETRY_SECONDS") or 30.0)
AI_PROVIDER_ORDER = ["groq"]

# PH01-03 & PH04-03 Deterministic Gate, Sizing & Mode Matrix Constants (F06, F16, F33, F38, T19)
MAX_SPREAD_PCT = float(secret("MAX_SPREAD_PCT") or 0.35)
MIN_NET_RR = float(secret("MIN_NET_RR") or 1.5)
ESTIMATED_ROUND_TRIP_COST_PCT = float(secret("ESTIMATED_ROUND_TRIP_COST_PCT") or 0.12)
PAPER_INITIAL_EQUITY = float(secret("PAPER_INITIAL_EQUITY") or 10000.0)
PAPER_RISK_FRACTION = float(secret("PAPER_RISK_FRACTION") or 0.005)
PAPER_FEE_PCT_ROUND_TRIP = float(secret("PAPER_FEE_PCT_ROUND_TRIP") or 0.08)
PAPER_SLIPPAGE_PCT_ROUND_TRIP = float(secret("PAPER_SLIPPAGE_PCT_ROUND_TRIP") or 0.04)
PAPER_MAX_POSITION_EXPOSURE_PCT = float(secret("PAPER_MAX_POSITION_EXPOSURE_PCT") or 0.25)
PAPER_MAX_TOTAL_EXPOSURE_PCT = float(secret("PAPER_MAX_TOTAL_EXPOSURE_PCT") or 0.80)
PAPER_MAX_TOTAL_RISK_FRACTION = float(secret("PAPER_MAX_TOTAL_RISK_FRACTION") or 0.02)
PAPER_MAX_OPEN_POSITIONS = int(secret("PAPER_MAX_OPEN_POSITIONS") or 4)
PAPER_INSTRUMENT_MODE = str(secret("PAPER_INSTRUMENT_MODE") or "spot_reference")
MIN_JUDGE_CONFIDENCE = float(secret("MIN_JUDGE_CONFIDENCE") or 55.0)
LEGACY_ACCOUNTING_MODE = "LEGACY_R_SNAPSHOT_ONLY"

MODE_TIMEFRAME_MATRIX = {
    "scalping": {
        "bias_tf": "1h",
        "setup_tf": "15m",
        "trigger_tf": "5m",
        "required_timeframes": ("5m", "15m", "1h"),
    },
    "intraday": {
        "bias_tf": "4h",
        "setup_tf": "1h",
        "trigger_tf": "15m",
        "required_timeframes": ("15m", "1h", "4h"),
    },
    "swing": {
        "bias_tf": "1d",
        "setup_tf": "4h",
        "trigger_tf": "1h",
        "required_timeframes": ("1h", "4h", "1d"),
    },
}

MODEL_TIERS = {
    "analyst": {
        "model": ANALYST_MODEL,
        "max_tokens": ANALYST_MAX_TOKENS,
        "effective_max_tokens": max(MIN_COMPLETION_TOKENS, ANALYST_MAX_TOKENS),
        "min_completion_tokens": MIN_COMPLETION_TOKENS,
        "reasoning_effort": "low",
    },
    "debate": {
        "model": DEBATE_MODEL,
        "max_tokens": DEBATE_MAX_TOKENS,
        "effective_max_tokens": max(MIN_COMPLETION_TOKENS, DEBATE_MAX_TOKENS),
        "min_completion_tokens": MIN_COMPLETION_TOKENS,
        "reasoning_effort": "low",
    },
    "research": {
        "model": RESEARCH_MODEL,
        "max_tokens": RESEARCH_MAX_TOKENS,
        "effective_max_tokens": max(MIN_COMPLETION_TOKENS, RESEARCH_MAX_TOKENS),
        "min_completion_tokens": MIN_COMPLETION_TOKENS,
        "reasoning_effort": "low",
    },
    "judge": {
        "model": JUDGE_MODEL,
        "max_tokens": JUDGE_MAX_TOKENS,
        "effective_max_tokens": max(MIN_COMPLETION_TOKENS, JUDGE_MAX_TOKENS),
        "min_completion_tokens": MIN_COMPLETION_TOKENS,
        "reasoning_effort": "low",
    },
}

AI_SCHEMA = AI_JUDGE_SCHEMA if _agt_mod is not None else {
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
        "modes": {
            "type": "object",
            "properties": {
                "scalping": {
                    "type": "object",
                    "properties": {
                        "decision": {"type": "string", "enum": ["LONG", "SHORT", "WAIT"]},
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                    "required": ["decision", "confidence", "reason"],
                    "additionalProperties": False,
                },
                "intraday": {
                    "type": "object",
                    "properties": {
                        "decision": {"type": "string", "enum": ["LONG", "SHORT", "WAIT"]},
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                    "required": ["decision", "confidence", "reason"],
                    "additionalProperties": False,
                },
                "swing": {
                    "type": "object",
                    "properties": {
                        "decision": {"type": "string", "enum": ["LONG", "SHORT", "WAIT"]},
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                    "required": ["decision", "confidence", "reason"],
                    "additionalProperties": False,
                },
            },
            "required": ["scalping", "intraday", "swing"],
            "additionalProperties": False,
        },
    },
    "required": ["decision", "confidence", "reasoning", "key_risk", "evidence", "modes"],
    "additionalProperties": False,
}

_REQUIRED_AI_FIELDS = ("decision", "confidence", "reasoning", "key_risk", "evidence")
_ALLOWED_AI_FIELDS = {"decision", "confidence", "reasoning", "key_risk", "evidence", "modes"}
_ALLOWED_DECISIONS = {"LONG", "SHORT", "WAIT"}
_ALLOWED_MODES = ("scalping", "intraday", "swing")
_REQUIRED_MODE_FIELDS = {"decision", "confidence", "reason"}

# Thread-safe atomic quota scope reservation lock & ledger (PH01-02 / F02 / T02)
_QUOTA_SCOPE_LOCK = threading.Lock()
_QUOTA_SCOPE_BUDGETS = {}


def reset_quota_scope_budget(scope_id="groq_default", max_physical_attempts=None, deadline_epoch=None):
    """Initialize or reset a shared quota scope budget for atomic physical attempt reservation."""
    with _QUOTA_SCOPE_LOCK:
        _QUOTA_SCOPE_BUDGETS[scope_id] = {
            "physical_used": 0,
            "logical_used": 0,
            "max_physical": int(max_physical_attempts) if max_physical_attempts is not None else None,
            "deadline_epoch": float(deadline_epoch) if deadline_epoch is not None else None,
            "exhausted_until": 0.0,
            "exhausted_reason": "",
        }
        return dict(_QUOTA_SCOPE_BUDGETS[scope_id])


def get_quota_scope_state(scope_id="groq_default"):
    with _QUOTA_SCOPE_LOCK:
        entry = _QUOTA_SCOPE_BUDGETS.get(scope_id)
        return dict(entry) if entry else {"physical_used": 0, "logical_used": 0, "max_physical": None, "deadline_epoch": None, "exhausted_until": 0.0, "exhausted_reason": ""}


def _mark_quota_scope_exhausted(scope_id="groq_default", seconds=86400.0, reason="DAILY_QUOTA_EXHAUSTED"):
    with _QUOTA_SCOPE_LOCK:
        scope = _QUOTA_SCOPE_BUDGETS.setdefault(
            scope_id,
            {"physical_used": 0, "logical_used": 0, "max_physical": None, "deadline_epoch": None, "exhausted_until": 0.0, "exhausted_reason": ""},
        )
        scope["exhausted_until"] = time.time() + float(seconds)
        scope["exhausted_reason"] = str(reason)
    if _stg_mod is not None:
        try:
            GLOBAL_DURABLE_BUDGET_MANAGER.mark_scope_exhausted(scope_id=scope_id, seconds=seconds, reason=reason, db_file=DB_FILE)
        except Exception:
            pass


def _resolve_physical_cap(explicit_cap=None):
    if explicit_cap is not None:
        return max(0, int(explicit_cap))
    return max(0, min(int(MAX_AI_CALLS_PER_CANDIDATE), int(MAX_PHYSICAL_AI_ATTEMPTS_PER_CANDIDATE)))


def _reserve_physical_attempt(scope_id="groq_default", max_physical_attempts=None, now_fn=time.time):
    """Atomically check and reserve 1 physical transport attempt across threads/workers, processes, and session state."""
    now = float(now_fn())
    if _stg_mod is not None:
        try:
            is_exh, exh_reason, _ = GLOBAL_DURABLE_BUDGET_MANAGER.is_scope_exhausted(scope_id=scope_id, now_fn=now_fn, db_file=DB_FILE)
            if is_exh:
                return False, f"QUOTA_SCOPE_EXHAUSTED:{exh_reason}", 0, _resolve_physical_cap(max_physical_attempts)
        except Exception:
            pass
    with _QUOTA_SCOPE_LOCK:
        scope = _QUOTA_SCOPE_BUDGETS.setdefault(
            scope_id,
            {"physical_used": 0, "logical_used": 0, "max_physical": None, "deadline_epoch": None, "exhausted_until": 0.0, "exhausted_reason": ""},
        )
        effective_cap = (
            int(max_physical_attempts)
            if max_physical_attempts is not None
            else (
                int(scope["max_physical"])
                if scope.get("max_physical") is not None
                else _resolve_physical_cap()
            )
        )
        if float(scope.get("exhausted_until", 0.0) or 0.0) > now:
            return False, f"QUOTA_SCOPE_EXHAUSTED:{scope.get('exhausted_reason', 'DAILY_QUOTA_EXHAUSTED')}", scope["physical_used"], effective_cap
        deadline = scope.get("deadline_epoch")
        if deadline is not None and now > float(deadline):
            return False, "DEADLINE_EXCEEDED", scope["physical_used"], effective_cap

        sess_used = int(st.session_state.get("_ai_physical_attempts_current_candidate", 0) or 0)
        current_used = max(int(scope["physical_used"]), sess_used)
        if current_used >= effective_cap:
            return False, "PHYSICAL_CAP_REACHED", current_used, effective_cap

        new_used = current_used + 1
        scope["physical_used"] = new_used
        st.session_state["_ai_physical_attempts_current_candidate"] = new_used
        return True, "OK", new_used, effective_cap


def resolve_effective_max_tokens(tier, requested_budget=None):
    """Explicitly resolve completion token cap without a hidden 384 floor override (F20 / T04)."""
    cfg = MODEL_TIERS.get(tier, MODEL_TIERS["analyst"])
    raw_target = int(requested_budget) if requested_budget is not None else int(cfg["max_tokens"])
    floor = max(1, int(MIN_COMPLETION_TOKENS))
    return max(floor, raw_target)


def _is_finite_number(value):
    """Return True iff value is an int/float (not bool) and math.isfinite."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _is_valid_finite_confidence(value):
    """Strictly validate that confidence is a finite int/float in [0, 100], rejecting bool, str, NaN, and ±Infinity (F03 / T03)."""
    if not _is_finite_number(value):
        return False
    val = float(value)
    return 0.0 <= val <= 100.0


def _sanitize_confidence_for_summary(value):
    if _is_valid_finite_confidence(value):
        return round(float(value), 2)
    return 0


def compact_json(obj, max_chars=1400):
    """Serialize a compact summary as guaranteed-valid JSON within max_chars by trimming fields before serialization (F19 / T04)."""
    max_chars = max(72, int(max_chars))
    if not isinstance(obj, dict):
        obj = {"decision": "WAIT", "confidence": 0, "reasoning": str(obj or ""), "key_risk": "", "evidence": []}

    raw_decision = obj.get("decision", "WAIT")
    decision = str(raw_decision).upper().strip() if isinstance(raw_decision, str) else "WAIT"
    if decision not in _ALLOWED_DECISIONS:
        decision = "WAIT"

    confidence = _sanitize_confidence_for_summary(obj.get("confidence", 0))
    reasoning = str(obj.get("reasoning", "") or "")
    key_risk = str(obj.get("key_risk", "") or "")
    raw_ev = obj.get("evidence", [])
    if not isinstance(raw_ev, list):
        raw_ev = [str(raw_ev)] if raw_ev else []
    evidence = [str(x) for x in raw_ev[:3] if x is not None]

    def _dump(r_text, k_text, ev_list):
        payload = {
            "decision": decision,
            "confidence": confidence,
            "reasoning": r_text,
            "key_risk": k_text,
            "evidence": ev_list,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    full = _dump(reasoning, key_risk, evidence)
    if len(full) <= max_chars:
        return full

    if len(_dump("", "", [])) > max_chars and isinstance(confidence, float):
        confidence = int(round(confidence))

    trimmed_ev = list(evidence)
    while trimmed_ev and len(_dump("", "", trimmed_ev)) > max(max_chars - 20, 72):
        if len(trimmed_ev[-1]) > 16:
            trimmed_ev[-1] = trimmed_ev[-1][: max(1, len(trimmed_ev[-1]) // 2)]
        else:
            trimmed_ev.pop()

    if len(_dump(reasoning, key_risk, trimmed_ev)) <= max_chars:
        return _dump(reasoning, key_risk, trimmed_ev)

    while trimmed_ev and len(_dump(reasoning[:12], key_risk[:12], trimmed_ev)) > max_chars:
        trimmed_ev.pop()

    lo = 0
    hi = max(len(reasoning), len(key_risk), 1)
    best_json = _dump("", "", [])
    while lo <= hi:
        mid = (lo + hi) // 2
        r_slice = reasoning[:mid]
        k_slice = key_risk[: max(0, mid // 2)]
        candidate_str = _dump(r_slice, k_slice, trimmed_ev)
        if len(candidate_str) <= max_chars:
            best_json = candidate_str
            lo = mid + 1
        else:
            hi = mid - 1

    json.loads(best_json)
    return best_json


def _provider_state(name):
    key = f"{name}_key_state"
    if key not in st.session_state:
        keys = GROQ_KEYS if name == "groq" else (GEMINI_KEYS if name == "gemini" else [])
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


def _build_prompt(role, data, instructions, tier="analyst", include_modes=None):
    _extra_s = list(GROQ_KEYS) + ([TG_TOKEN] if TG_TOKEN else [])
    if _agt_mod is not None:
        return _agt_mod.build_agent_prompt(
            role,
            data,
            instructions,
            tier=tier,
            include_modes=include_modes,
            extra_secrets=_extra_s,
        )
    if _cfg_mod is not None:
        data = redact_secrets(str(data or ""), extra_secrets=_extra_s)
        instructions = redact_secrets(str(instructions or ""), extra_secrets=_extra_s)
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
{{"decision":"LONG|SHORT|WAIT","confidence":0,"reasoning":"alasan singkat","key_risk":"risiko utama","evidence":["bukti 1","bukti 2"]}}
6. decision hanya LONG, SHORT, atau WAIT.
7. confidence adalah angka finite 0 sampai 100 (bukan NaN/Infinity/string/boolean).
8. evidence adalah array string, maksimal 3 item.
9. Jangan gunakan markdown, jangan gunakan ```json, dan jangan menulis teks sebelum/sesudah JSON.
10. Kata JSON di atas berarti output akhir harus benar-benar JSON yang dapat diparse Python.

DATA:
{data}
"""


def _reject_non_finite_constant(constant_str):
    raise ValueError(f"Konstanta JSON non-finite tidak diizinkan: {constant_str}")


def _safe_json_from_text(raw):
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Respons AI kosong.")

    cleaned = re.sub(r"```(?:json)?\s*", "", raw, flags=re.I).replace("```", "").strip()

    try:
        obj = json.loads(cleaned, parse_constant=_reject_non_finite_constant)
        if isinstance(obj, dict):
            return obj
    except ValueError as ve:
        if "Konstanta JSON non-finite" in str(ve):
            raise
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
                        obj = json.loads(candidate, parse_constant=_reject_non_finite_constant)
                        if isinstance(obj, dict):
                            return obj
                    except ValueError as ve:
                        if "Konstanta JSON non-finite" in str(ve):
                            raise
                        break
                    except Exception:
                        break
    raise ValueError("Respons AI bukan JSON valid.")


def _extract_usage(raw_response):
    """Extract token usage metadata from provider response without fabricating zero-cost when usage is missing (F20 / T04)."""
    usage_obj = getattr(raw_response, "usage", None) if raw_response is not None else None
    if usage_obj is None and isinstance(raw_response, dict):
        usage_obj = raw_response.get("usage")

    if usage_obj is None:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": None,
            "usage_known": False,
            "cost_status": "UNKNOWN_USAGE",
        }

    def _get_int(src, *names):
        for n in names:
            val = getattr(src, n, None) if not isinstance(src, dict) else src.get(n)
            if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
                return val
        return None

    in_tok = _get_int(usage_obj, "prompt_tokens", "input_tokens")
    out_tok = _get_int(usage_obj, "completion_tokens", "output_tokens")
    tot_tok = _get_int(usage_obj, "total_tokens")
    if tot_tok is None and in_tok is not None and out_tok is not None:
        tot_tok = in_tok + out_tok

    details = (
        getattr(usage_obj, "completion_tokens_details", None)
        if not isinstance(usage_obj, dict)
        else usage_obj.get("completion_tokens_details")
    )
    reasoning_tok = _get_int(details, "reasoning_tokens") if details is not None else None
    known = any(x is not None for x in (in_tok, out_tok, tot_tok))

    return {
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "reasoning_tokens": reasoning_tok,
        "total_tokens": tot_tok,
        "usage_known": bool(known),
        "cost_status": "MEASURED" if known else "UNKNOWN_USAGE",
    }


def _invalid_ai_output(reason, tier, provider="unknown", model=None, finish_reason=None, usage=None, requested_max_tokens=None, effective_max_tokens=None):
    cfg = MODEL_TIERS.get(tier, MODEL_TIERS["analyst"])
    eff_tok = effective_max_tokens if effective_max_tokens is not None else resolve_effective_max_tokens(tier, requested_max_tokens)
    req_tok = requested_max_tokens if requested_max_tokens is not None else cfg["max_tokens"]
    return {
        "status": "INVALID_OUTPUT",
        "decision": "WAIT",
        "confidence": 0.0,
        "reasoning": f"Output AI ditolak validator (INVALID_OUTPUT): {str(reason)[:500]}",
        "key_risk": "Output AI tidak memenuhi skema atau batas numerik finite.",
        "evidence": [],
        "modes": {},
        "tier": tier,
        "provider": provider,
        "model": model or cfg["model"],
        "finish_reason": finish_reason,
        "usage": usage or _extract_usage(None),
        "requested_max_tokens": int(req_tok),
        "effective_max_tokens": int(eff_tok),
    }


def _normalize_ai(
    obj,
    tier,
    provider="unknown",
    model=None,
    finish_reason=None,
    usage=None,
    requested_max_tokens=None,
    effective_max_tokens=None,
    strict=True,
    role="",
    strict_role_schema=False,
):
    """Validate and normalize AI output; reject non-finite numbers, invalid types, missing/extra fields, invalid enums, or length truncation (F03, F18, F20, T03)."""
    if tier not in MODEL_TIERS:
        tier = "analyst"
    cfg = MODEL_TIERS[tier]
    eff_tok = effective_max_tokens if effective_max_tokens is not None else resolve_effective_max_tokens(tier, requested_max_tokens)
    req_tok = requested_max_tokens if requested_max_tokens is not None else cfg["max_tokens"]
    norm_usage = usage if isinstance(usage, dict) else _extract_usage(None)

    if str(finish_reason or "").strip().lower() == "length":
        return _invalid_ai_output(
            "Respons terpotong karena batas token (finish_reason=length).",
            tier,
            provider=provider,
            model=model,
            finish_reason=finish_reason,
            usage=norm_usage,
            requested_max_tokens=req_tok,
            effective_max_tokens=eff_tok,
        )

    if not isinstance(obj, dict):
        return _invalid_ai_output(
            "Output AI bukan object JSON.",
            tier,
            provider=provider,
            model=model,
            finish_reason=finish_reason,
            usage=norm_usage,
            requested_max_tokens=req_tok,
            effective_max_tokens=eff_tok,
        )

    missing = [k for k in _REQUIRED_AI_FIELDS if k not in obj]
    if missing:
        return _invalid_ai_output(
            f"Field wajib tidak lengkap: {', '.join(missing)}",
            tier,
            provider=provider,
            model=model,
            finish_reason=finish_reason,
            usage=norm_usage,
            requested_max_tokens=req_tok,
            effective_max_tokens=eff_tok,
        )

    if strict:
        judge_flag = (_agt_mod.is_judge_role(role, tier) if _agt_mod is not None else (tier == "judge"))
        if strict_role_schema:
            allowed_fields = _ALLOWED_AI_FIELDS if judge_flag else set(_REQUIRED_AI_FIELDS)
            if judge_flag and "modes" not in obj:
                return _invalid_ai_output(
                    "Final Judge wajib menyertakan field modes (scalping, intraday, swing).",
                    tier,
                    provider=provider,
                    model=model,
                    finish_reason=finish_reason,
                    usage=norm_usage,
                    requested_max_tokens=req_tok,
                    effective_max_tokens=eff_tok,
                )
        else:
            allowed_fields = _ALLOWED_AI_FIELDS
        extra = set(obj.keys()) - allowed_fields
        if extra:
            return _invalid_ai_output(
                f"Field ekstra tidak dikenal: {', '.join(sorted(extra))}",
                tier,
                provider=provider,
                model=model,
                finish_reason=finish_reason,
                usage=norm_usage,
                requested_max_tokens=req_tok,
                effective_max_tokens=eff_tok,
            )

    raw_decision = obj.get("decision")
    if not isinstance(raw_decision, str):
        return _invalid_ai_output(
            "Field decision harus berupa string.",
            tier,
            provider=provider,
            model=model,
            finish_reason=finish_reason,
            usage=norm_usage,
            requested_max_tokens=req_tok,
            effective_max_tokens=eff_tok,
        )
    decision = raw_decision.strip().upper()
    if decision not in _ALLOWED_DECISIONS:
        return _invalid_ai_output(
            f"Enum decision tidak valid: {raw_decision}",
            tier,
            provider=provider,
            model=model,
            finish_reason=finish_reason,
            usage=norm_usage,
            requested_max_tokens=req_tok,
            effective_max_tokens=eff_tok,
        )

    raw_conf = obj.get("confidence")
    if not _is_valid_finite_confidence(raw_conf):
        return _invalid_ai_output(
            f"Nilai confidence tidak valid atau non-finite: {raw_conf!r}",
            tier,
            provider=provider,
            model=model,
            finish_reason=finish_reason,
            usage=norm_usage,
            requested_max_tokens=req_tok,
            effective_max_tokens=eff_tok,
        )
    confidence = float(raw_conf)

    reasoning = obj.get("reasoning")
    key_risk = obj.get("key_risk")
    if not isinstance(reasoning, str) or not isinstance(key_risk, str):
        return _invalid_ai_output(
            "Field reasoning dan key_risk harus berupa string.",
            tier,
            provider=provider,
            model=model,
            finish_reason=finish_reason,
            usage=norm_usage,
            requested_max_tokens=req_tok,
            effective_max_tokens=eff_tok,
        )

    evidence = obj.get("evidence")
    if not isinstance(evidence, list) or any(not isinstance(item, str) for item in evidence):
        return _invalid_ai_output(
            "Field evidence harus berupa array of strings.",
            tier,
            provider=provider,
            model=model,
            finish_reason=finish_reason,
            usage=norm_usage,
            requested_max_tokens=req_tok,
            effective_max_tokens=eff_tok,
        )

    modes = obj.get("modes")
    normalized_modes = {}
    if modes is not None:
        if not isinstance(modes, dict):
            return _invalid_ai_output(
                "Field modes harus berupa object.",
                tier,
                provider=provider,
                model=model,
                finish_reason=finish_reason,
                usage=norm_usage,
                requested_max_tokens=req_tok,
                effective_max_tokens=eff_tok,
            )
        extra_modes = set(modes.keys()) - set(_ALLOWED_MODES)
        if strict and extra_modes:
            return _invalid_ai_output(
                f"Mode tidak dikenal: {', '.join(sorted(extra_modes))}",
                tier,
                provider=provider,
                model=model,
                finish_reason=finish_reason,
                usage=norm_usage,
                requested_max_tokens=req_tok,
                effective_max_tokens=eff_tok,
            )
        for mode_name in _ALLOWED_MODES:
            if mode_name not in modes:
                continue
            m = modes.get(mode_name)
            if not isinstance(m, dict):
                return _invalid_ai_output(
                    f"Mode {mode_name} harus berupa object.",
                    tier,
                    provider=provider,
                    model=model,
                    finish_reason=finish_reason,
                    usage=norm_usage,
                    requested_max_tokens=req_tok,
                    effective_max_tokens=eff_tok,
                )
            if not _REQUIRED_MODE_FIELDS.issubset(set(m.keys())):
                return _invalid_ai_output(
                    f"Mode {mode_name} kehilangan field wajib.",
                    tier,
                    provider=provider,
                    model=model,
                    finish_reason=finish_reason,
                    usage=norm_usage,
                    requested_max_tokens=req_tok,
                    effective_max_tokens=eff_tok,
                )
            if strict and (set(m.keys()) - _REQUIRED_MODE_FIELDS):
                return _invalid_ai_output(
                    f"Mode {mode_name} memiliki field ekstra tidak dikenal.",
                    tier,
                    provider=provider,
                    model=model,
                    finish_reason=finish_reason,
                    usage=norm_usage,
                    requested_max_tokens=req_tok,
                    effective_max_tokens=eff_tok,
                )
            raw_md = m.get("decision")
            raw_mc = m.get("confidence")
            raw_mr = m.get("reason")
            if not isinstance(raw_md, str) or raw_md.strip().upper() not in _ALLOWED_DECISIONS:
                return _invalid_ai_output(
                    f"Decision mode {mode_name} tidak valid: {raw_md!r}",
                    tier,
                    provider=provider,
                    model=model,
                    finish_reason=finish_reason,
                    usage=norm_usage,
                    requested_max_tokens=req_tok,
                    effective_max_tokens=eff_tok,
                )
            if not _is_valid_finite_confidence(raw_mc) or not isinstance(raw_mr, str):
                return _invalid_ai_output(
                    f"Confidence/reason mode {mode_name} tidak valid.",
                    tier,
                    provider=provider,
                    model=model,
                    finish_reason=finish_reason,
                    usage=norm_usage,
                    requested_max_tokens=req_tok,
                    effective_max_tokens=eff_tok,
                )
            normalized_modes[mode_name] = {
                "decision": raw_md.strip().upper(),
                "confidence": float(raw_mc),
                "reason": raw_mr[:240],
            }

    return {
        "status": "OK",
        "decision": decision,
        "confidence": confidence,
        "reasoning": reasoning[:700],
        "key_risk": key_risk[:400],
        "evidence": [x[:180] for x in evidence[:3]],
        "modes": normalized_modes,
        "tier": tier,
        "provider": provider,
        "model": model or cfg["model"],
        "finish_reason": finish_reason,
        "usage": norm_usage,
        "requested_max_tokens": int(req_tok),
        "effective_max_tokens": int(eff_tok),
    }


def _groq_client(key):
    """Single retry owner: disable internal SDK retries (max_retries=0) and enforce explicit HTTP timeout (F02 / T02)."""
    try:
        return Groq(api_key=key, max_retries=0, timeout=AI_HTTP_TIMEOUT_SECONDS)
    except TypeError:
        return Groq(api_key=key)


def _request_groq(model, prompt, key, max_tokens, tier, role="", strict_registry=True):
    """Groq request with capability registry check, explicit token budget, and role-specific JSON schema (PH05-01, F18, F20, T04, T21)."""
    effective_tokens = resolve_effective_max_tokens(tier, max_tokens)
    if _prov_mod is not None:
        req_bundle = _prov_mod.build_provider_request_payload(
            role=role,
            tier=tier,
            model_id=model,
            prompt=prompt,
            max_completion_tokens=effective_tokens,
            provider="groq",
            reasoning_effort=MODEL_TIERS.get(tier, {}).get("reasoning_effort", "low"),
            strict_registry=strict_registry,
        )
        return _groq_client(key).chat.completions.create(**req_bundle["sdk_kwargs"])

    target_schema = _agt_mod.get_schema_for_role(role, tier) if _agt_mod is not None else AI_SCHEMA
    schema_name = "trading_judge_decision" if (_agt_mod is not None and _agt_mod.is_judge_role(role, tier)) else "trading_agent_decision"
    kwargs = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_completion_tokens": effective_tokens,
    }
    if model in {"openai/gpt-oss-20b", "openai/gpt-oss-120b"}:
        kwargs["reasoning_effort"] = MODEL_TIERS[tier].get("reasoning_effort", "low")
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": target_schema,
            },
        }
    elif model.startswith("qwen/"):
        kwargs["reasoning_effort"] = "none"
        kwargs["response_format"] = {"type": "json_object"}
    else:
        kwargs["response_format"] = {"type": "json_object"}

    return _groq_client(key).chat.completions.create(**kwargs)


def _request_gemini(model, prompt, key, max_tokens, tier, role="", strict_registry=True):
    """Optional Gemini fallback request enforcing capability registry and role-specific schema (PH05-04, T21)."""
    effective_tokens = resolve_effective_max_tokens(tier, max_tokens)
    payload_bundle = (
        _prov_mod.build_gemini_http_payload(
            role=role,
            tier=tier,
            model_id=model,
            prompt=prompt,
            max_completion_tokens=effective_tokens,
            strict_registry=strict_registry,
        )
        if _prov_mod is not None
        else {"http_body": {"contents": [{"parts": [{"text": prompt}]}]}}
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    r = requests.post(
        url,
        params={"key": key},
        json=payload_bundle["http_body"],
        timeout=AI_HTTP_TIMEOUT_SECONDS,
    )
    if r.status_code != 200:
        err_exc = RuntimeError(f"Gemini HTTP {r.status_code}: {(r.text or '')[:300]}")
        err_exc.status_code = r.status_code
        err_exc.headers = dict(getattr(r, "headers", {}) or {})
        raise err_exc
    data = r.json()
    cand_list = data.get("candidates") or []
    first_cand = cand_list[0] if cand_list else {}
    parts = first_cand.get("content", {}).get("parts") or []
    text_out = parts[0].get("text", "") if parts else ""
    finish_r = str(first_cand.get("finishReason", "STOP")).lower()
    if finish_r == "max_tokens":
        finish_r = "length"
    u_meta = data.get("usageMetadata") or {}

    class _GemMsg:
        def __init__(self, c):
            self.content = c
            self.reasoning = None

    class _GemChoice:
        def __init__(self, c, fr):
            self.message = _GemMsg(c)
            self.finish_reason = fr

    class _GemResp:
        def __init__(self, c, fr, um):
            self.choices = [_GemChoice(c, fr)]
            self.usage = (
                {
                    "prompt_tokens": um.get("promptTokenCount"),
                    "completion_tokens": um.get("candidatesTokenCount"),
                    "total_tokens": um.get("totalTokenCount"),
                }
                if um
                else None
            )

    return _GemResp(text_out, finish_r, u_meta)


def _ordered_key_indices(provider):
    if provider == "groq":
        if not GROQ_KEYS:
            return []
        active = int(st.session_state.get("_active_groq_key", 0)) % len(GROQ_KEYS)
        return list(range(active, len(GROQ_KEYS))) + list(range(0, active))
    if provider == "gemini":
        if not GEMINI_KEYS:
            return []
        active = int(st.session_state.get("_active_gemini_key", 0)) % len(GEMINI_KEYS)
        return list(range(active, len(GEMINI_KEYS))) + list(range(0, active))
    return []


def _provider_models(provider, tier):
    if provider == "groq":
        return [MODEL_TIERS[tier]["model"]]
    if provider == "gemini":
        return [GEMINI_MODEL_TIERS.get(tier, "gemini-2.5-flash")]
    return []


def call_ai(
    role,
    data,
    instructions,
    tier="analyst",
    budget=None,
    quota_scope="groq_default",
    max_physical_attempts=None,
    deadline_epoch=None,
    analysis_id=None,
    budget_manager=None,
):
    if tier not in MODEL_TIERS:
        tier = "analyst"

    cfg = MODEL_TIERS[tier]
    requested_tokens = int(budget) if budget is not None else int(cfg["max_tokens"])
    effective_tokens = resolve_effective_max_tokens(tier, requested_tokens)

    physical_cap = _resolve_physical_cap(max_physical_attempts)
    with _QUOTA_SCOPE_LOCK:
        if "_ai_calls_current_candidate" not in st.session_state:
            st.session_state["_ai_calls_current_candidate"] = 0
        if "_ai_physical_attempts_current_candidate" not in st.session_state:
            st.session_state["_ai_physical_attempts_current_candidate"] = 0

        logical_used = int(st.session_state.get("_ai_calls_current_candidate", 0) or 0)
        physical_used = int(st.session_state.get("_ai_physical_attempts_current_candidate", 0) or 0)

        if logical_used >= int(MAX_AI_CALLS_PER_CANDIDATE) or physical_used >= physical_cap:
            return {
                "status": "BUDGET_EXHAUSTED",
                "decision": "WAIT",
                "confidence": 0.0,
                "reasoning": "Batas panggilan/percobaan fisik AI per kandidat tercapai.",
                "key_risk": "Quota dan batas percobaan fisik dihemat.",
                "evidence": [],
                "modes": {},
                "tier": tier,
                "provider": "none",
                "model": "",
                "finish_reason": None,
                "usage": _extract_usage(None),
                "requested_max_tokens": requested_tokens,
                "effective_max_tokens": effective_tokens,
            }

        st.session_state["_ai_calls_current_candidate"] = logical_used + 1

    call_deadline = (
        float(deadline_epoch)
        if deadline_epoch is not None
        else float(st.session_state.get("_ai_candidate_deadline", 0) or 0)
    )
    prompt = _build_prompt(role, data, instructions, tier=tier)
    errors = []
    attempts_this_call = 0
    per_call_limit = max(1, int(MAX_PHYSICAL_ATTEMPTS_PER_CALL))
    last_err_category = None
    durable_mgr = budget_manager or (GLOBAL_DURABLE_BUDGET_MANAGER if _stg_mod is not None else None)
    durable_res_id = None

    if durable_mgr is not None and analysis_id:
        reserved_durable, res_reason_durable, durable_res_id = durable_mgr.reserve_attempt(
            analysis_id=analysis_id,
            scope_id=quota_scope,
            role=role,
            tier=tier,
            requested_budget=effective_tokens,
            max_physical_attempts=physical_cap,
            deadline_epoch=(call_deadline if call_deadline > 0 else None),
            db_file=DB_FILE,
        )
        if not reserved_durable:
            return {
                "status": "BUDGET_EXHAUSTED",
                "error_category": res_reason_durable.split(":")[0],
                "decision": "WAIT",
                "confidence": 0.0,
                "reasoning": f"Batas kuota/percobaan fisik tercapai ({res_reason_durable}).",
                "key_risk": "Quota scope dan Final Judge terlindungi.",
                "evidence": [],
                "modes": {},
                "tier": tier,
                "provider": "none",
                "model": "",
                "finish_reason": None,
                "usage": _extract_usage(None),
                "requested_max_tokens": requested_tokens,
                "effective_max_tokens": effective_tokens,
            }

    providers = []
    if GROQ_KEYS:
        providers.append("groq")
    if GEMINI_ENABLED and GEMINI_KEYS:
        providers.append("gemini")

    abort_fallback_providers = False

    for provider in providers:
        if abort_fallback_providers:
            break
        keys = GROQ_KEYS if provider == "groq" else (GEMINI_KEYS if provider == "gemini" else [])
        if not keys:
            continue
        provider_scope = quota_scope if provider == "groq" else f"{provider}_default"

        for model in _provider_models(provider, tier):
            if _prov_mod is not None:
                try:
                    _prov_mod.get_model_capability(model, provider=provider, strict_registry=True)
                except _prov_mod.ProviderModelUnsupportedError as cap_err:
                    last_err_category = "MODEL_UNSUPPORTED"
                    errors.append(f"{provider} / {model}: {str(cap_err)[:240]}")
                    abort_fallback_providers = True
                    break
            for key_index in _ordered_key_indices(provider):
                if attempts_this_call >= per_call_limit:
                    break
                if _key_disabled(provider, key_index):
                    continue
                if call_deadline > 0 and time.time() > call_deadline:
                    errors.append("Deadline kandidat terlampaui sebelum request.")
                    break

                reserved, reserve_reason, _, _ = _reserve_physical_attempt(
                    scope_id=provider_scope if provider != "groq" and get_quota_scope_state(quota_scope).get("exhausted_until", 0.0) > time.time() else quota_scope,
                    max_physical_attempts=max_physical_attempts,
                )
                if not reserved:
                    errors.append(f"Reservasi fisik ditolak: {reserve_reason}")
                    break

                attempts_this_call += 1
                key = keys[key_index]
                try:
                    last = float(st.session_state.get("_last_ai_call", 0) or 0)
                    wait = GROQ_MIN_DELAY - (time.time() - last)
                    if wait > 0:
                        time.sleep(wait)
                    st.session_state["_last_ai_call"] = time.time()
                    if provider == "gemini":
                        try:
                            raw = _request_gemini(model, prompt, key, effective_tokens, tier, role=role)
                        except TypeError:
                            raw = _request_gemini(model, prompt, key, effective_tokens, tier)
                        st.session_state["_active_gemini_key"] = key_index
                    else:
                        try:
                            raw = _request_groq(model, prompt, key, effective_tokens, tier, role=role)
                        except TypeError:
                            raw = _request_groq(model, prompt, key, effective_tokens, tier)
                        st.session_state["_active_groq_key"] = key_index

                    choice = raw.choices[0]
                    finish_reason = getattr(choice, "finish_reason", None)
                    usage_info = _extract_usage(raw)
                    message = choice.message
                    content = message.content or ""

                    if str(finish_reason or "").strip().lower() == "length":
                        if durable_mgr is not None and durable_res_id:
                            durable_mgr.commit_usage(durable_res_id, usage_info, status_str="INVALID_OUTPUT", role=role, provider=provider, model_id=model, finish_reason=finish_reason, db_file=DB_FILE)
                        return _invalid_ai_output(
                            "Respons terpotong karena batas token (finish_reason=length).",
                            tier,
                            provider=provider,
                            model=model,
                            finish_reason=finish_reason,
                            usage=usage_info,
                            requested_max_tokens=requested_tokens,
                            effective_max_tokens=effective_tokens,
                        )

                    if not content.strip():
                        reasoning = getattr(message, "reasoning", None) or ""
                        if reasoning:
                            raise RuntimeError(
                                "Respons AI kosong (reasoning ada tetapi content JSON tidak keluar)."
                            )
                        raise RuntimeError("Respons AI kosong.")

                    obj = _safe_json_from_text(content)
                    normalized = _normalize_ai(
                        obj,
                        tier,
                        provider=provider,
                        model=model,
                        finish_reason=finish_reason,
                        usage=usage_info,
                        requested_max_tokens=requested_tokens,
                        effective_max_tokens=effective_tokens,
                        role=role,
                    )
                    if durable_mgr is not None and durable_res_id:
                        durable_mgr.commit_usage(durable_res_id, usage_info, status_str=normalized.get("status", "OK"), role=role, provider=provider, model_id=model, finish_reason=finish_reason, db_file=DB_FILE)
                    if normalized.get("status") != "OK":
                        return normalized
                    return normalized

                except Exception as e:
                    msg = str(e)
                    errors.append(f"{provider} {_key_mask(key)} / {model}: {msg[:240]}")

                    if _prov_mod is not None:
                        typed_err = _prov_mod.coerce_to_provider_error(
                            e,
                            provider=provider,
                            model_id=model,
                            default_retry_seconds=GROQ_RETRY_SECONDS,
                        )
                        last_err_category = typed_err.category
                        if typed_err.disable_key:
                            _disable_key(provider, key_index, typed_err.cooldown_seconds, typed_err.category)
                        if typed_err.exhaust_quota_scope:
                            _mark_quota_scope_exhausted(quota_scope, seconds=typed_err.cooldown_seconds, reason=typed_err.category)
                            break
                        if not typed_err.allow_fallback_provider:
                            abort_fallback_providers = True
                        if not typed_err.retry_another_key and typed_err.category == "MODEL_UNSUPPORTED":
                            break
                        continue

                    err_cls = (
                        _agt_mod.classify_provider_error(msg, default_retry_seconds=GROQ_RETRY_SECONDS)
                        if _agt_mod is not None
                        else None
                    )
                    if err_cls is not None:
                        last_err_category = err_cls["category"]
                        if err_cls["disable_key"]:
                            _disable_key(provider, key_index, err_cls["cooldown_seconds"], err_cls["reason"])
                        if err_cls["exhaust_quota_scope"]:
                            _mark_quota_scope_exhausted(quota_scope, seconds=err_cls["cooldown_seconds"], reason=err_cls["category"])
                            break
                        continue

                    if _is_quota_error(msg):
                        last_err_category = "DAILY_QUOTA_EXHAUSTED"
                        _disable_key(provider, key_index, 24 * 60 * 60, "Quota harian")
                        _mark_quota_scope_exhausted(quota_scope, seconds=24 * 60 * 60, reason="DAILY_QUOTA_EXHAUSTED")
                        break
                    if _is_rate_limit_error(msg):
                        last_err_category = "RATE_LIMIT_TEMPORARY"
                        _disable_key(provider, key_index, GROQ_RETRY_SECONDS, "Rate limit")
                        continue
                    if _is_auth_error(msg):
                        last_err_category = "AUTH_ERROR"
                        _disable_key(provider, key_index, 24 * 60 * 60, "API key/balance")
                        continue
                    if _is_model_not_found(msg):
                        last_err_category = "MODEL_UNSUPPORTED"
                        continue
                    if "timeout" in msg.lower() or "connection" in msg.lower():
                        last_err_category = "TIMEOUT_OR_NETWORK"
                        _disable_key(provider, key_index, 60, "Network/timeout")
                        continue
                    continue

            continue

    raw_summary = " | ".join(errors[-5:]) if errors else "Tidak ada provider API yang tersedia."
    summary = redact_secrets(raw_summary, extra_secrets=list(GROQ_KEYS) + list(GEMINI_KEYS)) if _cfg_mod is not None else raw_summary
    status_code = (
        "BUDGET_EXHAUSTED"
        if (
            last_err_category == "DAILY_QUOTA_EXHAUSTED"
            or any("PHYSICAL_CAP_REACHED" in e or "DEADLINE_EXCEEDED" in e or "QUOTA_SCOPE_EXHAUSTED" in e for e in errors)
        )
        else "FAILED"
    )
    return {
        "status": status_code,
        "error_category": last_err_category or "UNKNOWN",
        "decision": "WAIT",
        "confidence": 0.0,
        "reasoning": f"AI gagal setelah mencoba provider yang tersedia: {summary}",
        "key_risk": "Periksa API key, quota, akses model, atau koneksi provider.",
        "evidence": [],
        "modes": {},
        "tier": tier,
        "provider": "none",
        "model": "",
        "finish_reason": None,
        "usage": _extract_usage(None),
        "requested_max_tokens": requested_tokens,
        "effective_max_tokens": effective_tokens,
    }

# -------------------------
# Market packet (PH04-01 Multi-Horizon 5m/15m/1h/4h/1d)
# -------------------------
def market_packet(symbol, as_of=None, use_cache=False, cache=None, budget=None, include_higher_timeframes=True):
    active_cache = cache if cache is not None else (GLOBAL_MARKET_CACHE if _mkt_mod is not None else None)
    active_budget = budget
    frames = {}
    snapshots = {}
    timeframe_diagnostics = {}

    tf_specs = [("5m", 260), ("15m", 260), ("1h", 260)]
    if include_higher_timeframes:
        tf_specs.extend([("4h", 260), ("1d", 260)])

    for tf, limit in tf_specs:
        is_core_tf = tf in ("5m", "15m", "1h")
        snap = None
        raw_df = None
        try:
            if use_cache and active_cache is not None:
                snap = active_cache.get_kline_snapshot(symbol, tf, limit, now_ms=as_of)
            if snap is None:
                if active_budget is not None:
                    ok_b, reason_b = active_budget.check_and_reserve("/api/v3/klines", now_ms=as_of)
                    if not ok_b:
                        if is_core_tf:
                            raise RuntimeError(reason_b)
                        timeframe_diagnostics[tf] = {
                            "loaded": False,
                            "status": reason_b.split(":")[0],
                            "error": reason_b,
                            "ema200_ready": False,
                            "warmup_status": "MISSING",
                        }
                        continue
                raw_df = klines(symbol, tf, limit)
                if _mkt_mod is not None:
                    snap = _mkt_mod.build_timeframe_snapshot(raw_df, symbol=symbol, interval=tf, as_of=as_of, limit=limit)
                    if use_cache and active_cache is not None and snap.validation.valid:
                        active_cache.put_kline_snapshot(snap, limit=limit, fetched_at_ms=snap.as_of_ms)
            if snap is not None:
                df_closed = snap.copy_closed_df() if len(snap.closed_df) > 0 else snap.raw_df
                if raw_df is not None and hasattr(raw_df, "attrs"):
                    for ak in ("warmup_report", "ema200_ready", "warmup_status", "indicator_version"):
                        if ak in raw_df.attrs:
                            df_closed.attrs[ak] = raw_df.attrs[ak]
                if len(df_closed) == 0 or not snap.validation.valid:
                    if is_core_tf and len(df_closed) == 0:
                        raise RuntimeError(f"Kline {tf} kosong untuk {symbol}")
                    timeframe_diagnostics[tf] = {
                        "loaded": False,
                        "status": "INVALID_OR_EMPTY_DATA",
                        "errors": list(snap.validation.errors),
                        "ema200_ready": False,
                        "warmup_status": "EMPTY_DATA" if len(df_closed) == 0 else "INVALID_DATA",
                    }
                    continue
                snapshots[tf] = snap
                frames[tf] = df_closed
            else:
                df_closed = _closed_df(raw_df, as_of=as_of, interval=tf)
                if len(df_closed) == 0:
                    if is_core_tf:
                        raise RuntimeError(f"Kline {tf} kosong untuk {symbol}")
                    timeframe_diagnostics[tf] = {
                        "loaded": False,
                        "status": "EMPTY_DATA",
                        "ema200_ready": False,
                        "warmup_status": "EMPTY_DATA",
                    }
                    continue
                frames[tf] = df_closed
        except Exception as exc:
            if _scan_mod is not None and _scan_mod.is_http_429_error(exc) and active_budget is not None:
                active_budget.record_rate_limit(_scan_mod.extract_retry_after_seconds(exc), now_ms=as_of)
            if is_core_tf:
                raise
            timeframe_diagnostics[tf] = {
                "loaded": False,
                "status": "FETCH_FAILED",
                "error": _safe_ui_error(exc)[:240],
                "ema200_ready": False,
                "warmup_status": "FETCH_FAILED",
            }

    ob = None
    if use_cache and active_cache is not None:
        ob = active_cache.get_order_book(symbol, limit=20, now_ms=as_of)
    if ob is None:
        if active_budget is not None:
            ok_ob, reason_ob = active_budget.check_and_reserve("/api/v3/depth", now_ms=as_of)
            if not ok_ob:
                raise RuntimeError(reason_ob)
        ob = order_book(symbol)
        if use_cache and active_cache is not None and isinstance(ob, dict):
            active_cache.put_order_book(symbol, ob, limit=20, fetched_at_ms=as_of)
    d = frames["15m"]
    c = candle_info(d)
    a = d.iloc[-1]
    support = float(d.Low.tail(30).min())
    resistance = float(d.High.tail(30).max())

    smc_by_tf = {tf: smc_analysis(df, interval=tf) for tf, df in frames.items()}
    as_of_ms_val = snapshots["15m"].as_of_ms if "15m" in snapshots else (int(time.time() * 1000))
    as_of_iso_val = snapshots["15m"].as_of_iso if "15m" in snapshots else datetime.now(timezone.utc).isoformat()

    live_15m = snapshots["15m"].live_bar.to_dict() if ("15m" in snapshots and snapshots["15m"].live_bar is not None) else None
    quote_price = float(live_15m["close"]) if (live_15m is not None and live_15m.get("close", 0) > 0) else float(a.Close)

    packet = {
        "symbol": symbol,
        "as_of_ms": as_of_ms_val,
        "as_of": as_of_iso_val,
        "price": quote_price,
        "closed_price": float(a.Close),
        "market_regime": market_regime(d),
        "order_book": ob,
        "support": support,
        "resistance": resistance,
        "candlestick": c,
        "timeframes": {},
        "timeframe_diagnostics": timeframe_diagnostics,
        "higher_tf_errors": {
            tf: str(diag.get("error") or diag.get("status") or "FAILED")
            for tf, diag in timeframe_diagnostics.items()
            if not diag.get("loaded", False)
        },
        "watermarks": {tf: s.watermark.to_dict() for tf, s in snapshots.items()},
        "live_quotes": {tf: (s.live_bar.to_dict() if s.live_bar is not None else None) for tf, s in snapshots.items()},
        "validation": {tf: s.validation.to_dict() for tf, s in snapshots.items()},
        "freshness": {
            tf: {
                "freshness_seconds": s.freshness_seconds,
                "is_fresh": s.is_fresh,
                "status": s.freshness_status,
                "cache_key": s.cache_key,
                "from_cache": s.from_cache,
            }
            for tf, s in snapshots.items()
        },
        "smc": smc_by_tf["15m"],
        "smc_by_tf": smc_by_tf,
    }
    for tf, df in frames.items():
        x = df.iloc[-1]
        wm_info = packet["watermarks"].get(tf, {})
        w_rep = df.attrs.get("warmup_report") if isinstance(df.attrs.get("warmup_report"), dict) else {}
        raw_bar_count = int(w_rep.get("bar_count", len(df)))
        ema200_ready = bool(df.attrs.get("ema200_ready", raw_bar_count >= 200))
        warmup_status = str(df.attrs.get("warmup_status", "READY" if ema200_ready else "PARTIAL_WARMUP_NO_EMA200"))
        quality_status = str(w_rep.get("quality_status", "OK"))
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
            "open_time": wm_info.get("open_time"),
            "close_time": wm_info.get("close_time"),
            "is_closed": wm_info.get("is_closed", True),
            "bar_count": raw_bar_count,
            "usable_bar_count": len(df),
            "ema200_ready": ema200_ready,
            "warmup_status": warmup_status,
            "quality_status": quality_status,
            "indicator_version": str(df.attrs.get("indicator_version", "v6.0-wilder-ewm-1")),
        }
        packet["timeframe_diagnostics"][tf] = {
            "loaded": True,
            "status": warmup_status,
            "bar_count": raw_bar_count,
            "usable_bar_count": len(df),
            "ema200_ready": ema200_ready,
            "warmup_status": warmup_status,
            "quality_status": quality_status,
            "from_cache": bool(packet["freshness"].get(tf, {}).get("from_cache", False)),
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
        "=== SMART MONEY / PRICE STRUCTURE ===",
        smc_text(packet["smc"]),
        "",
    ]
    for tf, x in packet["timeframes"].items():
        lines.extend([
            f"[TIMEFRAME {tf}]",
            f"WARMUP_STATUS={x.get('warmup_status', 'READY')} EMA200_READY={x.get('ema200_ready', True)}",
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
            f"[SMC {tf}]",
            smc_text(packet["smc_by_tf"][tf]),
            "",
        ])
    return "\n".join(lines)

def specialist_data(packet, focus):
    tf = packet["timeframes"]
    smc = packet["smc"]
    avail_tfs = [name for name in ("5m", "15m", "1h", "4h", "1d") if name in packet.get("smc_by_tf", {}) and name in tf]
    lines = [
        f"Symbol: {packet['symbol']}",
        f"Harga: {packet['price']:.8f}",
        f"Regime: {packet['market_regime']}",
        f"Support: {packet['support']:.8f}",
        f"Resistance: {packet['resistance']:.8f}",
        f"Available Timeframes: {', '.join(avail_tfs)}",
    ]
    if focus == "structure":
        for name in avail_tfs:
            stc = packet["smc_by_tf"][name]["structure"]
            t_info = tf[name]
            lines += [
                f"[{name}] bias={stc['bias']} BOS={stc['bos']} CHOCH={stc['choch']} warmup={t_info.get('warmup_status', 'READY')}",
                f"[{name}] swing_high={stc['last_swing_high']:.8f} swing_low={stc['last_swing_low']:.8f}",
            ]
    elif focus == "liquidity":
        for name in avail_tfs:
            liq = packet["smc_by_tf"][name]["liquidity"]
            lines += [
                f"[{name}] sweep_high={liq['sweep_high']} sweep_low={liq['sweep_low']}",
                f"[{name}] equal_high={liq['equal_high']} equal_low={liq['equal_low']}",
            ]
    elif focus == "smc":
        lines.append("=== ORDER BLOCK / FVG ===")
        lines.append(smc_text(smc))
        for z in smc["order_blocks"][:8]:
            lines.append(
                f"OB id={z.get('zone_id', '-')} {z['direction']} {z['low']:.8f}..{z['high']:.8f} state={z.get('state', 'FRESH')} age={z['age']} vol={z['volume_ratio']:.2f}"
            )
        for z in smc["fvgs"][:8]:
            lines.append(
                f"FVG id={z.get('zone_id', '-')} {z['direction']} {z['low']:.8f}..{z['high']:.8f} state={z.get('state', 'FRESH')} age={z['age']} filled={z['filled']}"
            )
        for htf in ("1h", "4h", "1d"):
            if htf in packet.get("smc_by_tf", {}):
                h_smc = packet["smc_by_tf"][htf]
                lines.append(
                    f"[{htf} SMC] bias={h_smc['structure']['bias']} timing={h_smc['timing']} score={h_smc['smc_score']}"
                )
    else:
        ob = packet["order_book"]
        for name in avail_tfs:
            x = tf[name]
            lines += [
                f"[{name}] RSI={x['rsi']:.2f} VOL_RATIO={x['vol_ratio']:.2f}x BUY_PRESSURE={x['buy_pressure']:.1%} EMA200_READY={x.get('ema200_ready', True)}",
                f"[{name}] MACD_HIST={x['macd_hist']:.8f} EMA20={x['ema20']:.8f} EMA50={x['ema50']:.8f} EMA200={x['ema200']:.8f}",
            ]
        lines += [
            f"Order imbalance={ob['imbalance']:+.2%}",
            f"Spread={ob['spread']:.3f}%",
            f"15m entry timing={smc['timing']} SMC score={smc['smc_score']}",
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
# V5.3 / V6 Hierarchical orchestration (PH04-02 Neutral Framing & Multi-Round Debate)
# -------------------------
ANALYSTS = list(NEUTRAL_ANALYSTS) if _agt_mod is not None else [
    ("🏗️ Structure Analyst", "Fokus market structure multi-timeframe: HH/HL/LH/LL, BOS, CHOCH, swing highs/lows, trend vs range."),
    ("💧 Liquidity Analyst", "Fokus liquidity pool, equal high/low, liquidity sweep, stop-run, false breakout, dan lokasi likuiditas."),
    ("🧱 Order Block & FVG Analyst", "Fokus Order Block, Fair Value Gap, displacement, freshness, confluence, kualitas zona, dan lokasi entry."),
    ("⚡ Execution Analyst", "Fokus volume, taker-buy pressure, order-book imbalance, ATR, RSI, EMA/MACD sebagai konfirmasi timing."),
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


def preflight_market_check(packet):
    """Deterministic preflight validation before spending AI tokens (PH01-03 / F38)."""
    veto_codes = []
    if not isinstance(packet, dict):
        return {"passed": False, "veto_codes": ["INSUFFICIENT_DATA"], "reason": "Packet pasar kosong/bukan dict."}

    tfs = packet.get("timeframes")
    smcs = packet.get("smc_by_tf")
    if not isinstance(tfs, dict) or not isinstance(smcs, dict):
        veto_codes.append("INSUFFICIENT_DATA")
    else:
        for req_tf in ("5m", "15m", "1h"):
            if req_tf not in tfs or req_tf not in smcs:
                veto_codes.append("INSUFFICIENT_DATA")
                break

    price = packet.get("price")
    support = packet.get("support")
    resistance = packet.get("resistance")
    ob = packet.get("order_book") if isinstance(packet.get("order_book"), dict) else {}
    spread = ob.get("spread", 0.0)
    tf15 = tfs.get("15m", {}) if isinstance(tfs, dict) else {}
    atr15 = tf15.get("atr")
    rsi15 = tf15.get("rsi")

    for val in (price, support, resistance, spread, atr15, rsi15):
        if not _is_finite_number(val):
            if "INVALID_NUMERIC" not in veto_codes:
                veto_codes.append("INVALID_NUMERIC")

    if "INVALID_NUMERIC" not in veto_codes:
        if float(price) <= 0 or float(atr15) <= 0 or float(support) <= 0 or float(resistance) <= 0 or float(support) > float(resistance):
            veto_codes.append("NON_POSITIVE_PRICE")
        if float(spread) < 0:
            veto_codes.append("INVALID_NUMERIC")
        elif float(spread) > float(MAX_SPREAD_PCT):
            veto_codes.append("SPREAD_TOO_WIDE")

    return {
        "passed": len(veto_codes) == 0,
        "veto_codes": veto_codes,
        "reason": "OK" if not veto_codes else f"Preflight veto: {', '.join(veto_codes)}",
    }


def _compute_candidate_trade_levels(packet, direction):
    """Calculate candidate entry, SL, TP, gross RR, and net RR after spread/fees for a proposed direction."""
    if direction not in {"LONG", "SHORT"}:
        return None, None, None, 0.0, 0.0

    current = packet.get("price")
    tf15 = packet.get("timeframes", {}).get("15m", {})
    atr15 = tf15.get("atr")
    if not _is_finite_number(current) or not _is_finite_number(atr15) or float(current) <= 0 or float(atr15) <= 0:
        return None, None, None, 0.0, 0.0

    current = float(current)
    atr15 = float(atr15)
    smc = packet.get("smc", {})

    if direction == "LONG":
        zone = smc.get("bullish_entry", {})
        z_low = zone.get("zone_low")
        sl_anchor = float(z_low) if _is_finite_number(z_low) and float(z_low) > 0 else current - atr15
        sl = min(current - 0.35 * atr15, sl_anchor - 0.10 * atr15)
        risk = max(current - sl, 0.25 * atr15)
        tp = current + 2.0 * risk
    else:
        zone = smc.get("bearish_entry", {})
        z_high = zone.get("zone_high")
        sl_anchor = float(z_high) if _is_finite_number(z_high) and float(z_high) > 0 else current + atr15
        sl = max(current + 0.35 * atr15, sl_anchor + 0.10 * atr15)
        risk = max(sl - current, 0.25 * atr15)
        tp = current - 2.0 * risk

    gross_rr = 2.0
    spread = float(packet.get("order_book", {}).get("spread", 0.0) or 0.0)
    cost_abs = current * ((max(0.0, spread) + max(0.0, float(ESTIMATED_ROUND_TRIP_COST_PCT))) / 100.0)
    net_reward = abs(tp - current) - cost_abs
    net_risk = abs(current - sl) + cost_abs
    net_rr = (net_reward / net_risk) if net_risk > 0 else 0.0
    return current, sl, tp, gross_rr, net_rr


def validate_trade_plan_gate(direction, entry, sl, tp, gross_rr=2.0, spread_pct=0.0):
    """Deterministic post-AI validation of entry/SL/TP ordering, finite positive numbers, and net R:R (F38)."""
    veto_codes = []
    if direction not in {"LONG", "SHORT"}:
        return {"valid": False, "veto_codes": [], "net_rr": 0.0}

    for v in (entry, sl, tp, gross_rr, spread_pct):
        if not _is_finite_number(v):
            veto_codes.append("INVALID_NUMERIC")
            return {"valid": False, "veto_codes": veto_codes, "net_rr": 0.0}

    entry_f, sl_f, tp_f = float(entry), float(sl), float(tp)
    if entry_f <= 0 or sl_f <= 0 or tp_f <= 0:
        veto_codes.append("NON_POSITIVE_PRICE")
        veto_codes.append("INVALID_SL_TP")
        return {"valid": False, "veto_codes": veto_codes, "net_rr": 0.0}

    if direction == "LONG" and not (sl_f < entry_f < tp_f):
        veto_codes.append("INVALID_SL_TP")
    elif direction == "SHORT" and not (tp_f < entry_f < sl_f):
        veto_codes.append("INVALID_SL_TP")

    cost_abs = entry_f * ((max(0.0, float(spread_pct)) + max(0.0, float(ESTIMATED_ROUND_TRIP_COST_PCT))) / 100.0)
    net_reward = abs(tp_f - entry_f) - cost_abs
    net_risk = abs(entry_f - sl_f) + cost_abs
    net_rr = (net_reward / net_risk) if net_risk > 0 else 0.0
    if net_rr < float(MIN_NET_RR):
        veto_codes.append("INSUFFICIENT_NET_RR")

    return {
        "valid": len(veto_codes) == 0,
        "veto_codes": veto_codes,
        "net_rr": round(float(net_rr), 4),
    }


def _extract_risk_vetoes(risk_agents, proposed_direction):
    """Detect explicit risk vetoes from Risk Manager agents (F16 / F38 / T05)."""
    vetoes = []
    if not isinstance(risk_agents, list):
        return vetoes
    for r in risk_agents:
        if not isinstance(r, dict):
            continue
        if r.get("veto") is True or r.get("veto_code"):
            code = str(r.get("veto_code") or "RISK_VETO").strip().upper()
            if code not in vetoes:
                vetoes.append(code)
            continue
        agent_name = str(r.get("agent", ""))
        r_dec = str(r.get("decision", "WAIT")).upper().strip()
        r_conf = float(r.get("confidence", 0.0) or 0.0) if _is_finite_number(r.get("confidence", 0.0)) else 0.0
        if "Conservative" in agent_name and proposed_direction in {"LONG", "SHORT"}:
            if (proposed_direction == "LONG" and r_dec == "SHORT") or (proposed_direction == "SHORT" and r_dec == "LONG"):
                if r_conf >= 70.0 and "RISK_VETO" not in vetoes:
                    vetoes.append("RISK_VETO")
    return vetoes


def is_timeframe_warmed_up_check(tf_info):
    if _dom_mod is not None:
        return _dom_mod.is_timeframe_warmed_up(tf_info)
    if not isinstance(tf_info, dict):
        return False, "MISSING_TIMEFRAME"
    if tf_info.get("ema200_ready") is False:
        return False, "PARTIAL_WARMUP_NO_EMA200"
    ws = str(tf_info.get("warmup_status", "READY") or "READY").upper()
    return (ws == "READY"), ws


def evaluate_mode_decisions(packet, raw_modes, gated_final, global_veto_codes=None, base_confidence=0.0):
    """Evaluate per-mode decisions separating directional_bias from entry_action and enforcing horizon/global/warmup gates (F16, F17, T05, T15)."""
    if _dom_mod is not None:
        return _dom_mod.evaluate_mode_decisions(
            packet,
            raw_modes,
            gated_final,
            global_veto_codes=global_veto_codes,
            base_confidence=base_confidence,
        )
    return {}


def _extract_closed_bar_timestamp(frames_or_snap, tf="15m"):
    """Extract deterministic closed bar timestamp from frames/snapshot for idempotency and pattern fingerprinting (PH01-04 / F22, F24)."""
    if isinstance(frames_or_snap, dict):
        if frames_or_snap.get("bar_time"):
            return str(frames_or_snap["bar_time"])
        if frames_or_snap.get("created_at"):
            return str(frames_or_snap["created_at"])[:19]
        frames = frames_or_snap.get("frames") if isinstance(frames_or_snap.get("frames"), dict) else frames_or_snap
        if isinstance(frames, dict):
            df = frames.get(tf)
            if df is None and "5m" in frames:
                df = frames.get("5m")
            if isinstance(df, pd.DataFrame) and len(df) > 0:
                closed = _closed_df(df)
                if len(closed) > 0:
                    return str(closed.index[-1])
                return str(df.index[-1])
        smc = frames_or_snap.get("smc") or frames_or_snap.get("packet", {}).get("smc")
        if isinstance(smc, dict):
            for z_list_key in ("order_blocks", "fvgs"):
                z_list = smc.get(z_list_key)
                if isinstance(z_list, list) and z_list and isinstance(z_list[0], dict) and z_list[0].get("created_at"):
                    return str(z_list[0]["created_at"])
        if frames_or_snap.get("analyzed_at"):
            return str(frames_or_snap["analyzed_at"])[:16]
    return "no_bar_time"


def compute_signal_idempotency_key(result, mode="scalping"):
    """Compute a durable setup idempotency key so manual UI and momentum monitor cannot save duplicate signals for the same bar/setup (PH01-04 / F24 / T16)."""
    if not isinstance(result, dict):
        return None
    if result.get("idempotency_key"):
        return str(result["idempotency_key"])
    symbol = str(result.get("symbol", "UNKNOWN")).upper().strip()
    final_dec = str(result.get("final", "WAIT")).upper().strip()
    mode_str = str(result.get("mode") or mode or "scalping").lower().strip()
    bar_ts = _extract_closed_bar_timestamp(result, tf="15m")
    entry_status = str(
        result.get("entry_status")
        or result.get("packet", {}).get("smc", {}).get("timing", "NONE")
    ).upper().strip()
    z_low = result.get("entry_zone_low")
    z_high = result.get("entry_zone_high")
    z_low_s = f"{float(z_low):.8f}" if _is_finite_number(z_low) else "none"
    z_high_s = f"{float(z_high):.8f}" if _is_finite_number(z_high) else "none"
    return f"sig|{symbol}|{mode_str}|{final_dec}|{bar_ts}|{entry_status}|{z_low_s}|{z_high_s}"


def run_multi_agent(
    symbol,
    scanner_score_value,
    debate_rounds=None,
    resume_analysis_id=None,
    max_parallel_workers=1,
    fail_after_node=None,
):
    is_resuming = bool(resume_analysis_id and str(resume_analysis_id).strip())
    analysis_id = (
        str(resume_analysis_id).strip()
        if is_resuming
        else f"ana_{symbol}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    )
    analyzed_at = datetime.now(timezone.utc).isoformat()
    candidate_deadline = time.time() + float(AI_CANDIDATE_DEADLINE_SECONDS)
    num_debate_rounds = max(1, min(2, int(debate_rounds if debate_rounds is not None else DEFAULT_DEBATE_ROUNDS)))
    physical_cap = _resolve_physical_cap()

    existing_ckpts = (
        _stg_mod.load_dag_checkpoints(analysis_id, db_file=DB_FILE)
        if (_stg_mod is not None and is_resuming)
        else {}
    )
    resumed_ai_nodes_count = sum(
        1
        for nid, cinfo in existing_ckpts.items()
        if nid not in {"preflight_gate", "post_ai_gate"}
        and str(cinfo.get("status", "")).upper() in {"COMPLETED", "OK"}
    )

    st.session_state["_ai_calls_current_candidate"] = resumed_ai_nodes_count
    st.session_state["_ai_physical_attempts_current_candidate"] = resumed_ai_nodes_count
    st.session_state["_ai_candidate_deadline"] = candidate_deadline
    reset_quota_scope_budget(
        scope_id="groq_default",
        max_physical_attempts=physical_cap,
        deadline_epoch=candidate_deadline,
    )
    with _QUOTA_SCOPE_LOCK:
        if "groq_default" in _QUOTA_SCOPE_BUDGETS:
            _QUOTA_SCOPE_BUDGETS["groq_default"]["physical_used"] = resumed_ai_nodes_count
            _QUOTA_SCOPE_BUDGETS["groq_default"]["logical_used"] = resumed_ai_nodes_count

    frames, packet = market_packet(symbol)
    bar_time_15m = _extract_closed_bar_timestamp({"frames": frames, "packet": packet}, tf="15m")
    data = packet_text(packet)
    _extra_s = list(GROQ_KEYS) + ([TG_TOKEN] if TG_TOKEN else [])
    durable_mgr = GLOBAL_DURABLE_BUDGET_MANAGER if _stg_mod is not None else None

    def _invoke_ai(role_name, prompt_data, instructions_text, tier="analyst", budget=None):
        try:
            return call_ai(
                role_name,
                prompt_data,
                instructions_text,
                tier=tier,
                budget=budget,
                analysis_id=analysis_id,
            )
        except TypeError:
            return call_ai(
                role_name,
                prompt_data,
                instructions_text,
                tier=tier,
                budget=budget,
            )

    analyst_meta = {
        "analyst_structure": ("🏗️ Structure Analyst", "structure", ANALYSTS[0][1]),
        "analyst_liquidity": ("💧 Liquidity Analyst", "liquidity", ANALYSTS[1][1]),
        "analyst_smc": ("🧱 Order Block & FVG Analyst", "smc", ANALYSTS[2][1]),
        "analyst_execution": ("⚡ Execution Analyst", "execution", ANALYSTS[3][1]),
    }

    risk_meta = {
        "risk_aggressive": (
            "🟢 Aggressive Risk Manager",
            "Toleransi risiko lebih tinggi; evaluasi peluang LONG maupun SHORT tetapi tetap cek invalidation.",
        ),
        "risk_neutral": (
            "🟡 Neutral Risk Manager",
            "Seimbangkan peluang vs risiko secara netral dan validasi setup LONG/SHORT.",
        ),
        "risk_conservative": (
            "🔴 Conservative Risk Manager",
            "Prioritaskan perlindungan modal, spread, volatilitas, dan false breakout.",
        ),
    }

    def _run_preflight_node(_upstream):
        pf = preflight_market_check(packet)
        if not pf["passed"]:
            return {
                "passed": False,
                "stage": "PREFLIGHT",
                "veto_codes": list(pf["veto_codes"]),
                "reason": pf["reason"],
                "budget_exhausted": False,
            }
        if durable_mgr is not None:
            judge_budget = MODEL_TIERS.get("judge", {}).get("max_tokens", JUDGE_MAX_TOKENS)
            ok_pf, pf_reason, judge_res_id = durable_mgr.preflight_reserve_judge(
                analysis_id=analysis_id,
                scope_id="groq_default",
                judge_budget=judge_budget,
                max_physical_attempts=physical_cap,
                deadline_epoch=candidate_deadline,
                db_file=DB_FILE,
            )
            if not ok_pf:
                return {
                    "passed": False,
                    "stage": "PREFLIGHT_BUDGET",
                    "veto_codes": [pf_reason.split(":")[0]],
                    "reason": pf_reason,
                    "budget_exhausted": True,
                }
            return {
                "passed": True,
                "stage": "PREFLIGHT",
                "veto_codes": [],
                "reason": "OK",
                "judge_reservation_id": judge_res_id,
            }
        return {
            "passed": True,
            "stage": "PREFLIGHT",
            "veto_codes": [],
            "reason": "OK",
        }

    def _make_analyst_runner(nid):
        role_name, focus_key, rule_text = analyst_meta[nid]
        def _runner(_upstream):
            raw_an = _invoke_ai(
                role_name,
                specialist_data(packet, focus_key),
                rule_text + " Berikan thesis mandiri secara netral (LONG, SHORT, atau WAIT) untuk diteruskan ke Bull dan Bear.",
                tier="analyst",
            )
            safe_an = (
                _agt_mod.sanitize_agent_output_for_downstream(raw_an, extra_secrets=_extra_s)
                if _agt_mod is not None
                else raw_an
            )
            return {"agent": role_name, **safe_an}
        return _runner

    def _collect_analysts(upstream):
        return [upstream[nid] for nid in ("analyst_structure", "analyst_liquidity", "analyst_smc", "analyst_execution") if nid in upstream]

    def _run_bull_r1(upstream):
        a_summary = analyst_summary_text(_collect_analysts(upstream))
        d_data = data + "\n\nRINGKASAN ANALYST:\n" + a_summary
        inst = (
            _agt_mod.build_bull_instructions(round_num=1, total_rounds=num_debate_rounds)
            if _agt_mod is not None
            else "Bangun kasus LONG paling kuat secara netral."
        )
        raw_b = _invoke_ai("🐂 Bull Researcher", d_data, inst, tier="debate")
        return _agt_mod.sanitize_agent_output_for_downstream(raw_b, extra_secrets=_extra_s) if _agt_mod is not None else raw_b

    def _run_bear_r1(upstream):
        a_summary = analyst_summary_text(_collect_analysts(upstream))
        d_data = data + "\n\nRINGKASAN ANALYST:\n" + a_summary
        bull_r1 = upstream["debate_bull_r1"]
        bear_r1_data = (
            d_data
            + "\n\nARGUMEN BULL (PUTARAN 1) UNTUK DIUJI SECARA KRITIS:\n"
            + compact_json(bull_r1, 1100)
        )
        inst = (
            _agt_mod.build_bear_instructions(round_num=1, total_rounds=num_debate_rounds)
            if _agt_mod is not None
            else "Serang thesis Bull secara spesifik dan bangun tesis SHORT mandiri."
        )
        raw_br = _invoke_ai("🐻 Bear Researcher", bear_r1_data, inst, tier="debate")
        return _agt_mod.sanitize_agent_output_for_downstream(raw_br, extra_secrets=_extra_s) if _agt_mod is not None else raw_br

    def _run_bull_r2(upstream):
        a_summary = analyst_summary_text(_collect_analysts(upstream))
        d_data = data + "\n\nRINGKASAN ANALYST:\n" + a_summary
        bull_r1 = upstream["debate_bull_r1"]
        bear_r1 = upstream["debate_bear_r1"]
        bull_r2_data = (
            d_data
            + "\n\nARGUMEN BULL (PUTARAN 1):\n"
            + compact_json(bull_r1, 900)
            + "\n\nKRITIK & TESIS BEAR (PUTARAN 1) YANG HARUS DITANGGAPI:\n"
            + compact_json(bear_r1, 900)
        )
        inst = _agt_mod.build_bull_instructions(round_num=2, total_rounds=num_debate_rounds)
        raw_b2 = _invoke_ai("🐂 Bull Researcher (Rebuttal)", bull_r2_data, inst, tier="debate")
        return _agt_mod.sanitize_agent_output_for_downstream(raw_b2, extra_secrets=_extra_s) if _agt_mod is not None else raw_b2

    def _run_bear_r2(upstream):
        a_summary = analyst_summary_text(_collect_analysts(upstream))
        d_data = data + "\n\nRINGKASAN ANALYST:\n" + a_summary
        bear_r1 = upstream["debate_bear_r1"]
        bull_r2 = upstream["debate_bull_r2"]
        bear_r2_data = (
            d_data
            + "\n\nTESIS BEAR (PUTARAN 1):\n"
            + compact_json(bear_r1, 900)
            + "\n\nREBUTTAL BULL (PUTARAN 2) YANG HARUS DITANGGAPI:\n"
            + compact_json(bull_r2, 900)
        )
        inst = _agt_mod.build_bear_instructions(round_num=2, total_rounds=num_debate_rounds)
        raw_br2 = _invoke_ai("🐻 Bear Researcher (Rebuttal)", bear_r2_data, inst, tier="debate")
        return _agt_mod.sanitize_agent_output_for_downstream(raw_br2, extra_secrets=_extra_s) if _agt_mod is not None else raw_br2

    def _reconstruct_debate(upstream):
        rounds_hist = [
            {
                "round": 1,
                "bull": upstream["debate_bull_r1"],
                "bear": upstream["debate_bear_r1"],
            }
        ]
        if num_debate_rounds >= 2 and "debate_bull_r2" in upstream and "debate_bear_r2" in upstream:
            rounds_hist.append({
                "round": 2,
                "bull": upstream["debate_bull_r2"],
                "bear": upstream["debate_bear_r2"],
            })
        transcript = (
            _agt_mod.format_debate_transcript(rounds_hist)
            if _agt_mod is not None
            else debate_summary_text(rounds_hist[-1]["bull"], rounds_hist[-1]["bear"])
        )
        return rounds_hist[-1]["bull"], rounds_hist[-1]["bear"], rounds_hist, transcript

    def _run_research_manager(upstream):
        a_summary = analyst_summary_text(_collect_analysts(upstream))
        _, _, _, debate_txt = _reconstruct_debate(upstream)
        r_input = (
            data
            + "\n\nANALYST SUMMARY:\n" + a_summary
            + "\n\nBULL vs BEAR:\n" + debate_txt
            + "\n\nMEMORY HISTORIS PAPER TRADE:\n" + memory_context(symbol)
        )
        raw_rm = _invoke_ai(
            "🧠 Research Manager",
            r_input,
            "Sintesis bukti terbaik secara netral untuk peluang LONG maupun SHORT. Nilai structure, liquidity, OB/FVG, displacement, konflik timeframe, kualitas Bull vs Bear, dan bedakan SIGNAL dari ENTRY. Tetapkan kondisi yang wajib terpenuhi sebelum entry dan tolak setup jika timing sudah terlambat.",
            tier="research",
        )
        return _agt_mod.sanitize_agent_output_for_downstream(raw_rm, extra_secrets=_extra_s) if _agt_mod is not None else raw_rm

    def _run_trader(upstream):
        _, _, _, debate_txt = _reconstruct_debate(upstream)
        rm = upstream["research_manager"]
        t_input = (
            f"Market: {packet['symbol']} price={packet['price']:.8f}, ATR15={packet['timeframes']['15m']['atr']:.8f}, "
            f"support={packet['support']:.8f}, resistance={packet['resistance']:.8f}, regime={packet['market_regime']}\n"
            f"SMC: {smc_text(packet['smc'])}\n"
            f"Research: {compact_json(rm)}\n"
            f"Bull/Bear: {debate_txt}"
        )
        raw_tr = _invoke_ai(
            "💹 Trader",
            t_input,
            "Ubah thesis menjadi rencana paper-trade LONG/SHORT/WAIT secara netral. Fokus SIGNAL vs ENTRY, entry zone OB/FVG bullish atau bearish, retest, trigger displacement/liquidity, invalidation, dan apakah harga sudah terlambat. Jika belum berada di zona/trigger, pilih WAIT. Jangan menjadi final judge.",
            tier="analyst",
            budget=TRADER_MAX_TOKENS,
        )
        return _agt_mod.sanitize_agent_output_for_downstream(raw_tr, extra_secrets=_extra_s) if _agt_mod is not None else raw_tr

    def _make_risk_runner(nid):
        r_name, r_rule = risk_meta[nid]
        def _runner(upstream):
            rm = upstream["research_manager"]
            tr = upstream["trader_plan"]
            r_input = (
                f"Symbol={symbol}; price={packet['price']:.8f}; regime={packet['market_regime']}; "
                f"spread={packet['order_book']['spread']:.3f}%; imbalance={packet['order_book']['imbalance']:+.2%}; "
                f"support={packet['support']:.8f}; resistance={packet['resistance']:.8f}; "
                f"SMC={smc_text(packet['smc'])}; "
                f"Research={compact_json(rm)}; Trader={compact_json(tr)}"
            )
            raw_rk = _invoke_ai(
                r_name,
                r_input,
                r_rule + " Boleh menolak rencana Trader jika risiko tidak layak.",
                tier="analyst",
                budget=RISK_MAX_TOKENS,
            )
            safe_rk = (
                _agt_mod.sanitize_agent_output_for_downstream(raw_rk, extra_secrets=_extra_s)
                if _agt_mod is not None
                else raw_rk
            )
            return {"agent": r_name, **safe_rk}
        return _runner

    def _collect_risks(upstream):
        return [upstream[nid] for nid in ("risk_aggressive", "risk_neutral", "risk_conservative") if nid in upstream]

    def _run_final_judge(upstream):
        a_summary = analyst_summary_text(_collect_analysts(upstream))
        _, _, _, debate_txt = _reconstruct_debate(upstream)
        rm = upstream["research_manager"]
        tr = upstream["trader_plan"]
        r_summary = risk_summary_text(_collect_risks(upstream))
        j_input = (
            f"MARKET: {symbol} price={packet['price']:.8f} regime={packet['market_regime']} "
            f"support={packet['support']:.8f} resistance={packet['resistance']:.8f} "
            f"spread={packet['order_book']['spread']:.3f}% imbalance={packet['order_book']['imbalance']:+.2%}\n"
            f"SMC / ENTRY TIMING:\n{smc_text(packet['smc'])}\n"
            f"ANALYSTS:\n{a_summary}\n"
            f"BULL/BEAR:\n{debate_txt}\n"
            f"RESEARCH:\n{compact_json(rm)}\n"
            f"TRADER:\n{compact_json(tr)}\n"
            f"RISK:\n{r_summary}\n"
            f"MEMORY: {memory_context(symbol, 3)}"
        )
        return _invoke_ai(
            "👨‍⚖️ Final Judge",
            j_input,
            "Ambil keputusan final LONG/SHORT/WAIT secara netral tanpa bias arah bawaan. Jangan voting mayoritas secara buta. Prioritaskan structure, liquidity, OB/FVG quality, displacement, entry zone, entry timing, konflik timeframe, Bull vs Bear, research, trader plan, risk team, spread, regime, dan risk/reward. SIGNAL boleh benar tetapi ENTRY harus tetap WAIT jika belum retest/trigger atau sudah LATE. Selain keputusan utama, WAJIB isi modes untuk tiga gaya: scalping memakai 1H→15M→5M, intraday memakai 4H→1H→15M, dan swing memakai 1D→4H→1H (jika timeframe 4H/1D tidak tersedia atau belum warm-up, tetapkan decision mode tersebut menjadi WAIT). Jangan mengarang timeframe yang tidak diberikan.",
            tier="judge",
        )

    def _run_post_ai_gate(upstream):
        portfolio_node = upstream["final_judge"]
        analyst_list = _collect_analysts(upstream)
        risk_list = _collect_risks(upstream)

        raw_model_dec = str(portfolio_node.get("decision", "WAIT")).upper().strip()
        if raw_model_dec not in _ALLOWED_DECISIONS:
            raw_model_dec = "WAIT"

        final_dec = raw_model_dec
        v_codes = []

        if portfolio_node.get("status", "OK") != "OK":
            v_codes.append(str(portfolio_node.get("status", "INVALID_AI_OUTPUT")))

        timing_val = packet["smc"]["timing"]
        if raw_model_dec in {"LONG", "SHORT"}:
            if timing_val == "LATE":
                v_codes.append("LATE_ENTRY")
            elif timing_val == "WAIT_RETEST":
                v_codes.append("WAIT_RETEST")
            elif timing_val == "READY_LONG" and raw_model_dec == "SHORT":
                v_codes.append("TIMEFRAME_CONFLICT")
            elif timing_val == "READY_SHORT" and raw_model_dec == "LONG":
                v_codes.append("TIMEFRAME_CONFLICT")

        analyst_dir = sum(
            1 if (x.get("status", "OK") == "OK" and x.get("decision") == "LONG")
            else -1 if (x.get("status", "OK") == "OK" and x.get("decision") == "SHORT")
            else 0
            for x in analyst_list
        )
        risk_dir = sum(
            1 if (x.get("status", "OK") == "OK" and x.get("decision") == "LONG")
            else -1 if (x.get("status", "OK") == "OK" and x.get("decision") == "SHORT")
            else 0
            for x in risk_list
        )
        j_conf = (
            float(portfolio_node.get("confidence", 0.0) or 0.0)
            if _is_valid_finite_confidence(portfolio_node.get("confidence", 0.0))
            else 0.0
        )

        if raw_model_dec in {"LONG", "SHORT"} and j_conf < float(MIN_JUDGE_CONFIDENCE):
            v_codes.append("LOW_CONFIDENCE")
        elif raw_model_dec in {"LONG", "SHORT"}:
            support_dir = analyst_dir + risk_dir
            if raw_model_dec == "LONG" and support_dir <= -3:
                v_codes.append("ANALYST_RISK_OPPOSITION")
            elif raw_model_dec == "SHORT" and support_dir >= 3:
                v_codes.append("ANALYST_RISK_OPPOSITION")

        for r_veto in _extract_risk_vetoes(risk_list, raw_model_dec):
            if r_veto not in v_codes:
                v_codes.append(r_veto)

        c_entry, c_sl, c_tp, c_rr, c_net_rr = _compute_candidate_trade_levels(packet, raw_model_dec)
        tp_bundle = None
        if _risk_mod is not None:
            tp_bundle = _risk_mod.build_trade_plan_and_sizing(
                packet,
                raw_model_dec,
                equity=PAPER_INITIAL_EQUITY,
                risk_fraction=PAPER_RISK_FRACTION,
                fee_pct_round_trip=PAPER_FEE_PCT_ROUND_TRIP,
                slippage_pct_round_trip=PAPER_SLIPPAGE_PCT_ROUND_TRIP,
                max_position_exposure_pct=PAPER_MAX_POSITION_EXPOSURE_PCT,
                max_total_exposure_pct=PAPER_MAX_TOTAL_EXPOSURE_PCT,
                max_total_risk_fraction=PAPER_MAX_TOTAL_RISK_FRACTION,
                max_open_positions=PAPER_MAX_OPEN_POSITIONS,
                min_net_rr=MIN_NET_RR,
                instrument_mode=PAPER_INSTRUMENT_MODE,
            )
        if raw_model_dec in {"LONG", "SHORT"}:
            p_gate = validate_trade_plan_gate(
                raw_model_dec,
                c_entry,
                c_sl,
                c_tp,
                gross_rr=c_rr,
                spread_pct=packet.get("order_book", {}).get("spread", 0.0),
            )
            for pv in p_gate["veto_codes"]:
                if pv not in v_codes:
                    v_codes.append(pv)
            if tp_bundle is not None:
                for rv in tp_bundle.get("veto_codes", []):
                    if rv not in {"WAIT_DECISION"} and rv not in v_codes:
                        v_codes.append(rv)
                c_net_rr = tp_bundle["net_rr"]
            else:
                c_net_rr = p_gate["net_rr"]

        if v_codes:
            final_dec = "WAIT"

        if final_dec in {"LONG", "SHORT"}:
            entry_val, sl_val, tp_val, rr_val = c_entry, c_sl, c_tp, c_rr
            pos_size = tp_bundle["position_size"] if tp_bundle is not None else 0.0
            notional_val = tp_bundle["notional_value"] if tp_bundle is not None else 0.0
            risk_budget_val = tp_bundle["risk_budget"] if tp_bundle is not None else round(PAPER_INITIAL_EQUITY * PAPER_RISK_FRACTION, 4)
            net_rr_val = round(float(c_net_rr), 4) if _is_finite_number(c_net_rr) else 0.0
            inst_dict = tp_bundle["instrument"] if tp_bundle is not None else {}
            sizing_dict = tp_bundle["sizing"] if tp_bundle is not None else {}
        else:
            entry_val, sl_val, tp_val, rr_val = None, None, None, 0.0
            pos_size = 0.0
            notional_val = 0.0
            risk_budget_val = round(PAPER_INITIAL_EQUITY * PAPER_RISK_FRACTION, 4)
            net_rr_val = 0.0
            inst_dict = (
                _risk_mod.resolve_instrument_semantics("WAIT", instrument_mode=PAPER_INSTRUMENT_MODE).to_dict()
                if _risk_mod is not None
                else {}
            )
            sizing_dict = tp_bundle["sizing"] if tp_bundle is not None else {}

        v_summary = ", ".join(v_codes) if v_codes else "NONE"
        g_report = {
            "approved": final_dec in {"LONG", "SHORT"} and len(v_codes) == 0,
            "stage": "POST_AI",
            "raw_model_decision": raw_model_dec,
            "gated_decision": final_dec,
            "veto_codes": v_codes,
            "veto_summary": v_summary,
            "gross_rr": rr_val,
            "net_rr": round(float(c_net_rr), 4) if _is_finite_number(c_net_rr) else 0.0,
            "risk_budget": risk_budget_val,
            "position_size": pos_size,
            "notional_value": notional_val,
            "instrument_type": inst_dict.get("instrument_type", "NO_POSITION"),
        }

        global_only_vetoes = [
            vc for vc in v_codes
            if vc not in {"WAIT_RETEST", "LATE_ENTRY", "TIMEFRAME_CONFLICT"}
        ]
        m_decisions = evaluate_mode_decisions(
            packet,
            portfolio_node.get("modes") or {},
            final_dec,
            global_veto_codes=global_only_vetoes,
            base_confidence=j_conf,
        )

        return {
            "status": "OK",
            "raw_model_decision": raw_model_dec,
            "final": final_dec,
            "veto_codes": v_codes,
            "veto_summary": v_summary,
            "gate_report": g_report,
            "modes": m_decisions,
            "entry": entry_val,
            "sl": sl_val,
            "tp": tp_val,
            "rr": rr_val,
            "gross_rr": rr_val,
            "net_rr": net_rr_val,
            "risk_budget": risk_budget_val,
            "position_size": pos_size,
            "notional_value": notional_val,
            "instrument": inst_dict,
            "instrument_type": inst_dict.get("instrument_type", "NO_POSITION"),
            "trade_plan": sizing_dict,
            "timing": timing_val,
        }

    node_runners = {
        "preflight_gate": _run_preflight_node,
        "analyst_structure": _make_analyst_runner("analyst_structure"),
        "analyst_liquidity": _make_analyst_runner("analyst_liquidity"),
        "analyst_smc": _make_analyst_runner("analyst_smc"),
        "analyst_execution": _make_analyst_runner("analyst_execution"),
        "debate_bull_r1": _run_bull_r1,
        "debate_bear_r1": _run_bear_r1,
        "research_manager": _run_research_manager,
        "trader_plan": _run_trader,
        "risk_aggressive": _make_risk_runner("risk_aggressive"),
        "risk_neutral": _make_risk_runner("risk_neutral"),
        "risk_conservative": _make_risk_runner("risk_conservative"),
        "final_judge": _run_final_judge,
        "post_ai_gate": _run_post_ai_gate,
    }
    if num_debate_rounds >= 2:
        node_runners["debate_bull_r2"] = _run_bull_r2
        node_runners["debate_bear_r2"] = _run_bear_r2

    def _skip_node_factory(node_spec, reason_str):
        base_skip = {
            "status": "SKIPPED",
            "decision": "WAIT",
            "confidence": 0.0,
            "reasoning": f"Preflight gate menolak setup sebelum AI dipanggil: {reason_str}.",
            "key_risk": reason_str,
            "evidence": [],
            "modes": {},
            "tier": node_spec.tier if node_spec.tier in MODEL_TIERS else "analyst",
            "provider": "none",
            "model": "",
            "finish_reason": None,
            "usage": _extract_usage(None),
            "requested_max_tokens": 0,
            "effective_max_tokens": 0,
        }
        if node_spec.node_id in analyst_meta or node_spec.node_id in risk_meta:
            return {"agent": node_spec.role, **base_skip}
        return base_skip

    orchestrator = _agt_mod.AnalysisDAGOrchestrator(
        debate_rounds=num_debate_rounds,
        checkpoint_loader=(
            (lambda aid: _stg_mod.load_dag_checkpoints(aid, db_file=DB_FILE))
            if _stg_mod is not None
            else None
        ),
        checkpoint_saver=(
            (lambda **kw: _stg_mod.save_dag_checkpoint(db_file=DB_FILE, extra_secrets=_extra_s, **kw))
            if _stg_mod is not None
            else None
        ),
        max_workers=max_parallel_workers,
        fail_after_node=fail_after_node,
    )

    dag_exec = orchestrator.execute(
        analysis_id,
        node_runners,
        skip_factory=_skip_node_factory,
    )
    outputs = dag_exec["outputs"]
    dag_report = dag_exec["dag_report"]

    pf_out = outputs["preflight_gate"]
    if not pf_out.get("passed", False):
        veto_codes = list(pf_out.get("veto_codes") or [])
        veto_summary = ", ".join(veto_codes)
        is_budget_pf = bool(pf_out.get("budget_exhausted", False))
        stage_name = "PREFLIGHT_BUDGET" if is_budget_pf else "PREFLIGHT"
        status_label = "BUDGET_EXHAUSTED" if is_budget_pf else "SKIPPED"
        preflight_msg = (
            f"Preflight Final Judge reservation gagal: {pf_out.get('reason')}"
            if is_budget_pf
            else f"Preflight gate menolak setup sebelum AI dipanggil: {veto_summary}."
        )
        skipped_agent = {
            "status": status_label,
            "decision": "WAIT",
            "confidence": 0.0,
            "reasoning": preflight_msg,
            "key_risk": str(pf_out.get("reason") or veto_summary),
            "evidence": [],
            "modes": {},
            "tier": "analyst",
            "provider": "none",
            "model": "",
            "finish_reason": None,
            "usage": _extract_usage(None),
            "requested_max_tokens": 0,
            "effective_max_tokens": 0,
        }
        analyst_results = [{"agent": name, **copy.deepcopy(skipped_agent)} for name, _ in ANALYSTS]
        risk_agents = [
            {"agent": "🟢 Aggressive Risk Manager", **copy.deepcopy(skipped_agent)},
            {"agent": "🟡 Neutral Risk Manager", **copy.deepcopy(skipped_agent)},
            {"agent": "🔴 Conservative Risk Manager", **copy.deepcopy(skipped_agent)},
        ]
        portfolio = {
            **copy.deepcopy(skipped_agent),
            "tier": "judge",
            "reasoning": preflight_msg,
        }
        mode_decisions = (
            {}
            if is_budget_pf
            else evaluate_mode_decisions(
                packet,
                {},
                "WAIT",
                global_veto_codes=veto_codes,
                base_confidence=0.0,
            )
        )
        gate_report = {
            "approved": False,
            "stage": stage_name,
            "raw_model_decision": "WAIT",
            "gated_decision": "WAIT",
            "veto_codes": veto_codes,
            "veto_summary": pf_out.get("reason") if is_budget_pf else veto_summary,
            "net_rr": 0.0,
        }
        out_res = {
            "analysis_id": analysis_id,
            "analyzed_at": analyzed_at,
            "bar_time": bar_time_15m,
            "symbol": symbol,
            "frames": frames,
            "packet": packet,
            "analysts": analyst_results,
            "bull": copy.deepcopy(skipped_agent),
            "bear": copy.deepcopy(skipped_agent),
            "research_manager": copy.deepcopy(skipped_agent),
            "trader": copy.deepcopy(skipped_agent),
            "risk_agents": risk_agents,
            "portfolio": portfolio,
            "modes": mode_decisions,
            "raw_model_decision": "WAIT",
            "final": "WAIT",
            "gate_report": gate_report,
            "veto_reasons": pf_out.get("reason") if is_budget_pf else veto_summary,
            "accounting_mode": LEGACY_ACCOUNTING_MODE,
            "scanner_score": scanner_score_value,
            "entry": None,
            "sl": None,
            "tp": None,
            "rr": 0.0,
            "gross_rr": 0.0,
            "net_rr": 0.0,
            "risk_budget": round(PAPER_INITIAL_EQUITY * PAPER_RISK_FRACTION, 4),
            "position_size": 0.0,
            "quantity": 0.0,
            "notional_value": 0.0,
            "instrument_type": "NO_POSITION",
            "entry_status": packet.get("smc", {}).get("timing", "WAIT_RETEST"),
            "entry_zone_low": None,
            "entry_zone_high": None,
            "smc_score": packet.get("smc", {}).get("smc_score", 0),
            "smc_bias": packet.get("smc", {}).get("structure", {}).get("bias", "RANGE"),
            "model_tiers": MODEL_TIERS,
            "dag_report": dag_report,
            "budget_summary": (
                {"preflight_rejected": True, "reason": pf_out.get("reason")}
                if is_budget_pf
                else {
                    "logical_calls_used": 0,
                    "physical_attempts_used": 0,
                    "physical_attempts_cap": physical_cap,
                    "min_completion_tokens": MIN_COMPLETION_TOKENS,
                }
            ),
        }
        out_res["idempotency_key"] = compute_signal_idempotency_key(out_res)
        return out_res

    analyst_results = _collect_analysts(outputs)
    bull, bear, debate_rounds_history, debate = _reconstruct_debate(outputs)
    research_manager = outputs["research_manager"]
    trader = outputs["trader_plan"]
    risk_agents = _collect_risks(outputs)
    portfolio = outputs["final_judge"]
    post_gate = outputs["post_ai_gate"]

    final = post_gate["final"]
    raw_model_decision = post_gate["raw_model_decision"]
    gate_report = post_gate["gate_report"]
    veto_summary = post_gate["veto_summary"]
    mode_decisions = post_gate["modes"]
    entry_val = post_gate["entry"]
    sl = post_gate["sl"]
    tp = post_gate["tp"]
    rr = post_gate["rr"]
    net_rr_val = post_gate["net_rr"]
    risk_budget_val = post_gate["risk_budget"]
    pos_size = post_gate["position_size"]
    notional_val = post_gate["notional_value"]
    inst_dict = post_gate["instrument"]
    sizing_dict = post_gate["trade_plan"]
    timing = post_gate["timing"]

    out_res = {
        "analysis_id": analysis_id,
        "analyzed_at": analyzed_at,
        "bar_time": bar_time_15m,
        "symbol": symbol,
        "frames": frames,
        "packet": packet,
        "analysts": analyst_results,
        "bull": bull,
        "bear": bear,
        "debate_rounds": debate_rounds_history,
        "debate_rounds_count": len(debate_rounds_history),
        "debate_transcript": debate,
        "research_manager": research_manager,
        "trader": trader,
        "risk_agents": risk_agents,
        "portfolio": portfolio,
        "modes": mode_decisions,
        "raw_model_decision": raw_model_decision,
        "final": final,
        "gate_report": gate_report,
        "veto_reasons": veto_summary,
        "accounting_mode": LEGACY_ACCOUNTING_MODE,
        "scanner_score": scanner_score_value,
        "entry": entry_val,
        "sl": sl,
        "tp": tp,
        "rr": rr,
        "gross_rr": rr,
        "net_rr": net_rr_val,
        "risk_budget": risk_budget_val,
        "position_size": pos_size,
        "quantity": pos_size,
        "notional_value": notional_val,
        "instrument": inst_dict,
        "instrument_type": inst_dict.get("instrument_type", "NO_POSITION"),
        "trade_plan": sizing_dict,
        "entry_status": timing,
        "entry_zone_low": (
            packet["smc"]["bullish_entry"]["zone_low"] if final == "LONG"
            else packet["smc"]["bearish_entry"]["zone_low"] if final == "SHORT"
            else None
        ),
        "entry_zone_high": (
            packet["smc"]["bullish_entry"]["zone_high"] if final == "LONG"
            else packet["smc"]["bearish_entry"]["zone_high"] if final == "SHORT"
            else None
        ),
        "smc_score": packet["smc"]["smc_score"],
        "smc_bias": packet["smc"]["structure"]["bias"],
        "model_tiers": MODEL_TIERS,
        "dag_report": dag_report,
        "budget_summary": {
            "logical_calls_used": int(st.session_state.get("_ai_calls_current_candidate", 0) or 0),
            "physical_attempts_used": int(st.session_state.get("_ai_physical_attempts_current_candidate", 0) or 0),
            "physical_attempts_cap": _resolve_physical_cap(),
            "min_completion_tokens": MIN_COMPLETION_TOKENS,
            "resumed_nodes_count": len(dag_report.get("nodes_resumed") or []),
            "executed_nodes_count": len(dag_report.get("nodes_executed") or []),
        },
    }
    out_res["idempotency_key"] = compute_signal_idempotency_key(out_res)
    if _agt_mod is not None:
        cons_report = _agt_mod.compute_directional_consensus(out_res)
        out_res["consensus_report"] = cons_report
        out_res["score_consensus"] = cons_report["score_consensus"]
    else:
        out_res["score_consensus"] = compute_consensus_score(out_res) if _stg_mod is not None else None
    out_res["workflow_status"] = (
        _agt_mod.build_workflow_stage_status(out_res, fingerprint=out_res["idempotency_key"])
        if _agt_mod is not None
        else {}
    )
    out_res["strategy_version"] = STRATEGY_VERSION if _cfg_mod is not None else "v5.6-ph02"
    out_res["prompt_version"] = PROMPT_VERSION if _cfg_mod is not None else "v5.6-id-strict-1"
    out_res["schema_version"] = SCHEMA_VERSION_AI if _cfg_mod is not None else "v5.6-json-1"
    out_res["model_config_hash"] = compute_model_config_hash(MODEL_TIERS) if _cfg_mod is not None else ""
    out_res["origin"] = "v6_runtime"
    out_res["accounting_quality"] = "legacy_r_snapshot"
    return out_res

# -------------------------
# Memory / paper trading
# -------------------------
def _clone_analysis_snapshot(result):
    """Create an immutable-style snapshot copy of an analysis result while keeping DataFrame frames intact."""
    snap = {}
    for k, v in result.items():
        if k == "frames":
            snap[k] = dict(v) if isinstance(v, dict) else v
        else:
            snap[k] = copy.deepcopy(v)
    if not snap.get("analysis_id"):
        snap["analysis_id"] = f"ana_{snap.get('symbol', 'UNKNOWN')}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    if not snap.get("analyzed_at"):
        snap["analyzed_at"] = datetime.now(timezone.utc).isoformat()
    if not snap.get("idempotency_key"):
        snap["idempotency_key"] = compute_signal_idempotency_key(snap)
    return snap


def ensure_analysis_state(state=None):
    """Initialize session_state keys required for decoupled analysis and save."""
    target = state if state is not None else st.session_state
    if "scan" not in target:
        target["scan"] = []
    if "last_result" not in target:
        target["last_result"] = None
    if "analyses_by_id" not in target:
        target["analyses_by_id"] = {}
    if "candidate_analysis_ids" not in target:
        target["candidate_analysis_ids"] = {}
    if "saved_analysis_ids" not in target:
        target["saved_analysis_ids"] = {}
    if "notified_analysis_ids" not in target:
        target["notified_analysis_ids"] = set()
    if "v56_processed_fingerprints" not in target:
        target["v56_processed_fingerprints"] = set()
    return target


def store_analysis_result(result, state=None):
    """Store an analysis result under its unique analysis_id and bind it to its symbol."""
    target = ensure_analysis_state(state)
    snap = _clone_analysis_snapshot(result)
    aid = snap["analysis_id"]
    target["analyses_by_id"][aid] = snap
    target["candidate_analysis_ids"][snap["symbol"]] = aid
    target["last_result"] = snap
    return snap


def get_rendered_analysis(symbol=None, state=None, analysis_id=None):
    """Pure lookup of stored analysis for rendering/saving; never triggers AI calls."""
    target = ensure_analysis_state(state)
    aid = analysis_id
    if not aid and symbol:
        aid = target["candidate_analysis_ids"].get(symbol)
    if not aid:
        return None
    return target["analyses_by_id"].get(aid)


def run_and_store_candidate_analysis(
    symbol,
    scanner_score_value,
    state=None,
    runner=None,
    resume_analysis_id=None,
    debate_rounds=None,
    max_parallel_workers=1,
    fail_after_node=None,
):
    """Explicit command to execute multi-agent analysis once (or resume from DAG checkpoint) and store its immutable snapshot."""
    target = ensure_analysis_state(state)
    fn = runner or run_multi_agent
    if fn is run_multi_agent or resume_analysis_id is not None or debate_rounds is not None or max_parallel_workers != 1 or fail_after_node is not None:
        try:
            raw_result = fn(
                symbol,
                scanner_score_value,
                debate_rounds=debate_rounds,
                resume_analysis_id=resume_analysis_id,
                max_parallel_workers=max_parallel_workers,
                fail_after_node=fail_after_node,
            )
        except TypeError:
            raw_result = fn(symbol, scanner_score_value)
    else:
        raw_result = fn(symbol, scanner_score_value)
    return store_analysis_result(raw_result, state=target)


def analyze_candidates_batch(candidates, top_n, state=None, force_refresh=False, runner=None):
    """Explicit command to analyze up to top_n candidates when requested by user action."""
    target = ensure_analysis_state(state)
    results = []
    for candidate in (candidates or [])[: max(int(top_n), 0)]:
        symbol = candidate["symbol"]
        existing = get_rendered_analysis(symbol=symbol, state=target)
        if existing is not None and not force_refresh:
            results.append(existing)
            continue
        res = run_and_store_candidate_analysis(
            symbol,
            candidate["score"],
            state=target,
            runner=runner,
        )
        results.append(res)
    return results


def _resolve_veto_reasons_str(result):
    if isinstance(result.get("veto_reasons"), str) and result["veto_reasons"].strip():
        return result["veto_reasons"].strip()
    gr = result.get("gate_report")
    if isinstance(gr, dict):
        codes = gr.get("veto_codes")
        if isinstance(codes, list) and codes:
            return ", ".join(str(c) for c in codes)
        if isinstance(gr.get("veto_summary"), str) and gr["veto_summary"].strip():
            return gr["veto_summary"].strip()
    return "NONE"


def _find_existing_signal_id(con, aid, ikey):
    if aid:
        row = con.execute("SELECT id FROM signals WHERE analysis_id=? LIMIT 1", (aid,)).fetchone()
        if row:
            return int(row["id"])
    if ikey:
        row = con.execute("SELECT id FROM signals WHERE idempotency_key=? LIMIT 1", (ikey,)).fetchone()
        if row:
            return int(row["id"])
    return None


def save_signal(result, analysis_id=None, idempotency_key=None):
    if _stg_mod is not None:
        return _stg_mod.save_signal_record(
            result,
            db_file=DB_FILE,
            analysis_id=analysis_id,
            idempotency_key=idempotency_key,
            extra_secrets=list(GROQ_KEYS) + ([TG_TOKEN] if TG_TOKEN else []),
        )
    return 0


def save_rendered_analysis(analysis_id, state=None, tg_enabled=False):
    """Save a previously rendered analysis by its exact analysis_id or setup idempotency_key without re-running AI."""
    target = ensure_analysis_state(state)
    stored = get_rendered_analysis(state=target, analysis_id=analysis_id)
    if stored is None:
        raise KeyError(f"Analysis ID tidak ditemukan di session state: {analysis_id}")

    ikey = stored.get("idempotency_key") or compute_signal_idempotency_key(stored)
    already_in_session = analysis_id in target["saved_analysis_ids"]
    con = db()
    existing_id = _find_existing_signal_id(con, analysis_id, ikey)
    con.close()
    already_in_db = existing_id is not None

    signal_id = save_signal(stored, analysis_id=analysis_id, idempotency_key=ikey)
    target["saved_analysis_ids"][analysis_id] = signal_id
    created_new = not (already_in_session or already_in_db)

    tg_sent = False
    tg_msg = ""
    outbox_status = "NOT_APPLICABLE"
    if (
        tg_enabled
        and stored.get("final") in {"LONG", "SHORT"}
        and analysis_id not in target["notified_analysis_ids"]
        and ikey not in target["notified_analysis_ids"]
    ):
        try:
            ok, msg = telegram(telegram_message(stored))
            tg_sent = bool(ok)
            tg_msg = _safe_ui_error(msg)
        except Exception as tg_exc:
            tg_sent = False
            tg_msg = _safe_ui_error(tg_exc)
        if tg_sent:
            outbox_status = "SENT"
            target["notified_analysis_ids"].add(analysis_id)
            if ikey:
                target["notified_analysis_ids"].add(ikey)
        else:
            outbox_status = "FAILED_RETRYABLE"

    return {
        "signal_id": signal_id,
        "analysis_id": analysis_id,
        "idempotency_key": ikey,
        "created_new": created_new,
        "tg_sent": tg_sent,
        "tg_msg": tg_msg,
        "persistence_status": "SAVED",
        "notification_status": outbox_status,
    }


def reset_all_trading_state(state=None, confirmed=False, backup_before_reset=True):
    """Scoped destructive reset of sandbox DB tables (signals, votes, memories, pattern_events) and session state (PH01-04 / F32 / T26)."""
    if not confirmed:
        raise PermissionError("Reset database memerlukan konfirmasi eksplisit (confirmed=True).")

    backup_path = None
    con = db()
    if backup_before_reset and DB_FILE != ":memory:":
        try:
            row_count = sum(
                con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                for tbl in ("signals", "agent_votes", "memories", "pattern_events")
            )
            if row_count > 0:
                backup_path = f"{DB_FILE}.reset_backup"
                bcon = sqlite3.connect(backup_path)
                con.backup(bcon)
                bcon.close()
        except Exception:
            backup_path = None

    con.executescript(
        """
        DELETE FROM paper_account_ledger;
        DELETE FROM paper_positions;
        DELETE FROM agent_votes;
        DELETE FROM memories;
        DELETE FROM signals;
        DELETE FROM pattern_events;
        DELETE FROM usage_events;
        DELETE FROM agent_runs;
        DELETE FROM dag_checkpoints;
        DELETE FROM analysis_runs;
        DELETE FROM budget_reservations;
        DELETE FROM quota_scopes;
        DELETE FROM monitor_runtime_state;
        """
    )
    con.commit()
    con.close()

    target = ensure_analysis_state(state)
    target["saved_analysis_ids"] = {}
    target["notified_analysis_ids"] = set()
    target["v56_processed_fingerprints"] = set()
    target["analyses_by_id"] = {}
    target["candidate_analysis_ids"] = {}
    target["last_result"] = None
    target["confirm_reset"] = False

    with _QUOTA_SCOPE_LOCK:
        _QUOTA_SCOPE_BUDGETS.clear()

    return {"reset": True, "backup_path": backup_path}

def current_price(symbol):
    data = bget("/api/v3/ticker/price", {"symbol": symbol})
    return float(data["price"])

def update_paper_trades(
    bars_by_symbol=None,
    use_klines=False,
    tie_breaker="SL_FIRST_CONSERVATIVE",
    max_wait_bars=None,
    max_hold_bars=None,
    return_detailed=False,
):
    if _stg_mod is not None:
        return _stg_mod.reconcile_paper_positions(
            db_file=DB_FILE,
            bars_by_symbol=bars_by_symbol,
            kline_fetcher=(lambda sym, tf, lim: klines(sym, tf, lim)) if use_klines else None,
            price_fetcher=lambda sym: current_price(sym),
            tie_breaker=tie_breaker,
            max_wait_bars=max_wait_bars,
            max_hold_bars=max_hold_bars,
            initial_equity=PAPER_INITIAL_EQUITY,
            return_detailed=return_detailed,
        )
    return []

def format_signal_history_status(row):
    """Format signal history status so WAIT observations are never displayed as OPEN positions (F33)."""
    keys = row.keys() if hasattr(row, "keys") else ()
    p_state = str(row["position_state"] if ("position_state" in keys and row["position_state"]) else "").upper().strip()
    res = row["result"] if "result" in keys else None
    if p_state in {"CLOSED_TP", "CLOSED_SL", "CLOSED_TIME_STOP", "CLOSED_INVALIDATED", "EXPIRED_UNFILLED", "CANCELLED"}:
        return f"{res} ({p_state})" if res and res != p_state else p_state
    if res:
        return str(res)
    if p_state == "PENDING_ENTRY":
        return "PENDING_ENTRY"
    dec = str(row["final_decision"] if "final_decision" in keys else "").upper().strip()
    if dec in {"LONG", "SHORT"}:
        return "OPEN (LEGACY PAPER)"
    return "OBSERVED (WAIT)"


def performance():
    if _stg_mod is not None:
        return _stg_mod.compute_dual_performance_metrics(db_file=DB_FILE, initial_equity=PAPER_INITIAL_EQUITY)
    return {}

# -------------------------
# Telegram
# -------------------------
def _safe_ui_error(err_or_msg):
    extra_s = list(GROQ_KEYS) + ([TG_TOKEN] if TG_TOKEN else [])
    if _svc_mod is not None:
        return _svc_mod.sanitize_ui_error(err_or_msg, extra_secrets=extra_s)
    if _cfg_mod is not None:
        return str(redact_secrets(str(err_or_msg or ""), extra_secrets=extra_s))
    return str(err_or_msg or "")


def telegram(message):
    extra_s = list(GROQ_KEYS) + ([TG_TOKEN] if TG_TOKEN else [])
    if _svc_mod is not None:
        try:
            return _svc_mod.send_telegram_notification(
                message,
                tg_token=TG_TOKEN,
                tg_chat=TG_CHAT,
                post_fn=requests.post,
                extra_secrets=extra_s,
            )
        except Exception as exc:
            return False, _safe_ui_error(exc)
    if not TG_TOKEN or not TG_CHAT:
        return False, "Telegram belum dikonfigurasi."

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": _safe_ui_error(message)},
            timeout=12,
        )
        if r.status_code != 200:
            return False, _safe_ui_error((r.text or "")[:300])
        data = r.json()
        return bool(data.get("ok")), _safe_ui_error(data.get("description", "OK"))
    except Exception as exc:
        return False, _safe_ui_error(exc)

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
🏗️ Structure: {result['packet']['smc']['structure']['bias']} | BOS={result['packet']['smc']['structure']['bos']}
🧱 SMC Score: {result['packet']['smc']['smc_score']}/100
⏱️ Entry Timing: {result['entry_status']}
📚 Order Imbalance: {result['packet']['order_book']['imbalance']:+.1%}
🕯 Candle: {result['packet']['candlestick']['name']}

👨‍⚖️ Final Judge:
{result['portfolio']['reasoning']}

⚠️ Paper-analysis only. Tidak ada order otomatis."""

# -------------------------
# V5.6 Pattern & Momentum Engine (PH04-01 Multi-Horizon 5m/15m/1h/4h/1d)
# -------------------------
def _pattern_score(frames):
    """Fast deterministic radar across scalping (1h->15m->5m), intraday (4h->1h->15m), and swing (1d->4h->1h)."""
    s5 = frames["5m"]["smc"]
    s15 = frames["15m"]["smc"]
    s1h = frames["1h"]["smc"]
    s4h = frames.get("4h", {}).get("smc") if isinstance(frames.get("4h"), dict) else None
    s1d = frames.get("1d", {}).get("smc") if isinstance(frames.get("1d"), dict) else None

    def _tf_warmed(tf_name):
        tf_entry = frames.get(tf_name)
        if not isinstance(tf_entry, dict) or not isinstance(tf_entry.get("smc"), dict):
            return False
        if "ema200_ready" in tf_entry and tf_entry.get("ema200_ready") is False:
            return False
        ws = str(tf_entry.get("warmup_status", "READY") or "READY").upper()
        return ws == "READY"

    score_long = 0
    score_short = 0
    evidence_long = []
    evidence_short = []

    def add(direction, points, text):
        nonlocal score_long, score_short
        if direction == "LONG":
            score_long += points; evidence_long.append(text)
        elif direction == "SHORT":
            score_short += points; evidence_short.append(text)

    if s1h["structure"]["bias"] == "BULLISH": add("LONG", 15, "1H structure bullish")
    if s1h["structure"]["bias"] == "BEARISH": add("SHORT", 15, "1H structure bearish")
    if s15["structure"]["bias"] == "BULLISH": add("LONG", 15, "15M structure bullish")
    if s15["structure"]["bias"] == "BEARISH": add("SHORT", 15, "15M structure bearish")

    if s15["structure"]["bos"] == "BULLISH BOS": add("LONG", 12, "15M bullish BOS")
    if s15["structure"]["bos"] == "BEARISH BOS": add("SHORT", 12, "15M bearish BOS")
    if s5["structure"]["choch"] == "BULLISH CHOCH": add("LONG", 14, "5M bullish CHOCH")
    if s5["structure"]["choch"] == "BEARISH CHOCH": add("SHORT", 14, "5M bearish CHOCH")
    if s5["liquidity"].get("sweep_low"): add("LONG", 15, "5M sell-side liquidity sweep")
    if s5["liquidity"].get("sweep_high"): add("SHORT", 15, "5M buy-side liquidity sweep")
    if s5.get("bullish_trigger"): add("LONG", 18, "5M bullish trigger")
    if s5.get("bearish_trigger"): add("SHORT", 18, "5M bearish trigger")
    if s15["bullish_entry"]["confluence"]: add("LONG", 8, "15M OB + FVG confluence")
    if s15["bearish_entry"]["confluence"]: add("SHORT", 8, "15M OB + FVG confluence")
    if s5["displacement"]["bullish"]: add("LONG", 8, "5M bullish displacement")
    if s5["displacement"]["bearish"]: add("SHORT", 8, "5M bearish displacement")

    if s4h is not None and _tf_warmed("4h"):
        if s4h["structure"]["bias"] == "BULLISH": evidence_long.append("4H structure bullish")
        elif s4h["structure"]["bias"] == "BEARISH": evidence_short.append("4H structure bearish")
    if s1d is not None and _tf_warmed("1d"):
        if s1d["structure"]["bias"] == "BULLISH": evidence_long.append("1D structure bullish")
        elif s1d["structure"]["bias"] == "BEARISH": evidence_short.append("1D structure bearish")

    long_score = min(100, score_long)
    short_score = min(100, score_short)
    if max(long_score, short_score) < 42:
        state = "QUIET"
        direction = "WAIT"
        score = max(long_score, short_score)
    else:
        direction = "LONG" if long_score >= short_score else "SHORT"
        score = max(long_score, short_score)
        if score >= 78 and ((direction == "LONG" and s5.get("bullish_trigger")) or (direction == "SHORT" and s5.get("bearish_trigger"))):
            state = "TRIGGERED"
        elif score >= 60:
            state = "DEVELOPING"
        else:
            state = "WATCH"

    def _mode_matrix_entry(req_tfs, b_tf, st_tf, tr_tf):
        missing = [t for t in req_tfs if not isinstance(frames.get(t), dict) or not isinstance(frames.get(t, {}).get("smc"), dict)]
        unwarmed = [t for t in req_tfs if t not in missing and not _tf_warmed(t)]
        ok = (len(missing) == 0 and len(unwarmed) == 0)
        b_smc = frames.get(b_tf, {}).get("smc") if (b_tf not in missing and b_tf not in unwarmed) else None
        st_smc = frames.get(st_tf, {}).get("smc") if (st_tf not in missing and st_tf not in unwarmed) else None
        tr_smc = frames.get(tr_tf, {}).get("smc") if (tr_tf not in missing and tr_tf not in unwarmed) else None
        return {
            "bias_tf": b_tf,
            "setup_tf": st_tf,
            "trigger_tf": tr_tf,
            "required_timeframes": list(req_tfs),
            "missing_timeframes": missing,
            "unwarmed_timeframes": unwarmed,
            "bias": b_smc["structure"]["bias"] if b_smc is not None else "INSUFFICIENT_DATA",
            "setup": st_smc["structure"]["bias"] if st_smc is not None else "INSUFFICIENT_DATA",
            "trigger": tr_smc["timing"] if (ok and tr_smc is not None) else "INSUFFICIENT_DATA",
            "data_status": "COMPLETE" if ok else "INSUFFICIENT_DATA",
        }

    scalp_matrix = _mode_matrix_entry(("5m", "15m", "1h"), "1h", "15m", "5m")
    intraday_matrix = _mode_matrix_entry(("15m", "1h", "4h"), "4h", "1h", "15m")
    swing_matrix = _mode_matrix_entry(("1h", "4h", "1d"), "1d", "4h", "1h")

    return {
        "state": state,
        "direction": direction,
        "score": int(score),
        "long_score": int(long_score),
        "short_score": int(short_score),
        "evidence": (evidence_long if direction == "LONG" else evidence_short)[:6],
        "scalping": scalp_matrix,
        "intraday": intraday_matrix,
        "swing": swing_matrix,
    }


def pattern_snapshot(symbol, as_of=None, use_cache=True, cache=None, budget=None, include_higher_timeframes=True):
    active_cache = cache if cache is not None else (GLOBAL_MARKET_CACHE if _mkt_mod is not None else None)
    active_budget = budget
    now_ms = normalize_as_of_ms(as_of) if _mkt_mod is not None else None
    frames = {}
    from_cache_flags = {}
    timeframes_meta = {}

    tf_specs = [("5m", 120), ("15m", 140), ("1h", 140)]
    if include_higher_timeframes:
        tf_specs = [("5m", 260), ("15m", 260), ("1h", 260), ("4h", 260), ("1d", 260)]

    for tf, limit in tf_specs:
        is_core_tf = tf in ("5m", "15m", "1h")
        snap = None
        raw_df = None
        try:
            if use_cache and active_cache is not None:
                snap = active_cache.get_kline_snapshot(symbol, tf, limit, now_ms=now_ms)
            if snap is None:
                if active_budget is not None:
                    ok_b, reason_b = active_budget.check_and_reserve("/api/v3/klines", now_ms=now_ms)
                    if not ok_b:
                        if is_core_tf:
                            raise RuntimeError(reason_b)
                        timeframes_meta[tf] = {"loaded": False, "ema200_ready": False, "warmup_status": "BUDGET_BLOCKED"}
                        continue
                raw_df = klines(symbol, tf, limit)
                if _mkt_mod is not None:
                    snap = _mkt_mod.build_timeframe_snapshot(raw_df, symbol=symbol, interval=tf, as_of=now_ms, limit=limit)
                    if use_cache and active_cache is not None and snap.validation.valid:
                        active_cache.put_kline_snapshot(snap, limit=limit, fetched_at_ms=snap.as_of_ms)
            if snap is not None:
                df_closed = snap.copy_closed_df() if len(snap.closed_df) > 0 else snap.raw_df
                if raw_df is not None and hasattr(raw_df, "attrs"):
                    for ak in ("warmup_report", "ema200_ready", "warmup_status", "indicator_version"):
                        if ak in raw_df.attrs:
                            df_closed.attrs[ak] = raw_df.attrs[ak]
                if len(df_closed) == 0:
                    if is_core_tf:
                        raise RuntimeError(f"Kline {tf} kosong untuk {symbol}")
                    timeframes_meta[tf] = {"loaded": False, "ema200_ready": False, "warmup_status": "EMPTY_DATA"}
                    continue
                frames[tf] = df_closed
                from_cache_flags[tf] = bool(snap.from_cache)
            else:
                df_closed = _closed_df(raw_df, as_of=now_ms, interval=tf)
                if len(df_closed) == 0:
                    if is_core_tf:
                        raise RuntimeError(f"Kline {tf} kosong untuk {symbol}")
                    timeframes_meta[tf] = {"loaded": False, "ema200_ready": False, "warmup_status": "EMPTY_DATA"}
                    continue
                frames[tf] = df_closed
                from_cache_flags[tf] = False

            w_rep = frames[tf].attrs.get("warmup_report") if isinstance(frames[tf].attrs.get("warmup_report"), dict) else {}
            raw_bar_count = int(w_rep.get("bar_count", len(frames[tf])))
            ema200_ok = bool(frames[tf].attrs.get("ema200_ready", raw_bar_count >= 200)) if include_higher_timeframes else True
            w_status = str(frames[tf].attrs.get("warmup_status", "READY" if ema200_ok else "PARTIAL_WARMUP_NO_EMA200")) if include_higher_timeframes else "READY"
            timeframes_meta[tf] = {
                "loaded": True,
                "bar_count": raw_bar_count,
                "usable_bar_count": len(frames[tf]),
                "ema200_ready": ema200_ok,
                "warmup_status": w_status,
            }
        except Exception:
            if is_core_tf:
                raise
            timeframes_meta[tf] = {"loaded": False, "ema200_ready": False, "warmup_status": "FETCH_FAILED"}

    if not include_higher_timeframes and use_cache and active_cache is not None:
        for htf in ("4h", "1d"):
            cached_htf = active_cache.get_kline_snapshot(symbol, htf, 260, now_ms=now_ms)
            if cached_htf is not None and len(cached_htf.closed_df) > 0:
                frames[htf] = cached_htf.copy_closed_df()
                from_cache_flags[htf] = True
                raw_cnt = len(frames[htf])
                ema_ok = bool(frames[htf].attrs.get("ema200_ready", raw_cnt >= 200))
                timeframes_meta[htf] = {
                    "loaded": True,
                    "bar_count": raw_cnt,
                    "usable_bar_count": raw_cnt,
                    "ema200_ready": ema_ok,
                    "warmup_status": "READY" if ema_ok else "PARTIAL_WARMUP_NO_EMA200",
                }

    smcs = {tf: smc_analysis(df, interval=tf) for tf, df in frames.items()}
    score_input = {
        tf: {
            "smc": smcs[tf],
            "ema200_ready": timeframes_meta.get(tf, {}).get("ema200_ready", True),
            "warmup_status": timeframes_meta.get(tf, {}).get("warmup_status", "READY"),
        }
        for tf in smcs
    }
    signal = _pattern_score(score_input)
    price = float(frames["5m"].iloc[-1].Close)
    bar_time = _extract_closed_bar_timestamp({"frames": frames}, tf="15m")
    return {
        "symbol": symbol,
        "frames": frames,
        "smc": smcs,
        "timeframes_meta": timeframes_meta,
        "pattern": signal,
        "price": price,
        "bar_time": bar_time,
        "from_cache": from_cache_flags,
    }


def momentum_candidates(n=8, as_of=None, use_cache=True, cache=None, budget=None, return_report=False, include_higher_timeframes=False):
    active_cache = cache if cache is not None else (GLOBAL_MARKET_CACHE if _mkt_mod is not None else None)
    active_budget = budget if budget is not None else (GLOBAL_REQUEST_BUDGET if _scan_mod is not None else None)
    now_ms = normalize_as_of_ms(as_of) if _mkt_mod is not None else None

    failed_symbols = []
    rate_limited = False
    retry_after_sec = 0.0

    tickers = None
    if use_cache and active_cache is not None:
        tickers = active_cache.get_tickers_24h(now_ms=now_ms)
    if tickers is None:
        if active_budget is not None:
            ok_t, reason_t = active_budget.check_and_reserve("/api/v3/ticker/24hr", now_ms=now_ms)
            if not ok_t:
                diag = {
                    "target_scan_count": 0,
                    "succeeded_count": 0,
                    "failed_count": 1,
                    "failed_symbols": [{"symbol": "ALL_TICKERS", "stage": "ticker_24hr", "error_code": reason_t.split(":")[0], "message": reason_t}],
                    "cache_stats": active_cache.stats() if active_cache else {},
                    "budget_stats": active_budget.snapshot(now_ms=now_ms),
                    "rate_limited": "RATE_LIMIT" in reason_t,
                    "retry_after_seconds": active_budget.last_retry_after_seconds,
                }
                if hasattr(st, "session_state") and isinstance(st.session_state, dict):
                    st.session_state["last_momentum_diagnostics"] = diag
                return {"candidates": [], "diagnostics": diag} if return_report else []
        try:
            tickers = bget("/api/v3/ticker/24hr")
            if use_cache and active_cache is not None and isinstance(tickers, list):
                active_cache.put_tickers_24h(tickers, fetched_at_ms=now_ms)
        except Exception as exc:
            if _scan_mod is not None and _scan_mod.is_http_429_error(exc):
                rate_limited = True
                retry_after_sec = _scan_mod.extract_retry_after_seconds(exc)
                if active_budget is not None:
                    active_budget.record_rate_limit(retry_after_sec, now_ms=now_ms)
            raise

    pool = []
    stable_like = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE"}
    for t in tickers:
        symbol = t.get("symbol", "")
        if not symbol.endswith("USDT") or symbol[:-4] in stable_like:
            continue
        qvol = float(t.get("quoteVolume", 0) or 0)
        if qvol < 3_000_000:
            continue
        change = float(t.get("priceChangePercent", 0) or 0)
        activity = min(qvol / 20_000_000, 5) * 10 + min(abs(change), 10) * 2
        pool.append((symbol, qvol, change, activity))
    pool.sort(key=lambda x: x[3], reverse=True)

    selected = pool[:max(int(n), 1)]
    out = []
    for symbol, qvol, change, activity in selected:
        try:
            snap = pattern_snapshot(symbol, as_of=now_ms, use_cache=use_cache, cache=active_cache, budget=active_budget, include_higher_timeframes=include_higher_timeframes)
            snap["qvol"] = qvol
            snap["change"] = change
            snap["activity_score"] = round(activity, 2)
            out.append(snap)
        except Exception as exc:
            err_code = "HTTP_429_RATE_LIMIT" if (_scan_mod is not None and _scan_mod.is_http_429_error(exc)) else "PATTERN_SNAPSHOT_FAILED"
            if _scan_mod is not None and _scan_mod.is_http_429_error(exc):
                rate_limited = True
                retry_after_sec = _scan_mod.extract_retry_after_seconds(exc)
                if active_budget is not None:
                    active_budget.record_rate_limit(retry_after_sec, now_ms=now_ms)
            elif "RATE_LIMIT_COOLDOWN" in str(exc):
                err_code = "RATE_LIMIT_COOLDOWN"
                rate_limited = True
            elif "BUDGET_EXCEEDED" in str(exc):
                err_code = "REQUEST_BUDGET_EXCEEDED"
            failed_symbols.append({
                "symbol": symbol,
                "stage": "pattern_snapshot",
                "error_code": err_code,
                "message": _safe_ui_error(exc)[:240],
            })

    sorted_out = sorted(out, key=lambda x: x["pattern"]["score"], reverse=True)
    diag = {
        "total_tickers": len(tickers or []),
        "liquid_universe_count": len(pool),
        "target_scan_count": len(selected),
        "succeeded_count": len(sorted_out),
        "failed_count": len(failed_symbols),
        "failed_symbols": failed_symbols,
        "cache_stats": active_cache.stats() if active_cache is not None else {},
        "budget_stats": active_budget.snapshot(now_ms=now_ms) if active_budget is not None else {},
        "rate_limited": rate_limited or (active_budget.snapshot(now_ms=now_ms)["in_cooldown"] if active_budget is not None else False),
        "retry_after_seconds": retry_after_sec or (active_budget.last_retry_after_seconds if active_budget is not None else 0.0),
    }
    if hasattr(st, "session_state") and isinstance(st.session_state, dict):
        st.session_state["last_momentum_diagnostics"] = diag

    if return_report:
        return {"candidates": sorted_out, "diagnostics": diag}
    return sorted_out


def pattern_fingerprint(snap, mode=None):
    """Time- and mode-aware pattern fingerprint so new setups on later bars/dates are not blocked forever (PH01-04 / F22 / T26)."""
    p = snap.get("pattern", {})
    smc = snap.get("smc", {})
    s5 = smc.get("5m", {})
    s15 = smc.get("15m", {})
    mode_str = str(mode or snap.get("mode") or "scalping").lower().strip()
    bar_time = _extract_closed_bar_timestamp(snap, tf="15m")

    dir_str = str(p.get("direction", "WAIT")).upper().strip()
    entry_key = "bullish_entry" if dir_str == "LONG" else "bearish_entry"
    zone_info = s15.get(entry_key, {}) if isinstance(s15.get(entry_key), dict) else {}
    z_low = zone_info.get("zone_low")
    z_high = zone_info.get("zone_high")
    zone_str = (
        f"{float(z_low):.8f}..{float(z_high):.8f}"
        if _is_finite_number(z_low) and _is_finite_number(z_high)
        else "no_zone"
    )

    return "|".join([
        str(snap.get("symbol", "UNKNOWN")),
        mode_str,
        str(bar_time),
        str(p.get("state", "QUIET")),
        dir_str,
        str(s5.get("timing", "NONE")),
        str(s15.get("timing", "NONE")),
        str(s5.get("structure", {}).get("bos", "NONE")),
        str(s5.get("structure", {}).get("choch", "NONE")),
        zone_str,
    ])


def save_pattern_event(snap, mode="scalping", notified=0):
    p = snap["pattern"]
    fp = pattern_fingerprint(snap, mode=mode)
    if _stg_mod is not None:
        return _stg_mod.save_pattern_event_record(
            snap,
            fingerprint=fp,
            mode=mode,
            notified=notified,
            db_file=DB_FILE,
        )
    now = datetime.now(timezone.utc).isoformat()
    con = db()
    row = con.execute(
        "SELECT id, notified FROM pattern_events WHERE fingerprint=? ORDER BY id DESC LIMIT 1",
        (fp,),
    ).fetchone()
    if row:
        con.close()
        return int(row["id"]), bool(row["notified"])
    cur = con.execute(
        "INSERT INTO pattern_events(created_at,symbol,mode,state,direction,score,fingerprint,notified) VALUES(?,?,?,?,?,?,?,?)",
        (now, snap["symbol"], mode, p["state"], p["direction"], p["score"], fp, int(notified)),
    )
    con.commit()
    event_id = int(cur.lastrowid)
    con.close()
    return event_id, bool(notified)


def mark_pattern_notified(event_id):
    con = db()
    con.execute("UPDATE pattern_events SET notified=1 WHERE id=?", (event_id,))
    con.commit()
    con.close()


def process_monitor_candidate_workflow(snap, state=None, tg_enabled=False, runner=None):
    """
    Execute the 3-stage monitor workflow (validation -> persistence -> notification) with durable
    SQLite checkpoints so WAIT outcomes or Telegram failures never re-run paid AI (F23).
    """
    target = ensure_analysis_state(state)
    event_id, already_notified = save_pattern_event(snap)
    fp = pattern_fingerprint(snap)
    wf_db = (
        _stg_mod.get_pattern_event_workflow(event_id=event_id, db_file=DB_FILE)
        if _stg_mod is not None
        else {"validation_status": "UNVALIDATED", "persistence_status": "UNSAVED", "notification_status": "SENT" if already_notified else "NOT_APPLICABLE", "analysis_id": None}
    )

    val_status = wf_db.get("validation_status", "UNVALIDATED")
    pers_status = wf_db.get("persistence_status", "UNSAVED")
    notif_status = wf_db.get("notification_status", "NOT_APPLICABLE")
    aid = wf_db.get("analysis_id")

    if val_status == "VALIDATED_WAIT" or fp in target["v56_processed_fingerprints"] and val_status != "VALIDATED_ENTRY":
        target["v56_processed_fingerprints"].add(fp)
        return {
            "event_id": event_id,
            "fingerprint": fp,
            "ai_called": False,
            "validation_status": "VALIDATED_WAIT",
            "persistence_status": pers_status,
            "notification_status": notif_status,
            "analysis_id": aid,
        }

    if val_status == "VALIDATED_ENTRY" and pers_status == "SAVED" and (notif_status == "SENT" or already_notified or not tg_enabled):
        target["v56_processed_fingerprints"].add(fp)
        return {
            "event_id": event_id,
            "fingerprint": fp,
            "ai_called": False,
            "validation_status": val_status,
            "persistence_status": pers_status,
            "notification_status": "SENT" if already_notified else notif_status,
            "analysis_id": aid,
        }

    if val_status == "VALIDATED_ENTRY" and pers_status == "SAVED" and tg_enabled and notif_status != "SENT":
        stored = get_rendered_analysis(state=target, analysis_id=aid) if aid else None
        if stored is not None:
            ok_tg, msg_tg = telegram(telegram_message(stored))
        else:
            ok_tg, msg_tg = telegram(pattern_alert_message(snap))
        new_notif = "SENT" if ok_tg else "FAILED_RETRYABLE"
        if _stg_mod is not None:
            _stg_mod.update_pattern_event_workflow(
                event_id=event_id,
                notification_status=new_notif,
                notified=1 if ok_tg else 0,
                db_file=DB_FILE,
            )
        elif ok_tg:
            mark_pattern_notified(event_id)
        return {
            "event_id": event_id,
            "fingerprint": fp,
            "ai_called": False,
            "validation_status": val_status,
            "persistence_status": pers_status,
            "notification_status": new_notif,
            "analysis_id": aid,
            "tg_msg": msg_tg,
        }

    result = run_and_store_candidate_analysis(
        snap["symbol"],
        snap["pattern"]["score"],
        state=target,
        runner=runner,
    )
    aid = result["analysis_id"]
    target["v56_processed_fingerprints"].add(fp)
    gate_ok = bool(result.get("gate_report", {}).get("approved", False))

    if result["final"] in {"LONG", "SHORT"} and gate_ok:
        val_status = "VALIDATED_ENTRY"
        if _stg_mod is not None:
            _stg_mod.update_pattern_event_workflow(
                event_id=event_id,
                validation_status=val_status,
                analysis_id=aid,
                db_file=DB_FILE,
            )
        save_out = save_rendered_analysis(
            aid,
            state=target,
            tg_enabled=tg_enabled and not already_notified,
        )
        pers_status = "SAVED"
        notif_status = "SENT" if save_out["tg_sent"] else ("FAILED_RETRYABLE" if (tg_enabled and not already_notified) else "NOT_APPLICABLE")
        if _stg_mod is not None:
            _stg_mod.update_pattern_event_workflow(
                event_id=event_id,
                validation_status=val_status,
                persistence_status=pers_status,
                notification_status=notif_status,
                analysis_id=aid,
                notified=1 if save_out["tg_sent"] else 0,
                db_file=DB_FILE,
            )
        elif save_out["tg_sent"]:
            mark_pattern_notified(event_id)
        return {
            "event_id": event_id,
            "fingerprint": fp,
            "ai_called": True,
            "validation_status": val_status,
            "persistence_status": pers_status,
            "notification_status": notif_status,
            "analysis_id": aid,
            "signal_id": save_out["signal_id"],
            "result": result,
        }

    val_status = "VALIDATED_WAIT"
    if _stg_mod is not None:
        _stg_mod.update_pattern_event_workflow(
            event_id=event_id,
            validation_status=val_status,
            persistence_status="UNSAVED",
            notification_status="NOT_APPLICABLE",
            analysis_id=aid,
            db_file=DB_FILE,
        )
    return {
        "event_id": event_id,
        "fingerprint": fp,
        "ai_called": True,
        "validation_status": val_status,
        "persistence_status": "UNSAVED",
        "notification_status": "NOT_APPLICABLE",
        "analysis_id": aid,
        "result": result,
    }


def pattern_alert_message(snap):
    """Format momentum radar alert message cleanly without malformed nested f-strings or global variable dependence (PH01-04 / PH04-01 / F31 / T26)."""
    p = snap.get("pattern", {})
    smc = snap.get("smc", {})
    s5 = smc.get("5m", {"timing": "-", "structure": {"bias": "-"}})
    s15 = smc.get("15m", {"timing": "-", "structure": {"bias": "-"}})
    s1h = smc.get("1h", {"timing": "-", "structure": {"bias": "-"}})
    s4h = smc.get("4h", {"timing": "INSUFFICIENT_DATA", "structure": {"bias": "INSUFFICIENT_DATA"}})
    s1d = smc.get("1d", {"timing": "INSUFFICIENT_DATA", "structure": {"bias": "INSUFFICIENT_DATA"}})
    raw_evidence = p.get("evidence") or []
    evidence_lines = "\n".join(f"• {item}" for item in raw_evidence) if raw_evidence else "• Tidak ada bukti tambahan"
    price_val = float(snap.get("price", 0.0) or 0.0)
    intra_status = p.get("intraday", {}).get("data_status", "COMPLETE") if isinstance(p.get("intraday"), dict) else "COMPLETE"
    swing_status = p.get("swing", {}).get("data_status", "COMPLETE") if isinstance(p.get("swing"), dict) else "COMPLETE"
    intra_str = (
        f"4H {s4h['structure']['bias']} → 1H {s1h['structure']['bias']} → 15M {s15.get('timing', s15['structure']['bias'])} [{intra_status}]"
        if "4h" in smc else f"1H {s1h['structure']['bias']} → 15M {s15['structure']['bias']}"
    )
    swing_str = (
        f"1D {s1d['structure']['bias']} → 4H {s4h['structure']['bias']} → 1H {s1h.get('timing', s1h['structure']['bias'])} [{swing_status}]"
        if ("1d" in smc and "4h" in smc) else f"1H {s1h['structure']['bias']}"
    )
    return (
        f"🚨 V5.6 MOMENTUM ALERT\n\n"
        f"{snap.get('symbol', '-')} — {p.get('state', '-')}\n"
        f"Direction: {p.get('direction', 'WAIT')}\n"
        f"Radar score: {p.get('score', 0)}/100\n"
        f"Price: ${price_val:.8f}\n\n"
        f"⚡ Scalping: 1H {s1h['structure']['bias']} → 15M {s15['structure']['bias']} → 5M {s5['timing']}\n"
        f"📊 Intraday: {intra_str}\n"
        f"🌊 Swing: {swing_str}\n\n"
        f"Evidence:\n{evidence_lines}\n\n"
        f"⚠️ Radar only — AI council masih harus memvalidasi sebelum paper trade."
    )


def mode_fallback(result):
    """Fallback & per-mode gate if Final Judge omits or provides raw mode objects (PH01-03 / F16 / T05)."""
    packet = result.get("packet", {})
    final = str(result.get("final", "WAIT")).upper().strip()
    portfolio = result.get("portfolio", {}) if isinstance(result.get("portfolio"), dict) else {}
    conf = portfolio.get("confidence", 0.0)
    global_vetoes = []
    if isinstance(result.get("gate_report"), dict):
        global_vetoes = list(result["gate_report"].get("veto_codes") or [])
    raw_modes = portfolio.get("modes") if isinstance(portfolio.get("modes"), dict) else {}
    return evaluate_mode_decisions(
        packet,
        raw_modes,
        final,
        global_veto_codes=global_vetoes,
        base_confidence=conf,
    )


# -------------------------
# PH07-01 Independent Background Worker / CLI Monitor & Runtime Status Honesty (F29 / M7)
# -------------------------
def get_runtime_monitor_status(
    auto_enabled=False,
    interval_seconds=45,
    worker_id="default_worker",
    now_ms=None,
):
    """Return honest, SQLite-backed monitor runtime status (worker vs session_only) (F29)."""
    if _mon_mod is not None:
        return _mon_mod.get_durable_monitor_runtime_status(
            db_file=DB_FILE,
            auto_enabled=auto_enabled,
            interval_seconds=interval_seconds,
            worker_id=worker_id,
            now_ms=now_ms,
        )
    if _mkt_mod is not None:
        return _mkt_mod.get_monitor_runtime_status(
            execution_mode="session_only",
            auto_enabled=auto_enabled,
            interval_seconds=interval_seconds,
            now_ms=now_ms,
        )
    return {
        "execution_mode": "session_only",
        "runtime_label": "SESSION_ONLY_FRAGMENT" if auto_enabled else "MANUAL_ONLY",
        "worker_alive": False,
        "auto_fragment_enabled": bool(auto_enabled),
        "disclaimer": "Mode Session-Only (Streamlit UI): pemantauan otomatis hanya berjalan selama tab browser aktif.",
    }


def run_single_monitor_cycle(
    monitor_n=8,
    max_validations=1,
    interval_seconds=45,
    execution_mode="worker",
    worker_id="default_worker",
    tg_enabled=False,
    state=None,
    as_of=None,
):
    """Execute one complete headless or UI monitor + paper reconciliation cycle (PH07-01 / F29)."""
    target_state = ensure_analysis_state(state)
    extra_s = list(GROQ_KEYS) + list(GEMINI_KEYS) + ([TG_TOKEN] if TG_TOKEN else [])
    ai_avail = bool(GROQ_KEYS or (GEMINI_ENABLED and GEMINI_KEYS))
    if _mon_mod is not None:
        return _mon_mod.run_monitor_cycle(
            monitor_n=monitor_n,
            max_validations=max_validations,
            interval_seconds=interval_seconds,
            execution_mode=execution_mode,
            worker_id=worker_id,
            db_file=DB_FILE,
            tg_enabled=tg_enabled,
            ai_available=ai_avail,
            state=target_state,
            momentum_fetcher=lambda n, return_report=True: momentum_candidates(n, as_of=as_of, return_report=return_report),
            workflow_processor=lambda snap, state=None, tg_enabled=False: process_monitor_candidate_workflow(
                snap, state=state, tg_enabled=tg_enabled
            ),
            pattern_saver=lambda snap: save_pattern_event(snap),
            reconcile_fn=lambda: update_paper_trades(return_detailed=True),
            as_of=as_of,
            extra_secrets=extra_s,
        )
    return {}


def create_background_monitor_worker(
    worker_id="default_worker",
    interval_seconds=45,
    monitor_n=8,
    max_validations=1,
    tg_enabled=False,
):
    """Create a controllable BackgroundMonitorWorker wired to the active runtime and SQLite DB (PH07-01 / F29)."""
    extra_s = list(GROQ_KEYS) + list(GEMINI_KEYS) + ([TG_TOKEN] if TG_TOKEN else [])
    return BackgroundMonitorWorker(
        worker_id=worker_id,
        db_file=DB_FILE,
        interval_seconds=interval_seconds,
        monitor_n=monitor_n,
        max_validations=max_validations,
        tg_enabled=tg_enabled,
        ai_available_fn=lambda: bool(GROQ_KEYS or (GEMINI_ENABLED and GEMINI_KEYS)),
        momentum_fetcher=lambda n, return_report=True: momentum_candidates(n, return_report=return_report),
        workflow_processor=lambda snap, state=None, tg_enabled=False: process_monitor_candidate_workflow(
            snap, state=state, tg_enabled=tg_enabled
        ),
        pattern_saver=lambda snap: save_pattern_event(snap),
        reconcile_fn=lambda: update_paper_trades(return_detailed=True),
        extra_secrets=extra_s,
    )


def run_cli_entrypoint(argv=None, print_output=False, sleep_fn=time.sleep):
    """
    Headless CLI entrypoint for `--worker`, `--monitor-once`, `--reconcile-only`, and `--status`
    (PH07-01 / F29 / M7). Never launches Streamlit UI when CLI mode flags are passed.
    """
    global DB_FILE
    args = parse_cli_args(argv if argv is not None else sys.argv[1:])
    if not args.get("is_cli_mode"):
        return {"executed": False, "reason": "NOT_CLI_MODE", "args": args}

    if args.get("db_file"):
        DB_FILE = str(args["db_file"])
    init_db()

    tg_flag = bool(args.get("telegram") or (TG_TOKEN and TG_CHAT))
    w_id = str(args.get("worker_id") or "cli_worker")
    interval_s = int(args.get("interval") or 45)
    mon_n = int(args.get("monitor_n") or 8)
    max_ai = int(args.get("max_ai") or 1)

    if args.get("status"):
        st_out = get_runtime_monitor_status(
            auto_enabled=False,
            interval_seconds=interval_s,
            worker_id=w_id,
        )
        res = {"executed": True, "mode": "status", "status": st_out}
    elif args.get("reconcile_only"):
        upd = update_paper_trades(return_detailed=True)
        res = {"executed": True, "mode": "reconcile_only", "updates_count": len(upd), "updates": upd}
    elif args.get("monitor_once"):
        rep = run_single_monitor_cycle(
            monitor_n=mon_n,
            max_validations=max_ai,
            interval_seconds=interval_s,
            execution_mode="worker",
            worker_id=w_id,
            tg_enabled=tg_flag,
        )
        res = {"executed": True, "mode": "monitor_once", "report": rep}
    else:
        worker = create_background_monitor_worker(
            worker_id=w_id,
            interval_seconds=interval_s,
            monitor_n=mon_n,
            max_validations=max_ai,
            tg_enabled=tg_flag,
        )
        reports = worker.run_loop(max_cycles=args.get("max_cycles"), sleep_fn=sleep_fn)
        res = {
            "executed": True,
            "mode": "worker",
            "worker_id": w_id,
            "cycles_executed": len(reports),
            "reports": reports,
            "status": worker.status(),
        }

    if print_output:
        print(json.dumps(res, ensure_ascii=False, default=str))
    return res

# -------------------------
# UI
# -------------------------
def render_app():
    _configure_page()

    ensure_analysis_state(st.session_state)

    st.title("🧠 AI Consensus Trading V5.5")
    st.caption(
        "Crypto Scanner → 🚨 Pattern Radar → 🏗️ Structure/Liquidity/OB-FVG/Execution → 🐂 Bull ↔ 🐻 Bear → "
        "🧠 Research → 💹 Trader → 🛡️ Risk Team → 👨‍⚖️ Final Judge → 3 Trading Modes → Paper Trading"
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
                    st.error(f"Scanner gagal: {_safe_ui_error(e)}")

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
    st.sidebar.caption("V5.6: Radar pola cepat → AI hanya dipanggil saat setup menarik")
    st.sidebar.caption(
        f"⚡ Analysts: {ANALYST_MODEL} | cap={ resolve_effective_max_tokens('analyst') } tok | reasoning low"
    )
    st.sidebar.caption(
        f"🐂🐻 Bull/Bear: {DEBATE_MODEL} | cap={ resolve_effective_max_tokens('debate') } tok | reasoning none"
    )
    st.sidebar.caption(
        f"🧠 Research: {RESEARCH_MODEL} | cap={ resolve_effective_max_tokens('research') } tok | strict JSON"
    )
    st.sidebar.caption(
        f"💹 Trader/Risk: cap={ resolve_effective_max_tokens('analyst', TRADER_MAX_TOKENS) }/{ resolve_effective_max_tokens('analyst', RISK_MAX_TOKENS) } tok (floor={MIN_COMPLETION_TOKENS})"
    )
    st.sidebar.caption(
        f"👨‍⚖️ Judge: {JUDGE_MODEL} | cap={ resolve_effective_max_tokens('judge') } tok | strict JSON"
    )
    st.sidebar.caption(
        f"🛡️ Cap per kandidat: {MAX_AI_CALLS_PER_CANDIDATE} logis / {_resolve_physical_cap()} fisik | SDK retry=0"
    )
    st.sidebar.caption(f"⏱️ Jeda AI: {GROQ_MIN_DELAY:.1f}s | Timeout: {AI_HTTP_TIMEOUT_SECONDS:.0f}s")

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
        ok, msg = telegram("✅ TEST AI CONSENSUS TRADING V5.3\nTelegram berhasil terhubung.")
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
            "Ini akan menghapus histori signal, agent votes, memory, pattern_events, dan cache sesi aktif."
        )
        if st.sidebar.button("⚠️ YA, HAPUS SEMUA"):
            reset_all_trading_state(state=st.session_state, confirmed=True)
            st.rerun()

    # -------------------------
    # Performance dashboard
    # -------------------------
    perf = performance()

    st.subheader("📊 V5.6 Performance Memory (Legacy R-Snapshot)")
    st.caption(
        f"ℹ️ Mode Akuntansi: `{perf.get('accounting_mode', LEGACY_ACCOUNTING_MODE)}` (n={perf.get('sample_size', perf['closed'])} trade tertutup, "
        f"{perf.get('observed_wait', 0)} observasi WAIT). {perf.get('accounting_disclaimer', '')} "
        f"{perf.get('confidence_disclaimer', '')}"
    )

    p1, p2, p3, p4, p5 = st.columns(5)
    p1.metric("Closed Trades (n)", perf["closed"])
    p2.metric("Open Paper Trades", perf["open"])
    p3.metric("Win Rate (R-based)", f"{perf['win_rate']:.1f}%")
    p4.metric("Average R (Gross)", f"{perf['avg_r']:+.2f}R")
    pf = (
        "N/A"
        if perf["profit_factor"] is None or not _is_finite_number(perf["profit_factor"])
        else f"{perf['profit_factor']:.2f}"
    )
    p5.metric("Profit Factor (R)", pf)

    v6_perf = perf.get("v6_realistic") if isinstance(perf.get("v6_realistic"), dict) else None
    if v6_perf is not None:
        v6_an = v6_perf.get("analytics") or {}
        v6_cal = v6_perf.get("calibration") or {}
        st.caption(
            f"📒 **V6 Realistic Paper Ledger (`{v6_perf.get('accounting_mode', REALISTIC_ACCOUNTING_MODE)}`)**: "
            f"Equity=${v6_perf.get('current_equity', PAPER_INITIAL_EQUITY):,.2f} "
            f"(Free=${v6_perf.get('free_equity', PAPER_INITIAL_EQUITY):,.2f}) | "
            f"Net PnL=${v6_perf.get('total_net_pnl', 0.0):+,.2f} (Costs=${v6_perf.get('total_cost', 0.0):,.2f}) | "
            f"Avg Net R={v6_perf.get('avg_net_r', 0.0):+.2f}R | "
            f"Max DD={v6_an.get('max_drawdown_pct', 0.0):.2f}% | "
            f"{v6_an.get('sample_warning', '')} | "
            f"Kalibrasi: `{v6_cal.get('calibration_status', 'NO_DATA')}`"
        )

    latest_smc = None
    try:
        con = db()
        latest_smc = con.execute(
            "SELECT smc_score, entry_status, smc_bias FROM signals "
            "WHERE smc_score IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        con.close()
    except Exception:
        latest_smc = None
    if latest_smc:
        q1, q2, q3 = st.columns(3)
        q1.metric("Latest SMC Score", f"{float(latest_smc['smc_score'] or 0):.0f}/100")
        q2.metric("Entry Timing", latest_smc["entry_status"] or "-")
        q3.metric("SMC Bias", latest_smc["smc_bias"] or "-")

    st.divider()

    # -------------------------
    # V5.6 Momentum Monitor
    # -------------------------
    st.subheader("🚨 V5.6 Pattern & Momentum Monitor")
    st.caption("Radar Python mencari pola lebih dulu. AI Council hanya dipanggil ketika setup cukup menarik; semua trade tetap PAPER.")
    mc1, mc2, mc3 = st.columns(3)
    monitor_n = mc1.slider("Pasangan yang dimonitor", 4, 12, 8, key="v56_monitor_n")
    monitor_auto = mc2.checkbox("🔄 Auto monitor", value=False, key="v56_auto")
    monitor_interval = mc3.selectbox("Interval", [30, 45, 60, 90], index=1, format_func=lambda x: f"{x} detik", key="v56_interval")
    max_validations = st.slider("Maksimal validasi AI per siklus", 1, 2, 1, key="v56_max_ai")

    rt_mon = get_runtime_monitor_status(
        auto_enabled=monitor_auto,
        interval_seconds=monitor_interval,
    )
    if rt_mon.get("worker_alive"):
        st.success(
            f"🟢 **Runtime Monitor: `{rt_mon.get('runtime_label', 'BACKGROUND_WORKER_ACTIVE')}`** "
            f"(Worker `{rt_mon.get('worker_id')}` | Siklus={rt_mon.get('cycles_completed', 0)} | "
            f"Heartbeat={rt_mon.get('heartbeat_age_seconds', 0.0):.1f}s lalu) — {rt_mon.get('disclaimer', '')}"
        )
    else:
        st.caption(
            f"ℹ️ **Runtime Monitor: `{rt_mon.get('runtime_label', 'SESSION_ONLY')}`** "
            f"(Mode=`{rt_mon.get('execution_mode', 'session_only')}`) — {rt_mon.get('disclaimer', '')}"
        )


    def _run_v56_monitor():
        if "v56_processed_fingerprints" not in st.session_state:
            st.session_state.v56_processed_fingerprints = set()
        with st.spinner("📡 Mencari pola BOS/CHOCH + liquidity + OB/FVG + displacement..."):
            snaps = momentum_candidates(monitor_n)
        if not snaps:
            st.warning("Belum menemukan pasangan dengan data yang cukup.")
            return
        rows = []
        for snap in snaps:
            p = snap["pattern"]
            rows.append({
                "Symbol": snap["symbol"],
                "State": p["state"],
                "Direction": p["direction"],
                "Score": p["score"],
                "5M": snap["smc"]["5m"]["timing"],
                "15M": snap["smc"]["15m"]["timing"],
                "1H": snap["smc"]["1h"]["structure"]["bias"],
                "24h": f"{snap['change']:+.2f}%",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        interesting = [x for x in snaps if x["pattern"]["state"] in {"DEVELOPING", "TRIGGERED"}]
        if not interesting:
            st.info("👀 Belum ada setup yang cukup kuat. Sistem tetap memonitor; tidak memaksa entry.")
            return

        st.markdown("### 🎯 Setup yang sedang berkembang")
        validations = 0
        for snap in interesting:
            if validations >= max_validations:
                break
            p = snap["pattern"]
            icon = "🔥" if p["state"] == "TRIGGERED" else "⚠️"
            st.markdown(f"#### {icon} {snap['symbol']} — {p['state']} {p['score']}/100 — {p['direction']}")
            st.caption(" • ".join(p["evidence"][:6]))
            event_id, already_notified = save_pattern_event(snap)
            fp = pattern_fingerprint(snap)
            if fp in st.session_state.v56_processed_fingerprints:
                st.info("Pattern ini sudah diproses pada siklus sebelumnya; menunggu perubahan struktur.")
                continue

            con = db()
            event_row = con.execute("SELECT notified FROM pattern_events WHERE id=?", (event_id,)).fetchone()
            con.close()
            if event_row and event_row["notified"] and p["state"] == "TRIGGERED":
                st.info("Pattern ini sudah divalidasi/di-alert sebelumnya; menunggu fingerprint baru.")
                continue

            if p["state"] == "TRIGGERED" and GROQ_KEYS:
                try:
                    result = run_and_store_candidate_analysis(
                        snap["symbol"],
                        p["score"],
                        state=st.session_state,
                    )
                    validations += 1
                    st.session_state.v56_processed_fingerprints.add(fp)
                    modes = result.get("modes", {})
                    cols = st.columns(3)
                    labels = [("scalping", "⚡ Scalping"), ("intraday", "📊 Intraday"), ("swing", "🌊 Swing")]
                    for col, (key, label) in zip(cols, labels):
                        m = modes.get(key, {"decision": "WAIT", "entry_action": "WAIT", "directional_bias": "UNKNOWN", "confidence": 0, "reason": "-"})
                        act = m.get("entry_action", m.get("decision", "WAIT"))
                        bias = m.get("directional_bias", "UNKNOWN")
                        card_txt = f"**{label} — Action: {act} (Bias: {bias})**\n\nModel Score: {m['confidence']:.0f}/100\n\n{m['reason']}"
                        if act == "LONG": col.success(card_txt)
                        elif act == "SHORT": col.error(card_txt)
                        else: col.warning(card_txt)

                    gate_ok = bool(result.get("gate_report", {}).get("approved", False))
                    if result["final"] in {"LONG", "SHORT"} and gate_ok:
                        st.success(f"👨‍⚖️ Final Judge: {result['final']} — {result['portfolio']['confidence']:.0f}/100 | Timing: {result['entry_status']}")
                        try:
                            save_out = save_rendered_analysis(
                                result["analysis_id"],
                                state=st.session_state,
                                tg_enabled=tg_on and not already_notified,
                            )
                            signal_id = save_out["signal_id"]
                            st.caption(f"💾 Paper trade otomatis tersimpan. ID {signal_id}")
                            if save_out["tg_sent"]:
                                mark_pattern_notified(event_id)
                                st.success("📱 Telegram: signal paper-trade terkirim.")
                            elif tg_on and not already_notified and save_out["created_new"] and save_out["tg_msg"]:
                                st.warning(f"Telegram belum terkirim: {save_out['tg_msg']}")
                        except Exception as save_err:
                            st.warning(f"Paper trade terdeteksi tetapi gagal disimpan: {_safe_ui_error(save_err)}")
                    else:
                        st.warning(f"AI memvalidasi setup tetapi belum memberi entry: {result['portfolio']['confidence']:.0f}/100")
                except Exception as e:
                    st.error(f"AI validation gagal untuk {snap['symbol']}: {_safe_ui_error(e)}")
            elif p["state"] == "TRIGGERED":
                st.warning("Setup terpicu, tetapi Groq belum tersedia. Radar tidak membuka trade otomatis.")
            else:
                st.info("Setup berkembang. AI belum dipanggil untuk menghemat token; tunggu trigger lebih kuat.")

    if monitor_auto:
        if hasattr(st, "fragment"):
            @st.fragment(run_every=f"{monitor_interval}s")
            def _auto_v56_fragment():
                _run_v56_monitor()
            _auto_v56_fragment()
        else:
            st.info("Auto monitor memerlukan Streamlit versi yang mendukung fragment. Gunakan tombol SCAN MOMENTUM di bawah.")

    if st.button("🚨 SCAN MOMENTUM SEKARANG", use_container_width=True, key="v56_manual_scan"):
        _run_v56_monitor()

    st.divider()

    # -------------------------
    # Scanner results
    # -------------------------
    if not st.session_state.scan:
        st.info(
            "Klik **SCAN MARKET** untuk mencari kandidat crypto. "
            "V5.3 tidak mengeksekusi order."
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
        st.caption(
            "Analisis AI dipisahkan dari render UI (PH01-01): interaksi widget atau tombol Simpan "
            "tidak memicu ulang panggilan LLM. Gunakan tombol eksplisit di bawah untuk menganalisis kandidat."
        )

        act_c1, act_c2 = st.columns(2)
        if act_c1.button(
            f"🧠 ANALISIS AI — TOP {top_n} KANDIDAT",
            key="btn_analyze_top",
            use_container_width=True,
        ):
            with st.spinner(f"🧠 Menjalankan analisis AI untuk Top {top_n} kandidat..."):
                try:
                    analyze_candidates_batch(
                        st.session_state.scan,
                        top_n,
                        state=st.session_state,
                        force_refresh=False,
                    )
                except Exception as e:
                    st.error(f"Gagal menjalankan analisis Top {top_n}: {_safe_ui_error(e)}")

        if act_c2.button(
            f"🔄 REFRESH ULANG AI — TOP {top_n} KANDIDAT",
            key="btn_refresh_top",
            use_container_width=True,
        ):
            with st.spinner(f"🔄 Menyegarkan ulang analisis AI untuk Top {top_n} kandidat..."):
                try:
                    analyze_candidates_batch(
                        st.session_state.scan,
                        top_n,
                        state=st.session_state,
                        force_refresh=True,
                    )
                except Exception as e:
                    st.error(f"Gagal menyegarkan analisis Top {top_n}: {_safe_ui_error(e)}")

        for rank, candidate in enumerate(
            st.session_state.scan[:top_n],
            1,
        ):
            symbol = candidate["symbol"]

            st.markdown(
                f"## #{rank} {symbol} — Scanner {candidate['score']}/100"
            )

            existing_result = get_rendered_analysis(symbol=symbol, state=st.session_state)
            btn_label = (
                f"🔄 Refresh Analisis AI {symbol}"
                if existing_result is not None
                else f"🧠 Analisis AI {symbol}"
            )
            if st.button(btn_label, key=f"analyze_{symbol}_{rank}"):
                with st.spinner(
                    f"🧠 Menjalankan analyst team + bull/bear + risk team untuk {symbol}..."
                ):
                    try:
                        existing_result = run_and_store_candidate_analysis(
                            symbol,
                            candidate["score"],
                            state=st.session_state,
                        )
                    except Exception as e:
                        st.error(f"Gagal menganalisis {symbol}: {_safe_ui_error(e)}")
                        continue

            result = get_rendered_analysis(symbol=symbol, state=st.session_state)
            if result is None:
                st.info(
                    f"⏸️ **{symbol}** belum dianalisis AI pada sesi ini. "
                    f"Klik **{btn_label}** atau **🧠 ANALISIS AI — TOP {top_n} KANDIDAT** untuk memulai."
                )
                st.divider()
                continue

            analysis_id = result["analysis_id"]
            veto_str = _resolve_veto_reasons_str(result)
            raw_dec_str = result.get("raw_model_decision", result["portfolio"]["decision"])
            st.caption(
                f"🆔 Analysis ID: `{analysis_id}` | "
                f"🔑 Setup Key: `{result.get('idempotency_key', '-')}` | "
                f"🕒 Waktu Analisis (UTC): {result.get('analyzed_at', '-')[:19].replace('T', ' ')} | "
                f"🎯 Skor saat analisis: {result.get('scanner_score', candidate['score'])}/100 | "
                f"🛡️ Raw Model: `{raw_dec_str}` → Gated: `{result['final']}` (Veto: `{veto_str}`)"
            )

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

            smc_chart = result["packet"]["smc"]
            for idx, z in enumerate(smc_chart.get("order_blocks", [])[:6]):
                fig.add_hrect(
                    y0=z["low"], y1=z["high"],
                    line_width=1, opacity=0.10,
                    annotation_text=f"OB {z['direction']}",
                    annotation_position="left",
                )
            for idx, z in enumerate(smc_chart.get("fvgs", [])[:6]):
                fig.add_hrect(
                    y0=z["low"], y1=z["high"],
                    line_width=1, opacity=0.06,
                    annotation_text=f"FVG {z['direction']}",
                    annotation_position="right",
                )

            bull_zone = result["packet"]["smc"]["bullish_entry"]
            bear_zone = result["packet"]["smc"]["bearish_entry"]
            if bull_zone["zone_low"] is not None and bull_zone["zone_high"] is not None:
                fig.add_hrect(
                    y0=bull_zone["zone_low"],
                    y1=bull_zone["zone_high"],
                    line_width=1,
                    opacity=0.12,
                    annotation_text="Bullish OB/FVG",
                    annotation_position="top left",
                )
            if bear_zone["zone_low"] is not None and bear_zone["zone_high"] is not None:
                fig.add_hrect(
                    y0=bear_zone["zone_low"],
                    y1=bear_zone["zone_high"],
                    line_width=1,
                    opacity=0.12,
                    annotation_text="Bearish OB/FVG",
                    annotation_position="bottom left",
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

            st.markdown("### 🧱 Smart Money / Entry Timing")
            smc = result["packet"]["smc"]
            sc1, sc2, sc3, sc4 = st.columns(4)
            sc1.metric("Structure", smc["structure"]["bias"])
            sc2.metric("BOS / CHOCH", f"{smc['structure']['bos']} / {smc['structure']['choch']}")
            sc3.metric("SMC Score", f"{smc['smc_score']}/100")
            sc4.metric("Timing", smc["timing"])

            z1, z2 = st.columns(2)
            with z1:
                b = smc["bullish_entry"]
                st.write(
                    f"**🟢 Bullish Entry Zone:** "
                    f"{b['zone_low'] if b['zone_low'] is not None else '-'} → "
                    f"{b['zone_high'] if b['zone_high'] is not None else '-'} | "
                    f"Quality {b['quality']}/100 | {b['status']}"
                )
            with z2:
                b = smc["bearish_entry"]
                st.write(
                    f"**🔴 Bearish Entry Zone:** "
                    f"{b['zone_low'] if b['zone_low'] is not None else '-'} → "
                    f"{b['zone_high'] if b['zone_high'] is not None else '-'} | "
                    f"Quality {b['quality']}/100 | {b['status']}"
                )
            st.caption(
                f"Liquidity sweep high={smc['liquidity']['sweep_high']} | "
                f"sweep low={smc['liquidity']['sweep_low']} | "
                f"Displacement body={smc['displacement']['body_ratio']:.2f} | "
                f"volume={smc['displacement']['volume_ratio']:.2f}x"
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

            st.markdown("### 🎯 Tiga Trading Mode (Bias vs Entry Action)")
            mode_cols = st.columns(3)
            for col, (mode_key, mode_label) in zip(
                mode_cols, (("scalping", "⚡ Scalping"), ("intraday", "📊 Intraday"), ("swing", "🌊 Swing"))
            ):
                m = result.get("modes", {}).get(
                    mode_key,
                    {
                        "decision": "WAIT",
                        "entry_action": "WAIT",
                        "directional_bias": "UNKNOWN",
                        "data_status": "INSUFFICIENT_DATA",
                        "veto_codes": ["INSUFFICIENT_DATA"],
                        "confidence": 0,
                        "reason": "Tidak tersedia",
                    },
                )
                act = m.get("entry_action", m.get("decision", "WAIT"))
                bias = m.get("directional_bias", "UNKNOWN")
                v_codes = ", ".join(m.get("veto_codes") or []) or "NONE"
                text = (
                    f"**{mode_label} — Action: {act}**\n\n"
                    f"Bias: `{bias}` | Data: `{m.get('data_status', '-')}` | Veto: `{v_codes}`\n\n"
                    f"Skor Model: {m['confidence']:.0f}/100\n\n{m['reason']}"
                )
                if act == "LONG": col.success(text)
                elif act == "SHORT": col.error(text)
                else: col.warning(text)

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
                    save_outcome = save_rendered_analysis(
                        analysis_id,
                        state=st.session_state,
                        tg_enabled=tg_on,
                    )
                    signal_id = save_outcome["signal_id"]
                    if save_outcome["created_new"]:
                        st.success(
                            f"Signal tersimpan ke memory V5.6 (Analysis ID: {analysis_id}). ID: {signal_id}"
                        )
                    else:
                        st.info(
                            f"Signal untuk setup/Analysis ID {analysis_id} sudah tersimpan sebelumnya (ID: {signal_id}). Tidak membuat duplikat."
                        )

                    if tg_on and save_outcome["created_new"]:
                        if save_outcome["tg_sent"]:
                            st.success("📱 Alert Telegram terkirim.")
                        else:
                            st.error(f"Telegram gagal: {_safe_ui_error(save_outcome['tg_msg'])}")

            else:
                if st.button(
                    f"💾 Simpan WAIT {symbol}",
                    key=f"save_wait_{symbol}_{rank}",
                ):
                    save_outcome = save_rendered_analysis(
                        analysis_id,
                        state=st.session_state,
                        tg_enabled=False,
                    )
                    signal_id = save_outcome["signal_id"]
                    if save_outcome["created_new"]:
                        st.success(
                            f"WAIT tersimpan ke memory V5.6 (Analysis ID: {analysis_id} | Veto: {veto_str}). ID: {signal_id}"
                        )
                    else:
                        st.info(
                            f"WAIT untuk setup/Analysis ID {analysis_id} sudah tersimpan sebelumnya (ID: {signal_id}). Tidak membuat duplikat."
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
               result, r_multiple, market_regime,
               smc_score, entry_status,
               raw_model_decision, veto_reasons, accounting_mode
        FROM signals
        ORDER BY id DESC
        LIMIT 100
        """
    ).fetchall()
    con.close()

    if history:
        hist_rows = []

        for x in history:
            row_keys = x.keys()
            hist_rows.append({
                "ID": x["id"],
                "Waktu": x["created_at"][:19].replace("T", " "),
                "Symbol": x["symbol"],
                "Raw Model": x["raw_model_decision"] if "raw_model_decision" in row_keys and x["raw_model_decision"] else x["final_decision"],
                "Decision": x["final_decision"],
                "Model Score": round(float(x["confidence"] or 0)),
                "Entry": x["entry"],
                "SL": x["stop_loss"],
                "TP": x["take_profit"],
                "Status": format_signal_history_status(x),
                "R (Legacy)": x["r_multiple"],
                "Veto": x["veto_reasons"] if "veto_reasons" in row_keys and x["veto_reasons"] else "NONE",
                "Regime": x["market_regime"],
                "SMC": x["smc_score"] if "smc_score" in row_keys else None,
                "Entry Status": x["entry_status"] if "entry_status" in row_keys else None,
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


if (__name__ == "__main__" or _is_streamlit_runtime()) and not os.getenv("AI_CONSENSUS_NO_AUTORUN"):
    if __name__ == "__main__" and not _is_streamlit_runtime() and parse_cli_args(sys.argv[1:]).get("is_cli_mode"):
        run_cli_entrypoint(sys.argv[1:], print_output=True)
    else:
        init_db()
        render_app()