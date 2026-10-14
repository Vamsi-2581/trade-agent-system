"""扫描全部已注册策略：params_schema 默认值 vs __init__ 兜底值是否分家。

P1 让实盘也注入 schema 默认值。若某策略的 schema 默认值与其 __init__ 里的
.get(key, fallback) 兜底**不一致**，该策略的实盘行为会因这次修复而改变——
这正是需要提前知道的事（可能是修复，也可能是意外）。

用法（生产容器内）：
  sudo docker exec -w /app trade_agent_sys-backend-1 \
      python /app/scripts/scan_schema_drift.py
"""

from __future__ import annotations

import inspect
import os
import re
import sys

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

from apps.strategy_engine.registry import StrategyRegistry  # noqa: E402


def parse_fallbacks(src: str) -> dict[str, str]:
    """抽 .get("key", fallback) 的键与兜底字面量（粗解析，仅用于比对）。"""
    out = {}
    for m in re.finditer(r'\.get\(\s*"([a-z_]+)"\s*,\s*([^)\n]+?)\s*\)', src):
        out[m.group(1)] = m.group(2).strip()
    return out


def norm(v) -> str:
    """把 schema 默认值与兜底字面量归一成可比较的字符串。"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "none"
    s = str(v).strip()
    s = s.replace("Decimal(", "").replace("str(", "")
    s = s.strip("()\"' ")
    if s.lower() in ("true", "false"):
        return s.lower()
    return s


def main():
    StrategyRegistry.discover()
    names = sorted(StrategyRegistry.list_registered())
    print(f"@@ 已注册策略 {len(names)} 个\n")

    total_mismatch = 0
    for name in names:
        try:
            cls = StrategyRegistry.get_class(name)
        except Exception as e:
            print(f"@@ {name}: 取类失败 {e}")
            continue
        schema = getattr(cls, "params_schema", {}) or {}
        if not schema:
            continue
        try:
            src = inspect.getsource(cls.__init__)
        except (OSError, TypeError):
            src = ""
        if not src:
            continue
        fb = parse_fallbacks(src)

        mismatches = []
        for key, spec in schema.items():
            if not isinstance(spec, dict) or "default" not in spec:
                continue
            if key not in fb:
                continue
            a, b = norm(spec["default"]), norm(fb[key])
            # 数值宽松比较
            try:
                if float(a) == float(b):
                    continue
            except (TypeError, ValueError):
                pass
            if a != b:
                mismatches.append((key, spec["default"], fb[key]))

        if mismatches:
            total_mismatch += len(mismatches)
            print(
                f"@@ {name}  ★ schema 默认值 与 __init__ 兜底 分家 {len(mismatches)} 处"
            )
            for key, sd, fbv in mismatches:
                print(f"     {key}: schema 默认={sd!r}  __init__ 兜底={fbv}")

    print(f"\n@@ 合计分家处数: {total_mismatch}")
    if total_mismatch == 0:
        print("@@ ⇒ P1 对现有全部策略是行为保持的（注入值与兜底值一致）")


if __name__ == "__main__":
    main()
