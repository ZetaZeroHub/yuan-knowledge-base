# Agent 响应速度优化计划 — Prompt 瘦身

## 问题现象

1. 所有对话模式（检索/八股/面试/元skills/Agent）Agent 回复慢
2. 元 skills 不稳定：有时 2 分钟，有时几秒

## 根因定位：Prompt 过大

实测各模式 prompt 大小（10 轮对话场景）：

| 模式 | 首轮 prompt | 10 轮 prompt | 主要膨胀来源 |
|------|-----------|-------------|------------|
| 八股 | ~12,800 chars (~6,400 tok) | ~16,000 chars (~8,000 tok) | **章节正文占 77%**（12,000 字截断） |
| Agent | ~12,700 chars | ~12,700 chars | **章节正文占 94%**（12,000 字截断） |
| 面试 | ~4,100 chars | ~6,600 chars | flow.md(2,601) + rubric(908) + kb_index(2,376) + **内容重复** |
| 元skill | ~4,350 chars | 增长缓慢 | flow.md(3,436) 占 79% |
| 检索 | ~1,100 chars | N/A | 不是问题 |

**章节源文件实际大小**：绝大多数 .md 文件在 57,000-95,000 字符，当前 `max_chars=12000` 截断后仍占 prompt 的 77-94%。

---

## 修改方案（共 5 项，全部围绕 prompt 瘦身）

### 修改 1：章节正文截断上限从 12,000 降低

**位置**：`server.py:2046` — `_read_chapter_source(current_file, max_chars=12000)`

**现状**：所有 .md 源文件都在 12,000 字以上（最小的 `modern_transformer_architecture_map.md` 也有 11,687），所以 12,000 的截断上限几乎等于"总是截满"。

**八股/Agent 模式的章节正文占了 prompt 的 77-94%，是首要瘦身目标。**

**方案**：

| 选项 | max_chars | 预计 prompt 缩减 | 风险 |
|------|----------|-----------------|------|
| A（保守） | 8,000 | 八股 prompt 从 ~12,800 降到 ~8,800（-31%） | 低，多数核心知识点在前 8,000 字内 |
| B（推荐） | 6,000 | 八股 prompt 从 ~12,800 降到 ~6,800（-47%） | 中低，靠后的知识点考不到 |
| C（激进） | 4,000 | 八股 prompt 从 ~12,800 降到 ~4,800（-62%） | 中，可能遗漏章节后半内容 |

**建议选 B（6,000）**。理由：八股/Agent 模式中 claude 拥有 Read 工具权限，当需要更深入的细节时可以自行读取原文件。prompt 中内联的章节正文主要起"快速概览"作用，6,000 字足以覆盖核心概念和章节结构。

**待决策**：选 A / B / C？

---

### 修改 2：面试 prompt 去重（`build_interview_prompt` 与 flow.md 大面积重复）

**位置**：`server.py:2263-2436` — `build_interview_prompt()`

**现状**：

`build_interview_prompt()` 中的硬编码 prompt 与 `skills/interview/flow.md` 存在大面积语义重复：

| 内容 | flow.md 已有 | prompt 又注入 |
|------|------------|-------------|
| 难度说明（标准/严格） | ✅ L139-143 | ✅ 硬编码 `diff_inst`（L2325-2327） |
| 轮次人设（auto/HR/tech/...） | ✅ L147-160 | ✅ 硬编码 `round_personas`（L2330-2337） |
| 评分标准（90-100/70-89/...） | ✅ L98-102 | ✅ 额外注入 rubric.md（L2404-2405）|
| 开场规则（resume+JD/resume_only/...） | ✅ L29-66 | ✅ 硬编码 4 种 opening 分支（L2346-2393） |

**重复造成**：flow.md 整体 2,601 字 + 硬编码的重复开场/难度/人设指令 ≈ 额外多了 ~1,500 字无用 token。

**方案**：

移除 `build_interview_prompt()` 中与 flow.md 重复的硬编码内容：
- 删除 `diff_inst` 和 `round_inst` 硬编码文本，改为只传参数名（如 `当前难度：standard`，`当前轮次人设：auto`），flow.md 中已有完整定义
- 删除 4 个 `opening` 分支的详细格式说明，这些在 flow.md 的"开场规则"里已有
- 保留开场/后续的**分支判断逻辑**（`is_opening`, `input_profile`），只是不再重复注入文案

预计缩减：面试 prompt 从 ~4,100 降到 ~2,800（-32%）

**风险**：低。flow.md 已包含完整规则，去掉重复不影响 Agent 行为。

**待决策**：是否同意此方案？或者你希望保留哪些重复作为"强调"？

---

### 修改 3：面试 prompt 中 KB 索引按需注入

**位置**：`server.py:2238-2249` — `_read_kb_index_summary(max_chars=4000)` 和 `build_interview_prompt` 中注入 kb_index 的逻辑（L2419-2428）

**现状**：面试模式始终注入 `knowledge/INDEX_KB.md` 摘要（当前 2,376 字），但只有"无简历无JD"的兜底场景才真正用到它出题。有简历/JD时仅注入一句"候选人有知识库可参考"，但 `kb_index` 变量已经被读入内存、参与 prompt 拼接判断。

**方案**：

当有简历或 JD 时，跳过 `_read_kb_index_summary()` 调用，只保留现有的一句话提示。这减少了一次文件 I/O + prompt 拼接开销。

对于无简历无JD的兜底场景，将 `max_chars` 从 4,000 降到 2,000（INDEX_KB.md 目前只有 2,376 字，几乎不截断；但如果知识库持续增长，2,000 的上限可防止未来膨胀）。

预计缩减：面试 prompt 在有简历/JD 场景下减少 ~0 字（已是一句话），在兜底场景下未来可防止膨胀。主要收益是代码清晰度。

**风险**：无。

---

### 修改 4：历史对话截断 — 多轮后只保留最近 N 轮

**位置**：`server.py:2065-2075` — `_format_history(history)`

**现状**：`_format_history()` 将 `history` 列表中的**所有**消息原样格式化为文本。前端每次调用 `sendInteractive` 时传入 `chatTranscript.slice()`（完整历史），所以：
- 第 5 轮：history 约 10 条消息，~1,500 字
- 第 10 轮：history 约 20 条消息，~3,000 字
- 第 20 轮（面试长对话）：history 约 40 条消息，~6,000+ 字

每条消息都是 prompt 的一部分，每一轮都比上一轮多消耗 ~300 字 token。

**方案**：

在 `_format_history()` 中加入截断逻辑：

```python
def _format_history(history: list, max_turns: int = 6) -> str:
    """Format history, keeping only the last max_turns pairs.
    
    Older turns are summarized as a count to preserve context awareness.
    """
    entries = [h for h in (history or []) if isinstance(h, dict) and (h.get("text") or "").strip()]
    if len(entries) > max_turns * 2:
        omitted = len(entries) - max_turns * 2
        entries = entries[-(max_turns * 2):]
        prefix = f"（前 {omitted} 条对话已省略）\n"
    else:
        prefix = ""
    lines = []
    for h in entries:
        who = "用户" if h.get("role") == "user" else "助手"
        lines.append(f"{who}：{h['text'].strip()}")
    return prefix + "\n".join(lines)
```

| 参数 | 保留轮数 | 预计效果 |
|------|---------|---------|
| `max_turns=8` | 最近 8 轮（16 条） | 保守，20 轮面试后省 ~2,400 字 |
| `max_turns=6`（推荐） | 最近 6 轮（12 条） | 平衡，10 轮后开始省 token |
| `max_turns=4` | 最近 4 轮（8 条） | 激进，可能丢失重要上下文 |

**注意**：面试模式的 `build_interview_prompt` 用 history 做阶段判断（`agent_turns` 计数、`is_opening` 检测）。这些判断直接操作原始 `history` 列表（msg 中的），不走 `_format_history`，所以截断不影响阶段判断逻辑。

**风险**：低。被截断的历史是"更早期的问答"，Agent 仍能通过最近 6 轮获得足够的对话上下文。面试笔记文件 `notes_*.md` 中保留了完整的每轮评分记录，Agent 可按需读取。

**待决策**：选 `max_turns=8` / `6` / `4`？

---

### 修改 5：元 skill prompt 中 flow.md 按需读取（延迟注入）

**位置**：`server.py:2439-2499` — `build_yuan_prompt()`

**现状**：`build_yuan_prompt` 每次都内联 `skills/yuan-skill/flow.md` 全文（3,436 字），占 prompt 的 79%。但 flow.md 中大量内容（阶段 2 方案设计、阶段 3 验证、完整的模糊点表格）只在后续轮次才需要，开场轮（`__YUAN_START__`）只需要阶段 1 的 Entry Gate 部分。

元 skill Agent 拥有 Read 权限，可以自行读取 flow.md。

**方案**：

不再内联 flow.md 全文，改为只注入摘要 + 文件路径：

```python
# 开场轮：只注入 flow.md 前几行概要
yuan_flow_brief = (
    "工作流文件：skills/yuan-skill/flow.md（需要时自行 Read 查阅完整规范）\n"
    "核心流程：阶段1 意图澄清 → 阶段2 方案设计与生成/修改 → 阶段3 验证\n"
    "阶段1 先确认「新建 vs 修改」，做模糊点分析和 P0-P2 优先级排序后再动手。"
)
```

预计缩减：元 skill prompt 从 ~4,350 降到 ~1,500（-66%）

**这可能解释了元 skill 的不稳定响应时间**：当 API 负载高时，3,400+ 字的 flow.md 会显著增加首 token 延迟；负载低时这部分处理很快。缩减 prompt 后，API 端的 input 处理时间更稳定。

**风险**：中低。Agent 需要多一次 Read 调用来获取完整 flow.md，增加 ~1-2 秒的工具调用延迟。但考虑到这换来了 prompt 瘦身 66% 带来的 API 响应加速，净收益为正。

**待决策**：
1. 是否同意延迟注入（Agent 自行 Read）方案？
2. 还是你偏好只注入 flow.md 的阶段 1 部分（约 1,800 字，缩减 48%）？

---

## 修改汇总与预计效果

| 修改 | 目标模式 | 预计缩减 | 文件 | 改动范围 |
|------|---------|---------|------|---------|
| 1. 章节截断上限 | 八股/Agent | -31% ~ -62% | server.py L2046（1 个参数） | 极小 |
| 2. 面试去重 | 面试 | -32% | server.py L2263-2436 | 中等 |
| 3. KB 索引按需注入 | 面试 | 防膨胀 | server.py L2419-2428 | 小 |
| 4. 历史截断 | 所有对话模式 | 多轮后 -30%~50% | server.py L2065-2075 | 小 |
| 5. 元 skill 延迟注入 | 元skill | -48% ~ -66% | server.py L2439-2499 | 中等 |

**组合效果**（以选项 B 为例）：

| 模式 | 当前首轮 | 优化后首轮 | 10 轮后当前 | 10 轮后优化 |
|------|---------|----------|-----------|-----------|
| 八股 | ~12,800 | ~6,800 (-47%) | ~16,000 | ~8,300 (-48%) |
| Agent | ~12,700 | ~6,700 (-47%) | ~12,700 | ~6,700 (-47%) |
| 面试 | ~4,100 | ~2,800 (-32%) | ~6,600 | ~4,100 (-38%) |
| 元skill | ~4,350 | ~1,500 (-66%) | ~5,500 | ~2,300 (-58%) |

---

## 不变项（确保现有功能不受影响）

- 前端发送逻辑（`sendInteractive` / `sendMessage`）：不修改
- 面试阶段判断逻辑（`is_opening`, `agent_turns` 等）：直接读原始 history，不受 `_format_history` 截断影响
- 八股笔记写入（`notes_*.md`）：不受章节截断影响，Agent 可 Read 完整文件
- 面试笔记写入（`notes_*.md`）：同上
- 补充完善流程（`*_complete` / `*_append_complete`）：不修改
- generate 模式 prompt：不修改（当前只有 ~1,100 字，不是瓶颈）
- 轮询间隔（watcher 1s / poll 2s）：不修改
- 文件 IPC 机制（inbox/outbox）：不修改

---

*计划生成日期：2026-06-21*
