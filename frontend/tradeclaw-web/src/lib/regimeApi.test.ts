/**
 * `regimeApi` 的调用契约（P5 子票 #46）。
 *
 * 这块的风险不在「有没有这几个方法」，而在**URL 与请求体的形状**：后端每一条都有一条
 * 明确的纪律，前端猜错一次就会得到 400/404，而错误信息看起来会像「这个功能不存在」。
 *
 *   1. `switchMode` 的请求体**只有 `{kind, mode}`**。后端忽略客户端提交的 `reason`
 *      （T4 #42 有一条用例专门传假 reason 并断言库里不出现它），所以前端如果「顺手」
 *      把页面上的理由塞进请求体，就会造出「我写了但它没落库」这种最难查的错。
 *   2. `getBriefing` 的路径段是 kind 本身，不是 `briefing/?kind=`。
 *   3. `getRebuildStatus` 的 task_id 必须编码：它来自 Celery，理论上是不含 `/` 的 uuid，
 *      但拼路径时不对它做 `encodeURIComponent` 就是把「一个变量拼进 URL」这件事做错了一半。
 *   4. `getPool` 不带 `?generation=` 时**不许**留下一个裸 `?`。
 */

import { afterEach, describe, expect, it, vi } from "vitest";

function okResponse(body: unknown) {
  const text = JSON.stringify(body);
  return {
    ok: true,
    status: 200,
    text: async () => text,
    json: async () => JSON.parse(text),
  };
}

describe("regimeApi", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
    localStorage.clear();
  });

  function stub() {
    const fetchMock = vi.fn().mockResolvedValue(okResponse({}));
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("switchMode 的请求体只有 kind 与 mode（reason 由服务端取）", async () => {
    const fetchMock = stub();
    const { regimeApi } = await import("@/lib/api");

    await regimeApi.switchMode("event_breaker", "shadow");

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/regime/switch");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      kind: "event_breaker",
      mode: "shadow",
    });
  });

  it("getBriefing 把 kind 放路径段里", async () => {
    const fetchMock = stub();
    const { regimeApi } = await import("@/lib/api");

    await regimeApi.getBriefing("regime_gate");

    expect(String(fetchMock.mock.calls[0][0])).toContain(
      "/api/regime/briefing/regime_gate"
    );
  });

  it("getPool 不带参数时不留一个裸 ?", async () => {
    const fetchMock = stub();
    const { regimeApi } = await import("@/lib/api");

    await regimeApi.getPool();

    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain("/api/regime/pool");
    expect(url).not.toContain("?");
  });

  it("getPool(12) 带上 generation", async () => {
    const fetchMock = stub();
    const { regimeApi } = await import("@/lib/api");

    await regimeApi.getPool(12);

    expect(String(fetchMock.mock.calls[0][0])).toContain("generation=12");
  });

  it("getPool(0) 也带上 generation（0 是有效 id，不能被当成「没传」）", async () => {
    const fetchMock = stub();
    const { regimeApi } = await import("@/lib/api");

    await regimeApi.getPool(0);

    expect(String(fetchMock.mock.calls[0][0])).toContain("generation=0");
  });

  it("getGenerations 组装分页与 generation 参数", async () => {
    const fetchMock = stub();
    const { regimeApi } = await import("@/lib/api");

    await regimeApi.getGenerations({ page: 2, page_size: 5, generation: 9 });

    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain("page=2");
    expect(url).toContain("page_size=5");
    expect(url).toContain("generation=9");
  });

  it("rebuildPool 是 POST 到 pool/rebuild", async () => {
    const fetchMock = stub();
    const { regimeApi } = await import("@/lib/api");

    await regimeApi.rebuildPool();

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/regime/pool/rebuild");
    expect(init.method).toBe("POST");
  });

  it("getRebuildStatus 对 task_id 做编码，不会多切出一层路径", async () => {
    const fetchMock = stub();
    const { regimeApi } = await import("@/lib/api");

    await regimeApi.getRebuildStatus("a/b c");

    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain("a%2Fb%20c");
    expect(url).not.toContain("a/b");
  });
});
