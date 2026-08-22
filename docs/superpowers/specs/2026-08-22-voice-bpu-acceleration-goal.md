# 语音模型 BPU 加速实现目标（RDK X5 / 征程6E）

> 日期：2026-08-22 ｜ 状态：目标与接口设计（板卡阶段 M5 前置） ｜ 归属：板卡实施
> 关联：`docs/superpowers/specs/2026-08-22-voice-pipeline-design.md`（语音子系统规格）
> 关联：`docs/superpowers/plans/2026-08-22-voice-pipeline.md`（任务 2B：推理后端抽象）
> 说明：本文档将被迁移到板卡，板卡端 agent 依据"§6 板卡实施指引"查询地平线 BPU 工具链并实现。

## 1. 背景

- RDK X5（征程6E J6E）CPU 为 8× Cortex-A55，**实测跑 1.5B LLM 仅 6.5~6.8 tok/s**（见 `docs/板卡硬件规格.md`）；语音推理若全走 CPU，多模型并发时程序明显卡顿。
- 板卡另有 **BPU 10 TOPS**（ION 预留 320MB），当前主要留给视觉 CV 推理，空闲可复用。
- **用户硬要求**：相关语音/大模型优先跑在 BPU 上；**识别不到 BPU 时自动切换回 CPU 运行**，程序不能因此卡死或崩溃。
- 现状：语音五件套（ASR/TTS/VAD/KWS/声纹）在 Windows 开发机全部走 CPU（sherpa-onnx onnxruntime + 声纹 torch）；板卡移植（M5）尚未开始。

## 2. 目标

1. 交付**推理后端抽象层**：每个语音模型可按 `bpu` / `cpu` 运行，`auto` 时自动探测。
2. 探测不到 BPU / 模型加载失败 → **逐模型静默降级 CPU**，功能不中断、主程序不崩（复用 voice worker 的心跳与降级上报）。
3. 板卡阶段由板卡 agent 用**地平线 BPU 工具链**实测各模型转换率，优先把 CNN 类模型（声纹 ERes2NetV2）转换上 BPU。
4. 端到端延迟目标维持需求文档的 **2 秒**（语音进 → 文字出 → 模型处理 → 语音播报）。
5. 验收判据：有 BPU 且模型支持 → 走 BPU；禁用 BPU / 强制 cpu / 转换失败 → 自动 CPU 且功能照常。

## 3. 各模型 BPU 可行性初判（板卡实测后回填修正）

| 模型 | 结构类型 | BPU 初判 | 说明 |
|------|----------|----------|------|
| 声纹 ERes2NetV2 | 纯 CNN（ResNet 系） | ✅ 高 | **BPU 首选**。注意其 ONNX 输入是 **FBank 特征 (B,T,80)** 而非波形，特征提取仍需 CPU 侧完成（torchaudio/kaldi 对齐） |
| VAD silero | LSTM | ❌ 低 | BPU 对循环网络支持有限，倾向 CPU |
| 唤醒 KWS zipformer | transformer | ⚠️ 待实测 | J6 BPU 对 transformer/attention 支持需工具链确认，倾向 CPU |
| ASR zipformer-14M | transformer / transducer | ⚠️ 待实测 | 同上；8×A55 跑它已实时，CPU 可接受 |
| TTS vits-zh-ll | flow / VAE 生成 | ❌ 低 | 生成式结构 BPU 难覆盖，CPU |
| 本地 LLM（可选） | transformer LLM | ⚠️ 待实测 | 需求文档已定 **LLM 走云端**、本地仅离线兜底；BPU 跑 LLM 需地平线专用部署方案评估，**本期不做** |

**结论**：本期框架交付 = 自动检测 + 后端切换 + CPU 兜底；**具体哪些模型真正上 BPU 由板卡实测决定，首选 ERes2NetV2**。ASR/TTS/VAD/KWS 即使留在 CPU，8×A55 实时性已满足，卡顿主要靠"后端抽象 + 不重复加载 + 流式"缓解。

## 4. 架构设计：推理后端抽象层

新增 `LLM/voice/backend.py`（Windows 阶段已落地骨架，见实现计划任务 2B）：

```
env VOICE_BACKEND (bpu|cpu|auto, 默认 auto)
        │
        ▼
detect_backend() ── 强制值优先 ── 探测 hobot_dnn 可导入 ── 兜底 "cpu"
        │
        ▼
resolve_backend(model_key, requested="auto")
   = "bpu" 仅当 模型在 BPU_SUPPORTED 表 且 探测到 BPU 且 未强制 cpu
   否则 "cpu"
```

板卡阶段需遵守的后端契约（BPU runner 按此实现）：

```python
class InferenceBackend(Protocol):
    name: str                                    # "bpu" / "cpu"
    def load(self, model_key: str, model_spec) -> "ModelRunner": ...

class ModelRunner(Protocol):
    def run(self, **inputs) -> np.ndarray | dict: ...
```

- 各模型封装（`vad.py` / `kws.py` / `asr.py` / `tts.py` / `speaker.py`）在板卡阶段增加 `backend="auto"` 构造参数，内部按 `resolve_backend(model_key, backend)` 分流到对应 runner。
- **Windows 阶段恒为 cpu，行为不变**；抽象层只保证接口与降级路径存在。

## 5. 检测与降级策略

1. **检测优先级**：`VOICE_BACKEND` 环境变量（bpu|cpu|auto）> 自动探测（尝试 `import hobot_dnn`）> 默认 cpu。
2. **逐模型降级**：探测到 BPU，但某模型转换/加载失败 → 该模型单独回退 CPU，**不整体崩、不阻塞其它模型**。
3. **状态可观测**：voice worker 的 `sub_status` 增加字段：
   ```json
   {"backend": "bpu", "bpu_models": ["speaker_eres2netv2"], "cpu_models": ["asr_zipformer", "tts_vits", "vad_silero", "kws_zipformer"]}
   ```
   `GET /api/voice/status` 直接可查；`/api/events` 广播 `voice_status`。
4. **审计**：新增事件 `voice_backend`，记录切换原因（probe 结果 / 转换失败 / 强制 cpu）。
5. **一致性**：切换只影响推理路径，不改变 worker 编排、会话状态机、降级矩阵（规格 §7）。

## 6. 板卡实施指引（板卡 agent 需查询/验证的工具链）

> 迁移到板卡后，板卡 agent 依据本节查询地平线官方工具链，核实版本与调用方式后再实现，不要照抄本文假设。

1. **模型转换（onnx → .hbm）**：查询 RDK X5 对应的模型转换工具链（地平线 `hobot_model_convert` / hbdk4 编译器），确认与征程6E 匹配的版本、支持算子清单、量化方式（INT8/FP16）。
2. **BPU 推理运行时**：查询 `hobot_dnn`（Python/C++）或 TogetheROS.Bot 的 `dnn_node` 在 RDK X5 上的安装方式与调用 API；确认与 onnxruntime 的对接路径。
3. **每模型必测并回填**：转换成功率、BPU 延迟 vs CPU 延迟、精度校验（声纹：同输入 cos(onnx输出, bpu输出)；ASR：WER 对比）、内存占用、量化损失是否可接受。
4. **ERes2NetV2 特殊注意**：官方 ONNX 输入为 FBank 特征（`speakerlab/bin/export_speaker_embedding_onnx.py`，opset 11，动态轴 batch/frame_num）；BPU 转换前需在 CPU 侧用与训练一致的 fbank 参数（16k、25ms/10ms、80 mel、dither=0、按帧均值减除）产出特征。
5. **交付物**：BPU runner 实现（遵守 §4 契约）+ 实测报告（延迟/精度表）+ 回填 `BPU_SUPPORTED` 注册表。
6. **失败兜底**：任何模型转换失败即从 `BPU_SUPPORTED` 移除，走 CPU，不影响整体可用性。

## 7. 里程碑与交付物

| 阶段 | 内容 | 交付物 |
|------|------|--------|
| Windows（本期，M1-M4） | `backend.py` 骨架 + 单测 | 自动检测/解析/降级逻辑，恒 cpu 运行 |
| 板卡（M5） | BPU runner 实现 + 实测 | 转换脚本、runner 代码、实测报告（延迟/精度）、BPU_SUPPORTED 回填 |
| 验收 | 强制 cpu / 禁用 BPU / 有 BPU 三种场景 | 程序不卡不崩、功能照常、状态可查 |

## 8. 风险与对策

| 风险 | 对策 |
|------|------|
| BPU 算子支持矩阵限制（LSTM/transformer/flow 不支持） | 逐模型回退 CPU；不追求全量上 BPU |
| 工具链版本与 RDK X5 不匹配 | 板卡 agent 先用官方示例模型跑通全流程再转业务模型 |
| 量化后精度下降 | 保留 CPU 路径做 A/B 对比；精度不达标宁可用 CPU |
| 转换/加载失败拖累启动 | 懒加载 + 逐模型降级 + 审计，主程序不受影响 |
| 端到端 2s 目标 | BPU 覆盖有限时维持"流式 ASR + TTS 提前拼接 + 并行"优化路径 |
