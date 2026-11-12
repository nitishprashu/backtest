"""optbt - 1-minute options backtesting engine (StockMock-style), index-agnostic."""
from .analytics import build_result
from .data import DataStore, DayData, load_breeze_options_dir, load_options, load_spot
from .engine import run_backtest, simulate_day
from .expiry import choose_expiry
from .explorer import intraday_path
from .indicators import supertrend, vwap
from .signal_engine import run_signal_backtest, simulate_signal_day
from .signals import (EMASignalMode, EMASource, EMAParams, MACDParams, MovingAverageType, OppositeSignal, OptionExpression,
                      SignalStrategyConfig, SupertrendParams, VwapParams, VwapSource)
from .instruments import DEFAULT_REGISTRY, NIFTY, InstrumentRegistry, InstrumentSpec
from .schemas import (ATMReference, BacktestResult, ExitReason, ExpirySelect, LegConfig, OptionType,
                      EntryCondition, HoldingMode, PortfolioTrail, ReEntryMode, ReEntryRule, RiskRule,
                      RolloverStrikeMode, Side, SlippageModel, StrategyConfig,
                      StrikeMode, StrikeSelection, TrailRule, ValueType)
from .greeks import (GreekSettings, b76_price, chain_greeks_at, estimate_carry, greeks_at,
                     greeks_from_iv, greeks_series, implied_vol, tte_years)

__all__ = [n for n in dir() if not n.startswith("_")]
