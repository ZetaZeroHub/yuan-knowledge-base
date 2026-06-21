# yuan-skill 工作流

> Yuan Knowledge Base 原生 Skill 工厂的核心工作流。
> 方法论融合：**Ask_Why 的结构化意图澄清** + **yao-meta-skill 的 archetype / near-neighbor**。
> 全程中文。先理解，不急着改动 / 给方案——帮用户把每个重要决定想清楚。

工作前置：先读 `project-architecture.md`（项目架构）和 `existing-skills-inventory.md`（现有 skill 清单）。
若是修改现有 skill，还要先读目标 skill 的全部现有文件建立基线。

---

## 阶段 1：意图澄清

**核心理念**（来自 Ask_Why）："先理解，不急着改动 / 给方案。帮用户把每个重要决定想清楚。"

### 1.1 Entry Gate（入口判断）

只有当**以下任一为真**时才进入追问模式：

- **创建场景**：需求描述模糊、开放，或存在多种合理方向（如 chat-coach vs knowledge-evolver）。
- **修改场景**：修改动机不清、修改范围不明确，或修改可能破坏现有功能 / 与其他 skill 产生职责冲突。
- 用户偏好或设计选择会实质性影响 skill 的文件结构、交互流或边界。
- 早期错误选择或鲁莽修改会导致大量返工或破坏现有功能。

若用户已给出完整且无可争议的规格（创建时提供了完整的 name/trigger/workflow/constraints，
或修改时明确给出了具体要改的文件、具体 diff 及逻辑），**跳过追问，直接进入阶段 2**。

### 1.2 Ambiguity Map（模糊点地图）

根据操作类型自适应构建模糊点地图。

#### A. 创建新 Skill 时的 6 类模糊点

| 模糊点类别 | Ask_Why 对应 | 在 skill 创建中的含义 |
|-----------|-------------|---------------------|
| **重复性工作** | Goal ambiguity | 这个 skill 自动化什么工作？用户为什么需要它？ |
| **用户与入口** | User/audience ambiguity | 谁用？怎么触发？通过 paper-ui 哪个模式？ |
| **输入与输出** | Output format ambiguity | 输入是什么（文本/PDF/章节）？输出是什么（文件/对话/HTML）？ |
| **交互模式** | Interaction granularity | 对话交互（多轮）还是一次性生成？需要状态文件吗？ |
| **边界与不做** | Boundary/non-goal ambiguity | 明确不做什么？和哪个现有 skill 边界模糊？ |
| **风险与约束** | Risk tolerance ambiguity | 允许联网？允许改文件？安全约束是什么？ |

#### B. 修改现有 Skill 时的 6 类模糊点

| 模糊点类别 | Ask_Why 对应 | 在 skill 修改中的含义 |
|-----------|-------------|---------------------|
| **修改动机** | Goal ambiguity | 为什么要修改？修 bug、优化体验，还是新增子功能？ |
| **受影响范围** | Output/Scope ambiguity | 只改内部逻辑，还是会改变输入输出、触发条件或工作流？ |
| **向下兼容性** | Compatibility ambiguity | 修改后是否影响已有用户对话状态、配置或关联文件（如 rubric.md）？ |
| **与 server.py 耦合** | Coupling ambiguity | 该 skill 是否与 server.py 硬编码绑定？修改是否会打破现有路由？ |
| **边界与重叠** | Boundary/non-goal ambiguity | 新增功能是否应属于另一个现有 skill？是否导致职责重叠？ |
| **风险与回归** | Risk tolerance ambiguity | 可能引入什么副作用？如何确保修改后现有链路不被破坏？ |

### 1.3 优先级排序

| 级别 | Ask_Why 定义 | 在 Skill 工厂中的含义 |
|------|-------------|---------------------|
| **P0** | 阻塞设计 / 修改 | 创建：重复性工作 + 输入输出不清（无法选型）<br>修改：修改动机不清 + 改动范围不明（无法确定修改点） |
| **P1** | 改变核心方案 | 创建：交互模式 + 边界（影响文件结构）<br>修改：兼容性要求 + 耦合方式变更（影响实现方案与 server.py） |
| **P2** | 可暂时假设 | 具体的 workflow 步骤、rubric 评分标准、优化提示词等（可先生成 / 修改后再迭代） |

### 1.4 Decision Rounds（决策轮次）

借鉴 Ask_Why 的问题卡格式，但**轻量化**。

**问题卡格式**（每个问题包含）：

```
### 问题 X：[核心问题]
为什么现在必须回答：[影响说明 / 潜在副作用]
选项：A. … / B. … / C. …
推荐：[推荐选项 + 理由]
你也可以用自己的话回答。
```

**节奏控制**：

- 每轮 3-5 个聚焦问题。
- 第 1 轮偏发散与风险提示：指出新需求的潜在弱点、与现有 skill 的重叠风险（近邻检查）、
  修改可能带来的副作用与回归风险。
- 第 2 轮开始收敛：当 P0 问题解决后进入阶段 2。
- 如果用户连续 2 轮无新方向，直接进入阶段 2。

**轻量化适配**（与 Ask_Why 的差异）：

- ❌ 不创建 `p2a-session/` 等状态文件夹。
- ✅ 在对话中内联维护意图与修改计划状态。
- ✅ 通常 1-2 轮即可收敛（修改或创建 skill 的决策路径较短）。

---

## 阶段 2：方案设计与生成 / 修改

### 2.1 场景 A：创建新 Skill

1. 读取 `existing-skills-inventory.md` 做**近邻检查**（near-neighbor）：确认新 skill 与现有 5 个不重叠。
2. 根据阶段 1 收集到的意图，判断属于 4 种模式中的哪一种（读 `skill-patterns.md`）。
3. 使用对应模板在 `skills/<new-skill-name>/` 生成完整 skill 文件包。

### 2.2 场景 B：修改现有 Skill

1. 读取目标 skill 的所有现有文件（如 `skill.json`、`flow.md`、任何关联 python 或 markdown 文件）。
2. 根据阶段 1 的澄清结果，设计**最小化、高精度**的修改方案（精确到具体文件、具体行与逻辑）。
3. **安全原则**：禁止大范围重写，优先增量修改，确保不破坏原有的正常链路与契约。
4. 应用修改至目标 skill 文件。

---

## 阶段 3：验证

1. 检查生成 / 修改后的文件是否符合项目规范（对照 `project-architecture.md`）。
2. 确认 `skill.json` 必要字段齐全（name / description / trigger / workflow / constraints）。
3. 检查是否与现有 skill 的职责产生非预期的重叠。
4. 如果创建或修改的是需要 server.py 硬编码路由的 chat-coach 型 skill，且改动了入口或工作流，
   **明确提醒用户需要手动修改 server.py**（参见 `project-architecture.md` 的耦合说明）。
