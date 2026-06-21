# Yuan Knowledge Base

[English](README.md)

Yuan Knowledge Base 是一个本地优先的个人 AI 知识工作台，用来构建、浏览、演化和练习自己的技术知识库。它把轻量 Web 控制台、文件式 Agent IPC、Markdown/HTML 知识笔记，以及可扩展的 workspace skills 放在同一个项目里。

这个项目面向 Agent 辅助学习流程：检索一个主题，保留原始资料，生成可审稿的知识笔记，用八股/面试模式练习，再按需要创建或修改自己的 skill。

## 亮点

- **本地 Web 控制台**：在 `paper-ui/` 中浏览知识库、与 Agent 对话、上传 PDF、查看任务进度、切换运行配置。
- **Agent 驱动的知识演化**：通过 Codex 或 Claude Code 检索主题、生成 draft 章节、更新索引并渲染笔记。
- **内置学习模式**：
  - `检索`：根据研究任务生成知识笔记。
  - `八股`：围绕当前打开的知识章节进行口测练习。
  - `面试`：结合简历和 JD 进行模拟面试。
  - `元 skill`：通过结构化澄清流程新建或修改 workspace skill。
  - 默认 `Agent`：自由对话和工作区协助，不进入完整知识生成管线。
- **可审稿的知识流程**：新知识先生成 draft，确认后再提升为正式内容。
- **原始资料留存**：来自联网检索的笔记可以在同级 `.sources/` 目录中保留原文摘录，方便后续复核。
- **可续聊历史**：对话历史以 JSON 形式保存在 `.agent/history/`。
- **可扩展 skills**：`skills/` 下每个子目录都是一个可复用工作流或工具。

## 快速启动

### macOS

```bash
./start.command
```

### Windows

```bat
start.bat
```

### 手动启动

```bash
python3 -u server.py --port 8741 --bind 127.0.0.1 --agent codex
```

然后打开：

```text
http://127.0.0.1:8741/paper-ui/index.html
```

启动脚本保留了 legacy compatibility 环境变量 `EVOLVEKB_PORT` 和 `EVOLVEKB_AGENT`：

```bash
EVOLVEKB_PORT=8741 EVOLVEKB_AGENT=codex ./start.command
```

## 运行要求

- Python 3.10+
- 现代浏览器
- 可选，Agent 工作流需要：
  - Codex CLI，或
  - Claude Code CLI

只浏览已有知识库时，本地 server 就够了。生成知识、面试反馈、修改 skill 等 Agent 能力，需要安装并登录所选择的 CLI。

## 项目结构

```text
yuan-knowledge-base-workspace/
├── INDEX.md              # 给人和 Agent 看的工作区规则
├── server.py             # 本地服务、API、Agent IPC、prompt 路由
├── paper-ui/             # 浏览器控制台
├── knowledge/            # Markdown/HTML 知识库
│   ├── INDEX_KB.md       # 知识库路由表和 Pending Review 索引
│   └── 01_* ... 07_*     # 冷启动精选章节
├── skills/               # Agent 可读取的 skills 和工具
│   ├── baguwen/
│   ├── interview/
│   ├── personalize/
│   ├── render_html/
│   ├── search_evolve/
│   └── yuan-skill/
└── .agent/               # 运行时 IPC、上传、历史、临时笔记
```

## 核心工作流规则

完整规则以 `INDEX.md` 为准。最重要的约定是：

1. `knowledge/01_*` 到 `knowledge/07_*` 是冷启动精选章节，默认只读。
2. 普通新增知识应追加到新的递增编号章节目录中，并注册到 `knowledge/INDEX_KB.md`。
3. HTML 是展示格式。修改 Markdown 后，需要用 `skills/render_html/render.py` 重新生成 HTML。
4. 新知识先进入 draft 审稿流程，确认后再提升为正式内容。
5. 联网检索得到的原始资料应保存在 `<note_stem>.sources/` 目录中，不要登记为知识笔记。

## Skills

- `search_evolve`：检索并生成新的知识章节。
- `personalize`：使用 PDF 简历作为上下文生成个性化知识内容。
- `render_html`：把 Markdown/JSON 产物转换成单文件 HTML。
- `baguwen`：围绕章节进行八股口测。
- `interview`：结合简历/JD 进行模拟面试。
- `yuan-skill`：通过结构化澄清创建或修改 workspace skills。

## 数据与隐私

这个工作区是本地优先的，但 Agent 工作流是否调用外部工具或模型服务，取决于你选择的 CLI。

运行时文件会放在 `.agent/` 下，包括上传的 PDF、inbox/outbox 消息、面试临时笔记和保存的聊天历史。`.gitignore` 默认排除了这些临时目录；如果你的 `knowledge/` 里有私人笔记，公开仓库前仍然需要认真检查。

## 常用命令

把 Markdown 笔记渲染为 HTML：

```bash
python3 skills/render_html/render.py knowledge/01_general/attention_tutorial.md --out knowledge/01_general/attention_tutorial.html
```

使用 Claude Code 启动：

```bash
python3 -u server.py --port 8741 --bind 127.0.0.1 --agent claude
```

使用 Codex 启动：

```bash
python3 -u server.py --port 8741 --bind 127.0.0.1 --agent codex
```

## 当前状态

这是一个个人工作区项目，目标是实用、可改、容易扩展，而不是一个已经产品化的托管服务。
