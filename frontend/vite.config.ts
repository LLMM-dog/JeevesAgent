import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath, URL } from "node:url";

// 后端默认 9000。8000 在 Windows 的 Hyper-V 保留段（7905-8928）里，
// 见 docs/05-dev/setup.md
const BACKEND = process.env.JEEVES_BACKEND ?? "http://127.0.0.1:9000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  define: {
    // 版本唯一来源是 package.json，构建时注入，前端不手写版本号
    __APP_VERSION__: JSON.stringify(process.env.npm_package_version ?? "0.0.0"),
  },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    // 显式绑 127.0.0.1。不指定时 Vite 只监听 IPv6 的 ::1，
    // 用 http://127.0.0.1:5173 访问会"无法连接"，而日志里明明写着
    // ready 且 Local: http://localhost:5173 —— 看起来像启动失败。
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: BACKEND,
        changeOrigin: true,
        // SSE 走 HTTP/1.1 长连接，不能被 websocket 升级逻辑接管
        ws: false,
        // SSE 必须关缓冲，否则事件会被攒起来批量发出，流式效果完全消失
        configure: (proxy) => {
          proxy.on("proxyRes", (proxyRes) => {
            proxyRes.headers["cache-control"] = "no-cache, no-transform";
          });
        },
      },
    },
  },
  build: {
    // 后端 main.py 从这里托管静态文件
    outDir: "dist",
    sourcemap: true,
  },
  test: {
    // jsdom 而不是 node：要测的是浏览器 API 的封装
    // （SpeechRecognition、document.documentElement.lang）
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
