import logging
from decimal import Decimal

from asgiref.sync import async_to_sync
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.utils import timezone
from .models import Order, Strategy, LiveSession
from .serializers import OrderSerializer, StrategySerializer, LiveSessionSerializer

logger = logging.getLogger(__name__)


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
        {"symbol": symbol, "interval": interval, "count": len(ohlcv), "ohlcv_data": ohlcv}
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
    active_count = Order.objects.filter(
        status__in=Order.ACTIVE_STATUSES
    ).count()

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

    # 按**账户**遍历，不按交易所名：`_adapters` 以交易所名为键，同交易所有两个活跃
    # 账户时它只留最后一个，于是两个账户的持仓都会记在那个账户名下（原先这里是
    # `exchange -> account_id` 手工反查，同样是后写覆盖）。账户地图才是每个账户各自
    # 的适配器，`exchange_account_id` 也就直接是键本身。
    account_by_id = {
        str(acc.id): acc for acc in ExchangeAccount.objects.filter(is_active=True)
    }

    all_positions = []
    for account_id, adapter in executor._account_adapters.items():
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
        for account_id, adapter in executor._account_adapters.items():
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


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def live_session_create(request):
    """
    从回测创建并启动实盘会话。

    Request body:
      backtest_result_id: str (UUID) — 来源回测
      mode: str — 'paper' 或 'live'（首次必须为 paper）
      exchange_account_id: str (UUID) — 交易所账户
      config: dict (optional) — 运行参数
    """
    from apps.backtest.models import BacktestResult
    from apps.exchange.models import ExchangeAccount

    backtest_id = request.data.get("backtest_result_id")
    mode = request.data.get("mode", "paper")
    exchange_account_id = request.data.get("exchange_account_id")
    config = request.data.get("config", {})

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

    # 强制先模拟：live 模式必须有 paper 会话记录
    if mode == "live":
        has_paper = LiveSession.objects.filter(
            user=request.user,
            backtest_result=backtest,
            mode="paper",
        ).exists()
        if not has_paper:
            return Response(
                {"error": "Must run a paper trading session first before going live"},
                status=400,
            )

    # 校验交易所账户
    try:
        exchange_account = ExchangeAccount.objects.get(
            pk=exchange_account_id, is_active=True
        )
    except ExchangeAccount.DoesNotExist:
        return Response({"error": "Exchange account not found or inactive"}, status=404)

    # 初始资金取绑定账户的实时 USDT 余额，而非回测的固定金额
    try:
        initial_capital = async_to_sync(_fetch_usdt_balance)(exchange_account)
    except Exception as e:
        logger.exception("Failed to fetch account balance for session creation: %s", e)
        return Response(
            {"error": f"Failed to fetch exchange account balance: {e}"},
            status=status.HTTP_502_BAD_GATEWAY,
        )

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

    frame_manager = FrameManager.get_instance()

    # 如果已在运行，先停掉旧运行器（处理之前框架崩溃导致的幽灵状态）
    # 注意：只停当前 session 的 runner，不动整个 trading frame（其他策略可能还在跑）
    if session.status == "running":
        logger.warning(
            "Session %s is already running, stopping old runner before restart", pk
        )
        try:
            async_to_sync(frame_manager.stop_strategy_runner)(live_session_id=str(session.id))
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
            async_to_sync(frame_manager.stop_strategy_runner)(live_session_id=str(session.id))
        except Exception:
            pass
        return Response({"error": f"Failed to start strategy: {e}"}, status=500)

    session.status = "running"
    session.started_at = timezone.now()
    session.stopped_at = None
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
            "notice": start_notice.start_notice(session.symbol, str(session.strategy_id)),
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

    session.status = "running"
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
            "notice": start_notice.start_notice(session.symbol, str(session.strategy_id)),
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

    # promote 把源账户**原样继承**给新的 live 会话，而 live 会话按不变式要求
    # testnet=False；源会话是 paper、按同一条不变式必须绑 testnet 账户，所以继承
    # 来的账户必然 testnet=True——产物是一支永远启动不了的 live 会话，而本接口却
    # 返回 201「Promoted to live trading session」。一个起不来的会话配一句成功
    # 文案，比直接失败更难查，所以这里 fail-loud。
    # （要让 promote 真正可用，得让它接受一个 testnet=False 的目标账户——那是新增
    # 能力，不在第①段「只把静默失效变成可见失败」的范围内。）
    account = session.exchange_account
    if account is None:
        return Response(
            {"error": "源会话未绑定交易所账户，无法 promote 出实盘会话"}, status=400
        )
    if account.testnet:
        return Response(
            {
                "error": (
                    f"promote 需要实盘账户（testnet=False），但源会话绑定的"
                    f"「{account.label}」testnet=True：新的 live 会话会继承该账户，"
                    f"启动时必被 mode 一致性校验拒绝。"
                    "请改用 testnet=False 的账户直接创建 live 会话。"
                )
            },
            status=400,
        )

    # 停止当前 paper 会话
    session.status = "stopped"
    session.stopped_at = timezone.now()
    session.save(update_fields=["status", "stopped_at", "updated_at"])

    # 创建新的 live 会话
    live_session = LiveSession.objects.create(
        user=request.user,
        backtest_result=session.backtest_result,
        strategy=session.strategy,
        symbol=session.symbol,
        mode="live",
        status="pending",
        exchange_account=session.exchange_account,
        initial_capital=session.current_equity or session.initial_capital,
        current_equity=session.current_equity or session.initial_capital,
        config=session.config,
    )

    return Response(
        {
            "id": str(live_session.id),
            "mode": live_session.mode,
            "status": live_session.status,
            "message": "Promoted to live trading session",
        },
        status=status.HTTP_201_CREATED,
    )
