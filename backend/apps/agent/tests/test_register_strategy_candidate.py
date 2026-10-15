"""生成侧登记工具（spec #19 D16 / D17）。

这个文件测的是**登记那一段**的三件事：

1. **可达**（D16 验收 1）：工具在 `quant` 的 `tools:` 列表里、能被路由；`list_strategies`
   同样可达（那是本票要修的既有缺口——它已注册但不在任何 agent 的列表里，
   所以 LLM 从来没见过它的 schema）。
2. **成功落 `registered` 且 `strategy` 指向可解析的实现类**（D16 验收 2）。
3. **失败落 `registration_failed` 且原因可自我修正**（D17 验收 3）：注册键不唯一 / 解析
   不到 / 描述模板缺 4 字段 / 语法错 / 名字不一致——每一类都要给出**具体**的原因与修法，
   **不让静默失败穿过去**（`discover()` 把异常吞成 `logger.warning`，正是这里要补的洞）。

另有一条 D2 的钉子：登记**绝不**写 `Strategy.is_active`。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.agent.tools.base import BaseTool, ToolRegistry
from apps.agent.tools.register_strategy_candidate import (
    RegistrationProblem,
    RegisterStrategyCandidateTool,
    check_registry_key,
    inspect_strategy_file,
    resolve_strategy_path,
)

# --------------------------------------------------------------------------- #
#  夹具                                                                        #
# --------------------------------------------------------------------------- #


#: 一个**完全合规**的策略文件。`name` 由格式化参数填。
STRATEGY_SOURCE = '''\
from apps.strategy_engine.base import BaseStrategy, StrategyContext
from apps.strategy_engine.registry import register_strategy


@register_strategy(name="{name}")
class {cls}(BaseStrategy):
    name = "{name}"
    description = """策略类型：趋势跟踪
核心指标：EMA
适用场景：趋势行情，不适合震荡盘整
入场逻辑：快线上穿慢线时买入"""

    def on_bar(self, kline, history):
        return None
'''


def write_strategy(
    directory: Path,
    *,
    filename: str,
    name: str,
    cls: str,
    decorated: bool = True,
    description: str | None = None,
    source: str | None = None,
) -> Path:
    """把一个策略文件写进 `directory`，返回它的路径。

    允许刻意写坏：`decorated=False` 去掉装饰器（注册键就只剩文件名）、
    `description` 覆盖描述（用来打掉 4 字段模板）、`source` 整段替换（用来写语法错）。
    """
    if source is None:
        source = STRATEGY_SOURCE.format(name=name, cls=cls)
        if not decorated:
            source = source.replace('@register_strategy(name="{0}")\n'.format(name), "")
        if description is not None:
            start = source.index('    description = """')
            end = source.index('"""', start + len('    description = """')) + 3
            source = (
                source[:start]
                + '    description = """'
                + description
                + '"""'
                + source[end:]
            )
    path = directory / filename
    path.write_text(source, encoding="utf-8")
    return path


@pytest.fixture
def strategies_dir(tmp_path):
    """把 `StrategyRegistry` 指向一个临时目录，用完原样还回去。

    注册表是**进程级单例**，`_strategies` 是类属性。不隔离的话，宿主机
    `~/.tradelogx/strategies` 里那 32 个键会混进来，用例的结论就依赖运行环境了。
    """
    from apps.strategy_engine.registry import StrategyRegistry

    saved_strategies = dict(StrategyRegistry._strategies)
    saved_path = StrategyRegistry._strategy_path

    directory = tmp_path / "strategies"
    directory.mkdir()
    StrategyRegistry._strategies.clear()
    StrategyRegistry.set_strategy_path(str(directory))

    yield directory

    StrategyRegistry._strategies.clear()
    StrategyRegistry._strategies.update(saved_strategies)
    StrategyRegistry.set_strategy_path(saved_path)


@pytest.fixture
def tool():
    return RegisterStrategyCandidateTool()


def _populate_tool_registry() -> None:
    """把 `tools/` 下的工具类重新灌进 `ToolRegistry`。

    工具是靠 `apps/agent/tools/__init__.py` 的**导入副作用**注册的，那个副作用一个进程
    只跑一次。而别处的 autouse 夹具（如 `test_analyst_prompt_loading.py`）会
    `ToolRegistry.reset()` 清空它，之后没有任何东西会再灌回来——于是「工具已注册」这类
    断言会**依赖用例的执行顺序**。这里自己灌一遍，把顺序依赖去掉。
    """
    import importlib

    tools_dir = Path(__file__).resolve().parents[1] / "tools"
    for f in sorted(tools_dir.glob("*.py")):
        if f.name.startswith(("_", "test")) or f.name in ("base.py", "__init__.py"):
            continue
        mod = importlib.import_module(f"apps.agent.tools.{f.stem}")
        for attr in dir(mod):
            cls = getattr(mod, attr)
            if (
                isinstance(cls, type)
                and issubclass(cls, BaseTool)
                and cls is not BaseTool
            ):
                ToolRegistry.register(cls())


@pytest.fixture(autouse=True)
def _tools_available():
    """每个用例前保证工具注册表是满的（见 `_populate_tool_registry` 的顺序依赖说明）。"""
    _populate_tool_registry()
    yield


async def register(tool, path: Path, **kwargs):
    """调用工具，`strategy_file_path` 默认用绝对路径（绕开 ~/.tradelogx 解析）。"""
    kwargs.setdefault("strategy_file_path", str(path))
    return await tool.execute(**kwargs)


# --------------------------------------------------------------------------- #
#  可达性（D16 验收 1）                                                        #
# --------------------------------------------------------------------------- #


def test_tool_is_registered_in_the_registry():
    """工具靠 `tools/__init__.py` 自动发现注册——不需要手写注册代码。"""
    assert isinstance(ToolRegistry.get("register_strategy_candidate"), BaseTool)


def _quant_tool_names() -> list[str]:
    """读 `backend/prompts/v1/quant.txt` 的 frontmatter `tools:` 列表。

    刻意读**仓库里**那一份（而不是 `~/.tradelogx/agents/v1/quant.txt`）：CI 上只有仓库
    那一份存在，而两份必须一致（票面要求「两处都改」）。
    """
    from apps.agent.prompt_loader import _parse_frontmatter

    # 本文件在 backend/apps/agent/tests/ 下 → parents[3] == backend/
    prompt = Path(__file__).resolve().parents[3] / "prompts" / "v1" / "quant.txt"
    meta, _ = _parse_frontmatter(prompt.read_text(encoding="utf-8"))
    return meta["tools"]


def test_registration_tool_and_list_strategies_are_declared_for_quant():
    """两个工具名都必须出现在 `quant.txt` 的 `tools:` 列表里。

    `list_strategies` 这一半是**既有缺口**的回归钉子：它早就注册了、`quant-backtest`
    技能也强制要求它，但它不在任何 agent 的 `tools:` 列表里，所以 LLM 从来没见过它的
    schema（`_get_tools_schema` 只发声明过的名字）。
    """
    declared = _quant_tool_names()
    assert "register_strategy_candidate" in declared
    assert "list_strategies" in declared


def test_quant_agent_schema_actually_carries_both_tools():
    """光在 frontmatter 里写了不算数——要真的进了发给 LLM 的 schema。

    走**真实的接线路径**（frontmatter meta → `_build_dynamic_agent_class` →
    `_agent_tools` → `_get_tools_schema()`），但 meta 取自**仓库里**那份 prompt：CI 上
    `~/.tradelogx/agents/` 不存在，走 `AgentRegistry.discover_from_prompts()` 会一个
    agent 都发现不了，用例就变成依赖运行环境了。
    """
    from apps.agent.prompt_loader import _parse_frontmatter
    from apps.agent.sub_agents import _build_dynamic_agent_class

    prompt = Path(__file__).resolve().parents[3] / "prompts" / "v1" / "quant.txt"
    meta, _ = _parse_frontmatter(prompt.read_text(encoding="utf-8"))
    agent = _build_dynamic_agent_class(meta)()
    try:
        names = [t["function"]["name"] for t in agent._get_tools_schema()]
    finally:
        from apps.agent.llm_client import LLMClient

        LLMClient._instance = None

    assert "register_strategy_candidate" in names, names
    assert "list_strategies" in names, names


def test_every_declared_quant_tool_exists_in_the_registry():
    """声明了但注册表里没有的名字 = 一个永远查不到的 schema（`_get_tools_schema` 只打
    warning）。这条把「声明」与「实现」钉在一起。"""
    registered = {t.name for t in ToolRegistry.all()}
    assert [name for name in _quant_tool_names() if name not in registered] == []


# --------------------------------------------------------------------------- #
#  路径解析                                                                    #
# --------------------------------------------------------------------------- #


def test_relative_path_resolves_against_the_workspace_root():
    from apps.agent.tools.file_io import WORKSPACE_ROOT

    assert (
        resolve_strategy_path("strategies/x.py") == WORKSPACE_ROOT / "strategies/x.py"
    )


def test_absolute_path_is_used_as_is(tmp_path):
    path = tmp_path / "x.py"
    assert resolve_strategy_path(str(path)) == path


# --------------------------------------------------------------------------- #
#  成功登记（D16 验收 2）                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_successful_registration_lands_registered(tool, strategies_dir):
    from apps.strategy_factory.models import StrategyCandidate
    from apps.strategy_engine.registry import StrategyRegistry

    path = write_strategy(
        strategies_dir, filename="alpha_probe.py", name="alpha_probe", cls="AlphaProbe"
    )
    result = await register(
        tool,
        path,
        strategy_name="alpha_probe",
        hypothesis="快线上穿慢线时做多",
        origin="agent",
    )

    assert result.success, result.error
    assert result.data["status"] == "registered"

    candidate = await StrategyCandidate.objects.aget(id=result.data["candidate_id"])
    assert candidate.name == "alpha_probe"
    assert candidate.hypothesis == "快线上穿慢线时做多"
    assert candidate.origin == "agent"
    assert candidate.code_path == "strategies/alpha_probe.py"
    assert candidate.evidence == {}

    # 验收 2 的后半句：`strategy` 指向**可解析的实现类**。
    assert candidate.strategy_id is not None
    resolved = StrategyRegistry.get_class(candidate.strategy.name)
    assert resolved.__name__ == "AlphaProbe"
    assert resolved.name == "alpha_probe"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_normal_registration_is_not_rejected_as_not_unique(tool, strategies_dir):
    """**假警报回归钉子。**

    `discover()` 会给同一个类两个注册键（文件名 + 装饰器名，实测 21 个模块 → 32 个键）。
    若把「唯一」实现成**按键计数**，每一个正常登记都会被判成「不唯一」；若实现成**对象身份**
    比较，`discover()` 的 `del sys.modules` + 重新 import 会让同一个类变成另一个对象，
    于是同样误判。这条用例把「正常登记必须通过」钉死。

    刻意让**文件名与声明的名字不同**（`beta_file.py` → `beta_probe`）：两者相同时两个键会
    合并成一个，双注册这个形状根本不会出现（仓库里 `ema_crossover_simple.py` →
    `EMACrossoverSimple` 才是它的真实样子）。
    """
    from apps.agent.tools.register_strategy_candidate import (
        inspect_strategy_file,
        qualified_identity,
    )
    from apps.strategy_engine.registry import StrategyRegistry

    path = write_strategy(
        strategies_dir, filename="beta_file.py", name="beta_probe", cls="BetaProbe"
    )
    StrategyRegistry.discover()

    # 前提确认：既有缺陷的形状——同一个类真的拿到了两个键（文件名 + 装饰器名）。
    registered = StrategyRegistry.list_registered()
    assert "beta_probe" in registered, registered
    assert "beta_file" in registered, registered

    by_declared = StrategyRegistry.get_class("beta_probe")
    by_filename = StrategyRegistry.get_class("beta_file")
    # 两个键必须指向**语义上同一个**实现类（这正是「唯一」该用的判据）。
    assert qualified_identity(by_declared) == qualified_identity(by_filename)

    # 而**对象身份**是靠不住的：`inspect_strategy_file` 会自己 import 一次文件，
    # `discover()` 随后又 `del sys.modules` 再 import 一次 → 同一个类变成两个对象。
    # 用 `is` 比较「注册键是不是指向本次登记的这个类」会误判成撞车。
    inspected, failure = inspect_strategy_file(path, "beta_probe")
    assert failure is None
    assert inspected is not by_declared, (
        "这里必须是两个不同的对象，否则下面的 qualified_identity 断言就失去意义"
    )
    assert qualified_identity(inspected) == qualified_identity(by_declared)

    result = await register(tool, path, strategy_name="beta_probe")
    assert result.success, result.error
    assert result.data["status"] == "registered"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_decorator_name_different_from_filename_still_registers(
    tool, strategies_dir
):
    """装饰器名与文件名不同（仓库里 `ema_crossover_simple.py` → `EMACrossoverSimple`
    就是这个形状）时，用**声明的名字**登记必须成功。"""
    path = write_strategy(
        strategies_dir,
        filename="gamma_file.py",
        name="GammaDecorated",
        cls="GammaDecorated",
    )
    result = await register(tool, path, strategy_name="GammaDecorated")
    assert result.success, result.error


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_paper_account_and_source_ref_are_persisted(tool, strategies_dir):
    from apps.exchange.models import ExchangeAccount
    from apps.strategy_factory.models import StrategyCandidate

    account = await ExchangeAccount.objects.acreate(
        exchange="binance",
        label="paper-testnet",
        api_key_enc=b"k",
        api_secret_enc=b"s",
        testnet=True,
    )
    path = write_strategy(
        strategies_dir, filename="delta_probe.py", name="delta_probe", cls="DeltaProbe"
    )
    result = await register(
        tool,
        path,
        strategy_name="delta_probe",
        origin="manual",
        source_ref={"research_url": "https://example.com/x"},
        paper_account_id=str(account.id),
    )
    assert result.success, result.error

    candidate = await StrategyCandidate.objects.aget(id=result.data["candidate_id"])
    assert candidate.origin == "manual"
    assert candidate.source_ref == {"research_url": "https://example.com/x"}
    assert candidate.paper_account_id == account.id


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_next_step_tells_what_the_pipeline_will_do(tool, strategies_dir):
    """D16 明确要求返回里给出「下一步会自动做什么」。"""
    path = write_strategy(
        strategies_dir,
        filename="epsilon_probe.py",
        name="epsilon_probe",
        cls="EpsilonProbe",
    )
    result = await register(tool, path, strategy_name="epsilon_probe")
    assert result.success, result.error
    assert "样本内" in result.data["next_step"]
    assert "paper" in result.data["next_step"]


# --------------------------------------------------------------------------- #
#  D2：绝不碰 Strategy.is_active                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_registration_never_reactivates_a_retired_strategy(tool, strategies_dir):
    """已退役（`is_active=False`）的策略行**一个字段都不许改**（D2）。"""
    from apps.trading.models import Strategy

    await Strategy.objects.acreate(
        name="zeta_probe", code_path="strategies/zeta_probe.py", is_active=False
    )
    path = write_strategy(
        strategies_dir, filename="zeta_probe.py", name="zeta_probe", cls="ZetaProbe"
    )
    result = await register(tool, path, strategy_name="zeta_probe")
    assert result.success, result.error

    rows = [s async for s in Strategy.objects.filter(name="zeta_probe")]
    assert len(rows) == 1, "登记不该重复建策略行"
    assert rows[0].is_active is False, "流水线绝不自动恢复人工总开关（D2）"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_newly_created_strategy_row_keeps_the_model_default(tool, strategies_dir):
    """新建的策略行走模型默认值（`False`）——工具**不传** `is_active`。"""
    from apps.trading.models import Strategy

    path = write_strategy(
        strategies_dir, filename="eta_probe.py", name="eta_probe", cls="EtaProbe"
    )
    result = await register(tool, path, strategy_name="eta_probe")
    assert result.success, result.error

    row = await Strategy.objects.aget(name="eta_probe")
    assert row.is_active == Strategy._meta.get_field("is_active").default


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_duplicate_strategy_rows_do_not_break_registration(tool, strategies_dir):
    """**既有数据债回归钉子。**

    `Strategy.name` 没有唯一约束（模型上是裸 `CharField`），库里可能有两行同名——幽灵策略
    行就是这么攒出来的。裸 `get_or_create` 在这时会抛 `MultipleObjectsReturned`，而那时
    候选行已经建好了，留下「候选已建、strategy 没接上」的半个状态。登记必须能扛住这种脏
    数据（清理是 `cleanup_ghost_strategies` 的职责，不是登记侧的）。
    """
    from apps.strategy_factory.models import StrategyCandidate
    from apps.trading.models import Strategy

    first = await Strategy.objects.acreate(
        name="theta_dup", code_path="strategies/a.py"
    )
    await Strategy.objects.acreate(name="theta_dup", code_path="strategies/b.py")

    path = write_strategy(
        strategies_dir, filename="theta_dup.py", name="theta_dup", cls="ThetaDup"
    )
    result = await register(tool, path, strategy_name="theta_dup")

    assert result.success, result.error
    candidate = await StrategyCandidate.objects.aget(id=result.data["candidate_id"])
    assert candidate.strategy_id == first.id, "取稳定排序的第一行"
    # 两行都还在（登记侧不清理既有数据），且都没被改写。
    assert await Strategy.objects.filter(name="theta_dup").acount() == 2


# --------------------------------------------------------------------------- #
#  登记失败（D17 验收 3）                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("problem", "strategy_name", "build"),
    [
        (
            RegistrationProblem.DESCRIPTION_TEMPLATE_MISSING,
            "bad_desc",
            lambda d: write_strategy(
                d,
                filename="bad_desc.py",
                name="bad_desc",
                cls="BadDesc",
                description="策略类型：趋势跟踪\n核心指标：EMA",
            ),
        ),
        (
            RegistrationProblem.STRATEGY_NAME_MISMATCH,
            # 用**文件名**去登记，而文件里声明的是另一个名字。
            "bad_name",
            lambda d: write_strategy(
                d, filename="bad_name.py", name="declared_name", cls="BadName"
            ),
        ),
        (
            RegistrationProblem.SYNTAX_ERROR,
            "bad_syntax",
            lambda d: write_strategy(
                d,
                filename="bad_syntax.py",
                name="bad_syntax",
                cls="BadSyntax",
                source='x = "unterminated\n',
            ),
        ),
        (
            RegistrationProblem.REGISTRY_KEY_UNRESOLVED,
            # 名字自洽、语法也对，但**没有装饰器**：`discover()` 只按文件名注册，
            # 于是声明的名字在注册表里根本不存在。
            "declared_only",
            lambda d: write_strategy(
                d,
                filename="unregistered_file.py",
                name="declared_only",
                cls="DeclaredOnly",
                decorated=False,
            ),
        ),
    ],
)
async def test_each_failure_mode_lands_registration_failed(
    tool, strategies_dir, problem, strategy_name, build
):
    from apps.strategy_factory.models import StrategyCandidate

    path = build(strategies_dir)
    result = await register(tool, path, strategy_name=strategy_name)

    assert result.success is False
    assert result.data["reason_code"] == problem.value
    assert result.data["status"] == "registration_failed"
    # 原因必须**可自我修正**：有具体 reason，也有怎么修的 hint。
    assert result.data["reason"]
    assert result.data["hint"]
    # 给 LLM 看的那句 error 里要带人话的原因名（不是裸 reason code）。
    assert result.data["reason"] in result.error

    candidate = await StrategyCandidate.objects.aget(id=result.data["candidate_id"])
    assert candidate.status == "registration_failed"
    assert candidate.strategy_id is None
    assert candidate.evidence["registration"]["code"] == problem.value
    assert candidate.evidence["registration"]["hint"] == result.data["hint"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_syntax_error_is_reported_specifically_not_swallowed(
    tool, strategies_dir
):
    """D17 的核心：`discover()` 把语法错吞成 `logger.warning`，于是注册表只能告诉你
    「这个名字没有」。工具必须**读开文件**、把语法错如实报出来（带行号），否则生成侧
    拿到的是一句无从下手的「解析不到」。"""
    path = write_strategy(
        strategies_dir,
        filename="swallow_probe.py",
        name="swallow_probe",
        cls="SwallowProbe",
        source="def broken(:\n    pass\n",
    )
    result = await register(tool, path, strategy_name="swallow_probe")

    assert result.success is False
    assert result.data["reason_code"] == RegistrationProblem.SYNTAX_ERROR.value
    assert "行" in result.data["reason"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_duplicate_registry_key_is_rejected(tool, strategies_dir):
    """两个文件声明同一个 `name` → 后导入的会**静默覆盖**先导入的注册表项。这条要拦下，
    且要指出撞车的是哪两个实现类。"""
    from apps.strategy_factory.models import StrategyCandidate

    write_strategy(
        strategies_dir, filename="dup_one.py", name="dup_probe", cls="DupOne"
    )
    path = write_strategy(
        strategies_dir, filename="dup_two.py", name="dup_probe", cls="DupTwo"
    )
    result = await register(tool, path, strategy_name="dup_probe")

    assert result.success is False
    assert (
        result.data["reason_code"] == RegistrationProblem.REGISTRY_KEY_NOT_UNIQUE.value
    )
    assert "DupOne" in result.data["reason"]
    assert "DupTwo" in result.data["reason"]

    candidate = await StrategyCandidate.objects.aget(id=result.data["candidate_id"])
    assert candidate.status == "registration_failed"


def test_registry_key_check_fails_closed_when_the_key_holds_an_unrelated_class(
    strategies_dir,
):
    """**静默成功钉子（独立复核发现）：空 declaring 必须判失败，不能跳过。**

    键已注册、但键后面的实现类没声明这个候选名字时（登记的是 A、流水线将来解析到的是
    B），若把空集当成「可以跳过检查」，就是一次 `status=registered`、`evidence` 空的
    静默成功。直接打 `check_registry_key`：这是守卫本体的性质，不必绕道完整的工具链路。

    形状按实测搭：`discover()` 把**字母序最后一个**策略类占住文件名键，所以文件名键
    后面挂的是 ZetaBug（name="other_one"），而本次登记的实现类声明的是 "target"。
    """
    from apps.strategy_engine.registry import StrategyRegistry

    class _KeyHolder:
        name = "other_one"

    _KeyHolder.__module__ = "twin_file"
    _KeyHolder.__qualname__ = "ZetaBug"

    class _Claimed:
        name = "target"

    _Claimed.__module__ = "alpha_file"
    _Claimed.__qualname__ = "AlphaBug"

    # 键 "target" 存在，但它后面的类没有声明 "target"（登记 A、解析到 B 的形状）。
    StrategyRegistry._strategies["target"] = _KeyHolder
    try:
        failure = check_registry_key("target", StrategyRegistry, strategy_cls=_Claimed)
    finally:
        StrategyRegistry._strategies.pop("target", None)

    assert failure is not None
    assert failure.code is RegistrationProblem.REGISTRY_KEY_NOT_UNIQUE
    assert "ZetaBug" in failure.reason


def test_inherited_name_from_a_subclass_does_not_false_alarm(strategies_dir):
    """**假警报钉子（独立复核发现）。

    网格变体走「子类化父策略」这条路时，子类不重写 `name` → 继承父类的名字。此时注册表
    里有**两个类**声明同一个名字、但注册键仍然唯一指向其中一个。判据按「键后面那个类
    就是本次登记的实现类」判定，而不是按「声明这个名字的类的个数」判定——后者会把这条
    合法登记误判成不唯一。"""
    from apps.strategy_engine.base import BaseStrategy
    from apps.strategy_engine.registry import StrategyRegistry

    class ParentStrategy(BaseStrategy):
        name = "parent"
        description = (
            "策略类型：趋势跟踪\n核心指标：EMA\n适用场景：趋势行情\n入场逻辑：上穿买入"
        )

    class FastVariant(ParentStrategy):  # 不重写 name → 继承 "parent"
        pass

    ParentStrategy.__module__ = "parent_file"
    FastVariant.__module__ = "parent_fast_file"
    StrategyRegistry._strategies["parent"] = ParentStrategy
    StrategyRegistry._strategies["parent_fast"] = FastVariant
    try:
        failure = check_registry_key(
            "parent", StrategyRegistry, strategy_cls=ParentStrategy
        )
    finally:
        StrategyRegistry._strategies.pop("parent", None)
        StrategyRegistry._strategies.pop("parent_fast", None)

    assert failure is None, failure and failure.reason


def test_a_rival_class_from_an_unrelated_file_is_rejected(strategies_dir):
    """**抢键钉子（独立复核 Q1）。

    两个**互不相关**的文件声明同一个 `name`（没有继承关系）→ 后导入的会静默覆盖先导入
    的注册表项。此刻键可能恰好还指向本次登记的类，但下一次 `discover()` 的导入顺序一变，
    键就会指到别的实现——这种「现在对、将来错」必须拦下。与上面的继承场景相对：那里
    两个类是父子关系（键由文件名决定，互不抢），这里是平行竞争。"""
    from apps.strategy_engine.base import BaseStrategy
    from apps.strategy_engine.registry import StrategyRegistry

    class FirstImpl(BaseStrategy):
        name = "rival_name"
        description = (
            "策略类型：趋势跟踪\n核心指标：EMA\n适用场景：趋势行情\n入场逻辑：上穿买入"
        )

    class SecondImpl(BaseStrategy):
        name = "rival_name"
        description = (
            "策略类型：动量\n核心指标：RSI\n适用场景：单边行情\n入场逻辑：突破买入"
        )

    FirstImpl.__module__ = "rival_one_file"
    SecondImpl.__module__ = "rival_two_file"
    # 键必须等于策略名（判据是 get_class(name)）；两个类各自挂自己的文件名键。
    StrategyRegistry._strategies["rival_name"] = FirstImpl
    StrategyRegistry._strategies["rival_two"] = SecondImpl
    try:
        failure = check_registry_key(
            "rival_name", StrategyRegistry, strategy_cls=FirstImpl
        )
    finally:
        StrategyRegistry._strategies.pop("rival_name", None)
        StrategyRegistry._strategies.pop("rival_two", None)

    assert failure is not None
    assert failure.code is RegistrationProblem.REGISTRY_KEY_NOT_UNIQUE
    assert "SecondImpl" in failure.reason


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_file_outside_the_strategies_dir_is_rejected(tool, strategies_dir):
    """**身份回归钉子（独立复核发现）。**

    注册表只扫 `strategies/`，所以被校验的文件与将来会被解析到的文件必须是**同一份**。
    文件落在别处时（哪怕 stem 撞巧同名）`code_path` 还会把落点记错——必须先拒收，且
    不建行（登记尝试都没发生）。
    """
    from apps.strategy_factory.models import StrategyCandidate

    stray = strategies_dir.parent / "elsewhere.py"
    stray.write_text(
        STRATEGY_SOURCE.format(name="stray_name", cls="StrayName"), encoding="utf-8"
    )
    result = await register(tool, stray, strategy_name="stray_name")

    assert result.success is False
    assert (
        result.data["reason_code"] == RegistrationProblem.FILE_NOT_IN_STRATEGY_DIR.value
    )
    # 这次调用**是**一次登记尝试（参数齐全，只是文件放错了地方），所以照 D1/US26
    # 留一条 registration_failed 行并带原因——而不是静默什么都没有。
    candidate = await StrategyCandidate.objects.aget(id=result.data["candidate_id"])
    assert candidate.status == "registration_failed"
    assert candidate.strategy_id is None
    assert candidate.evidence["registration"]["code"] == "file_not_in_strategy_dir"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_placeholder_name_is_rejected(tool, strategies_dir):
    """类里没写 `name` 时会继承 `BaseStrategy.name = "unnamed"`（实测
    `obi_vwap_strategy.py` 就是这个形状）。占位名不是身份，必须拦下。"""
    path = write_strategy(
        strategies_dir,
        filename="placeholder_probe.py",
        name="placeholder_probe",
        cls="PlaceholderProbe",
        source=(
            "from apps.strategy_engine.base import BaseStrategy\n"
            "from apps.strategy_engine.registry import register_strategy\n\n\n"
            '@register_strategy(name="placeholder_probe")\n'
            "class PlaceholderProbe(BaseStrategy):\n"
            '    description = """策略类型：趋势跟踪\n'
            '核心指标：EMA\n适用场景：趋势行情\n入场逻辑：上穿买入"""\n\n'
            "    def on_bar(self, kline, history):\n"
            "        return None\n"
        ),
    )
    result = await register(tool, path, strategy_name="placeholder_probe")

    assert result.success is False
    assert result.data["reason_code"] == RegistrationProblem.STRATEGY_NAME_MISSING.value


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_missing_file_is_reported(tool, strategies_dir):
    result = await register(tool, strategies_dir / "nope.py", strategy_name="nope")
    assert result.success is False
    assert result.data["reason_code"] == RegistrationProblem.FILE_NOT_FOUND.value


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_failure_keeps_the_candidate_visible_with_its_reason(
    tool, strategies_dir
):
    """US26 / D1：失败的候选**留在系统里**并带原因（`strategy` 为空）。"""
    from apps.strategy_factory.models import StrategyCandidate

    path = write_strategy(
        strategies_dir,
        filename="kept_probe.py",
        name="kept_probe",
        cls="KeptProbe",
        description="没有四个字段",
    )
    result = await register(tool, path, strategy_name="kept_probe")
    assert result.success is False

    # 能用名字查到这条失败候选。
    candidate = await StrategyCandidate.objects.aget(name="kept_probe")
    assert str(candidate.id) == result.data["candidate_id"]
    assert candidate.status == "registration_failed"
    assert candidate.evidence["registration"]["reason"]


# --------------------------------------------------------------------------- #
#  参数级错误：不建行                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "kwargs",
    [
        {"strategy_name": "", "strategy_file_path": "strategies/x.py"},
        {"strategy_name": "x", "strategy_file_path": ""},
    ],
)
async def test_missing_required_params_create_no_rows(tool, kwargs):
    from apps.strategy_factory.models import StrategyCandidate

    result = await tool.execute(**kwargs)
    assert result.success is False
    assert await StrategyCandidate.objects.acount() == 0


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_unknown_origin_is_rejected_without_a_row(tool, strategies_dir):
    """`origin` 不认识 = 调用方还没把话说清楚，不该往库里塞一条候选。"""
    from apps.strategy_factory.models import StrategyCandidate

    path = write_strategy(
        strategies_dir, filename="theta_probe.py", name="theta_probe", cls="ThetaProbe"
    )
    result = await register(
        tool, path, strategy_name="theta_probe", origin="not_a_real_origin"
    )
    assert result.success is False
    assert "origin" in result.error
    assert await StrategyCandidate.objects.acount() == 0


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_unresolvable_paper_account_is_reported_not_silently_dropped(
    tool, strategies_dir
):
    """**静默失败回归钉子。**

    `paper_account` 是整条流水线唯一的人工输入（D10）。指定的 id 解析不到时若悄悄落成
    `None`，候选会停在 `paper_pending` 干等，而调用方以为已经指定过了——这正是 D17 要
    杜绝的静默失败形状。必须当场报错，且不建行（参数错误，一次登记尝试都还没发生）。
    """
    from apps.strategy_factory.models import StrategyCandidate

    path = write_strategy(
        strategies_dir, filename="iota_probe.py", name="iota_probe", cls="IotaProbe"
    )
    result = await register(
        tool, path, strategy_name="iota_probe", paper_account_id="does-not-exist"
    )

    assert result.success is False
    assert "找不到对应的交易所账户" in result.error
    assert await StrategyCandidate.objects.acount() == 0


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resolvable_paper_account_is_bound(tool, strategies_dir):
    """反向：账户 id 正确时必须真的绑上（否则上面那条只是在测「永远报错」）。"""
    from apps.exchange.models import ExchangeAccount
    from apps.strategy_factory.models import StrategyCandidate

    account = await ExchangeAccount.objects.acreate(
        exchange="binance",
        label="paper-ok",
        api_key_enc=b"k",
        api_secret_enc=b"s",
        testnet=True,
    )
    path = write_strategy(
        strategies_dir, filename="kappa_probe.py", name="kappa_probe", cls="KappaProbe"
    )
    result = await register(
        tool, path, strategy_name="kappa_probe", paper_account_id=str(account.id)
    )

    assert result.success, result.error
    candidate = await StrategyCandidate.objects.aget(id=result.data["candidate_id"])
    assert candidate.paper_account_id == account.id


# --------------------------------------------------------------------------- #
#  create-strategy 技能的收尾改造（D16 交付 4）                                #
# --------------------------------------------------------------------------- #


def test_create_strategy_skill_registers_instead_of_handing_off_a_name():
    """收尾那一步必须从「告知用户 strategy_name 供回测」改成「登记为候选」。

    读仓库里那份 `skills/create-strategy/SKILL.md`（CI 上 `~/.tradelogx/` 不存在）。
    """
    skill = (
        Path(__file__).resolve().parents[4] / "skills" / "create-strategy" / "SKILL.md"
    )
    text = skill.read_text(encoding="utf-8")

    assert "register_strategy_candidate" in text, "收尾步骤没有接上登记工具"
    assert "阶段 5：登记为候选" in text
    # 旧口径必须消失：它正是登记要取代的手工串联。
    assert "通过后告知用户：strategy_name（供回测使用）" not in text
    # 需求收集与代码生成阶段**不动**（票面交付 4 明确要求）。
    assert "阶段 1：需求收集（对话轮次）" in text
    assert "阶段 3：生成策略代码" in text
    assert "阶段 4：测试策略" in text


# --------------------------------------------------------------------------- #
#  纯函数那一半（不碰 DB）                                                     #
# --------------------------------------------------------------------------- #


def test_inspect_strategy_file_is_db_free(strategies_dir):
    """文件级校验必须能在**没有 `django_db`** 的用例里跑（不需要库就能给出原因）。"""
    path = write_strategy(
        strategies_dir, filename="pure_probe.py", name="pure_probe", cls="PureProbe"
    )
    cls, failure = inspect_strategy_file(path, "pure_probe")
    assert failure is None
    assert cls.__name__ == "PureProbe"
