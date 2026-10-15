"""把「已通过 `test_strategy` 的策略文件」登记为候选策略（spec #19 D16 / D17）。

这是生成侧的**收尾动作**：`quant` agent 写完策略文件、`test_strategy` 跑通之后，调用本
工具把这份代码登记成一条 `StrategyCandidate`，之后由流水线接管（#22 的 `advance()` 负责
「回测 → 观察 → 上实盘」的推进），agent 不再自己往下走。

两条纪律，都是 spec 明写的：

1. **登记前先校验注册键（D17）**。`StrategyRegistry.discover()` 会**同时按文件名与装饰器名**
   注册（实测 21 个模块 → 32 个键），目录里还躺着 `unnamed` 这类占位键，另有一个文件因
   `SyntaxError` 被 `logger.warning` 静默吞掉。所以「登记成功」的判据**不能**是「注册表里
   有个同名的东西」，而必须是：**注册键唯一、等于策略自己声明的名字、且能解析到唯一的实现
   类**。任何一条不满足都落 `registration_failed` 并写明**可自我修正**的原因——静默失败穿
   过去，生成侧就永远不知道自己写坏了。

2. **绝不写 `Strategy.is_active`（D2）**。它是人工总开关（`False` = 已退役，永不被自动停用、
   也永不被自动恢复），候选生命周期与它互不干扰。本模块按注册键取/建 `trading.Strategy` 行
   时**不传 `is_active`**，走模型默认值；已存在的行一个字段都不改。

**登记失败也建候选行**（D1：`strategy` 外键「登记失败时为空」；US26：失败的候选要留在系统里
并带原因）。失败行先按 `registered` 建出来，再走 `transition_to()` 落到 `registration_failed`
——那条边在 D3 的转移表里是合法的，这里让它做一次真实的守门，而不是直接给 `status=` 赋值。

**参数级错误（缺必填项 / `origin` 不认识 / `paper_account` 解析不到）不建行**：那是调用方还没
把话说清楚，没有一次「登记尝试」发生过，建行只会往库里塞噪音。这些错误在 `execute()` 里当场
返回，消息里写清怎么改。
"""

from __future__ import annotations

import ast
import importlib.util
import logging
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from django.core.exceptions import ValidationError

from .base import BaseTool, ToolResult
from .file_io import WORKSPACE_ROOT

logger = logging.getLogger(__name__)

#: 策略文件目录名（`~/.tradelogx/strategies/`）。注册表只扫这个目录（D17），所以登记
#: 成功的前提之一就是文件落在这里——落在别处的文件在注册表里根本不存在。
STRATEGIES_DIRNAME = "strategies"

#: 占位名。`BaseStrategy.name` 的默认值就是 `"unnamed"`，于是**没声明 `name` 的策略类**
#: 会带着这个占位名被注册（实测 `obi_vwap_strategy.py` 就是这一档）。它不是一个身份，
#: 而是一次漏写——放行它等于让一条候选永远答不出「我是谁」。
PLACEHOLDER_NAMES = frozenset({"", "unnamed"})


class RegistrationProblem(str, Enum):
    """登记失败的原因码。

    做成枚举而不是自由字符串：原因码是**机器可读的结论**（落 `evidence`，通知与事后归因
    都读它），自由字符串拦不住「同一个意思两种写法」，那会让「生成侧最常错在哪」变成手工活。
    """

    FILE_NOT_FOUND = "file_not_found"
    SYNTAX_ERROR = "syntax_error"
    MODULE_LOAD_ERROR = "module_load_error"
    NO_STRATEGY_CLASS = "no_strategy_class"
    DESCRIPTION_TEMPLATE_MISSING = "description_template_missing"
    STRATEGY_NAME_MISSING = "strategy_name_missing"
    STRATEGY_NAME_MISMATCH = "strategy_name_mismatch"
    REGISTRY_KEY_UNRESOLVED = "registry_key_unresolved"
    REGISTRY_KEY_NOT_UNIQUE = "registry_key_not_unique"


#: 原因码 → 展示名（进通知文案与工具返回）。
_PROBLEM_DISPLAY = {
    RegistrationProblem.FILE_NOT_FOUND: "策略文件不存在",
    RegistrationProblem.SYNTAX_ERROR: "语法错",
    RegistrationProblem.MODULE_LOAD_ERROR: "模块加载失败",
    RegistrationProblem.NO_STRATEGY_CLASS: "文件里没有策略类",
    RegistrationProblem.DESCRIPTION_TEMPLATE_MISSING: "描述模板不合规",
    RegistrationProblem.STRATEGY_NAME_MISSING: "策略类没有声明 name",
    RegistrationProblem.STRATEGY_NAME_MISMATCH: "声明的名字与文件里的 name 不一致",
    RegistrationProblem.REGISTRY_KEY_UNRESOLVED: "注册表里解析不到这个名字",
    RegistrationProblem.REGISTRY_KEY_NOT_UNIQUE: "注册键不唯一",
}


@dataclass(frozen=True)
class RegistrationFailure:
    """一次登记失败的完整说明：原因码 + 人话原因 + **怎么修**。

    `hint` 是这份结构里最重要的字段——D17 要的不是「失败了」，而是「失败原因可自我修正」。
    生成侧（LLM）读的就是它。
    """

    code: RegistrationProblem
    reason: str
    hint: str

    @property
    def display(self) -> str:
        return _PROBLEM_DISPLAY[self.code]


def resolve_strategy_path(raw_path: str) -> Path:
    """把工具参数解析成绝对路径。

    与 `test_strategy` 同一口径：绝对路径原样用，相对路径相对 `~/.tradelogx/` 解析
    （所以 `strategies/xxx.py` 与 `xxx.py` 都能工作）。
    """
    path = Path(raw_path)
    if not path.is_absolute():
        path = WORKSPACE_ROOT / raw_path
    return path


def inspect_strategy_file(
    path: Path, strategy_name: str
) -> tuple[type | None, RegistrationFailure | None]:
    """加载策略文件并做**文件级**校验，返回 `(策略类, 失败说明)`。

    校验顺序是从「最根本」到「最具体」，这样报出来的永远是**第一个**该修的问题：文件在不在
    → 语法对不对 → 有没有策略类 → 描述模板全不全 → 名字声明了没有、对不对。

    刻意**不用**注册表来做这些判断：`discover()` 把异常吞成 `logger.warning`，从注册表
    只能看到「这个名字没有」，看不到「为什么没有」。要给出可自我修正的原因，就必须自己
    把文件读开、把异常接住。
    """
    from apps.strategy_engine.base import BaseStrategy
    from apps.strategy_engine.registry import StrategyRegistry

    if not path.exists():
        return None, RegistrationFailure(
            RegistrationProblem.FILE_NOT_FOUND,
            f"策略文件不存在：{path}",
            f"先用 write_file 把策略写到 ~/.tradelogx/{STRATEGIES_DIRNAME}/"
            f"{strategy_name}.py，再登记；注册表只扫这个目录（D17）。",
        )

    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, RegistrationFailure(
            RegistrationProblem.FILE_NOT_FOUND,
            f"策略文件读不出来：{path}（{exc}）",
            "检查文件权限，或重新用 write_file 写一遍。",
        )

    try:
        ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return None, RegistrationFailure(
            RegistrationProblem.SYNTAX_ERROR,
            f"策略文件语法错：第 {exc.lineno} 行 {exc.msg}",
            "修正这一行的 Python 语法后重新调用 test_strategy，通过后再登记。",
        )

    module_name = path.stem
    try:
        strategy_cls = _load_first_strategy_class(module_name, path, BaseStrategy)
    except ValueError as exc:
        # `@register_strategy` 装饰器在**import 时**就调 `validate_description`，所以描述
        # 模板不合规是**加载期**抛出来的 `ValueError`。不单独接住它，这条最常见的失败会被
        # 报成一句泛泛的「模块加载失败」，生成侧就拿不到「缺哪四个字段」这个可修正的原因。
        if "description" in str(exc):
            return None, RegistrationFailure(
                RegistrationProblem.DESCRIPTION_TEMPLATE_MISSING,
                f"策略描述不符合 4 字段模板：{exc}",
                "description 必须逐行包含「策略类型：」「核心指标：」「适用场景：」「入场逻辑：」"
                "四个字段（详见 create-strategy 阶段 3.1）。",
            )
        return None, RegistrationFailure(
            RegistrationProblem.MODULE_LOAD_ERROR,
            f"策略文件加载失败：ValueError: {exc}",
            "检查策略类里 import 期执行的代码（装饰器参数、模块级常量）是否合法。",
        )
    except Exception as exc:  # noqa: BLE001 — 任何导入期异常都要变成可修正的原因
        return None, RegistrationFailure(
            RegistrationProblem.MODULE_LOAD_ERROR,
            f"策略文件加载失败：{type(exc).__name__}: {exc}",
            "多半是 import 路径写错或依赖不存在。按 create-strategy 的「导入路径」章节"
            "改成 apps.strategy_engine.* 的精确导入后重试。",
        )

    if strategy_cls is None:
        return None, RegistrationFailure(
            RegistrationProblem.NO_STRATEGY_CLASS,
            f"文件里没有继承 BaseStrategy 的策略类：{path}",
            "按 create-strategy 的模板生成一个继承 BaseStrategy 的策略类（并加 "
            "@register_strategy() 装饰器）。",
        )

    try:
        StrategyRegistry.validate_description(strategy_cls)
    except ValueError as exc:
        return None, RegistrationFailure(
            RegistrationProblem.DESCRIPTION_TEMPLATE_MISSING,
            f"策略描述不符合 4 字段模板：{exc}",
            "description 必须逐行包含「策略类型：」「核心指标：」「适用场景：」「入场逻辑：」"
            "四个字段（详见 create-strategy 阶段 3.1）。",
        )

    declared = getattr(strategy_cls, "name", "") or ""
    if declared in PLACEHOLDER_NAMES:
        return None, RegistrationFailure(
            RegistrationProblem.STRATEGY_NAME_MISSING,
            f"策略类没有声明自己的 name（取到的是占位名 {declared!r}）："
            f"{strategy_cls.__name__}",
            f'在策略类里显式写 name = "{strategy_name}"（装饰器上的 name= 只决定注册键，'
            "不会写回类属性）。",
        )

    if declared != strategy_name:
        return None, RegistrationFailure(
            RegistrationProblem.STRATEGY_NAME_MISMATCH,
            f"登记用的名字 {strategy_name!r} 与策略文件声明的 name {declared!r} 不一致",
            f"二选一：用 strategy_name={declared!r} 重新登记，或把文件里的 name 改成 "
            f"{strategy_name!r}。两者必须相等，否则注册键会变成文件名别名（D17）。",
        )

    return strategy_cls, None


def _load_first_strategy_class(
    module_name: str, path: Path, base_cls: type
) -> type | None:
    """按文件路径加载模块，返回其中**定义在本模块**的第一个策略类。

    `__module__` 必须等于 `module_name`：与 `discover()` 的判据保持一致，否则会顺手把
    策略文件里 import 进来的别的策略类当成它的实现（那正是「一个文件两个身份」的来源）。
    """
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {path} 创建模块 spec")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        # 半加载的模块留在 sys.modules 里会污染后续 import（`discover()` 会先 del 再导入，
        # 但别的调用方不一定）。失败就清干净。
        sys.modules.pop(module_name, None)
        raise

    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if (
            isinstance(attr, type)
            and attr is not base_cls
            and issubclass(attr, base_cls)
            and getattr(attr, "__module__", "") == module_name
        ):
            return attr
    return None


def qualified_identity(cls: type) -> str:
    """实现类的**语义身份**：`模块名.类名`。

    刻意**不用对象身份**（`is`）：`discover()` 会 `del sys.modules[...]` 再重新 import，
    于是同一个策略类在注册表里是**另一个对象**——实测 `A is B` 为假而
    `(A.__module__, A.__qualname__) == (B.__module__, B.__qualname__)` 为真。用 `is`
    比较会把每一个正常登记都误判成「注册键被别人占了」，那是最坏的一种假警报。
    """
    return f"{cls.__module__}.{cls.__qualname__}"


def classes_declaring(name: str, registry: Any) -> dict[str, list[str]]:
    """`{实现类身份: [注册键, ...]}`：注册表里所有**自己声明了 `name`** 的类。

    这是「注册键唯一」的判据（D17）。为什么要按**类身份**去重而不是按**键**计数：
    `discover()` 的既有缺陷是同一个类会拿到两个键（文件名 + 装饰器名），按键计数会把一个
    正常的策略判成「不唯一」——那是个假警报，比漏报更糟，因为它会拦住正确的登记。

    反过来，两个文件都声明了同一个 `name` 时，后导入的那个会**静默覆盖**先导入的注册表项，
    但两个类都还在（各自还挂着自己的文件名键）——按类身份去重就能看见「两个不同的实现类都
    叫这个名字」，这正是要拦下的那种不唯一。
    """
    declaring: dict[str, list[str]] = {}
    for key in registry.list_registered():
        try:
            cls = registry.get_class(key)
        except ValueError:
            continue
        if getattr(cls, "name", "") == name:
            declaring.setdefault(qualified_identity(cls), []).append(key)
    return declaring


def check_registry_key(
    name: str, registry: Any, *, strategy_cls: type
) -> RegistrationFailure | None:
    """校验注册键 `name` 唯一且指向 `strategy_cls`（D17）。通过返回 `None`。

    调用前注册表必须已经是**当下磁盘**的状态（调用方负责先 `discover()`），否则刚写出来
    的策略会报「解析不到」——那是一次假失败。
    """
    if name not in registry.list_registered():
        return RegistrationFailure(
            RegistrationProblem.REGISTRY_KEY_UNRESOLVED,
            f"注册表里解析不到注册键 {name!r}",
            f"确认文件落在 ~/.tradelogx/{STRATEGIES_DIRNAME}/{name}.py、类上有 "
            f"@register_strategy() 装饰器、且 description 四个字段齐全——"
            "这三样缺一样，discover() 都会静默跳过它。",
        )

    declaring = classes_declaring(name, registry)
    if len(declaring) > 1:
        detail = "；".join(
            f"{identity} 挂在这些键上：{keys}" for identity, keys in declaring.items()
        )
        return RegistrationFailure(
            RegistrationProblem.REGISTRY_KEY_NOT_UNIQUE,
            f"注册键 {name!r} 不唯一：有 {len(declaring)} 个不同的实现类都声明了这个名字。"
            f"{detail}",
            "给每个策略一个独有的 name（后导入的会静默覆盖先导入的，所以这不报错、只是"
            "选错了实现）。改名后重新登记。",
        )

    # 唯一，且必须就是本次登记的这个实现类（比语义身份，不比对象身份）。
    if declaring and qualified_identity(strategy_cls) not in declaring:
        occupied = next(iter(declaring))
        return RegistrationFailure(
            RegistrationProblem.REGISTRY_KEY_NOT_UNIQUE,
            f"注册键 {name!r} 指向的是另一个实现类 {occupied}，不是本次登记的 "
            f"{qualified_identity(strategy_cls)}",
            "换一个没人用过的策略名，或先清理撞车的那个策略文件。",
        )
    return None


# ---------------------------------------------------------------------- #
#  DB 侧：建候选行 + 取/建 Strategy 行（全在 sync 线程里跑）             #
# ---------------------------------------------------------------------- #


def _resolve_strategy_row(strategy_model: type, name: str, code_path: str):
    """按注册键取/建 `trading.Strategy` 行，**不传 `is_active`**（D2）。

    `Strategy.name` **没有唯一约束**（模型上是裸 `CharField`），所以不能直接用
    `get_or_create`：库里一旦有两行同名，它会抛 `MultipleObjectsReturned`，而那时候选行
    已经建好了——留下一个「候选已建、strategy 没接上」的半个状态。这里显式取第一行
    （按 `created_at` 稳定排序），并且**只读**：既有行一个字段都不改，尤其不碰
    `is_active`（人工总开关，`False` = 已退役，永不被自动化恢复）。

    取第一行而不是报错，是因为同名的重复行本身是既有数据债（幽灵策略行），登记侧不该
    因为库里脏数据就拒绝一条能进流水线的候选；清理是 `cleanup_ghost_strategies` 的职责。
    """
    existing = (
        strategy_model.objects.filter(name=name).order_by("created_at", "id").first()
    )
    if existing is not None:
        return existing
    return strategy_model.objects.create(name=name, code_path=code_path)


def _persist_candidate(
    *,
    strategy_name: str,
    hypothesis: str,
    origin: str,
    source_ref: dict,
    paper_account_id: str | None,
    code_path: str,
    proposed_by_id: str | None,
    failure: RegistrationFailure | None,
) -> dict:
    """在 sync 线程里落库，返回给工具用的结果字典。

    成功与失败**都建行**（D1 / US26），差别只在 `status`、`strategy` 与 `evidence`。

    整个过程在**一个事务**里：候选行与它指向的 `Strategy` 行要么一起落下，要么都不落。
    分两次写会留下「候选已建、`strategy` 没接上」的半个状态，而那个状态从外面看与
    「登记失败」长得一模一样——正是 D17 要杜绝的静默失败。
    """
    from django.db import transaction

    from apps.exchange.models import ExchangeAccount
    from apps.strategy_factory.models import StrategyCandidate
    from apps.strategy_factory.status import StrategyCandidateStatus
    from apps.trading.models import Strategy

    paper_account = None
    if paper_account_id:
        paper_account = ExchangeAccount.objects.filter(id=paper_account_id).first()

    with transaction.atomic():
        candidate = StrategyCandidate.objects.create(
            name=strategy_name,
            hypothesis=hypothesis,
            code_path=code_path,
            origin=origin,
            source_ref=source_ref,
            proposed_by_id=proposed_by_id,
            paper_account=paper_account,
            status=StrategyCandidateStatus.REGISTERED.value,
            evidence={},
        )

        if failure is None:
            candidate.strategy = _resolve_strategy_row(
                Strategy, strategy_name, code_path
            )
            candidate.save(update_fields=["strategy", "updated_at"])
        else:
            # 走真实的守门方法，而不是直接给 status= 赋值：`registered →
            # registration_failed` 是 D3 转移表里的一条边，让它在这里生效一次。
            candidate.transition_to(StrategyCandidateStatus.REGISTRATION_FAILED)
            candidate.evidence = {
                "registration": {
                    "code": failure.code.value,
                    "reason": failure.reason,
                    "hint": failure.hint,
                }
            }
            candidate.save(update_fields=["status", "evidence", "updated_at"])

    return {
        "candidate_id": str(candidate.id),
        "name": candidate.name,
        "status": candidate.status,
        "origin": candidate.origin,
        "code_path": candidate.code_path,
        "strategy_id": str(candidate.strategy_id) if candidate.strategy_id else "",
        "paper_account_id": str(candidate.paper_account_id)
        if candidate.paper_account_id
        else "",
    }


def _next_step_for(status: str) -> str:
    """候选登记之后「下一步会自动做什么」——工具返回里必须写清楚的那句话（D16）。

    按状态分岔而不是写一句通用的话：登记失败的候选下一步是**人来修代码**，不是流水线，
    两者说成同一句话会让人以为可以走开。

    判据取枚举的 `.value` 而不是裸字面量 `"registration_failed"`：字面量会在枚举改名那天
    静默失配，于是失败候选拿到一句「已登记，流水线自动接管」——正好把要区分的那件事说反。
    """
    from apps.strategy_factory.status import StrategyCandidateStatus

    if status == StrategyCandidateStatus.REGISTRATION_FAILED.value:
        return (
            "登记失败，流水线**不会**推进它。按 evidence.registration.hint 修好代码后"
            "重新登记（会回到 registered）。"
        )
    return (
        "已登记，接下来由流水线自动接管：先跑样本内回测（夏普 ≥1.5 且样本量达标），"
        "过了自动跑 walk-forward 样本外分折（衰减 <30%），再过自动升 paper 观察期。"
        "中间任何一层不过都会停下并留下原因码。升实盘是人工卡点，不会自动发生。"
    )


class RegisterStrategyCandidateTool(BaseTool):
    """把已通过 `test_strategy` 的策略登记为候选策略。"""

    name = "register_strategy_candidate"
    description = (
        "把已通过 test_strategy 的策略文件登记为候选策略（StrategyCandidate），"
        "之后由流水线自动接管（样本内回测 → 样本外分折 → paper 观察）。"
        "登记前会校验注册键唯一且能解析到实现类；失败时也会留下候选行并写明可自我修正的原因。"
        "必须在 test_strategy 通过之后调用。"
    )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "strategy_name": {
                    "type": "string",
                    "description": "策略注册键（必须与策略类声明的 name 完全一致）",
                },
                "strategy_file_path": {
                    "type": "string",
                    "description": "策略文件路径，相对 ~/.tradelogx/（如 strategies/xxx.py）",
                },
                "hypothesis": {
                    "type": "string",
                    "description": "一句话假设：这条策略认为市场会怎样、靠什么赚钱",
                },
                "origin": {
                    "type": "string",
                    "enum": ["agent", "grid_variant", "manual"],
                    "description": "候选来源：agent 生成 / 网格变体 / 人工投喂（默认 agent）",
                },
                "source_ref": {
                    "type": "object",
                    "description": "来源引用（可选）：agent 会话 id / GridSearchJob id + 组合参数 / 外部研究链接",
                },
                "paper_account_id": {
                    "type": "string",
                    "description": "（可选）paper 阶段绑哪个 testnet 账户 id；不填则流水线会停下来问",
                },
            },
            "required": ["strategy_name", "strategy_file_path"],
        }

    async def execute(self, **kwargs) -> ToolResult:
        from apps.core.db_utils import db_async

        strategy_name = (kwargs.get("strategy_name") or "").strip()
        raw_path = (kwargs.get("strategy_file_path") or "").strip()
        hypothesis = (kwargs.get("hypothesis") or "").strip()
        origin = (kwargs.get("origin") or "agent").strip()
        source_ref = kwargs.get("source_ref") or {}
        paper_account_id = (kwargs.get("paper_account_id") or "").strip() or None
        user_id = (kwargs.get("user_id") or "").strip() or None

        # ---- 参数级校验：不建行（一次「登记尝试」都还没发生） ---- #
        if not strategy_name:
            return ToolResult(success=False, error="缺少 strategy_name 参数")
        if not raw_path:
            return ToolResult(success=False, error="缺少 strategy_file_path 参数")

        from apps.strategy_factory.status import StrategyCandidateOrigin

        valid_origins = {o.value for o in StrategyCandidateOrigin}
        if origin not in valid_origins:
            return ToolResult(
                success=False,
                error=f"origin 只能是 {sorted(valid_origins)} 之一，收到 {origin!r}",
            )
        if not isinstance(source_ref, dict):
            return ToolResult(
                success=False, error="source_ref 必须是对象（JSON object）"
            )

        path = resolve_strategy_path(raw_path)
        code_path = f"{STRATEGIES_DIRNAME}/{path.name}"

        # ---- 文件级校验（纯读，不碰 DB） ---- #
        strategy_cls, failure = inspect_strategy_file(path, strategy_name)

        # ---- 注册键校验（D17）：文件级过了才查注册表 ---- #
        if failure is None:
            from apps.regime import pool_rebuild
            from apps.strategy_engine.registry import StrategyRegistry

            # 注册表可能装的是「进程启动那一刻」的磁盘内容（`apps.ready()` 只在目录当时
            # 存在时才 discover）。不补这一次，刚写出来的策略文件会报「解析不到」——
            # 那是假失败，会把生成侧引向错误的修正方向。
            pool_rebuild.ensure_strategies_discovered()
            failure = check_registry_key(
                strategy_name, StrategyRegistry, strategy_cls=strategy_cls
            )

        # ---- 解析提出人（解析不到不拦登记：proposed_by 可空，D1） ---- #
        proposed_by_id = await _resolve_proposed_by(user_id)

        # `paper_account` 是整条流水线**唯一**的人工输入（D10）。指定的账户 id 解析不到时
        # 必须当场报错，**不能静默丢成「没指定」**——那会让候选停在 `paper_pending` 干等，
        # 而调用方以为已经指定过了（这正是 D17 要杜绝的那种静默失败）。
        if paper_account_id and not await _account_exists(paper_account_id):
            return ToolResult(
                success=False,
                error=(
                    f"paper_account_id={paper_account_id!r} 找不到对应的交易所账户；"
                    "先用 list_exchange_accounts 查到正确的账户 id（须为 testnet 账户），"
                    "或省略该参数由流水线稍后询问。"
                ),
            )

        result = await db_async(_persist_candidate)(
            strategy_name=strategy_name,
            hypothesis=hypothesis,
            origin=origin,
            source_ref=source_ref,
            paper_account_id=paper_account_id,
            code_path=code_path,
            proposed_by_id=proposed_by_id,
            failure=failure,
        )

        if failure is not None:
            logger.warning(
                "[RegisterStrategyCandidateTool] registration failed for %s: %s (%s)",
                strategy_name,
                failure.reason,
                failure.code.value,
            )
            return ToolResult(
                success=False,
                data={
                    **result,
                    "reason_code": failure.code.value,
                    "reason": failure.reason,
                    "hint": failure.hint,
                    "next_step": _next_step_for(result["status"]),
                },
                error=f"{failure.display}：{failure.reason}；怎么修：{failure.hint}",
            )

        logger.info(
            "[RegisterStrategyCandidateTool] registered candidate %s (name=%s)",
            result["candidate_id"],
            strategy_name,
        )
        return ToolResult(
            success=True,
            data={
                **result,
                "next_step": _next_step_for(result["status"]),
            },
        )


async def _account_exists(account_id: str) -> bool:
    """指定的 `paper_account_id` 在库里存在吗（用来把「id 写错」变成一条可修正的参数错误）。

    `ExchangeAccount.id` 是 UUIDField，所以传进来的可能是**不是 UUID 的字符串**（LLM 偶尔
    会编一个短 id）。Django 在这种输入上抛 `ValidationError` 而不是返回空集——必须接住它，
    否则「id 写错了」会变成一次未捕获异常，工具返回里就只剩一句栈信息。
    """
    from apps.core.db_utils import db_async
    from apps.exchange.models import ExchangeAccount

    def _lookup() -> bool:
        try:
            return ExchangeAccount.objects.filter(id=account_id).exists()
        except (ValueError, ValidationError):
            return False

    return await db_async(_lookup)()


async def _resolve_proposed_by(user_id: str | None) -> str | None:
    """渠道 user_id → Django User UUID；解析不到返回 `None`。

    复用 `apps.agent.user_resolver`（渠道 ID / UUID / 飞书 open_id 都认），**不**在解析
    失败时拦住登记：`proposed_by` 按 D1 是可空的（`SET_NULL`），而「谁提的」缺失不该让
    一条能进流水线的候选卡在门口。
    """
    if not user_id:
        return None
    from apps.agent.user_resolver import resolve_user_id

    resolved = await resolve_user_id(user_id)
    if resolved is None:
        logger.info(
            "[RegisterStrategyCandidateTool] could not resolve user %r; "
            "registering without proposed_by",
            user_id,
        )
    return str(resolved) if resolved else None
