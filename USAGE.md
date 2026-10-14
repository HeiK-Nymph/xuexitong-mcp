# xuexitong_mcp 使用说明（写给 AI 助理看）

> 你拿到这个项目时，按本文 **4 步**配置好，就能替用户操作《学习通 / 超星》的作业：
> 查课程 → 查作业 → 下附件 → 解压/转格式 → 取材料全文 → （你来分析作答）→ 回填 DOCX → 上传附件 → 准备提交 →（用户确认后）提交。
>
> **本项目不调用任何 LLM。** 分析、判断、写内容都由**你**（当前窗口的模型）做；
> MCP 只负责取材料、写文件、上传、提交这些"手"的活。

---

## 0. 它是什么

- 一个 **stdio JSON-RPC** 的 MCP 服务（`server.py`，19 个工具），零框架依赖。
- 凭据**只**从环境变量 / `.env` 读，**绝不作为工具参数传入**。
- 只依赖：`httpx` / `cryptography` / `beautifulsoup4` / `python-docx`。
- 项目目录里**只有代码 + 文档 + 测试**（运行时数据全部落在作业根目录，见第 3 节）。

## 1. 环境要求

| 项 | 说明 |
| --- | --- |
| 系统 | Windows（首选；其他平台部分兜底） |
| Python | ≥ 3.10 |
| 依赖 | `pip install -r requirements.txt` |
| 可选 | WinRAR 或 7-Zip（解压 `.rar/.7z`）；Word 或 LibreOffice（`.doc → .docx`） |
| 可选 | 浏览器 + BrowserSkill CLI `bsk`（**只有兜底收割作答页时才用**，服务本体不依赖） |

## 2. 配置（两步就够）

### 2.1 凭据

查找顺序（前面的优先）：**进程环境变量 → 项目根 `.env` → `<作业根>/.xuexitong_mcp/.env`**

推荐做法：把 `.env.example` 复制到 `D:\学习通作业\.xuexitong_mcp\.env`，填两项即可：

```ini
XUEXITONG_USERNAME=你的手机号或账号
XUEXITONG_PASSWORD=你的密码
```

> ⚠️ **这一步必须做**（账号密码或 cookie 任一项）。凭据不随项目分发：换机器 / 重新导入后要重配，
> 否则服务**能启动**，但每个工具都报缺凭据（`MissingCredential`）。

也可以只给 cookie（优先于账号密码）：`XUEXITONG_COOKIE=...`（格式 `a=1; b=2`）。
其它可选变量：`XUEXITONG_ROOT`（作业根，默认 `D:/学习通作业`）、`XUEXITONG_STATE_DIR`、
`XUEXITONG_STATE_PATH`（cookie 文件位置）、`XUEXITONG_INSECURE=1`（放宽 TLS，默认严格）。

> ⚠️ 真实 `.env` **不要**放进项目目录、不要提交 git。分享项目时务必确认项目里没有 `.env`。

### 2.2 注册到 MCP 客户端

Codex：

```powershell
codex mcp add xuexitong -- <你的 python.exe 绝对路径> <本项目绝对路径>\server.py
# 例：codex mcp add xuexitong -- D:\Python\python.exe D:\myproject\McpAndSkill\mcp\xuexitong_mcp\server.py
```

通用 JSON（Claude Desktop / 其他客户端）：

```json
{ "mcpServers": { "xuexitong": {
    "command": "D:\\Python\\python.exe",
    "args": ["D:\\myproject\\McpAndSkill\\mcp\\xuexitong_mcp\\server.py"] } } }
```

也可以注册时顺手把凭据注进去（会**明文写进** `~/.codex/config.toml`）：

```powershell
codex mcp add xuexitong `
  --env XUEXITONG_USERNAME=你的手机号或账号 `
  --env XUEXITONG_PASSWORD=你的密码 `
  -- <你的 python.exe 绝对路径> <本项目绝对路径>\server.py
```

或在 `~/.codex/config.toml` 里加（等价）：

```toml
[mcp_servers.xuexitong.env]
XUEXITONG_USERNAME = "你的手机号或账号"
XUEXITONG_PASSWORD = "你的密码"
```

> 用**你自己的**解释器与项目路径；别照抄示例里的路径。
> 改完 `config.toml` 要**重启 Codex**（或重连该 MCP server）才生效。

## 3. 运行时数据放哪里（保证项目目录纯净）

| 内容 | 位置 |
| --- | --- |
| 作业附件、完成版 DOCX | `<作业根>/<账号uid>/<作业名>/`（默认 `<作业根>` = `D:/学习通作业`） |
| 服务状态：cookie、菜单缓存、材料缓存、提交 payload、作答页地址表 | `<作业根>/.xuexitong_mcp/` |
| 项目自身 | 只有代码、文档、测试 → 可直接打包分享/提交 |

改作业根：工具参数 `root=...`，或环境变量 `XUEXITONG_ROOT`；state 可用 `XUEXITONG_STATE_DIR` 单独指定。

## 4. 工具清单（19 个）

### 4.1 只读查询

| 工具 | 用途 |
| --- | --- |
| `whoami` | 会话是否有效（uid / 凭据来源 / cookie 路径）。**接手任务第一件事** |
| `list_courses` | 课程列表（course_id / class_id / cpi） |
| `list_homeworks` | 作业列表，`status=any\|unfinished\|completed`；附带状态文案分布与「状态未识别」清单 |
| `probe_homework_files` | 诊断某作业的附件、压缩包内容、本机能不能解压/转换 |
| `get_homework_materials` | **核心**：该作业所有文件清单 + 逐份文档全文 + 模板建议。目录里没有可读文档时，会**自动把「题目内嵌附件」取回本地**（题目正文常是题目里嵌的一份 docx）；`node_modules` 等依赖/缓存目录不算材料 |
| `open_homework_submit_page` | 打开新版作答页，回显上传令牌/题目 id/提交次数/表单 action |
| `list_prepared_submissions` | 清点"阶段一"产物（payload、将交附件、完成版 DOCX） |

### 4.2 下载 / 写盘

| 工具 | 用途 |
| --- | --- |
| `download_unfinished_attachments` | 【正式功能】下载全部**待做**作业的附件 |
| `download_homework_attachments` | 下载指定 `work_id` 的附件 |
| `download_random_completed_attachments` | 测试用（账号没有待做作业时验证链路，**要说明这不是正式功能**） |
| `apply_homework_edits` | 锚点式回填：`edits=[{anchor, content, mode}]` → 生成 `*_完成版.docx` |

### 4.3 提交链路（准备 → 提交）

| 工具 | 用途 |
| --- | --- |
| `upload_homework_attachment` | 只上传附件，返回 `objectid` + 可插入富文本的 `insert_html` |
| `prepare_homework_submission` | 组装提交 payload 并落盘，产出 `review_token` 与 `review_checklist` |
| `submit_homework` | 【不可逆】真提交，必须 `confirm=true` **且** `review_token` 正确 |
| `register_study_page` | 兜底：个别课程菜单没有「体验新版」入口时，人工给一次新版课程页 URL |

### 4.4 批量（服务端循环，单项失败不影响其它）

`batch_prepare_materials` / `batch_apply_edits` / `batch_prepare_submissions` / `batch_submit`

## 5. 标准用法（两段式，中间必须停）

### 阶段一：拉取并做作业（用户说"帮我把作业做了"）

1. `whoami` → 确认账号。
2. `list_homeworks(status="unfinished")` → 把结果**原文**告诉用户（没有待做作业就直说，不要说成"已下载"）。
3. 每个作业派一个子 agent（并行）。子 agent 的操作手册是 `docs/homework-playbook.md`：
   `get_homework_materials` → 自己分析 → `apply_homework_edits` → `upload_homework_attachment`
   → `prepare_homework_submission`。
4. 主 agent 用 `list_prepared_submissions` 把成品清单**原文**摆给用户看，然后**停下**。

### 阶段二：用户的下一条命令

- 用户说「**重修 / 再改**」→ 你自己灵活改文件（**这一段不用 MCP**）；改完要提交则重新走 `prepare`（文件变了）。
- 用户说「**提交**」→ 主 agent 逐个 `submit_homework(work_id, confirm=true, review_token=…)`，
  一次一个、每个都把返回的 `summary` 原文转述给用户。

## 6. 硬规则（必须遵守，违反会造成真实损害）

1. **完成和提交永远不能一步做完。** 即使用户说"做完直接交""不用给我看"，也必须先展示成品、等用户审核。
   技术闸门：`submit_homework` / `batch_submit` 除 `confirm=true` 外**必须**带 `prepare_homework_submission`
   产出的 `review_token`；缺 token / token 过期一律**零请求**拒绝。
2. **子 agent 只做阶段一**，禁止调用 `submit_homework` / `batch_submit`（连预览都别调）。
3. **个人信息禁止编造**：姓名/学号/班级/学院/日期/指导教师等只有用户知道；代码会在 `apply_homework_edits`
   层拒绝模型给的值 —— 把它列进 `needs_user_fields` 交给用户。
4. **材料不足必须明说**：附件里没有完成任务所需信息时，用 `{"INSUFFICIENT": true, "reason": "…"}` 标注，
   禁止猜测、禁止跳过、禁止硬填。
5. 调用 `download_*` / `prepare_*` / `submit_*` / `register_study_page` 之后，**把返回的 `summary` 原文转述**
   给用户（含下载了几个文件、落了哪个目录、失败几个）。
6. 批量操作要节流；遇到反爬（HTTP 202 / 验证页）**停下来报告**，不要死循环重试。
7. 提交被拒（"作业已截止"/提交次数超限要图形验证码）→ **让用户手动提交**，不要绕。

## 7. 常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| `whoami` 报会话不可用 | 检查 `.env` 是否配好/是否被验证码拦住；必要时改用 `XUEXITONG_COOKIE` |
| 某作业拿不到作答页 | 站点对新版作答页只给 `/work/view` → **该作业已截止**，不能上传/提交，如实告诉用户 |
| `enc` 自动发现失败 | 先重试一次；仍失败再看课程菜单有没有「体验新版」入口，没有就用 `register_study_page` |
| 压缩包解不开 | 本机缺 WinRAR/7-Zip；`probe_homework_files` 会明确说缺什么 |
| `.doc` 读不了 | 缺 Word/LibreOffice；手动另存为 `.docx` 后重试 |
| 找不到附件在哪 | `<作业根>/<账号uid>/<作业名>/`，清单在同目录 `_attachments.json` |
| 报「该作业没有任何附件」 | 先看是不是**题目内嵌附件**（题目正文＝题目里嵌的一份文档）：现在的 `get_homework_materials` 会自动取回；仍为空才是真没材料，见 `docs/findings-prompt-attachments.md` |
| 提交报「无效的参数：code-1！」 | `answerwqbid` 没带上（应为 `"题目id,"`）。本站提交/保存都必须带，见 `docs/findings-submit-payload.md` |
| 提交回 `success` 但正文是空的 | `answers` 的键被写成了表单字段名（真正的 `answer<qid>` 没被覆盖）。修复与自查见 `docs/findings-submit-payload.md` |
| 取材料把 `node_modules` 也吞了 | 已修：依赖/缓存目录会被跳过并写进 `warnings`；单次最多列 400 个文件 |

## 8. 自检

```powershell
python tests\run_all.py     # 应当 126 项全绿（全离线，不联网、不写用户目录）
```

进一步的设计与实测记录（可选读）：`README.md`（架构与安全设计）、
`docs/main-agent-orchestration.md`（主 agent 编排）、`docs/homework-playbook.md`（子 agent 手册）、
`docs/findings-*.md`（登录加密、附件、账号切换、enc 自动发现、**提交 payload 的两个坑**、**题目内嵌附件**的实测笔记）。
