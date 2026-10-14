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
      throw new Error("Unauthorized");
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
      throw new Error(`API error: ${retryRes.status}`);
    }
    if (retryRes.status === 204) {
      return undefined as T;
    }
    return retryRes.json() as Promise<T>;
  }

  if (!res.ok) {
    throw new Error(`API error: ${res.status}`);
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

export interface LoginResponse {
  user: {
    id: number;
    email: string;
    username: string;
    is_active: boolean;
    is_frozen: boolean;
    created_at: string;
    last_login_at: string | null;
  };
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

  me: () => request<LoginResponse["user"]>("/api/auth/me/"),

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
  testnet_status: boolean;
  created_at: string;
  balance?: {
    total: string;
    available: string;
    used: string;
  };
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
  testnet_status: boolean;
  created_at: string;
  balance?: {
    total: string;
    available: string;
    used: string;
  };
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
  config: Record<string, unknown>;
  started_at: string | null;
  stopped_at: string | null;
  created_at: string;
  updated_at: string;
}

/** 会话收益统计。由后端算好，前端只显示——**门槛与口径都在后端**
 * （`sample_sufficient` 由后端按 `sample_threshold` 判定，前端不许自己比大小）。 */
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
