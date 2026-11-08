/**
 * Vite 开发与构建配置（OAG 版）
 * 与 RAG 版共用同一套前端，只把代理目标改到 OAG 后端（8010），
 * 端口用 5174，避免两个版本同时启动时冲突。
 */
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const backend = env.VITE_DEV_PROXY_TARGET || "http://127.0.0.1:8010";

  return {
    plugins: [react()],
    server: {
      host: "0.0.0.0",
      port: 5174,
      proxy: {
        "/api": {
          target: backend,
          changeOrigin: true,
        },
      },
    },
  };
});
