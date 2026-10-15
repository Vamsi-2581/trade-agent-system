---
name: create-strategy
description: 创建量化交易策略 — 通过对话了解需求，生成继承自 BaseStrategy 的策略代码文件
when_to_use: 用户要求"创建策略"、"编写策略"、"新建一个交易策略"时激活
autonomous: false
---

# 创建策略技能

通过结构化对话理解用户需求，生成继承自 `BaseStrategy` 的量化交易策略。

## 核心原则

> 以下原则在 `autonomous: false` 时适用。autonomous 模式见下一节。

1. **先对话，后编码** — 必须通过提问确认需求，不能直接生成代码
2. **确认后再生成** — 需求不完整时持续追问，完整后给出总结并确认
3. **自动生成策略名** — 根据策略逻辑自动生成 `{indicator1}_{indicator2}_strategy` 格式的名称，无需用户确认
4. **输出到统一策略目录** — 生成到 `~/.tradelogx/strategies/` 目录

## Autonomous 模式

由 frontmatter 的 `autonomous` 字段控制（默认 `false`）。

| 阶段 | 默认行为 | `autonomous: true` |
|------|----------|-------------------|
| 0. 检查相似策略 | 询问是否使用 | 告知已存在，**流程结束** |
| 1. 需求收集 | 对话逐轮收集 | **跳过**，从初始消息推断，缺失用默认值（symbol=BTC/USDT, timeframe=1d, exchange=binance, 止损=2%, 止盈=5%, 仓位=10%, 不做空, 指标参数=行业标准值） |
| 2. 确认需求 | 汇总后等确认 | **跳过**，直接阶段 3 |
| 3. 生成代码 | 同默认 | 同默认 |
| 4. 测试策略 | 同默认 | 同默认 |
| 5. 登记为候选 | 同默认 | 同默认 |
| 完成/失败 | 通知用户 | 通知用户 |

**不跨越 skill 边界自动触发其他 skill。**

## 策略命名规则

**格式**：`{核心指标1}_{核心指标2}_strategy`，由 agent 根据策略使用的指标自动生成：

| 策略逻辑 | strategy_name |
|---------|---------------|
| RSI 超买超卖 + EMA 均线交叉 | `rsi_ema_strategy` |
| 布林带 + RSI 超卖 | `bollinger_rsi_strategy` |
| MACD 交叉 + ATR 止损 | `macd_atr_strategy` |
| 双均线交叉 | `ma_cross_strategy` |
| 仅 RSI 超买超卖 | `rsi_strategy` |

**命名优先级**：取最重要的 1-2 个指标用 `_` 连接 → 辅以策略类型（`breakout`/`regression`/`cross`）→ 加 `_strategy` 后缀 → 全部小写英文。

## 工作流程

### 阶段 0：检查是否已存在相似策略

在开始需求收集前，先检查系统中是否已有匹配的策略：

1. **获取所有策略列表**：调用工具 `list_strategies()`（即 `StrategyRegistry.list_registered_with_descriptions()`），返回所有策略的 name + description。若注册表为空，用 `list_directory(path="strategies/")` 查看 `~/.tradelogx/strategies/` 下的已有策略文件，并 `read_file(file_path="strategies/<文件名>", agent_name="quant")` 读取至少一个作为真实 API 结构参照 — 不要从网络搜索结果猜测文件名。

2. **LLM 语义匹配**：将用户意图和策略列表传给 LLM，判断是否已存在相似策略：

   ```
   用户意图：{用户的描述，如 "RSI 均值回归策略"}
   
   已有策略：
   {策略列表 JSON}
   
   请判断是否有匹配的策略。
   返回 JSON 格式：{"name": "策略名", "score": 0-100}
   如果没有匹配的（score < 80），返回 {"name": null, "reason": "原因"}
   ```

3. **判断结果**：
   - 如果 `score >= 80`，告知用户：
     > 已找到相似策略：{name}
     > {description}
     > 
     > 是否直接使用此策略？如需创建新版本，请继续。
     
     <!-- autonomous=true: 告知用户已存在相似策略后直接结束流程，不询问 -->
   - 如果 `score < 80` 或 `name = null`，进入阶段 1 需求收集。

### 阶段 1：需求收集（对话轮次）

<!-- autonomous=true: 跳过本阶段，从用户初始消息推断需求，缺失参数用"Autonomous 模式"章节中的默认值填充，然后直接进入阶段 3 -->

通过提问收集以下信息，每轮聚焦 1-2 个维度：

- **1.1 基本信息**：核心策略逻辑（一句话，agent 据此推导 strategy_name）、交易品种（单/多品种）、交易周期（1h/4h/1d）
- **1.2 交易逻辑**：入场条件、出场条件（止盈/止损/信号反转）、仓位管理（固定数量/百分比/动态）、是否做空
- **1.3 技术指标**：使用哪些指标（RSI/MACD/MA/布林带/ATR）及其参数（周期、阈值）
- **1.4 风控规则**：止损比例、止盈比例、单日最大亏损限制

### 阶段 2：确认需求

<!-- autonomous=true: 跳过本阶段，直接进入阶段 3 生成代码 -->

生成代码前，总结需求并明确确认。**必须等待用户确认后才能生成代码。**

```
我理解你的策略需求如下：

- **策略名称**：`xxx_strategy`（自动生成）
- **交易品种**：`BTCUSDT`
- **交易周期**：`1h`
- **入场逻辑**：...
- **出场逻辑**：...
- **风控**：...
- **参数**：...

请确认是否正确，我将开始生成策略代码。
```

### 阶段 3：生成策略代码

1. **先生成 description**（必须，注册时会校验）— 按 4 字段模板填写策略描述：
   ```python
   description = """策略类型：{均值回归/趋势跟踪/突破/动量/套利}
   核心指标：{RSI/MACD/布林带/均线/ATR/...，只写指标名，不写具体参数值}
   适用场景：{震荡行情/趋势行情/高波动/低波动/...}
   入场逻辑：{一句话概括买入条件，不含具体参数值}"""
   ```
   **示例**：
   ```python
   description = """策略类型：均值回归
   核心指标：RSI
   适用场景：震荡行情，不适合单边趋势
   入场逻辑：RSI 跌破超卖阈值且出现反转信号时买入"""
   ```

2. **分析入场条件** — 从确认的需求中提取主要指标、触发条件、K 线周期

3. **推导 watch signals** — 按 **`references/watch-signals.md`** 的规则，将入场条件映射为最小周期信号（**必须实现，禁止返回 `[]`**）

4. **生成完整代码** — 复制 **`references/strategy-template.md`** 模板，按需填充；API 细节查 **`references/api-reference.md`**。代码须含 `description`、`on_bar`、`get_watch_signals`、`on_start`、`on_stop`

**输出路径**：`~/.tradelogx/strategies/{strategy_name}.py`，调用 `write_file`：

```
write_file(
    file_path="~/.tradelogx/strategies/{strategy_name}.py",
    agent_name="quant",
    content="<策略代码>"
)
```

生成后**逐条核对下方"生成代码必查清单"，在输出中列出每条的 ✓/✗ 结果**，全部通过后才能进入阶段 4。

### 阶段 4：测试策略

代码写入后，**必须**调用 `test_strategy` 验证：

```
test_strategy(
    strategy_name="{strategy_name}",
    strategy_file_path="strategies/{strategy_name}.py"
)
```

`test_strategy` 自动验证：语法检查、静态分析（Decimal 混用、不存在的 ctx 属性如 `portfolio_value`/`equity`/`cash`）、结构验证、`@register_strategy()` 注册检查、200 根模拟 K 线回测、零交易检测、仓位验证（quantity ≤ 0 / 资金不足 / 使用率过低）。

测试失败（`success: false`）时：分析错误类型 → 修改代码 → 重新 `test_strategy` → 重复直到通过。

### 阶段 5：登记为候选（收尾）

`test_strategy` 通过后，**必须**调用 `register_strategy_candidate` 把这份代码登记成一条候选策略，之后由流水线接管：

```
register_strategy_candidate(
    strategy_name="{strategy_name}",
    strategy_file_path="strategies/{strategy_name}.py",
    hypothesis="<一句话假设：这条策略认为市场会怎样、靠什么赚钱>",
    origin="agent"
)
```

**为什么必须登记，而不是只告诉用户名字**：登记之前，这份代码只是磁盘上的一个文件；登记之后它才有身份，才能被流水线自动推进——样本内回测（夏普 ≥1.5 且样本量达标）→ 样本外 walk-forward 分折（衰减 <30%）→ paper 观察期。**不要再让用户手工拿 strategy_name 去回测**，那是登记要取代的手工串联。

登记返回的 `next_step` 字段就是「下一步会自动做什么」，**照实转述给用户**，不要自己编。

登记失败（`success: false`）时：返回里带 `reason_code` / `reason` / `hint` 三件，**按 `hint` 修**——`hint` 就是给生成侧自我修正用的。典型三类：
- `description_template_missing` → `description` 缺「策略类型：」「核心指标：」「适用场景：」「入场逻辑：」中的某个字段（阶段 3.1 的模板）
- `strategy_name_mismatch` / `strategy_name_missing` → 文件里 `name = "..."` 与 `strategy_name` 不一致，或类里根本没写 `name`
- `syntax_error` / `module_load_error` / `registry_key_unresolved` → 语法、导入路径或 `@register_strategy()` 装饰器的问题

失败**也会留下一条 `registration_failed` 的候选行**（带原因，可在对话里查到）。修好后用同样的参数重新登记即可回到 `registered`。

登记成功后告知用户：候选 id、状态、文件路径、模拟运行统计（信号数/买卖次数/收益率）、warning 修复建议，以及 `next_step` 里那句「接下来会自动做什么」。

## 导入路径（必须遵守）

**所有策略必须使用以下精确导入，禁止任何变体：**

```python
from apps.strategy_engine.base import BaseStrategy, StrategyContext
from apps.strategy_engine.indicators import rsi, sma, ema, macd, bollinger, atr, stoch
from apps.strategy_engine.registry import register_strategy
```

**禁止**（会导致 `No module named`）：`tradelogx.*`、不带 `apps.` 前缀的 `strategy_engine.*`、任何其他路径。不确定时一律用上方模板。

## 生成代码必查清单（95% 回测 bug 由此引起）

逐条核对，任一不满足 = 代码不合格：

1. **on_bar 签名** 必须是 `def on_bar(self, kline: dict, history: list[dict]) -> OrderSignal | None`。`history` 是参数传入的历史 K 线列表（含当前），**禁止在 on_bar 内调用 `ctx.history()`**。

2. **必须 return 信号** — `self.ctx.buy()` / `sell()` / `close_position()` 必须作为返回值返回，仅调用不 return = 0 交易。
   ```python
   if should_buy:
       return self.ctx.buy(quantity=qty, signal_name="entry")  # ✅ 不能漏掉 return
   ```

3. **必须有 `@register_strategy()` 装饰器** — 否则回测引擎找不到策略。

4. **路径依赖指标**（EMA/ATR）必须用有限回溯窗口 `history[-period*5:]`，禁止对全部 `history` 计算 — 否则不同回测长度信号不一致（1年6笔、2年4笔）。SMA/RSI/Bollinger/Donchian 无此限制。详见 `references/api-reference.md`。
   ```python
   _lookback = min(self.ema_period * 5, len(history))
   ema_array = ema(history[-_lookback:], period=self.ema_period)
   ```

5. **Portfolio 与手动 quantity 二选一** — 设了 `self.portfolio = PctCapitalPortfolio()` 就不要手动算 quantity（会被静默覆盖）；默认不设 portfolio、手动计算直接生效。详见 `references/api-reference.md`。

6. **指标返回 np.ndarray，必须 `float(arr[-1])`** 取值后才能比较，直接与标量比较会抛 `ValueError`。先 `len(arr)==0` 检查空数组。

7. **`get_watch_signals` 必须实现**，禁止保留 `return []`。每个 dict 含 `interval`/`indicator_type`/`indicator_params`/`condition`/`trigger_type` 五字段。规则见 `references/watch-signals.md`。

其余约定：参数从 `self.ctx.params` 读取、`params_schema` 定义可调参数、指标计算前检查 `len(history)`、买入前查 `position == 0`、卖出前查 `position > 0`、用 `Decimal` 处理数量价格、信号命名用 `entry`/`exit`/`stop_loss`/`take_profit`、含 docstring。

## 参考文件

阶段 3 生成代码时按需读取。

> **路径解析**：技能上下文开头会给出「技能目录」路径（read_file 用，相对 `~/.tradelogx/`）。
> 下表中的相对路径均基于技能目录，例如：
> `read_file(file_path="<技能目录>/references/strategy-template.md", agent_name="quant")`
> 不要把裸 `references/xxx.md` 直接传给 `read_file`（会解析到 `~/.tradelogx/references/` 导致文件不存在）。

| 文件 | 内容 | 何时读 |
|------|------|--------|
| `<技能目录>/references/strategy-template.md` | 完整 Python 文件模板（含内联陷阱注释） | 阶段 3.3 起手 |
| `<技能目录>/references/api-reference.md` | StrategyContext API、仓位计算三法、指标回溯窗口、指标列表、ndarray 处理、OrderSignal | 填充逻辑时查 |
| `<技能目录>/references/watch-signals.md` | watch signals 推导规则、格式要求、必须实现的自查 | 阶段 3.2 推导信号时 |
