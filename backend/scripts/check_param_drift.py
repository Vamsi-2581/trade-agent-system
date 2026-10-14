"""P1 安全性核对：注入 params_schema 默认值后，各策略的实际参数是否发生变化。

resolve_params 现在给实盘也注入 schema 默认值。若某策略的 schema 默认值与
其 __init__ 里的 .get(key, fallback) 兜底不一致，实盘行为就会改变——必须逐个查清。

用法（生产容器内）：
  sudo docker exec -w /app trade_agent_sys-backend-1 \
      python /app/scripts/check_param_drift.py
"""

from __future__ import annotations

import inspect
import os
import re
import sys
from decimal import Decimal

# 让脚本能 import apps.* —— 走与 manage.py 相同的路径（容器里 __file__ 在
# /app/scripts/ 下，同一式子也成立；/app 兜底用于被拷到 /tmp 执行的场景）
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _root in (_BACKEND_ROOT, "/app"):
    if os.path.isdir(os.path.join(_root, "apps")) and _root not in sys.path:
        sys.path.insert(0, _root)
        break
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings.dev")

import django  # noqa: E402

django.setup()

from apps.strategy_engine.base import StrategyContext  # noqa: E402
from apps.strategy_engine.registry import StrategyRegistry  # noqa: E402

# 生产 live_sessions 的 config（DB 实测）
SESSIONS = [
    (
        "box_time_range_breakout_strategy",
        "SOL/USDT",
        {"lookback": 240, "exit_at_mid": 0, "position_pct": 0.3, "max_width_pct": 0.04},
    ),
    ("box_time_range_breakout_strategy", "DOGE/USDT", {}),
    ("box_time_range_breakout_strategy", "BNB/USDT", {"exit_at_mid": False}),
    ("momentum_mean_reversion", "ETH/USDT", {}),
    ("mean_reversion_optimized", "BTC/USDT", {"position_pct": 0.5}),
]


def main():
    StrategyRegistry.discover()
    registered = set(StrategyRegistry.list_registered())

    for name, symbol, config in SESSIONS:
        print(f"\n@@ {name}  {symbol}  config={config}")
        if name not in registered:
            print("   (未注册，跳过)")
            continue
        cls = StrategyRegistry.get_class(name)
        schema = getattr(cls, "params_schema", {}) or {}

        # 旧口径：只 update(config)
        old = dict(config)
        # 新口径：schema 默认值 ⊕ config
        new = {
            k: (v.get("default") if isinstance(v, dict) else v)
            for k, v in schema.items()
        }
        new.update(config)

        added = {k: v for k, v in new.items() if k not in old}
        changed = {k: (old[k], new[k]) for k in old if k in new and old[k] != new[k]}
        print(f"   schema 键数={len(schema)}  旧={len(old)}  新={len(new)}")
        print(f"   新增注入的键: {added if added else '无'}")
        print(f"   值发生变化的键: {changed if changed else '无'}")

        # 真正实例化两种口径，比较策略读到的属性
        try:
            src = inspect.getsource(cls.__init__)
        except (OSError, TypeError):
            src = ""
        gets = dict(re.findall(r'\.get\(\s*"([a-z_]+)"\s*,\s*([^)]+)\)', src))
        print(f"   __init__ 里的 .get(键, 兜底) 共 {len(gets)} 处")

        def build(params):
            ctx = StrategyContext(
                symbol=symbol,
                timeframe="4h",
                mode="live",
                params=params,
                balance=Decimal("10000"),
                position=Decimal("0"),
            )
            return cls(ctx)

        try:
            s_old, s_new = build(old), build(new)
        except Exception as e:
            print(f"   实例化失败: {type(e).__name__}: {e}")
            continue

        diffs = []
        for attr in sorted(vars(s_old)):
            a, b = getattr(s_old, attr), getattr(s_new, attr)
            if isinstance(a, Decimal) and isinstance(b, Decimal):
                same = a == b
            else:
                try:
                    same = a == b
                except Exception:
                    same = True
            if not same:
                diffs.append((attr, a, b))
        print(f"   >>> 实例属性差异: {len(diffs)}")
        for attr, a, b in diffs:
            print(f"       {attr}: 旧={a}  新={b}")


if __name__ == "__main__":
    main()
