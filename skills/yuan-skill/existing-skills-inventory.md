# 现有 Skill 清单（近邻检查用）

> 创建新 skill 前，先对照本清单做 **near-neighbor 检查**：确认新需求不与下列任何 skill 的职责重叠。
> 若重叠，应优先考虑"修改现有 skill"而非新建。
>
> **维护方式：静态文件（决策 A1）**。新增 / 删除 skill 后请手动更新本表。
> 截至 2026-06-20，工作区共 **5 个** skill（不含 yuan-skill 自身）。

| skill | 模式 archetype | 职责（做什么） | 边界（不做什么 / 不要与之重叠） | 触发 / 入口 |
|-------|---------------|---------------|-------------------------------|------------|
| **baguwen** | chat-coach | 知识库八股口测：逐章提问、追问薄弱点、记录盲点笔记 | 不写入 knowledge/（仅追加盲点笔记）；一次只抛一个问题；全程中文 | paper-ui `baguwen` 模式 |
| **interview** | chat-coach | 真实求职模拟面试：基于简历 + 岗位 + 知识库一题一答、追问、评分、复盘 | 不编造经历；不提前透题；不默认写入 knowledge/ | paper-ui `interview` 模式 |
| **search_evolve** | knowledge-evolver | 系统性联网检索：按 P0–P4 优先级检索、校验，编译成新知识章节（draft） | 仅生成知识章节；不处理上传文件；不做对话辅导 | generate 模式 / agent 自发现 |
| **personalize** | file-personalizer | 解析用户 PDF 简历 → 按 A/B/C/D 路径生成个性化知识章节 | 不自己跑 pdftotext（服务端预抽取）；输出单个 draft；等待确认 | 上传 PDF / generate 模式 |
| **render_html** | utility-tool | 把 Markdown/JSON 转成单文件 HTML（academic / dashboard 模板） | 纯 stdlib，无 pip 依赖；只做确定性渲染；被其他 skill 或 server.py 调用 | `python3 skills/render_html/render.py …` |

---

## 近邻冲突速查

- 想做"测验 / 提问 / 辅导用户" → 很可能撞 **baguwen** 或 **interview**，先判断是否只需扩展它们。
- 想做"联网找资料生成知识" → 撞 **search_evolve**，除非检索域 / 产出形态显著不同。
- 想做"处理用户上传的文件" → 撞 **personalize**，除非文件类型 / 产出不同。
- 想做"格式转换 / 渲染" → 撞 **render_html**，优先扩展其模板而非新建。

若新需求确实落在以上任一格内，回到 `flow.md` 阶段 1，用"边界与重叠"问题卡与用户确认是改还是建。
