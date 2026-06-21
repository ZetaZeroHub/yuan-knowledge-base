# Skill 模式库（4 种 archetype）

> 从现有 5 个 skill 归纳出的 4 种模式。创建新 skill 时，先在阶段 2 判定属于哪一种，
> 再套用对应模板。判定不清时，回到 flow.md 阶段 1 追问"交互模式 / 输入输出"。

| 模式 | 代表 | 特征 | 必需文件 | 可选文件 | server.py 耦合 |
|------|------|------|---------|---------|---------------|
| **chat-coach** | baguwen, interview | 对话交互、一问一答、状态文件 | `skill.json` + `flow.md` | `rubric.md`、状态笔记 | **方式一：硬编码**，需手动改 server.py |
| **knowledge-evolver** | search_evolve | 联网检索、生成知识章节、注册索引 | `skill.json` | `search_prompts.md`、prompt 模板 | 方式二：自发现，零改动 |
| **file-personalizer** | personalize | 上传文件 → 解析 → 生成个性化内容 | `skill.json` | `pdf_parser.py`、解析器 | 方式一（服务端预抽取），常需改 server.py |
| **utility-tool** | render_html | 确定性转换、被其他 skill 调用 | `render.py` 或脚本 | `templates/`、配置 | 方式一：被 prompt 硬编码调用 |

---

## 模式 1：chat-coach（对话教练型）

**何时用**：需要多轮一问一答、追问、评分、记录盲点。典型如测验、面试、苏格拉底式辅导。

**必需文件**：`skill.json` + `flow.md`。
**可选文件**：`rubric.md`（评分标准）、运行时状态笔记。
**⚠️ 耦合提醒**：该模式靠 server.py 在专属 mode 下硬编码读 flow.md。生成后**必须提醒用户手动在 server.py 添加 mode 路由 + `_read_<name>_flow()`**，否则前端无法触发。

### `skill.json` 模板

```json
{
  "name": "<skill-name>",
  "description": "<一句话：这个教练帮用户做什么>",
  "trigger": "When the user enters <模式名> mode.",
  "flow": "flow.md",
  "rubric": "rubric.md",
  "constraints": ["一次只问一个问题", "不提前透题", "全程中文", "<其它边界>"]
}
```

### `flow.md` 模板

```markdown
# <skill-name> 工作流
你是 <角色>。每次只问一个问题，等用户回答后再追问。全程中文。

## 流程
1. <开场 / 选题>
2. <提问规则：一次一题、由浅入深>
3. <追问 / 评分规则（可引用 rubric.md）>
4. <收尾：复盘 + 记录盲点到 <状态文件>>

## 约束
- <不做什么>
```

---

## 模式 2：knowledge-evolver（知识演化型）

**何时用**：联网检索权威资料 → 校验 → 编译成新知识章节 → 走 draft 管线。**yuan-skill 自身与 search_evolve 同属此类。**

**必需文件**：`skill.json`（自解释、可执行——这是唯一路由契约）。
**可选文件**：`search_prompts.md` 或 prompt 模板。
**✅ 耦合提醒**：方式二自发现，**零 server.py 改动**。用户在 agent mode 下 Agent 自读 skill.json。

### `skill.json` 模板

```json
{
  "name": "<skill-name>",
  "description": "<一句话：检索什么、产出什么知识>",
  "trigger": "When the user requests <检索 / 综述 / 演化 触发条件>.",
  "priority_matrix": {
    "P0": "ArXiv / 顶会论文", "P1": "官方白皮书 / 厂商文档",
    "P2": "高星 GitHub 实现", "P3": "专家深度博客", "P4": "社媒线索（仅引子，不作推导来源）"
  },
  "quality_gate": { "required": ["严谨推导", "可运行代码 + shape 注释", "真实引用"], "forbidden": ["无指标的模糊总结", "已弃用 API"] },
  "output_format": "knowledge/[NN]_[topic]/*.draft.html",
  "raw_sources": { "dir": "knowledge/[NN]_[topic]/[note_stem].sources/", "rule": "每来源一个 NN_<slug>.md + _manifest.md；参考资料，不注册不渲染。" }
}
```

> 普通知识演化任务必须遵守 INDEX.md 规则 2/4/5：新章节递增编号、先 `.draft.html` 进 Pending Review、`.sources/` 留存。若用户明确要求维护 `01_*` ~ `07_*` 冷启动库，则按 INDEX.md 的 cold-start seed 例外处理，并在加入后视为只读冷启动内容。

---

## 模式 3：file-personalizer（文件个性化型）

**何时用**：用户上传文件（PDF/简历等）→ 解析 → 生成个性化内容。

**必需文件**：`skill.json`。
**可选文件**：解析脚本（如 `pdf_parser.py`）。
**⚠️ 耦合提醒**：通常由 server.py **预抽取文本**注入 prompt（如 `_extract_resume_text()`），Agent 不自己跑 pdftotext。新增此类 skill 常需在 server.py 加预处理，**提醒用户手动改**。

### `skill.json` 模板

```json
{
  "name": "<skill-name>",
  "description": "<解析什么文件 → 生成什么个性化内容>",
  "trigger": "When user uploads <文件类型> or requests <个性化场景>.",
  "workflow": [
    "1. <文件文本由服务端预抽取并注入 prompt，不要自己解析>。",
    "2. 提取关键信息：<字段>。",
    "3. 联网检索（独立路径并行）。",
    "4. 按 INDEX.md 规则 5 留存 .sources/。",
    "5. 合成为单个 draft HTML，注册到 Pending Review，等待确认。"
  ],
  "output_format": "knowledge/[NN]_<topic>/<stem>.draft.html"
}
```

---

## 模式 4：utility-tool（确定性工具型）

**何时用**：确定性转换 / 计算，被其他 skill 或 server.py 调用，无对话、无联网。

**必需文件**：脚本（如 `render.py`）。
**可选文件**：`templates/`、配置、`skill.json`（描述用法）。
**⚠️ 耦合提醒**：往往被 prompt 硬编码调用路径（如 `python3 skills/render_html/render.py …`）。改动 CLI 接口会波及调用方，**提醒用户检查 server.py 中的调用点**。

### `skill.json` 模板

```json
{
  "name": "<skill-name>",
  "description": "<确定性地把 X 转换成 Y>",
  "trigger": "Called by other skills or server.py when <场景>.",
  "usage": "python3 skills/<skill-name>/<script>.py <input> [--opt ...]",
  "constraints": ["纯 stdlib，无第三方 pip 依赖", "<确定性 / 单文件输出等>"]
}
```

### 脚本约定

- 纯标准库，无 pip 依赖（与 render_html 一致，保证零环境配置）。
- 入参 / 出参用 CLI flag 明确；失败要有非零退出码与清晰报错。
