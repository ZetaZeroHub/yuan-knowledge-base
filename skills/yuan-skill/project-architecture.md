# Yuan Knowledge Base 项目架构（供 yuan-skill 理解）

> 这是 yuan-skill 在创建 / 修改 skill 前必须掌握的项目背景知识。
> 内容与 `INDEX.md`、`server.py` 的真实状态对齐。

---

## 1. 目录结构

```
yuan-knowledge-base-workspace/
├── INDEX.md              # 工作区总入口 + 工作流规则
├── server.py             # 后端：模式路由、prompt 构造、IPC、清理策略
├── knowledge/            # 知识库（章节制，详见 knowledge/INDEX_KB.md）
│   ├── INDEX_KB.md       # 知识库路由表 + Pending Review 区
│   ├── 01_* ~ 07_*       # 冷启动章节（默认只读；可含明确维护加入的新 seed 内容）
│   └── 08_*+             # 普通演化追加章节（笔记位于深度 2）
├── skills/               # 技能目录（每个子目录一个 skill）
│   ├── baguwen/  interview/  search_evolve/  personalize/  render_html/
│   └── yuan-skill/       # 本 skill
├── paper-ui/             # 前端
└── .agent/               # Agent 运行时 IPC：inbox / outbox / uploads
```

---

## 2. 核心工作流规则（来自 INDEX.md）

1. **冷启动语料定义**：`knowledge/01_*` ~ `knowledge/07_*` 是 curated cold-start library。这里也包括用户明确要求维护冷启动库时加入的新 seed 内容；加入后默认视为只读冷启动内容。
2. **追加式演化 + 编号追加规则**：普通新内容放进**新的递增编号**章节目录（如 `knowledge/08_<topic>/`），
   再注册到 `knowledge/INDEX_KB.md`。例外：用户明确要求扩展冷启动库时，可把新 seed 内容加入 `01_*` ~ `07_*` 并登记在 `INDEX_KB.md` 的 Cold-Start 区。
3. **HTML 是展示格式**：所有知识以编译后的单文件 `.html` 查看。修改任何 Markdown 源后，
   需调用 `skills/render_html/render.py` 重新生成对应 HTML。
4. **Draft 审稿管线**：新内容先生成为 `.draft.html`，注册到 `INDEX_KB.md` 的 "Pending Review" 区，
   用户确认后才提升为正式状态。
5. **原始资料留存（`<stem>.sources/`）**：凡是来自联网检索的笔记，把原始资料留存在同级
   `knowledge/<NN_topic>/<note_stem>.sources/`——每个来源一个 `NN_<slug>.md`
   （关键摘录 + 来源 URL + 检索日期 + 优先级 P0–P4）外加 `_manifest.md` 索引。
   该文件夹是**参考资料，不是笔记**：绝不注册进 `INDEX_KB.md`、绝不渲染为 HTML、绝不走 draft 管线。

---

## 3. server.py 对 skill 的两种耦合方式

理解这点是判断"创建 / 修改 skill 是否需要改 server.py"的关键。

### 方式一：硬编码路由（chat-coach / file-personalizer 型）

server.py 在特定 mode 下，用**硬编码路径**读取 skill 的文件并注入 prompt：

- `_read_baguwen_flow()` → 硬编码读 `skills/baguwen/flow.md`（`server.py` L2099）
- `_read_interview_flow()` / `_read_interview_rubric()` → 硬编码读 `skills/interview/flow.md`、`rubric.md`
- `_extract_resume_text()` → 服务端预抽取 PDF 文本，供 personalize 使用（L2135）
- 生成完知识后，prompt 里硬编码 `python3 skills/render_html/render.py …` 调用渲染器

> ⚠️ **如果创建 / 修改这类 skill 改动了入口或工作流，必须手动改 server.py，否则不生效。**

### 方式二：Agent 自发现（knowledge-evolver / 通用型）

在 `agent` mode 下，`build_agent_prompt()`（L2405）告诉 Agent：
`skills/ 目录下的技能（可自行阅读 skill.json 了解用法）`。
Agent 自行读取目标 skill 的 `skill.json` 与 `flow.md` 并执行——**无需任何 server.py 改动**。

`search_evolve` 与 **yuan-skill 本身**都走这条路径（自发现仍然可用）。

> 注：yuan-skill 现在**同时**拥有一条方式一（硬编码路由）路径——顶部导航栏「元 skill」
> 按钮对应 `YUAN_MODE`，由 server.py 读取本 skill 的 `flow.md` 注入 prompt（见下表）。
> 两条路径并存：导航按钮走方式一，`agent` mode 仍可方式二自发现。

---

## 4. paper-ui 当前可用模式（server.py 模式路由）

| mode 常量 | 值 | 耦合的 skill | 说明 |
|-----------|-----|-------------|------|
| `BAGUWEN_MODE` | `baguwen` | baguwen | 八股口测，硬编码读 flow.md |
| `INTERVIEW_MODE` | `interview` | interview | 模拟面试，硬编码读 flow.md + rubric.md |
| `YUAN_MODE` | `yuan` | yuan-skill | 元 skill：硬编码读 flow.md，新建/修改 skills/ 下文件 |
| `AGENT_MODE` | `agent` | 任意（自发现） | 通用 Agent，自读 skills/*/skill.json |
| generate（内联） | — | search_evolve / personalize / render_html | 生成知识章节 |

- `INTERACTIVE_MODES = {BAGUWEN_MODE, INTERVIEW_MODE, YUAN_MODE}`
- 各模式还有 `*_complete` / `*_append_complete` 收尾变体。

---

## 5. skill.json 的运行时语义

- 方式二（自发现）下，Agent 直接读 `skill.json` 的 `description` / `trigger` / `workflow` / `constraints` 决定是否与如何使用。
- 因此对自发现型 skill，`skill.json` 是唯一的"路由契约"，字段必须自解释、可执行。
- 方式一（硬编码）下，`skill.json` 主要供人和 Agent 阅读，真正生效的是 server.py 读取的具体文件（如 flow.md）。

---

## 6. 给 yuan-skill 的硬约束

- ✅ 所有新增 / 修改文件都在 `skills/<skill-name>/` 之下。
- ❌ 不修改 `knowledge/` 内容。
- ❌ 不修改 `server.py`（如必须，改为**提醒用户手动改**，不代劳）。
- ❌ 不修改 `INDEX.md`、`paper-ui/`、`.agent/`。
