const API_BASE = process.env.NEXT_PUBLIC_API_URL || "";

// ── Token Management ──

export function getAccessToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("tradeclaw_access");
}

export function getRefreshToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("tradeclaw_refresh");
}

export function setTokens(access: string, refresh: string) {
  localStorage.setItem("tradeclaw_access", access);
  localStorage.setItem("tradeclaw_refresh", refresh);
}

export function clearTokens() {
  localStorage.removeItem("tradeclaw_access");
  localStorage.removeItem("tradeclaw_refresh");
}

// ── Errors ──

/**
 * 一次失败的 API 调用：**状态码 + 后端给的那句话**。
 *
 * 存在的理由只有一个：后端已经把「为什么不行」写成了人话
 * （`apps/regime/views.py` 的 `{"error": "切换原因不能为空"}`），而 `request()` 此前把它
 * 丢掉、只留一句 `API error: 400`。页面上要显示原文时，就只能靠猜状态码反推原因。
 *
 * **必须继承 `Error`**：全仓既有调用方写的是 `err instanceof Error ? err.message : ...`
 * （`settings/page.tsx`、`hooks/useOrders.ts` 等几十处）。不继承，那些地方会静默退化成
 * 「未知错误」——一个没有报错路径的错。
 *
 * `status` 是给**分支**用的（400 请求拼错 vs 403 权限 vs 503 通路故障），`message` 是给
 * **人看**的。两者都要：只给 message，前端就得解析字符串才知道该不该显示「再试一次」。
 */
export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/**
 * 从失败响应里取那句话。**解析不出来就退回 `API error: <status>`**，绝不抛第二个错。
 *
 * 三条容错都是真实情形，不是防御性编程：
 * - 后端 500 白页 / 反代 HTML 错误页 → `JSON.parse` 失败；
 * - `204` 之类的空体 → 空串；
 * - 结构化但不是 `{error}`（DRF 的字段校验错）→ 取不到字符串。
 *
 * 认 `error` 与 `detail` 两个键：本仓自己的视图一律回 `{"error"}`，而 DRF 的权限类
 * （`IsStaffUser` 的 `message`）回的是 `{"detail"}`——403 那句「此操作需要管理员权限。」
 * 只有认 `detail` 才到得了页面上。
 */
async function messageFrom(res: Response): Promise<string> {
  const fallback = `API error: ${res.status}`;
  let text: string;
  try {
    text = await res.text();
  } catch {
    return fallback;
  }
  if (!text) return fallback;

  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    return fallback;
  }
  if (body === null || typeof body !== "object") return fallback;

  const record = body as Record<string, unknown>;
  for (const key of ["error", "detail"]) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return fallback;
}

// ── Base Fetch ──

async function request<T>(
  path: string,
  method = "GET",
  body?: unknown
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  const token = getAccessToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }

  const res = await fetch(`${API_BASE}${path.replace(/\/$/, "")}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });

  if (res.status === 401) {
    // Try refresh
    const refreshed = await tryRefreshToken();
    if (!refreshed) {
      clearTokens();
      window.location.href = "/login";
      // 仍然是同一句话、同一个类型（`ApiError` 是 `Error` 的子类），只是多带了状态码。
      // **刷新失败这条路径不读响应体**：这个 401 说的是「令牌过期了」，而后端对它的正文
      // 通常是 `{"detail": "Given token not valid..."}`——把它显示给人只会盖住「请重新登录」。
      throw new ApiError("Unauthorized", 401);
    }
    // Retry with new token
    const newToken = getAccessToken();
    if (newToken) {
      headers.Authorization = `Bearer ${newToken}`;
    }
    const retryRes = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!retryRes.ok) {
      throw new ApiError(await messageFrom(retryRes), retryRes.status);
    }
    if (retryRes.status === 204) {
      return undefined as T;
    }
    return retryRes.json() as Promise<T>;
  }

  if (!res.ok) {
    throw new ApiError(await messageFrom(res), res.status);
  }

  // 204 No Content has no body
  if (res.status === 204) {
    return undefined as T;
  }

  return res.json() as Promise<T>;
}

export async function tryRefreshToken(): Promise<boolean> {
  const refresh = getRefreshToken();
  if (!refresh) return false;
  try {
    const res = await fetch(`${API_BASE}/api/auth/token/refresh/`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh }),
    });
    if (!res.ok) return false;
    const data = await res.json();
    setTokens(data.access, refresh);
    return true;
  } catch {
    return false;
  }
}

// ── Auth API ──

export interface LoginPayload {
  email: string;
  password: string;
}

/** 当前用户的可读档案（`/api/auth/me/` 与登录/注册响应里的 `user` 是同一个形状）。
 *
 *  `is_staff` 是 P5 加的（`docs/adr/0005-web-write-requires-staff.md`）：页面要按它决定
 *  「翻开关 / 触发重算」这些按钮显不显示。**只有 `is_staff`，没有 `is_admin`**——两者是
 *  同一个布尔，并排出现会让人以为有粒度。
 */
export interface UserProfile {
  id: number;
  email: string;
  username: string;
  is_active: boolean;
  is_frozen: boolean;
  /** 写入口的界面门槛。`true` 才显示写按钮；读对全部登录用户开放。 */
  is_staff: boolean;
  created_at: string;
  last_login_at: string | null;
}

export interface LoginResponse {
  user: UserProfile;
  access: string;
  refresh: string;
}

export interface RegisterPayload {
  email: string;
  username: string;
  password: string;
}

export const authApi = {
  login: (payload: LoginPayload) =>
    request<LoginResponse>("/api/auth/login/", "POST", payload),

  register: (payload: RegisterPayload) =>
    request<LoginResponse>("/api/auth/register/", "POST", payload),

  me: () => request<UserProfile>("/api/auth/me/"),

  logout: () => {
    const refresh = getRefreshToken();
    return request<Record<string, string>>("/api/auth/logout/", "POST", {
      refresh,
    });
  },
};

// ── Trading API ──

export interface Order {
  id: string;
  request_id: string;
  exchange_account: number;
  symbol: string;
  side: "buy" | "sell";
  order_type: "market" | "limit" | "stop";
  quantity: string;
  price: string | null;
  status: "pending" | "submitted" | "partial" | "filled" | "cancelled" | "failed";
  exchange_order_id: string;
  filled_quantity: string;
  avg_fill_price: string | null;
  realized_pnl: string | null;
  error_message: string;
  live_session_id: string | null;
  strategy_id: string | null;
  strategy_name: string | null;
  triggered_strategy: string;
  created_at: string;
  updated_at: string;
}

export interface Strategy {
  id: string;
  name: string;
  code_path: string;
  git_commit_hash: string;
  is_active: boolean;
  created_at: string;
}

/** 策略**声明**的一个参数（`params_schema` 的一项）。
 *  说"策略允许你设什么"——与 `LiveSessionDetail["params"]` 的"这条会话实际在跑什么"
 *  是两件事，别混用。`default` 是该键未被覆盖时真正生效的值。 */
export interface StrategyParamSpec {
  key: string;
  /** 声明里的 type（"int" | "float" | "bool" | "string" | null）。null = 声明里没写。 */
  type: string | null;
  default: unknown;
  description: string;
  min: number | null;
  max: number | null;
}

export interface StrategyParamsResponse {
  strategy_name: string;
  /** false = 解析不到策略实现类，`params` 必为空。 */
  resolved: boolean;
  params: StrategyParamSpec[];
}

export const tradingApi = {
  getOrders: () => request<Order[]>("/api/trading/orders/"),
  getOrder: (id: string) => request<Order>(`/api/trading/orders/${id}/`),
  getStrategies: () => request<Strategy[]>("/api/trading/strategies/"),
  /** 某策略声明的参数表。部署会话时用它渲染"可覆盖的参数"——
   *  没有这个入口，像 `volume_filter` 这种默认关闭的键就没地方打开
   *  （2026-10-12 SOL/USDT 审计）。 */
  getStrategyParams: (name: string) =>
    request<StrategyParamsResponse>(
      `/api/trading/strategies/${encodeURIComponent(name)}/params/`
    ),
  createStrategy: (data: Partial<Strategy>) =>
    request<Strategy>("/api/trading/strategies/", "POST", data),
  getSummary: () =>
    request<TradingSummary>("/api/trading/summary/"),
  getPositions: () =>
    request<PositionResponse>("/api/trading/positions/"),
  getAccounts: () =>
    request<ExchangeAccountWithBalance[]>("/api/trading/accounts/"),
  getKlines: (symbol: string, interval: string, limit = 300) =>
    request<{ symbol: string; interval: string; count: number; ohlcv_data: OHLCVPoint[] }>(
      `/api/trading/klines/?${new URLSearchParams({
        symbol,
        interval,
        limit: String(limit),
      }).toString()}`
    ),
};

// ── Trading Page Types ──

export interface PositionInfo {
  exchange: string;
  exchange_account_id?: string;
  symbol: string;
  side: "long" | "short";
  quantity: string;
  entry_price: string;
  mark_price: string;
  unrealized_pnl: string;
}

export interface PositionResponse {
  positions: PositionInfo[];
  executor_running: boolean;
  message?: string;
}

export interface TradingSummary {
  total_equity: string;
  today_realized_pnl: string;
  active_orders_count: number;
  total_orders_today: number;
  account_count: number;
}

export interface ExchangeAccountWithBalance {
  id: string;
  exchange: string;
  label: string;
  is_active: boolean;
  testnet: boolean;
  created_at: string;
  /** `null` = 这次没问过交易所（`OrderExecutor` 没跑），**不是**「余额是 0」。 */
  balance?: {
    total: string;
    available: string;
    used: string;
  } | null;
}

// ── Risk API ──
// 风控事件档案接口（右侧风控面板的轮询源）。前端可写、guard 从不读取的
// RiskConfig 已随 apps/risk 摘除（2026-10-12），不再提供 config 端点。

export interface RiskEvent {
  id: string;
  level: "P0" | "P1" | "P2";
  event_type:
    | "hard_limit"
    | "circuit_breaker"
    | "reconciliation"
    | "heartbeat"
    | "floating_loss";
  message: string;
  resolved: boolean;
  created_at: string;
  resolved_at: string | null;
}

export const riskApi = {
  getEvents: () => request<RiskEvent[]>("/api/risk/events/"),
};

// ── Exchange API ──

export interface ExchangeAccount {
  id: string;
  exchange: string;
  label: string;
  is_active: boolean;
  testnet: boolean;
  created_at: string;
  /** `null` = 这次没问过交易所（`OrderExecutor` 没跑），**不是**「余额是 0」。 */
  balance?: {
    total: string;
    available: string;
    used: string;
  } | null;
}

export interface CreateExchangeAccountPayload {
  exchange: string;
  label?: string;
  api_key: string;
  api_secret: string;
  testnet?: boolean;
  leverage?: number;
}

export const exchangeApi = {
  getAccounts: () => request<ExchangeAccount[]>("/api/exchange/accounts/"),
  createAccount: (data: CreateExchangeAccountPayload) =>
    request<ExchangeAccount>("/api/exchange/accounts/", "POST", data),
  deleteAccount: (id: string) => {
    // 204 No Content has no body, so we skip JSON parsing
    return request<void>(`/api/exchange/accounts/${id}/`, "DELETE");
  },
};

// ── Agent API ──

export interface AgentInfo {
  name: string;
  display_name: string;
  role: string;
  description: string;
  status: "ready" | "standby" | "running" | "stopped";
  tools: string[];
  intent: string;
  tag_color: string;
  tag_bg: string;
  frame_state?: string;
}

export interface AgentListResponse {
  agents: AgentInfo[];
}

export const agentApi = {
  chat: (message: string, frameId?: string) =>
    request<Record<string, unknown>>("/api/agent/chat/", "POST", {
      message,
      frame_id: frameId,
    }),
  getFrameStatus: () =>
    request<Record<string, unknown>>("/api/agent/frame/status/"),
  controlFrame: (action: string, mode = "live") =>
    request<Record<string, unknown>>("/api/agent/frame/control/", "POST", {
      frame: "trading",
      action,
      mode,
    }),
  listAgents: () => request<AgentListResponse>("/api/agent/list/"),
};

// ── Logging API ──

export interface SystemLog {
  id: string;
  level: "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";
  module: string;
  logger_name: string;
  message: string;
  trace_id: string;
  extra_data: Record<string, unknown>;
  created_at: string;
}

export interface LogQueryParams {
  level?: string;
  module?: string;
  search?: string;
  trace_id?: string;
  since?: string;
  before?: string;
  page?: number;
  page_size?: number;
}

export interface PaginatedLogs {
  count: number;
  page: number;
  page_size: number;
  has_next: boolean;
  results: SystemLog[];
}

export const loggingApi = {
  getLogs: (params?: LogQueryParams) => {
    const query = new URLSearchParams();
    if (params?.level) query.set("level", params.level);
    if (params?.module) query.set("module", params.module);
    if (params?.search) query.set("search", params.search);
    if (params?.trace_id) query.set("trace_id", params.trace_id);
    if (params?.since) query.set("since", params.since);
    if (params?.before) query.set("before", params.before);
    if (params?.page) query.set("page", String(params.page));
    if (params?.page_size) query.set("page_size", String(params.page_size));
    const qs = query.toString();
    return request<PaginatedLogs>(`/api/logs/${qs ? `?${qs}` : ""}`);
  },
};

// ── Live Session API ──

/**
 * 会话侧的**资金效率子载荷**（spec #30 D6/D9，子票 #38）。
 *
 * 与回测侧的 `CapitalEfficiency`（`metrics.capital_efficiency`）是**同一组契约键、逐字同名**
 * （spec 用户故事 22：口径不在两处分叉），但两处必须分开读，因为这个子载荷里多了一组
 * **只有会话侧才有**的键，它们说明「这一侧为什么是粗口径」：
 *
 * - `occupied_seconds` 恒为 0、`window_seconds` 是**墙钟**（`started_at → stopped_at/now`），
 *   不是「在仓时长」。会话侧的 `Order` 没有成对的开平仓时刻，只能喂**点观测**
 *   （每个平仓时刻一项，零时长、时间权重 0、仍计一笔）。
 * - 后果是确定的、也是必须标出来的：`avg_capital_used` 是 `0.00000000`、
 *   `time_in_market_pct` 是 `0.0`、`return_per_capital_used_pct` 是 `None`。
 *   **这三个数不是「这条会话没用钱」，而是「这一侧量不了持仓时长」**。当成效率数字读，
 *   会把「重仓短打」读成「几乎没用资金」——恰好把高风险读成高效率。
 *
 * 所以展示层**必须**连 `capital_efficiency_metrics_available` 与
 * `capital_efficiency_notes` 一起读，不能只看摊平到顶层的数字。
 */
export interface SessionCapitalEfficiency {
  /** 与 `capital_efficiency_metrics_available` 同源；子载荷里再带一份供单独调试。 */
  computed?: boolean;
  /** 判不了时的原因码（`invalid_capital` / `empty_window` / `no_trades`）。 */
  reason?: string | null;
  /** 中间量：校验「为什么 avg_capital_used 是 0」必须看到（点观测下恒为 0）。 */
  trade_count?: number;
  occupied_seconds?: number;
  window_seconds?: number;
  sample_threshold?: number;
  pnl_total?: string | null;
  avg_capital_used?: string | null;
  capital_utilization?: number | null;
  idle_pct?: number | null;
  time_in_market_pct?: number | null;
  return_per_capital_used_pct?: number | null;
  sample_sufficient?: boolean;
  notes?: string[];
}

export interface LiveSession {
  id: string;
  strategy: string;
  strategy_name: string;
  backtest_result: string | null;
  backtest_result_id: string | null;
  symbol: string;
  mode: "live" | "paper";
  status: "pending" | "running" | "paused" | "stopped" | "error";
  exchange_account: string;
  exchange_account_name: string | null;
  initial_capital: string;
  current_equity: string | null;
  /** 会话归因已实现盈亏（见 CONTEXT.md 词条）：按会话外键汇总订单的 realized_pnl。
   * 列表接口由 SQL 聚合一次算出，与详情里的统计同源同值。
   * **null 表示「没有这个数」（未注解），不是 0**——卡片对 null 显示「—」，
   * 绝不退回 `current_equity - initial_capital` 自己算一个。 */
  realized_pnl: string | null;
  /** 会话侧资金效率子载荷（见 `SessionCapitalEfficiency`）。 */
  capital_efficiency?: SessionCapitalEfficiency;
  /**
   * **展示这些数字之前必须先看它**（spec #30 用户故事 12 的会话侧）。
   *
   * `false` 时摊平的那六个键一律 `null`——而 `null` 会被 `Number(null)` 读成 `0`，
   * 于是「量不了」在界面上变成「完全没用钱」。所以为 `false` 时展示层只出「—」与
   * `capital_efficiency_notes`，**不渲染任何一个效率数字**。
   */
  capital_efficiency_metrics_available?: boolean;
  /**
   * 「为什么只是粗口径 / 为什么整段没有」。**算得出时也非空**——会话侧没有一次计算
   * 是等价于回测侧的，所以总得有一句说清楚。空列表会被读成「没有任何保留意见」，
   * 而那正是这条链路上最贵的一种误读，因此展示层原样带出。
   */
  capital_efficiency_notes?: string[];
  /** 金额：8 位定点字符串；`available` 为假时是 `null`。 */
  avg_capital_used?: string | null;
  /** 百分比：float 的**百分点**；`available` 为假时是 `null`（不是 0）。 */
  capital_utilization?: number | null;
  idle_pct?: number | null;
  time_in_market_pct?: number | null;
  return_per_capital_used_pct?: number | null;
  /** 订单笔数口径的「样本够不够」。**与子载荷里同名的那个（区间笔数）不是一回事**，
   * 所以后端给了这个不同名的出口——前端用这个，不要用子载荷里的。 */
  sample_sufficient_orders?: boolean;
  config: Record<string, unknown>;
  started_at: string | null;
  stopped_at: string | null;
  created_at: string;
  updated_at: string;
}

/** 会话收益统计。由后端算好，前端只显示——**门槛与口径都在后端**
 * （`sample_sufficient` 由后端按 `sample_threshold` 判定，前端不许自己比大小）。
 *
 * 尾部那几个 `capital_efficiency_*` 键来自子票 #38：详情视图把 `session_stats_for()` 的
 * 返回字典整个放在 `stats` 里，所以那一段在这里**自动出现**。它们与 `LiveSession` 顶层
 * 的同名键是**同一处汇总、同一个数**（有测试钉着两条取数路径不冲突），展示层只需要读一处
 * ——但读之前同样要先看 `capital_efficiency_metrics_available`。 */
export interface LiveSessionStats {
  realized_pnl_total: string;
  closed_orders: number;
  winning_orders: number;
  /** n=0 时是 null（不是 0）：还没成交 ≠ 一笔没赢过。 */
  win_rate: number | null;
  sample_threshold: number;
  sample_sufficient: boolean;
  avg_pnl: string | null;
  best_pnl: string | null;
  worst_pnl: string | null;
  /** 「这个数没覆盖什么」的边界文案，由后端随数字一起给，前端原样显示。 */
  caveats: string[];
  /** **展示效率数字之前必须先看它**——见 `LiveSession.capital_efficiency_metrics_available`。 */
  capital_efficiency_metrics_available?: boolean;
  /** 会话侧为粗口径的说明，算得出时也非空。 */
  capital_efficiency_notes?: string[];
  capital_efficiency?: SessionCapitalEfficiency;
  avg_capital_used?: string | null;
  capital_utilization?: number | null;
  idle_pct?: number | null;
  time_in_market_pct?: number | null;
  return_per_capital_used_pct?: number | null;
  /** 订单笔数口径的「样本够不够」（顶层 `sample_sufficient` 的别名，见后端注释）。 */
  sample_sufficient_orders?: boolean;
}

/** 一行生效参数：策略声明了什么 / 本会话覆盖了什么 / 实际传下去的是什么。 */
export interface SessionParamRow {
  key: string;
  /** false = 不在策略 `params_schema` 里，但会随 config 传到策略手上（runner 无白名单）。 */
  declared: boolean;
  default: unknown;
  override: unknown;
  effective: unknown;
}

export interface SessionParams {
  /** false = 解析不到策略实现类，`rows` 必为空、只有 `config` 原文。 */
  resolved: boolean;
  strategy_name: string | null;
  rows: SessionParamRow[];
  config: Record<string, unknown>;
}

/** 正在拦这条会话的熔断层。`text` 由后端逐字取自 `halt.HaltLayer.text`。 */
export interface SessionHaltLayer {
  text: string;
  reason: string;
  opened_at: string;
  expires_at: string | null;
}

/** 详情响应 = 会话本身 + 统计 + 生效参数 + 熔断层 + 订单流水。 */
export interface LiveSessionDetail extends LiveSession {
  stats: LiveSessionStats;
  params: SessionParams;
  /** 只含命中该会话的层；没有层时是空数组（不是缺字段）。 */
  halt_layers: SessionHaltLayer[];
  /** 订单表是截断的（最近 N 条），`orders_total` 是总数——不显示总数，这张表就像全部。 */
  orders_total: number;
  orders: Order[];
}

export interface CreateSessionPayload {
  backtest_result_id: string;
  mode: "live" | "paper";
  exchange_account_id: string;
  config?: Record<string, unknown>;
}

export const liveSessionApi = {
  list: () => request<LiveSession[]>("/api/trading/sessions/"),
  /** 会话本身的形状。详情弹窗要的统计/参数/熔断层/订单在 `getDetail` 里（同一个 URL，
   * 只是响应更宽）——两个名字并存是提醒：别拿着会话形状去找统计字段。 */
  get: (id: string) => request<LiveSession>(`/api/trading/sessions/${id}/`),
  getDetail: (id: string) => request<LiveSessionDetail>(`/api/trading/sessions/${id}/`),
  create: (data: CreateSessionPayload) =>
    request<{ id: string; status: string; mode: string; message: string }>(
      "/api/trading/sessions/create/",
      "POST",
      data
    ),
  start: (id: string) =>
    request<{ status: string; message: string }>(
      `/api/trading/sessions/${id}/start/`,
      "POST"
    ),
  pause: (id: string) =>
    request<{ status: string; message: string }>(
      `/api/trading/sessions/${id}/pause/`,
      "POST"
    ),
  resume: (id: string) =>
    request<{ status: string; message: string }>(
      `/api/trading/sessions/${id}/resume/`,
      "POST"
    ),
  stop: (id: string) =>
    request<{ status: string; message: string }>(
      `/api/trading/sessions/${id}/stop/`,
      "POST"
    ),
  promote: (id: string) =>
    request<{ id: string; mode: string; status: string; message: string }>(
      `/api/trading/sessions/${id}/promote/`,
      "POST"
    ),
  delete: (id: string) =>
    request<{ message: string; id: string }>(
      `/api/trading/sessions/${id}/delete/`,
      "DELETE"
    ),
};

// ── Backtest API ──

/**
 * 回测 `metrics.capital_efficiency` 那一段（spec #30 用户故事 12，子票 #35 写 / #36 展示）。
 *
 * **每一个字段都可能不出现**：`metrics` 是整份 JSON 原样透出的裸字段，而**存量回测
 * 里压根没有这个键**（不是 null、不是 `{}`）——见 `apps/backtest/serializers.py`。所以
 * 消费方拿到的类型是 `CapitalEfficiency | undefined`，且这里的每个键都标成可选：
 * 类型上说"一定有"，前端就会写出一个在真实数据上崩的读法。
 *
 * **两种数不能混着读**：金额（`avg_capital_used` / `pnl_total`）是 8 位定点**字符串**
 * （这条链路上的钱从不经过二进制浮点），而百分比（`capital_utilization` / `idle_pct` /
 * `time_in_market_pct` / `return_per_capital_used_pct`）是 **float 的百分点**
 * （`100.0` 就是 100%）。格式化分支靠的就是这个区别。
 *
 * **判不了的那几个是 `null`，不是 0**（`invalid_capital` / `empty_window` /
 * `no_trades` 三种）：`0%` 会被读成"完全没占用资金"，而真相是"这一段没有资金效率
 * 证据"。显示层必须把 `null` 渲染成「—」。
 */
export interface CapitalEfficiency {
  /** 载荷形状 / 口径的版本号。跟着口径走，用来判断老行是不是按旧口径算的。 */
  version?: number;
  computed?: boolean;
  /** 判不了时的原因码（`invalid_capital` / `empty_window` / `no_trades`）。 */
  reason?: string | null;
  /** 口径快照：`sample_sufficient` 是拿这个门槛比的。 */
  sample_threshold?: number;
  /** 金额：8 位定点字符串，**不是数字**（见上）。 */
  avg_capital_used?: string | null;
  /** 百分比：float 的**百分点**（25.0 就是 25%）。判不了时是 null。 */
  capital_utilization?: number | null;
  idle_pct?: number | null;
  time_in_market_pct?: number | null;
  return_per_capital_used_pct?: number | null;
  sample_sufficient?: boolean;
  pnl_total?: string | null;
  trade_count?: number;
  occupied_seconds?: number;
  window_seconds?: number;
  /**
   * 口径边界（`notional` 取的是哪一个本金口径、以及"它不是什么"）。**必须显示**：
   * 这是唯一一处说明"这个数覆盖了什么"的地方，藏起来会让数字读起来比它本身更大。
   * 回测侧由后端保证非空。
   */
  notes?: string[];
}

export interface BacktestResult {
  id: string;
  strategy: number;
  strategy_name: string;
  symbol: string;
  timeframe: string;
  start_date: string;
  end_date: string;
  initial_capital: string;
  final_capital: string;
  total_return_pct: number;
  sharpe_ratio: number | null;
  max_drawdown_pct: number | null;
  win_rate: number | null;
  total_trades: number;
  git_commit_hash: string;
  parameters: Record<string, unknown>;
  /** 整份 metrics 原样透出（后端无 allowlist）。`capital_efficiency` 这一段可能整个缺失。 */
  metrics?: Record<string, any>;
  review_status: "pending" | "approved" | "rejected";
  review_notes: string;
  reviewed_at: string | null;
  created_at: string;
}

export interface EquityPoint {
  timestamp: string;
  equity: number;
  drawdown: number;
}

export interface DrawdownPoint {
  timestamp: string;
  drawdown: number;
}

export interface OHLCVPoint {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface IndicatorData {
  ma7?: (number | null)[];
  ma25?: (number | null)[];
  ma99?: (number | null)[];
  boll?: { upper: (number | null)[]; mid: (number | null)[]; lower: (number | null)[] };
  macd?: { dif: (number | null)[]; dea: (number | null)[]; hist: (number | null)[] };
  rsi?: (number | null)[];
}

export interface BacktestDetail extends BacktestResult {
  equity_curve: EquityPoint[];
  drawdown_curve: DrawdownPoint[];
  ohlcv_data: OHLCVPoint[];
  ohlcv_total?: number;  // total bars count when truncated for initial load
  indicator_data: IndicatorData;
}

export interface OHLCVRangeResponse {
  ohlcv_data: OHLCVPoint[];
  indicator_data: IndicatorData;
  start: number;
  end: number;
  total: number;
}

export interface BacktestTrade {
  id: string;
  entry_time: string;
  exit_time: string | null;
  side: "long" | "short";
  entry_price: string | null;
  exit_price: string | null;
  quantity: string;
  pnl: string | null;
  pnl_pct: number | null;
  commission: string | null;
  signal: string;
  exit_reason: string | null;
  trade_type: "open" | "add" | "close";
}

export interface PaginatedTrades {
  count: number;
  num_pages: number;
  current_page: number;
  results: BacktestTrade[];
}

export interface CreateBacktestPayload {
  strategy_name: string;
  symbol: string;
  timeframe: string;
  start_date?: string;
  end_date?: string;
  initial_capital?: number;
  commission_rate?: number;
  parameters?: Record<string, unknown>;
  exchange?: string;
}

export interface BacktestSubmitResponse {
  task_id: string;
  message: string;
}

export interface PaginatedBacktestResults {
  count: number;
  num_pages: number;
  current_page: number;
  results: BacktestResult[];
}

export interface BacktestGroup {
  type: 'grid_search' | 'single' | 'orphaned_grid_search';
  job_id?: string;
  job_name?: string;
  symbol: string;
  timeframe: string;
  status?: 'running' | 'completed' | 'failed' | 'pending' | 'cancelled';
  error?: string;  // 失败原因（status=failed 时后端返回）
  total_combinations?: number;
  completed?: number;
  best_return_pct?: number;
  best_sharpe?: number;
  created_at: string;
  results?: BacktestResult[];  // for grid_search / orphaned
  result?: BacktestResult;     // for single
}

export interface GroupedBacktestResponse {
  groups: BacktestGroup[];
  group_count: number;
  total_records: number;
  num_pages: number;
  current_page: number;
}

export interface BacktestListParams {
  page?: number;
  page_size?: number;
}

export const backtestApi = {
  getList: (params?: BacktestListParams) => {
    const query = new URLSearchParams();
    if (params?.page) query.set("page", String(params.page));
    if (params?.page_size) query.set("page_size", String(params.page_size));
    const qs = query.toString();
    return request<PaginatedBacktestResults>(
      `/api/backtest/results/${qs ? `?${qs}` : ""}`
    );
  },
  getGroupedList: (params?: BacktestListParams) => {
    const query = new URLSearchParams();
    query.set("grouped", "1");
    if (params?.page) query.set("page", String(params.page));
    if (params?.page_size) query.set("page_size", String(params.page_size));
    const qs = query.toString();
    return request<GroupedBacktestResponse>(
      `/api/backtest/results/${qs ? `?${qs}` : ""}`
    );
  },
  getDetail: (id: string) =>
    request<BacktestResult>(`/api/backtest/results/${id}/`),
  getFullDetail: (id: string) =>
    request<BacktestDetail>(`/api/backtest/results/${id}/detail/`),
  fetchEarlierOhlcv: (id: string, limit = 200, cursor?: string, timeframe?: string, symbol?: string): Promise<{ ohlcv_data: OHLCVPoint[]; count: number }> => {
    const query = new URLSearchParams({ limit: String(limit) });
    if (cursor) query.set("cursor", cursor);
    if (timeframe) query.set("timeframe", timeframe);
    if (symbol) query.set("symbol", symbol);
    return request(`/api/backtest/results/${id}/earlier-ohlcv/?${query.toString()}`);
  },
  fetchLaterOhlcv: (id: string, limit = 200, cursor?: string, timeframe?: string, symbol?: string): Promise<{ ohlcv_data: OHLCVPoint[]; count: number }> => {
    const query = new URLSearchParams({ limit: String(limit) });
    if (cursor) query.set("cursor", cursor);
    if (timeframe) query.set("timeframe", timeframe);
    if (symbol) query.set("symbol", symbol);
    return request(`/api/backtest/results/${id}/later-ohlcv/?${query.toString()}`);
  },
  getTrades: (
    id: string,
    params?: { page?: number; page_size?: number; sort?: string }
  ) => {
    const query = new URLSearchParams();
    if (params?.page) query.set("page", String(params.page));
    if (params?.page_size) query.set("page_size", String(params.page_size));
    if (params?.sort) query.set("sort", params.sort);
    const qs = query.toString();
    return request<PaginatedTrades>(
      `/api/backtest/results/${id}/trades/${qs ? `?${qs}` : ""}`
    );
  },
  create: (data: CreateBacktestPayload) =>
    request<BacktestSubmitResponse>("/api/backtest/results/create/", "POST", data),
  review: (id: string, data: { approved: boolean; notes?: string }) =>
    request<{
      id: string;
      review_status: string;
      review_notes: string;
      reviewed_at: string;
    }>(`/api/backtest/results/${id}/review/`, "POST", data),
  rerun: (id: string) =>
    request<{
      task_id: string;
      message: string;
    }>(`/api/backtest/results/${id}/rerun/`, "POST", {}),
};

// ── Scheduled Task API ──

export interface ScheduledTask {
  id: number | string | null;
  name: string;
  agent_name: string;
  message: string;
  schedule: string;
  enabled: boolean;
  last_run_at: string | null;
  total_run_count: number;
  expires: string | null;
  start_time: string | null;
  source: "static" | "database" | "one_time";

  // one_time 特有字段
  status?: string;
  celery_task_id?: string | null;
  result?: string | null;
  error?: string | null;
}

export interface ScheduledTaskListResponse {
  tasks: ScheduledTask[];
}

export const scheduledTaskApi = {
  list: () => request<ScheduledTaskListResponse>("/api/agent/tasks/scheduled/"),
};

// ── Signal Monitor API ──

export interface SignalMonitor {
  id: string;
  name: string;
  symbol: string;
  interval: string;
  source: string;
  indicator_type: string;
  indicator_params: Record<string, unknown>;
  condition: Record<string, unknown>;
  trigger_type: "once" | "continuous";
  action_type: "notify" | "validate_strategy";
  action_params: Record<string, unknown>;
  status: "active" | "triggered" | "disabled" | "expired";
  last_triggered_at: string | null;
  trigger_count: number;
  expires_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface CreateSignalMonitorPayload {
  name: string;
  symbol: string;
  indicator_type: string;
  indicator_params?: Record<string, unknown>;
  condition: Record<string, unknown>;
}

export const signalMonitorApi = {
  list: () => request<{ success: boolean; data: SignalMonitor[] }>("/api/signal-monitor/"),
  create: (data: CreateSignalMonitorPayload) =>
    request<{ success: boolean; data: SignalMonitor }>("/api/signal-monitor/", "POST", data),
  delete: (id: string) =>
    request<void>(`/api/signal-monitor/${id}/`, "DELETE"),
  toggle: (id: string, enabled: boolean) =>
    request<{ success: boolean; data: SignalMonitor }>(
      `/api/signal-monitor/${id}/`,
      "PATCH",
      { status: enabled ? "active" : "disabled" }
    ),
};

// ── Memory API ──

export interface Memory {
  id: string;
  agent_type: string;
  content: string;
  metadata: Record<string, unknown>;
  created_at: string;
}

export const memoryApi = {
  list: () => request<Memory[]>("/api/memory/"),
  delete: (id: string) => request<void>(`/api/memory/${id}/`, "DELETE"),
};

// ── Channel / Feishu API ──

export interface FeishuAuthStatus {
  authorized: boolean;
  open_id?: string;
  expires_at?: string;
  created_at?: string;
}

export interface FeishuQRInitResponse {
  session_id: string;
  qr_url: string;
  expires_in: number;
}

export interface FeishuQRStatusResponse {
  session_id: string;
  status: "pending" | "completed" | "expired" | "failed";
}

export interface FeishuUrlInitResponse {
  session_id: string;
  authorize_url: string;
}

export const channelApi = {
  feishuStatus: () => request<FeishuAuthStatus>("/api/channel/auth/lark/status/"),
  feishuQR: () => request<FeishuQRInitResponse>("/api/channel/auth/lark/qr/"),
  feishuQRStatus: (sessionId: string) =>
    request<FeishuQRStatusResponse>(`/api/channel/auth/lark/qr/${sessionId}/status/`),
  feishuUrl: () => request<FeishuUrlInitResponse>("/api/channel/auth/lark/url/"),
  feishuRefresh: () => request<{ status: string; expires_at: string }>("/api/channel/auth/lark/refresh/", "POST"),
  feishuRevoke: () => request<void>("/api/channel/auth/lark/revoke/", "DELETE"),
};

// ── Workflow History API ──

export interface WorkflowHistoryItem {
  id: string;
  workflow_id: string;
  summary: string;
  status: "completed" | "failed" | "aborted";
  total_steps: number;
  completed_steps: number;
  elapsed_seconds: number;
  error: string;
  created_at: string;
  completed_at: string | null;
}

export interface WorkflowHistoryDetail extends WorkflowHistoryItem {
  step_results: {
    agent: string;
    data: string;
    step: number;
    skipped?: boolean;
    failed?: boolean;
  }[];
}

export interface PaginatedWorkflowHistory {
  count: number;
  num_pages: number;
  current_page: number;
  items: WorkflowHistoryItem[];
}

export const workflowApi = {
  list: (params?: { page?: number; status?: string }) => {
    const query = new URLSearchParams();
    if (params?.page) query.set("page", String(params.page));
    if (params?.status) query.set("status", params.status);
    const qs = query.toString();
    return request<PaginatedWorkflowHistory>(
      `/api/agent/workflow/history/${qs ? `?${qs}` : ""}`
    );
  },
  getDetail: (workflowId: string) =>
    request<WorkflowHistoryDetail>(`/api/agent/workflow/history/${workflowId}/`),
};

// ── Notification API ──

export interface Notification {
  id: string;
  channel: "telegram" | "web";
  message: string;
  is_read: boolean;
  created_at: string;
}

export const notificationApi = {
  list: () => request<Notification[]>("/api/notify/"),
  markRead: (id: string) =>
    request<Record<string, string>>(`/api/notify/${id}/read/`, "POST"),
  markAllRead: () => request<Record<string, string>>("/api/notify/mark-all-read/", "POST"),
  unreadCount: () => request<{ unread_count: number }>("/api/notify/unread-count/"),
};

// ── Market / Ticker API ──

export interface Ticker {
  symbol: string;
  last_price: number;
  bid_price: number;
  bid_quantity: number;
  ask_price: number;
  ask_quantity: number;
  high_24h: number;
  low_24h: number;
  volume_24h: number;
  turnover_24h: number;
  change_24h: number;
  change_pct_24h: number;
  timestamp: string;
  source: string;
}

export const marketApi = {
  ticker: (params: { source: string; symbol: string; market_type?: string }) => {
    const query = new URLSearchParams({
      source: params.source,
      symbol: params.symbol,
      market_type: params.market_type ?? "spot",
    });
    return request<Ticker>(`/api/datasource/api/market/ticker/?${query.toString()}`);
  },
};

// ── Regime API（P5：机制的 Web 读面与写入口） ──

/**
 * 一个枚举取值的传输形状：**取值与展示名成对**。
 *
 * 前端一律读 `display` 显示、读 `value` 做分支，**不许自己把 slug 翻成中文**：
 * `MechanismMode.display` 已经是「Shadow（只记录，不执行）」，前端再写一遍就是同一件事
 * 的第二个说法（`apps/regime/serializers.py::_labelled` 的 docstring）。
 */
export interface Labelled {
  value: string;
  display: string;
}

/** 一行切换流水：什么时候、谁、从哪档到哪档、为什么。 */
export interface RegimeSwitchRow {
  at: string | null;
  actor_kind: Labelled;
  actor_name: string;
  from_mode: Labelled;
  to_mode: Labelled;
  /** 落库的那一串。**不是**请求体里手写的——由服务端在同一次求值里取 `briefing.summary`。 */
  reason: string;
}

/** 一个开关的当前档 + 最近一次切换（`switch_payload`）。 */
export interface RegimeSwitch {
  kind: Labelled;
  mode: Labelled;
  latest: RegimeSwitchRow | null;
  /** `true` = 从没切换过。**是布尔不是文案**：怎么排版属于前端。 */
  never_switched: boolean;
  /**
   * 这一档的**常驻说明**，只有机制整体那一档有：那句「这一档不是执行开关、出 Shadow
   * 只是留一条记录」。**后端渲染的原样文本**，与确认页正文、chat 回执同一处字面量，
   * 前端一个字都不改写（map #40 的渲染分界）。
   */
  note: string | null;
}

/** 一代池化表（`generation_summary`）。 */
export interface RegimeGeneration {
  id: number;
  status: Labelled;
  actor_kind: Labelled;
  actor_name: string;
  pool_version: number;
  started_at: string | null;
  finished_at: string | null;
  candidates: number;
  results_used: number;
  cells_total: number;
  cells_changed: number;
  /** 三个键恒在（计数为 0 也在）：某个原因从没出现过 vs 这个键被改名了，要能分开。 */
  exclusions: Record<string, number>;
  failure: string;
  /** 这一代自己的收场。`reused` **不在**这里——它连行都没有（见 `getRebuildStatus`）。 */
  outcome: Labelled;
  /** 布尔 + 分钟数成对；`minutes === null` 表示**根本不是 `building`**（不是「跑了 0 分钟」）。 */
  stale_building: { stale: boolean; minutes: number | null; threshold_minutes: number };
}

/** 冷启动也是正常状态：`has_generation=false` + 一句后端渲染的 `display`。 */
export type RegimePoolState =
  | { has_generation: false; display: string; generation: null }
  | { has_generation: true; display: string; generation: RegimeGeneration };

export interface RegimeRegimeState {
  regime: Labelled | null;
  effective_at: string | null;
  /** `true` = 库中还没有任何生效过的判定（冷启动），`display` 是那句话。 */
  cold: boolean;
  display: string;
}

/** 首屏（`GET api/regime/overview/`）。**只装廉价只读**，60 秒轮询它。 */
export interface RegimeOverview {
  /** 数组而不是三个具名键：三个开关结构完全同形，顺序由后端给（`SWITCH_KINDS`）。 */
  switches: RegimeSwitch[];
  pool: RegimePoolState;
  regime_state: RegimeRegimeState;
  // 「数据截止」不在这里：由前端从 `pool.generation.finished_at` 与
  // `regime_state.effective_at` 取——后端不另造一个「现在」。
}

/** 池化表的一格（`cell_payload`）。`evidence` 原样透出，一个键都不加工。 */
export interface RegimePoolCell {
  /** `"<策略 id>|<阶段>"`。前端拿它做 key 与行内展开的锚。 */
  key: string;
  strategy: RegimeStrategyRef;
  regime: Labelled;
  source: Labelled;
  state: Labelled;
  reason: Labelled;
  needs_review: boolean;
  /**
   * 证据（去掉 `results`）在 `pool.evidence` 里的键。**不是证据本身**——原文一个键都没少，
   * 只是同一份只发一次（见 `RegimePool` 的 docstring）。用
   * `evidenceOf(evidenceIndex(pool), cell)` 取。
   */
  evidence_key: string;
  /**
   * 那一格的 `results` 在 `pool.results` 里的键。空串 = 那格没有 `results`。
   *
   * 单独一个键而不是并进 `evidence_key`：`results` 是 evidence 里唯一会跨格重复的大数组
   * （占 98.7% 体积），而其余键每格都不同——合成一个键就等于把整份 evidence 当整体去重，
   * 那在生产上等于没去（实测 248 格只降到 243 种）。
   */
  results_key: string;
}

/**
 * 格子里那个 `strategy` 对象：id + 名字 + **机制在册状态** + 最近一次启停流水。
 *
 * `lifecycle` 与 `latest_change` 是「删除策略」那两个按钮的判据：前者说它此刻在不在册
 * （决定按钮是「删除策略」还是「恢复启用」），后者说上一次是谁为什么动的（退役过的行
 * 要显示得出来，否则「删除」看起来没生效）。
 *
 * 两个方向的取值与人话都在后端（`strategy_lifecycle.LIFECYCLE_DISPLAY`）：前端判
 * `value`、显示 `display`，不把 `false` 翻成「已退役」。
 */
export interface RegimeStrategyRef {
  id: string;
  name: string;
  lifecycle: Labelled;
  latest_change: RegimeLifecycleChange | null;
}

/** 一次启停流水（`StrategyLifecycleChange`）：什么时候、谁、从哪档到哪档、为什么。 */
export interface RegimeLifecycleChange {
  at: string | null;
  actor_kind: Labelled;
  actor_name: string;
  from_active: Labelled;
  to_active: Labelled;
  reason: string;
}

/** `GET api/regime/strategies/<id>/`：一条策略的启停确认页。 */
export interface RegimeStrategyLifecyclePage {
  strategy: {
    id: string;
    name: string;
    lifecycle: Labelled;
    latest_change: RegimeLifecycleChange | null;
  };
  /** 退役那一侧。`available: false` 时 `body` 就是那句「本来就是退役状态」。 */
  retire: RegimeLifecycleAction;
  /** 恢复启用那一侧。 */
  restore: RegimeLifecycleAction;
}

/** 确认页的一侧（两个方向同形）。 */
export interface RegimeLifecycleAction {
  action: { value: "retire" | "restore"; display: string };
  /**
   * 这一次动作**会不会**真的写东西。`false` = 本来就是这一档（`body` 是那句话）。
   * 它是数据不是文案：按钮态由它决定，说法由 `body` 给。
   */
  available: boolean;
  /** 后端渲染的确认页正文。前端原样显示，一个字不改。 */
  body: string;
  /** 确认之后会落进流水 `reason` 的那句话（与这一页同源）。 */
  summary: string;
  /** 排版用的数：活跃会话 / 待解除的停用声明 / 停用决策 / 当前代池化格。 */
  counts: {
    active_sessions: number;
    declarations: number;
    decisions: number;
    pool_cells: number;
  };
}

/**
 * `POST api/regime/strategies/<id>/{retire,restore}/` 的回执。
 *
 * **形状与确认页不同**（照 `RegimeSwitchResult` 那条取向）：回执说的是「按下之后」，
 * 确认页那几个键（`available` / `body` / `counts`）说的是「按下之前」，两套事实混进
 * 一个对象会让读的人分不清哪几个数属于哪个时刻。
 */
export interface RegimeStrategyFlipResult {
  strategy: { id: string; name: string; lifecycle: Labelled };
  action: { value: "retire" | "restore"; display: string };
  /** 真翻了 `true`；「本来就是这一档」是 **200 + `false`**，不是错误。 */
  changed: boolean;
  /** 回执那句话：真的翻了就是「已退役 / 已恢复启用」，没翻就是「本来就是这一档」。 */
  display: string;
  /** 落库的那一串（没写流水时是本次求值那一份）。 */
  reason: string;
  at: string | null;
}

/**
 * `GET api/regime/pool/`。列序由后端的 `regimes` 数组给，前端按数组排。
 *
 * 与 `RegimePoolState` 交叉而不是 `extends`：后者对联合类型（有代 / 冷启动两支）不成立，
 * 交叉之后两支各自仍然带着 `regimes` / `cells`——冷启动时是空 `cells` + 那句 `display`，
 * **不是空白表格**。
 *
 * ## 证据是**两级去重**的（2026-10-23，在 txy 生产库上实测后定的形状）
 *
 * 响应里没有一格带 `evidence` 原文，只有两个键；原文在顶层两张表里：
 *
 *     evidence: [{key: <证据键>, results_key: <results 键>, ...其余键}]
 *     results:  [{key: <results 键>, results: [...]}]
 *     cells:    [{evidence_key: <证据键>, results_key: <results 键>, ...}]
 *
 * **为什么是两级**：只按整份 evidence 去重等于没去。生产实测 248 格里
 * `metrics` / `full_sample` / `own_sample` 是按阶段算的、每格都不同，整份去重只从 248
 * 格降到 243 种（仍是 59 MB）。真正重复的是里面那个 `results` 大数组——它按原型分桶
 * 生成、与阶段无关，占 evidence 体积的 **98.7%**，248 格里只有 **19 种**。
 * 两级去重后 1.6 MB，省 35 倍。
 *
 * **原文一个键都没少**：`evidenceOf` 把两张表按两个键拼回来，拿到的是逐字节相同的那份
 * （`results` 也回到它原来的位置）。
 */
export type RegimePool = RegimePoolState & {
  regimes: Labelled[];
  /** 去重后的证据表（不含 `results`，那一份在 `results` 表里）。顺序按首次出现。 */
  evidence: RegimeEvidenceEntry[];
  /** 去重后的 `results` 数组表。格子的 `results_key` 为空串时表示那格没有 `results`。 */
  results: RegimeResultsEntry[];
  cells: RegimePoolCell[];
};

/** 证据表的一项：`key` 是内容指纹，`results_key` 指向 `pool.results`，其余是原文。 */
export interface RegimeEvidenceEntry {
  key: string;
  results_key: string;
  [key: string]: unknown;
}

/** `results` 表的一项。 */
export interface RegimeResultsEntry {
  key: string;
  results: unknown[];
}

/** `GET api/regime/pool/cell/`：一格完整的依据（`results` 已在它原来的位置上）。 */
export interface RegimeCellEvidence {
  /** `"<策略 id>|<阶段>"`，与格子上的 `key` 同一个值。 */
  key: string;
  evidence: Record<string, unknown>;
}

/** 两张去重表建成的索引。页面按一次请求建一次，传给每一格。 */
export interface RegimeEvidenceIndex {
  evidence: Map<string, RegimeEvidenceEntry>;
  results: Map<string, unknown[]>;
}

/**
 * 取一格的证据原文（把两张表拼回来，与去重前逐字节相同）。
 *
 * `index` 由 `evidenceIndex(pool)` 建一次、传给每一格：每格各建一次 Map 就是
 * O(格数 × 表长)，而一张 61 行的表有 248 格。查不到时给 `{}` 而不是 `undefined`——
 * 格子的证据缺失是脏库的形状，页面上应当显示「证据里没有回测结果」而不是崩掉。
 */
export function evidenceOf(
  index: RegimeEvidenceIndex,
  cell: RegimePoolCell
): Record<string, unknown> | undefined {
  const entry = index.evidence.get(cell.evidence_key);
  // **`undefined` 而不是 `{}`**：这一格的依据不在表里有两件完全不同的事——
  // 列表用 `?evidence=0` 拉的时候（要按需去拉），或者库里那格真没有依据（不用拉）。
  // 前者由 `cell.evidence_key === ""` 区分（`evidence=0` 时后端给空串键）。
  if (entry === undefined) return undefined;
  // `key` / `results_key` 是传输层的键，不属于证据原文——剔除它们。
  const { key, results_key, ...evidence } = entry;
  void key;
  void results_key;
  // 空串 = 那格本来就没有 `results`（`pool._evidence` 一定带这个键，所以这里
  // 只在手工补录的脏行上出现）。有键就给回去，没有就保持缺席。
  if (cell.results_key !== "") {
    evidence.results = index.results.get(cell.results_key) ?? [];
  }
  return evidence;
}

/** `pool.evidence` / `pool.results` → 两个 Map。页面按一次请求建一次。 */
export function evidenceIndex(pool: RegimePool): RegimeEvidenceIndex {
  return {
    evidence: new Map(pool.evidence.map((entry) => [entry.key, entry])),
    results: new Map(pool.results.map((entry) => [entry.key, entry.results])),
  };
}

export interface RegimeDiffCell {
  key: string;
  strategy: { id: string; name: string };
  regime: Labelled;
  kind: "changed" | "added" | "removed";
  before: { state: Labelled; reason: Labelled; source: Labelled } | null;
  after: { state: Labelled; reason: Labelled; source: Labelled } | null;
}

/** 代际历史（`GET api/regime/pool/generations/`）。分页形状照 `apps/logging_app/views.py`。 */
export interface RegimeGenerations {
  count: number;
  page: number;
  page_size: number;
  has_next: boolean;
  results: RegimeGeneration[];
  /** 只在带 `?generation=<id>` 时出现。`changed+added+removed` 恒等于 `recorded_cells_changed`。 */
  diff?: {
    generation: number;
    previous_generation: number | null;
    previous_finished_at: string | null;
    recorded_cells_changed: number;
    counts: { changed: number; added: number; removed: number };
    cells: RegimeDiffCell[];
  };
}

/** 三张确认页之一（`GET api/regime/briefing/<kind>/`）。**重请求，按需拉，不进轮询。** */
export interface RegimeBriefing {
  kind: Labelled;
  mode: Labelled;
  /** 这一页对应的动作（与 `mode` 相反的那一档）。 */
  to_mode: Labelled;
  /** 叙述正文，后端渲染的原样文本。 */
  body: string;
  /** 这一动作会写进 `reason` 的审计串，与 `body` 同源。 */
  summary: string;
  /** 排版用的数。**不用于重拼 `body`**（重拼就是第二个说法）。 */
  details: Record<string, unknown>;
  /** 恒 `true`：这是一次重请求。 */
  heavy: boolean;
}

export type RegimeKind = "mechanism" | "event_breaker" | "regime_gate";
export type RegimeMode = "shadow" | "executing";

/** 写入口的回执。**成功与「本来就是这一档」两条路径键集恒定。** */
export interface RegimeSwitchResult {
  kind: Labelled;
  /** `false` = 本来就是这一档（200，不是 400）。 */
  changed: boolean;
  from_mode: Labelled;
  to_mode: Labelled;
  /** 操作者看到的那句话，与 CLI / slash 的回执同一批词。 */
  display: string;
  /** 真正落库的那一串（与页面同源）。 */
  reason: string;
  at: string | null;
  /** 只有 gate 方向有：这一轮对账解除/跳过了几条声明。 */
  sync?: Record<string, unknown>;
}

/** 一次重算的投递回执（`POST api/regime/pool/rebuild/`，202）。 */
export interface RegimeRebuildDispatch {
  dispatched: boolean;
  /** 这次投递的唯一凭据：`reused` 那一轮不建行，事后只认这个 id。 */
  task_id: string;
  /** 建议多久回来问一次（秒）。**建议值不是契约**——判据是状态读的 `done`。 */
  poll_after_seconds: number;
}

/**
 * 一次重算的收场（`GET api/regime/pool/rebuild/<task_id>/`）。
 *
 * `outcome` **五档**：`new_generation` / `reused` / `duplicate` / `failed` / `building`。
 * `outcome === null` 时读 `display`——那是「投递成功但 worker 没取走，或结果已过期」，
 * **不是「重算中」**。
 */
export interface RegimeRebuildStatus {
  task_id: string;
  state: string;
  done: boolean;
  outcome?: Labelled | null;
  generation?: RegimeGeneration | null;
  stale_building: { stale: boolean; minutes: number | null; threshold_minutes: number };
  /** `outcome === null` 时的那句话。 */
  display?: string;
  /** `true` = Celery 分不出「结果过期」与「还没开始」（未知 id 也回 PENDING）。 */
  state_ambiguous?: boolean;
  failure?: string;
  celery_error?: string;
  error?: string;
  cells_changed?: number | null;
  cells_total?: number | null;
  results_used?: number | null;
  candidates?: number | null;
}

/**
 * 机制读面与写入口（P5 map #40）。风格照 `liveSessionApi`。
 *
 * **读与写是两套权限**（ADR 0005）：读对所有登录用户开放，写要 `is_staff`。所以
 * `switchMode` / `rebuildPool` 调用方必须先用 `useAuth().isStaff` 决定按不按得动——
 * 这一层是界面纪律，不是安全边界（服务端仍然会挡）。
 */
export const regimeApi = {
  /** 首屏。**廉价只读**，60 秒轮询它。 */
  getOverview: () => request<RegimeOverview>("/api/regime/overview/"),

  /** 当前代（或 `?generation=<id>` 指定的一代）的全部格子。 */
  /**
   * 池化表矩阵。`evidence: false` 时**不带依据**（`?evidence=0`），一格一份都不发。
   *
   * 页面上用它：`/regime` 首屏只要矩阵（几十 KB），依据在展开某一格时用
   * `getCellEvidence` 按需拉。生产实测（从开发机到 txy 的链路约 2 KB/s、20% 丢包）
   * 带上依据的整份响应是 278 KB gzip，要 50 秒——那正是「一直转圈」的原因。
   */
  getPool: (params?: { generation?: number; evidence?: boolean }) => {
    const query = new URLSearchParams();
    if (params?.generation !== undefined) {
      query.set("generation", String(params.generation));
    }
    if (params?.evidence === false) query.set("evidence", "0");
    const qs = query.toString();
    return request<RegimePool>(`/api/regime/pool/${qs ? `?${qs}` : ""}`);
  },

  /**
   * **一格**的完整依据。`getPool({evidence:false})` 的配套。
   *
   * 与列表里带的那份**逐字节相同**（后端两条路径同源，`test_views` 有一条用例钉住），
   * 所以「从哪个入口点开」不影响审计看到的东西。
   */
  getCellEvidence: (params: {
    strategy: string;
    regime: string;
    generation?: number;
  }) => {
    const query = new URLSearchParams({
      strategy: params.strategy,
      regime: params.regime,
    });
    if (params.generation !== undefined) {
      query.set("generation", String(params.generation));
    }
    return request<RegimeCellEvidence>(
      `/api/regime/pool/cell/?${query.toString()}`
    );
  },

  /** 代际历史；带 `generation` 时附上那一代与上一代的逐格差异。 */
  getGenerations: (params?: { page?: number; page_size?: number; generation?: number }) => {
    const query = new URLSearchParams();
    if (params?.page) query.set("page", String(params.page));
    if (params?.page_size) query.set("page_size", String(params.page_size));
    if (params?.generation !== undefined) {
      query.set("generation", String(params.generation));
    }
    const qs = query.toString();
    return request<RegimeGenerations>(
      `/api/regime/pool/generations/${qs ? `?${qs}` : ""}`
    );
  },

  /**
   * 三张确认页之一。**每一次调用都会重导入全部策略模块（gate）或全量重算历史标签
   * （mechanism）**——所以只在人要做一个决定时拉一次，别放进轮询。
   */
  getBriefing: (kind: RegimeKind) =>
    request<RegimeBriefing>(`/api/regime/briefing/${kind}/`),

  /**
   * 翻一个开关的档位。**请求体只有 `{kind, mode}`**：`reason` 由服务端在同一次求值里取，
   * 客户端提交的任何 reason 一律被忽略（所以这个类型里没有那个字段——写上去就等于在说
   * 「你可以指定理由」）。
   *
   * 「本来就是这一档」是 **200 + `changed:false`**，不是错误。
   */
  switchMode: (kind: RegimeKind, mode: RegimeMode) =>
    request<RegimeSwitchResult>("/api/regime/switch/", "POST", { kind, mode }),

  /**
   * 投递一次池化表重算，**立刻返回**（202 + `task_id`），不等结果。
   *
   * 之后用 `getRebuildStatus(task_id)` 轮询：`reused` 那一轮不建代、不留审计行，
   * 代际表里看不出它发生过。
   */
  rebuildPool: () =>
    request<RegimeRebuildDispatch>("/api/regime/pool/rebuild/", "POST"),

  /** 一次重算的收场，按 `task_id` 读。纯读，轮询它没有副作用。 */
  getRebuildStatus: (taskId: string) =>
    request<RegimeRebuildStatus>(
      `/api/regime/pool/rebuild/${encodeURIComponent(taskId)}/`
    ),

  /**
   * 一条策略的启停确认页（`GET api/regime/strategies/<id>/`）。**读对所有登录用户开放。**
   *
   * 两个方向一次都给（退役 / 恢复启用），各自的 `body` 是后端渲染的正文：页面上那句
   * 「删除不删数据」「退役不停会话」的家在这里，前端不重拼。`available=false` 的那一侧
   * `body` 就是「本来就是这一档」，原样显示即可。
   *
   * 便宜（四条计数查询），但**不进轮询**：它只在人点开确认弹窗时拉一次。
   */
  getStrategyLifecycle: (strategyId: string) =>
    request<RegimeStrategyLifecyclePage>(
      `/api/regime/strategies/${encodeURIComponent(strategyId)}/`
    ),

  /**
   * **删除策略**：把这条策略退役（`Strategy.is_active` → `false`）。
   *
   * 名字里的「删除」是页面上那个按钮的说法，实际做的事由后端渲染的确认页正文写清楚
   * （不删任何数据、不停任何会话、下一轮对账解除它的停用声明之后可以重新开仓）。
   *
   * 幂等：「本来就是退役状态」是 **200 + `changed:false`**，不是错误。
   */
  retireStrategy: (strategyId: string) =>
    request<RegimeStrategyFlipResult>(
      `/api/regime/strategies/${encodeURIComponent(strategyId)}/retire/`,
      "POST"
    ),

  /**
   * **恢复启用**：把退役翻回去。它**不产生豁免**——下一轮对账若仍判它不适用会再次停它。
   *
   * 它存在的理由：`is_active=False` 的语义里写着「永不被**自动**恢复」，所以退役之后没有
   * 任何自动路径能把它放回来；只给一个单向按钮等于一次误点要去改数据库。
   */
  restoreStrategy: (strategyId: string) =>
    request<RegimeStrategyFlipResult>(
      `/api/regime/strategies/${encodeURIComponent(strategyId)}/restore/`,
      "POST"
    ),
};
