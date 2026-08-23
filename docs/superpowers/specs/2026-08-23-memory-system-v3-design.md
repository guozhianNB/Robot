# 记忆系统 v3 设计：全自动 RAG + 知识图谱 + 自我纠错

> 状态：设计定稿（待用户审查）
> 日期：2026-08-23
> 参考：`docs/maibot/memory-system.md`、`docs/maibot/记忆系统差距分析.md`、[MIRIX 记忆分层](https://docs.mirix.io/)、[MaiBot A_Memorix 配置](https://docs.mai-mai.org/manual/configuration/amemorix-config)
> 前置：P0 已落地（检索 query 接入 + episode 摘要入库，见 `LLM/chat.py` / `LLM/memory.py`）

---

## 一、目标

把记忆系统从「半自动 + 人工审核」升级为「**全自动记录 + 自我纠错 + 关系图谱 + 全面记忆**」，对齐 MaiBot / MIRIX 的效果：

1. **全自动记录**：性格、喜好、事件、对话内容、重要信息、实体关系全部由模型自动提取入库，不再需要人工逐条审核。
2. **自我纠错**：能主动发现并改正错误记忆（双重：对话即时 + 整理时）。
3. **关系图谱**：记录实体（人/物/话题/事件）与关系（喜欢/亲属/发生/关联）。
4. **语义检索**：用真正的 embedding 替代 n-gram 哈希，支撑"大 RAG"的语义召回。

---

## 二、已定决策（本轮拍板）

| 决策点 | 结论 |
|---|---|
| 医疗红线 | **保留**：医疗信息只护士人工录入，模型永不写、永不改 |
| 身份信息 | **新增红线**：老人基础身份（姓名/昵称/性别/年龄/生日/床位）也只护士人工录入，模型永不写、永不改 |
| 自我纠错时机 | **即时 + 整理双重** |
| Embedding | **阿里 text-embedding-v3**（DashScope OpenAI 兼容端点） |
| 部署目标 | **先本机跑通**，RDK X5 板卡部署后置 |
| 图存储 | **Kuzu 嵌入式图库** |

---

## 三、数据分层（对应 MIRIX 的 core / episodic+semantic / graph）

```
┌────────────────────────────────────────────────────────────────┐
│                     chat.py 对话编排                             │
│  build_system: 只读档案(全量) + 核心记忆(全量,cap) + RAG(Top-K)  │
│                + 图谱(一跳关系)                                   │
└───────────────────────────────┬────────────────────────────────┘
                                 │
┌────────────────────────────────▼────────────────────────────────┐
│                     memory.py 记忆服务                            │
│  写回(全自动) · 自我纠错(双重) · 检索 · 图谱抽取 · 迁移            │
└──────────┬──────────────────┬──────────────────┬────────────────┘
           │                  │                  │
  ┌────────▼────────┐  ┌───────▼────────┐  ┌─────▼──────────┐
  │ SQLite          │  │ ChromaDB       │  │ Kuzu           │
  │ 只读档案        │  │ 普通 RAG 记忆   │  │ 实体-关系图     │
  │ 核心记忆        │  │ (episodic/     │  │ (节点+边)      │
  │ 设置/审计       │  │  semantic)     │  │                │
  └─────────────────┘  └───────┬────────┘  └────────────────┘
                               │
                       ┌───────▼────────┐
                       │ embed.py        │
                       │ 阿里 embedding  │
                       │ (失败回退 n-gram)│
                       └────────────────┘
```

| 层 | 存储 | 内容 | 注入方式 | 写权限 |
|---|---|---|---|---|
| **只读档案** profile | SQLite `profiles` | 身份（姓名/昵称/性别/年龄/生日/床位）+ 医疗（病史/用药） | 全量注入 | **仅护士人工** |
| **核心记忆** core | SQLite `core_memories` | 核心偏好、重要关系、性格画像、说话风格 | 全量注入（cap） | 模型全自动写 + 纠错 |
| **普通 RAG** | ChromaDB | 事件/经历/对话摘要(episodic)、一般喜好/事实(semantic) | 按 query 检索 Top-K | 模型全自动写 + 纠错 |
| **知识图谱** | Kuzu | 实体 + 关系（喜欢/亲属/发生/关联…） | 按 query 抽实体 + 一跳关系 | 模型全自动写 + 纠错 |

---

## 四、数据模型

### 4.1 SQLite `profiles`（只读档案，模型永不写、永不改）

现有表增加 `gender TEXT`、`birthday TEXT` 两列。字段归属：

- **身份（护士只读）**：`name`、`nickname`、`gender`、`age`、`birthday`、`bed`
- **医疗（护士只读）**：`profile_json` 内的 `病史`、`用药`
- **护士备注**：`notes`
- 原 `style`、`preferences_json` **从 profiles 移除**（迁移到核心记忆，见 4.2 与迁移节）——它们属于"从对话学到的稳定信息"，不是护士录入的基础档案。

### 4.2 SQLite `core_memories`（核心记忆，模型可写可纠错）

```
core_memories(id PK, uid, type TEXT, content TEXT,
              confidence REAL, importance INTEGER DEFAULT 0,
              source TEXT, ts TEXT, updated_at TEXT)
```

- `type`：`preference`(核心偏好) / `relation`(重要关系，文本摘要) / `persona`(性格画像) / `style`(说话风格) / `fact`(基本事实)
- `importance`：0-5，检索注入与排序用
- 每老人 cap：注入时最多取 importance 降序前 N 条（默认 30 条 / 2000 字）

### 4.3 ChromaDB（普通 RAG 记忆）

- collection：`memories_{uid}`（每老人一个）
- document：记忆文本；metadata：`type(episodic|semantic)`、`ts`、`importance`、`source`、`sqlite_id`
- **SQLite 镜像表 `rag_memories`**（uid, chroma_id, type, content, ts, importance, source）：用于前端回读、降级兜底、审计回链

### 4.4 Kuzu（知识图谱）

```
Entity(id STRING PK, uid STRING, name STRING, type STRING)   -- type: person/object/topic/event
Relation(src_id STRING, dst_id STRING, type STRING, uid STRING, ts STRING)
```

- 初始关系类型：`likes / dislikes / family / related_to / happened_at`，LLM 抽取时可开放扩展
- 实体 id：`uid + ":" + 规范化名`，去重合并

---

## 五、全自动写回（去掉人工审核）

沿用「话题结束空闲 30s → consolidate」触发，产出改为全自动入库：

1. LLM 一次输出 JSON：`entries[]`（新记忆）+ `relations[]`（实体关系三元组）+ `digest`（episodic 摘要）+ `portrait`（核心画像）
   - 每条 entry：`{type, content, importance}`，`type` ∈ 下列 7 类：
     - RAG 层：`episodic`（事件/经历/对话摘要）、`semantic`（一般喜好/一般事实/知识）
     - 核心层：`preference`（核心偏好）、`relation`（重要关系）、`persona`（性格画像）、`style`（说话风格）、`fact`（基本事实）
   - `importance`：0-5 整数
2. `entries` 按 `type` 分流（分流规则见下）
3. `relations` → Kuzu（实体去重、边去重）
4. `digest` → ChromaDB（episodic）
5. `portrait` → `core_memories`（type=persona，importance 恒为 5）

**分流规则（明确）**：
- `type ∈ {episodic, semantic}` → 一律写 ChromaDB
- `type ∈ {preference, relation, persona, style, fact}` 且 `importance >= 3` → 写 `core_memories`
- `type ∈ {preference, relation, persona, style, fact}` 且 `importance < 3` → 降级写 ChromaDB（作为普通 RAG 记忆，仍可被检索命中）
6. **红线拦截**：`MEDICAL_KEYWORDS` 命中 → 拒绝写入并审计 `memory_reject`；身份字段（姓名/性别/年龄/生日/床位）的写入请求一律拒绝

> 去重/合并保留：向量相似度 ≥ 阈值（config 可调）视为重复，合并而非重复入库。

---

## 六、自我纠错（双重）

### 6.1 即时纠错（对话返回后异步，不阻塞回复）

- 每轮对话返回后，后台跑一个轻量 LLM 判断：`"这句话是否在纠正/更新之前的记忆？输出 {correct: bool, target: ..., new_value: ...}"`
- 命中则定位目标记忆（核心记忆或 RAG 条目）并**直接更新**，写审计 `memory_correct`
- **例外**：目标落在医疗字段或身份字段（姓名/性别/年龄/生日/床位）→ 不自动改，只记审计 `memory_correct_blocked`，交由护士处理

### 6.2 整理纠错（consolidate 内）

- 新信息与旧记忆矛盾 → 不再标 `conflict` 进待处理，而是**以新信息为准自动更新旧条目**（医疗/身份除外），写审计 `memory_correct`

### 6.3 可追溯

纠错全程 `audit.log("memory_correct", uid, target, old, new, source)`，出问题可查"谁/何时/把什么改成了什么"。

---

## 七、检索注入（build_system）

1. 只读档案全量注入（查 `profiles`，护士只读）
2. 核心记忆全量注入（importance 降序 cap）
3. 普通 RAG：`recall(uid, query)` 用阿里 embedding 检索 Top-K（默认 3）
4. 图谱：从 query 抽实体 → Kuzu 查一跳关系 → 拼"关系片段"注入
5. 混合去重、按 importance + 相似度加权排序

---

## 八、降级策略（AGENTS.md 硬要求：可选依赖缺失必须降级运行）

| 故障 | 降级行为 |
|---|---|
| 阿里 embedding 不可用/超时 | 回退现有 `vectors.py` n-gram 检索（代码保留） |
| ChromaDB 不可用 | 回退 SQLite `rag_memories` 镜像表简单检索 |
| Kuzu 不可用 | 关系信息降级为文本注入（不阻断对话） |
| 三者全挂 | 退化为「只读档案 + 核心记忆 + 纯对话」，不阻断对话 |

- 可选依赖（`chromadb`、`kuzu`）在模块顶层 `try/except` 引入，失败置模块级标志 `_CHROMA_AVAILABLE=False` / `_KUZU_AVAILABLE=False`，与 `voice_api.py` 降级范式一致。
- `server.py` 及导入链不得硬 import 这些依赖。

---

## 九、依赖、迁移、前端

### 9.1 依赖（写入 `requirement.txt`）

- `chromadb`、`kuzu`
- embedding 用阿里 DashScope 的 OpenAI 兼容端点（复用项目现有 `openai` 客户端，无需 dashscope SDK）：
  - base_url: `https://dashscope.aliyuncs.com/compatible-mode/v1`
  - model: `text-embedding-v3`
  - 需 `DASHSCOPE_API_KEY`（放 `.env`）

### 9.2 迁移（启动时一次性，幂等）

- 现有 `memories` 表条目 → 按类型迁：`event`→ChromaDB(episodic)，`preference/fact`→ChromaDB(semantic) 或 `core_memories`（高价值）
- 现有 `portraits` → `core_memories`(type=persona)
- 现有 `summaries` → ChromaDB(episodic)
- 现有 `profiles.style` → `core_memories`(type=style)；`preferences_json` → `core_memories`(type=preference)
- 迁移完成记审计 `memory_migrate`，原表保留备份不删除

### 9.3 前端（UI/index.html 记忆页签）

- 改为三块：**核心记忆 / RAG 记忆 / 图谱（列表式展示实体-关系）**
- 保留人工录入入口（医疗信息、身份信息仍走护士录入）
- 保留记忆删除（走软删除 + 审计）

---

## 十、错误处理与测试策略

- **错误处理**：所有 LLM 抽取/embedding/图写入异常捕获后 `audit.log(...)` + 静默降级，不阻断对话（遵循现有后台任务吞异常范式）。
- **测试**：
  - `embed.py`：mock embedding 失败 → 验证回退 n-gram；embedding 成功 → 验证向量写入/检索
  - `memory.py`：consolidate 全自动写回分流正确性；医疗/身份关键词拒绝写入；双重纠错（即时/整理）更新正确
  - 图谱：实体/边去重、一跳关系查询
  - 迁移：幂等性（重复启动不重复导入）
  - 降级：逐个关闭 chromadb/kuzu/embedding，对话链路仍可用

---

## 十一、范围与不做的事

- **不做**：Neo4j 独立图服务、ChromaDB 集群、多租户/鉴权、可视化图谱编辑器（列表式先够用）
- **后置**：RDK X5 板卡嵌入式适配（chromadb/kuzu 的 ARM 兼容验证）
