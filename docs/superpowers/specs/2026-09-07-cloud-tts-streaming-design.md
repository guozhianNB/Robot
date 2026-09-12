# 云端 TTS（火山豆包语音 2.0）+ 句级流式播报设计

日期：2026-09-07 ｜ 状态：设计定稿（待用户审查）｜ 关联：docs/log.md 2026-09-07 云端 ASR 条目（本设计为 TTS 侧的对称演进）

## 一、目标与验收

1. **本地 / 云端 TTS 都流式输出**：LLM 回复不再等整段结束才合成，而是边生成边按句切分、逐句合成、逐句连续播放（句级流式流水线）；云端引擎音频分片返回即入播放队列。
2. **LLM 流式内容实时在前端显现**：语音链路问答时，kiosk 对话区以打字机效果实时增长助手气泡（手动打字链路 admin ChatPage / kiosk sendText 已流式，本次不动）。
3. **切换按钮选择本地/云端 TTS，重启生效**：完全照抄 asr_provider 既有三件套模式（conf 默认设置 → worker 启动读取 → 前端单选+快捷切换，标注"重启服务后生效"）。

验收方式：后端语音链路真机问答回复边说边播、kiosk 气泡逐字增长、设置内切换本地/云端后重启服务生效、/api/voice/status 显示实际 tts 引擎；后端在云端不可用时回退本地不降级整机；`LLM/tests` 全量回归绿。

## 二、现状关键点（本次改造的落点）

- `worker._handle_speech` 通过 `chat_fn`（voice_api._chat_fn）**同步收完整段** LLM 回复，整段 `tts.synthesize()`（sherpa OfflineTts 一次性生成）→ `sink.play()` 一段一播。回复过程中 kiosk 看不到内容增长。
- 识别为整句 → chat_new（user+assistant 整段）→ kiosk 一次性 push 两条。
- `AudioSink.play()` 每次先 `stop()` 旧输出流 —— 一段一播、可打断，无法句间无缝拼接。
- 打字链路（前端直接 fetch /api/chat SSE）已逐 content 事件渐进显示。
- asr_provider 三件套（conf.DEFAULT_SETTINGS → worker._build_runtime 读取→kiosk 单选+底部切换按钮+alert 重启生效；/api/voice/status 显示 modules.asr）即本次 tts_provider 的模板。

## 三、云端协议事实（TTS 2.0 双向 WebSocket）

来源：豆包语音官方文档（volcengine docs 6561/2532486）+ GizClaw/doubao-speech-go `docs/tts_v2.md`。

- 端点：`wss://openspeech.bytedance.com/api/v3/tts/bidirection`（与 ASR 的 sauc/bigmodel 不同端点）。
- 鉴权：握手 header `X-Api-Key`（豆包语音控制台 API Key，与云端 ASR 同一把）+ `X-Api-Resource-Id`（默认 `seed-tts-2.0`，支持豆包 TTS 2.0 音色）+ 可选 `X-Api-Connect-Id`。
- 事件模型（客户端 JSON 事件）：`StartConnection` → `StartSession{session_id, req_params{speaker, model, audio_params, additions}}` → 可多次 `TaskRequest{text}`（流式文本输入）→ `FinishSession` → 同连接可顺序复用下一会话（`StartNextSession`）。
- 服务端事件：`ConnectionStarted/ConnectionFailed/SessionStarted/TTSResponse(音频分片)/TTSSentenceStart|End/TTSSubtitle/SessionFinished|Failed|Canceled`。
- 音频：`req_params.audio_params.format=pcm, sample_rate=16000`（另有 8k/22.05k/24k/32k/44.1k/48k 可选）→ **固定请求 16k PCM，与本地/声卡链路零转换对齐**。
- `req_params.speaker` = 控制台音色库音色 ID（用户提供）；`model` 默认 `seed-tts-2.0-standard`。
- 帧传输细节（JSON 信封、二进制音频片封装、连接复用时序）以官方协议包与 GizClaw 实现为准，属于实现任务内的协议核对项（参考 `asr_cloud.py` 当年核对 SAUC 的做法）。

**本设计采用按句请求**（一句话一次会话请求，音频分片回传即拼入队）：单句短 → 首音最快；本地/云端共用同一分句器与同一"句子→播放队列"编排。真·文本增量流（TaskRequest 多次下发）与 section_id 上下文复用仅作后续可选优化，第一版不做（YAGNI）。

## 四、架构与组件

### 4.1 新 `LLM/voice/tts_buffer.py` —— 分句缓冲器（纯逻辑，无 IO，可单测）

```python
class SentenceBuffer:
    def __init__(self, max_chars: int = 60): ...
    def feed(self, delta: str) -> list[str]   # 入缓冲，返回本次切出的完整句列表
    def flush(self) -> str                     # 返回剩余半句并清空（LLM 流结束收尾）
    def empty(self) -> bool
```

切分规则（全部在 feed 内增量处理）：
- 终止标点：`。！？…`（含全角）与换行 `\n` 作为断句点；断句点在标点**之后**（标点归属前句）。
- 连续终止标点压缩：`！！`、`？!`、`……` 整体归属前句，不产生空句/碎句。
- 句号+后随右引号（`。"` / `。」`）：引号归属前句后再断。
- 长度兜底：缓冲字符数 ≥ `max_chars` 且无断句点时强制切断（无标点长枚举/超长句场景），断点取最后一个空格/逗号/顿号，无任何次级分隔则硬切。
- 增量喂入的正确性：句边界跨两次 feed 也能切出（内部持缓冲区）。
- 清洗沿用 `tts.sanitize_tts_text`（在切分**前**对每句做，或 feed 前对 delta 做——取 feed 前对 delta 清洗，保证长度计算与词表一致）。

判定：**不需要**语义级/句法级解析、不需要 LLM 断句、不需要超时强切——LLM 停顿天然落在句间（句号后思考续写），已切出的整句照常先播。

### 4.2 新 `LLM/voice/tts_cloud.py` —— 火山 TTS 2.0 引擎（照 asr_cloud.py 模式）

- 顶层 `try/except import websocket`，失败置 `_WS_OK=False`（可选依赖，不污染导入链）。
- 类 `CloudStreamTTS`，`provider = "cloud"`：
  - `__init__`：`_WS_OK` 与 `config.cloud_tts_api_key()`、`config.cloud_tts_speaker()` 缺失时抛 RuntimeError（含"缺少依赖/未配置 key/未配置音色"三类可读信息），由 worker 启动回退处理。
  - `synthesize(text) -> tuple[np.ndarray, int]`：与本地 TTS 同签名（句级调用）。内部：建连（`StartConnection`）→ `StartSession`（audio_params: pcm/16000）→ `TaskRequest{text}`（text 为空直接返回空样本）→ 收 `TTSResponse` 音频分片拼装（与 `TTSResponse` 事件一一对应收集，直到 `SessionFinished`/`TTSResponse` 带终帧标记）→ `FinishSession`/`FinishConnection`。超时与失败：有限等待 + 抛 RuntimeError（带服务端错误文本）。
  - 音频片解码：pcm 16bit LE（与 ASR 相反的流向，同样 `np.frombuffer(..., "<i2")` → float32 /32768）；若服务端实际返回 wav/非 pcm 则按协议核对项处理并固定请求 pcm。
  - 每句新建一次连接代价可接受（句子短、对话低频）；连接复用（多句顺序复用同一 ws）作为第二版优化项，第一版不做。
  - `synthesize` 内不做文本清洗之外的预处理；`text` 先过 `tts.sanitize_tts_text`，清洗后为空则返回空样本（上层跳过）。
- 纯函数便于单测：JSON 事件请求构造 `build_start_session(session_id, speaker, ...)`、`build_task_request(session_id, text)`、服务端消息解析 `parse_server_message(raw) -> (kind, obj)`（kind: connection/session/audio/text_end/error/final）。

### 4.3 `LLM/voice/config.py` + `.env` —— 云端 TTS 配置（延迟读取，同 ASR）

```python
def cloud_tts_ws():        # 默认 wss://openspeech.bytedance.com/api/v3/tts/bidirection（可 VOLC_TTS_WS_URL 覆盖）
def cloud_tts_api_key():   # VOLC_TTS_API_KEY，为空回落 VOLC_ASR_API_KEY（同控制台 API Key，按需拆分的兼容）
def cloud_tts_speaker():   # VOLC_TTS_SPEAKER —— 必填，控制台音色库音色 ID
def cloud_tts_resource_id():  # VOLC_TTS_RESOURCE_ID 默认 "seed-tts-2.0"
def cloud_tts_model():     # VOLC_TTS_MODEL 默认 "seed-tts-2.0-standard"
```

### 4.4 `LLM/voice/audio.py` —— AudioSink 升级为队列播放器

- 写线程从**内部队列**取"样本段"，段内分块（~0.2s/块）阻塞写；段间无缝连续写（同一输出流）。
- `enqueue(samples, sample_rate)`：追加一段（若当前无播放则启动流）；`stop()` 置停止事件+abort 唤醒+**清空队列**（barge-in 语义：打断即丢弃未播内容）；`is_done()` = 队列空且流已关闭。
- `play(samples, sr)` 保留兼容：内部 = stop() + enqueue()（旧测试与一次性播放路径可用）。
- 队列与流对象全部在锁内维护；写线程仍是流的唯一创建/关闭者（沿用防 PortAudio 双关闭竞态模型）。
- 采样率变更（本地/云端都是 16k，理论不切换；若未来出现 24k 段，段内统一按段的采样率换算块长——不做重采样）。

### 4.5 `LLM/voice/worker.py` —— 流式问答编排

- `_build_runtime`：读 `settings.get("tts_provider", "cloud")`；cloud → 构造 `tts_cloud.CloudStreamTTS()`，构造抛错 → audit(`voice_error`, action="tts_provider_fallback") + 回退 `tts_mod.TTS()` + `sub_status["tts_fallback"]`；local → 直接用本地。`sub_status["tts"]` 记录实际引擎（/api/voice/status modules.tts）。
- 新增**应答线程**方法 `_answer(uid, user_text)`（一个 daemon 线程，回复期间同一时刻至多一个活跃，`self._answering` 标志）：
  1. publish `recognized`（沿用，但 kiosk 语义改为"user 气泡入屏"）→ 前端 push user 气泡。
  2. `self.session.start_speaking()`（线程安全赋值）、`_speak_started = monotonic()`、publish speaking —— **在首个完整句合成完成、准备入队播放时**调用，保证状态机在真实发声前进入 SPEAKING。
  3. `for ev in chat_stream(...)`：`type == "content"` → 清洗 → `buffer.feed(delta)` → 每切出句：合成（本地句级 / 云端句级）→ `sink.enqueue`；同时把 delta publish 给前端（见 §5）。`type == "done"` → 退出。
  4. `flush()` 剩余半句：非空则合成并入队。
  5. publish `chat_new`（完整 user+assistant，事件协议不变）作为最终一致兜底。
  6. `_answering = False`。
  7. 全程异常兜底：LLM 流异常/合成异常不抛穿（audit 记录）；某句云端合成失败 → 该句本地兜底合成（本地模型始终可用），仍失败则跳过该句。
- `chat_fn` 整段收集回调**不再用于语音链路**（voice_api 保留但改名或直接改签名，见 §4.6）；应答线程直接消费 `chat.chat_stream`。
- 主循环 `_step`：
  - SPEAKING 分支打断检测不变（VAD + `_speak_started`/BARGE_IN_GRACE_S）→ `sink.stop()` 清队列打断播放；打断时同时置应答中断标志（`_answer` 线程检查，停止后续句子合成与入队、清理缓冲）→ publish listening 语义沿用。
  - 播报自然结束判定改为：`sink.is_done() and not self._answering and self.session.finish_speaking()`（避免"队列瞬时空但应答线程还在合成下一句"时误判结束）。
  - 播报结束/打断两出口的 `_flush_vad()` + 回声静音窗（SPEAK_TAIL_BLANK_S）逻辑**保持不变**。
  - LLM 等待/句子合成期间（SPEAKING 未发声前 state 已置 SPEAKING）主循环继续喂 VAD——插话打断在合成期同样生效（`sink.stop` 对未入队内容无效时，靠中断标志 + 主循环发现 `session.barge_in()` 已切状态后，`_answer` 线程在下次 enqueue 前检查会话状态自行退出）。
- 识别新整句到达时若 `_answering` 活跃：等同打断语义（先 stop 播放、再走新问答），不复用旧应答。

### 4.6 `LLM/voice_api.py` / `server.py`

- `_chat_fn` 仅保留给非语音用途的场景；语音启动参数改为传入"流式消费工厂"或直接在 worker 构造处内联 `chat.chat_stream` 消费（最小改法：`VoiceWorker(uid, user_text, ...)` 内部直接调 `chat.chat_stream`，voice_api 不再包 `_chat_fn`；保留 `post_turn_fn` 落历史）。
- `get_status()` 返回 `tts_provider`（读 settings）与 `modules.tts`（worker.sub_status["tts"] 实际引擎 local/cloud + fallback 信息），照 asr 字段模式。

## 五、SSE 事件协议（events.ts 唯一事实来源同步，AGENTS.md 约定 4）

新增总线事件 `chat_partial`：

```ts
export interface ChatPartialEvent {
  type: "chat_partial";
  uid: string;
  delta: string;   // 助手回复的本次增量文本（content delta 原样，清洗留给 TTS 层）
}
```

- 由 worker 应答线程在收到每个 `chat_stream` content delta 时 `publish`。
- kiosk 消费：
  1. `voice_state state="recognized"`（含 text=老人语句）→ push user 气泡 + 空 assistant 气泡（若最后已有本次会话残留则复用），liveText 行收起。
  2. `chat_partial` → append delta 到最后的 assistant 气泡（自动滚动到底）。
  3. `chat_new`（最终）→ 找到本次会话的 assistant 气泡**整条覆盖**为完整文本（防 SSE 丢帧导致的渐进不完整）；若因手动输入等打断找不到该气泡（最后一条不是本次 user），回退为原 push 两条逻辑。
- `KNOWN_TYPES` 与 `parseBusPayload` 同步加 `chat_partial`；admin 端不消费该事件（打字页独立），Overview 页如消费 chat_new 不受影响。

## 六、前端改动

- `kiosk/src/App.vue`：onEvent 增加 chat_partial 分支 + recognized 分支改为入气泡；chat_new 分支改覆盖逻辑；sendText（打字链路）不动。
- `kiosk/src/components/SettingsSheet.vue`：新增「合成引擎（重启服务后生效）」单选组（本地识别/云端），读写 `tts_provider`；与 asr 组并列。
- kiosk 底部：在 asr-toggle 旁加 tts 快捷切换按钮（显示"合成：云端/本地"，点击 POST /api/settings 切 tts_provider + alert 重启生效），样式沿用 `.asr-toggle`。
- `admin/src/pages/VoiceStatusPage.vue`：显示 tts_provider 与 modules.tts（含回退提示）。

## 七、系统稳健性（对齐 AGENTS.md 红线）

- 可选依赖（websocket-client）不进 server.py/voice_api.py 顶层硬 import —— tts_cloud.py 内部 try/except，与 asr_cloud 同款。
- tts_provider=cloud 但无 key/音色/依赖/开通失败：worker 启动回退本地（audit + print WARN + sub_status 标注），语音链路照常，不降级整机。
- 运行期句合成失败：该句本地兜底 → 再失败跳过该句（文本仍完整上屏），不中断整段播报。
- 后端在无音频/无声卡环境仍可 import、lifespan 正常（voice 整体降级逻辑不变）。
- 审计：`voice_tts`（记录 provider、句数、时长）在每轮播报结束落一次；`voice_error` 记 tts 异常与回退。

## 八、测试计划

- 新单测：`test_tts_buffer.py`（切分：标点/换行/连续标点/引号归属/跨 feed 切句/长度兜底/清洗后空缓冲）；`test_tts_cloud.py`（事件请求构造、服务端消息解析纯函数，mock ws 连接）；`test_audio_sink.py`（mock sounddevice：enqueue 顺序播放、多段拼接、stop 清队、is_done 语义）；worker 流式事件测试改造（模拟 chat_stream 事件序列 → 断言 chat_partial 顺序、speaking 置位时机、chat_new 覆盖、打断中断标志、_answering 竞态保护）。
- 回归：既有 `LLM/tests` 全量（重点 test_worker_events / test_session / test_voice_api_enroll 不破坏）；回归后测试数应 ≥ 现有 64。
- 真机复测项：①语音问答"边说边播"首句延迟与句间连续性；②kiosk 气泡逐字增长 + 最终与历史一致；③打断（老人插话）在合成期与播放期都生效且不回问自答；④切本地/云端重启生效（/api/voice/status modules.tts 变化）；⑤云端 key/网络故障回退本地不崩。

## 九、决策记录

| 决策 | 选择 | 理由 |
| --- | --- | --- |
| 流式粒度 | 句级流水线（不逐字/不整段） | 本地是离线模型逐字不现实；整段等待首音慢。句子是合成与听感的自然单元 |
| 分句方式 | 标点切分+长度兜底，无超时强切、无语义解析 | LLM 停顿在句间；同步流式下超时检测收益低（YAGNI）；语义断句成本高无收益 |
| 云端请求形态 | 每句一次会话请求（不用文本增量流） | 首音最快、两端统一编排、连接复用列为后续优化 |
| 播放模型 | AudioSink 队列播放器（enqueue 无缝拼接） | 句间无隙无爆音；barge-in=stop 清队语义清晰 |
| worker 模型 | 应答线程（_answer）+ 主循环专职状态机 | 合成/网络阻塞不再卡 VAD/打断；现状"回复期间不响应"顺带修复 |
| 事件协议 | 新增 chat_partial(delta) + chat_new 覆盖兜底 | 事件少、最终一致、现有 chat_new 消费者不破坏 |
| 默认引擎 | tts_provider="cloud"（不可用自动回退 local） | 与 asr_provider 对称；默认优先云端音质 |
| 云端协议 | 豆包语音 TTS 2.0（/api/v3/tts/bidirection, seed-tts-2.0） | 已核实支持流式音频分片+16k pcm+连接复用；与 ASR 同控制台 key |

## 十、待用户提供 / 开工前核对（非 TODO，是阻塞性输入）

1. **音色 ID**（VOLC_TTS_SPEAKER）：豆包语音控制台 → 音色库选 1 个（如养老场景语速适中、语气温和的普通话女声）。
2. **API Key**：确认与云端 ASR 同一把（VOLC_ASR_API_KEY）还是独立新建 VOLC_TTS_API_KEY（.env 支持拆分）。
3. 控制台确认开通产品确为「语音合成」支持 2.0（resource `seed-tts-2.0`）；若是旧版 SAUC 大模型资源则端点/帧格式按老协议实现（改动仅限 tts_cloud.py 内部）。
4. 帧封装细节核对：官方协议包 zip / GizClaw doubao-speech-go `examples/tts_v2/websocket` 为准（实现任务第一步）。
