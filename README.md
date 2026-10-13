# Trade Agent System

> 以 Agent 为核心的 AI 量化交易助手系统

---

## 系统概述

Trade Agent System 是一套以 AI Agent 为主框架的量化交易辅助平台。用户通过 Telegram Bot 或 Web 界面与系统交互，SupervisorAgent 解析用户意图并路由到专业子 Agent 执行分析、策略生成、风控评估、交易计划等任务。交易执行框架、辅助框架、回测框架均为懒加载，由 Agent 按需启动。

```
用户 (Telegram / Web)
       │
       ▼
 SupervisorAgent  ←── LLM 意图解析
   ├── AnalystAgent         市场分析 / 信号生成
   ├── QuantEngineerAgent   策略代码生成
   ├── CoachAgent           复盘 / 交易计划
   └── RiskAdvisorAgent     风险评估
       │
       ▼
 交易框架 / 辅助框架 / 回测框架  (懒加载)
       │
       ▼
 PostgreSQL + Redis + pgvector + TimescaleDB
```

---

## 技术栈

| 层次 | 技术 |
|------|------|
| Web 框架 | Django 5 + DRF + Django Channels |
| 异步任务 | Celery + Redis |
| LLM | OpenAI GPT-4o（主）→ Anthropic Claude（降级）|
| 向量记忆 | pgvector (L3 语义检索) |
| 时序数据 | TimescaleDB klines 超表 |
| 消息通道 | Telegram Bot (Phase 1) / WebSocket (Phase 2) |
| 交易所 | CCXT (Binance / OKX / Bybit) |
| 加密 | Fernet (API Key 加密存储) |
| 容器化 | Docker + docker-compose |

---

## 目录结构

```
trade_agent_sys/
├── backend/
│   ├── apps/
│   │   ├── authentication/   # 用户注册/登录 (JWT)
│   │   ├── agent/            # BaseAgent, SupervisorAgent, 子Agent, LLM客户端
│   │   ├── skill/            # BaseSkill, SkillRegistry, 具体Skill实现
│   │   ├── memory/           # 分层记忆管理器 (L1/L2/L3)
│   │   ├── exchange/         # 交易所账户管理 (CCXT + Fernet)
│   │   ├── trading/          # Order, Strategy 模型 & 视图
│   │   ├── riskguard/        # 风控守卫 + RiskEvent 档案（旧 apps/risk 已摘除）
│   │   ├── backtest/         # BacktestResult
│   │   ├── channel/          # Telegram Channel 适配器
│   │   └── notify/           # 通知管理
│   ├── core/
│   │   ├── settings/         # base / dev / prod 分层配置
│   │   ├── urls.py
│   │   └── asgi.py
│   ├── prompts/              # Prompt 文件（版本化，v1/v2/...）
│   │   └── v1/
│   │       ├── supervisor.txt
│   │       ├── analyst.txt
│   │       ├── coach.txt
│   │       ├── quant.txt
│   │       └── risk_advisor.txt
│   ├── celery_app.py         # Celery 定时任务
│   ├── manage.py
│   ├── requirements.txt
│   └── Dockerfile
├── scripts/
│   └── init_db.sql           # pgvector + TimescaleDB 初始化
├── docs/
│   ├── arch/                 # 架构设计文档 (v1~final)
│   └── design/               # 详细设计文档 (agent_skill, infra, trading_risk)
├── docker-compose.yml
└── README.md
```

---

## 快速开始

### 1. 配置环境变量

```bash
cp backend/.env.example backend/.env
# 编辑 .env，填入以下必填项：
# OPENAI_API_KEY=...
# TELEGRAM_BOT_TOKEN=...
# FERNET_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
```

### 2. 启动基础服务

```bash
docker-compose up -d db redis
```

### 3. 安装依赖并初始化数据库

```bash
cd backend
uv venv
uv pip install -r requirements.txt

DJANGO_SETTINGS_MODULE=core.settings.dev uv run python manage.py migrate
DJANGO_SETTINGS_MODULE=core.settings.dev uv run python manage.py createsuperuser
```

### 4. 启动开发服务器

```bash
# Django (ASGI)
DJANGO_SETTINGS_MODULE=core.settings.dev uv run python manage.py runserver

# Celery worker (另一个终端)
DJANGO_SETTINGS_MODULE=core.settings.dev uv run celery -A celery_app worker -l info

# Celery beat (定时任务，另一个终端)
DJANGO_SETTINGS_MODULE=core.settings.dev uv run celery -A celery_app beat -l info
```

### 5. Docker 一键启动（完整栈）

```bash
docker-compose up --build
```

---

## API 概览

| 方法 | 路径 | 描述 |
|------|------|------|
| POST | `/api/auth/register/` | 注册 |
| POST | `/api/auth/login/` | 登录，返回 JWT |
| POST | `/api/auth/refresh/` | 刷新 Token |
| POST | `/api/agent/chat/` | 向 SupervisorAgent 发送消息 |
| GET  | `/api/agent/frame/status/` | 查询框架运行状态 |
| POST | `/api/agent/frame/control/` | 启动/停止交易或监控框架 |
| GET  | `/api/exchange/` | 交易所账户列表 |
| POST | `/api/exchange/` | 添加交易所账户 |
| GET  | `/api/trading/orders/` | 订单列表 |
| GET  | `/api/trading/strategies/` | 策略列表 |
| GET  | `/api/risk/events/` | 风险事件列表 |
| GET  | `/api/risk/config/` | 风控配置 |
| GET  | `/api/backtest/` | 回测结果列表 |
| GET  | `/api/memory/` | Agent 记忆列表 (L3) |
| GET  | `/api/skill/` | 已注册 Skill 列表 |
| GET  | `/api/notify/` | 通知列表 |

认证方式：`Authorization: Bearer <access_token>`

---

## 核心设计要点

### Agent 生命周期
- **SupervisorAgent**：常驻单例，接收所有用户消息
- **子 Agent**：懒加载单例（通过 `AgentRegistry`），首次调用时实例化
- **框架**：交易框架、辅助框架、回测框架均默认停止，由 Agent 显式启动

### LLM 降级机制
```
OpenAI GPT-4o  →（超时/限速/错误）→  Anthropic Claude  →（失败）→  FALLBACK_MARKER
```
Skill 内检测到 `FALLBACK_MARKER` 时走规则引擎兜底分支。

### 分层记忆
| 层 | 存储 | 用途 | TTL |
|----|------|------|-----|
| L1 | 进程内 LRU (50条/agent) | 当前会话上下文 | 进程生命周期 |
| L2 | Redis LIST | 短期决策记忆 | 24h |
| L3 | PostgreSQL + pgvector | 长期语义记忆 | 永久 |

### Prompt 版本管理
Prompt 文件存放在 `prompts/v{N}/` 目录下，变更必须新建版本目录，保证可回滚（ADR-004）。

---

## 开发说明

### 添加新 Skill

```python
# apps/skill/skills/my_skill.py
from apps.skill.base import BaseSkill
from apps.skill.registry import SkillRegistry


@SkillRegistry.register
class MySkill(BaseSkill):
    name = 'MySkill'
    description = '...'

    async def execute(self, payload: dict):
        ...
```

然后在 `apps/skill/skills/__init__.py` 中 import 即可自动注册。

### 添加新 Agent

```python
# apps/agent/sub_agents.py 中继承 _LLMAgent
@AgentRegistry.register_class
class MyAgent(_LLMAgent):
    name = 'my_agent'
    prompt_name = 'my_agent'  # prompts/v1/my_agent.txt
```

并在 `INTENT_TO_AGENT` 映射中添加对应意图。

---

## 设计文档

- [最终架构文档](docs/arch/architecture_final.md)
- [Agent 与 Skill 详细设计](docs/design/agent_skill.md)
- [基础设施设计](docs/design/infra.md)
- [交易与风控设计](docs/design/trading_risk.md)
