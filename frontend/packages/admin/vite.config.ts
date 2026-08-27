import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { fileURLToPath, URL } from "node:url";

export default defineConfig({
  plugins: [vue()],
  resolve: {
    // shared 包 main 指向 TS 源码（src/index.ts），必须 alias 到源码路径，
    // 否则 Vite build 无法解析 workspace 包（monorepo 已知坑）
    alias: {
      shared: fileURLToPath(new URL("../shared/src/index.ts", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
