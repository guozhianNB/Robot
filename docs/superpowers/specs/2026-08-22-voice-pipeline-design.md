# 语音交互子系统设计规格（ASR / TTS / VAD / 关键词唤醒 / 声纹）

> 日期：2026-08-22 ｜ 状态：待用户审查 ｜ 范围：LLM/ 语音链路
> 关联：`docs/目标文档及说明/大模型端开发目标.md` 模块 7（语音链路）、模块 4（身份确认）、决策表

## 1. 目标与范围

打通"语音进 → 文字出 → 大模型处理 → 语音播报"完整回路，并实现：

- **语音输入（ASR）**：中文流式识别，边录边出字
- **TTS**：中文语音合成与播放
- **VAD**：语音活动检测 —— 说话切句 + 播报打断（barge-in）
- **关键词唤醒（KWS）**：混合模式唤醒词
- **声纹识别**：说话人验证（1:1，是不是登记过的老人）+ 说话人识别（1:N，是哪一位）

**范围外（本期不做，接口预留）：**

- 视觉人脸识别（身份融合层的第二路信号源，本期只接声纹一路）
- 方言识别（需求文档已定后置）
- 云 ASR/TTS SDK（讯飞/百度）——仅作将来降级备选，本期全部本地
- 语音在 RDK X5 上的实际部署（本期 Windows 开发机跑通，移植要点见 §9）

## 2. 已确认决策记录

| # | 决策点 | 结论 |
|---|--------|------|
| D1 | 交互模式 | **混合模式**：唤醒词激活 + 30s 免唤醒连续对话窗口，超时回待机 |
| D2 | 声纹注册方式 | **固定注册**：对麦录 10~30s → VAD 切句 → 逐句提特征 → 平均建档案 |
| D3 | 声纹需求 | **验证 + 识别都要**（先验证是不是家里人，再分辨是哪一位） |
| D4 | 技术选型 | **sherpa-onnx 主干**（ASR/TTS/VAD/KWS）+ **独立声纹库**（3D-Speaker ERes2NetV2，要精准） |
| D5 | 架构形态 | **方案 C 双形态**：`LLM/voice/` 纯逻辑子包，开发期内嵌 server.py，上板可切独立进程 |
| D6 | 开发环境 | 先 Windows 开发机跑通（sounddevice 笔记本麦克风），再移植 RDK X5 |
| D7 | 稳健性（硬要求） | 音频模块掉线/异常，**主程序绝不崩溃**，逐模块降级 |
| D8 | 身份判定 | 最终 uid 由**声纹 + 人脸融合层**裁决；本期只实现声纹路，人脸路接口预留（视觉后置） |
| D9 | 演示档位 | 预置 2 个测试声纹档案，演示"识别出是谁" |

## 3. 架构形态

```
LLM/voice/                  # 语音子包 —— 纯逻辑，不依赖 FastAPI
  __init__.py
  config.py      # 语音专属配置：模型路径、阈值、超时、唤醒词
  audio.py       # 音频采集/播放抽象（sounddevice）—— 唯一接触声卡的模块
  vad.py         # VAD 切句（silero-vad ONNX，~2MB）
  kws.py         # 关键词唤醒（sherpa-onnx kws-zipformer，中文 3.3M）
  asr.py         # 流式识别（sherpa-onnx zipformer-zh，~60MB）
  tts.py         # 合成+播放（sherpa-onnx vits-zh，~190MB）
  speaker.py     # 声纹（3D-Speaker ERes2NetV2，torch，192 维）
  identity.py    # 身份融合层（本期：声纹单路；预留人脸信号源）
  session.py     # 会话状态机：IDLE→LISTENING→SPEAKING
  worker.py      # 后台线程 + 心跳状态 + 崩溃兜底（稳健性核心）
LLM/voice_api.py     # 内嵌形态：FastAPI 挂载层（端点 + 设置开关联动）
run_voice.py         # 部署形态：独立进程入口（上板时用，可选）
LLM/data/speakers/   # 声纹档案存储 <uid>.npz（embedding + 元信息）
```

**分层原则**：`voice/` 内模块互不依赖 FastAPI；`voice_api.py` 是唯一 FastAPI 触点；`worker.py` 管理音频线程生命周期。这样 Windows 内嵌 / 板子独立进程共用同一套 `voice/` 代码。

## 4. 状态机与数据流

### 4.1 状态机

```
IDLE ──唤醒词命中──▶ LISTENING ──一句话结束──▶ SPEAKING
 ▲                     │  ▲                        │
 │                     │  │ VAD 检测到人声（打断）   │
 │                     │  └────────────────────────┘
 │                     ▼
 │                30s 无人说话（超时）
 └─────────────────────┘
```

- **IDLE**：待机，只跑 VAD + KWS 听唤醒词（低功耗，模型最小化）
- **LISTENING**：30s 免唤醒窗口，流式 ASR 持续出字，按 VAD 切句
- **SPEAKING**：TTS 播报中；VAD 检测到人声 → 立即停播回 LISTENING（barge-in）
- 任意状态异常 → 回 IDLE 并记审计

### 4.2 数据流（一条语音的旅程）

```
麦克风 ─▶ VAD 分帧 ─┬─ IDLE: KWS 唤醒词检测 ──命中──▶ 进入 LISTENING
                    └─ LISTENING: 流式 ASR 出字 ──句子边界──▶
  句子音频 ─▶ 声纹提取 ─▶ 身份融合层裁决（本期=声纹路）──▶ uid
  uid + 文本 ─▶ 调用现有 /api/chat（SSE 流式）──▶ 回复文本
  回复文本 ─▶ TTS 合成 ─▶ 播放（SPEAKING，可被打断）
```

复用现有 `chat.py::chat_stream` 与 `bus.py`，语音链路只做"入口"和"出口"，不改对话编排。

## 5. 声纹模块（精准优先）

- **模型**：3D-Speaker **ERes2NetV2**（ModelScope `damo/speech_eres2netV2_sv_zh-cn_16k-common`，34.3M 参数，192 维 embedding，短语音验证友好，中文评测 SOTA 级）
- **推理**：torch（Windows 开发机）；上板时导出 ONNX 用 onnxruntime（3D-Speaker 官方支持 ONNX Runtime 部署，免装 torch，见 §9）
- **注册（enroll）**：录 10~30s → VAD 切成若干句 → 每句提取 embedding → **平均合并**为档案 embedding，存 `LLM/data/speakers/<uid>.npz`（含元信息：注册时间、句子数、uid）
- **验证（verify，1:1）**：`cosine(当前句, 档案) ≥ spk_threshold`（默认 **0.55**，可调）→ 确认是登记过的老人；官方模型基线阈值 0.360，因"精准优先"（宁可拒识不误识）默认上调，开发期实测校准
- **识别（identify，1:N）**：对全部档案算 cosine → 最高分 ≥ 阈值 → 判为某人；否则"未知说话人"（不切 uid、按陌生人处理）
- **阈值校准**：开发期用"同人自比 / 异人对比"实测校准默认阈值，写入配置
- **性能**：192 维 embedding 对比开销可忽略；模型推理在 8 核 A55 上 CPU 实时（单句 <100ms 量级）

## 6. 身份融合层（D8：声纹 + 人脸，本期只接声纹）

`identity.py` 定义统一裁决入口：

```python
class IdentitySource(Protocol):
    name: str                                # "voiceprint" / "face"
    def probe(self, ctx) -> IdentityVote:    # 返回 (uid | None, confidence)
```

- **`IdentityVote`**：`(candidate_uid, confidence)` 结构化投票
- **融合裁决**：本期实现 `VoiceprintOnlyFusion`（声纹单路：验证 + 识别 + 置信度阈值）；未来加 `FaceSource` + `WeightedFusion`（多源加权/最高分裁决），**接口不变，零改造接入**
- **宁问勿猜**（对应需求文档模块 4）：置信度低 → 不切换 uid，或机器人反问"是张爷爷吗？"；裁决结果联动 `/api/chat` 的 uid 参数
- 声纹模块掉线 → 融合层降级"跳过身份判定，保持当前 uid"

## 7. 稳健性（硬要求，D7）

### 7.1 线程级兜底

- `worker.py` 所有音频线程的**循环体整体 try/except**：任何异常只落审计日志 + 状态置 `degraded`，**绝不向 FastAPI 事件循环抛出** → 主程序不崩
- 服务关闭：`finally` 关音频流、释放模型，优雅停线程（`threading.Event` 协作退出）

### 7.2 心跳与状态

- worker 每秒上报状态：`running / degraded / stopped`
- `GET /api/voice/status` 可查；状态变化经 `bus.py` 广播，前端 toast"语音服务异常"

### 7.3 设备掉线自愈

- 拔麦 / 设备占线 / sounddevice 异常 → 捕获 → **退避重连**（最多 N 次，指数退避）→ 仍失败 → 降级"无声模式"：网页聊天照常，语音开关置灰

### 7.4 逐模块降级矩阵（对齐需求文档既有决策）

| 故障 | 行为 |
|------|------|
| 声卡/采集挂 | 无声模式：网页文字聊天照常，语音开关置灰，持续重试 |
| ASR 挂 | 语音入口失效，网页打字可用；语音模块整体置 degraded |
| TTS 挂 | 播报降级为前端 toast（现有提醒播报路径） |
| VAD 挂 | 关打断：播报不被打断；切句退化按固定时长 |
| 声纹挂 | 跳过身份判定，保持当前 uid（宁问勿猜） |
| KWS 挂 | 退化为纯 VAD 触发（临时），或整体待机并告警 |

**原则：任何语音子模块故障都不影响现有网页对话、提醒、告警功能。**

## 8. 配置与集成

### 8.1 conf.py 新增

```python
VOICE_ENABLED = True             # 即设置项 voice_enabled 的默认值
WAKE_WORD = "小机器人"           # 可配
HANDFREE_SECONDS = 30            # 免唤醒窗口
SPK_THRESHOLD = 0.55             # 声纹余弦阈值
VOICE_MODEL_DIR = BASE_DIR / "LLM" / "models" / "voice"
VOICE_SAMPLE_RATE = 16000
```

### 8.2 设置页开关（占位变真实）

`asr_enabled` / `tts_enabled` 从占位开关接真实服务；新增 `voice_enabled`（总开关）、`wakeword`、`handsfree_seconds`、`spk_threshold`。开关持久化（复用现有设置机制），重启不丢。

### 8.3 审计事件（log.py 扩展）

新增事件类型：`voice_wake` / `voice_asr` / `voice_spk`（验证/识别结果与置信度）/ `voice_tts` / `voice_error`（含模块名与异常摘要）。

### 8.4 新端点（server.py / voice_api.py）

- `POST /api/voice/enroll` — 触发录音建档（对麦录 10~30s，body 含 uid；录音在语音服务侧完成，不做音频上传）
- `GET /api/voice/status` — 语音服务状态（心跳 + 各子模块状态）
- `GET /api/voice/speakers` — 已注册档案列表

### 8.5 依赖（requirement.txt 固化）

`sherpa-onnx`、`sounddevice`、`onnxruntime`、`torch`（CPU）、`modelscope`（仅开发期拉模型，可拆）—— 具体版本实现计划里定。

## 9. 移植要点（RDK X5，本期不落地）

- 音频：sounddevice 在 aarch64 可用（ALSA 后端），接口已抽象，换板子只改 `audio.py` 设备名
- 声纹：ERes2NetV2 **导出 ONNX → onnxruntime 推理**，板子免装 torch；模型 34.3M 参数，8 核 A55 CPU 实时
- ASR/TTS/VAD/KWS：本就是 ONNX，直接跑 onnxruntime，跨平台无差异
- 部署形态：`run_voice.py` 独立进程常驻，通过内部 HTTP 调 LLM server
- 验证手段：板子实测各模块单步延迟（目标：VAD 打断 ≤500ms，端到端 ≤2s 由需求文档既有优化路径保障）

## 10. 测试与里程碑

### 10.1 单元测试

- VAD 切句边界（静音/噪声/短句）
- 声纹：同人自比 ≥ 阈值、异人对比 < 阈值（阈值校准数据）
- 状态机：唤醒命中、30s 超时回 IDLE、播报中打断
- 降级：各子模块抛异常 → 状态 degraded、主线程存活

### 10.2 集成实测（Windows）

1. 完整链路："喊小机器人 → 说话 → 识别出 1 号 → 播报回应"
2. **拔麦/停音频设备 → 验证主程序不崩**、状态降级、恢复后自愈
3. 两个档案演示："识别出是 1 号还是 2 号"
4. 低置信度场景：不切 uid 或反问

### 10.3 里程碑

| 阶段 | 内容 | 验收 |
|------|------|------|
| M1 | 音频采集 + VAD + KWS | 唤醒词能唤醒、切句正确 |
| M2 | ASR + TTS | 语音→文字→播报闭环 |
| M3 | 声纹注册/验证/识别 + 融合层 | 1:1 与 1:N 正确，两个档案可区分 |
| M4 | 集成 server.py + 稳健性加固 | 掉线不崩、降级矩阵生效、设置页开关真实 |
| M5（可选） | RDK X5 移植 | 板子跑通完整链路 |

## 11. 风险与对策

| 风险 | 对策 |
|------|------|
| 中文唤醒词 KWS 模型效果不佳 | 备选：流式 ASR + 首词关键词匹配做唤醒（零额外模型） |
| ERes2NetV2 torch 依赖重 | 上板前导出 ONNX（官方支持）；开发期 torch CPU 即可 |
| 笔记本麦克风近场 vs 板子远场差异 | 架构按远场预留（VAD 阈值、增益可配）；板子阶段再实测调参 |
| 唤醒误触发（电视声等） | 混合模式窗口限制 + 声纹验证兜底 + 阈值可调 |
| 2s 延迟目标 | LLM 走云端不变，本地环节全部流式/低延迟选型；M2 实测分解 |
