/**
 * 策略声明参数表接口的调用契约（2026-10-12 SOL/USDT 审计的 P3）。
 *
 * 这块的风险只有一个：**URL 必须按策略名正确编码**。策略名来自回测记录，
 * 是用户/Agent 生成的字符串；直接拼进路径时，名字里带 `/` 或空格就会打到
 * 另一个路由（甚至 404），而错误信息看起来会像"策略不存在"——
 * 于是一个拼错的 URL 会被当成"这策略没有参数"。
 *
 * 另外钉住：这个接口说的是"策略允许设什么"，不与会话 config 合并。
 */

import { afterEach, describe, expect, it, vi } from "vitest";

describe("tradingApi.getStrategyParams", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  function stubFetch() {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        strategy_name: "box_time_range_breakout_strategy",
        resolved: true,
        params: [
          {
            key: "volume_filter",
            type: "bool",
            default: false,
            description: "True=启用突破 K 线量能过滤",
            min: null,
            max: null,
          },
        ],
      }),
    });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("普通策略名打到 /api/trading/strategies/<name>/params", async () => {
    const fetchMock = stubFetch();
    const { tradingApi } = await import("@/lib/api");

    await tradingApi.getStrategyParams("box_time_range_breakout_strategy");

    const url = String(fetchMock.mock.calls[0][0]);
    // `request()` 统一剥掉末尾斜杠（全仓一致，Django 侧 APPEND_SLASH=True 补回），
    // 所以这里断言的是**剥掉之后**的形状。
    expect(url).toContain(
      "/api/trading/strategies/box_time_range_breakout_strategy/params"
    );
  });

  it("名字里的斜杠被编码，不会多切出一层路径", async () => {
    const fetchMock = stubFetch();
    const { tradingApi } = await import("@/lib/api");

    await tradingApi.getStrategyParams("weird/name");

    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain("weird%2Fname");
    expect(url).not.toContain("weird/name");
  });

  it("返回的默认值原样透出（false 也是有效默认值，不能被当成空）", async () => {
    stubFetch();
    const { tradingApi } = await import("@/lib/api");

    const res = await tradingApi.getStrategyParams("box_time_range_breakout_strategy");

    expect(res.resolved).toBe(true);
    expect(res.params[0].key).toBe("volume_filter");
    expect(res.params[0].default).toBe(false);
    expect(res.params[0].default).not.toBeUndefined();
  });
});
