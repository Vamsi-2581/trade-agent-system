from __future__ import annotations

import re
from typing import Optional


class IntentRouter:
    """动态意图路由注册表

    支持运行时注册意图与Agent的映射、框架操作意图、降级规则。
    SubAgent 可通过装饰器或调用注册方法动态添加意图。
    """

    _instance: Optional["IntentRouter"] = None

    def __init__(self):
        # 意图 → Agent 名称
        self._intent_to_agent: dict[str, str] = {}
        # Agent 名称 → 意图（反向映射）
        self._agent_to_intent: dict[str, str] = {}
        # 框架意图 → (frame_type, action)
        self._frame_intents: dict[str, tuple[str, str]] = {}
        # 降级规则列表 [(regex_pattern, intent), ...]
        self._fallback_rules: list[tuple[str, str]] = []

    @classmethod
    def get_instance(cls) -> "IntentRouter":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """重置单例（主要用于测试）"""
        cls._instance = None

    # ---- 意图注册 ----

    def register_intent(self, intent: str, agent_name: str) -> None:
        """注册单个意图到Agent的映射"""
        self._intent_to_agent[intent] = agent_name
        self._agent_to_intent[agent_name] = intent

    def register_intents(self, intent_map: dict[str, str]) -> None:
        """批量注册意图映射 {intent: agent_name}"""
        for intent, agent in intent_map.items():
            self.register_intent(intent, agent)

    def unregister_intent(self, intent: str) -> None:
        """取消注册意图"""
        agent = self._intent_to_agent.pop(intent, None)
        if agent:
            self._agent_to_intent.pop(agent, None)

    # ---- 框架意图注册 ----

    def register_frame_intent(self, intent: str, frame_type: str, action: str) -> None:
        """注册框架操作意图"""
        self._frame_intents[intent] = (frame_type, action)
        self.register_intent(intent, "frame_manager")

    def unregister_frame_intent(self, intent: str) -> None:
        """取消注册框架意图"""
        self._frame_intents.pop(intent, None)
        self.unregister_intent(intent)

    # ---- 降级规则注册 ----

    def register_fallback_rule(self, pattern: str, intent: str) -> None:
        """注册降级规则：正则匹配 → 意图"""
        self._fallback_rules.append((pattern, intent))

    def register_fallback_rules(self, rules: list[tuple[str, str]]) -> None:
        """批量注册降级规则 [(pattern, intent), ...]"""
        self._fallback_rules.extend(rules)

    # ---- 查询接口 ----

    def get_agent_for_intent(self, intent: str) -> str | None:
        """获取意图对应的Agent名称"""
        return self._intent_to_agent.get(intent)

    def get_intent_for_agent(self, agent_name: str) -> str | None:
        """获取Agent对应的意图"""
        return self._agent_to_intent.get(agent_name)

    def get_frame_intent(self, intent: str) -> tuple[str, str] | None:
        """获取框架意图的 (frame_type, action)"""
        return self._frame_intents.get(intent)

    def is_frame_intent(self, intent: str) -> bool:
        """判断是否为框架意图"""
        return intent in self._frame_intents

    def all_intents(self) -> list[str]:
        """返回所有已注册的意图"""
        return list(self._intent_to_agent.keys())

    def all_frame_intents(self) -> list[str]:
        """返回所有已注册的框架意图"""
        return list(self._frame_intents.keys())

    def match_fallback(self, text: str) -> str | None:
        """按降级规则匹配文本，返回第一个匹配的意图"""
        for pattern, intent in self._fallback_rules:
            if re.search(pattern, text, re.IGNORECASE):
                return intent
        return None

    def valid_intents_for_prompt(self) -> list[str]:
        """返回所有有效意图（用于 LLM prompt 构建）"""
        return self.all_intents()


# ------------------------------------------------------------------ #
#  模块级装饰器（兼容旧 API）                                           #
# ------------------------------------------------------------------ #


def register_intent(intent: str, agent_name: str):
    """装饰器：注册意图到Agent的映射

    @register_intent("analyze_chart", "analyst")
    class AnalystAgent(BaseAgent):
        ...
    """

    def decorator(target):
        IntentRouter.get_instance().register_intent(intent, agent_name)
        return target

    return decorator


def register_frame_intent(intent: str, frame_type: str, action: str):
    """装饰器：注册框架操作意图

    @register_frame_intent("create_frame", "frame", "create")
    class FrameManagerAgent(BaseAgent):
        ...
    """

    def decorator(target):
        IntentRouter.get_instance().register_frame_intent(intent, frame_type, action)
        return target

    return decorator


def register_fallback_rule(pattern: str, intent: str):
    """装饰器：注册降级规则

    @register_fallback_rule(r"帮我.*分析", "analyze_chart")
    def some_handler():
        ...
    """

    def decorator(target):
        IntentRouter.get_instance().register_fallback_rule(pattern, intent)
        return target

    return decorator
