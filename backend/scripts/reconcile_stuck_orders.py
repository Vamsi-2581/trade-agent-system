"""运维工具：对账卡在活跃状态的订单（成交同步补跑）。

用法（后端容器内）：
    DJANGO_SETTINGS_MODULE=core.settings.dev python scripts/reconcile_stuck_orders.py [symbol]

按订单自身所属的交易所账户构建适配器直查交易所，
把 pending/submitted/partial 订单更新为真实成交状态。
"""

import asyncio
import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings.dev")

import django

django.setup()

from django.conf import settings  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402

from apps.trading.models import Order  # noqa: E402
from apps.trading.adapters.binance import BinanceAdapter  # noqa: E402
from apps.trading.adapters.base import OrderNotFoundError  # noqa: E402


def _to_bytes(value):
    if isinstance(value, (bytes, str)):
        return value if isinstance(value, bytes) else value.encode()
    if isinstance(value, (memoryview, bytearray)):
        return bytes(value)
    return None


def main():
    symbol_filter = sys.argv[1].upper() if len(sys.argv) > 1 else ""
    active = ["pending", "submitted", "partial"]
    qs = Order.objects.select_related("exchange_account").filter(status__in=active)
    if symbol_filter:
        qs = qs.filter(symbol__icontains=symbol_filter)
    orders = list(qs.order_by("-created_at"))
    if not orders:
        print("NO_ACTIVE_ORDERS")
        return

    fernet = Fernet(settings.FERNET_KEY.encode())
    print(f"FOUND {len(orders)} active order(s)")

    for order in orders:
        acc = order.exchange_account
        api_key = _to_bytes(acc.api_key_enc)
        api_secret = _to_bytes(acc.api_secret_enc)
        if not api_key or not api_secret or not order.exchange_order_id:
            print(f"SKIP {order.id} {order.symbol} (missing keys/order id)")
            continue
        try:
            key = fernet.decrypt(api_key).decode()
            secret = fernet.decrypt(api_secret).decode()
            adapter = BinanceAdapter(key, secret, testnet=acc.testnet)

            async def _fetch():
                await adapter.connect()
                try:
                    return await adapter.fetch_order(
                        order.exchange_order_id, order.symbol
                    )
                finally:
                    await adapter.disconnect()

            fill = asyncio.run(_fetch())
            updates = {"status": fill.status}
            if fill.filled_quantity is not None:
                updates["filled_quantity"] = fill.filled_quantity
            if fill.avg_fill_price is not None:
                updates["avg_fill_price"] = fill.avg_fill_price
            Order.objects.filter(id=order.id).update(**updates)
            print(
                f"SYNCED {order.symbol} {order.status} -> {fill.status} "
                f"filled={fill.filled_quantity} avg={fill.avg_fill_price} "
                f"(account={acc.label or acc.exchange}, testnet={acc.testnet})"
            )
        except OrderNotFoundError as e:
            Order.objects.filter(id=order.id).update(
                status="cancelled", error_message=str(e)
            )
            print(f"SYNCED_AS_CANCELLED {order.symbol} {order.id}: {e}")
        except Exception as e:
            print(f"SYNC_ERROR {order.symbol} {order.id}: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
