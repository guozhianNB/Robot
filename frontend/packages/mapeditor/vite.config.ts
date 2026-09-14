import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { fileURLToPath, URL } from "node:url";

export default defineConfig({
  // base 与后端静态挂载路径一致（server.py 把产物挂到 /mapeditor）；
  // 注意 public/pixel-editor.html 也依赖这个前缀才能被访问到
  base: "/mapeditor/",
  plugins: [vue()],
  resolve: {
    // shared 包 main 指向 TS 源码，必须 alias 到源码路径（monorepo 已知坑）
    alias: {
      shared: fileURLToPath(new URL("../shared/src/index.ts", import.meta.url)),
    },
  },
  server: {
    port: 5175,
    proxy: {
      // 允许用 VITE_API_TARGET 指向板卡/远端后端
      "/api": process.env.VITE_API_TARGET || "http://127.0.0.1:8000",
    },
  },
});
