/**
 * `request()` 的错误形状（P5 子票 #46）。
 *
 * 这一层的全部风险在「后端说的那句话能不能到人眼前」。改之前 `request()` 把所有非 2xx
 * 压成一句 `API error: 400`，于是后端精心写的「切换原因不能为空」在页面上永远看不到，
 * 而前端要显示原因只能靠状态码猜。
 *
 * 四条契约，各自对应一个真实的失败形态：
 *
 *   1. **`ApiError` 带 `status` 与后端原文** —— 否则「为什么不行」只剩一个数字。
 *   2. **`ApiError` 是 `Error` 的子类** —— 全仓几十处 `err instanceof Error ? err.message`
 *      靠它；不继承，那些地方会静默退化成「未知错误」（本仓最贵的那类错：没有报错路径）。
 *   3. **非 JSON 的响应体不许让 `request()` 自己炸** —— 后端 500 白页、反代 HTML 错误页
 *      解析出来是 `SyntaxError`，那会把「500」这个真原因替换成「JSON 解析失败」。
 *   4. **401 刷新流程原样保留** —— 刷新成功后重试、刷新失败才跳登录。这段代码是最不该
 *      被顺手重写的一段，所以用行为把它钉住。
 */

import { afterEach, describe, expect, it, vi } from "vitest";

const ACCESS_KEY = "tradeclaw_access";
const REFRESH_KEY = "tradeclaw_refresh";

/** 一个最小的 `Response` 替身：只实现 `request()` 真正读的那几样。 */
function response(body: string, init: { ok?: boolean; status?: number } = {}) {
  const status = init.status ?? 200;
  return {
    ok: init.ok ?? (status >= 200 && status < 300),
    status,
    text: async () => body,
    json: async () => JSON.parse(body),
  };
}

describe("request() 的错误形状", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
    localStorage.clear();
  });

  it("400 + {error} → ApiError 的 message 是后端那句话，status 是 400", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        response(JSON.stringify({ error: "切换原因不能为空" }), { status: 400 })
      )
    );
    const { regimeApi, ApiError } = await import("@/lib/api");

    const failure = await regimeApi.switchMode("mechanism", "executing").catch((e) => e);

    expect(failure).toBeInstanceOf(ApiError);
    expect(failure).toBeInstanceOf(Error);
    expect(failure.message).toBe("切换原因不能为空");
    expect(failure.status).toBe(400);
  });

  it("403 + {detail} → 用 detail（权限类是这么回话的）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        response(JSON.stringify({ detail: "此操作需要管理员权限。" }), { status: 403 })
      )
    );
    const { regimeApi, ApiError } = await import("@/lib/api");

    const failure = await regimeApi.switchMode("mechanism", "executing").catch((e) => e);

    expect(failure).toBeInstanceOf(ApiError);
    expect(failure.message).toBe("此操作需要管理员权限。");
    expect(failure.status).toBe(403);
  });

  it("HTML 响应体（反代错误页）→ 退回 API error: 500，不是 SyntaxError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        response("<!doctype html><html><body>502 Bad Gateway</body></html>", {
          status: 500,
        })
      )
    );
    const { regimeApi, ApiError } = await import("@/lib/api");

    const failure = await regimeApi.getOverview().catch((e) => e);

    expect(failure).toBeInstanceOf(ApiError);
    expect(failure.message).toBe("API error: 500");
    expect(failure.status).toBe(500);
    // 关键：不是 `SyntaxError: Unexpected token '<'`。那会把真原因（500）换掉。
    expect(failure).not.toBeInstanceOf(SyntaxError);
  });

  it("空响应体（204 之外的裸状态码）→ 同样退回 API error: <status>", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response("", { status: 502 })));
    const { regimeApi, ApiError } = await import("@/lib/api");

    const failure = await regimeApi.getPool().catch((e) => e);

    expect(failure).toBeInstanceOf(ApiError);
    expect(failure.message).toBe("API error: 502");
  });

  it("JSON 但不是 {error}/{detail}（DRF 字段校验错）→ 退回 API error: <status>", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        response(JSON.stringify({ email: ["这个字段是必填项。"] }), { status: 400 })
      )
    );
    const { authApi, ApiError } = await import("@/lib/api");

    const failure = await authApi.login({ email: "", password: "" }).catch((e) => e);

    expect(failure).toBeInstanceOf(ApiError);
    expect(failure.message).toBe("API error: 400");
  });

  it("成功路径不受影响：2xx 照旧返回解析后的 JSON", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(response(JSON.stringify({ ok: true }), { status: 200 }))
    );
    const { regimeApi } = await import("@/lib/api");

    await expect(regimeApi.getOverview()).resolves.toEqual({ ok: true });
  });
});

describe("401 刷新流程（不许被这次改动碰坏）", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
    localStorage.clear();
  });

  it("刷新成功 → 用新令牌重试一次，返回重试的结果", async () => {
    localStorage.setItem(ACCESS_KEY, "stale-access");
    localStorage.setItem(REFRESH_KEY, "refresh-token");

    const fetchMock = vi
      .fn()
      // 1) 原请求：401
      .mockResolvedValueOnce(response(JSON.stringify({ detail: "expired" }), { status: 401 }))
      // 2) 刷新：成功
      .mockResolvedValueOnce(
        response(JSON.stringify({ access: "fresh-access" }), { status: 200 })
      )
      // 3) 重试：成功
      .mockResolvedValueOnce(response(JSON.stringify({ switches: [] }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    const { regimeApi } = await import("@/lib/api");

    await expect(regimeApi.getOverview()).resolves.toEqual({ switches: [] });

    // 刷新那一次打到 refresh 端点
    expect(String(fetchMock.mock.calls[1][0])).toContain("/api/auth/token/refresh/");
    // 重试带上了**新**令牌——这是刷新这段代码唯一的目的
    const retryHeaders = fetchMock.mock.calls[2][1].headers as Record<string, string>;
    expect(retryHeaders.Authorization).toBe("Bearer fresh-access");
    expect(localStorage.getItem(ACCESS_KEY)).toBe("fresh-access");
  });

  it("刷新失败 → 清令牌、跳登录，抛的仍是 ApiError(401)", async () => {
    localStorage.setItem(ACCESS_KEY, "stale-access");
    localStorage.setItem(REFRESH_KEY, "refresh-token");

    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response(JSON.stringify({ detail: "expired" }), { status: 401 }))
      .mockResolvedValueOnce(response(JSON.stringify({ detail: "bad" }), { status: 401 }));
    vi.stubGlobal("fetch", fetchMock);

    const { regimeApi, ApiError } = await import("@/lib/api");

    const failure = await regimeApi.getOverview().catch((e) => e);

    expect(failure).toBeInstanceOf(ApiError);
    expect(failure.message).toBe("Unauthorized");
    expect(failure.status).toBe(401);
    expect(localStorage.getItem(ACCESS_KEY)).toBeNull();
    expect(localStorage.getItem(REFRESH_KEY)).toBeNull();
  });

  it("重试也失败 → 抛 ApiError 带重试那次的 status，不是 401", async () => {
    localStorage.setItem(ACCESS_KEY, "stale-access");
    localStorage.setItem(REFRESH_KEY, "refresh-token");

    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response(JSON.stringify({ detail: "expired" }), { status: 401 }))
      .mockResolvedValueOnce(
        response(JSON.stringify({ access: "fresh-access" }), { status: 200 })
      )
      .mockResolvedValueOnce(
        response(JSON.stringify({ error: "还是不行" }), { status: 400 })
      );
    vi.stubGlobal("fetch", fetchMock);

    const { regimeApi, ApiError } = await import("@/lib/api");

    const failure = await regimeApi.getOverview().catch((e) => e);

    expect(failure).toBeInstanceOf(ApiError);
    expect(failure.message).toBe("还是不行");
    expect(failure.status).toBe(400);
  });
});
