#!/usr/bin/env python
"""快速验证通道（fast lane）—— 让「改一行代码」的验证从分钟级降到秒级。

设计目标
--------
1. **不降级覆盖**：资金 / 订单 / 风控 / 鉴权路径上的改动，一律强制跑该 app 的
   完整测试目录 + 所有反向依赖它的其他 app 测试，绝不允许降级为「只跑同名测试」。
2. **变更感知**：用 `git diff` 推导受影响范围，映射规则基于 **AST 反向依赖**，
   而不是文件名猜测（文件名映射在本仓库漏检率约 70%，见设计文档）。
3. **分层执行**：L0 静态 → L1 受影响测试 → L2 不变量 → L3 全量（仅按需）。
4. **可审计**：每个被选中的测试文件都带 `reason`，`--json` 可被 T4 验证者逐条核对。
5. **诚实**：跳过了什么、为什么跳过、哪些是「已知坏文件」被显式跳过，全部打印。

用法（cwd = backend/ 或仓库根目录均可）
--------------------------------------
    python scripts/verify_fast.py --changed          # 默认：只验证本次改动
    python scripts/verify_fast.py --all              # L3 全量
    python scripts/verify_fast.py --path apps/trading/executor.py
    python scripts/verify_fast.py --changed --json
    python scripts/verify_fast.py --dry-run --changed   # 只看选了哪些测试

退出码
------
    0  全部通过
    1  有失败
    2  环境 / 参数错误
    3  测试通过，但存在被显式跳过的「已知收集错误文件」（不是干净的绿灯）

注意：本文件是 CLI 工具，正常的结果输出用 print 是刻意的（见任务要求）。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

# --------------------------------------------------------------------------- #
# 路径常量
# --------------------------------------------------------------------------- #

BACKEND = Path(__file__).resolve().parents[1]          # <ROOT>/backend
ROOT = BACKEND.parent                                  # 仓库根
BASELINE_FILE = BACKEND / ".verify-fast" / "baseline.json"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_KNOWN_BLOCKERS = 3

# --------------------------------------------------------------------------- #
# 已知收集错误（known collection blockers）
#
# 这些文件会让 `pytest apps/` 在 **收集阶段** 直接中断（exit code 2），
# 用户只能看到一个 3 秒的报错，完全不知道发生了什么。fast lane 的职责是
# 「明确报错 + 指路」，而不是静默 ignore 掉它们。
#
# 运行时还会自动发现新的 blocker（见 run_pytest 的 auto-discover 逻辑），
# 所以这个列表只是为了让常见情况在 0 成本下就能被识别。
# --------------------------------------------------------------------------- #
KNOWN_COLLECTION_BLOCKERS: list[dict[str, str]] = [
    {
        "path": "apps/strategy_engine/tests/test_universe.py",
        "reason": (
            "ImportError: cannot import name 'HybridUniverse' "
            "from 'apps.strategy_engine.universe'"
        ),
        "detail": (
            "apps/strategy_engine/universe.py 目前只定义了 "
            "BaseUniverseModel / FixedListUniverse，测试文件却 import 了 "
            "HybridUniverse 与 VolumeTopUniverse。"
        ),
        "hint": (
            "修复二选一：(a) 在 apps/strategy_engine/universe.py 中补齐 "
            "HybridUniverse / VolumeTopUniverse 的实现；"
            "(b) 若这两个类已废弃，改写或删除该测试文件。"
            "fast lane 不会替你改 apps/ 下的文件。"
        ),
    },
]

# --------------------------------------------------------------------------- #
# 变更分类规则表（--list-rules 可导出，供 T4 审计）
# --------------------------------------------------------------------------- #

# 命中即升级为 L3 全量：影响面是「全局」的配置文件
FULL_SUITE_BASENAMES = {
    "conftest.py",
    "pytest.ini",
    "pyproject.toml",
    "setup.cfg",
    "tox.ini",
    "manage.py",
    "celery_app.py",
}
FULL_SUITE_PATTERNS = [
    (r"(^|/)requirements[^/]*\.txt$", "依赖清单变更会影响所有测试的可导入性"),
    (r"(^|/)core/settings/", "Django settings 变更影响全局测试环境"),
    # 注意：这里必须锚定 backend 根，否则 `apps/core/`（一个普通 app）会被
    # 误判成全局层，导致任何 apps/core 下的改动都升级为全量。
    (r"^core/", "backend/core 为全局共享层（Django settings / 全局配置）"),
    (r"(^|/)migrations/", "migration 变更影响全部 DB 相关测试"),
    (r"(^|/)apps/common/", "apps.common 为跨 app 公共层"),
    (r"^utils/", "backend/utils 为跨 app 公共层"),
    (r"^conftest\.py$", "根 conftest 变更影响全部测试"),
]

# 资金 / 订单 / 风控 / 鉴权路径：强制跑整个 app 的测试目录，不允许降级
MONEY_PATH_APPS = {
    "trading": "资金/下单执行",
    "risk": "风控引擎",
    "riskguard": "风控守卫",
    "authentication": "鉴权",
}

# 语义扫描：文件名 / 内容命中这些关键词即视为资金路径（保守起见宁可升级）
MONEY_PATH_SEMANTIC = re.compile(
    r"(order|trade|fill|position|balance|pnl|ledger|withdraw|deposit|"
    r"risk|guard|halt|circuit|margin|leverage|account|auth|token|"
    r"下单|订单|成交|持仓|余额|风控|资金|鉴权|认证)",
    re.IGNORECASE,
)

# 「公共模块」文件名：改这些等于改契约，直接升级为整个 app 的测试目录
PUBLIC_BASENAMES = {
    "__init__.py",
    "base.py",
    "models.py",
    "apps.py",
    "settings.py",
    "constants.py",
    "utils.py",
    "admin.py",
    "serializers.py",
    "urls.py",
    "tasks.py",
    "registry.py",
    "signals.py",
    "exceptions.py",
    "config.py",
}

TEST_FILE_RE = re.compile(r"(^|/)(test_.*\.py|.*_tests\.py|tests\.py)$")
TEST_DIR_RE = re.compile(r"(^|/)tests(/|$)")

# fast lane 自己的状态目录：不能把它自己算成「本次改动」，否则基线快照
# 一保存就会自我触发一次「未映射 → 保守升级」。
SELF_STATE_PREFIXES = ("backend/.verify-fast/", ".verify-fast/")

# 依赖闭包深度：改 A → 谁 import A → 谁 import 那些文件。
# 深度 2 覆盖 `executor.py → views.py → test_position_list_view.py` 这类两跳扇出。
DEFAULT_DEP_DEPTH = 2
DEFAULT_CROSS_APP_DEPTH = 2
DEFAULT_PUBLIC_IMPORT_THRESHOLD = 4

# 「枢纽模块」阈值：某模块的**无界**传递闭包若覆盖超过这个比例的测试文件，
# 说明它是个 hub（例如 apps/trading/executor.py 实测覆盖 139/177 个测试文件）。
# 此时 fast lane 会明确建议 `--all`，而不是假装自己做了精确选择。
HUB_TEST_COVERAGE_RATIO = 0.40


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def _run(
    argv: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
) -> tuple[int, str, float]:
    """跑一个子进程，返回 (exit_code, 合并输出, 墙钟耗时秒)。

    耗时用 time.perf_counter() 量的是**真实墙钟**，可直接被
    `/usr/bin/time -p` 交叉验证。
    """
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            env=merged_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
        out = proc.stdout or ""
        code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        out += f"\n[verify_fast] 超时（{timeout}s）被强制终止\n"
        code = 124
    except FileNotFoundError as exc:
        out = f"[verify_fast] 命令不存在: {exc}\n"
        code = 127
    return code, out, time.perf_counter() - started


def _git(*args: str) -> tuple[int, str]:
    code, out, _ = _run(["git", *args], cwd=ROOT)
    return code, out


def sha256_of(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def module_name(backend_rel: str) -> str | None:
    """`apps/trading/executor.py` → `apps.trading.executor`（pytest rootdir=backend）。"""
    p = PurePosixPath(backend_rel)
    if p.suffix != ".py":
        return None
    parts = list(p.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]
    if not parts:
        return None
    return ".".join(parts)


def module_path(backend_rel: str) -> str | None:
    return module_name(backend_rel)


def app_of(backend_rel: str) -> str | None:
    """`apps/trading/executor.py` → `trading`。"""
    parts = PurePosixPath(backend_rel).parts
    if len(parts) >= 2 and parts[0] == "apps":
        return parts[1]
    return None


def is_test_file(backend_rel: str) -> bool:
    return bool(TEST_FILE_RE.search(backend_rel))


def app_tests_dir(app: str) -> str | None:
    d = BACKEND / "apps" / app / "tests"
    if d.is_dir():
        return f"apps/{app}/tests"
    d2 = BACKEND / "apps" / app
    return f"apps/{app}" if d2.is_dir() else None


# --------------------------------------------------------------------------- #
# import 反向依赖索引
# --------------------------------------------------------------------------- #


def _extract_imports(tree: ast.AST, this_module: str, is_package: bool) -> tuple[set[str], set[str]]:
    """抽取一个文件引用的模块。

    返回 (dotted, potential)：
      dotted     —— 明确的模块引用，如 `import apps.trading.executor`
      potential  —— `from X import Y` 里可能是子模块的 X.Y
                    （保守起见一并计入，宁可多选不可漏选）
    """
    dotted: set[str] = set()
    potential: set[str] = set()
    pkg = this_module.rsplit(".", 1)[0] if "." in this_module else ""
    if is_package:
        pkg = this_module

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                dotted.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = pkg.split(".") if pkg else []
                cut = node.level - 1
                if cut:
                    base_parts = base_parts[: len(base_parts) - cut]
                base = ".".join(base_parts)
                target = f"{base}.{node.module}" if node.module else base
            else:
                target = node.module or ""
            if not target:
                continue
            dotted.add(target)
            for alias in node.names:
                if alias.name != "*":
                    potential.add(f"{target}.{alias.name}")
    return dotted, potential


class DepIndex:
    """backend/ 下全部 .py 的模块级反向依赖索引。"""

    def __init__(self) -> None:
        self.mod_to_path: dict[str, str] = {}
        self.rev: dict[str, set[str]] = defaultdict(set)   # module -> 直接 importer 文件
        self.importers_of: dict[str, set[str]] = defaultdict(set)
        self.all_files: list[str] = []
        self.test_files: list[str] = []

    @classmethod
    def build(cls, backend: Path = BACKEND) -> DepIndex:
        idx = cls()
        scan_roots = ["apps", "core", "utils", "scripts"]
        files: list[str] = []
        for root in scan_roots:
            base = backend / root
            if base.is_dir():
                files.extend(
                    p.relative_to(backend).as_posix()
                    for p in base.rglob("*.py")
                    if "__pycache__" not in p.parts
                )
        for extra in ("conftest.py", "manage.py", "celery_app.py"):
            if (backend / extra).is_file():
                files.append(extra)

        idx.all_files = sorted(files)
        idx.test_files = sorted(f for f in idx.all_files if is_test_file(f))

        # 第一遍：module -> path
        for rel in idx.all_files:
            mod = module_name(rel)
            if mod:
                idx.mod_to_path[mod] = rel

        # 第二遍：解析 import
        for rel in idx.all_files:
            mod = module_name(rel)
            if not mod:
                continue
            path = backend / rel
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except (SyntaxError, OSError):
                continue
            is_pkg = rel.endswith("__init__.py")
            dotted, potential = _extract_imports(tree, mod, is_pkg)
            refs = dotted | potential
            idx.importers_of[mod] = set(refs)
            for ref in refs:
                # 加上所有父前缀：import apps.trading.executor 也依赖 apps.trading
                parts = ref.split(".")
                for i in range(1, len(parts) + 1):
                    idx.rev[".".join(parts[:i])].add(rel)

        return idx

    def dependents(self, module: str, max_depth: int = DEFAULT_DEP_DEPTH) -> dict[str, int]:
        """返回 {文件: 跳数}。深度 0 表示 module 自己。"""
        seen: dict[str, int] = {self.mod_to_path.get(module, ""): 0}
        seen.pop("", None)
        frontier = {module: 0}
        while frontier:
            nxt: dict[str, int] = {}
            for mod, depth in frontier.items():
                if depth >= max_depth:
                    continue
                for f in self.rev.get(mod, ()):
                    if f in seen:
                        continue
                    seen[f] = depth + 1
                    fm = module_name(f)
                    if fm:
                        nxt[fm] = depth + 1
            frontier = nxt
        return seen

    def transitive_test_coverage(self, module: str, cap_depth: int = 50) -> tuple[int, list[str]]:
        """无界（可封顶）传递闭包覆盖了多少个测试文件。

        用于「枢纽模块」判定：如果一个模块的改动在传递闭包上波及大半个测试套件，
        那么任何「精确子集」都是自欺欺人，应当直接建议 --all。
        """
        deps = self.dependents(module, cap_depth)
        tests = sorted({f for f in deps if is_test_file(f)})
        return len(tests), tests


# --------------------------------------------------------------------------- #
# 变更集
# --------------------------------------------------------------------------- #


@dataclass
class Change:
    root_rel: str                       # 相对仓库根，posix
    backend_rel: str | None             # 相对 backend/，posix；None = 不在后端范围
    tracked: bool = True                # False = 未跟踪新文件
    origin: str = "working-tree"        # working-tree / staged / untracked / baseline

    @property
    def in_backend(self) -> bool:
        return self.backend_rel is not None


def collect_changes(args: argparse.Namespace) -> tuple[list[Change], dict[str, object]]:
    """根据模式收集「本次改动」。"""
    meta: dict[str, object] = {"source": args.mode, "git_ref": None, "warnings": []}
    raw: list[tuple[str, str]] = []   # (root_rel, origin)

    if args.mode == "path":
        unknown_paths: list[str] = []
        for item in args.path:
            p = Path(item)
            if not p.is_absolute():
                # 支持从 backend/ 或仓库根两种 cwd 传入
                cand_backend = BACKEND / item
                cand_root = ROOT / item
                if cand_backend.exists():
                    p = cand_backend
                elif cand_root.exists():
                    p = cand_root
                else:
                    p = cand_root
            try:
                rel = p.resolve().relative_to(ROOT).as_posix()
            except ValueError:
                meta["warnings"].append(f"路径不在仓库内，已忽略: {item}")  # type: ignore[union-attr]
                continue
            # 显式指定的路径必须真实存在：否则后面会被当成「非后端」而静默
            # 0 目标并通过（假绿灯）。这里宁可报错也要拦住。
            if not p.exists():
                meta["warnings"].append(f"显式路径不存在，拒绝静默跳过: {item}")  # type: ignore[union-attr]
                unknown_paths.append(item)
                continue
            raw.append((rel, "explicit"))
        if unknown_paths:
            meta["unknown_paths"] = unknown_paths  # type: ignore[assignment]

    elif args.mode == "files-from":
        src = sys.stdin if args.files_from == "-" else open(args.files_from, encoding="utf-8")
        try:
            for line in src:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                p = Path(line)
                if not p.is_absolute():
                    p = (BACKEND / line) if (BACKEND / line).exists() else (ROOT / line)
                try:
                    rel = p.resolve().relative_to(ROOT).as_posix()
                except ValueError:
                    meta["warnings"].append(f"路径不在仓库内，已忽略: {line}")  # type: ignore[union-attr]
                    continue
                raw.append((rel, "explicit"))
        finally:
            if args.files_from != "-":
                src.close()

    elif args.mode == "all":
        meta["source"] = "all"
        return [], meta

    else:  # changed / baseline
        ref = args.since
        meta["git_ref"] = ref
        code, out = _git("diff", "--name-only", ref)
        if code != 0:
            meta["warnings"].append(f"git diff {ref} 失败: {out.strip()}")  # type: ignore[union-attr]
        for line in out.splitlines():
            if line.strip():
                raw.append((line.strip(), "working-tree"))
        # 已 staged 的改动
        code, out = _git("diff", "--name-only", "--cached")
        if code == 0:
            for line in out.splitlines():
                if line.strip():
                    raw.append((line.strip(), "staged"))
        # 未跟踪的新文件
        code, out = _git("ls-files", "--others", "--exclude-standard")
        if code == 0:
            for line in out.splitlines():
                if line.strip():
                    raw.append((line.strip(), "untracked"))

    # 去重（保留首次出现的 origin）
    seen: dict[str, str] = {}
    for rel, origin in raw:
        if rel not in seen:
            seen[rel] = origin

    changes: list[Change] = []
    ignored_self: list[str] = []
    for rel, origin in sorted(seen.items()):
        if rel.startswith(SELF_STATE_PREFIXES):
            ignored_self.append(rel)
            continue
        if rel.startswith("backend/"):
            backend_rel: str | None = rel[len("backend/"):]
        elif (BACKEND / rel).exists() and not (ROOT / rel).exists():
            backend_rel = rel
        else:
            backend_rel = None
        changes.append(
            Change(root_rel=rel, backend_rel=backend_rel, origin=origin)
        )

    if ignored_self:
        meta["warnings"].append(  # type: ignore[union-attr]
            f"已忽略 fast lane 自身状态文件 {len(ignored_self)} 个: {', '.join(ignored_self)}"
        )

    if args.mode == "changed" and args.baseline:
        changes, meta2 = apply_baseline(changes, meta)
        meta.update(meta2)
        meta["source"] = "baseline-diff"

    return changes, meta


def apply_baseline(
    changes: list[Change], meta: dict[str, object]
) -> tuple[list[Change], dict[str, object]]:
    """只保留「基线之后新变化」的文件。

    工作区里往往存在用户正在进行中的大量改动（本仓库当前就有 22 个）。
    把它们全部算作「本次改动」会让 `--changed` 一上来就触发资金路径门禁，
    既慢又不符合「快速响应简单更新」的目标。

    基线记录 = {git HEAD, 每个文件的 sha256}。只有「不在基线里」或
    「sha256 变了」的文件才算本次新改动；其余作为「存量改动」单列输出。
    """
    if not BASELINE_FILE.is_file():
        meta["warnings"].append(  # type: ignore[union-attr]
            f"未找到基线快照 {BASELINE_FILE.relative_to(ROOT)}，"
            "本次仍按全部工作区改动处理。先运行 `--baseline-save` 建立基线。"
        )
        return changes, {}

    try:
        snap = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        meta["warnings"].append(f"基线快照损坏，已忽略: {exc}")  # type: ignore[union-attr]
        return changes, {}

    old: dict[str, str] = snap.get("files", {})
    new_changes: list[Change] = []
    preexisting: list[str] = []
    for ch in changes:
        digest = sha256_of(ROOT / ch.root_rel)
        if old.get(ch.root_rel) == digest:
            preexisting.append(ch.root_rel)
            continue
        new_changes.append(ch)
    return new_changes, {
        "baseline_head": snap.get("head"),
        "preexisting_count": len(preexisting),
        "preexisting_files": preexisting,
    }


def save_baseline() -> int:
    code, head = _git("rev-parse", "HEAD")
    if code != 0:
        head = "unknown"
    code, out = _git("diff", "--name-only", "HEAD")
    files = [ln.strip() for ln in out.splitlines() if ln.strip()]
    code, out = _git("ls-files", "--others", "--exclude-standard")
    files += [ln.strip() for ln in out.splitlines() if ln.strip()]
    snap = {
        "head": head.strip(),
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "files": {f: sha256_of(ROOT / f) for f in sorted(set(files))},
    }
    BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_FILE.write_text(json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[baseline] 已保存 {len(snap['files'])} 个文件的哈希 → "
          f"{BASELINE_FILE.relative_to(ROOT)}")
    print(f"[baseline] HEAD = {snap['head']}")
    print("[baseline] 提示：该文件是本地缓存，建议加入 .gitignore（本次未改动 .gitignore）。")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 选择器：变更 → 受影响测试
# --------------------------------------------------------------------------- #


@dataclass
class Selection:
    tests: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    full_suite: bool = False
    full_suite_reasons: list[str] = field(default_factory=list)
    escalations: list[dict[str, str]] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    hub_modules: list[dict[str, object]] = field(default_factory=list)

    def add(self, test_path: str, reason: str) -> None:
        self.tests[test_path].add(reason)

    def add_app(self, app: str, reason: str) -> None:
        d = app_tests_dir(app)
        if d:
            self.add(d, reason)
        else:
            self.add(f"apps/{app}", reason)

    def to_json(self) -> dict[str, object]:
        return {
            "selected_tests": {
                k: sorted(v) for k, v in sorted(self.tests.items())
            },
            "selected_count": len(self.tests),
            "full_suite": self.full_suite,
            "full_suite_reasons": self.full_suite_reasons,
            "escalations": self.escalations,
            "unmapped": self.unmapped,
            "out_of_scope": self.out_of_scope,
            "notes": self.notes,
            "hub_modules": self.hub_modules,
        }


def triggers_full_suite(backend_rel: str) -> str | None:
    name = PurePosixPath(backend_rel).name
    if backend_rel in FULL_SUITE_BASENAMES or name in FULL_SUITE_BASENAMES:
        return f"全局配置文件 {backend_rel}"
    for pattern, why in FULL_SUITE_PATTERNS:
        if re.search(pattern, backend_rel):
            return f"{why}（{backend_rel}）"
    return None


def is_money_path(backend_rel: str, app: str | None) -> str | None:
    if app and app in MONEY_PATH_APPS:
        return f"资金路径 app: apps/{app}（{MONEY_PATH_APPS[app]}）"
    # 语义扫描：不在上述 app 内，但文件名带金额/下单/风控语义
    if MONEY_PATH_SEMANTIC.search(PurePosixPath(backend_rel).name):
        return f"文件名命中资金/风控语义: {PurePosixPath(backend_rel).name}"
    return None


def select_tests(
    changes: list[Change],
    index: DepIndex,
    dep_depth: int,
    public_threshold: int,
    explicit_all: bool,
    cross_app_depth: int = DEFAULT_CROSS_APP_DEPTH,
) -> Selection:
    sel = Selection()

    if explicit_all:
        sel.full_suite = True
        sel.full_suite_reasons.append("显式 --all")
        return sel

    if not changes:
        sel.notes.append("没有检测到改动，仅执行 L0 静态层与 L2 不变量层。")
        return sel

    total_tests = max(len(index.test_files), 1)

    for ch in changes:
        if not ch.in_backend:
            sel.out_of_scope.append(ch.root_rel)
            continue
        rel = ch.backend_rel
        assert rel is not None

        # 1) 改的就是测试文件 → 直接跑它
        if is_test_file(rel):
            sel.add(rel, "changed-test")
            continue

        # 2) 全局配置 → L3（必须在「非 .py」过滤**之前**判断，
        #    否则 requirements.txt / docker-compose.yml 这类非 .py 的全局文件
        #    会被静默跳过，造成「改了依赖却什么都没跑」的假绿灯）
        why_full = triggers_full_suite(rel)
        if why_full:
            sel.full_suite = True
            sel.full_suite_reasons.append(why_full)
            continue

        # 3) 非 .py（md/json/yml/...）→ 不选测试，只记录
        if not rel.endswith(".py"):
            sel.notes.append(f"非 Python 文件，不参与测试映射: {rel}")
            continue

        app = app_of(rel)
        mod = module_name(rel)

        # 3.5) 枢纽模块检测：无界传递闭包覆盖大半个测试套件 → 硬升级为 L3 全量。
        #
        # 为什么必须是「硬升级」而不是「告警 + 继续跑子集」：
        # 一个模块的传递依赖若覆盖 79% 的测试文件（实测 apps/trading/executor.py
        # 与 apps/core/db_signals.py 都是 139/177），那么「精确子集」在语义上已经
        # 不成立——任何小于全量的选择都必然漏检。此时给绿灯就是假阴性。
        # 宁可慢一次，也不能给出一个会骗人的 PASS。
        if mod:
            n_cov, hub_tests = index.transitive_test_coverage(mod)
            if n_cov / total_tests >= HUB_TEST_COVERAGE_RATIO:
                ratio = n_cov / total_tests
                sel.full_suite = True
                sel.full_suite_reasons.append(
                    f"枢纽模块 {rel}：传递依赖覆盖 {n_cov}/{total_tests} "
                    f"（{ratio:.0%}）个测试文件 ≥ 阈值 {HUB_TEST_COVERAGE_RATIO:.0%}。"
                    "精确子集必然漏检，已强制升级为全量。"
                )
                sel.hub_modules.append(
                    {"file": rel, "covered_tests": n_cov, "total_tests": total_tests}
                )
                continue

        if app is None:
            # 不在 apps/ 下的后端文件（scripts/ 等）。它不被任何 app 拥有，
            # 因此不能按 app 升级；但仍然做反向依赖映射——如果有测试 import 它，
            # 就必须跑那些测试（否则是假阴性）。
            if mod:
                deps_ns = index.dependents(mod, dep_depth)
                mapped_ns = _map_tests(sel, rel, mod, app=None, deps=deps_ns, index=index)
                if mapped_ns:
                    sel.notes.append(f"{rel} 不在 apps/ 下，已按反向依赖选中其测试。")
                else:
                    sel.unmapped.append(rel)
                    sel.notes.append(
                        f"{rel} 不在 apps/ 下且无测试反向依赖它 → 不选测试"
                        "（L0 静态检查仍会覆盖它）。"
                    )
            else:
                sel.unmapped.append(rel)
            continue

        # 4) 资金/风控/鉴权路径 → 强制整个 app + 所有跨 app 反向依赖
        money = is_money_path(rel, app)
        if money:
            sel.escalations.append(
                {"file": rel, "action": f"强制 apps/{app}/tests 全量", "reason": money}
            )
            sel.add_app(app, f"money-path-full-app({app})")
            _add_cross_app_dependents(sel, rel, index, cross_app_depth, skip_app=app)
            continue

        deps = index.dependents(mod, dep_depth) if mod else {}
        dep_count = max(len(deps) - 1, 0)   # 减掉自己

        # 5) 公共模块 → 升级为该 app 全量
        base = PurePosixPath(rel).name
        parts = PurePosixPath(rel).parts
        at_app_root = len(parts) == 3   # apps/<app>/<file>.py
        if base in PUBLIC_BASENAMES:
            _escalate(sel, app, rel, f"公共模块文件名 {base}")
            _add_cross_app_dependents(sel, rel, index, cross_app_depth, skip_app=app)
            continue
        if dep_count >= public_threshold:
            _escalate(sel, app, rel, f"被 {dep_count} 个文件依赖（阈值 {public_threshold}）")
            _add_cross_app_dependents(sel, rel, index, cross_app_depth, skip_app=app)
            continue
        if at_app_root and dep_count >= 2:
            _escalate(sel, app, rel, f"app 根层模块且被 {dep_count} 个文件依赖")
            _add_cross_app_dependents(sel, rel, index, cross_app_depth, skip_app=app)
            continue

        # 6) 常规映射：同名测试 + 反向依赖
        mapped = _map_tests(sel, rel, mod, app, deps, index)
        if not mapped:
            _escalate(sel, app, rel, "找不到任何映射测试（保守升级）")
            continue

    return sel


def _escalate(sel: Selection, app: str, rel: str, reason: str) -> None:
    sel.escalations.append(
        {"file": rel, "action": f"升级为 apps/{app}/tests 全量", "reason": reason}
    )
    sel.add_app(app, f"app-full-escalation({app})")


def _add_cross_app_dependents(
    sel: Selection, rel: str, index: DepIndex, dep_depth: int, skip_app: str
) -> None:
    """资金路径 / 公共模块升级时，把**其他 app** 里反向依赖它的测试也纳入。

    这是 Lead 指出的第二层假阴性来源：改 apps/trading/executor.py 只跑
    apps/trading/tests 会漏掉 apps/riskguard 与 apps/strategy_engine 的回归。
    """
    mod = module_name(rel)
    if not mod:
        return
    deps = index.dependents(mod, dep_depth)
    for f, depth in sorted(deps.items()):
        if not is_test_file(f):
            continue
        other = app_of(f)
        if other == skip_app:
            continue   # 同 app 已被 app 全量覆盖
        sel.add(f, f"cross-app-reverse-import(depth={depth})")


def _map_tests(
    sel: Selection,
    rel: str,
    mod: str | None,
    app: str | None,
    deps: dict[str, int],
    index: DepIndex,
) -> bool:
    """常规映射：同名测试优先，其次反向依赖。返回是否有命中。"""
    hit = False
    stem = PurePosixPath(rel).stem

    # (a) 同名测试（仅当文件归属某个 app 时才有 app 内的同名测试目录）
    if app:
        for cand in (
            f"apps/{app}/tests/test_{stem}.py",
            f"apps/{app}/tests/{stem}_tests.py",
            f"apps/{app}/tests/{stem}.py",
        ):
            if (BACKEND / cand).is_file():
                sel.add(cand, "same-name")
                hit = True

    # (b) 反向依赖（含 1~N 跳），覆盖其他 app 的测试
    if mod:
        for f, depth in sorted(deps.items()):
            if not is_test_file(f):
                continue
            if depth == 0:
                continue
            sel.add(f, f"reverse-import(depth={depth})")
            hit = True

    return hit


# --------------------------------------------------------------------------- #
# 分层执行
# --------------------------------------------------------------------------- #


@dataclass
class LayerResult:
    name: str
    ran: bool
    ok: bool
    duration_s: float
    detail: str = ""
    output: str = ""
    skipped_reason: str = ""
    extra: dict[str, object] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "ran": self.ran,
            "ok": self.ok,
            "duration_s": round(self.duration_s, 3),
            "detail": self.detail,
            "skipped_reason": self.skipped_reason,
            **({"extra": self.extra} if self.extra else {}),
        }


def find_ruff() -> str | None:
    """ruff 不在 venv 里，需要在 PATH / homebrew 里找。"""
    from shutil import which

    return which("ruff")


def layer_l0_static(py_files: list[str], verbose: bool) -> LayerResult:
    """L0：语法编译 + ruff check（仅改动文件）。ruff format --check 为建议项。"""
    started = time.perf_counter()
    if not py_files:
        return LayerResult("L0-static", False, True, 0.0, skipped_reason="无 .py 改动")

    notes: list[str] = []
    ok = True
    outputs: list[str] = []

    # L0.1 py_compile（最便宜、最确定的语法门禁）
    code, out, _ = _run(
        [sys.executable, "-m", "py_compile", *py_files], cwd=BACKEND
    )
    if code != 0:
        ok = False
        outputs.append("--- py_compile ---\n" + out)
    notes.append(f"py_compile {len(py_files)} 个文件")

    # L0.2 ruff check（仓库风格门禁，阻断）
    ruff = find_ruff()
    if ruff:
        code, out, _ = _run([ruff, "check", "--no-cache", *py_files], cwd=BACKEND)
        if code != 0:
            ok = False
            outputs.append("--- ruff check ---\n" + out)
        notes.append("ruff check")
        # L0.3 ruff format --check（建议项，不阻断）
        # 仓库现有 283 个文件未格式化，若设为阻断则任何改动都会红，失去意义。
        fcode, fout, _ = _run(
            [ruff, "format", "--check", "--no-cache", *py_files], cwd=BACKEND
        )
        if fcode != 0:
            n = len([ln for ln in fout.splitlines() if ln.startswith("Would reformat")])
            notes.append(f"ruff format --check: {n} 个文件建议格式化（建议项，不阻断）")
            if verbose:
                outputs.append("--- ruff format --check (advisory) ---\n" + fout)
    else:
        notes.append("ruff 未安装，已跳过 ruff 检查（建议 `brew install ruff`）")

    return LayerResult(
        "L0-static",
        True,
        ok,
        time.perf_counter() - started,
        detail=" · ".join(notes),
        output="\n".join(outputs),
    )


COLLECT_ERROR_RE = re.compile(r"^ERROR\s+(\S+\.py)", re.MULTILINE)


def _blocker_in_targets(blocker_path: str, targets: list[str]) -> bool:
    """判断一个已知坏文件是否会被本次选中的目标收集到。

    只有「会被收集到」时才需要处理并在报告里算作本次跳过；
    否则它只是仓库级既存问题，单列为信息提示即可。
    """
    bp = blocker_path.rstrip("/")
    for t in targets:
        tp = t.rstrip("/")
        if bp == tp or bp.startswith(tp + "/") or tp.startswith(bp + "/"):
            return True
    return False


def _partition_blocked(
    targets: list[str], blocked_paths: set[str]
) -> tuple[list[str], list[str], list[str]]:
    """把被阻塞的路径从 target 列表里摘掉。

    关键实现细节：pytest 的 `--ignore` **无法**覆盖直接写在命令行上的文件参数
    （实测：`pytest <bad_file> --ignore <bad_file>` 仍然在收集阶段中断）。
    所以对「显式文件 target」必须从参数里删掉；对「目录 target」则用 --ignore
    精确排除目录内的坏文件。

    返回 (保留的 targets, 需要 --ignore 的路径, 被摘掉/排除的路径)。
    """
    kept: list[str] = []
    ignores: list[str] = []
    removed: list[str] = []
    for t in targets:
        tp = t.rstrip("/")
        if tp in {b.rstrip("/") for b in blocked_paths}:
            removed.append(t)
            continue
        # 目录 target：把落在它下面的坏文件用 --ignore 排除
        inside = [
            b for b in blocked_paths
            if b.rstrip("/").startswith(tp + "/")
        ]
        if inside:
            ignores.extend(inside)
        kept.append(t)
    return kept, ignores, removed


def run_pytest(
    targets: list[str],
    label: str,
    known_blockers: list[dict[str, str]],
    verbose: bool,
    extra_args: list[str] | None = None,
    auto_retry: bool = True,
) -> LayerResult:
    """跑 pytest，并自动处理「已知收集错误」。

    收集错误（exit code 2）会让整轮测试中断——用户等了几秒只看到一个报错。
    这里做三件事：
      1. 只对「本次会收集到」的已知坏文件做排除（显式文件 target 从参数里摘掉，
         目录 target 用 --ignore），快路径 0 成本；
      2. 若仍然出现收集错误，从输出里解析出出错文件，**明确报告 + 指路**，
         然后排除它们重试一次（只重试一次，避免死循环）；
      3. 未被选中的已知坏文件作为「仓库级既存问题」信息提示，不算本次跳过。
    """
    started = time.perf_counter()
    if not targets:
        return LayerResult(label, False, True, 0.0, skipped_reason="没有选中的测试")

    skipped: list[dict[str, str]] = []
    informational: list[dict[str, str]] = []
    blocked_paths: set[str] = set()
    for b in known_blockers:
        if _blocker_in_targets(b["path"], targets):
            blocked_paths.add(b["path"])
            skipped.append(b)
        else:
            informational.append(b)

    def _build(kept: list[str], ignores: list[str]) -> list[str]:
        argv = [sys.executable, "-m", "pytest", *kept, "-p", "no:cacheprovider"]
        if extra_args:
            argv.extend(extra_args)
        for ig in ignores:
            argv.extend(["--ignore", ig])
        return argv

    kept, ignores, removed = _partition_blocked(targets, blocked_paths)
    outputs: list[str] = []

    if not kept:
        # 本次选中的目标全是已知坏文件 → 不跑 pytest，直接给出可操作的报告
        outputs.append(
            "[verify_fast] 本次选中的目标全部是已知收集错误文件，已跳过执行。\n"
            "被跳过的目标: " + ", ".join(removed) + "\n"
        )
        return LayerResult(
            label,
            True,
            True,          # 不是测试失败；由 known_blockers 决定退出码 3
            time.perf_counter() - started,
            detail="全部目标被已知收集错误排除",
            output="\n".join(outputs),
            extra={
                "targets": targets,
                "pytest_exit_code": None,
                "no_tests_collected": True,
                "skipped_known_blockers": skipped,
                "informational_blockers": informational,
            },
        )

    argv = _build(kept, ignores)
    code, out, _ = _run(argv, cwd=BACKEND)
    outputs.append(out)

    # 自动发现新的收集错误并重试一次
    if code == 2 and auto_retry:
        found = sorted(set(COLLECT_ERROR_RE.findall(out)))
        found = [f for f in found if f not in blocked_paths]
        if found:
            for f in found:
                blocked_paths.add(f)
                skipped.append(
                    {
                        "path": f,
                        "reason": "运行时自动发现的收集错误（详见下方 pytest 输出）",
                        "hint": "修复该文件的 import/语法错误，或从测试集中移除。",
                    }
                )
            kept2, ignores2, _ = _partition_blocked(kept, blocked_paths)
            outputs.append(
                "\n[verify_fast] 检测到收集错误，已排除上述文件并重试一次\n"
            )
            if kept2:
                code, out2, _ = _run(_build(kept2, ignores2), cwd=BACKEND)
                outputs.append(out2)
            else:
                outputs.append("[verify_fast] 排除后已无可执行目标\n")

    ok = code == 0
    no_tests_collected = code == 5
    detail = "pytest " + " ".join(kept[:3]) + (" ..." if len(kept) > 3 else "")
    return LayerResult(
        label,
        True,
        ok,
        time.perf_counter() - started,
        detail=detail,
        output="\n".join(outputs),
        extra={
            "targets": kept,
            "requested_targets": targets,
            "pytest_exit_code": code,
            "no_tests_collected": no_tests_collected,
            "skipped_known_blockers": skipped,
            "informational_blockers": informational,
        },
    )


def layer_l2_invariants() -> LayerResult:
    started = time.perf_counter()
    script = BACKEND / "scripts" / "check_invariants.py"
    if not script.is_file():
        return LayerResult(
            "L2-invariants", False, True, 0.0,
            skipped_reason="scripts/check_invariants.py 不存在",
        )
    code, out, _ = _run(
        [sys.executable, str(script)],
        cwd=BACKEND,
        env={"DJANGO_SETTINGS_MODULE": "core.settings.test"},
    )
    return LayerResult(
        "L2-invariants",
        True,
        code == 0,
        time.perf_counter() - started,
        detail="scripts/check_invariants.py",
        output=out,
        extra={"exit_code": code},
    )


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #


class Reporter:
    """人类可读输出 → stdout；--json 模式下人类输出 → stderr。"""

    def __init__(self, json_mode: bool, quiet: bool) -> None:
        self.json_mode = json_mode
        self.quiet = quiet
        self.stream = sys.stderr if json_mode else sys.stdout

    def out(self, text: str = "") -> None:
        if self.quiet:
            return
        print(text, file=self.stream)

    def head(self, text: str) -> None:
        self.out("")
        self.out("=" * 72)
        self.out(text)
        self.out("=" * 72)


def report_selection(rep: Reporter, sel: Selection, changes: list[Change],
                     meta: dict[str, object]) -> None:
    rep.head("fast lane · 变更范围")
    rep.out(f"模式        : {meta.get('source')}   git_ref={meta.get('git_ref')}")
    rep.out(f"改动文件数  : {len(changes)}")
    if meta.get("preexisting_count"):
        rep.out(f"其中存量改动: {meta['preexisting_count']}（基线之前就已存在，本次不计入）")
    for ch in changes:
        scope = ch.backend_rel if ch.in_backend else "(非后端)"
        rep.out(f"  - [{ch.origin}] {ch.root_rel}   → {scope}")

    for w in meta.get("warnings", []) or []:
        rep.out(f"  ! {w}")

    if sel.out_of_scope:
        rep.out("")
        rep.out(f"超出后端范围（fast lane 不处理，共 {len(sel.out_of_scope)}）:")
        for f in sel.out_of_scope:
            rep.out(f"  - {f}")
        rep.out("  → 前端请单独运行 frontend 的 lint/build。")

    if sel.full_suite:
        rep.out("")
        rep.out("!! 升级为 L3 全量，原因:")
        for r in sel.full_suite_reasons:
            rep.out(f"   - {r}")

    if sel.escalations:
        rep.out("")
        rep.out(f"保守升级（{len(sel.escalations)} 条）:")
        for e in sel.escalations:
            rep.out(f"  - {e['file']}: {e['action']}  原因: {e['reason']}")

    if sel.notes:
        rep.out("")
        for n in sel.notes:
            rep.out(f"  · {n}")


def report_selection_tests(rep: Reporter, sel: Selection) -> None:
    rep.head(f"fast lane · 选中的测试目标（{len(sel.tests)} 个）")
    if not sel.tests:
        rep.out("（无）")
        return
    for path, reasons in sorted(sel.tests.items()):
        rep.out(f"  {path}")
        rep.out(f"      reason: {', '.join(sorted(reasons))}")


def report_layers(rep: Reporter, layers: list[LayerResult]) -> None:
    rep.head("fast lane · 分层执行结果")
    for lr in layers:
        if lr.ran:
            mark = "PASS" if lr.ok else "FAIL"
        else:
            mark = "SKIP"
        rep.out(f"[{mark}] {lr.name:<14} {lr.duration_s:7.3f}s  {lr.detail}")
        if lr.skipped_reason:
            rep.out(f"         跳过原因: {lr.skipped_reason}")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="verify_fast.py",
        description="快速验证通道：按改动范围分层执行验证，资金路径强制全量。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python scripts/verify_fast.py --changed\n"
            "  python scripts/verify_fast.py --path apps/trading/executor.py --dry-run\n"
            "  python scripts/verify_fast.py --all --json\n"
        ),
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--changed", dest="mode", action="store_const", const="changed",
                      help="只验证 git 改动（默认）")
    mode.add_argument("--all", dest="mode", action="store_const", const="all",
                      help="L3 全量测试")
    mode.add_argument("--path", dest="path", action="append", default=None,
                      metavar="FILE",
                      help="显式指定改动文件（可重复）")
    mode.add_argument("--files-from", dest="files_from", default=None,
                      metavar="FILE",
                      help="从文件/`-`(stdin) 读取改动文件列表，每行一个")
    p.set_defaults(mode=None, path=None, files_from=None)

    p.add_argument("--since", default="HEAD", help="git 对比基线（默认 HEAD）")
    p.add_argument("--baseline", action="store_true",
                   help="只验证基线快照之后的新变化（先 --baseline-save）")
    p.add_argument("--baseline-save", action="store_true", help="保存当前状态为验证基线")

    p.add_argument("--json", action="store_true", help="stdout 输出 JSON，人类日志走 stderr")
    p.add_argument("--dry-run", action="store_true", help="只展示计划，不执行")
    p.add_argument("--quiet", "-q", action="store_true", help="抑制人类可读输出")
    p.add_argument("--verbose", "-v", action="store_true", help="打印子进程完整输出")
    p.add_argument("--skip-static", action="store_true", help="跳过 L0")
    p.add_argument("--skip-invariants", action="store_true", help="跳过 L2")
    p.add_argument("--no-auto-retry", action="store_true",
                   help="收集错误时不自动 --ignore 重试")
    p.add_argument("--dep-depth", type=int, default=DEFAULT_DEP_DEPTH,
                   help=f"反向依赖闭包深度（默认 {DEFAULT_DEP_DEPTH}）")
    p.add_argument("--cross-app-depth", type=int, default=DEFAULT_CROSS_APP_DEPTH,
                   help="资金路径/公共模块升级时，跨 app 反向依赖的闭包深度"
                        f"（默认 {DEFAULT_CROSS_APP_DEPTH}）")
    p.add_argument("--public-import-threshold", type=int,
                   default=DEFAULT_PUBLIC_IMPORT_THRESHOLD,
                   help="被多少文件依赖即视为公共模块并升级为该 app 全量")
    p.add_argument("--list-rules", action="store_true", help="打印映射规则表后退出")
    return p


def list_rules() -> int:
    print("=" * 72)
    print("fast lane 变更映射规则表")
    print("=" * 72)
    print("\n[A] 命中即升级为 L3 全量（全局影响）:")
    print(f"    文件名: {sorted(FULL_SUITE_BASENAMES)}")
    for pat, why in FULL_SUITE_PATTERNS:
        print(f"    路径正则: {pat:<28} {why}")
    print("\n[B] 资金/风控/鉴权强制门禁（跑整个 app 测试目录 + 跨 app 反向依赖）:")
    for app, why in sorted(MONEY_PATH_APPS.items()):
        print(f"    apps/{app:<16} {why}")
    print(f"    文件名语义正则: {MONEY_PATH_SEMANTIC.pattern}")
    print("\n[C] 公共模块 → 升级为该 app 全量测试目录:")
    print(f"    文件名 ∈ {sorted(PUBLIC_BASENAMES)}")
    print(f"    或 被依赖文件数 ≥ {DEFAULT_PUBLIC_IMPORT_THRESHOLD}")
    print("    或 位于 app 根层（apps/<app>/<x>.py）且被依赖数 ≥ 2")
    print("\n[D] 常规映射（仅当以上都不命中）:")
    print("    (1) 同名测试: apps/<app>/tests/test_<stem>.py")
    print("    (2) AST 反向依赖: 所有 import 该模块的测试文件（含跨 app）")
    print(f"        依赖闭包深度 = {DEFAULT_DEP_DEPTH}")
    print("\n[E] 兜底:")
    print("    找不到任何映射测试 → 升级为该 app 全量（绝不放过）")
    print("=" * 72)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # 模式推导：--path / --files-from 隐含对应模式；都没给则默认 --changed
    if args.mode is None:
        if args.path:
            args.mode = "path"
        elif args.files_from:
            args.mode = "files-from"
        else:
            args.mode = "changed"

    if args.list_rules:
        return list_rules()

    if args.baseline_save:
        return save_baseline()

    rep = Reporter(json_mode=args.json, quiet=args.quiet)
    t0 = time.perf_counter()

    # ---- 收集改动 ----
    changes, meta = collect_changes(args)
    if args.mode == "path" and not changes:
        rep.out("[verify_fast] --path 未解析出任何仓库内文件。")
        for w in meta.get("warnings", []) or []:
            rep.out(f"  ! {w}")
        rep.out(f"  仓库根: {ROOT}")
        rep.out("  请传入仓库内路径，例如 backend/apps/trading/executor.py")
        return EXIT_USAGE

    # 显式给出的路径若不存在，必须显式报错并以非零退出结束；
    # 绝不允许落进「0 个测试目标 → PASS」的假绿灯（已实测出现过）。
    unknown_paths = meta.get("unknown_paths") or []
    if unknown_paths:
        rep.out("[verify_fast] 显式指定的路径不存在，已拒绝静默跳过：")
        for item in unknown_paths:
            rep.out(f"  ? {item}")
        rep.out(f"  仓库根: {ROOT}")
        rep.out("  （不存在的路径会映射不到任何测试，进而产生假 PASS；请修正路径后重试。）")
        return EXIT_USAGE

    # ---- 构建依赖索引 & 选择测试 ----
    index = DepIndex.build()
    sel = select_tests(
        changes,
        index,
        dep_depth=args.dep_depth,
        public_threshold=args.public_import_threshold,
        explicit_all=(args.mode == "all"),
        cross_app_depth=args.cross_app_depth,
    )

    if sel.full_suite:
        all_tests = list(index.test_files)
        sel.tests = defaultdict(set)
        for t in all_tests:
            sel.add(t, "full-suite")

    report_selection(rep, sel, changes, meta)
    report_selection_tests(rep, sel)

    if args.dry_run:
        rep.head("dry-run：未执行任何检查")
        if args.json:
            print(json.dumps({
                "schema": 1,
                "mode": args.mode,
                "dry_run": True,
                "changes": [c.root_rel for c in changes],
                "selection": sel.to_json(),
            }, indent=2, ensure_ascii=False))
        return EXIT_OK

    # ---- 执行各层 ----
    py_files = sorted(
        c.backend_rel for c in changes if c.in_backend and c.backend_rel.endswith(".py")
    )
    layers: list[LayerResult] = []

    if args.skip_static:
        layers.append(LayerResult("L0-static", False, True, 0.0,
                                  skipped_reason="--skip-static"))
    else:
        layers.append(layer_l0_static(py_files, verbose=args.verbose))

    targets = sorted(sel.tests.keys())
    if sel.full_suite:
        # L3 全量：直接用 `apps/` 作为唯一 target，与 legacy `pytest apps/` 的
        # 收集语义完全一致（逐个文件传参在 conftest/插件层面不等价，且更慢）。
        # 逐文件清单仍保留在 --json 的 selection 里供审计。
        targets = ["apps/"]
    if targets:
        l1 = run_pytest(
            targets,
            "L1-affected",
            KNOWN_COLLECTION_BLOCKERS,
            verbose=args.verbose,
            auto_retry=not args.no_auto_retry,
        )
    else:
        l1 = LayerResult(
            "L1-affected", False, True, 0.0,
            skipped_reason=(
                "没有改动 → 无需跑测试"
                if not changes
                else "改动未映射到任何后端测试（见上方 note）"
            ),
        )
    layers.append(l1)

    if args.skip_invariants:
        layers.append(LayerResult("L2-invariants", False, True, 0.0,
                                  skipped_reason="--skip-invariants"))
    else:
        layers.append(layer_l2_invariants())

    if not sel.full_suite:
        layers.append(LayerResult(
            "L3-full", False, True, 0.0,
            skipped_reason=(
                "未显式请求 --all，且本次改动未命中全量升级规则"
                "（conftest/pytest.ini/settings/migrations/requirements 等）"
            ),
        ))

    # ---- 打印子进程输出 ----
    for lr in layers:
        if not lr.output:
            continue
        if args.verbose or not lr.ok:
            rep.head(f"{lr.name} 输出")
            rep.out(lr.output)

    # ---- 汇总 ----
    total = time.perf_counter() - t0
    failed = [lr for lr in layers if lr.ran and not lr.ok]
    blockers: list[dict[str, str]] = []
    informational: list[dict[str, str]] = []
    for lr in layers:
        blockers.extend(lr.extra.get("skipped_known_blockers", []) or [])  # type: ignore[arg-type]
        informational.extend(lr.extra.get("informational_blockers", []) or [])  # type: ignore[arg-type]
    # 去重
    seen_b: set[str] = set()
    blockers = [b for b in blockers if not (b["path"] in seen_b or seen_b.add(b["path"]))]
    seen_i: set[str] = set()
    informational = [
        b for b in informational if not (b["path"] in seen_i or seen_i.add(b["path"]))
    ]

    report_layers(rep, layers)

    if blockers:
        rep.head("!! 本次运行中被显式跳过的「已知收集错误」文件（不是干净的绿灯）")
        for b in blockers:
            rep.out(f"  ✗ {b['path']}")
            rep.out(f"      原因: {b.get('reason', '?')}")
            if b.get("detail"):
                rep.out(f"      说明: {b['detail']}")
            if b.get("hint"):
                rep.out(f"      修复: {b.get('hint')}")
        rep.out("")
        rep.out("  → 这些文件会让包含它们的 pytest 调用在**收集阶段**直接中断（几秒后报错退出）。")
        rep.out("  → fast lane 显式列出并指路，而不是静默忽略。")
        rep.out("  → 修复后重跑本命令即可恢复干净状态（退出码从 3 变回 0）。")

    if informational:
        rep.head("ℹ 仓库级既存问题（本次未收集到，仅供知悉）")
        for b in informational:
            rep.out(f"  · {b['path']}")
            rep.out(f"      {b.get('reason', '?')}")
            if b.get("hint"):
                rep.out(f"      修复: {b.get('hint')}")
        rep.out("")
        rep.out("  → 不在本次选中范围内，因此不影响本次结论；但 `--all` 会撞上它们。")

    rep.head(f"总计 {total:.3f}s · " + ("FAIL" if failed else ("PASS(带已知阻塞)" if blockers else "PASS")))
    for lr in layers:
        if lr.ran:
            rep.out(f"  {lr.name:<14} {lr.duration_s:7.3f}s  {'ok' if lr.ok else 'FAILED'}")
        else:
            rep.out(f"  {lr.name:<14} {'-':>7}  skipped: {lr.skipped_reason}")
    if failed:
        rep.out("")
        rep.out("失败层: " + ", ".join(lr.name for lr in failed))

    if failed:
        exit_code = EXIT_FAIL
    elif blockers:
        exit_code = EXIT_KNOWN_BLOCKERS
    else:
        exit_code = EXIT_OK

    if args.json:
        payload = {
            "schema": 1,
            "mode": args.mode,
            "dry_run": False,
            "duration_s": round(total, 3),
            "exit_code": exit_code,
            "status": (
                "failed" if failed
                else ("passed_with_known_blockers" if blockers else "passed")
            ),
            "changes": [{"path": c.root_rel, "origin": c.origin} for c in changes],
            "change_meta": meta,
            "selection": sel.to_json(),
            "layers": [lr.to_json() for lr in layers],
            "known_collection_blockers": blockers,
            "informational_blockers": informational,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
