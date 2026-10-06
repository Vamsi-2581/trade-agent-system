"use client";

import DashboardShell from "@/components/layout/DashboardShell";
import { useState, useEffect, useCallback } from "react";
import { useAuth } from "@/context/AuthContext";
import {
  exchangeApi,
  riskApi,
  scheduledTaskApi,
  signalMonitorApi,
  memoryApi,
  channelApi,
  Memory,
  SignalMonitor,
  ScheduledTask,
  RiskConfig as RiskConfigType,
  FeishuAuthStatus,
} from "@/lib/api";

export default function SettingsPage() {
  const { logout } = useAuth();
  const [activeTab, setActiveTab] = useState("general");
  const [saved, setSaved] = useState(false);
  const [loading, setLoading] = useState(false);

  // General settings (LLM - no backend API yet)
  const [llmProvider, setLlmProvider] = useState("openai");
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("gpt-4o");
  const [fallbackProvider, setFallbackProvider] = useState("anthropic");
  const [temperature, setTemperature] = useState("0.3");
  const [maxTokens, setMaxTokens] = useState("4096");

  // Exchange settings - from backend
  const [exchange, setExchange] = useState("binance");
  const [apiKeyExchange, setApiKeyExchange] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [testnet, setTestnet] = useState(false);
  const [leverage, setLeverage] = useState("5");
  const [existingAccounts, setExistingAccounts] = useState<{
    id: string;
    exchange: string;
    label: string;
    is_active: boolean;
    created_at: string;
  }[]>([]);

  // Risk settings - from backend
  const [riskConfig, setRiskConfig] = useState<RiskConfigType | null>(null);
  const [maxPosition, setMaxPosition] = useState("35");
  const [maxDrawdown, setMaxDrawdown] = useState("8");
  const [varLimit, setVarLimit] = useState("3.0");
  const [stopLoss, setStopLoss] = useState("2.0");
  const [autoStop, setAutoStop] = useState(true);

  // Scheduled tasks - from backend
  const [scheduledTasks, setScheduledTasks] = useState<ScheduledTask[]>([]);
  const [tasksLoading, setTasksLoading] = useState(false);

  // Signal monitors - from backend
  const [signalMonitors, setSignalMonitors] = useState<SignalMonitor[]>([]);
  const [monitorsLoading, setMonitorsLoading] = useState(false);
  const [showAddMonitor, setShowAddMonitor] = useState(false);

  // Memories
  const [memories, setMemories] = useState<Memory[]>([]);
  const [memoriesLoading, setMemoriesLoading] = useState(false);

  // Feishu auth
  const [feishuStatus, setFeishuStatus] = useState<FeishuAuthStatus | null>(null);
  const [feishuLoading, setFeishuLoading] = useState(false);
  const [qrSessionId, setQrSessionId] = useState<string | null>(null);
  const [qrUrl, setQrUrl] = useState<string>("");
  const [authMode, setAuthMode] = useState<"qr" | "url">("qr");

  // Load existing exchange accounts and risk config
  const loadInitialData = useCallback(async () => {
    try {
      const [accounts, risk] = await Promise.all([
        exchangeApi.getAccounts(),
        riskApi.getConfig(),
      ]);
      setExistingAccounts(accounts);
      setRiskConfig(risk);
      if (risk.max_position_pct) setMaxPosition(String(risk.max_position_pct));
      if (risk.max_drawdown_pct) setMaxDrawdown(String(risk.max_drawdown_pct));
      if (risk.var_limit_pct) setVarLimit(String(risk.var_limit_pct));
      if (risk.stop_loss_pct) setStopLoss(String(risk.stop_loss_pct));
      if (risk.auto_stop !== undefined) setAutoStop(risk.auto_stop);
    } catch {
      // API not available or error - use defaults
    }
  }, []);

  const loadScheduledTasks = useCallback(async () => {
    setTasksLoading(true);
    try {
      const res = await scheduledTaskApi.list();
      setScheduledTasks(res.tasks);
    } catch {
      // API not available
    } finally {
      setTasksLoading(false);
    }
  }, []);

  const loadSignalMonitors = useCallback(async () => {
    setMonitorsLoading(true);
    try {
      const res = await signalMonitorApi.list();
      setSignalMonitors(res.data);
    } catch {
      // API not available
    } finally {
      setMonitorsLoading(false);
    }
  }, []);

  const loadMemories = useCallback(async () => {
    setMemoriesLoading(true);
    try {
      const res = await memoryApi.list();
      setMemories(res);
    } catch {
      // API not available
    } finally {
      setMemoriesLoading(false);
    }
  }, []);

  const loadFeishuStatus = useCallback(async () => {
    try {
      const res = await channelApi.feishuStatus();
      setFeishuStatus(res);
    } catch {
      // API not available
    }
  }, []);

  const handleFeishuRevoke = async () => {
    if (!confirm("确定要撤销飞书授权吗？此操作不可撤销。")) return;
    try {
      await channelApi.feishuRevoke();
      setFeishuStatus(null);
      loadFeishuStatus();
    } catch (err) {
      alert(`撤销失败: ${err instanceof Error ? err.message : "未知错误"}`);
    }
  };

  const handleFeishuRefresh = async () => {
    try {
      await channelApi.feishuRefresh();
      alert("Token 刷新成功");
      loadFeishuStatus();
    } catch (err) {
      alert(`刷新失败: ${err instanceof Error ? err.message : "未知错误"}`);
    }
  };

  const handleStartQR = async () => {
    setFeishuLoading(true);
    try {
      const res = await channelApi.feishuQR();
      setQrSessionId(res.session_id);
      setQrUrl(res.qr_url);
    } catch (err) {
      alert(`获取二维码失败: ${err instanceof Error ? err.message : "未知错误"}`);
    } finally {
      setFeishuLoading(false);
    }
  };

  const handleStartUrl = async () => {
    setFeishuLoading(true);
    try {
      const res = await channelApi.feishuUrl();
      window.open(res.authorize_url, "_blank");
    } catch (err) {
      alert(`获取授权链接失败: ${err instanceof Error ? err.message : "未知错误"}`);
    } finally {
      setFeishuLoading(false);
    }
  };

  const handleDeleteMemory = useCallback(async (id: string) => {
    if (!confirm("确定要删除该记忆吗？此操作不可撤销。")) {
      return;
    }
    try {
      await memoryApi.delete(id);
      await loadMemories();
    } catch (err) {
      alert(`删除失败: ${err instanceof Error ? err.message : "未知错误"}`);
    }
  }, [loadMemories]);

  // Load scheduled tasks when tab is activated
  useEffect(() => {
    if (activeTab === "scheduled") {
      loadScheduledTasks();
    }
  }, [activeTab, loadScheduledTasks]);

  // Load signal monitors when tab is activated
  useEffect(() => {
    if (activeTab === "signals") {
      loadSignalMonitors();
    }
  }, [activeTab, loadSignalMonitors]);

  // Load memories when tab is activated
  useEffect(() => {
    if (activeTab === "memory") {
      loadMemories();
    }
  }, [activeTab, loadMemories]);

  // Load feishu status when tab is activated
  useEffect(() => {
    if (activeTab === "feishu") {
      loadFeishuStatus();
    }
  }, [activeTab, loadFeishuStatus]);

  // Poll QR status when QR auth is in progress
  useEffect(() => {
    if (!qrSessionId || feishuStatus?.authorized) return;

    const interval = setInterval(async () => {
      try {
        const res = await channelApi.feishuQRStatus(qrSessionId);
        if (res.status === "completed") {
          clearInterval(interval);
          setQrSessionId(null);
          setQrUrl("");
          await loadFeishuStatus();
        } else if (res.status === "expired" || res.status === "failed") {
          clearInterval(interval);
          setQrSessionId(null);
        }
      } catch {
        // ignore polling errors
      }
    }, 3000);

    return () => clearInterval(interval);
  }, [qrSessionId, feishuStatus?.authorized, loadFeishuStatus]);

  useEffect(() => {
    loadInitialData();
  }, [loadInitialData]);

  const showSaved = () => {
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  };

  const handleExchangeSave = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    try {
      await exchangeApi.createAccount({
        exchange,
        label: `${exchange} ${testnet ? "(Testnet)" : ""}`,
        api_key: apiKeyExchange,
        api_secret: apiSecret,
        testnet,
        leverage: parseInt(leverage, 10),
      });
      setApiKeyExchange("");
      setApiSecret("");
      showSaved();
      loadInitialData();
    } catch (err) {
      alert(`保存失败: ${err instanceof Error ? err.message : "未知错误"}`);
    } finally {
      setLoading(false);
    }
  };

  const handleRiskSave = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    try {
      await riskApi.updateConfig({
        max_position_pct: parseFloat(maxPosition),
        max_drawdown_pct: parseFloat(maxDrawdown),
        var_limit_pct: parseFloat(varLimit),
        stop_loss_pct: parseFloat(stopLoss),
        auto_stop: autoStop,
      });
      showSaved();
      loadInitialData();
    } catch (err) {
      alert(`保存失败: ${err instanceof Error ? err.message : "未知错误"}`);
    } finally {
      setLoading(false);
    }
  };

  const handleDeleteAccount = async (id: string) => {
    if (!confirm("确定要删除该交易所账户吗？此操作不可撤销。")) {
      return;
    }
    try {
      await exchangeApi.deleteAccount(id);
      loadInitialData();
    } catch (err) {
      alert(`删除失败: ${err instanceof Error ? err.message : "未知错误"}`);
    }
  };

  const handleToggleMonitor = async (id: string, currentlyActive: boolean) => {
    try {
      await signalMonitorApi.toggle(id, !currentlyActive);
      loadSignalMonitors();
    } catch (err) {
      alert(`操作失败: ${err instanceof Error ? err.message : "未知错误"}`);
    }
  };

  const handleDeleteMonitor = async (id: string) => {
    if (!confirm("确定要删除该信号监控吗？此操作不可撤销。")) {
      return;
    }
    try {
      await signalMonitorApi.delete(id);
      loadSignalMonitors();
    } catch (err) {
      alert(`删除失败: ${err instanceof Error ? err.message : "未知错误"}`);
    }
  };

  const TABS = [
    { key: "general", label: "通用" },
    { key: "exchange", label: "交易所" },
    { key: "risk", label: "风控" },
    { key: "scheduled", label: "定时任务" },
    { key: "signals", label: "信号监控" },
    { key: "memory", label: "记忆" },
    { key: "feishu", label: "飞书授权" },
    { key: "account", label: "账户" },
  ];

  const inputClass =
    "w-full bg-bg2 border border-[rgba(255,255,255,0.07)] rounded-md px-3 py-2 text-xs text-text outline-none focus:border-green transition-colors";
  const labelClass =
    "text-[10px] text-text3 uppercase tracking-wider font-semibold mb-1 block";

  return (
    <DashboardShell>
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-sm font-bold">系统配置</h1>
        {saved && (
          <div className="text-xs text-green font-semibold bg-green-dim px-3 py-1.5 rounded-md">
            已保存
          </div>
        )}
      </div>

      {/* Tabs */}
      <div className="flex gap-1.5">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`px-4 py-1.5 rounded-md text-xs font-semibold cursor-pointer transition-all border ${
              activeTab === tab.key
                ? "bg-green-dim border-green/20 text-green"
                : "bg-bg2 border-[rgba(255,255,255,0.07)] text-text3 hover:text-text"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Content */}
      <div className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-xl overflow-hidden">
        {activeTab === "general" && (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              showSaved();
            }}
            className="p-5 flex flex-col gap-4"
          >
            <div className="text-xs font-semibold text-text mb-1">LLM 配置</div>

            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className={labelClass}>主 LLM 提供商</label>
                <select
                  value={llmProvider}
                  onChange={(e) => setLlmProvider(e.target.value)}
                  className={inputClass}
                >
                  <option value="openai">OpenAI</option>
                  <option value="anthropic">Anthropic</option>
                </select>
              </div>
              <div>
                <label className={labelClass}>模型</label>
                <select
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  className={inputClass}
                >
                  <option value="gpt-4o">GPT-4o</option>
                  <option value="gpt-4o-mini">GPT-4o-mini</option>
                  <option value="o1">O1</option>
                </select>
              </div>
            </div>

            <div>
              <label className={labelClass}>API Key</label>
              <input
                type="password"
                autoComplete="off"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder="sk-..."
                className={inputClass}
              />
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className={labelClass}>降级 LLM</label>
                <select
                  value={fallbackProvider}
                  onChange={(e) => setFallbackProvider(e.target.value)}
                  className={inputClass}
                >
                  <option value="anthropic">Anthropic Claude</option>
                  <option value="none">无</option>
                </select>
              </div>
              <div>
                <label className={labelClass}>Temperature</label>
                <input
                  type="number"
                  step="0.1"
                  min="0"
                  max="1"
                  value={temperature}
                  onChange={(e) => setTemperature(e.target.value)}
                  className={inputClass}
                />
              </div>
            </div>

            <div>
              <label className={labelClass}>Max Tokens</label>
              <input
                type="number"
                value={maxTokens}
                onChange={(e) => setMaxTokens(e.target.value)}
                className={inputClass}
              />
            </div>
          </form>
        )}

        {activeTab === "exchange" && (
          <div className="p-5 flex flex-col gap-4">
            <div className="text-xs font-semibold text-text mb-1">
              交易所连接
            </div>

            {/* Existing accounts */}
            <div className="flex flex-col gap-2">
              <div className="text-[10px] text-text3 uppercase tracking-wider font-semibold">
                已连接账户 ({existingAccounts.length})
              </div>
              {existingAccounts.length === 0 ? (
                <div className="text-center py-6 text-[11px] text-text3 bg-bg2 border border-[rgba(255,255,255,0.07)] rounded-lg">
                  暂无已连接的交易所账户，请在下方添加
                </div>
              ) : (
                existingAccounts.map((acc) => (
                  <div
                    key={acc.id}
                    className="flex items-center justify-between bg-bg2 border border-[rgba(255,255,255,0.07)] rounded-lg px-3 py-2.5"
                  >
                    <div className="flex items-center gap-3">
                      <div className="flex flex-col">
                        <div className="flex items-center gap-2">
                          <span className="text-xs font-semibold text-text capitalize">
                            {acc.exchange}
                          </span>
                          <span
                            className={`text-[9px] px-1.5 py-0.5 rounded-sm font-semibold ${
                              acc.is_active
                                ? "bg-green-dim text-green"
                                : "bg-red-dim text-red"
                            }`}
                          >
                            {acc.is_active ? "活跃" : "已停用"}
                          </span>
                        </div>
                        <span className="text-[10px] text-text3">
                          {acc.label}
                        </span>
                      </div>
                    </div>
                    <div className="flex items-center gap-3">
                      <span className="text-[9px] text-text3">
                        {new Date(acc.created_at).toLocaleDateString("zh-CN")}
                      </span>
                      <button
                        onClick={() => handleDeleteAccount(acc.id)}
                        className="text-[10px] text-red hover:opacity-80 cursor-pointer"
                      >
                        删除
                      </button>
                    </div>
                  </div>
                ))
              )}
            </div>

            <div className="border-t border-[rgba(255,255,255,0.07)] pt-4 mt-1">
              <div className="text-[10px] text-text3 uppercase tracking-wider font-semibold mb-3">
                添加新账户
              </div>
              <form
                onSubmit={handleExchangeSave}
                className="flex flex-col gap-4"
              >
                <div>
                  <label className={labelClass}>交易所</label>
                  <select
                    value={exchange}
                    onChange={(e) => setExchange(e.target.value)}
                    className={inputClass}
                  >
                    <option value="binance">Binance</option>
                    <option value="okx">OKX</option>
                    <option value="bybit">Bybit</option>
                  </select>
                </div>

                <div>
                  <label className={labelClass}>API Key</label>
                  <input
                    type="password"
                    autoComplete="off"
                    value={apiKeyExchange}
                    onChange={(e) => setApiKeyExchange(e.target.value)}
                    placeholder="交易所 API Key"
                    className={inputClass}
                  />
                </div>

                <div>
                  <label className={labelClass}>API Secret</label>
                  <input
                    type="password"
                    autoComplete="off"
                    value={apiSecret}
                    onChange={(e) => setApiSecret(e.target.value)}
                    placeholder="交易所 API Secret"
                    className={inputClass}
                  />
                </div>

                <div className="flex items-center justify-between">
                  <div>
                    <div className="text-xs text-text font-semibold">
                      测试网模式
                    </div>
                    <div className="text-[10px] text-text3">
                      使用模拟环境进行交易测试
                    </div>
                  </div>
                  <label className="relative w-8 h-[18px]">
                    <input
                      type="checkbox"
                      className="hidden"
                      checked={testnet}
                      onChange={() => setTestnet(!testnet)}
                    />
                    <div
                      className={`absolute inset-0 rounded-[9px] cursor-pointer transition-all before:content-[''] before:absolute before:w-3 before:h-3 before:left-0.5 before:top-0.5 before:rounded-full before:bg-text3 before:transition-all before:duration-200 ${
                        testnet
                          ? "!bg-green-dim !border-green before:!translate-x-[14px] before:!bg-green"
                          : "bg-bg border border-[rgba(255,255,255,0.12)]"
                      }`}
                      style={
                        testnet ? { borderColor: "var(--color-green)" } : {}
                      }
                    />
                  </label>
                </div>

                <div>
                  <label className={labelClass}>默认杠杆 (x)</label>
                  <input
                    type="number"
                    value={leverage}
                    onChange={(e) => setLeverage(e.target.value)}
                    className={inputClass}
                  />
                </div>

                <div className="flex items-center justify-end gap-3">
                  {saved && (
                    <span className="text-xs text-green font-semibold">
                      已添加成功
                    </span>
                  )}
                  <button
                    type="submit"
                    disabled={loading || !apiKeyExchange || !apiSecret}
                    className="bg-green text-black text-xs font-semibold px-5 py-2 rounded-lg hover:opacity-85 transition-all cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    {loading ? "保存中..." : "添加交易所账户"}
                  </button>
                </div>
              </form>
            </div>
          </div>
        )}

        {activeTab === "risk" && (
          <form onSubmit={handleRiskSave} className="p-5 flex flex-col gap-4">
            <div className="text-xs font-semibold text-text mb-1">
              风控参数
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className={labelClass}>最大仓位集中度 (%)</label>
                <input
                  type="number"
                  value={maxPosition}
                  onChange={(e) => setMaxPosition(e.target.value)}
                  className={inputClass}
                />
              </div>
              <div>
                <label className={labelClass}>最大回撤限额 (%)</label>
                <input
                  type="number"
                  value={maxDrawdown}
                  onChange={(e) => setMaxDrawdown(e.target.value)}
                  className={inputClass}
                />
              </div>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className={labelClass}>日 VaR 限额 (%)</label>
                <input
                  type="number"
                  step="0.1"
                  value={varLimit}
                  onChange={(e) => setVarLimit(e.target.value)}
                  className={inputClass}
                />
              </div>
              <div>
                <label className={labelClass}>止损阈值 (%)</label>
                <input
                  type="number"
                  step="0.1"
                  value={stopLoss}
                  onChange={(e) => setStopLoss(e.target.value)}
                  className={inputClass}
                />
              </div>
            </div>

            <div className="flex items-center justify-between">
              <div>
                <div className="text-xs text-text font-semibold">自动风控</div>
                <div className="text-[10px] text-text3">
                  触发风控条件时自动平仓
                </div>
              </div>
              <label className="relative w-8 h-[18px]">
                <input
                  type="checkbox"
                  className="hidden"
                  checked={autoStop}
                  onChange={() => setAutoStop(!autoStop)}
                />
                <div
                  className={`absolute inset-0 rounded-[9px] cursor-pointer transition-all before:content-[''] before:absolute before:w-3 before:h-3 before:left-0.5 before:top-0.5 before:rounded-full before:bg-text3 before:transition-all before:duration-200 ${
                    autoStop
                      ? "!bg-green-dim !border-green before:!translate-x-[14px] before:!bg-green"
                      : "bg-bg border border-[rgba(255,255,255,0.12)]"
                  }`}
                  style={autoStop ? { borderColor: "var(--color-green)" } : {}}
                />
              </label>
            </div>

            {/* Current risk status - show live data from backend */}
            {riskConfig && (
              <div className="bg-bg2 border border-[rgba(255,255,255,0.07)] rounded-lg p-4 mt-2">
                <div className="text-[10px] text-text3 uppercase tracking-wider font-semibold mb-2">
                  当前风控状态
                </div>
                <div className="grid grid-cols-4 gap-3">
                  <div>
                    <div className="text-[9px] text-text3">仓位集中度</div>
                    <div className="font-mono text-sm font-semibold text-amber">
                      {riskConfig.max_position_pct.toFixed(1)}%
                    </div>
                  </div>
                  <div>
                    <div className="text-[9px] text-text3">最大回撤</div>
                    <div className="font-mono text-sm font-semibold text-amber">
                      {riskConfig.max_drawdown_pct.toFixed(1)}%
                    </div>
                  </div>
                  <div>
                    <div className="text-[9px] text-text3">日VaR</div>
                    <div className="font-mono text-sm font-semibold text-amber">
                      {riskConfig.var_limit_pct.toFixed(1)}%
                    </div>
                  </div>
                  <div>
                    <div className="text-[9px] text-text3">自动风控</div>
                    <div
                      className={`font-mono text-sm font-semibold ${
                        riskConfig.auto_stop ? "text-green" : "text-red"
                      }`}
                    >
                      {riskConfig.auto_stop ? "开启" : "关闭"}
                    </div>
                  </div>
                </div>
              </div>
            )}

            <div className="flex justify-end">
              <button
                type="submit"
                disabled={loading}
                className="bg-green text-black text-xs font-semibold px-5 py-2 rounded-lg hover:opacity-85 transition-all cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {loading ? "保存中..." : "保存风控配置"}
              </button>
            </div>
          </form>
        )}

        {activeTab === "account" && (
          <div className="p-5 flex flex-col gap-4">
            <div className="text-xs font-semibold text-text mb-1">账户管理</div>

            <div className="bg-bg2 border border-[rgba(255,255,255,0.07)] rounded-lg p-4">
              <div className="flex items-center gap-3">
                <div className="w-10 h-10 bg-gradient-to-br from-purple/30 to-blue/30 rounded-full flex items-center justify-center text-sm font-bold">
                  A
                </div>
                <div>
                  <div className="text-xs font-semibold text-text">Admin</div>
                  <div className="text-[10px] text-text3">超级管理员</div>
                </div>
              </div>
            </div>

            <div className="flex flex-col gap-2">
              <button className="w-full bg-bg2 border border-[rgba(255,255,255,0.07)] rounded-lg px-4 py-2.5 text-xs text-text hover:bg-bg3 transition-colors text-left cursor-pointer">
                修改密码
              </button>
              <button className="w-full bg-bg2 border border-[rgba(255,255,255,0.07)] rounded-lg px-4 py-2.5 text-xs text-text hover:bg-bg3 transition-colors text-left cursor-pointer">
                管理 API Keys
              </button>
              <button className="w-full bg-bg2 border border-[rgba(255,255,255,0.07)] rounded-lg px-4 py-2.5 text-xs text-text hover:bg-bg3 transition-colors text-left cursor-pointer">
                导出配置
              </button>
              <button
                onClick={logout}
                className="w-full bg-red-dim border border-red/20 rounded-lg px-4 py-2.5 text-xs text-red hover:bg-red/20 transition-colors text-left cursor-pointer"
              >
                退出登录
              </button>
            </div>
          </div>
        )}

        {activeTab === "scheduled" && (
          <div className="p-5 flex flex-col gap-4">
            <div className="text-xs font-semibold text-text mb-1">
              定时任务
            </div>

            {tasksLoading ? (
              <div className="flex items-center justify-center py-12 text-text3 text-xs">
                正在加载...
              </div>
            ) : scheduledTasks.length === 0 ? (
              <div className="text-center py-12 text-[11px] text-text3 bg-bg2 border border-[rgba(255,255,255,0.07)] rounded-lg">
                暂无定时任务
              </div>
            ) : (
              <div className="flex flex-col gap-2">
                {scheduledTasks.map((task) => (
                  <div
                    key={task.source === "database" ? task.id : task.name}
                    className="bg-bg2 border border-[rgba(255,255,255,0.07)] rounded-lg px-4 py-3"
                  >
                    <div className="flex items-center justify-between mb-2">
                      <div className="flex items-center gap-2">
                        <span className="text-xs font-semibold text-text">
                          {task.name}
                        </span>
                        <span
                          className={`text-[9px] px-1.5 py-0.5 rounded-sm font-semibold ${
                            task.source === "database"
                              ? task.enabled
                                ? "bg-green-dim text-green"
                                : "bg-text3/20 text-text3"
                              : "bg-blue-dim text-blue"
                          }`}
                        >
                          {task.source === "database" ? (task.enabled ? "动态·启用" : "动态·禁用") : "静态"}
                        </span>
                      </div>
                      <div className="text-[9px] text-text3 font-mono">
                        {task.schedule}
                      </div>
                    </div>
                    <div className="grid grid-cols-4 gap-3 text-[9px]">
                      <div>
                        <div className="text-text3">目标 Agent</div>
                        <div className="font-mono text-xs text-text2">
                          {task.agent_name}
                        </div>
                      </div>
                      <div>
                        <div className="text-text3">已执行次数</div>
                        <div className="font-mono text-xs text-text2">
                          {task.total_run_count}
                        </div>
                      </div>
                      <div>
                        <div className="text-text3">上次执行</div>
                        <div className="font-mono text-xs text-text2">
                          {task.last_run_at
                            ? new Date(task.last_run_at).toLocaleString("zh-CN")
                            : "未执行"}
                        </div>
                      </div>
                      <div>
                        <div className="text-text3">开始时间</div>
                        <div className="font-mono text-xs text-text2">
                          {task.start_time
                            ? new Date(task.start_time).toLocaleString("zh-CN")
                            : "—"}
                        </div>
                      </div>
                    </div>
                    {task.message && (
                      <div className="mt-2 pt-2 border-t border-[rgba(255,255,255,0.05)]">
                        <div className="text-[9px] text-text3 mb-0.5">任务内容</div>
                        <div className="text-[10px] text-text2 line-clamp-2">
                          {task.message}
                        </div>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Signal Monitor Tab */}
        {activeTab === "signals" && (
          <div className="p-5 flex flex-col gap-4">
            <div className="flex items-center justify-between">
              <div className="text-xs font-semibold text-text">信号监控</div>
              <button
                onClick={() => setShowAddMonitor(true)}
                className="bg-green text-black text-xs font-semibold px-3 py-1.5 rounded-md hover:opacity-85 transition-all cursor-pointer"
              >
                + 添加监控
              </button>
            </div>

            {/* Add Monitor Form */}
            {showAddMonitor && <AddMonitorForm onClose={() => setShowAddMonitor(false)} onAdded={loadSignalMonitors} />}

            {/* Monitor List */}
            {monitorsLoading ? (
              <div className="flex items-center justify-center py-12 text-text3 text-xs">
                正在加载...
              </div>
            ) : signalMonitors.length === 0 ? (
              <div className="text-center py-12 text-[11px] text-text3 bg-bg2 border border-[rgba(255,255,255,0.07)] rounded-lg">
                暂无信号监控
              </div>
            ) : (
              <div className="flex flex-col gap-2">
                {signalMonitors.map((monitor) => (
                  <div
                    key={monitor.id}
                    className="bg-bg2 border border-[rgba(255,255,255,0.07)] rounded-lg px-4 py-3"
                  >
                    <div className="flex items-center justify-between mb-2">
                      <div className="flex items-center gap-2">
                        <span className="text-xs font-semibold text-text">{monitor.name}</span>
                        <span
                          className={`text-[9px] px-1.5 py-0.5 rounded-sm font-semibold ${
                            monitor.status === "active"
                              ? "bg-green-dim text-green"
                              : monitor.status === "disabled"
                              ? "bg-text3/20 text-text3"
                              : "bg-amber-dim text-amber"
                          }`}
                        >
                          {monitor.status === "active" ? "活跃" : monitor.status === "disabled" ? "已禁用" : monitor.status}
                        </span>
                      </div>
                      <div className="flex items-center gap-3">
                        <span className="text-[9px] text-text3 font-mono">
                          {monitor.symbol} {monitor.interval}
                        </span>
                        <button
                          onClick={() => handleToggleMonitor(monitor.id, monitor.status === "active")}
                          className="text-[10px] text-blue hover:opacity-80 cursor-pointer"
                        >
                          {monitor.status === "active" ? "禁用" : "启用"}
                        </button>
                        <button
                          onClick={() => handleDeleteMonitor(monitor.id)}
                          className="text-[10px] text-red hover:opacity-80 cursor-pointer"
                        >
                          删除
                        </button>
                      </div>
                    </div>
                    <div className="grid grid-cols-4 gap-3 text-[9px]">
                      <div>
                        <div className="text-text3">指标类型</div>
                        <div className="font-mono text-xs text-text2 uppercase">{monitor.indicator_type}</div>
                      </div>
                      <div>
                        <div className="text-text3">触发类型</div>
                        <div className="font-mono text-xs text-text2">
                          {monitor.trigger_type === "once" ? "单次" : "持续"}
                        </div>
                      </div>
                      <div>
                        <div className="text-text3">触发次数</div>
                        <div className="font-mono text-xs text-text2">{monitor.trigger_count}</div>
                      </div>
                      <div>
                        <div className="text-text3">最后触发</div>
                        <div className="font-mono text-xs text-text2">
                          {monitor.last_triggered_at
                            ? new Date(monitor.last_triggered_at).toLocaleString("zh-CN")
                            : "—"}
                        </div>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {activeTab === "memory" && (
          <MemoryList
            memories={memories}
            loading={memoriesLoading}
            onDelete={handleDeleteMemory}
          />
        )}

        {activeTab === "feishu" && (
          <div className="p-5 flex flex-col gap-4">
            <div className="text-xs font-semibold text-text mb-1">飞书授权</div>

            {/* Auth status */}
            {feishuStatus?.authorized ? (
              <>
                <div className="bg-green/5 border border-green/20 rounded-lg p-4">
                  <div className="flex items-center gap-2 mb-3">
                    <div className="w-2 h-2 rounded-full bg-green" />
                    <span className="text-xs font-semibold text-green">已授权</span>
                  </div>
                  <div className="grid grid-cols-3 gap-4 text-[10px]">
                    <div>
                      <div className="text-text3">Open ID</div>
                      <div className="font-mono text-xs text-text2">{feishuStatus.open_id}</div>
                    </div>
                    <div>
                      <div className="text-text3">过期时间</div>
                      <div className="font-mono text-xs text-text2">
                        {feishuStatus.expires_at
                          ? new Date(feishuStatus.expires_at).toLocaleString("zh-CN")
                          : "—"}
                      </div>
                    </div>
                    <div>
                      <div className="text-text3">授权时间</div>
                      <div className="font-mono text-xs text-text2">
                        {feishuStatus.created_at
                          ? new Date(feishuStatus.created_at).toLocaleString("zh-CN")
                          : "—"}
                      </div>
                    </div>
                  </div>
                </div>

                <div className="flex gap-3">
                  <button
                    onClick={handleFeishuRefresh}
                    className="bg-blue text-black text-xs font-semibold px-4 py-2 rounded-lg hover:opacity-85 transition-all cursor-pointer"
                  >
                    刷新 Token
                  </button>
                  <button
                    onClick={handleFeishuRevoke}
                    className="bg-red-dim border border-red/20 text-red text-xs font-semibold px-4 py-2 rounded-lg hover:bg-red/20 transition-all cursor-pointer"
                  >
                    撤销授权
                  </button>
                </div>
              </>
            ) : (
              <>
                {/* Auth mode selector */}
                <div className="flex gap-2">
                  <button
                    onClick={() => setAuthMode("qr")}
                    className={`px-3 py-1.5 rounded-md text-xs font-semibold cursor-pointer transition-all border ${
                      authMode === "qr"
                        ? "bg-green-dim border-green/20 text-green"
                        : "bg-bg2 border-[rgba(255,255,255,0.07)] text-text3 hover:text-text"
                    }`}
                  >
                    扫码授权
                  </button>
                  <button
                    onClick={() => setAuthMode("url")}
                    className={`px-3 py-1.5 rounded-md text-xs font-semibold cursor-pointer transition-all border ${
                      authMode === "url"
                        ? "bg-green-dim border-green/20 text-green"
                        : "bg-bg2 border-[rgba(255,255,255,0.07)] text-text3 hover:text-text"
                    }`}
                  >
                    链接授权
                  </button>
                </div>

                {/* QR mode */}
                {authMode === "qr" && (
                  <div className="flex flex-col items-center gap-4 py-8">
                    {qrUrl ? (
                      <>
                        <div className="bg-white rounded-xl p-4 w-64 h-64 flex items-center justify-center">
                          <img
                            src={`https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=${encodeURIComponent(qrUrl)}`}
                            alt="飞书授权二维码"
                            className="w-48 h-48"
                          />
                        </div>
                        <div className="text-xs text-text3">请使用飞书 App 扫码授权</div>
                      </>
                    ) : (
                      <>
                        <div className="w-16 h-16 bg-bg2 border border-[rgba(255,255,255,0.07)] rounded-xl flex items-center justify-center text-2xl">
                          📱
                        </div>
                        <div className="text-xs text-text3 text-center max-w-xs">
                          点击按钮获取二维码，然后用飞书 App 扫码完成授权
                        </div>
                      </>
                    )}
                    <button
                      onClick={handleStartQR}
                      disabled={feishuLoading || !!qrSessionId}
                      className="bg-green text-black text-xs font-semibold px-5 py-2 rounded-lg hover:opacity-85 transition-all cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      {feishuLoading ? "加载中..." : qrSessionId ? "等待扫码..." : "获取二维码"}
                    </button>
                  </div>
                )}

                {/* URL mode */}
                {authMode === "url" && (
                  <div className="flex flex-col items-center gap-4 py-8">
                    <div className="w-16 h-16 bg-bg2 border border-[rgba(255,255,255,0.07)] rounded-xl flex items-center justify-center text-2xl">
                      🔗
                    </div>
                    <div className="text-xs text-text3 text-center max-w-xs">
                      点击按钮将打开飞书授权页面，在新窗口中完成授权
                    </div>
                    <button
                      onClick={handleStartUrl}
                      disabled={feishuLoading}
                      className="bg-green text-black text-xs font-semibold px-5 py-2 rounded-lg hover:opacity-85 transition-all cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      {feishuLoading ? "跳转中..." : "打开授权页面"}
                    </button>
                  </div>
                )}
              </>
            )}
          </div>
        )}

        {/* Save button for general tab only */}
        {activeTab === "general" && (
          <div className="px-5 py-3 border-t border-[rgba(255,255,255,0.07)] flex justify-end">
            <button
              onClick={() => showSaved()}
              className="bg-green text-black text-xs font-semibold px-5 py-2 rounded-lg hover:opacity-85 transition-all cursor-pointer"
            >
              保存配置
            </button>
          </div>
        )}
      </div>
    </DashboardShell>
  );
}

interface AddMonitorFormProps {
  onClose: () => void;
  onAdded: () => void;
}

function AddMonitorForm({ onClose, onAdded }: AddMonitorFormProps) {
  const [name, setName] = useState("");
  const [symbol, setSymbol] = useState("");
  const [indicatorType, setIndicatorType] = useState("rsi");
  const [condition, setCondition] = useState("");
  const [threshold, setThreshold] = useState("30");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name || !symbol || !condition) return;

    setLoading(true);
    try {
      const operatorMap: Record<string, string> = {
        below: "lt",
        above: "gt",
        cross_up: "gte_cross",
        cross_down: "lte_cross",
      };

      await signalMonitorApi.create({
        name,
        symbol: symbol.toUpperCase(),
        indicator_type: indicatorType,
        indicator_params: {},
        condition: {
          operator: operatorMap[condition] || "lt",
          left: { field: "" },
          right: { value: parseFloat(threshold) },
        },
      });
      onAdded();
      onClose();
    } catch (err) {
      alert(`创建失败: ${err instanceof Error ? err.message : "未知错误"}`);
    } finally {
      setLoading(false);
    }
  };

  const inputClass =
    "w-full bg-bg border border-[rgba(255,255,255,0.12)] rounded-md px-3 py-2 text-xs text-text outline-none focus:border-green transition-colors";
  const labelClass =
    "text-[10px] text-text3 uppercase tracking-wider font-semibold mb-1 block";

  return (
    <div className="bg-bg2 border border-[rgba(255,255,255,0.1)] rounded-lg p-4">
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <div className="text-xs font-semibold text-text">添加信号监控</div>
          <button
            type="button"
            onClick={onClose}
            className="text-[10px] text-text3 hover:text-text cursor-pointer"
          >
            取消
          </button>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className={labelClass}>名称</label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="如: BTC RSI 超卖"
              className={inputClass}
              required
            />
          </div>
          <div>
            <label className={labelClass}>交易对</label>
            <input
              type="text"
              value={symbol}
              onChange={(e) => setSymbol(e.target.value)}
              placeholder="BTCUSDT"
              className={inputClass}
              required
            />
          </div>
        </div>

        <div className="grid grid-cols-3 gap-3">
          <div>
            <label className={labelClass}>指标</label>
            <select
              value={indicatorType}
              onChange={(e) => setIndicatorType(e.target.value)}
              className={inputClass}
            >
              <option value="rsi">RSI</option>
              <option value="macd">MACD</option>
              <option value="ema">EMA</option>
              <option value="ma">MA</option>
            </select>
          </div>
          <div>
            <label className={labelClass}>条件</label>
            <select
              value={condition}
              onChange={(e) => setCondition(e.target.value)}
              className={inputClass}
              required
            >
              <option value="">选择条件</option>
              <option value="below">低于</option>
              <option value="above">高于</option>
              <option value="cross_up">向上突破</option>
              <option value="cross_down">向下突破</option>
            </select>
          </div>
          <div>
            <label className={labelClass}>阈值</label>
            <input
              type="number"
              step="0.1"
              value={threshold}
              onChange={(e) => setThreshold(e.target.value)}
              className={inputClass}
              required
            />
          </div>
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-2 text-xs text-text hover:text-text2 cursor-pointer"
          >
            取消
          </button>
          <button
            type="submit"
            disabled={loading || !name || !symbol || !condition}
            className="bg-green text-black text-xs font-semibold px-4 py-2 rounded-md hover:opacity-85 transition-all cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {loading ? "创建中..." : "创建监控"}
          </button>
        </div>
      </form>
    </div>
  );
}

// ── Memory utility functions ──

function memoryAgentTypeColor(type: string): string {
  switch (type) {
    case "user":
      return "bg-green-dim text-green";
    case "strategy":
      return "bg-blue-dim text-blue";
    case "risk":
      return "bg-amber-dim text-amber";
    default:
      return "bg-text3/20 text-text3";
  }
}

function memoryTruncatedContent(content: string): string {
  const lines = content.split("\n").slice(0, 3).join("\n");
  return lines.length > 200 ? lines.slice(0, 200) + "..." : lines;
}

function memoryTitle(content: string): string {
  const firstLine = content.split("\n")[0];
  return firstLine.length > 80 ? firstLine.slice(0, 80) + "..." : firstLine;
}

function memoryFormattedDate(dateStr: string): string {
  if (!dateStr) return "—";
  return new Date(dateStr).toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

interface MemoryListProps {
  memories: Memory[];
  loading: boolean;
  onDelete: (id: string) => void;
}

function MemoryList({ memories, loading, onDelete }: MemoryListProps) {
  if (loading) {
    return (
      <div className="p-5 flex flex-col gap-4">
        <div className="text-xs font-semibold text-text">记忆</div>
        <div className="flex items-center justify-center py-12 text-text3 text-xs">
          正在加载...
        </div>
      </div>
    );
  }

  if (memories.length === 0) {
    return (
      <div className="p-5 flex flex-col gap-4">
        <div className="text-xs font-semibold text-text">记忆</div>
        <div className="text-center py-12 text-[11px] text-text3 bg-bg2 border border-[rgba(255,255,255,0.07)] rounded-lg">
          暂无记忆
        </div>
      </div>
    );
  }

  return (
    <div className="p-5 flex flex-col gap-4">
      <div className="text-xs font-semibold text-text">记忆 ({memories.length})</div>
      <div className="flex flex-col gap-2">
        {memories.map((mem) => (
          <MemoryCard
            key={mem.id}
            memory={mem}
            onDelete={onDelete}
          />
        ))}
      </div>
    </div>
  );
}

interface MemoryCardProps {
  memory: Memory;
  onDelete: (id: string) => void;
}

function MemoryCard({
  memory,
  onDelete,
}: MemoryCardProps) {
  const [expanded, setExpanded] = useState(false);
  const content = expanded ? memory.content : memoryTruncatedContent(memory.content);

  return (
    <div className="bg-bg2 border border-[rgba(255,255,255,0.07)] rounded-lg px-4 py-3">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <span
            className={`text-[9px] px-1.5 py-0.5 rounded-sm font-semibold ${memoryAgentTypeColor(memory.agent_type)}`}
          >
            {memory.agent_type}
          </span>
          <span className="text-xs font-semibold text-text">{memoryTitle(memory.content)}</span>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-[9px] text-text3 font-mono">{memoryFormattedDate(memory.created_at)}</span>
          <button
            onClick={() => onDelete(memory.id)}
            className="text-[10px] text-red hover:opacity-80 cursor-pointer"
          >
            删除
          </button>
        </div>
      </div>
      <div className={`text-[10px] text-text2 whitespace-pre-wrap leading-relaxed ${!expanded ? "line-clamp-3" : ""}`}>
        {content}
      </div>
      <button
        onClick={() => setExpanded(!expanded)}
        className="text-[10px] text-text3 mt-1 cursor-pointer hover:text-text transition-colors"
      >
        {expanded ? "收起 ▲" : "展开 ▼"}
      </button>
    </div>
  );
}
