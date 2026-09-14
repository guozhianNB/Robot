# 地图编辑器·像素修图加装项 设计（第三期）

> **目的：** 给地图编辑器加一个「能改像素的画笔」——擦噪点、补墙、把一块 unknown 涂成 free；并且**后端与浏览器都跑在 PC 上**（板卡性能有限，地图工作不下放板卡），改完的改动经 SSH **通过网络落到板卡的地图文件**，全程不碰板卡键盘。
> **状态：** 设计定稿待评审（未开工）。
> **日期：** 2026-09-14
> **依赖：** 本文是《2026-09-14-map-editor-design.md》（第一期：看图 / 标地点 / 划区域）的**加装项**，复用它的 `/mapeditor` 挂载点、`mapserver.py`（PGM 解析）与地图文件接口。**第一期尚未开工**，故本文 §十三 任务 1 自带「最小可用子集」，保证加装项不被第一期阻塞。
> **用户决策（2026-09-14，原话）：**
> ① 「网上有没有已经开源的 slam 地图编辑器？可以改像素的那种，我直接拿过来改造」
> ② 「我想走路线 a，写一份文档吧。我还想让 io 通过网络连接，这样就不用在开发板上编辑，而是可以在上位机编辑」
> ③ 拓扑选择：**「两个都要，加一个 `MAPS_IO=local|ssh` 开关」**
> ④ 「我想了想，还是把地图工作放到PC上吧，因为板卡性能不是很好。」
> **拓扑定案（按 ④ 重排）：** 主形态 = **后端跑在 PC 上**（`MAPS_IO=ssh`，**默认值**），地图文件真相仍在板卡，经 SSH 读写；备用形态 = 后端跑在板卡上（`MAPS_IO=local`），与第一期口径一致。板卡因此不再承担 PGM 解码、灰度 PNG 编码、未知率统计、静态文件服务这类 CPU 活——这顺带解掉了第一期 §7.4 对板卡算力的隐忧。
> **选型结论：** 直接拿 **GyroPalm/ROS-SLAM-Map-Editor**（MIT，浏览器单文件 `editor.html`，43 KB）改造，**不自己写、不用 RViz 系插件**（RViz 系要 ROS 环境，违背第一期「Windows 无 ROS 也能开发」的立身之本）。

---

## 一、目标与验收

**目标：** 打开 `/mapeditor/pixel?map=my_map`，毛笔刷擦掉走廊尽头的噪点、补一堵漏掉的墙、把门口一小块 unknown 涂成 free，点保存，改动**通过网络落到板卡** `ros2_car/maps/`；全程在 Windows 上完成，不碰板卡键盘。

**验收标准（可逐条打勾）：**

1. `/mapeditor/pixel?map=<name>` 能打开像素编辑器，并且**自动加载**该图的 `yaml`+`pgm`（不需要用户拖文件）。
2. 加载后画笔、擦除、Un-Scan（标未知）、线、矩形、撤销重做、keepout 掩膜、量距**全部可用**——即 GyroPalm 原生功能零损失。
3. **完全离线可用**：板卡/PC 断开公网，页面照常打开与绘制（5 个 CDN 资源全部本地自托管）。
4. 点「Download Map」后**不弹出浏览器下载框**，而是被截获 → `POST` 回后端 → 落盘到板卡 maps 目录；页面上给出「已保存到 `<路径>`，备份 `<路径>`」的明文反馈。
5. 保存目标的**默认行为是另存为新图**（`<原名>_edited`），覆盖原图必须显式勾选「覆盖原图」且后端先做自动备份。
6. 后端对回传 yaml 做**白名单校验**：除 `image` 字段外，任何字段的磁盘原值与回传值不一致 → 拒绝保存并说明差异（防止 `resolution`/`origin` 被静默改掉、让第一期已标地点全部失效）。
7. **主形态可用（默认）**：后端跑在 PC 上、`MAPS_IO=ssh` 时，能列出、读取、写回板卡 `/home/sunrise/Robot/ros2_car/maps/`；`MAPS_IO=local`（后端跑在板卡上）作为备用形态，行为与第一期完全一致。
8. **远程不可用不崩**：ssh 连不上/依赖缺失时，接口返回 `{"ok": True, "status": "unavailable", "reason": "..."}`（查询类保持 `ok: True`，遵 AGENTS「系统稳健性」），且**离线缓存副本仍可看图**并明确标注「当前离线，显示缓存（时间戳）」。
9. 路径与命令安全：地图名白名单 `^[A-Za-z0-9_-]{1,64}$`，任何含 `..`、`/`、空格的输入一律拒绝；ssh 子进程模式下这条是**防命令注入红线**。
10. 保存 / 拒绝 / IO 模式切换全部落审计日志（事件名 `map_edit_save` / `map_edit_reject` / `map_io_change`）。

---

## 二、范围

### 2.1 本轮做

| 能力 | 说明 |
|---|---|
| 像素级修图（浏览器） | 复用 GyroPalm `editor.html`：画笔/擦除/Un-Scan/线/矩形/填充/撤销重做/keepout/量距 |
| 资源自托管 | jQuery 3.4.1、js-yaml 4.1.0、Bootstrap 4.4.1（CSS+JS）、Font Awesome 4.7.0（含 webfonts）落入 `public/vendor/` |
| 网络 IO（读） | 页面按 URL 参数自动从后端拉 `yaml`+`pgm`，不走拖拽 |
| 网络 IO（写） | 覆写下载通道 → 回传后端 → 落盘 + 备份 + 审计 |
| `MapStore` 抽象 | `ssh`（**默认**，PC 上跑后端 → 远程读写板卡）/ `local`（备用，板卡上跑后端）双实现，配置切换 |
| 远程缓存 | 本地缓存目录 + 远程 `mtime/size` 新鲜度判断；离线可看 |
| 安全 | 地图名白名单、上传体积上限、yaml 白名单校验、备份保留 N 份 |

### 2.2 本轮明确不做

| 不做 | 理由 |
|---|---|
| 自己实现画笔/撤销栈/线算法 | 拿现成的，YAGNI；这也是用户拍板的「路线 a」 |
| 把绘制逻辑移植进 `MapCanvas.vue`（路线 b） | 用户已明确否决；且会牵动第一期画布的坐标换算，风险集中 |
| 多边形工具、速度限制掩膜 | GyroPalm 自己也列为 Future Work，与本次痛点无关 |
| 裁剪 / 改分辨率后重存 / 改 origin | 第一期 §7.2 已定：改这两个字段一律要显式确认；本加装项**只改像素，不动元数据** |
| 建图、从 SLAM 直接存图、远程启停 Nav2 | 沿用第一期 §2.2：一律走 `~/tools/nav_screen.sh` |
| 权限与登录 | 沿用第一期 §10 留白：接口暂不鉴权，仅 Tailscale/局域网内使用 |
| ⚠️ **重扫一张地图** | **不是「不做」，是「必须先做」**——见 §十四 前置条件 |

---

## 三、拓扑与数据流

用户拍板：**`MAPS_IO=local|ssh` 双模式，两个都要**；并在 2026-09-14 进一步定为**以 PC 为主**（「还是把地图工作放到PC上吧，因为板卡性能不是很好」）。

### 3.1 主形态：后端跑在 PC 上（`MAPS_IO=ssh`，**默认**）

```
PC 浏览器 ──HTTP──▶ PC :8000（LLM 后端，工作目录 D:\_project\Robot）
                     │
                     ├─ MapStore(ssh) ──SSH/SFTP──▶ sunrise@100.65.82.93
                     │                               /home/sunrise/Robot/ros2_car/maps/
                     ├─ 本地缓存 LLM/data/mapcache/（离线看图）
                     └─（第一期）rosbridge ws://100.65.82.93:9090 读位姿
```

- **为什么放 PC**：板卡（RDK X5）性能有限，而地图工作里吃 CPU 的活（PGM 解码、灰度 PNG 编码、未知率直方图统计、静态文件服务）现在全落在 PC 上；板卡只留下它必须独占的本职（串口、建图/定位/导航）。
- 好处：浏览器与后端同在 PC（localhost，零网络延迟）；板卡不在、Tailscale 只通一半、临时断网时仍能看图（缓存）与画图，改动攒在缓存里、恢复连接后写入。
- 代价：多一层网络文件代码，本身会成为新的故障点——因此**所有失败都必须降级而非报错**（§八）；同时**板卡可达性从「验收相关」升级为「开工前置」**（§十四）。
- 附带要求：第一期的「车在哪」「当前地图识别」也随之改由 PC 侧读板卡，因此**板卡上的 rosbridge 必须从 PC 可达**（Tailscale 通 + 板卡上起 `~/tools/nav_screen.sh lat`，用 `ss -ltnp | grep 9090` 复核）。但看图 / 标点 / 像素修图**不依赖 rosbridge**——它挂了照样能编辑，一期 §八 的降级口径不变。

### 3.2 备用形态：后端跑在板卡上（`MAPS_IO=local`）

```
PC 浏览器 ──HTTP──▶ 板卡 :8000（LLM 后端，工作目录 /home/sunrise/Robot）
                      │
                      ├─ MapStore(local) ──▶ /home/sunrise/Robot/ros2_car/maps/
                      └─（第一期）rosbridge :9090 读位姿
```

- 地图文件对后端就是本地文件，**零新增依赖、零远程代码**，实现最简单（纯 stdlib）。
- 保留它的三个用途：① 作 `SshMapStore` 的**行为基准与契约测试对照组**（`MapStore` 接口对上层完全一致，上层代码一行不用改）；② 主形态断网时，开发期仍可用它 + `MAPS_DIR` 指向仓库副本推进前端；③ 万一 PC 不在场，一台能上网的终端也能临时顶上。
- 此形态下前端开发仍可 `vite dev`(:5175) 把 `/api` 代理到 `http://100.65.82.93:8000`。

### 3.3 前端资源落点

```
frontend/packages/mapeditor/public/
  pixel-editor.html          ← GyroPalm editor.html 原样 + 仅 6 处改动（§六 6.1）
  pixel-netio.js             ← 我们的补丁脚本（同文档加载，覆写浏览器 API 截获 IO）
  vendor/
    jquery-3.4.1.min.js
    js-yaml-4.1.0.min.js
    bootstrap-4.4.1.min.css / bootstrap-4.4.1.bundle.min.js
    font-awesome-4.7.0/css/font-awesome.min.css
    font-awesome-4.7.0/fonts/*            ← CSS 用相对路径引 webfonts，目录结构必须照搬
    ROS-SLAM-Map-Editor.LICENSE          ← MIT 原文，必须保留（§附录）
```

生产路径：`http://<后端地址>:8000/mapeditor/pixel-editor.html?map=my_map`。

---

## 四、后端：`LLM/mapstore.py`（新增）

一期一责：**只负责「地图文件在哪、怎么读写」，不含任何 HTTP 与业务校验**。

```python
class MapStore(Protocol):
    def available(self) -> tuple[bool, str]: ...        # (是否可用, 不可用原因)
    def list(self) -> list[MapEntry]: ...               # name/width/height/resolution/origin/
                                                        # unknown_ratio/has_pgm/mtime/source
    def read(self, name: str, ext: str) -> bytes: ...    # ext ∈ {"yaml","pgm"}
    def write(self, name: str, ext: str, data: bytes) -> None
    def remove(self, name: str, ext: str) -> None
    def rename(self, name: str, new_name: str) -> None
    def stat(self, name: str, ext: str) -> tuple[float, int] | None   # (mtime, size)
```

### 4.1 两个实现

| 实现 | 传输 | 依赖 | 说明 |
|---|---|---|---|
| `LocalMapStore(root)` | `pathlib` 直接读写 | **纯 stdlib** | **备用形态**（板卡上跑后端），兼作契约测试基准；`root = conf.MAPS_DIR = BASE_DIR/"ros2_car"/"maps"` |
| `SshMapStore(host, user, port, root, auth)` | SFTP | ① 优先 `paramiko`（**可选依赖**，`try/except` 顶层引入）；② 降级为 `ssh.exe`/`scp.exe` 子进程（密钥认证） | **主形态（默认）**（PC 上跑后端）；两种通道都不可用 → `available() = (False, "缺少 paramiko 且 ssh 客户端不可用")` |

**`SshMapStore` 的两条通道为什么都要留：**
- `paramiko` 走 SFTP，能流式读写、能改密码认证，最省事，但它是外部依赖 → 必须遵 AGENTS「可选依赖顶层 try/except、缺失只降级不崩」；
- 子进程 `ssh.exe`/`scp.exe` 是 Windows 自带的（本机已确认存在 `C:\Windows\System32\OpenSSH\ssh.exe`），零依赖，但**只支持密钥认证**（明文密码无法自动化，Windows 没有 `sshpass`）。
- 选择顺序在配置里可强制：`MAPS_SSH_TRANSPORT=auto|paramiko|cli`。

> **子进程模式的硬约束：** 命令由字符串拼接 `host`/`user`/`path` 而成，因此 `name` 必须先过 §五 的白名单正则——**这是本加装项唯一的一处命令注入面**。

### 4.2 缓存与新鲜度

- 缓存目录 `DATA_DIR/mapcache/`，键 = `sha1(io_mode + root + name + ext)`，值为 `{data, mtime, size, at}`。
- 远程模式下读流程：`stat()`（一次轻量 SSH 往返）→ 与缓存记录的 `mtime/size` 一致则直接用缓存 → 否则 `read()` 并刷新缓存。
- **`stat()` 失败（断网）时**：如果缓存里有值，返回缓存并置 `stale=True`（前端显示「当前离线，显示缓存（2026-09-14 11:00）」）；缓存也没有 → `status: "unavailable"`。
- 缓存**永不**用于 `list()` 的权威结论：列表以远程为准，失败时才用缓存兜底并标 `stale`。

### 4.3 依赖与启动

`server.py` 的 `lifespan` 里**不新增任何启动步骤**（`MapStore` 是惰性构造、按需连接），避免拖累后端启动；只加一行 `audit.log("map_io_change", mode=...)` 记录生效模式。

---

## 五、后端接口（`server.py` 路由）

### 5.1 复用第一期（本加装项不重新定义，只要它存在）

| 方法 | 路径 | 本文用途 |
|---|---|---|
| GET | `/api/map/list` | 页面顶部的选图下拉 |
| GET | `/api/map/{name}/download?file=yaml\|pgm` | **读原始字节**。`fetch()` 忽略 `Content-Disposition: attachment`，直接拿 bytes 即可，故**不再新增 `/raw` 接口** |

> 若第一期尚未开工，任务 1 只需实现这两条（约 60 行，`mapstore.py` + 两个路由），即可支撑本加装项独立落地。

### 5.2 新增

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/map/{name}/save` | 保存像素改动。**JSON 体**：`{pgm_b64, yaml_text, mode:"saveas"\|"overwrite", new_name?, confirm?}`。返回 `{ok, wrote:[...], backup, rejected[]}` |
| GET | `/api/mapeditor/io` | `{mode, root, available, reason, transport, stale, cached_at}`，供页面状态条显示 |
| POST | `/api/mapeditor/io/test` | 主动做一次连通性自检（`list()` 一次），返回耗时与错误原因；**不修改任何文件** |

**`/api/map/{name}/save` 的执行顺序（每一步都不可省）：**

1. 白名单校验 `name` 与 `new_name`（§七 7.1），拒绝路径穿越与命令注入；
2. 体积校验：解码后 `pgm` ≤ `MAPS_MAX_PGM_BYTES`（默认 10 MB），`yaml_text` ≤ 64 KB；
3. 解析上传的 `yaml_text`（**纯 stdlib 的扁平 `key: value` 解析器**——一期 §7.4 已定「能用 stdlib 就绝不引外部依赖」，本项目依赖清单里没有 pyyaml），与磁盘原 yaml 逐字段比对——**除 `image` 外任何字段不一致 → `map_edit_reject` + 409**，响应里列出差异键；
4. 决定落盘名：`mode=saveas` → `new_name or f"{name}_edited"`；`mode=overwrite` 且 `confirm=true` → `name`；
5. 备份：把目标图**当前**的 `pgm`+`yaml` 复制到 `maps/.backup/<name>.<YYYYmmdd-HHMMSS>.{pgm,yaml}`，保留最近 `MAPS_BACKUP_KEEP`（默认 10）组，超出即删最旧（备份目录的清理**只按本加装项自己的命名规则删**，绝不递归删目录）；
6. 写 `pgm`：先写 `<目标>.pgm.tmp` 再 `rename` 原子替换（远程模式先传 tmp 再 mv）；
7. 写 `yaml`：**以磁盘原文为本**，只替换 `image:` 那一行（无该行则追加），使注释与键顺序 100% 保留；`image:` 值写成与第 4 步一致的实际文件名；
8. `audit.log("map_edit_save", ...)`，返回 `wrote` 与 `backup` 路径。

> **为什么 yaml 以磁盘原文为本、而不是用前端回传的文本：** GyroPalm 内部用 `js-yaml` 的 `dump()` **重新序列化**，注释、缩进风格、引号风格全部丢失（键值本身保留）。它对我们的 7 行 yaml 无害，但一旦将来有人往 yaml 里写注释，就会被静默清掉。以磁盘原文改一行为准，成本更低、更安全。

---

## 六、前端设计

### 6.1 `pixel-editor.html`：相对原文件**只改 6 处**

保持其余字节完全不动，方便将来上游更新时做「重新替换这 6 处」的机械操作（改动清单写进 §附录，便于 diff 校验）：

| # | 改动 | 原内容 → 新内容 |
|---|---|---|
| 1 | jQuery | `https://code.jquery.com/jquery-3.4.1.min.js` → `./vendor/jquery-3.4.1.min.js` |
| 2 | js-yaml | `https://cdn.jsdelivr.net/npm/js-yaml@4.1.0/dist/js-yaml.min.js` → `./vendor/js-yaml-4.1.0.min.js` |
| 3 | Bootstrap CSS | stackpath CDN → `./vendor/bootstrap-4.4.1.min.css` |
| 4 | Bootstrap JS | stackpath CDN → `./vendor/bootstrap-4.4.1.bundle.min.js` |
| 5 | Font Awesome CSS | cdnjs CDN → `./vendor/font-awesome-4.7.0/css/font-awesome.min.css` |
| 6 | 补丁入口 | `</body>` 前加一行 `<script src="./pixel-netio.js"></script>` |

> 第 1、2 条是**硬依赖**：子代理读源码确认整页用 `$` 绑事件、用 `jsyaml.load/dump` 解析与生成 yaml，任一 CDN 不可达即整页不可用。第 3~5 条挂了只是样式/图标退化。

### 6.2 `pixel-netio.js`：补丁机制（为什么必须在**同文档**里）

**关键事实（来自对 `editor.html` 的源码取证）：** 全部 JS 内联在**一个 IIFE** 里，状态（`yamlObj/pgm/mask/tool/brush/zoom`）与函数（`handleFiles/parsePGM/encodePGM/dlBytes/dlText/redrawMap`）都是闭包局部量，**没有挂到 `window`**。

推论：**「外部脚本替换内部函数」这条路根本不存在**（等于改源码）。补丁只能挂在 **DOM / 浏览器 API 层**。而跨源 iframe 里每个 realm 有各自的 `URL` 构造器，父页覆写不了子页的——所以补丁脚本必须与 `editor.html` **同文档**（第 6 处改动的那一行就是为此）。

**读通道（进）：** 注入文件对象。
```js
function inject(sel, file) {
  const dt = new DataTransfer();        // 每个 input 各建一个 dt，避免互相污染
  dt.items.add(file);
  const input = document.querySelector(sel);
  input.files = dt.files;               // 直接赋值 FileList（需 DataTransfer 构造器可用）
  input.dispatchEvent(new Event('change', {bubbles: true}));
}
inject('#yamlInput', new File([yamlBytes], `${name}.yaml`, {type: 'text/yaml'}));
inject('#pgmInput',  new File([pgmBytes],  `${name}.pgm`,  {type: 'application/octet-stream'}));
```
- 依据：`$('#yamlInput').on('change', e => handleFiles(e.target.files))`，pgm 同；`#drop` 的 drop 也汇入同一个 `handleFiles`。**不需要**先伪造 `dragover`。
- **必须 yaml 与 pgm 都注入**：两个下载按钮都有 `if(!pgm || !yamlObj){ alert('Load YAML and PGM first.'); return; }`。
- 文件名必须**以 `.pgm` 结尾**（`isPgm = /\.pgm$/i`）；且**名字里不能出现 `keepout`**（`isKeepoutName` 正则会把含 keepout 的一律当掩膜处理）。→ 后端限制 + 文档告知：不要把地图命名成带 `keepout` 的名字。
- 注入顺序：**base pgm 先、mask 后**（`loadBasePGM` 在尺寸不一致时会把 mask 重置成全 255）；`yaml` 与 `pgm` 之间无顺序要求。本轮不涉及掩膜文件，顺序无影响，但注释里留痕。

**写通道（出）：** 覆写下载。
```js
const blobURLs = new Map();                     // url -> Blob
const _create = URL.createObjectURL.bind(URL);
URL.createObjectURL = (blob) => { const u = _create(blob); blobURLs.set(u, blob); return u; };
URL.revokeObjectURL = () => {};                 // ← 必须 no-op：原实现紧随其后同步 revoke
const _click = HTMLAnchorElement.prototype.click;
HTMLAnchorElement.prototype.click = function () {
  if (this.download && /\.(pgm|ya?ml)$/i.test(this.download)) {
    const blob = blobURLs.get(this.href);
    if (blob) { upload(blob, this.download); return; }   // 不发真实下载
  }
  return _click.apply(this, arguments);
};
```
- 依据：两个下载入口都汇入 `dlBytes(bytes, filename, mime)` / `dlText(...)`：`new Blob` → `createObjectURL` → 生成游离 `<a href download>` → `a.click()` → 同步 `revokeObjectURL`；**无 `window.open`、无 `msSaveBlob`、无 `appendChild`**，通道单一封闭。
- **覆写时机**：补丁在文档尾部执行，用户点击必然发生在之后，时机足够。
- 若 `blobURLs` 查不到（上游改版），**兜底放行原始下载**并弹提示「补丁可能已失效，已退回浏览器下载」，不静默丢数据。

**状态条与保存目标：** 补丁在页面顶部插一条自己的状态栏（脱离 Bootstrap 样式，独立 `<div>`），显示：当前图名 / IO 模式（local·ssh）/ 是否离线缓存 / 保存目标（下拉：另存 `<名>_edited` ← 默认、覆盖原图）；并接管保存流程的提示与错误显示。

### 6.3 开发与生产接线

- `frontend/packages/mapeditor/vite.config.ts`：`base: "/mapeditor/"`、端口 `5175`、`proxy: { "/api": { target: process.env.VITE_API_TARGET || "http://127.0.0.1:8000", changeOrigin: true } }`。上位机开发时 `VITE_API_TARGET=http://100.65.82.93:8000`。
- `frontend/package.json` 加 `"dev:mapeditor": "pnpm --filter mapeditor dev"`。
- `server.py` 的 `_ADMIN_DIST/_KIOSK_DIST` 之后加 `_MAPEDITOR_DIST` 并挂 `/mapeditor`（`StaticFiles` 的 `html=True` 只能 index 回退，故本页用**显式文件名** `pixel-editor.html`，不做 SPA 路由）。

---

## 七、安全与保存策略

### 7.1 名字白名单（红线）

`^[A-Za-z0-9_-]{1,64}$`，且不含 `keepout`（§6.2）。`name` 与 `new_name` 都要过。用途：
- 防路径穿越（`../../etc/passwd`）；
- **防 ssh 子进程模式的命令注入**（§4.1）；
- 避免触发 GyroPalm 的 `keepout` 文件名误判。

拒绝时返回 `{"ok": False, "error": "地图名不合法"}` 并写 `map_edit_reject`。

### 7.2 保存策略：默认另存，覆盖要确认

| 模式 | 落盘 | 是否备份 | 触发条件 |
|---|---|---|---|
| `saveas`（默认） | `<原名>_edited.{pgm,yaml}` | 不备份（目标通常不存在；若已存在则先备份） | 页面默认 |
| `overwrite` | `<原名>.{pgm,yaml}` | **强制备份** | 页面勾选 + `confirm=true` |

**覆盖保存后，运行中的 Nav2 不会自动换图**：`map_server` 的地图是启动时一次性读入的，改完文件必须重启导航才生效。页面在覆盖保存成功后直接显示可一键复制的命令：`~/tools/nav_screen.sh nav <地图名>`（与第一期 §7.3 同一口径：后端**不**去 SSH 杀/起 screen 会话）。

### 7.3 体积与类型

| 项 | 上限/规则 | 超限行为 |
|---|---|---|
| `pgm` 解码后 | `MAPS_MAX_PGM_BYTES` = 10 MB | 413 |
| `yaml_text` | 64 KB | 413 |
| 上传体 `Content-Length` | 16 MB | 413 |
| `pgm` 魔数 | 接受 `P5`（二进制）与 `P2`（ASCII），其他魔数拒绝（沿用第一期 §7.4） | 400 |
| 未知字段 | yaml 只认一期的扁平 `map_saver` schema | 400 |

---

## 八、错误处理与降级

| 情况 | 行为 |
|---|---|
| `MAPS_IO=ssh` 且 paramiko 缺失、ssh 客户端不可用 | `available=False`；查询类接口 `{"ok": True, "status": "unavailable", "reason": "..."}`；写操作 `{"ok": False, "error": "..."}` |
| ssh 连不上 / 超时 | 读：有缓存 → 返回缓存 + `stale: True` + `cached_at`；无缓存 → `unavailable`。写：明确报错（含主机与错误串），**不做静默重试** |
| 板卡 maps 目录为空 | 列表返回空数组 + 页面提示「maps 目录为空」，不报错 |
| yaml 缺失对应 pgm | 列表该条标 `has_pgm: false` 并标红，其余条目照常 |
| yaml 字段非法/缺失 | 该图标 `meta_ok: false`，不参与第一期「当前地图识别」 |
| 上传 yaml 与磁盘原值除 `image` 外不一致 | **409 + 差异键列表**，写 `map_edit_reject` |
| 备份目录写不进去（只读挂载/权限） | **拒绝覆盖保存**（备份是覆盖的前置条件，不允许"备份失败但继续"）；另存模式可继续 |
| 写盘失败 | 返回含目标路径的错误字符串；`.tmp` 残留由下次写入覆盖，不做后台清理 |
| 本机没有地图目录却把 `MAPS_IO` 设为 `local`（默认已是 `ssh`，只有手工改回才会遇上） | 列表为空 + 提示「本机没有地图目录，请把 MAPS_IO 设为 ssh 或配置 MAPS_DIR」 |
| 补丁注入失败（上游改版） | 保留原生拖拽/下载能力可用，页面顶部红字提示「网络 IO 补丁失效」 |

---

## 九、已知坑清单（动手前先读这一节）

1. **两个 CDN 是硬依赖**：jQuery 与 js-yaml 不通 → 整页白屏。自托管是本加装项的第一件事，不是优化项。
2. **Font Awesome 的 CSS 用相对路径引 webfonts**（`../fonts/fontawesome-webfont.woff2` 等），必须把 `fonts/` 目录一并搬，否则图标全成空框。
3. **补丁必须与编辑器同文档**（IIFE 闭包 + 跨源 iframe 无法覆写 `URL`）。这一条决定了第 6.1 节第 6 处改动不是"可选优化"。
4. **`revokeObjectURL` 必须改成 no-op**：原实现点击后**同步** revoke，异步去取 `blob:` URL 会失败。
5. **必须 yaml+pgm 一起注入**，否则两个下载按钮都直接 `alert` 返回。
6. **文件名不能含 `keepout`**，否则被当掩膜，编辑的是另一张画布。
7. **yaml 被前端重新序列化**（键值保留、注释丢失）→ 后端以磁盘原文为本，只改 `image:` 一行（§五 5.2 第 7 步）。
8. **编辑器给出的 yaml 里 `image:` 是 `<名>_edited.pgm`**，与我们的目标名可能不一致 → 以第 4 步决定的目标名为准覆写。
9. **改完地图不等于生效**：`map_server` 启动时一次性加载，必须重启导航（§7.2）。
10. **未重扫的地图，修图收益有限**：现有三张图未知率 `my_map` 73.3% / `my_map2` 85.1% / `my_map3` 60.7%（一期 §十二）。手涂 85% 的未知区不现实——本加装项适合「走廊尽头一小块噪点」「门口漏了一堵墙」这种局部修补。**它替代不了重扫一张图。**
11. **备份目录会污染一期的地图列表**：`maps/.backup/` 必须在 `list()` 里排除（回填项，见 §十二）。
12. **`saveas` 生成的 `_edited` 图是新坐标系原点相同、但内容不同的图**，一期 `destinations`/`zones` 是按 `map_name` 绑定的，新图视为**空白图**（沿用一期 §5.1 `copy` 的既定语义：不复制地点/区域）。
13. **命令注入**：ssh 子进程模式拼接命令行，名字白名单是唯一防线（§7.1）。

---

## 十、配置项（`conf.py`）

| 键 | 默认 | 含义 |
|---|---|---|
| `MAPS_IO` | `"ssh"` | `ssh`（**默认**：PC 上跑后端，远程读写板卡——用户 2026-09-14 定「地图工作放到 PC 上」）\| `local`（备用：板卡上跑后端） |
| `MAPS_DIR` | `BASE_DIR / "ros2_car" / "maps"` | `local` 模式的根目录（一期已规划同一项） |
| `MAPS_SSH_HOST` | `"100.65.82.93"` | 板卡地址（Tailscale） |
| `MAPS_SSH_USER` | `"sunrise"` | 登录用户 |
| `MAPS_SSH_PORT` | `22` | 端口 |
| `MAPS_SSH_ROOT` | `"/home/sunrise/Robot/ros2_car/maps"` | 远程 maps 目录（**路径勘误：不是 `~/ros2/car_ws`**） |
| `MAPS_SSH_KEY` | `""` | 私钥路径（空则用 `~/.ssh/id_*` 默认） |
| `MAPS_SSH_PASSWORD` | `""` | 仅 paramiko 通道使用；**从 `.env` 读，不入 git** |
| `MAPS_SSH_TRANSPORT` | `"auto"` | `auto` \| `paramiko` \| `cli` |
| `MAPS_SSH_TIMEOUT` | `10` | 单次连接/命令超时（秒） |
| `MAPS_MAX_PGM_BYTES` | `10 * 1024 * 1024` | 上传 pgm 上限 |
| `MAPS_BACKUP_KEEP` | `10` | `.backup/` 保留组数 |
| `MAPS_CACHE_DIR` | `DATA_DIR / "mapcache"` | 远程模式本地缓存 |

> 密码走 `.env`（`MAPS_SSH_PASSWORD`），并确认 `.gitignore` 已排除 `.env`（AGENTS 已排除）。

---

## 十一、测试与验收

### 11.1 无网络、无板卡（Windows 主力口径）

> **测试策略（主形态是 ssh 之后尤其重要）：** `MapStore` 的两条实现要跑**同一套契约测试**（CRUD / rename / stat / `list()` 排除备份 / 名字白名单），`SshMapStore` 一侧通过注入指向 `127.0.0.1`（本机 OpenSSH server）或假 transport 的配置来跑，**不让单测依赖板卡**。板卡只出现在 §11.3 的真机验收里。

1. `LocalMapStore` 对临时目录的 CRUD / rename / stat 正确；`list()` **排除** `.backup/`；
2. 名字白名单：`../etc/passwd`、`a/b`、`a b`、`x"y`、`含keepout名`、超 64 字符 → 全部 400，且**断言没有任何文件被触碰**；
3. yaml 白名单校验：改 `resolution` 的请求 → 409 且列出 `resolution`；只改 `image` → 通过；
4. 保存流程：`saveas` 产出 `_edited.{pgm,yaml}`，`image:` 与文件名一致；`overwrite` 产出备份文件且备份内容 == 保存前的字节；
5. 备份保留：连存 12 次 → `.backup/` 只剩 10 组且删的是最旧的；
6. 体积上限：11 MB 的 pgm → 413；
7. `MAPS_IO=ssh` 但 `paramiko` 未安装 + `MAPS_SSH_TRANSPORT=cli` + 假 `ssh.exe`（一个假脚本）→ 走子进程路径；全不可用 → `available=False` 且**后端照常启动**（AGENTS 红线：可选依赖不得进顶层硬 import）；
8. 缓存降级：先成功读一次填充缓存 → 让 `stat()` 抛异常 → 接口返回缓存 + `stale: True` + `cached_at`；
9. `pixel-netio.js` 的纯函数部分（Blob→目标名映射、`_edited` 后缀处理、白名单前端预校验）用 Vitest 单测。

### 11.2 浏览器手工验收（无需板卡：临时用 `MAPS_IO=local` + `MAPS_DIR` 指向仓库副本，绕开板卡先把整条前端通路验完）

10. 断网（禁用网卡）打开 `/mapeditor/pixel-editor.html?map=my_map` → 页面正常、图标正常、地图自动加载；
11. 画一笔 → 点「Download Map」→ **无浏览器下载框**，页面显示保存成功与备份路径；磁盘上出现 `my_map_edited.pgm`；
12. 用一期 `mapserver.py` 重新渲染该图 → 修改的像素可见；
13. 勾选覆盖原图 → 保存 → 备份目录出现一组备份；页面给出 `nav_screen.sh nav` 命令；
14. 撤销重做、Un-Scan、矩形、量距各点一遍，确认功能未被补丁破坏。

### 11.3 板卡真机（需现场安全确认后再动）

15. `MAPS_IO=ssh`，上位机起后端 → `/api/mapeditor/io/test` 返回成功与耗时；
16. 上位机浏览器改一笔 → 保存 → 板卡上 `ls -l /home/sunrise/Robot/ros2_car/maps/` 复核，字节数与上位机一致；
17. 板卡上 `~/tools/nav_screen.sh nav my_map_edited` 起导航 → 车能按改后的图规划（**这一步才是像素修图的意义所在**）；
18. 拔掉 Tailscale/关机 → 页面仍能打开、显示缓存并标注「离线」；恢复后保存成功。

---

## 十二、与既有规格的关系（要做回填）

> **回填进度（2026-09-14 整理）**：一期 §2.2、§3 已在原文就地标注，其余（§5.1 排除 `.backup/`、§6 新资源登记、§〇 口径台账）已集中记录在一期新增的 **§〇 口径变更与加装项**；`AGENTS.md` 的「已知坑」与运行位置口径已补。**尚待**：一期 §十一 任务表补加装项引用、`ros2_car/建图与导航操作手册.md` 的「改完必须重启导航」一节、`2026-08-27` 规格的 vendor 说明。

| 既有文档 | 回填内容 |
|---|---|
| `2026-09-14-map-editor-design.md` §2.2 | 「像素级修图：本轮明确不做」→ 改为「**已由《2026-09-14-mapeditor-pixel-edit-design.md》作为加装项实现**」，并链过去 |
| 同上 §5.1 | `GET /api/map/list` 需排除 `maps/.backup/`；补 `POST /api/map/{name}/save` 与 `/api/mapeditor/io` 三条 |
| 同上 §3 | 原文「地图文件真相在板卡…后端在板卡上跑时自然指向正确目录；Windows 端用仓库里的副本开发」→ 改为「**后端主跑在 PC 上**（用户 2026-09-14：「还是把地图工作放到PC上吧，因为板卡性能不是很好」），地图文件真相仍在板卡、经 `MapStore(ssh)` 读写；仓库里的 `ros2_car/maps/` 降级为离线样本，不再当作开发期真相」 |
| 同上 §6 | 登记 `public/pixel-editor.html`、`public/pixel-netio.js`、`public/vendor/` 三处新资源与 `/mapeditor/pixel-editor.html` 路径 |
| 同上 §十一（任务表） | 新增一期之外的加装任务（本文 §十三），并注明「独立于一期其余任务，可先落地」 |
| `2026-08-27-frontend-multi-end-design.md` | 无需改动（`mapeditor` 包已登记）；仅补一句「含 vendor 静态资源，`pnpm build` 直接拷贝 `public/`」 |
| `ros2_car/建图与导航操作手册.md` | 补一节「**改完地图必须重启导航才生效**」+ 在浏览器里改图的入口与备份位置 |
| `AGENTS.md` | ①「已知坑」补一条：地图像素修图在 `/mapeditor/pixel-editor.html`，变更前自动备份到 `maps/.backup/`；②「快速上手」补一句后端运行位置口径：**默认跑在 PC 上**，地图文件在板卡、经 `MAPS_IO=ssh` 读写 |

---

## 十三、任务分解（粗粒度，供实现计划展开）

**阶段一 · `ssh` 主通路（PC 上跑后端——用户 2026-09-14 定的主形态）**

> 任务 1~4 与板卡解耦：`SshMapStore` 先对着 `127.0.0.1` 或假 transport 跑通，**不等板卡**；任务 5~9 是纯前端与本地落盘逻辑。也就是说**除了任务 11~12 的真机验收，整条链路都能在板卡不可达的情况下开发和验证完**。

| # | 任务 | 依赖 |
|---|---|---|
| 1 | `LLM/mapstore.py`：`MapStore` 协议 + `LocalMapStore`（纯 stdlib，先做它当契约基准与备用形态）；若一期未开工，附带 `GET /api/map/list`、`GET /api/map/{name}/download` 两条最小只读路由 | — |
| 2 | `SshMapStore`：paramiko 通道（可选依赖、try/except） | 1 |
| 3 | `SshMapStore`：`ssh.exe/scp.exe` 子进程通道（密钥认证，`MAPS_SSH_TRANSPORT=cli`） | 1 |
| 4 | 本地缓存与新鲜度判定 + 离线降级（§4.2）；`GET /api/mapeditor/io`、`POST /api/mapeditor/io/test` | 2、3 |
| 5 | 自托管 5 个 CDN 资源到 `public/vendor/`（含 Font Awesome `fonts/` 与 MIT LICENSE）；核对离线可用 | — |
| 6 | 复制 `editor.html` → `public/pixel-editor.html`，按 §6.1 改 6 处 | 5 |
| 7 | `public/pixel-netio.js`：读通道注入 + 写通道覆写 + 状态条（显示 IO 模式 / 离线标注 / 保存目标） | 4、6 |
| 8 | `POST /api/map/{name}/save`：白名单、体积、yaml 白名单校验、备份、原子写、审计 | 4 |
| 9 | `frontend/packages/mapeditor` 骨架（vite、base、proxy、`dev:mapeditor`、`server.py` 挂载） | — |
| 10 | 测试 §11.1 的 1~9 + 浏览器验收 §11.2（用 `MAPS_IO=local` 绕开板卡） | 1~9 |

**阶段二 · 板卡真机与备用形态**

| # | 任务 | 依赖 |
|---|---|---|
| 11 | 板卡真机验收 §11.3（**板卡可达是前置，当前实测不通**，见 §十四） | 10 |
| 12 | 备用形态在板卡上跑一次回归（`MAPS_IO=local`），确认与主形态行为一致 | 11 |

**阶段三 · 收尾**

| # | 任务 | 依赖 |
|---|---|---|
| 13 | 文档回填（§十二） | 全部 |
| 14 | `requirement.txt` 增补 `paramiko`（**可选依赖，注释说明可缺**） | 2 |

---

## 十四、动手前的前置条件（不解决则这次加装收益有限）

1. **板卡可达（主形态的第一前置）**：`ssh sunrise@100.65.82.93` 与 Tailscale 必须通。**本文档编写时实测失败**——`ssh: connect to host 100.65.82.93 port 22: Connection timed out`（板卡未开机或 Tailscale 未登录）。主形态的整个 IO 都压在这条上，**真机验收前必须恢复**；开发期可用 `MAPS_IO=local` + `MAPS_DIR` 指向仓库副本先行推进（任务 1~10 不受影响）。
2. **ssh 认证方式要先定**：`cli` 通道（Windows 自带 `ssh.exe`/`scp.exe`，零依赖）**只支持密钥**——Windows 没有 `sshpass`，明文密码无法自动化；要用它，需先给 PC 配一次公钥到板卡 `~/.ssh/authorized_keys`。paramiko 通道则可以吃密码（`MAPS_SSH_PASSWORD` 从 `.env` 读，不入 git）。
3. **重扫一张地图**（一期同款前置）：现有三张图未知率 60.7%~85.1%，"没扫到"占了绝大多数。像素修图能擦噪点、补漏墙、把**小片** unknown 涂成 free，但没法凭空扫出一栋楼。**先重扫，再修图。**
4. **`ros2_car/maps/` 里有脏文件**（`my_map2`/`my_map3` 未入 git）：本加装项的 `list()` 必须容错，且 `.backup/` 要在列表里排除。

---

## 附录 · 上游归属与改动清单

- **上游**：GyroPalm/ROS-SLAM-Map-Editor，作者 Dominick Lee（GyroPalm, LLC），**MIT License**，2025。
  - 引用格式（README 要求）：Lee, Dominick. (2025). *ROS SLAM Map Editor* [Computer software]. GyroPalm, LLC. https://github.com/GyroPalm/ROS-SLAM-Map-Editor
  - `editor.html` 的 MIT 原文必须落到 `public/vendor/ROS-SLAM-Map-Editor.LICENSE`，并在 `pixel-netio.js` 头部注明"本页为改造自上述项目的衍生作品"。
  - **只改 6 处**（§6.1）：5 条 CDN URL + 1 行补丁 `<script>`。若将来同步上游，按此 6 处重做即可，其余字节保持原样以便逐字节 diff。
- 备选（本轮不用，留档）：[vdovetzi/oge](https://github.com/vdovetzi/oge)（Python 纯 stdlib + Tk 桌面版，MIT，约 26 KB，含单测）——若将来想要一个**不依赖浏览器的命令行修图工具**，它的 PGM/YAML 读写（`oge/model.py`）是现成参考；[Tony-tpc/map_edit](https://github.com/Tony-tpc/map_edit)（RViz2 插件，无 license，需要 ROS 环境）。
