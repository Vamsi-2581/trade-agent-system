/**
 * `AuthContext` 的 `isStaff`（P5 子票 #46）。
 *
 * 这个布尔是**写按钮的显示门槛**，所以它只有一种安全的方向：拿不到、读不懂、字段缺失，
 * 一律当 `false`。反过来（`!== false`、默认 true）会在后端漏字段那天把「翻开关」「触发
 * 重算」显示给每一个登录用户，而那两个按钮**真的会改全局行为**。
 *
 * 另一条：`isStaff` 从 `user` 派生。两份 state 之间会有「user 换了而 isStaff 还是上一任
 * 的」这种中间态，而它恰好是权限判断——那种不一致会显示出一个按下去才 403 的按钮。
 */

import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

const me = vi.hoisted(() => vi.fn());
const login = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  authApi: { me, login, logout: vi.fn() },
  setTokens: vi.fn(),
  clearTokens: vi.fn(),
  getAccessToken: vi.fn(() => "a-token"),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => "/overview",
}));

import { AuthProvider, useAuth } from "@/context/AuthContext";

function wrapper({ children }: { children: ReactNode }) {
  return <AuthProvider>{children}</AuthProvider>;
}

const profile = {
  id: 1,
  email: "a@b.c",
  username: "alice",
  is_active: true,
  is_frozen: false,
  is_staff: true,
  created_at: "2026-01-01T00:00:00Z",
  last_login_at: null,
};

describe("AuthContext.isStaff", () => {
  beforeEach(() => {
    me.mockReset();
    login.mockReset();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("is_staff=true → isStaff 为 true，user 也带上它", async () => {
    me.mockResolvedValue({ ...profile, is_staff: true });

    const { result } = renderHook(() => useAuth(), { wrapper });

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.isStaff).toBe(true);
    expect(result.current.user).toEqual({
      username: "alice",
      email: "a@b.c",
      isStaff: true,
    });
  });

  it("is_staff=false → isStaff 为 false", async () => {
    me.mockResolvedValue({ ...profile, is_staff: false });

    const { result } = renderHook(() => useAuth(), { wrapper });

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.isStaff).toBe(false);
  });

  it("响应里没有 is_staff 字段 → fail-closed 当 false（不是 undefined）", async () => {
    // 后端漏了字段 / 旧版本部署。**不许**因为「不是明确的 false」就放行。
    const { is_staff: _omitted, ...withoutFlag } = profile;
    void _omitted;
    me.mockResolvedValue(withoutFlag);

    const { result } = renderHook(() => useAuth(), { wrapper });

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.isStaff).toBe(false);
    expect(result.current.user?.isStaff).toBe(false);
  });

  it("is_staff 是字符串 \"true\" 也当 false（只认布尔 true）", async () => {
    me.mockResolvedValue({ ...profile, is_staff: "true" });

    const { result } = renderHook(() => useAuth(), { wrapper });

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.isStaff).toBe(false);
  });

  it("还没登录（user 为 null）→ isStaff 为 false", async () => {
    me.mockRejectedValue(new Error("API error: 401"));

    const { result } = renderHook(() => useAuth(), { wrapper });

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.user).toBeNull();
    expect(result.current.isStaff).toBe(false);
  });

  it("登录成功后 isStaff 跟着登录响应的 user 走", async () => {
    me.mockRejectedValue(new Error("API error: 401"));
    login.mockResolvedValue({
      user: { ...profile, is_staff: true },
      access: "a",
      refresh: "r",
    });

    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    await result.current.login("a@b.c", "pw");

    await waitFor(() => expect(result.current.isStaff).toBe(true));
  });
});
