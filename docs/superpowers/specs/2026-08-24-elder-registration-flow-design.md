# 老人注册流程 + 身份样本管理 设计

> 日期：2026-08-24
> 状态：已与用户确认（原型 v2 + 后端两步式 API）
> 范围：前端注册向导（4 步）、记忆页身份样本管理（追加/清除声纹）、后端两步式声纹 API + 人脸占位

## 1. 背景与目标

护士在控制台（`UI/index.html`，单文件 SPA）为**新入住的老人**完成身份注册：

1. 录入老人基本信息、基本喜好与备注；
2. 让老人对着机器人录制声纹（服务端麦克风）；
3. 人脸录入 —— **本期只做空壳**（人脸识别后续接入，想法备忘见 `docs/temp/face-recognition-notes.md`）；
4. 提示注册成功，自动切换到新老人。

同时，记忆页需要能对**已有老人**追加声纹样本（合并平均，越录越准）或清除声纹档案；人脸"追加照片"入口本期置灰（占位）。

## 2. 已确认的关键决策

| 决策点 | 结论 |
|---|---|
| 采集端 | **服务端采集为主**：声纹走现有服务端麦克风（`/api/voice/enroll` 同链路）；人脸本期不接采集，只留空壳 |
| 追加声纹语义 | **合并平均**：新录音特征与已有档案加权平均，档案单文件不变，识别路径不变，额外记录样本计数 |
| 实现方案 | **方案 A：前端向导编排**，逐步复用/扩展现有 API（profiles + voice），不建后端注册会话 |
| 声纹交互 | **两步式**：录制（暂存）与入档（保存/丢弃）分离，支持"录后重录/保存"；面板提供"追加/清除"两个操作 |
| 注册入口 | 顶栏「➕ 注册老人」按钮（任何页面可见）+ 记忆页档案卡片内「🎙️ 追加声纹样本 / 📷 追加人脸照片」 |
| UID | 自动生成 `elder_00N`（现有最大编号 +1），可手改 |
| 注册后 | 自动切换当前老人到新 uid |
| 人脸 | 占位步骤 + 后端 `GET /api/face/status` 占位路由；追加照片按钮置灰 |

## 3. 后端改动

### 3.1 声纹两步式 API（`voice_api.py` / `server.py` / `speaker.py` / `conf.py`）

**`speaker.py`：声纹档案格式升级（向后兼容）**

- 新格式：`np.savez(path, emb=profile, count=n)`；旧 npz 只有 `emb`，`_load()` 读不到 `count` 时视为 `1`。
- 新增纯函数便于测试：`merge_profile(old: np.ndarray|None, old_count: int, new: np.ndarray) -> (np.ndarray, int)`
  - 无旧档案：`(new, 1)`；
  - 有旧档案：`new_count = old_count + 1`，`merged = (old * old_count + new) / new_count`，再归一化。
- `SpeakerRecognizer.enroll(uid, segments, append=False)`：
  - `append=False`：直接平均本次 segments → 覆盖档案，`count = 1`；
  - `append=True`：先算本次均值 `new`，再 `merge_profile(现有档案, 现有count, new)` 写回。
  - 更新内存 `self._profiles[uid]`。
- 新增 `SpeakerRecognizer.delete(uid)`：删除 `data/speakers/{uid}.npz`，从 `self._profiles` 移除。
- 新增 `SpeakerRecognizer.sample_count(uid) -> int`：读 npz 的 count（无档案返回 0）。

**`voice_api.py`：录制/入档分离**

- `record_speaker(seconds) -> dict`：录制 → VAD 切段 → 提特征（本次均值 emb + segments 数），**同时保留原始音频**（wav bytes），**暂存内存**：
  - 模块级 `_pending: dict[recording_id, {"emb", "segments", "wav", "ts"}]`，recording_id 用 `uuid4().hex`；
  - 录制前清理超过 `VOICE_PENDING_TTL_S`（默认 600s）的旧暂存；
  - 返回 `{"ok": True, "recording_id", "segments"}`；语音不可用/无有效语音段时返回错误（`ok: False`）。
- `get_recording_audio(recording_id) -> (bytes, str) | None`：取暂存 wav bytes（供试听接口），不存在返回 None。
- `commit_speaker(recording_id, uid, append) -> dict`：取暂存 emb → `enroll(uid, [emb], append=append)` 落档 → 删除暂存 → 返回 `{"ok": True, "uid", "samples": count}`；暂存不存在返回错误。
- `discard_recording(recording_id) -> dict`：删除暂存（幂等）。
- `delete_speaker(uid) -> dict`：`_recognizer.delete(uid)` + audit。语音不可用时降级返回 `{"ok": False, "error": ...}`。
- `list_speakers()` 保持返回 uid 字符串列表（`get_status()` 的 `speakers` 字段与旧前端依赖它）；新增 `list_speaker_details() -> {uid: {"samples": n}}` 供前端渲染样本数。

**`server.py` 路由**

| 方法 | 路径 | 体/参数 | 返回 |
|---|---|---|---|
| POST | `/api/voice/record` | `{uid?, seconds}`（uid 仅审计用） | `{ok, recording_id, segments}` |
| GET  | `/api/voice/record/{recording_id}/audio` | — | `audio/wav` 试听音频（暂存存在时；过期/不存在返回 404 JSON） |
| POST | `/api/voice/enroll` | `{uid, recording_id, append}`（`append` 默认 True；**向后兼容**：无 `recording_id` 时回退旧行为：直接录 seconds 秒建档） | `{ok, uid, samples, segments?}` |
| DELETE | `/api/voice/record/{recording_id}` | — | `{ok}` |
| DELETE | `/api/voice/speakers/{uid}` | — | `{ok, uid}` |
| GET | `/api/voice/speakers` | — | `{ok, speakers: [...], details: {uid: {samples: n}}}`（旧 `speakers` 字符串数组保持兼容，前端新代码用 `details`） |

- 审计：`log("voice_spk", action="record"|"commit"|"discard"|"delete", uid, segments, append)`；异常走 `log("voice_error", ...)` + `{"ok": False}`。

**`conf.py`**：新增 `VOICE_PENDING_TTL_S = 600`、`VOICE_ENROLL_SECONDS = 15`（默认录制时长）。

### 3.2 人脸占位

- `server.py`：`GET /api/face/status` → `{"ok": True, "status": "unavailable", "reason": "人脸录入尚未接入（占位接口，见 docs/temp/face-recognition-notes.md）"}`。
- 不新增表/存储；前端据此渲染置灰。

### 3.3 不动的东西

- `db.py` 不加表（声纹档案在文件系统，与现状一致）；
- `identity.py` 融合层不动（人脸 FaceSource 后续再加，接口已预留）；
- `worker.py` 识别链路不动。

## 4. 前端改动（`UI/index.html`）

### 4.1 顶栏

- 新增按钮「➕ 注册老人」（绿色，放在"模块状态"左侧），点击打开注册向导 modal。

### 4.2 注册向导（modal，复用现有 `openModal` 弹窗体系，4 步状态机）

- 步骤指示器：① 基本信息 → ② 声纹 → ③ 人脸（占位）→ ④ 完成。
- **步骤 1：基本信息**（护士录入）
  - 字段：UID（自动生成 elder_00N，可改）、姓名*、称呼、床位、年龄、性别、生日、偏好称呼、喜欢话题、说话风格画像、备注；
  - 病史/用药不进向导（注册完成后在记忆页档案表单补录）；
  - 「保存档案 → 下一步」调 `POST /api/profiles`（复用现有 upsert）。
- **步骤 2：声纹录制**
  - 显示提示"请老人对着机器人说话"+ 预计时长（15s）；
  - 「开始录制」→ `POST /api/voice/record {seconds}` → 倒计时 UI（前端定时器模拟，服务端实际录满返回）→ 完成显示检测到的语音段数；
  - 结果态按钮：「🔊 试听」（`GET /api/voice/record/{id}/audio`，`<audio>` 播放，确认老人说话清晰）「🔁 重录」（丢弃本次暂存 `DELETE /api/voice/record/{id}` 或直接再录，旧暂存自动作废）与「💾 保存」（`POST /api/voice/enroll {uid, recording_id, append:false}` 首次建档）；
  - **失败可跳过**：语音模块不可用（`ok: False` + error）时提示，并提供「跳过声纹（稍后在记忆页追加）」继续下一步 —— 注册流程不卡死（符合 AGENTS.md 稳健性）。
- **步骤 3：人脸（占位）**
  - 调 `GET /api/face/status`；展示"摄像头录入人脸 — 功能尚未接入"，拍照按钮禁用；
  - 「下一步」继续（本期恒可跳过）。
- **步骤 4：完成**
  - 汇总：姓名 / uid / 声纹（已建档 N 次样本 或 未建档）/ 人脸（待接入）；
  - 「完成」→ 关闭弹窗、`currentUid` 切换为新 uid、刷新 `loadProfiles()` / `loadMemory()` / 对话历史。

### 4.3 记忆页档案卡片：身份样本管理

- 档案卡片（`📋 老人档案`）顶部新增「身份样本」区块：
  - 状态徽标：`🎙️ 声纹已建档（N 次样本）` / `🎙️ 声纹未建档`（数据来自 `GET /api/voice/speakers` 的 `details`）；`📷 人脸未接入`（灰）。
  - 按钮：「🎙️ 追加声纹样本」「📷 追加人脸照片（未开放，置灰）」。
- **追加声纹面板**（modal，对应 v2 原型三个状态）：
  - 初始：显示当前样本数 + 两个选项卡片「➕ 追加样本」「🗑️ 清除档案」；
  - 追加样本 → 录制态（`POST /api/voice/record`）→ 结果态「🔊 试听 / 🔁 重录 / 💾 保存」（保存时 `append: true` 合并平均）→ 成功 toast + 刷新样本数；
  - 清除档案 → `confirm()` 确认 → `DELETE /api/voice/speakers/{uid}` → toast + 刷新。

### 4.4 其他

- `loadMemory()` 加载档案时并行拉取 `GET /api/voice/speakers` 渲染身份样本状态；
- 错误处理：每步独立 try/catch，`toast(title, detail, "err")` 提示；语音模块不可用提示沿用后端返回的 error 文案。

## 5. 数据流

```
注册：填表 → POST /api/profiles（建档）
    → POST /api/voice/record（暂存音频+特征）→ [试听/重录循环] → POST /api/voice/enroll {append:false}（建档）
    → GET /api/face/status（占位确认）
    → 完成：切换 currentUid、刷新页面数据

追加：记忆页按钮 → POST /api/voice/record → [试听/重录循环] → POST /api/voice/enroll {append:true}（合并）→ 刷新样本数
试听：GET /api/voice/record/{recording_id}/audio（暂存 wav，播放确认清晰度）
清除：记忆页按钮 → confirm → DELETE /api/voice/speakers/{uid} → 刷新
```

## 6. 错误处理与降级

- 语音依赖缺失（`_VOICE_AVAILABLE=False`）：record/enroll/delete 返回 `{"ok": False, "error": "语音模块不可用（缺少依赖：…）"}`；前端注册向导允许跳过声纹，追加面板提示不可用并禁用录制按钮；`/api/voice/speakers` 查询仍返回 `ok: True` + 空 details（服务健康 ≠ 功能可用）。
- 录制无有效语音段：`ok: False` + 提示"没有检测到有效语音，请靠近麦克风再说一遍"，前端提供重试。
- 暂存 TTL 过期 / recording_id 不存在：`ok: False` + "录音已过期，请重新录制"（试听接口返回 404）。
- 所有后端异常：`audit.log("voice_error"/"memory_change", ...)` + 结构化错误返回，后台线程不崩。

## 7. 测试

- `LLM/tests/test_speaker_math.py`：新增 `merge_profile` 纯函数用例（无旧档案 / 合并平均数值正确 / 归一化）。
- `LLM/voice_api.py` 的 record/commit/discard/delete 逻辑：语音不可用时降级路径（已有 test_backend 风格，可加 `test_voice_api_degraded.py` 断言 `_VOICE_AVAILABLE=False` 时各函数返回 ok:False）。
- 手工验证（前端）：注册向导全流程（含跳过声纹）、追加/清除声纹、试听播放、顶栏入口、完成后自动切换。
- 兼容性：旧 npz（无 count）加载 → 视为 1 次样本；`/api/voice/speakers` 旧字段不变。

## 8. 范围外（YAGNI）

- 不做后端注册会话（方案 C 否决）；
- 不做人脸采集/识别/存储（本期占位；想法见 `docs/temp/face-recognition-notes.md`）；
- 不做按单张样本的删除 UI（"清除" = 清除该老人的整份**声纹**档案后重新录制建档；老人基本信息档案不受影响）。
