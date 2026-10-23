"use client";

import { createContext, useContext, useState, useCallback, useEffect, type ReactNode } from "react";
import { useRouter, usePathname } from "next/navigation";
import { authApi, setTokens, clearTokens, getAccessToken, type UserProfile } from "@/lib/api";

/** 已登录用户在界面上的可见身份。
 *
 *  `isStaff` 是 P5 加的（`docs/adr/0005-web-write-requires-staff.md`）：机制的 Web 写入口
 *  （翻开关 / 触发池化重算）只对 `is_staff` 显示。它是**界面纪律，不是安全边界**——服务端
 *  仍然会挡，而 slash 通路没有跟着收紧。
 */
export interface AuthUser {
  username: string;
  email: string;
  isStaff: boolean;
}

interface AuthContextType {
  isAuthenticated: boolean;
  isLoading: boolean;
  user: AuthUser | null;
  /** 写按钮的显示门槛。**默认 `false`**（fail-closed）：拿不到就不显示写入口。 */
  isStaff: boolean;
  login: (email: string, password: string) => Promise<boolean>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | null>(null);

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}

/** 响应 → 界面身份。**`isStaff` 只认 `=== true`**：字段缺失、`null`、`"false"` 一律当
 *  非 staff。反过来的默认（`!== false`）会在后端漏字段时把写按钮显示给所有人。 */
function toAuthUser(profile: UserProfile): AuthUser {
  return {
    username: profile.username,
    email: profile.email,
    isStaff: profile.is_staff === true,
  };
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [user, setUser] = useState<AuthUser | null>(null);
  const router = useRouter();
  const pathname = usePathname();

  // Restore session on mount
  useEffect(() => {
    const token = getAccessToken();
    if (token) {
      authApi
        .me()
        .then((userData) => {
          setUser(toAuthUser(userData));
          setIsAuthenticated(true);
        })
        .catch(() => {
          clearTokens();
        })
        .finally(() => {
          setIsLoading(false);
        });
    } else {
      setIsLoading(false);
    }
  }, []);

  // Redirect if not authenticated
  useEffect(() => {
    if (isLoading) return;
    if (!isAuthenticated && pathname !== "/login" && pathname !== "/") {
      router.push("/login");
    }
  }, [isAuthenticated, isLoading, pathname, router]);

  const login = useCallback(async (email: string, password: string) => {
    try {
      const data = await authApi.login({ email, password });
      setTokens(data.access, data.refresh);
      setUser(toAuthUser(data.user));
      setIsAuthenticated(true);
      router.push("/overview");
      return true;
    } catch {
      return false;
    }
  }, [router]);

  const logout = useCallback(async () => {
    try {
      await authApi.logout();
    } catch {
      // Ignore logout errors
    }
    clearTokens();
    setIsAuthenticated(false);
    setUser(null);
    router.push("/login");
  }, [router]);

  // `isStaff` 从 `user` 派生而不是另存一份 state：两份状态之间可以有「user 换了而
  // isStaff 还是上一任的」这种中间态，而它恰好是**权限判断**——那种不一致会显示出一个
  // 按下去才 403 的按钮。派生之后它们不可能分家。
  const isStaff = user?.isStaff === true;

  return (
    <AuthContext.Provider value={{ isAuthenticated, isLoading, user, isStaff, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}
