import logging
from decimal import Decimal

from asgiref.sync import async_to_sync
from django.core.exceptions import ValidationError
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.utils import timezone
from .models import (
    Order,
    Strategy,
    LiveSession,
    record_status_change,
)
from .serializers import OrderSerializer, StrategySerializer, LiveSessionSerializer

logger = logging.getLogger(__name__)

#: 明确表示「同意」的字面量。**只有这些**算同意，其余一律算没同意。
#:
#: 与 `apps/agent/tools/positions.py::_confirmed` 同一张表、同一条理由：降级 DSML 路径
#: 会把布尔参数传成**字符串**，而 `bool("false") is True`——LLM 明明说了「不要」，
#: 字符串 `"false"` 会被读成「已确认」。这里的代价是**一次不该发生的实盘升格**，
#: 所以方向必须反过来：只认明确的肯定，其余全当没确认（干跑）。
_AFFIRMATIVE = frozenset({"true", "1", "yes", "y", "on", "t"})


def _confirmed(raw) -> bool:
    """`confirmed` → `bool`，**fail-closed**：认不出的一律 `False`（干跑）。"""
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw == 1
    if isinstance(raw, str):
        return raw.strip().lower() in _AFFIRMATIVE
    return False


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def order_list(request):
    queryset = Order.objects.select_related(
        "exchange_account", "live_session__strategy"
    ).order_by("-created_at")
    status_filter = request.query_params.get("status")
    if status_filter:
        queryset = queryset.filter(status=status_filter)
    orders = queryset[:100]
    return Response(OrderSerializer(orders, many=True).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def order_detail(request, pk):
    order = Order.objects.select_related(
        "exchange_account", "live_session__strategy"
    ).get(pk=pk)
    return Response(OrderSerializer(order).data)


KLINE_INTERVALS = ("1m", "5m", "15m", "1h", "4h", "1d")


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def klines(request):
    """单品种 K 线数据（交易记录 / 历史订单图表展示）。

    查询参数：symbol（必填，如 SOL/USDT）、interval（默认 1h）、limit（默认 300，最大 1000）。
    数据从交易所实时拉取（经 WEB_PROXY 代理），返回升序 OHLCV。
    """
    from apps.backtest.tasks import _fetch_ohlcv_sync

    symbol = (request.query_params.get("symbol") or "").strip()
    if not symbol:
        return Response(
            {"error": "symbol is required"}, status=status.HTTP_400_BAD_REQUEST
        )
    interval = (request.query_params.get("interval") or "1h").strip()
    if interval not in KLINE_INTERVALS:
        return Response(
            {"error": f"interval must be one of {list(KLINE_INTERVALS)}"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        limit = int(request.query_params.get("limit") or 300)
    except (TypeError, ValueError):
        limit = 300
    limit = max(50, min(limit, 1000))

    ohlcv = _fetch_ohlcv_sync(symbol, interval, exchange="binance", limit=limit)
    if not ohlcv:
        return Response(
            {
                "error": f"failed to fetch OHLCV for {symbol} ({interval})",
                "ohlcv_data": [],
            },
            status=status.HTTP_502_BAD_GATEWAY,
        )
    return Response(
        {
            "symbol": symbol,
            "interval": interval,
            "count": len(ohlcv),
            "ohlcv_data": ohlcv,
        }
    )


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def strategy_list(request):
    if request.method == "GET":
        strategies = Strategy.objects.all().order_by("-created_at")
        return Response(StrategySerializer(strategies, many=True).data)
    serializer = StrategySerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response(serializer.data, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def trading_summary(request):
    from django.utils import timezone
    from apps.exchange.models import ExchangeAccount
    from apps.trading.models import DailySnapshot

    # 从最新快照获取总权益
    latest_snapshot = (
        DailySnapshot.objects.filter(user=request.user).order_by("-date").first()
    )
    total_equity = str(latest_snapshot.total_equity) if latest_snapshot else "0"

    # 今日已实现盈亏
    today = timezone.now().date()
    today_filled = list(
        Order.objects.filter(
            created_at__date=today,
            status="filled",
            realized_pnl__isnull=False,
        )
    )
    today_pnl = sum(float(o.realized_pnl or 0) for o in today_filled)
    today_count = len(today_filled)

    # 活跃订单数（含「未知」：它是非终态，可能仍在交易所活着）
    # 枚举不许手抄：常量在 Order 上（`Order.ACTIVE_STATUSES`），这里是它唯一的家。
    active_count = Order.objects.filter(status__in=Order.ACTIVE_STATUSES).count()

    # 交易所账户数
    account_count = ExchangeAccount.objects.filter(is_active=True).count()

    return Response(
        {
            "total_equity": total_equity,
            "today_realized_pnl": f"{today_pnl:.2f}",
            "active_orders_count": active_count,
            "total_orders_today": today_count,
            "account_count": account_count,
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def position_list(request):
    from apps.exchange.models import ExchangeAccount
    from apps.trading.executor import OrderExecutor

    executor = OrderExecutor.get_instance()
    if not executor or not executor._running:
        return Response(
            {
                "positions": [],
                "executor_running": False,
                "message": "交易框架未启动，持仓数据暂不可用",
            }
        )

    # 按**账户**遍历，走 `OrderExecutor.account_adapters()` 这个公开 interface：
    # 同一交易所有两个活跃账户时，按交易所名索引的表只留最后一个，于是两个账户的持仓
    # 都会记在那个账户名下（原先这里是 `exchange -> account_id` 手工反查，同样是后写
    # 覆盖）。账户地图才是每个账户各自的适配器，`exchange_account_id` 直接是键本身。
    account_by_id = {
        str(acc.id): acc for acc in ExchangeAccount.objects.filter(is_active=True)
    }

    all_positions = []
    for account_id, adapter in executor.account_adapters().items():
        account = account_by_id.get(account_id)
        if account is None:
            # 适配器还在但账户已停用/删除：不猜它的交易所名，跳过。
            continue
        exchange_name = (account.exchange or "").lower()
        try:
            positions = async_to_sync(adapter.get_positions)()
            for pos in positions:
                # pos is a Position dataclass
                all_positions.append(
                    {
                        "exchange": exchange_name,
                        "exchange_account_id": account_id,
                        "symbol": pos.symbol,
                        "side": pos.side,
                        "quantity": str(pos.quantity),
                        "entry_price": str(pos.entry_price),
                        "mark_price": str(
                            pos.mark_price
                            if pos.mark_price is not None
                            else pos.entry_price
                        ),
                        "unrealized_pnl": str(pos.unrealized_pnl),
                    }
                )
        except Exception:
            continue

    return Response(
        {
            "positions": all_positions,
            "executor_running": True,
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def account_list(request):
    """
    返回交易所账户列表 + 各自的余额信息。
    """
    from apps.exchange.models import ExchangeAccount
    from apps.exchange.serializers import ExchangeAccountSerializer
    from apps.trading.executor import OrderExecutor

    accounts = ExchangeAccount.objects.filter(is_active=True)
    serializer = ExchangeAccountSerializer(accounts, many=True)
    account_data = serializer.data

    # 尝试从 OrderExecutor 获取各账户余额。**按账户 id 索引**：同一交易所有两个账户
    # 时按交易所名索引会让两个账户共用（且是最后加载那个账户的）余额。
    executor = OrderExecutor.get_instance()
    balances = {}
    if executor and executor._running:
        for account_id, adapter in executor.account_adapters().items():
            try:
                balance = async_to_sync(adapter.get_balance)()
                # balance is dict[str, Decimal], e.g. {"USDT": Decimal("100.0")}
                usdt_balance = balance.get("USDT", Decimal("0"))
                balances[account_id] = {
                    "total": str(usdt_balance),
                    "available": str(usdt_balance),
                    "used": "0",
                }
            except Exception:
                pass

    # 合并余额信息
    for acc in account_data:
        acc["balance"] = balances.get(str(acc["id"]))

    return Response(account_data)


#: 会话详情里内嵌的订单条数上限。**必须连同 `orders_total` 一起回**，否则这张表看起来
#: 像全部——那正是「订单接口硬截断 100 条」这个坑的同一个形状（`order_list`）。
SESSION_DETAIL_ORDERS = 50


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def live_session_list(request):
    from .session_stats import annotate_realized_pnl

    sessions = annotate_realized_pnl(
        LiveSession.objects.filter(user=request.user)
    ).order_by("-created_at")[:50]
    return Response(LiveSessionSerializer(sessions, many=True).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def live_session_detail(request, pk):
    """一条会话的详情：会话本身 + 收益统计 + 生效参数 + 在拦的层 + 最近订单。

    这几块**只在详情算**。挂到列表上就是 50 条会话各算一次统计（50 次查询），而列表
    本来只需要一个 `realized_pnl`（`annotate_realized_pnl` 一次聚合算完）。
    """
    from apps.regime import halt

    from .session_params import effective_params
    from .session_stats import session_stats_for

    try:
        session = LiveSession.objects.get(pk=pk, user=request.user)
    except LiveSession.DoesNotExist:
        return Response({"error": "Live session not found"}, status=404)

    stats = session_stats_for(session)
    # 与下单通路同一个入口、同一张声明表（`start_notice` 也是这么调的），且
    # `halt_layers` 自己就按作用域裁剪——不命中该会话的层不会出现在这里。
    verdict = halt.halt_layers(
        session.symbol,
        str(session.strategy_id) if session.strategy_id else None,
    )
    session_orders = Order.objects.filter(live_session=session)

    payload = LiveSessionSerializer(session).data
    payload["realized_pnl"] = stats["realized_pnl_total"]
    payload["stats"] = stats
    payload["params"] = effective_params(session.strategy, session.config)
    payload["halt_layers"] = [
        {
            # `text` 是「行 → 层」的唯一换算口（`halt.HaltLayer.text`）。前端原样显示，
            # 不许自己按 label/trigger/scope 再拼一遍——拼法一旦分叉，同一层在两处就
            # 有两种说法。
            "text": layer.text,
            "reason": layer.reason,
            "opened_at": layer.opened_at.isoformat(),
            "expires_at": (
                layer.expires_at.isoformat() if layer.expires_at is not None else None
            ),
        }
        for layer in verdict.layers
    ]
    payload["orders_total"] = session_orders.count()
    payload["orders"] = OrderSerializer(
        session_orders.select_related(
            "exchange_account", "live_session__strategy"
        ).order_by("-created_at")[:SESSION_DETAIL_ORDERS],
        many=True,
    ).data
    return Response(payload)


async def _fetch_usdt_balance(exchange_account) -> Decimal:
    """实时查询交易所账户 USDT 余额，作为新会话的初始资金。"""
    from apps.trading.adapters import ADAPTER_MAP

    adapter_cls = ADAPTER_MAP.get(exchange_account.exchange.lower())
    if adapter_cls is None:
        raise ValueError(f"Exchange {exchange_account.exchange} has no adapter")

    adapter = adapter_cls(
        exchange_account.decrypt_api_key(),
        exchange_account.decrypt_api_secret(),
        exchange_account.testnet,
    )
    try:
        await adapter.connect()
        balance = await adapter.get_balance()
    finally:
        await adapter.disconnect()
    return balance.get("USDT", Decimal("0"))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def strategy_params(request, name):
    """某策略**声明**的参数表 —— 部署会话时可覆盖的键。

    为什么要这个接口（2026-10-12 SOL/USDT 审计）：`live_sessions.config` 只记录
    "本会话覆盖了什么"，没覆盖的键走策略声明的默认值。部署弹窗过去不发 `config`，
    于是像 `volume_filter` 这种键**没有任何入口**能显式打开——只能改策略文件默认值
    或手工写库。审计时"量能门全程是关的"就卡在这里：不是界面显示错了，是压根
    没有地方能把它打开。

    返回 `params_schema` 的原始声明（键 + 默认值 + 描述 + 约束），**不**与任何会话
    的 config 合并——合并后那张表是 `live_session_detail` 的 `params` 字段负责的。
    两个接口各说各的一件事：这里说"策略允许你设什么"，那里说"这条会话实际在跑什么"。
    """
    from apps.strategy_engine.registry import StrategyRegistry

    if name not in StrategyRegistry.list_registered():
        StrategyRegistry.discover()

    try:
        strategy_cls = StrategyRegistry.get_class(name)
    except Exception:
        return Response(
            {"error": f"Strategy {name!r} not resolvable"},
            status=status.HTTP_404_NOT_FOUND,
        )

    schema = getattr(strategy_cls, "params_schema", None)
    if not isinstance(schema, dict):
        return Response({"strategy_name": name, "resolved": True, "params": []})

    params = []
    for key, spec in schema.items():
        if isinstance(spec, dict):
            params.append(
                {
                    "key": key,
                    "type": spec.get("type"),
                    "default": spec.get("default"),
                    "description": spec.get("description", ""),
                    # 约束原样透出，由前端决定怎么呈现；后端不替它判合法性
                    "min": spec.get("min"),
                    "max": spec.get("max"),
                }
            )
        else:
            params.append(
                {
                    "key": key,
                    "type": None,
                    "default": spec,
                    "description": "",
                    "min": None,
                    "max": None,
                }
            )

    return Response({"strategy_name": name, "resolved": True, "params": params})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def live_session_create(request):
    """
    从回测创建并启动实盘会话。

    Request body:
      backtest_result_id: str (UUID) — 来源回测
      mode: str — 'paper' 或 'live'（首次必须为 paper）
      exchange_account_id: str (UUID) — 交易所账户
      config: dict (optional) — 运行参数；未传时继承回测的自定义参数
        （回测用什么参数验证的，会话就按什么参数跑），显式传入的键优先
    """
    from apps.backtest.models import BacktestResult
    from apps.exchange.models import ExchangeAccount

    backtest_id = request.data.get("backtest_result_id")
    mode = request.data.get("mode", "paper")
    exchange_account_id = request.data.get("exchange_account_id")

    if not backtest_id:
        return Response({"error": "backtest_result_id is required"}, status=400)
    if not exchange_account_id:
        return Response({"error": "exchange_account_id is required"}, status=400)

    # 校验回测结果
    try:
        backtest = BacktestResult.objects.get(pk=backtest_id)
    except BacktestResult.DoesNotExist:
        return Response({"error": "Backtest result not found"}, status=404)

    if backtest.review_status != "approved":
        return Response(
            {"error": "Backtest must be approved before deployment"}, status=400
        )

    # 强制先模拟（D12 后半，子票 #28）：live 模式的前置**不再**是「存在任意一条 paper
    # 会话」，而是「存在一条**跑满观察期且判定通过**的 paper 会话」。
    #
    # 旧口径只查 `.exists()`：一条刚建出来、一天都没跑、甚至盈亏为负的 paper 会话
    # 就能放行实盘。弱化的闸门比没有闸门更危险——它看起来像一道关。判定口径的**唯一
    # 出处**是 `apps.strategy_factory.live_gate`（读候选的 `awaiting_live_approval`
    # 状态），这里不重算观察期，否则同一口径会有两处实现、必然漂移。
    approved_gate = None
    if mode == "live":
        from apps.strategy_factory.live_gate import find_approved_paper_session

        approved_gate = find_approved_paper_session(
            user=request.user, backtest_result=backtest
        )
        if not approved_gate.allowed:
            return Response({"error": approved_gate.reason}, status=400)

    # 校验交易所账户
    try:
        exchange_account = ExchangeAccount.objects.get(
            pk=exchange_account_id, is_active=True
        )
    except ExchangeAccount.DoesNotExist:
        return Response({"error": "Exchange account not found or inactive"}, status=404)

    # runner 共享该品种的账户级持仓：同账户同品种两个策略必然互相平仓
    # （2026-10-07：box 会话平掉 donchian 开的 DOGE/USDT 仓）。创建时即拒绝。
    conflict = LiveSession.conflicting_active_session(exchange_account, backtest.symbol)
    if conflict:
        return Response({"error": conflict}, status=400)

    # 初始资金取绑定账户的实时 USDT 余额，而非回测的固定金额
    try:
        initial_capital = async_to_sync(_fetch_usdt_balance)(exchange_account)
    except Exception as e:
        logger.exception("Failed to fetch account balance for session creation: %s", e)
        return Response(
            {"error": f"Failed to fetch exchange account balance: {e}"},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    # 参数继承：未显式传 config 时继承回测的自定义参数。
    # 防止静默漂移（2026-10 BNB/USDT 事故：回测用 exit_at_mid=false 验证通过，
    # session.config 却是 {}，实盘按策略默认 exit_at_mid=true 跑，一周 10 次往返全亏）。
    # 显式传入的 config 键优先于回测参数。
    config = {**(backtest.parameters or {}), **(request.data.get("config") or {})}

    if approved_gate is not None:
        # 交付 4：新 live 会话继承 **paper 期**的权益与配置（D11）。
        #
        # 为什么覆盖掉上面那份「回测参数」继承：paper 会话的 config 是**真的跑过那两周**
        # 的那一份（有可能在观察期内被调过），而回测参数是它出生时的快照。让 live 会话
        # 跟着「实际观察过的配置」走，才是「继承 paper 期」这句话的意思。
        # 继承顺序与 promote 一致（那里也是从 paper 会话逐键取）。
        #
        # 显式传入的 config 键仍然是**最优先**的：人工可以在升实盘那一刻再做覆盖。
        paper_session = approved_gate.paper_session
        config = {
            **dict(paper_session.config or {}),
            **(request.data.get("config") or {}),
        }
        # 权益取 paper 会话的当前权益（没有则退回它的初始权益）——D11：升实盘不是
        # 「换一个账户重新开始」，而是「把观察过的这条策略挪到实盘账户上接着跑」。
        # 不用实盘账户余额覆盖它：那个数属于账户，不属于这条候选的观察结论。
        initial_capital = paper_session.current_equity or paper_session.initial_capital

    # 创建 LiveSession
    session = LiveSession.objects.create(
        user=request.user,
        backtest_result=backtest,
        strategy=backtest.strategy,
        symbol=backtest.symbol,
        mode=mode,
        status="pending",
        exchange_account=exchange_account,
        initial_capital=initial_capital,
        current_equity=initial_capital,
        config=config,
    )

    # 最后一道校验：不变式**不放松**。建完立刻自查 mode/账户是否自洽，错配就回滚，
    # 不留一支起不来的会话（与 promote 同一道保险）。
    mismatch = session.mode_account_mismatch()
    if mismatch:
        session.delete()
        return Response({"error": mismatch}, status=400)

    return Response(
        {
            "id": str(session.id),
            "status": session.status,
            "mode": session.mode,
            "message": f"Live session created ({mode} mode)",
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def live_session_start(request, pk):
    from apps.agent.frame_manager import FrameManager

    try:
        session = LiveSession.objects.select_related(
            "strategy", "exchange_account", "user", "backtest_result"
        ).get(pk=pk, user=request.user)
    except LiveSession.DoesNotExist:
        return Response({"error": "Live session not found"}, status=404)

    if session.status not in ("pending", "error", "running", "stopped"):
        return Response(
            {"error": f"Session is {session.status}, cannot start"}, status=400
        )

    if not session.exchange_account:
        return Response({"error": "No exchange account configured"}, status=400)

    # mode 与账户 testnet 必须自洽：错配意味着「以为在演练、实际在动钱」
    mismatch = session.mode_account_mismatch()
    if mismatch:
        return Response({"error": mismatch}, status=400)

    # 与 create 同一道检查：runner 共享账户级持仓，同账户同品种再起一支会互相平仓
    conflict = LiveSession.conflicting_active_session(
        session.exchange_account, session.symbol, exclude_pk=session.pk
    )
    if conflict:
        return Response({"error": conflict}, status=400)

    frame_manager = FrameManager.get_instance()

    # 如果已在运行，先停掉旧运行器（处理之前框架崩溃导致的幽灵状态）
    # 注意：只停当前 session 的 runner，不动整个 trading frame（其他策略可能还在跑）
    if session.status == "running":
        logger.warning(
            "Session %s is already running, stopping old runner before restart", pk
        )
        try:
            async_to_sync(frame_manager.stop_strategy_runner)(
                live_session_id=str(session.id)
            )
        except Exception:
            pass

    # 1. 启动交易框架
    try:
        async_to_sync(frame_manager.start_trading_frame)(mode=session.mode)
    except Exception as e:
        logger.exception("Failed to start trading frame for session %s: %s", pk, e)
        return Response({"error": f"Failed to start trading frame: {e}"}, status=500)

    # 2. 启动策略运行器（内部会注册信号监控到 SignalMonitor）
    timeframe = session.backtest_result.timeframe if session.backtest_result else "1h"

    try:
        async_to_sync(frame_manager.start_strategy_runner)(
            strategy_name=session.strategy.name,
            symbol=session.symbol,
            timeframe=timeframe,
            parameters=session.config or {},
            exchange_account_id=str(session.exchange_account.id),
            user_id=str(session.user.id),
            live_session_id=str(session.id),
            initial_balance=session.initial_capital,
        )
    except Exception as e:
        logger.exception("Failed to start strategy for session %s: %s", pk, e)
        # 只回滚当前 session 的 runner，不动整个 trading frame（避免误杀并发策略）
        try:
            async_to_sync(frame_manager.stop_strategy_runner)(
                live_session_id=str(session.id)
            )
        except Exception:
            pass
        return Response({"error": f"Failed to start strategy: {e}"}, status=500)

    session.status = "running"
    session.started_at = timezone.now()
    session.stopped_at = None
    # 时间线（#24，D8）：与 save 同一个事务（视图的原子块见下方 transaction.atomic）。
    # 重启动也记一行：观察期只计 running，而重启动是「离开 running（stopped/error）
    # 再回来」，不记的话第一段 running 的终点就丢了。
    from django.db import transaction

    with transaction.atomic():
        record_status_change(session, to_status="running", changed_at=timezone.now())
        session.save(update_fields=["status", "started_at", "stopped_at", "updated_at"])

    logger.info(
        "Live session %s started: strategy=%s symbol=%s timeframe=%s",
        pk,
        session.strategy.name,
        session.symbol,
        timeframe,
    )

    # CONTEXT.md 第 172 条：被 halt 拦下的动作必须回显是哪个触发源在拦，且回显落在
    # 「用户发起动作」的那一刻。这个会话的订单要到 K 线到达之后才产生，那时没有任何人
    # 正在看屏幕——所以在这里就把「你启动了它，但它开不了新仓」说清楚。
    # **不阻止启动**：存量仓位的止损保护照常上线（所以不 return 400）。
    # 只报**命中这个会话**的层（按 symbol / strategy），不命中就是空串。
    from apps.regime import start_notice

    return Response(
        {
            "status": session.status,
            "message": "Trading framework started",
            # 字段恒在：空串 = 没有层在拦。恒在的话客户端不用去区分「没有层」与
            # 「服务端没这个字段」——后者会把一次静默的裁剪读成一次未实现的回显。
            "notice": start_notice.start_notice(
                session.symbol, str(session.strategy_id)
            ),
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def live_session_pause(request, pk):
    try:
        session = LiveSession.objects.get(pk=pk, user=request.user)
    except LiveSession.DoesNotExist:
        return Response({"error": "Live session not found"}, status=404)

    if session.status != "running":
        return Response(
            {"error": f"Session is {session.status}, cannot pause"}, status=400
        )

    session.status = "paused"
    # 时间线（#24，D8）：paused 不计入观察，但**这一段从什么时候开始**必须被记住，
    # 否则恢复后无法从 running 段里把这段扣掉。
    from django.db import transaction

    with transaction.atomic():
        record_status_change(session, to_status="paused", changed_at=timezone.now())
        session.save(update_fields=["status", "updated_at"])

    # 真正停住 runner：只改 DB 状态的话（旧实现）runner 继续跑，且 Redis 兼容层键
    # 仍写着 running → 下次重启又被恢复（2026-09-21 实际踩到）。stop_strategy_runner
    # 会停 runner 并删除兼容层键，缓存与 DB 随之一致。
    from apps.agent.frame_manager import FrameManager

    frame_manager = FrameManager.get_instance()
    if frame_manager is not None:
        try:
            async_to_sync(frame_manager.stop_strategy_runner)(
                live_session_id=str(session.id)
            )
        except Exception as e:  # noqa: BLE001 - 暂停语义以 DB 为准，停 runner 失败不阻断
            logger.error("pause: failed to stop runner for %s: %s", session.id, e)

    return Response({"status": session.status, "message": "Session paused"})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def live_session_resume(request, pk):
    try:
        session = LiveSession.objects.get(pk=pk, user=request.user)
    except LiveSession.DoesNotExist:
        return Response({"error": "Live session not found"}, status=404)

    if session.status != "paused":
        return Response(
            {"error": f"Session is {session.status}, cannot resume"}, status=400
        )

    # 与 start 同一不变式：恢复也是一次「启动」，同样不能带着 mode 错配跑起来
    mismatch = session.mode_account_mismatch()
    if mismatch:
        return Response({"error": mismatch}, status=400)

    # 与 start 同一道检查：恢复前同账户同品种若已有活跃会话，同样拒绝
    conflict = LiveSession.conflicting_active_session(
        session.exchange_account, session.symbol, exclude_pk=session.pk
    )
    if conflict:
        return Response({"error": conflict}, status=400)

    session.status = "running"
    # 时间线（#24，D8）：resume 是重新进入 running，新的一段从这里起算。
    from django.db import transaction

    with transaction.atomic():
        record_status_change(session, to_status="running", changed_at=timezone.now())
        session.save(update_fields=["status", "updated_at"])

    # 真正拉起 runner（并重建 Redis 兼容层键）。失败时 DB 已是 running，
    # 下次重启仍会被恢复，属可自愈状态，故只记录错误。
    from apps.agent.frame_manager import FrameManager

    frame_manager = FrameManager.get_instance()
    if frame_manager is not None:
        try:
            async_to_sync(frame_manager.start_live_session)(str(session.id))
        except Exception as e:  # noqa: BLE001
            logger.error("resume: failed to start runner for %s: %s", session.id, e)

    # 恢复是**同一个时刻**的同一件事（第 172 条：回显落在用户发起动作的那一刻）：
    # 暂停过的会话从这里重新开始开仓，而它的订单同样在 K 线到达之后才产生。
    # 漏掉这一处，用户只要「暂停再恢复」就再也看不到那条回显了。
    from apps.regime import start_notice

    return Response(
        {
            "status": session.status,
            "message": "Session resumed",
            "notice": start_notice.start_notice(
                session.symbol, str(session.strategy_id)
            ),
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def live_session_stop(request, pk):
    from apps.agent.frame_manager import FrameManager

    try:
        session = LiveSession.objects.get(pk=pk, user=request.user)
    except LiveSession.DoesNotExist:
        return Response({"error": "Live session not found"}, status=404)

    if session.status not in ("running", "paused"):
        return Response(
            {"error": f"Session is {session.status}, cannot stop"}, status=400
        )

    session.status = "stopped"
    session.stopped_at = timezone.now()
    # 时间线（#24，D8）：离开 running 的时刻在这里落地——最后一段 running 的终点。
    from django.db import transaction

    with transaction.atomic():
        record_status_change(session, to_status="stopped", changed_at=timezone.now())
        session.save(update_fields=["status", "stopped_at", "updated_at"])

    # 停止当前 session 的策略运行器
    frame_manager = FrameManager.get_instance()
    async_to_sync(frame_manager.stop_strategy_runner)(live_session_id=str(session.id))
    # 仅当无其他策略运行时才停整个 trading frame（避免误杀并发策略）
    if not frame_manager._strategy_runners:
        async_to_sync(frame_manager.stop_trading_frame)()

    return Response({"status": session.status, "message": "Session stopped"})


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def live_session_delete(request, pk):
    """
    删除实盘会话。
    仅允许删除已停止(stopped)、异常(error)或待启动(pending)状态的会话。
    """
    try:
        session = LiveSession.objects.get(pk=pk, user=request.user)
    except LiveSession.DoesNotExist:
        return Response({"error": "Live session not found"}, status=404)

    if session.status in ("running", "paused"):
        return Response(
            {"error": f"Session is {session.status}, stop it first before deleting"},
            status=400,
        )

    session.delete()
    return Response(
        {"message": "Session deleted", "id": str(pk)},
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def live_session_promote(request, pk):
    """把一条 paper 会话升成 live 会话（spec #19 D11，子票 #28）。

    ## 三步人工卡点：干跑摘要 → 显式确认 → 目标账户

    复用 `reduce_position` 那种既有形状（`apps/agent/tools/positions.py`）：
    **默认干跑**（不带 `confirmed=true` 时只回一段「将执行」摘要，什么都不改），
    得到用户明确同意后再带 `confirmed=true` 与目标实盘账户重新调用。

    为什么不建在 workflow interrupt 上：那个 HITL 点**无法恢复**——暂停写
    `active_agent="workflow"`，恢复时 `AgentRegistry.get("workflow")` 抛 `KeyError`。
    一个只能暂停不能恢复的卡点，比没有卡点更糟。

    ## 与旧实现的两处关键差异

    1. **账户不再继承源会话**，而是由 `exchange_account_id` 显式指定。旧实现继承
       paper 账户 ⇒ 必然 `testnet=True` ⇒ 新 live 会话必然被 `mode_account_mismatch`
       拒绝，于是这个接口**永远 400**。源码注释当时就承认「要让 promote 真正可用，
       得让它接受一个 `testnet=False` 的目标账户——那是新增能力」；本票就是来加这个能力的。
    2. **前置闸门收紧**：源 paper 会话必须有候选主行且状态为
       `awaiting_live_approval`（跑满观察期且判定通过）。口径的**唯一出处**是
       `apps.strategy_factory.live_gate`，这里不重算一遍观察期（重算 = 第二处口径）。

    `mode_account_mismatch` 不变式**不放松**：它仍是最后一道校验（见下方显式调用）。
    """

    from apps.strategy_factory.live_gate import evaluate_live_gate

    try:
        session = LiveSession.objects.get(pk=pk, user=request.user)
    except LiveSession.DoesNotExist:
        return Response({"error": "Live session not found"}, status=404)

    if session.mode != "paper":
        return Response({"error": "Only paper sessions can be promoted"}, status=400)

    if session.status not in ("running", "stopped", "paused"):
        return Response(
            {"error": f"Session is {session.status}, cannot promote"}, status=400
        )

    # 前置闸门（D12 后半）：跑满观察期且判定通过。fail-closed。
    gate = evaluate_live_gate(session)
    if not gate.allowed:
        return Response({"error": gate.reason}, status=status.HTTP_400_BAD_REQUEST)

    # 目标实盘账户：显式指定，**不继承**源 paper 账户。
    target_account_id = request.data.get("exchange_account_id")
    if not target_account_id:
        return Response(
            {
                "error": (
                    "升实盘必须显式指定目标实盘账户（exchange_account_id，testnet=False）。"
                    "paper 会话绑的是 testnet 账户，它不能当实盘账户用——"
                    "旧的「原样继承」正是这个接口必然失败的原因。"
                )
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    from apps.exchange.models import ExchangeAccount

    try:
        target_account = ExchangeAccount.objects.get(
            pk=target_account_id, is_active=True
        )
    except (ExchangeAccount.DoesNotExist, ValidationError, ValueError):
        return Response({"error": "Exchange account not found or inactive"}, status=404)

    # 目标账户必须真的是实盘账户。**这一条先于 `confirmed` 检查**：一个 testnet
    # 账户无论确认与否都不该被用来建 live 会话，早一次拒绝就少一次「我确认了却没用」。
    if target_account.testnet:
        return Response(
            {
                "error": (
                    f"升实盘需要实盘账户（testnet=False），但指定的账户"
                    f"「{target_account.label}」testnet=True。"
                    "请改选一个 testnet=False 的账户。"
                )
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    # runner 共享该品种的账户级持仓：同账户同品种两个策略必然互相平仓
    # （2026-10-07：box 会话平掉 donchian 开的 DOGE/USDT 仓）。与 create 同一道闸门。
    conflict = LiveSession.conflicting_active_session(target_account, session.symbol)
    if conflict:
        return Response({"error": conflict}, status=status.HTTP_400_BAD_REQUEST)

    # 门①：是否已显式确认。fail-closed——认不出的值一律当没确认（干跑）。
    if not _confirmed(request.data.get("confirmed")):
        return Response(
            {
                "action": "promote",
                "dry_run": True,
                "candidate": {
                    "id": str(gate.candidate.pk),
                    "name": gate.candidate.name,
                    "status": gate.candidate.status,
                },
                "paper_session": {
                    "id": str(session.id),
                    "symbol": session.symbol,
                    "status": session.status,
                },
                "target_account": {
                    "id": str(target_account.id),
                    "label": target_account.label,
                    "testnet": target_account.testnet,
                },
                "will_inherit": {
                    "initial_capital": str(
                        session.current_equity or session.initial_capital
                    ),
                    "config": session.config,
                },
                "message": (
                    "这是干跑：还没有做任何改动。确认后将停止该 paper 会话，"
                    f"并用账户「{target_account.label}」建一条 live 会话。"
                    "把上面内容给用户看并得到明确同意后，带 confirmed=true 重新调用。"
                ),
            },
            status=status.HTTP_200_OK,
        )

    # -- 已确认：停止 paper 会话 + 建 live 会话（同一事务） ---------------- #
    from django.db import transaction

    now = timezone.now()
    with transaction.atomic():
        # 时间线（#24，D8）：promote 停掉 paper 会话也是一次「离开 running」，
        # 它的最后一段 running 终点同样要落地——观察期判定在 promote 之前发生，
        # 但判定要可复核，时间线就必须完整到 promote 这一刻。
        record_status_change(session, to_status="stopped", changed_at=now)
        session.status = "stopped"
        session.stopped_at = now
        session.save(update_fields=["status", "stopped_at", "updated_at"])

        # 继承 paper 期的权益与配置（D11，用户已定夺 Q3）：equity 从 paper 会话取，
        # config 逐键继承（`dict()` 拷贝——直接传 `session.config` 会让两条会话共享
        # 同一个可变对象，改一条的另一条跟着变）。
        inherited_capital = session.current_equity or session.initial_capital
        live_session = LiveSession.objects.create(
            user=request.user,
            backtest_result=session.backtest_result,
            strategy=session.strategy,
            symbol=session.symbol,
            mode="live",
            status="pending",
            exchange_account=target_account,
            initial_capital=inherited_capital,
            current_equity=inherited_capital,
            config=dict(session.config or {}),
        )

        # 最后一道校验：不变式**不放松**，只把「继承」换成「显式指定」。
        # 走到这里还错配，说明上面的前置漏了某个组合——fail-loud，删掉刚建的会话
        # （回滚）比留一支起不来的会话好。
        mismatch = live_session.mode_account_mismatch()
        if mismatch:
            transaction.set_rollback(True)
            return Response({"error": mismatch}, status=status.HTTP_400_BAD_REQUEST)

        # 候选主行：记下目标账户与 live 会话，并把状态推进到 `live`（终态）。
        # `decided_at` 由这一步写（D1：人工什么时候点的，不该由状态机替人回答）。
        candidate = gate.candidate
        candidate.live_account = target_account
        candidate.live_session = live_session
        candidate.decided_at = now
        candidate.transition_to("live")
        candidate.save(
            update_fields=[
                "live_account",
                "live_session",
                "decided_at",
                "status",
                "updated_at",
            ]
        )

    return Response(
        {
            "id": str(live_session.id),
            "mode": live_session.mode,
            "status": live_session.status,
            "candidate_id": str(gate.candidate.pk),
            "exchange_account_id": str(target_account.id),
            "message": "Promoted to live trading session",
        },
        status=status.HTTP_201_CREATED,
    )
