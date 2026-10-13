"""4h Donchian breakout COMPARISON strategy, not a port of the bespoke exits.

Research-only: this strategy deliberately uses simple fee-aware Freqtrade stop handling
instead of claiming 1:1 parity with custom Trend4h's two-stage ATR runner.
Never enable live trading based on these preliminary results.
"""
from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta


class TrendBreakoutResearch(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "4h"
    can_short = False
    startup_candle_count = 100
    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    minimal_roi = {"0": 100}
    stoploss = -0.08
    trailing_stop = True
    trailing_stop_positive = 0.04
    trailing_stop_positive_offset = 0.08
    trailing_only_offset_is_reached = True

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["donchian_high_previous"] = dataframe["high"].shift(1).rolling(55).max()
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["volume"] > 0)
            & (dataframe["close"] > dataframe["donchian_high_previous"])
            & (dataframe["rsi"] > 50)
            & (dataframe["close"] > dataframe["ema50"]),
            ["enter_long", "enter_tag"],
        ] = (1, "donchian_55")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["volume"] > 0) & (dataframe["close"] < dataframe["ema50"]),
            ["exit_long", "exit_tag"],
        ] = (1, "ema50_break")
        return dataframe
