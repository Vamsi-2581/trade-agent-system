# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

这是一个 **AI Agent 驱动的量化交易辅助系统**，用户通过 Telegram Bot 与 SupervisorAgent 交互，Agent 解析意图后路由到专业子 Agent（分析、策略、风控、教练）执行任务。所有子 Agent 和交易框架均为懒加载。

## 开发命令

### 环境初始化
```bash
cd backend
uv venv
uv pip install -r requirements.txt
cp .env.example .env
# 填入 .env 中的必填项（OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, FERNET_KEY 等）
```

### 数据库
所有环境统一使用 Docker Compose 运行的 PostgreSQL + PgBouncer：
```bash
# 启动数据库和依赖服务
docker-compose up -d db redis pgbouncer

# 执行迁移（后端容器已配置 DB_HOST=pgbouncer）
docker-compose exec backend python manage.py migrate
```

> PostgreSQL 端口映射到宿主机 `6432`（PgBouncer 映射到 `5432`）。
> 本地直连：`psql -h localhost -p 6432 -U trade -d trade_agent`

### 运行服务
```bash
# 完整栈（Django + Celery Worker + Celery Beat + Redis + PostgreSQL + PgBouncer + Lark WS）
docker-compose up -d --build

# 前端（如适用）
npm run dev
```

### 测试
```bash
# 确保 Docker 环境已启动
docker-compose up -d

# 运行所有测试（在后端容器中执行）
docker-compose exec backend pytest

# 运行单个测试文件
docker-compose exec backend pytest apps/agent/tests/test_xxx.py

# 带覆盖率
docker-compose exec backend pytest --cov=apps --cov-report=term-missing
```

### Docker 完整栈
```bash
docker-compose up --build
```

## 关键架构模式

### 1. 注册中心模式（Agent / Skill / SkillRegistry）

**Agent 注册** — `AgentRegistry` 使用类装饰器实现懒加载单例：
```python
# backend/apps/agent/registry.py
@AgentRegistry.register_class
class MyAgent(_LLMAgent):
    name = 'my_agent'
```
首次调用 `AgentRegistry.get('my_agent')` 时才实例化。

**Skill 注册** — `SkillRegistry` 使用类装饰器：
```python
# backend/apps/skill/registry.py
@SkillRegistry.register
class MySkill(BaseSkill):
    name = 'MySkill'
```
只需在 `apps/skill/skills/__init__.py` 中 import，装饰器自动注册。

**意图路由** — `apps/agent/supervisor.py` 中 SupervisorAgent 通过 LLM 动态识别：将各 Agent prompt body 中的概述描述传给 LLM，由 LLM 根据用户消息内容判断应路由到哪个 Agent，不再依赖 prompt frontmatter 的 `intent` 字段。新增 Agent 只需在 prompts 目录下添加 prompt 文件即可。

### 2. 分层记忆架构

| 层 | 存储 | TTL | 管理 |
|----|------|-----|------|
| L1 | 进程内 `OrderedDict`（LRU，50条/agent） | 进程生命周期 | `apps/memory/l1.py` |
| L2 | Redis LIST | 24h | `apps/memory/l2.py` |
| L3 | PostgreSQL + pgvector（向量检索） | 永久 | `apps/memory/l3.py` |

### 3. LLM 降级链

```
OpenAI gpt-4o → (超时/429/错误) → DeepSeek (deepseek-chat) → (失败) → FALLBACK_MARKER
```
实现在 `apps/agent/llm_client.py`（`chat()` / `chat_with_tools()`），第二跳由 `DEEPSEEK_MODEL_FALLBACK` 等设置决定；Skill 内检测 `FALLBACK_MARKER` 走规则引擎兜底。

### 4. Redis Stream 消息总线

各 Stream 使用独立 DB：
- `agent:tasks` (DB3) — Agent 任务队列
- `trading:orders` (DB4) — 订单执行
- `trading:positions` (DB4) — 持仓同步
- `risk:events` (DB4) — 风险事件

### 5. Prompt 版本管理

所有 Prompt 文件在 `backend/prompts/v{N}/` 下，变更必须新建版本目录。通过 `PromptLoader`（`apps/agent/prompt_loader.py`）加载。

### 6. 设置分层

| 文件 | 用途 |
|------|------|
| `core/settings/base.py` | 通用配置（所有环境共享） |
| `core/settings/dev.py` | 开发覆盖（连接 Docker 数据库、verbose 日志） |
| `core/settings/prod.py` | 生产覆盖（强制 PostgreSQL、严格安全设置） |

切换方式：`DJANGO_SETTINGS_MODULE=core.settings.dev`（或 `prod`）。

## 新增功能指南

### 新增 Skill
1. 创建 `apps/skill/skills/my_skill.py`，继承 `BaseSkill`
2. 用 `@SkillRegistry.register` 装饰
3. 在 `apps/skill/skills/__init__.py` 中 import

### 新增 Agent
1. 在 `apps/agent/sub_agents.py` 中继承 `_LLMAgent`
2. 用 `@AgentRegistry.register_class` 装饰
3. 在 `backend/prompts/v1/` 下创建 prompt 文件，frontmatter 中设置 `name`，body 中描述 Agent 的职责概述（LLM 会基于此自动路由）

### 新增 Celery 定时任务
在 `backend/celery_app.py` 的 `beat_schedule` 中添加条目。

## 核心依赖说明

- **LLM**: `openai>=1.35.0`, `anthropic>=0.29.0` — 通过 `llm_client.py` 统一调用
- **交易所**: `ccxt>=4.3.50` — 支持 Binance/OKX/Bybit
- **消息队列**: Celery + Redis Stream — 非 Celery 的高吞吐任务走 Stream 直连
- **向量检索**: pgvector — 用于 L3 语义记忆的相似度搜索
- **加密**: `cryptography` Fernet — 交易所 API Key 加密存储

## Database & Async Rules
- This project uses Django async. NEVER write raw sync database queries in async contexts. ALWAYS use `sync_to_async` or the centralized `db_async` utility for any DB operation.
- Before debugging database connection errors, verify the Docker environment is running and not serving stale code (restart containers first).
- When fixing async/threading issues, always check for `sync_to_async` usage and `close_old_connections()` calls in long-running loops (e.g., Celery tasks, grid search).
- Timezone: Django `TIME_ZONE="UTC"`; the machine's local tz is NOT reliable. `datetime.fromtimestamp(ts)` with no `tz=` returns the **local** tz and silently offsets timestamps (this caused a funding-settlement bug). ALWAYS pass `tz=timezone.utc` when converting epoch→datetime. `datetime.now()` (naive) is acceptable only for TUI display / stats; use `django.utils.timezone.now()` (aware-UTC) for data paths. Enforced by the `timezone_convention` invariant in `scripts/check_invariants.py` (L3 CI).

## Fix Verification Protocol
- After a bug fix, run the tests affected by the change (not necessarily the full suite). For reversible, low-impact changes, don't gate on the full suite—broaden scope only on new failures, regressions, or unresolved concerns.
- When debugging, verify the runtime environment FIRST (Docker containers running? correct ports? stale code?) before analyzing code logic.
- Do NOT make multiple speculative code changes. Diagnose the root cause thoroughly before editing. If 2 attempts fail, re-read the error and the actual code flow before a 3rd attempt.

## Code Change Discipline
- When using formatters (Prettier, Black, etc.), scope changes to ONLY the files/lines relevant to the task. Revert unrelated formatting changes before committing.
- Prefer minimal, targeted edits over broad refactors unless explicitly asked to refactor.

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary — `needs-triage` / `needs-info` / `ready-for-agent` / `ready-for-human` / `wontfix`, each label string equal to its role name. See `docs/agents/triage-labels.md`.

### Domain docs

multi-context — a root `CONTEXT-MAP.md` points at one `CONTEXT.md` per sub-project; ADRs in `docs/adr/` (system-wide) and the owning context's `docs/adr/` (context-scoped). See `docs/agents/domain.md`.
