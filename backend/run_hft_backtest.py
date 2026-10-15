"""
高频交易策略回测脚本 (高性能优化版)
修复了 BacktestEngine 中 O(N^2) 的历史数据切片问题。
"""

import asyncio
import os
import json
from decimal import Decimal

print("STEP 0: Starting script...", flush=True)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings.dev")

print("STEP 1: Importing django...", flush=True)
import django
django.setup()

print("STEP 2: Importing project modules...", flush=True)
from apps.strategy_engine.base import StrategyContext
from apps.strategy_engine.registry import StrategyRegistry

print("STEP 3: Defined constants.", flush=True)
SYMBOL = "ETH/USDT"
TIMEFRAME = "5m"
INITIAL_CAPITAL = Decimal("10000")
COMMISSION_RATE = Decimal("0.001")
STRATEGIES = ["mean_reversion", "momentum_breakout", "vwap_cross"]
PARAMS = {
    "mean_reversion": {"quantity": 0.1, "bb_period": 20, "rsi_period": 14},
    "momentum_breakout": {"quantity": 0.1, "donchian_period": 20, "volume_multiplier": 1.5},
    "vwap_cross": {"quantity": 0.1},
}

async def run_optimized_backtest(strategy_name, ohlcv_data):
    print(f"STEP 6: Optimized backtest for {strategy_name}...", flush=True)
    StrategyRegistry.discover() # Only needed once, but safe to call
    strategy_cls = StrategyRegistry.get(strategy_name)
    
    ctx = StrategyContext(symbol=SYMBOL, timeframe=TIMEFRAME, mode="backtest", params=PARAMS[strategy_name], balance=INITIAL_CAPITAL)
    strategy = strategy_cls(ctx)
    strategy.on_start()
    
    cash = INITIAL_CAPITAL
    position = Decimal("0")
    avg_entry = Decimal("0")
    trades = []
    
    # 优化：只保留最近 200 根 K 线用于指标计算，避免 O(N^2) 性能问题
    MAX_HISTORY = 200 
    
    for i, kline in enumerate(ohlcv_data):
        if i % 5000 == 0:
            print(f"  Progress: {i}/{len(ohlcv_data)} bars", flush=True)
        # 动态滑动窗口
        start = max(0, i - MAX_HISTORY + 1)
        history = ohlcv_data[start : i + 1]
        
        # 策略逻辑
        signal = strategy.on_bar(kline, history)
        if signal:
            close_price = Decimal(str(kline["close"]))
            if signal.side == "buy" and position == 0:
                cost = signal.quantity * close_price
                comm = cost * COMMISSION_RATE
                if cost + comm <= cash:
                    cash -= (cost + comm)
                    avg_entry = cost / signal.quantity
                    position = signal.quantity
                    ctx.balance = cash
                    ctx.set_position(SYMBOL, position)
            elif signal.side == "sell" and position > 0:
                qty = min(signal.quantity, position)
                proceeds = qty * close_price
                comm = proceeds * COMMISSION_RATE
                pnl = (close_price - avg_entry) * qty - comm
                cash += (proceeds - comm)
                position -= qty
                trades.append({"entry": float(avg_entry), "exit": float(close_price), "pnl": float(pnl)})
                ctx.balance = cash
                ctx.set_position(SYMBOL, position)

    # 强制平仓
    if position > 0:
        last_close = Decimal(str(ohlcv_data[-1]["close"]))
        pnl = (last_close - avg_entry) * position - (position * last_close * COMMISSION_RATE)
        cash += position * last_close * (1 - COMMISSION_RATE)
        trades.append({"entry": float(avg_entry), "exit": float(last_close), "pnl": float(pnl)})
        position = Decimal("0")
        
    strategy.on_stop()
    
    final_equity = cash
    total_return = float(((final_equity - INITIAL_CAPITAL) / INITIAL_CAPITAL) * 100)
    wins = len([t for t in trades if t["pnl"] > 0])
    
    print(f"\n=== {strategy_name} 完成 ===")
    print(f"总收益率:   {total_return:+.2f}%")
    print(f"最终资金:   ${final_equity:,.2f}")
    print(f"总交易次数: {len(trades)}")
    print(f"胜率:       {wins/len(trades)*100:.1f}%" if trades else "胜率: 0%")

async def main():
    print("MAIN: Starting main function!", flush=True)
    path = "/root/.tradelogx/data/eth_5m.json"
    print(f"STEP 4: Reading JSON from {path}...", flush=True)
    with open(path, "r") as f:
        ohlcv_data = json.load(f)
    print(f"STEP 5: Loaded {len(ohlcv_data)} candles.", flush=True)
    
    for s_name in STRATEGIES:
        await run_optimized_backtest(s_name, ohlcv_data)
    print("ALL DONE!", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
