/**
 * 鉴权状态。
 *
 * 三个态：
 * - checking  启动时正在询问后端
 * - ready     已拿到结果（authEnabled / authenticated 决定 UI）
 *
 * 鉴权未开启（本机模式）时 authenticated 恒视为 true ——
 * 界面行为与之前完全一致，不出现登录页。
 */

import { create } from "zustand";
import { api } from "@/lib/api";

interface AuthState {
  /** "checking" | "ready" */
  status: "checking" | "ready";
  authEnabled: boolean;
  authenticated: boolean;
  username: string;
  isAdmin: boolean;
  /** 启动时调用一次 */
  check: () => Promise<void>;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  /** 任意 API 返回 401（会话过期/被吊销）时调用 */
  sessionExpired: () => void;
}

export const useAuth = create<AuthState>((set, get) => ({
  status: "checking",
  authEnabled: false,
  authenticated: false,
  username: "",
  isAdmin: false,

  check: async () => {
    try {
      const me = await api.authMe();
      set({
        status: "ready",
        authEnabled: me.auth_enabled,
        // 鉴权未开启时直接放行 —— 本机模式不需要登录页。
        authenticated: me.auth_enabled ? me.authenticated : true,
        username: me.username,
        isAdmin: me.is_admin,
      });
    } catch {
      // 后端不可达时不能把用户锁在登录页外 —— 按无鉴权放行，
      // 页面自身会报网络错误。
      set({
        status: "ready",
        authEnabled: false,
        authenticated: true,
        username: "",
        isAdmin: false,
      });
    }
  },

  login: async (username, password) => {
    const r = await api.login(username, password);
    set({
      authEnabled: true,
      authenticated: true,
      username: r.username,
      isAdmin: r.is_admin,
    });
  },

  logout: async () => {
    await api.logout();
    set({ authenticated: false, username: "", isAdmin: false });
  },

  sessionExpired: () => {
    // 只有已开启鉴权才需要踢回登录页。
    if (get().authEnabled && get().authenticated) {
      set({ authenticated: false, username: "", isAdmin: false });
    }
  },
}));

