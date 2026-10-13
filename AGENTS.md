# AGENTS.md

## WHY (Purpose)
本系统是一个 AI Agent 驱动的量化交易辅助系统，旨在将自然语言交易意图转化为受控的 Django/Celery 执行逻辑，通过 Telegram Bot 与用户交互。

## WHAT (Stack & Map)

### 技术栈
- **Backend**: Django 5.x (REST API + ASGI WebSocket)
- **Task Queue**: Celery + Redis Stream (异步撮合与回测)
- **Database**: PostgreSQL + pgvector (交易指令、K线数据、向量记忆)
- **LLM**: OpenAI GPT-4o / Anthropic Claude (降级链)
- **Exchange**: ccxt (Binance/OKX/Bybit)

### 核心模块
- `apps/agent/` - SupervisorAgent + 子 Agent（分析、策略、风控、教练）
- `apps/skill/` - 技能注册中心
- `apps/trading/` - 交易执行框架（懒加载）
- `apps/riskguard/` - 风控守卫（含风控事件 RiskEvent 档案，2026-10-12 自已摘除的 apps/risk 迁入）
- `apps/backtest/` - 回测框架
- `apps/strategy_engine/` - 策略引擎（独立运行，支持回测/实盘双模式）
- `apps/signal_monitor/` - 信号监控（技术指标计算）
- `apps/channel/` - 通道管理（TUI 交互）
- `apps/notify/` - 通知系统（WebSocket 路由）
- `apps/logging_app/` - 日志应用
- `apps/datasource/` - 数据源管理
- `apps/exchange/` - 交易所连接（ccxt 封装）
- `apps/authentication/` - 用户认证
- `apps/memory/` - 分层记忆（L1/L2/L3）

### Frontend (tradeclaw-web)
- **框架**: Next.js (App Router)
- **位置**: `frontend/tradeclaw-web/`
- **入口**: `src/app/layout.tsx`
- **组件**: `src/components/`
- **API 层**: `src/lib/api.ts`
- **自定义 Hooks**: `src/hooks/`
- **状态上下文**: `src/context/`

### 架构约束
- **依赖层级**: Types → Config → Repo → Service → API → UI（单向依赖）
- **懒加载**: 所有子 Agent 和交易框架首次调用时才实例化
- **注册中心模式**: AgentRegistry / SkillRegistry 使用类装饰器

## HOW (Commands & Standards)

### 环境初始化
```bash
cd backend
uv venv
uv pip install -r requirements.txt
cp .env.example .env
```

### 数据库
```bash
# 开发环境 (SQLite)
DJANGO_SETTINGS_MODULE=core.settings.dev uv run python manage.py migrate

# 生产环境 (PostgreSQL)
python manage.py migrate
```

### 运行服务
```bash
# Django ASGI
DJANGO_SETTINGS_MODULE=core.settings.dev uv run python manage.py runserver

# Celery Worker
DJANGO_SETTINGS_MODULE=core.settings.dev uv run celery -A celery_app worker -l info

# Celery Beat
DJANGO_SETTINGS_MODULE=core.settings.dev uv run celery -A celery_app beat -l info
```

### Docker 完整栈
```bash
# 启动全部服务
docker-compose up --build

# 仅启动特定服务
docker-compose up backend celery redis
```

### 测试
```bash
# 相关测试（按需；资金/风控路径必须覆盖）
DJANGO_SETTINGS_MODULE=core.settings.dev uv run pytest --cov=apps --cov-report=term-missing

# 单文件测试
DJANGO_SETTINGS_MODULE=core.settings.dev uv run pytest apps/agent/tests/test_xxx.py

# DST 仿真测试
SIMULATION_MODE=True DJANGO_SETTINGS_MODULE=core.settings.dev uv run pytest apps/trading/tests/dst/
```

### 代码质量
```bash
# Lint
ruff check backend/

# 格式化
ruff format backend/

# 类型检查
pyright backend/
```

## VERIFICATION PYRAMID (Pointers)

验证协议详见：
- **L1 语义比对**: [docs/SHADOW_EVAL.md](docs/SHADOW_EVAL.md)
- **L2 确定性仿真**: [docs/DST_TESTING.md](docs/DST_TESTING.md)
- **L3 形式化规范**: [docs/TLA_SPECS.md](docs/TLA_SPECS.md)

### 自动验证机制

验证技能通过以下方式自动运行：

#### 1. PostToolUse Hook（编辑后触发）
当编辑 `backend/apps/*.py` 文件时，自动运行：
- 格式化检查
- Lint 检查
- 验证技能

#### 2. Stop Hook（会话结束时触发）
会话结束时自动运行完整验证：
- 代码变更验证
- 不变量检查

#### 3. CI/CD（提交时强制执行）
GitHub Actions 自动运行验证金字塔各层检查。

#### 4. 手动触发
```bash
# 不变量检查
cd backend && python scripts/check_invariants.py
```

## DONE CRITERIA (硬性契约)

智能体完成任务必须满足：
1. ✅ 改动相关测试通过（资金/订单/风控/鉴权路径必须有测试覆盖）
2. ✅ Ruff Lint 无错误
3. ✅ 类型检查通过
4. ✅ 无 console.log / print 调试语句
5. ✅ 无硬编码密钥/凭证

