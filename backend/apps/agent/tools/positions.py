"""仓位操作工具：查持仓（只读）+ 平仓/减仓（真实下单）。

## 这一层回答什么

2026-09-28 事故：用户说「平掉当前 BNB 的仓位」，supervisor 给 ``submit_scheduled_task``
填了 ``agent_name='trader'``——系统里**根本没有** trader 这个 Agent，也没有任何下单/平仓
能力，于是用户先收到一条「已提交平仓指令」，一分钟后才收到 ``Agent not registered``。
``schedule_task`` 的校验把这类「注定失败的任务」拦在写库之前，但那只是止血：**根因是系统
没有这个能力**。本模块补上它。

两个工具复用 ``apps/trading`` 已经铺好的机械层，**一行下单逻辑都不新写**：

* ``list_positions``  只读。列全部活跃账户的持仓，口径与既有 ``list_exchange_accounts`` 一致。
* ``reduce_position`` 写。**默认干跑**（``confirmed=false`` 只回一段「将执行」摘要），
  ``confirmed=true`` 才真的下一张 ``reduce_only`` 市价单。

## 为什么下单走 ``reduce.place_reduce_shard`` 而不是自己拼 ``OrderRequest``

那张单需要的四件东西——**只减不增**、**确定的重发锚点**、**「已受理」追问到终态**、
**「没有结论」绝不降级成「确定没成交」**——全部已经在那里实现并被实盘事故打磨过
（2026-09-26：一张全额成交的子单曾被记成 ``rejected``）。在这里重写一遍，就是把那份
教训重新犯一次的入口。手动平仓与自动减仓在**传输层**是同一件事，所以它们共用同一张单。

分片（``plan_shards``）**不参与**：分片是为熔断的滑点上限服务的（一笔大市价单的冲击），
而手动平仓/减仓的语义就是「一张单」，交易所的 ``reduceOnly`` 保证它不会反向开仓。

## 适配器按**账户**建，不按交易所名

``reduce.py`` 的模块 docstring 已经写明理由：机械层要能在没有框架单例的情况下单独跑，
而「哪个账户」才是适配器的唯一键（``OrderExecutor.resolve_adapter`` 是同一个判据）。
所以这里一律走 ``pending_reconcile.build_account_adapter``：它按 ``ExchangeAccount`` 建、
自己 ``connect()``、缺密钥/缺适配器时返回 ``None``（那与「确实空仓」必须分开说）。

## 四道安全门（真实资金操作，fail-closed）

0. **身份**：``user_id`` 为空一律拒（同 ``list_exchange_accounts`` / ``list_orders``）。
1. **账户唯一化**：解析不出唯一账户就**报错并列出候选**，绝不默认挑一个——减错账户
   不可逆。解析结果若归属**另一个**用户，拒（跨用户护栏）。
2. **确认**：``confirmed=false`` 不下单，只回摘要。即使 LLM 幻觉，首调也不产生订单。
   ``confirmed`` 走 ``_confirmed()`` **fail-closed**：降级 DSML 路径把参数传成字符串时
   ``bool("false") is True``，照单全收等于把「不要下单」读成「已确认」——只认明确的
   肯定字面量，其余一律干跑。
3. **持仓/方向**：读不到真实持仓 → 不下单（不知道持有多少时绝不下单）；方向认不出
   （非 ``long``/``short``）→ 拒单。数量取整与最小量校验交给适配器
   （``binance.place_order`` 已按 ``stepSize`` 向下取整、按 ``minQty`` 校验，不达标抛
   ``ValueError`` → ``reduce._retryable`` 判为不可重试 → 干净的 ``REJECTED``）。

## ``user_id`` 的口径

从工具调用拿到的 ``user_id`` 是**渠道身份**（WebSocket 的 User UUID / Telegram 的
``telegram_id`` 数字串 / 飞书的 ``ou_*``），不是 Django User 的 pk 本身。归属比对因此走
``user_resolver.resolve_user_id`` 解析一次，而不是直接建 ``User(pk=user_id)``——后者在
Telegram 下会拿一个数字串去当 UUID 解析并抛错。解析不出来时只跳过归属比对（未归属的账户
本来就放行），但工具入口的「身份非空」仍然要求。

## 留痕

``reduce_run`` 的账本是 ``RegimeReduceRecord``，它强绑 ``event_id``——手动平仓没有事件可挂，
所以 v1 不写那个模型（见 CONTEXT.md 对「减仓由一次性任务投递触发源」的定义）。本模块的
留痕是：结构化日志 + 工具返回明细 + ``reduce_position`` 成功后经 ``notify_user`` 落一行
``Notification``。需要独立审计模型时再补，不在本次。
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal, InvalidOperation

from apps.core.db_utils import db_async

from .base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 账户解析
# --------------------------------------------------------------------------- #


def _load_active_accounts() -> list:
    """全部 ``is_active`` 账户。**不过滤 user**，与 ``list_exchange_accounts`` 同一约定。"""
    from apps.exchange.models import ExchangeAccount

    return list(ExchangeAccount.objects.filter(is_active=True).order_by("-created_at"))


def _account_title(account) -> str:
    """账户在文字里的自称：label + 交易所 + testnet 标记 + id 前 8 位。"""
    label = account.label or "未命名"
    mark = "（测试网）" if account.testnet else ""
    return f"{label}｜{account.exchange}{mark}｜账户 {str(account.id)[:8]}"


def _resolve_accounts(accounts: list, raw: str) -> tuple[list, str]:
    """在活跃账户里解析用户说的那一个。``([], 错误)` / `([账户], "")` / `([]列表, "")`。

    退化顺序与 ``list_exchange_accounts`` 的既有手法一致：**先 label 精确**（``iexact``），
    没有再 ``icontains`` 模糊；UUID 精确匹配与它们并列。

    返回**列表**而不是单个账户，是因为 ``account`` 没给且系统里有多个活跃账户时，
    正确行为是「让用户指定」，而不是替用户挑一个——挑错了就是减错账户，而那条路不可逆。
    """
    raw = (raw or "").strip()
    if not raw:
        return list(accounts), ""

    exact_label = [a for a in accounts if (a.label or "").lower() == raw.lower()]
    candidates = exact_label or [
        a for a in accounts if raw.lower() in (a.label or "").lower()
    ]

    try:
        as_uuid = uuid.UUID(raw)
    except (ValueError, TypeError):
        as_uuid = None
    if as_uuid is not None:
        by_id = [a for a in accounts if a.id == as_uuid]
        # UUID 精确命中优先于 label 模糊：用户给的是一个标识符时，它指向谁就是谁。
        if by_id:
            return by_id, ""
        # 形如 UUID 却在账户里找不到 → 别退化成「拿它当 label 关键词乱撞」
        if not candidates:
            return [], f"没有找到 id 为 {raw} 的活跃交易所账户。"

    if not candidates:
        known = "、".join((a.label or "未命名") for a in accounts) or "（无活跃账户）"
        return [], f"没有找到匹配「{raw}」的活跃交易所账户。当前账户：{known}"
    return candidates, ""


async def _resolve_owner_id(user_id: str) -> str:
    """渠道身份 → Django User 的 pk 字符串；解析不出返回空串。

    ``resolve_user_id`` 是仓库里唯一干这件事的地方（它认得 UUID / 数字 telegram_id /
    ``ou_*``）。直接 ``uuid.UUID(user_id)`` 在 Telegram 下会抛错并让整条归属护栏失效，
    所以这里必须走它。
    """
    try:
        from apps.agent.user_resolver import resolve_user_id

        resolved = await resolve_user_id(user_id)
    except Exception as exc:  # noqa: BLE001 — 解析失败只让护栏退化为「不比对」
        logger.warning("[Positions] 解析调用人身份失败 user=%s：%s", user_id, exc)
        return ""
    return str(resolved) if resolved else ""


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #


def _decimal_text(value) -> str:
    """数量/价格的文字形态：取不到就是 ``N/A``，**绝不写成 0**（同 ``reduce._price``）。

    用 ``format(..., "f")`` 而不是 ``str()``/``normalize()``：后两者会把 10 印成 ``1E+1``、
    610 印成 ``6.1E+2``（``Decimal`` 的科学计数法），而这是要给人看的一行字。
    """
    if value is None:
        return "N/A"
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except (InvalidOperation, ValueError):
        return "N/A"


def _position_lines(account, positions: list) -> list[str]:
    """一个账户的持仓行。空列表的含义只能是「确实空仓」。"""
    if not positions:
        return [f"- {_account_title(account)}：没有持仓"]
    lines = [f"- {_account_title(account)}："]
    for position in positions:
        symbol = getattr(position, "symbol", "") or "?"
        side = {"long": "多", "short": "空"}.get(
            str(getattr(position, "side", "") or "").lower(),
            f"未知（{getattr(position, 'side', '')!r}）",
        )
        lines.append(
            f"    {symbol}｜{side}｜数量 {_decimal_text(getattr(position, 'quantity', None))}"
            f"｜开仓价 {_decimal_text(getattr(position, 'entry_price', None))}"
            f"｜标记价 {_decimal_text(getattr(position, 'mark_price', None))}"
            f"｜未实现盈亏 {_decimal_text(getattr(position, 'unrealized_pnl', None))}"
        )
    return lines


# --------------------------------------------------------------------------- #
# list_positions
# --------------------------------------------------------------------------- #


class ListPositionsTool(BaseTool):
    """查询交易所账户的真实持仓（只读，不碰任何写接口）。"""

    name = "list_positions"
    description = (
        "查询交易所账户当前的真实持仓（数量、方向、开仓价、标记价、未实现盈亏）。"
        "持仓只存在于交易所侧，本工具实时向交易所拉取，读的是唯一真相。"
        "当用户询问持仓、仓位、盈亏情况时使用。"
        "可选 account 参数指定账户（标签或 id 前若干位）；不指定则列出所有活跃账户。"
    )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "account": {
                    "type": "string",
                    "description": (
                        "可选，指定交易所账户（标签或账户 id）。"
                        "不填则列出所有活跃账户的持仓。"
                    ),
                },
            },
            "required": [],
        }

    async def _collect(self, accounts: list) -> tuple[list[str], int]:
        """逐个账户取持仓。返回（文本行，取不到持仓的账户数）。

        适配器**借了就还**（``finally: disconnect()``），与 ``reduce_run._run_account``
        同款生命周期：适配器按事件循环持 HTTP 客户端，不能把连接留到下一次调用。

        连不上 / 无适配器 / 拉取失败三条路都归到「取不到」，且**逐条说明原因**——它们与
        「这个账户确实没有持仓」是两件事，后者是一句可以据以行动的结论，前者不是。
        """
        from apps.trading.pending_reconcile import build_account_adapter

        lines: list[str] = []
        unavailable = 0
        for account in accounts:
            try:
                adapter = await build_account_adapter(account)
            except Exception as exc:  # noqa: BLE001 — 一个账户连不上不掀掉整次查询
                unavailable += 1
                lines.append(
                    f"- {_account_title(account)}：**持仓取不到**"
                    f"（适配器无法建立连接：{type(exc).__name__}: {exc}）"
                )
                continue
            if adapter is None:
                unavailable += 1
                lines.append(
                    f"- {_account_title(account)}：**持仓取不到**"
                    "（无该交易所适配器，或账户缺少 API 密钥）。**这不代表没有持仓。**"
                )
                continue
            try:
                positions = await adapter.get_positions()
            except Exception as exc:  # noqa: BLE001 — 一次拉取失败就是「没问出来」
                unavailable += 1
                lines.append(
                    f"- {_account_title(account)}：**持仓取不到**"
                    f"（{type(exc).__name__}: {exc}）。**这不代表没有持仓。**"
                )
                continue
            finally:
                try:
                    await adapter.disconnect()
                except Exception:  # noqa: BLE001 — 收尾失败不该盖掉已经拿到的持仓
                    logger.warning(
                        "[ListPositionsTool] 适配器断开失败 account=%s",
                        account.id,
                        exc_info=True,
                    )
            lines.extend(_position_lines(account, list(positions)))
        return lines, unavailable

    async def execute(
        self,
        account: str = "",
        user_id: str = "",  # 由 base.py 自动注入
        **kwargs,
    ) -> ToolResult:
        if not user_id:
            return ToolResult(success=False, error="无法确定用户身份")

        try:
            accounts = await db_async(_load_active_accounts)()
        except Exception as exc:  # noqa: BLE001
            logger.error("[ListPositionsTool] 账户查询失败: %s", exc, exc_info=True)
            return ToolResult(success=False, error=f"查询交易所账户失败: {exc}")

        if not accounts:
            return ToolResult(success=True, data="当前没有已激活的交易所账户。")

        selected, error = _resolve_accounts(accounts, account)
        if error:
            return ToolResult(success=False, error=error)

        lines, unavailable = await self._collect(selected)
        head = f"共 {len(selected)} 个活跃账户" + (
            f"，其中 {unavailable} 个取不到持仓（**不等于空仓**）"
            if unavailable
            else ""
        )
        return ToolResult(success=True, data="\n".join([head + "：", *lines]))


# --------------------------------------------------------------------------- #
# reduce_position
# --------------------------------------------------------------------------- #


def _parse_fraction(raw) -> tuple[Decimal | None, str]:
    """``fraction`` → ``Decimal``（0 < f ≤ 1）。``(None, 原因)`` = 拒绝。

    ``str()`` 兜住 DSML 降级路径把数字传成字符串的情形；``Decimal`` 避免 ``Decimal * float``
    直接崩（``plan_shards`` 那边吃过同一口）。
    """
    if raw is None or raw == "":
        return Decimal("1"), ""
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None, f"fraction 不是一个数字：{raw!r}"
    if value <= 0 or value > 1:
        return None, f"fraction 必须在 (0, 1] 之间，收到 {value}。全平请传 1.0。"
    return value, ""


#: 明确表示「同意」的字面量。**只有这些**算同意，其余一律算没同意。
_AFFIRMATIVE = frozenset({"true", "1", "yes", "y", "on", "t"})


def _confirmed(raw) -> bool:
    """``confirmed`` → ``bool``，**fail-closed**：认不出的一律 ``False``（干跑）。

    为什么不能直接 ``if confirmed:``——降级 DSML 路径（``_parse_dsml_parameters``）的默认是
    ``string="true"``，于是参数回来是**字符串**。而 ``bool("false") is True``：LLM 明明说了
    「不要下单」，字符串 ``"false"`` 会被读成「已确认」，直接下一张真实资金单。这与
    ``cb010a2`` 修的那七处是同一个根因（DSML 把参数传成字符串），但这一处的代价不是报错，
    是**一次不该发生的下单**，所以它的方向必须反过来：只认明确的肯定，其余全当没确认。

    数字 ``1`` 也认（原生 function calling 偶尔给 1/0）；``0`` 与任何认不出的字符串（含
    ``"false"``/``"no"``/``""``）都落回干跑。
    """
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw == 1
    if isinstance(raw, str):
        return raw.strip().lower() in _AFFIRMATIVE
    return False


def _match_symbol(
    positions: dict, symbol: str
) -> tuple[str, tuple[str, Decimal]] | None:
    """把用户说的品种对上账户里的真实持仓。

    先按 ``reduce.normalize_symbol`` 精确匹配（``BNB/USDT`` / ``BNBUSDT`` 都能命中），
    再退一步试 ``裸 base + USDT``——用户说「平掉 BNB」时给的就是裸 base。
    命中不了返回 ``None``，由调用方列出该账户的真实持仓让用户挑。
    """
    from apps.trading import reduce

    wanted = reduce.normalize_symbol(symbol)
    if not wanted:
        return None
    if wanted in positions:
        return wanted, positions[wanted]
    if not wanted.endswith("USDT"):
        extended = wanted + "USDT"
        if extended in positions:
            return extended, positions[extended]
    return None


class ReducePositionTool(BaseTool):
    """平仓/减仓：默认干跑，``confirmed=true`` 才真的下单。"""

    name = "reduce_position"
    description = (
        "对指定品种做平仓或减仓（下一张只减不增的市价单）。"
        "**默认是干跑**：第一次调用只返回「将执行」的摘要，不会产生任何订单；"
        "必须把摘要给用户看、并得到用户明确同意后，再以 confirmed=true 重新调用才真正下单。"
        "参数 symbol 例如 'BNB/USDT' 或 'BNB'；fraction 默认 1.0 表示全平，0.5 表示减半；"
        "account 可选，用于在多个账户间指定一个。"
    )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "品种，例如 'BNB/USDT' 或 'BNB'（裸 base 会补 USDT）",
                },
                "account": {
                    "type": "string",
                    "description": (
                        "可选，指定交易所账户（标签或账户 id）。"
                        "不填且有多个活跃账户时会报错并列出候选，不会替你挑一个。"
                    ),
                },
                "fraction": {
                    "type": "number",
                    "description": "减仓比例，0 < fraction ≤ 1。默认 1.0 = 全平；0.5 = 减半。",
                    "default": 1.0,
                },
                "confirmed": {
                    "type": "boolean",
                    "description": (
                        "是否已获得用户明确确认。**默认 false = 只干跑不下单**；"
                        "用户看过摘要并同意后才置 true。"
                    ),
                    "default": False,
                },
            },
            "required": ["symbol"],
        }

    # -- 前置解析（不碰网络） ------------------------------------------------ #

    async def _pick_account(self, raw: str, user_id: str) -> tuple:
        """解析账户 + 归属护栏。``(account, "")`` / ``(None, 错误消息)``。

        多账户且未指定 → **报错列候选**，不默认挑一个。
        """
        accounts = await db_async(_load_active_accounts)()
        if not accounts:
            return None, "当前没有已激活的交易所账户，无法执行仓位操作。"

        selected, error = _resolve_accounts(accounts, raw)
        if error:
            return None, error
        if not selected:
            return None, "没有可用的活跃交易所账户。"
        if len(selected) > 1:
            if raw:
                return None, (
                    f"账户「{raw}」匹配到 {len(selected)} 个，无法确定是哪一个是："
                    + "、".join(_account_title(a) for a in selected)
                    + "。请用更完整的标签或账户 id 再试一次。"
                )
            return None, (
                f"当前有 {len(selected)} 个活跃交易所账户，必须指定其中一个："
                + "、".join(_account_title(a) for a in selected)
                + "。请带上 account 参数重试（绝不会替你挑一个）。"
            )

        account = selected[0]

        # 跨用户护栏：目标是**别人的**账户就拒。未归属或归调用人则放行——单人部署无感，
        # 多用户时挡住「按 label 减到别人账户」这一最坏情形。
        owner_id = getattr(account, "user_id", None)
        if owner_id:
            caller = await _resolve_owner_id(user_id)
            if caller and str(owner_id) != caller:
                logger.warning(
                    "[ReducePositionTool] 拒绝跨用户仓位操作 account=%s owner=%s caller=%s",
                    account.id,
                    owner_id,
                    caller,
                )
                return None, (
                    f"账户 {_account_title(account)} 不属于当前用户，拒绝执行仓位操作。"
                )
        return account, ""

    # -- 执行 ---------------------------------------------------------------- #

    async def execute(
        self,
        symbol: str = "",
        account: str = "",
        fraction=1.0,
        confirmed=False,
        user_id: str = "",  # 由 base.py 自动注入
        **kwargs,
    ) -> ToolResult:
        from apps.trading import reduce
        from apps.trading.pending_reconcile import build_account_adapter

        if not user_id:
            return ToolResult(success=False, error="无法确定用户身份")
        if not symbol or not str(symbol).strip():
            return ToolResult(success=False, error="symbol 为必填项")

        ratio, why = _parse_fraction(fraction)
        if ratio is None:
            return ToolResult(success=False, error=why)

        # 字符串 "false" 必须读成「没确认」——DSML 降级路径的默认 `string="true"` 会把
        # 它传成字符串，而 `bool("false") is True`。这一处的代价是一次不该发生的下单。
        confirmed = _confirmed(confirmed)

        picked, error = await self._pick_account(account, user_id)
        if error or picked is None:
            return ToolResult(success=False, error=error)
        account_obj = picked

        try:
            adapter = await build_account_adapter(account_obj)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[ReducePositionTool] 适配器构建失败 account=%s: %s",
                account_obj.id,
                exc,
                exc_info=True,
            )
            return ToolResult(
                success=False, error=f"无法连接交易所账户：{type(exc).__name__}: {exc}"
            )
        if adapter is None:
            return ToolResult(
                success=False,
                error=(
                    f"账户 {_account_title(account_obj)} 没有可用的适配器"
                    "（缺少该交易所适配器或 API 密钥），无法执行仓位操作。"
                ),
            )

        try:
            # 持仓：**交易所是唯一真相**。取不到就整段作废——不知道持有多少时绝不下单。
            try:
                positions = await reduce.account_positions(adapter)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "[ReducePositionTool] 持仓读取失败 account=%s: %s",
                    account_obj.id,
                    exc,
                    exc_info=True,
                )
                return ToolResult(
                    success=False,
                    error=(
                        f"读取账户持仓失败（{type(exc).__name__}: {exc}），"
                        "本次未下单（不知道持有多少时不下单）。"
                    ),
                )

            matched = _match_symbol(positions, symbol)
            if matched is None:
                held = (
                    "、".join(sorted(positions))
                    if positions
                    else "（该账户当前没有持仓）"
                )
                return ToolResult(
                    success=False,
                    error=(
                        f"账户 {_account_title(account_obj)} 里没有找到与「{symbol}」"
                        f"匹配的持仓。该账户实际持仓：{held}。"
                    ),
                )
            native_symbol, (side_of_position, qty_before) = matched

            if side_of_position not in ("long", "short"):
                # 方向认不出 → **拒单**（同 reduce_run._run_symbol 的方向保险）：
                # 下面的 side 是照着方向取的，读出一个别的东西说明语义变了，
                # 这时按「做多」处理对一个其实是空头的持仓就是**加仓**。
                return ToolResult(
                    success=False,
                    error=(
                        f"持仓方向无法识别（{side_of_position!r}），已拒绝下单。"
                        "请人工核对账户持仓后再操作。"
                    ),
                )

            side = "sell" if side_of_position == "long" else "buy"
            target_qty = qty_before * ratio

            mid = await reduce.mid_price(adapter, native_symbol)

            if not confirmed:
                # 干跑：**一次下单调用都不发**。即使 LLM 幻觉，首调也不产生订单。
                return ToolResult(
                    success=True,
                    data={
                        "status": "DRY_RUN",
                        "account": _account_title(account_obj),
                        "symbol": native_symbol,
                        "position_side": side_of_position,
                        "order_side": side,
                        "position_qty": str(qty_before),
                        "fraction": str(ratio),
                        "planned_qty": str(target_qty),
                        "mid_price": str(mid) if mid is not None else None,
                        "message": (
                            f"【干跑·未下单】将{'全平' if ratio == 1 else '减仓'}"
                            f"{_account_title(account_obj)} 的 {native_symbol}："
                            f"当前持仓 {qty_before}（{side_of_position}），"
                            f"计划以市价 {side} {target_qty}"
                            f"（fraction={ratio}），当前中间价 "
                            f"{str(mid) if mid is not None else '取不到'}。"
                            "**本次未产生任何订单。**"
                            "请向用户复述以上摘要并取得明确同意；"
                            "用户同意后再以 confirmed=true 重新调用本工具才会真正下单。"
                        ),
                    },
                )

            # ---- 真实下单：一张只减不增的市价单 ----
            client_order_id = f"cl{account_obj.id.hex[:8]}{uuid.uuid4().hex[:10]}"
            shard = reduce.ShardOrder(
                client_order_id=client_order_id,
                symbol=native_symbol,
                side=side,
                quantity=target_qty,
            )
            result = await reduce.place_reduce_shard(adapter, shard)

            # 落库（fix 2）：减仓成交也要有 Order 行，否则本地成本链看不到它，
            # 后续会话卖单会重演 2026-09-28 那类「回放到 pos<=0 返回 None」的缺口。
            # **落库失败不得让工具失败**——成交已经发生，此刻抛异常只会让 Agent 把
            # 一次已完成的不可逆动作读成「操作失败」。只 warning。
            try:
                from apps.trading import reduce_ledger

                # 渠道身份 → Django User pk，落成 Order.user_id（与 _pick_account 的
                # 归属比对同一个解析口；解析不出传空，Order.user 本就允许为空）。
                owner_id = await _resolve_owner_id(user_id) or None
                await reduce_ledger.persist_reduce_order(
                    account_obj,
                    shard,
                    result,
                    adapter=adapter,
                    user_id=owner_id,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "[ReducePositionTool] 减仓成交落库失败 account=%s symbol=%s",
                    account_obj.id,
                    native_symbol,
                    exc_info=True,
                )

            qty_after: Decimal | None = None
            if result.status != reduce.SHARD_STATUS_UNFINISHED:
                # `unfinished` 时**不读余量**：交易所侧可能还有一张活单，此刻读到的任何数
                # 都只是「此刻」，写出来会让它看起来像一次结算后的余量（同 reduce_run）。
                try:
                    after = await reduce.account_positions(adapter)
                    qty_after = after.get(native_symbol, ("", Decimal("0")))[1]
                except Exception as exc:  # noqa: BLE001 — 余量读不到不影响「成交了多少」
                    logger.warning(
                        "[ReducePositionTool] 余量读取失败 symbol=%s: %s",
                        native_symbol,
                        exc,
                    )

            head = (
                f"仓位操作结果｜{_account_title(account_obj)}｜{native_symbol}"
                f"｜{side_of_position} → {side}｜委托 {result.requested_qty}"
            )
            body = [
                head,
                f"状态：{result.status}（发送 {result.sends} 次）",
                f"成交量：{result.filled_qty}"
                + (f"｜成交均价：{result.avg_price}" if result.avg_price else ""),
                f"余量：{qty_after if qty_after is not None else '未读取'}",
            ]
            if result.exchange_order_id:
                body.append(f"交易所单号：{result.exchange_order_id}")
            if result.error:
                body.append(f"说明：{result.error}")
            if result.status == reduce.SHARD_STATUS_UNFINISHED:
                # **必须单独点名**：unfinished 的语义是「可能仍有活单」，绝不降级成
                # 「确定没成交」（reduce.py 模块 docstring 的核心纪律）。
                body.append(
                    "⚠️ **本次没有拿到结论**：交易所侧可能仍有一张活单，成交情况未知。"
                    "**请人工到交易所核对后再做任何后续操作**，不要按「没成交」处理。"
                )

            text = "\n".join(body)
            logger.info(
                "[ReducePositionTool] account=%s symbol=%s side=%s requested=%s "
                "filled=%s status=%s cid=%s user=%s",
                account_obj.id,
                native_symbol,
                side,
                result.requested_qty,
                result.filled_qty,
                result.status,
                result.client_order_id,
                user_id,
            )
            await self._notify(user_id, text)
            return ToolResult(success=True, data=text)
        finally:
            try:
                await adapter.disconnect()
            except Exception:  # noqa: BLE001 — 收尾失败不该盖掉真正的结论
                logger.warning(
                    "[ReducePositionTool] 适配器断开失败 account=%s",
                    account_obj.id,
                    exc_info=True,
                )

    async def _notify(self, user_id: str, text: str) -> None:
        """把成交结果投递给用户（``Notification`` 行 + 即时推送）。

        尽力而为：通知失败只记日志，**不影响** 已经发生的那笔成交的回报——一次不可逆动作
        已经做完了，此刻再抛异常只会让 Agent 把它读成「操作失败」。
        """
        from apps.trading.alerts import notify_user

        try:
            await notify_user(user_id, "📉 手动仓位操作：" + text)
        except Exception:  # noqa: BLE001
            logger.warning(
                "[ReducePositionTool] 结果通知失败 user=%s", user_id, exc_info=True
            )
