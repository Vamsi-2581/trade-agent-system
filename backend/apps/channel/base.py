from __future__ import annotations
import logging
import re
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


def chunk_message(text: str, max_length: int = 3800) -> list[str]:
    """将长文本按语义边界拆分为多段，每段不超过 max_length。

    分割优先级：空行 > 换行 > 句末标点（。！？.!?） > 硬切。
    """
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    _split_into(text, max_length, chunks)
    return chunks


def _split_into(text: str, max_length: int, out: list[str]) -> None:
    if len(text) <= max_length:
        if text:
            out.append(text)
        return

    # 尝试按空行分割
    double_nl = text.find("\n\n")
    if double_nl != -1 and double_nl <= max_length:
        out.append(text[:double_nl])
        _split_into(text[double_nl + 2 :], max_length, out)
        return

    # 尝试按换行分割
    single_nl = text.find("\n")
    if single_nl != -1 and single_nl <= max_length:
        out.append(text[:single_nl])
        _split_into(text[single_nl + 1 :], max_length, out)
        return

    # 尝试按句末标点分割（中英文）
    m = re.search(r"[。！？.!?]+", text)
    if m:
        cut = m.end()
        if cut <= max_length:
            out.append(text[:cut])
            _split_into(text[cut:], max_length, out)
            return
        # 第一个句号就超长了，找最后一个在 max_length 内的句号
        last_cut = 0
        for m2 in re.finditer(r"[。！？.!?]+", text[:max_length]):
            last_cut = m2.end()
        if last_cut > 0:
            out.append(text[:last_cut])
            _split_into(text[last_cut:], max_length, out)
            return

    # 硬切
    out.append(text[:max_length])
    _split_into(text[max_length:], max_length, out)


class BaseChannel(ABC):
    """用户交互入口抽象接口。Phase1: Telegram。Phase2: Web。"""

    name: str = "base"

    @abstractmethod
    async def send_message(self, text: str) -> None:
        """向用户发送文本消息"""

    @abstractmethod
    async def send_photo(self, photo_bytes: bytes, caption: str = "") -> None:
        """向用户发送图片（K线图、回测结果等）"""

    @abstractmethod
    async def start(self) -> None:
        """启动Channel监听"""

    @abstractmethod
    async def stop(self) -> None:
        """停止Channel监听"""
