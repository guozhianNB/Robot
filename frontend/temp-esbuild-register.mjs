// 临时：在受限沙箱里跑 vitest 的模块预加载（esbuild 垫片 + 子进程 stdio 适配）。
// 复用地图编辑器那套沙箱适配，跑完即删。
import { register } from "node:module";
import { pathToFileURL } from "node:url";

import "./packages/mapeditor/scripts/child-process-stdio.mjs";

register("./packages/mapeditor/scripts/esbuild-shim.mjs", pathToFileURL(import.meta.filename));
