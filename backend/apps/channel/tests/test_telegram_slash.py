"""Telegram 的斜杠通路（第①段单元 8ii 的前置）。

CONTEXT.md 第 119 条把「修正 Telegram 斜杠通路」列为 v1 前置，并点名了原因：Telegram
的消息过滤器把斜杠消息挡在 Agent 之外，**只改 Agent 的命令表在 Telegram 上不生效**。
坏掉的样子是：命令发出去，什么都没发生——而「什么都没发生」与「Agent 正在想」在聊天
窗口里长得一样，所以这件事必须在测试里看得见，不能靠人去发现。

这里钉三件：

1. 兜底过滤器**收**未注册的斜杠消息（`/event …`、`/new`、`/cancel`）。这四个已经单独
   注册了处理器的命令（`/start` `/status` `/stop` `/help`）不在此列。
2. 同一个 group 内**第一个命中的处理器胜出**（Python-Telegram-Bot 的既定语义），
   所以已注册的那四个不会被兜底抢走——这一条如果反过来，`/status` 会变成一条发往
   Agent 的普通消息。
3. `/help` 里列出的命令与实际能用的命令对得上。

不起数据库，也不联网：`Update` 用 `Update.de_json` 造（就是 Telegram 发过来的那个
形状），Bot 用一个假 token 造，分派循环照 `Application.process_update` 的判定抄一遍。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from telegram import Bot, Update, User
from telegram.ext import filters

from apps.channel.telegram import (
    _MESSAGE_FILTER,
    TelegramChannel,
)

BOT = Bot(token="42:TEST")

# `CommandHandler.check_update` 要拿 bot 自己的用户名去比对 `/start@本bot` 那种写法，
# 而那条路径是 `Bot.username` → `get_me()`——网络调用。这里把 `get_me()` 的缓存值直接
# 填上：测验要的是分派判定，不是 Telegram 的连通性。
BOT._bot_user = User(id=42, is_bot=True, first_name="Bot", username="testbot")

#: 只用一次：`Bot` 会被 `Update.de_json` 拿去填 `get_bot()`。
CHAT_ID = 12345
USER_ID = 42


def _update(text: str, *, command: bool = True) -> Update:
    """造一条 Telegram 更新。

    Telegram 只给**看起来像命令**的那一段打 `bot_command` 实体，于是 `filters.COMMAND`
    认的正是这个实体（`Command.filter` 要求 `entities[0].type == BOT_COMMAND` 且
    `offset == 0`）。把实体按同样的规则造出来，测的才是真实形状。
    """
    entities = []
    if command and text.startswith("/"):
        head = text.split()[0]
        entities = [{"type": "bot_command", "offset": 0, "length": len(head)}]
    return Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 0,
                "text": text,
                "entities": entities,
                "chat": {"id": CHAT_ID, "type": "private"},
                "from": {"id": USER_ID, "is_bot": False, "first_name": "ops"},
            },
        },
        BOT,
    )


class _FakeApp:
    """只收 `add_handler` 的壳子——`start()` 里真正用到的那一个方法。"""

    def __init__(self):
        self.handlers: list = []

    def add_handler(self, handler) -> None:
        self.handlers.append(handler)


def _registered(channel: TelegramChannel) -> list:
    app = _FakeApp()
    channel._register_handlers(app)
    return app.handlers


def _first_matching(handlers: list, update: Update):
    """照 `Application.process_update` 的判定走：命中即停。

    PTB 的原话是 `if check is None or check is False: continue` 之后 `break`，所以
    **空参数列表也算命中**（`[]` 既不是 None 也不是 False）。
    """
    for handler in handlers:
        check = handler.check_update(update)
        if check is None or check is False:
            continue
        return handler
    return None


class TestTheFallbackFilterAdmitsUnregisteredCommands:
    def test_an_unregistered_slash_command_reaches_the_agent(self):
        update = _update("/event add 高 全市场 2026-09-25 20:30 FOMC")
        assert _MESSAGE_FILTER.check_update(update)

    def test_the_old_filter_would_have_dropped_it(self):
        # 这一条钉的是**曾经坏掉的样子**：带着 `~filters.COMMAND` 的过滤器会把任何斜杠
        # 消息挡在外面。留着它，是为了让下一个人改回去时立刻被拦下。
        update = _update("/event add 高 全市场 2026-09-25 20:30 FOMC")
        assert not (filters.TEXT & ~filters.COMMAND).check_update(update)

    def test_ordinary_text_still_passes(self):
        assert _MESSAGE_FILTER.check_update(
            _update("帮我看下现在的行情", command=False)
        )

    def test_a_slash_that_is_only_a_path_is_not_a_command(self):
        # 「/usr/local 这个路径」没有 bot_command 实体——它本来就该照常走 Agent。
        assert _MESSAGE_FILTER.check_update(
            _update("/usr/local 这个路径", command=False)
        )


class TestTheRegisteredHandlersSplitTheTraffic:
    def setup_method(self):
        self.channel = TelegramChannel(token="test_token", supervisor_agent=MagicMock())
        self.handlers = _registered(self.channel)

    def _handler_name(self, text: str, *, command: bool = True) -> str:
        handler = _first_matching(self.handlers, _update(text, command=command))
        assert handler is not None, text
        callback = handler.callback
        return getattr(callback, "__name__", repr(callback))

    def test_the_four_registered_commands_keep_their_own_handlers(self):
        # 顺序错了的话，这条会落到 `_on_message` 上——`/status` 于是变成一句发给 Agent 的
        # 普通消息，「查看框架状态」就再也不会有回复。
        assert self._handler_name("/start") == "_cmd_start"
        assert self._handler_name("/status") == "_cmd_status"
        assert self._handler_name("/stop") == "_cmd_stop"
        assert self._handler_name("/help") == "_cmd_help"

    def test_the_agent_side_commands_fall_through_to_the_pipeline(self):
        assert self._handler_name("/new") == "_on_message"
        assert self._handler_name("/cancel") == "_on_message"
        assert (
            self._handler_name("/event add 高 全市场 2026-09-25 20:30 FOMC")
            == "_on_message"
        )

    def test_an_unknown_command_also_falls_through(self):
        # 未知命令要让 Agent 回一句「未知命令: /halt」并列出可用命令；在 Telegram 这一层
        # 丢掉的话，人只会看到沉默。
        assert self._handler_name("/halt") == "_on_message"

    def test_plain_text_still_falls_through(self):
        assert self._handler_name("现在是什么行情", command=False) == "_on_message"


class TestTheHelpTextMatchesWhatActuallyWorks:
    def test_the_agent_side_commands_are_listed(self):
        # `Message` 是冻结的（PTB 的 `TelegramObject` 不许挂属性），所以这里整个 update
        # 用替身：`_cmd_help` 只碰 `update.message.reply_text` 这一处。
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        channel = TelegramChannel(token="test_token", supervisor_agent=MagicMock())

        asyncio.run(channel._cmd_help(update, MagicMock()))

        text = update.message.reply_text.await_args.args[0]
        for command in (
            "/start",
            "/status",
            "/stop",
            "/help",
            "/new",
            "/cancel",
            "/event",
            "/regime",
        ):
            assert command in text, command
