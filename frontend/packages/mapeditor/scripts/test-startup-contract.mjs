import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (name) => fs.readFileSync(path.join(root, "src", name), "utf8");

const app = read("App.vue");
const places = read("pages/PlacePanel.vue");
const zones = read("pages/ZonePanel.vue");
const canvas = read("pages/MapCanvas.vue");
const types = read("lib/types.ts");

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
assert.match(types, /DrawMode\s*=\s*[^;]*"goal"/, "DrawMode 必须支持区域停靠点模式");
assert.match(types, /interface ZoneGoal/, "共享类型必须声明 ZoneGoal");
assert.match(canvas, /\(e:\s*"goal-point"/, "画布必须发出独立 goal-point 事件");
assert.match(canvas, /z\.goal/, "画布必须绘制已有区域停靠点");
assert.match(app, /const doneGoal = ref/, "App 必须持有区域停靠点草稿");
assert.match(app, /@goal-point="onGoalPoint"/, "App 必须接收画布停靠点事件");
assert.match(app, /:done-goal="doneGoal"/, "App 必须把停靠点传给 ZonePanel");
assert.match(zones, /goal_validation/, "区域保存后必须显示停靠点校验警告");
assert.match(zones, /未标停靠点/, "区域列表必须提示缺少停靠点");

const service = fs.readFileSync(path.join(root, "src", "lib", "service.ts"), "utf8");
assert.match(app, /保存并退出/, "编辑器顶部必须有「保存并退出」");
assert.match(app, /仅关窗/, "编辑器顶部必须有「仅关窗（保留服务）」");
assert.match(app, /stopService\(\)/, "「保存并退出」必须调用 stopService()");
assert.match(service, /\/api\/mapeditor\/service\/stop/, "service.ts 必须打同源自停接口");
assert.match(service, /window\.close\(\)/, "service.ts 必须能关窗");

console.log("mapeditor startup contract: ok");
