"""Fetch ETH/USDT 5m data for backtest (run on host)"""
import asyncio
import json
from datetime import datetime, timedelta

import ccxt.async_support as ccxt

SYMBOL = "ETH/USDT"
TIMEFRAME = "5m"
DAYS = 90

async def main():
    proxy = "socks5://host.docker.internal:7890"
    exchange = ccxt.binance({
        "enableRateLimit": True,
        "aiohttp_proxy": proxy
    })
    
    since_ms = int((datetime.now() - timedelta(days=DAYS)).timestamp() * 1000)
    all_candles = []
    current_since = since_ms
    
    while True:
        candles = await exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, since=current_since, limit=1000)
        if not candles:
            break
        all_candles.extend(candles)
        current_since = candles[-1][0] + 1
        if current_since > int(datetime.now().timestamp() * 1000):
            break
            
    await exchange.close()
    
    result = []
    for c in all_candles:
        result.append({
            "timestamp": datetime.fromtimestamp(c[0] / 1000).isoformat(),
            "open": float(c[1]), "high": float(c[2]),
            "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])
        })
    
    with open("/root/.tradelogx/data/eth_5m.json", "w") as f:
        json.dump(result, f)
    print(f"Fetched {len(result)} candles to /root/.tradelogx/data/eth_5m.json")

asyncio.run(main())
