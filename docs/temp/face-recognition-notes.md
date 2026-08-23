# 人脸识别接入备忘（临时 · 未实现）

> 状态：**占位规划**。当前系统只有声纹一路身份识别（`LLM/voice/identity.py`，`FaceSource` 接口已预留）。
> 本文记录后续接入人脸识别的设计想法，供实现时参考，**不承诺**最终方案。

## 1. 目标场景

- 老人注册流程第 3 步：让老人对着摄像头拍照，存入该 uid 的人脸样本库（为 1:N 识别提供素材）。
- 记忆页"追加人脸照片"：给已有老人补充多角度/多光照照片，提升识别鲁棒性。
- 运行时身份识别：摄像头采集画面 → 人脸检测 → 提特征 → 与档案库比对 → 与声纹投票融合（`IdentityVote` 已支持多源投票）。

## 2. 数据存储

```
LLM/data/faces/<uid>/<yyyyMMdd_HHmmss>.jpg    # 原始照片（保留原图，特征可离线重算）
LLM/data/faces/<uid>/<yyyyMMdd_HHmmss>.npz    # 同名前缀的 embedding（选配，可延迟到有模型时再生成）
```

- 与声纹 `LLM/data/speakers/<uid>.npz` 平行；目录扫描即可列样本，不必建表（与 `list_speakers` 同思路）。
- `.gitignore` 已排除 `LLM/data/*` 运行时数据，无需额外处理。
- 识别时加载某 uid 全部 npz 取平均（多照片平均，类似声纹合并平均的思路）；未来可换更细粒度策略。

## 3. 采集端

- **首选：服务端摄像头（板卡/机器人摄像头）**——与声纹"服务端采集"架构一致，老人面对机器人即可。
  - 需板卡提供摄像头设备（CSI/USB），后端用 OpenCV 或 ffmpeg 抓帧。
- **兜底：前端浏览器拍照上传**（护士带终端到老人面前拍，`getUserMedia` → canvas 截帧 → 上传 JPEG）。
  - 前端拍照的通用性最好，适合先在开发期跑通全流程。
- 建议：后端抽象 `FaceCaptureSource`（协议/接口），两种采集可插拔，与 `voice_api` 降级模式一致。

## 4. 识别模型（参考）

- **检测**：RetinaFace / MTCNN（ONNX），或 InsightFace 自带检测。
- **特征**：InsightFace `buffalo_l` / `antelopev2`（512 维），或 modelscope 中文人脸模型。
- **部署**：板卡若为地瓜派 RDK，可走 BPU 加速（参考 `LLM/voice/backend.py` 的 backend 抽象，`BPU_SUPPORTED` 扩展人脸模型）。
- **比对**：余弦相似度 + 阈值（与声纹一致）；阈值初值 0.4~0.5 待实测调优。

## 5. API 草案（占位）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/api/face/status` | 模块可用性（依赖缺失时降级返回 unavailable，`ok` 保持 true） |
| GET  | `/api/face/photos?uid=` | 列出某 uid 的人脸样本（数量/时间/缩略图） |
| POST | `/api/face/photos` | 上传照片（multipart 或 base64），写入 `data/faces/<uid>/`；可选当场提特征 |
| DELETE | `/api/face/photos/<id>` | 删除单张样本 |
| GET  | `/api/face/identify` | （运行时）抓帧识别 → `{uid, score}`（后续做） |

- 写操作在依赖缺失时返回 `{"ok": False, "error": "人脸模块不可用（缺少依赖：…）"}`；查询类返回 `ok: True` + `status: "unavailable"`（遵循 AGENTS.md 稳健性红线）。

## 6. 与声纹融合

- `LLM/voice/identity.py` 已定义 `IdentityVote(candidate_uid, confidence, source)` 与 `IdentitySource` Protocol。
- 新增 `FaceSource(IdentitySource)`（`name = "face"`），`probe(frame) -> IdentityVote`。
- `VoiceprintOnlyFusion` 升级为多源融合：人脸/声纹各自投票，高置信单源可定，冲突时取最高置信或加权（阈值与置信度需要实测校准）。
- `effective_uid` 保持"宁问勿猜"策略。

## 7. 前端接入点（届时改动）

- 注册向导第 3 步：占位 → 启用"拍照录入"（调 POST /api/face/photos）。
- 记忆页档案卡片："📷 追加人脸照片" 按钮启用 + 显示人脸样本数。
- 模块状态弹窗：加"人脸识别"一行（复用 `/api/modules/status` 的模块聚合）。

## 8. 其他想法 / 坑

- **活体检测**：静态照片攻击风险高，正式上线建议加眨眼/张嘴动作指令（注册时录制 2-3 帧短视频）或红外深度相机。
- **注册拍照质量**：引导老人正对摄像头、光线均匀；可做简单人脸框检测提示"未检测到人脸/请正对镜头"。
- **多照片注册**：建议注册时至少拍 1 张，追加 2-3 张不同角度，识别鲁棒性显著提升。
- **隐私**：人脸照片属敏感生物信息，本地存储不上云；审计日志 `log("face_*", ...)` 记录增删操作。
- **降级**：模型/摄像头缺失时整套人脸能力 unavailable，不影响后端启动（与 `voice_api` 同模式）。
