import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (name) => fs.readFileSync(path.join(root, "src", name), "utf8");

const app = read("App.vue");
const places = read("pages/PlacePanel.vue");
const zones = read("pages/ZonePanel.vue");

assert.match(app, /:items="places"/, "PlacePanel 必须使用父组件的地点列表");
assert.match(app, /:items="zones"/, "ZonePanel 必须使用父组件的区域列表");
assert.match(places, /items:\s*Destination\[\]/, "PlacePanel 缺少 items prop");
assert.match(zones, /items:\s*Zone\[\]/, "ZonePanel 缺少 items prop");
assert.doesNotMatch(places, /destinationsUrl|getJson/, "PlacePanel 不应自行读取地点");
assert.doesNotMatch(zones, /zonesUrl|getJson/, "ZonePanel 不应自行读取区域");
assert.match(app, /<MapFiles\s+v-if="tab === 'files'"/,
  "地图文件面板必须懒挂载，避免首屏重复读取 meta");
assert.match(app, /statusPending/, "状态轮询必须有进行中守卫");
assert.match(app, /if \(statusPending\) return;/, "重叠状态轮询必须直接跳过");
assert.match(app, /getJson<[^>]+>\("\/api\/mapeditor\/status"\)/,
  "状态轮询必须使用单一汇总接口，避免并发读取 rosbridge");
assert.doesNotMatch(app, /getJson<[^>]+>\("\/api\/robot\/pose"\)/,
  "前端不应与当前地图接口并发读取 rosbridge");

console.log("mapeditor startup contract: ok");
