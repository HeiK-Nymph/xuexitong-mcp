# xuexitong_mcp

> **只想装起来用？直接看 [USAGE.md](USAGE.md)** —— 那份是写给 AI 助理的操作说明（配置 4 步 + 19 个工具 + 硬规则 + 排错）。
> 本 README 讲的是**设计与实测依据**（为什么这么做、踩过什么坑）。

## 认证层

学习通（超星）账号**登录 / 会话维护**层。协议取自参考实现 chaoxing_mcp（HTTP `fanyalogin`），
但修掉了它的三个坑，并把凭据从 tool 参数挪到 env。

## 最优路径（本层实现的策略）

```
磁盘 cookie（带 expires，过期即丢） → 探测会话是否真的有效
        ├─ 有效 → 直接复用，零登录请求
        └─ 失效 → 删除文件，往下走
env 里的 XUEXITONG_COOKIE → 探测有效 → 落盘复用
账号密码（AES-128-CBC 加密后 POST /fanyalogin）→ 成功后落盘带 expires 的 cookie
都没有 → 返回 NO_CREDENTIAL，并明确告诉用户该配哪个环境变量
```

三条铁律（都有单测钉住）：

1. **不每次重登**：会话能复用就复用 —— 频繁重登是触发滑动验证的主因。
2. **不静默重试**：登录页 `needVcode=1` 时直接返回 `NEED_HUMAN_VERIFICATION`，一次登录请求都不发。
3. **不假成功**：判据是"受保护页面没被 302 回登录页"，不是"cookie 里有没有 `_uid`"。

## 与参考实现（chaoxing_mcp）的差异

| | chaoxing_mcp | 本实现 |
|---|---|---|
| 凭据位置 | tool 参数（进 LLM 上下文、进 `Account.password` 字段） | env（MCP env 注入 / `.env` 兜底），只进内存 |
| 密码加密 | 无，但发 `t=true`（自相矛盾） | AES-128-CBC / PKCS7 / IV=key / Base64，与官方 JS 逐字节一致 |
| cookie 落盘 | `pickle`，且回填时丢 `expires` | JSON，保留 `expires/httpOnly/secure/domain/path`，加载时丢弃过期项 |
| 登录成功判据 | 文本含"恭喜您" / `status` / **cookie 存在即成功** | 302→登录页 判否 + SSO 补充 uid |
| 失败原因 | "用户名或密码错误，或需要验证码" 一锅烩 | `BAD_CREDENTIALS` / `NEED_HUMAN_VERIFICATION` / `NEED_2FA` / `WEAK_PASSWORD` / `HTTP_ERROR` |
| 登录验证码 | 完全不处理 | 检测出来并交给人工，不硬试 |
| TLS | `verify=False` | 默认严格校验，`XUEXITONG_INSECURE=1` 才放宽 |

## 配置

**方式 1（推荐）：注册时注入 env**，Codex 会把它写进 `~/.codex/config.toml`（明文）：

```powershell
codex mcp add xuexitong `
  --env XUEXITONG_USERNAME=手机号/超星号 `
  --env XUEXITONG_PASSWORD=密码 `
  -- D:\Python\python.exe D:\myproject\McpAndSkill\mcp\xuexitong_mcp\server.py
```

等价于在 `~/.codex/config.toml` 里加：

```toml
[mcp_servers.xuexitong.env]
XUEXITONG_USERNAME = "手机号/超星号"
XUEXITONG_PASSWORD = "密码"
```

**方式 2（不写 config.toml）：`<作业根>/.xuexitong_mcp/.env`** —— 把 `.env.example` 复制过去填两项即可，
别放进项目目录。

查找顺序：**进程环境变量 → 项目根 `.env` → `<作业根>/.xuexitong_mcp/.env`**；
三者都没有时服务**照常启动**，但每个工具都会报缺凭据（`MissingCredential`）。

可选变量：`XUEXITONG_COOKIE`（优先于账号密码）、`XUEXITONG_ROOT`、`XUEXITONG_STATE_DIR`、
`XUEXITONG_STATE_PATH`、`XUEXITONG_INSECURE`。

## 使用

```powershell
# 1) 先做无凭据体检：端点是否可达、未登录特征是否符合预期
python scripts/login_once.py --smoke

# 2) 真实登录（读 env），成功后把 cookie 落盘
python scripts/login_once.py --business

# 3) 只想确定"哪条登录路径是对的"（用自己的账号，最多 2 次请求）
python scripts/ab_login.py --yes
```

## 测试

```powershell
python tests/run_all.py              # 27 个用例（crypto / session / 编排端到端）
python scripts/check_crypto_vs_js.py # 与真 crypto-js 逐字节对照，并生成 tests/js_vectors.json
python scripts/probe_login_paths.py  # 真实服务器只读探测（加 --yes 才发登录请求）
python scripts/probe_endpoints.py    # 候选探测端点的未登录特征
```

`tests/test_auth_flow.py` 跑在 `tests/mock_chaoxing.py`（按实测协议形状写的本地模拟服务）上，
覆盖：cookie 复用 / 过期回落 / 失效回落 / env cookie / 需要验证码时不发登录请求 /
密码错不落盘 / state 文件不含明文密码 / 发出的确实是 AES 密文。

## 实测结论（本机、2026-10-13，未使用任何真实账号）

| 结论 | 证据 |
|---|---|
| AES 实现与官方 `login.js` 的 `encryptByAES` **逐字节一致** | `check_crypto_vs_js.py` 6/6 样本（含中文、空串、多分组） |
| 未登录特征：受保护页面 **302 → `passport2.chaoxing.com/login?...`** | `probe_endpoints.py`：`i.chaoxing.com/base`、`mycourse/backclazzdata`、`mooc1-2/visit/interaction` 均如此 |
| SSO `userLogin4UAP.do` 未登录返回 `{"result":"0","errorMsg":"…[50001]"}`（HTTP 200） | 同上 —— 所以它**不能**单独当判据，只能补充 uid |
| 登录失败响应形状：`{"status":false,"msg2":"用户名或密码错误"}` | 假账号实测；`msg2` 是唯一可信的失败原因字段 |
| AES / 明文+t=true / 明文+t=false 三形态在**假账号**下返回同一句错误 | 该实验**无法区分**三者：账号不存在时服务端在查用户阶段就短路，走不到密码比对。**要区分必须用真实账号**（见 `ab_login.py`） |

## 尚未覆盖 / 已知边界

- **滑动验证（`needVcode=1`）**：本层只负责检测并明确报错。要自动化得另配浏览器方案
  （Playwright 有头窗口，人工拖一次），或改用 `XUEXITONG_COOKIE`。
- **双因子 / 弱密码**：同样只识别并回报（`NEED_2FA` / `WEAK_PASSWORD`）。
- **cookie 生命周期**：学习通会话会失效，届时会重新走账号密码；本层保证"失效能被识别"而不是拖着重登。
- **多账号**：当前按单账号设计（一份 state 文件）。要支持多账号需引入 `account` 维度。
- 本层**只做认证**，不包含作业/课程等业务工具；业务层应复用 `SessionInfo.client`。

## 目录

```
xuexitong/
  config.py    env / .env 读取，凭据只进内存
  crypto.py    AES-128-CBC（key==IV）加密，输出 Base64
  session.py   cookie JSON 持久化（保留 expires、原子写入）、客户端构造
  login.py     登录、探测、错误分类、ensure_session 编排
scripts/
  login_once.py            真实登录入口
  ab_login.py              真实账号 A/B（AES vs 明文）
  probe_login_paths.py     登录路径实测
  probe_endpoints.py       探测端点未登录特征
  check_crypto_vs_js.py    Python AES vs 官方 crypto-js
  js_ref/encrypt_by_aes.js 官方 encryptByAES（抄自 login.js，仅对照用）
tests/
  mock_chaoxing.py         本地模拟学习通（协议形状来自实测）
  test_crypto.py test_session.py test_auth_flow.py run_all.py
```

---

# MCP 服务

`server.py` 是 stdio（按行分隔 JSON-RPC 2.0，与 gf2_ssr_mcp 同一套协议实现，零框架依赖）。
**凭据只从环境变量读取（兜底：项目根 `.env` → `<作业根>/.xuexitong_mcp/.env`），绝不作为工具参数传入。**

## 工具

| 工具 | 类型 | 说明 |
| --- | --- | --- |
| `whoami` | 只读 | 会话是否有效，返回 uid / 凭据配置状态 / cookie 文件路径 |
| `list_courses` | 只读 | 课程列表（course_id / class_id / cpi） |
| `list_homeworks` | 只读 | 作业列表，`status=any\|unfinished\|completed`；同时返回原始状态文案分布与「状态未识别」清单 |
| **`download_unfinished_attachments`** | **写盘（正式功能）** | 下载全部**未完成(待做)**作业的附件 → `<root>/<作业名>/<附件名>`，默认 root=`D:/学习通作业`；支持 `dry_run` |
| `download_homework_attachments` | 写盘 | 下载指定 `work_id` 的附件（补下已完成/待批阅用） |
| `download_random_completed_attachments` | 写盘 | **测试用**：随机 N 个已完成作业，用于验证下载链路 |
| `register_study_page` | 写缓存（按账号隔离） | **兜底，一般用不到**：只在个别课程菜单没有「体验新版」入口时才要手工给一次课程页 URL |
| `list_prepared_submissions` | 只读 | **阶段一收尾用**：清点 `state/submit/*.payload.json`、payload 里会带上的附件、`*_完成版.docx`，不联网/不写盘 |

所有 `download_*` 都会返回 `summary`，并且它单独出现在 `content[0]`；
服务的 `instructions` 里写明：**调用后必须把 summary 原文转述给用户**，
`errors` 非空（如反爬跳过的课程）时必须一并提示。

### 提交链路的两条硬语义（2026-10 实测踩过，别再踩）

| 语义 | 说明 |
| --- | --- |
| `answerwqbid` 必须带 | 站点自己的 `ready2Submit()` 会写 `#questionIds = "题目id,"`；漏掉站点直接回 `{"status": false, "msg": "无效的参数：code-1！"}`。提交和保存都要带 |
| `answers` 的键只能是**题目 id** | `prepare_payload(answers=…)` 会写 `fields["answer" + 键]`。把表单字段字典当 answers 传，会生成 `answercourseId` 之类的垃圾键，而真正的 `answer<qid>` 不被附件正文覆盖 → **提交成功但正文为空**（站点回 `success`，最难查）。覆盖表单字段请用 `extra_fields` |

> 诊断方法（A/B 对照、字段数异常、提交后回读核对）见 `docs/findings-submit-payload.md`。

## 运行时数据在哪（项目目录保持纯净）

| 内容 | 位置 |
| --- | --- |
| 作业附件、**题目内嵌附件**、完成版 DOCX | `<作业根>/<账号uid>/<作业名>/`（默认作业根 `D:/学习通作业`） |
| cookie、菜单缓存、材料缓存、提交 payload、作答页地址表 | `<作业根>/.xuexitong_mcp/` |
| 项目自身 | **只有代码 / 文档 / 测试** → 可直接打包分享 |

解析逻辑在 `xuexitong/paths.py`：`XUEXITONG_STATE_DIR` 可单独指定 state 根；
`XUEXITONG_ROOT` / 工具参数 `root` 改作业根；凭据文件 `<state>/.env`（查找顺序见 USAGE.md 第 2 节）。
`XUEXITONG_ROOT` 在**进程启动时**解析一次并成为默认作业根（2026-10 修复：以前只影响 state 目录，
材料仍落 `D:/学习通作业`，与文档不符）。

## 给别人用 / 换台机器部署

现在的项目目录**只包含代码、文档、测试**，运行时数据（cookie / 缓存 / payload）都在
`<作业根>/.xuexitong_mcp/`，凭据在 `<state>/.env` —— 所以**可以整包压缩发出去**。
打包前只需确认一件事（按优先级）：

| 检查 | 说明 |
| --- | --- |
| 项目里没有 `.env` | 凭据应放在 `<作业根>/.xuexitong_mcp/.env`；项目里只保留 `.env.example` |
| 没有 `state/`（旧版本遗留） | 旧版曾把 state 写在项目里，若见到就删掉（里面是 cookie 与个人数据） |

对方拿到后要做的事（详见 [USAGE.md](USAGE.md)）：装依赖 → **配自己的凭据**（这步不做，服务能
启动但工具一律报缺凭据）→ 注册 MCP。

<details><summary>历史说明（旧版本为什么不能直接发）</summary>

| 必须做 | 为什么 |
| --- | --- |
| **删掉 `.env`** | 里面有账号密码 / cookie（已在 `.gitignore`，但手工压缩会带上）。对方要填自己的：`XUEXITONG_USERNAME` / `XUEXITONG_PASSWORD`（或 `XUEXITONG_COOKIE`） |
| **删掉 `state/`** | 登录态 cookie、菜单缓存（含手机号）、作业材料、`pages.json`、彩排产物、截图 —— 全是你的个人数据 |
| 删掉 `__pycache__/`、`scripts/js_ref/node_modules/` | 无用体积 |
| 对方装依赖 | `pip install -r requirements.txt`（必需 bs4 / python-docx） |
| 对方注册 MCP | `codex mcp add xuexitong -- <对方的 python.exe> <对方路径>\server.py`（**别抄文档里的 `D:\Python\python.exe`**） |

对方机器上**可选**但影响体验的：

- 解压 `.rar/.7z`：需要 WinRAR 或 7-Zip 在 PATH / 默认安装位置（`probe_homework_files` 会诊断并如实报缺什么）。
- `.doc → .docx`：需要 Office（Word COM）或 LibreOffice；都没有就手动另存为 `.docx`。
- `scripts/harvest_submit_pages.py` 用的是 `bsk`（BrowserSkill CLI），**只有兜底收割才需要**，服务本体不依赖。

</details>

对方**不需要**做的事：不用给课程/作业任何配置，也不用给新版课程页 URL —— 登录后课程、作业、
作答页 `enc` 全是自动发现的（见 `docs/findings-enc-auto.md`）。

## 注册（Codex）

```powershell
codex mcp add xuexitong -- D:\Python\python.exe D:\myproject\McpAndSkill\mcp\xuexitong_mcp\server.py
```

- 必须用**绝对路径**的解释器：`D:\Python\python.exe` 装有 httpx/cryptography/beautifulsoup4；
  PATH 上的 miniconda python **缺 bs4**，用它会在启动时报 ImportError。
- **凭据是前置条件，而且不随项目走**：项目目录里只有 `.env.example`，真正生效的三处是
  **进程环境变量 → 项目根 `.env` → `<作业根>/.xuexitong_mcp/.env`**。所以**换台机器 / 重新导入
  项目后必须重新配一遍**：不配也能启动，但每个工具都报缺凭据。
- 两种配法见上一节「配置」：`--env` 注入（明文进 `~/.codex/config.toml`）
  或 `<作业根>/.xuexitong_mcp/.env`（不进 config.toml，适合不想把密码写进配置文件的人）。
  用 `whoami` 可确认当前读到的是哪一档：它只报「已配置/未配置」，不回声任何凭据内容。
- 服务不依赖当前工作目录（所有路径都由 `__file__` 推导成绝对路径），可从任意目录启动。

## 用 JSON-RPC 手工冒烟（不经客户端）

```powershell
$lines = @(
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","clientInfo":{"name":"t"}}}'
 '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
 '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"whoami","arguments":{}}}'
)
Set-Content -Path $env:TEMP\rpc.jsonl -Value $lines -Encoding utf8
Get-Content $env:TEMP\rpc.jsonl | & D:\Python\python.exe D:\myproject\McpAndSkill\mcp\xuexitong_mcp\server.py
```

注意：PowerShell 不支持 `<` 重定向，必须用 `Get-Content | & python ...` 管道喂 stdin；
日志一律走 stderr，stdout 是协议通道。

## 落盘路径（按账号隔离）

```
<root>/<账号 uid>/<作业名>/<附件名>
D:\学习通作业\100000002\影像增强示例（2）\实验三 影像增强示例（2）.rar
```

- 账号目录名优先取 **uid**（不含手机号等隐私），取不到时退化为配置的账号名，都取不到则不加这一级。
- `resolved_root()` 是**幂等**的：调用方若已传 `root=D:/学习通作业/100000002`，不会叠成两层。
- 原因：两个账号（同校）课程高度重叠，同名作业若写同一目录会互相覆盖或误跳过。

## 作业分析：MCP 只取材料，分析由当前窗口完成

**架构约定**：本 MCP 只做「查询课程/作业 → 下载附件 → **解压压缩包** → 把材料交给窗口读写」。
**它不做语义分析、不调用任何 LLM、不预设待填字段。**

```
① get_homework_materials(work_id, course_id?)   # 取材料
     · 列出作业目录全部文件
     · 解压 rar/zip/7z/tar（本机有 UnRAR/bsdtar；缺工具会明确报错）
     · .doc 自动转 .docx（用 Word COM；没有 Word/LibreOffice 时给出指引）
     · 逐份文档返回全文（带行号）、空白位置**提示**、识别到的个人信息字段
     · 没有附件 / 没有可读文档 → status=NEED_MORE_INFO（必须转述给用户）
② 当前窗口自己读全部材料、自己判断、自己作答
③ apply_homework_edits(work_id, edits, user_fields?)   # 写回
     edits = [{"anchor": "模板原文片段", "content": "内容", "mode": "after|replace|append"}]
     → 生成 <作业名>/<模板名>_完成版.docx
```

### 两条硬规则（代码强制，不靠模型自觉）

| 规则 | 实现 |
| --- | --- |
| **个人信息不得编造** | 目标段落含 姓名/学号/班级/学院/日期… 时，模型给的 content **一律拒绝**（记入 `refused_personal`），只接受调用方传入的 `user_fields`；未提供的字段写成 `＿＿＿（待填）` 并在 `summary` 里要求用户自己补 |
| **信息不足不得编造** | content 写成 `{"INSUFFICIENT": true, "reason": "..."}` → 文档里标 `【待补：原因】`，并在 summary 里列出 |

三种缺失情形都会明确告知用户、绝不跳过：
- **没有附件** → `NEED_MORE_INFO`（"该作业没有任何附件，也不能据此完成任务"）
- **附件里没有可读文档**（只有图片等）→ `NEED_MORE_INFO`
- **材料不足以完成任务**（如要求写"某某分析报告"但被分析对象不在附件里）→ 由窗口在作答时用 `INSUFFICIENT` 声明并告知用户

### 真实案例（影像增强示例（2））

作业目录里有两份东西：`.rar`（**原始附件**：3 张实验图像 + 原始模板 `.doc`）和
`_填写版.docx`（**已提交的答案**）。工具链的表现：

```
probe_homework_files → 2 个文件；rar 内含 1.jpg / headCTNoise.bmp / lenaNoise.png / 实验三….doc
                       工具: unrar=True, bsdtar=True, word_com=True
get_homework_materials → 解压 4 个文件 → .doc 自动转 .docx
                       可读文档 2 份；建议模板 = 转出来的原始模板（空白提示 4 处）
                       vs 已提交的填写版（1 处）→ 自动选中原始模板
                       个人信息字段: 姓名、学号、班级
apply_homework_edits   → 写入 3 处（问答/代码/小结），越权填姓名学号班级被拒 →
                       文档里留 ＿＿＿（待填），summary 要求用户自己补
```

### 已知边界

- 只支持 `.docx` 写入；`.doc` 依赖本机 Word/LibreOffice 转换（转换器可用性用 `probe_homework_files` 查）
- 压缩包依赖外部工具：`.rar` 需 WinRAR/7-Zip，`.7z` 需 7-Zip；缺失时**明确报错**而不是当作"包里没东西"
- 图像内容不做 OCR/识别：只把文件名与清单交给窗口，由窗口依据实验要求描述与处理
- 解压产物落在 `<作业目录>/_extracted/<包名>/`（缓存，可随时删）

## 提交链路（只实现到"准备提交"）

**服务里没有真提交工具** —— 绝不自作主张 POST 提交表单。已实现三步：

```
① open_homework_submit_page(work_id, course_id?, page_url?, enc?)      只读
     → 打开新版作答页(mooc2 dowork)，回显 uploadEnc/uploadTimeStamp/userId、
       题目 id、提交次数(addTimes/limit)、#submitForm 的 action
② upload_homework_attachment(work_id, file, dry_run?)                   只上传
     POST https://mooc1.chaoxing.com/upload-ans/ueditorupload/attachment
          ?uploadtype=work&source=1&enc2=<#uploadEnc>&t=<#uploadTimeStamp>&uid=<#userId>
     multipart: upfile
     → {"state":"SUCCESS","objectid":…,"original":…,"fileType":…,"size":…}
     · **只上传**：不挂作答、不保存、不提交（服务端同内容会去重）
③ prepare_homework_submission(work_id, files?, answers?, answer_qid?, save_only?)
     → 把 insert_html 拼进 answer<qid>，产出 **POST URL + 全部表单字段** 供审阅
     · pyFlag=3（提交）/ 1（保存）—— 这是保存与提交的唯一判别字段
     · 本工具**不发送**这个请求
```

关键实测结论：

| 项 | 结论 |
| --- | --- |
| 提交端点 | `POST /mooc-ans/work/addStudentWorkNewWeb?_classId&courseid&token&totalQuestionNum&wMicroNodeId=` |
| 答案字段 | `answer<questionId>`（字符串 HTML，附件就是里面的 `editor-iframe`） |
| 保存 vs 提交 | 同一端点、都带 `saveStatus=1`；**判别字段是 `#pyFlag`：保存=1、提交=3** |
| 提交前置（手动路径） | `submitValidate(3)` → `GET /work/validate` → `ready2Submit()`（字数校验 + `UE.getEditor().sync()`）→ 确认框 → `submitCheckTimes` → `submitWork` → `confirmSubmitWork` |
| 新版 `enc` | 由新版页面**自己下发**；用老版作业列表页的 `enc` 会 **403** —— 首次需用浏览器打开作答页把 `page_url` 传进来（之后缓存复用） |
| `standardEnc` | 可由 `GET /mooc-ans/work/isExpire` 取（`status==0`） |
| 未保存就离开 | 页面有 `beforeunload` 守卫（`isContentChanged`）。**纯 HTTP 链路完全不受影响**；浏览器自动化里离开前先 `window.isContentChanged=false`，否则会弹**原生对话框**（脚本控制不到，我的 `session stop` 因此超时过一次） |

## 真提交（`submit_homework`）—— 硬门禁

```
submit_homework(work_id, confirm=false, files?, answers?, answer_qid?, ...)
   · confirm 缺省/为 false → **只返回将提交的 payload，零请求**（可先给用户审阅）
   · confirm=true          → 才真正 POST 提交表单（不可逆）
   · 提交前的本地守卫（对应站点 submitCheckTimes）：
        addTimes > limitWorkSubmitTimes → 站点会先要图形验证码，纯 HTTP 过不了 → 直接拒绝并让人工提交
```

与手动路径的对应关系：

| 手动 | 本服务 |
| --- | --- |
| 点「提交」按钮（`submitValidate(3)`） | `prepare_homework_submission` / `submit_homework(confirm=false)` 组装 payload（`pyFlag=3`） |
| 「确认提交？」弹窗点确定 | `submit_homework(confirm=true)` → `POST $submitForm.action` |
| `submitCheckTimes`（次数校验→可能需要验证码） | `check_submit_guard()`：超限即拒绝，不会硬闯 |
| `UE.getEditor().sync()` 把编辑器内容写回 `#answer<qid>` | 由我们自己把答案 HTML 拼好放进该字段 |
| 提交成功后跳转/关窗 | 返回服务端 JSON（`status/msg`）并写入 summary |

**已知差异**：手动路径会先 `GET /work/validate`（账号异常/验证码判定），本实现**没有复刻**这一步 —— 因为它的 host 需要再确认；跳过的影响有限：账号异常时提交 POST 会直接返回失败信息（会被 `SubmitError` 抛出并转述）。

**测试覆盖**：`tests/test_submit.py::SubmitGateTest` 用 MockTransport 断言「没有 confirm 时零请求」「confirm 后按 pyFlag=3 提交」「次数超限被守卫拦住且零请求」「服务端拒绝时抛出 msg」。

## 两段式工作流（阶段一 / 阶段二）

服务层面有两条纪律：

1. **阶段一做干净就停**，提交永远由**主 agent** 单独执行（子 agent 禁止调用提交工具）。
2. **完成与提交永远不能一步做完** —— 没有"用户说了就直接交"的例外：必须先把成品原文展示给用户、
   用户审核后再提交。技术闸门：`submit_homework` / `batch_submit` 在 `confirm=true` 之外
   **还必须带 `review_token`**（由 `prepare_homework_submission` 产出，是当前 payload 的指纹）；
   缺 token / token 过期 → **零请求**被拒。

| | 阶段一：拉取并做作业 | 阶段二：用户的下一条命令 |
| --- | --- | --- |
| 触发 | 「拉取并做作业」「把待做作业做了」 | 「提交」／「重修、再改」 |
| 谁做 | 主 agent 派子 agent（每作业一个，最多并行） | **主 agent**（提交）；主 agent 自己改文件（重修） |
| 用 MCP 吗 | 用（③~⑧，终点 `prepare_homework_submission`） | 提交用（`submit_homework`/`batch_submit`）；**重修不用** |
| 终点 | payload 落盘 + `list_prepared_submissions` 摆成品 | 作业交出去 / 成品改好 |
| 之后 | ✋ **停下**，等用户说话 | 逐个如实汇报 |

细节写在两份文档里：

- `docs/main-agent-orchestration.md` —— **主 agent** 的编排手册（何时派子 agent、何时停、怎么提交、重修怎么接）
- `docs/homework-playbook.md` —— **子 agent** 的操作手册（只做阶段一，五条铁律 + 步骤表 + 输出契约）

## 批量 / 子 agent 架构

多份待做作业时，**③~⑧ 可以分发子 agent**（每个子 agent 一份作业，独立上下文 → 天然解决"全文塞爆上下文"）。
但要遵守四条：

| 约束 | 做法 |
| --- | --- |
| 统一操作手册 | 子 agent 都读 `docs/homework-playbook.md`（含五条铁律 + 步骤表 + **输出契约 JSON**） |
| 浏览器步骤**不能并行** | 新版作答页的 `enc` 必须过浏览器拿；用 `scripts/harvest_submit_pages.py` **串行**收割到 `state/submit/pages.json`，子 agent 只做无头部分 |
| **提交留闸** | 子 agent 只做到 `prepare_homework_submission`（payload 落盘）；真提交由主 agent 带 `confirm=true` 执行 |
| 认清串行现实 | MCP 主循环是单进程逐行处理（`for raw_line in sys.stdin.buffer`）→ 并行只买到上下文隔离，买不到速度；跨课程扫描有反爬节流 |

批量工具（4 个，全部"逐项 catch、单项失败不影响其它"，且**只回汇总不回全文**）：

```
batch_prepare_materials(work_ids, course_id?, refresh?)      # 逐个落盘 state/materials/<id>.json
batch_apply_edits(items=[{work_id, edits, user_fields?}])    # 逐个回填 DOCX
batch_prepare_submissions(work_ids, files_by_work?, …)       # 逐个组装并落盘 <id>.payload.json（不发提交）
batch_submit(work_ids, confirm=false)                        # confirm=false → 零请求；true 才逐个真提交
```

推荐编排（**两段式，中间必须停**）：

```
【阶段一：拉取并做作业】
主 agent: ① list_homeworks(status=unfinished) → ② download_unfinished_attachments
          → ⑥ harvest_submit_pages.py（串行收割 page_url）
          → 派 N 个子 agent（每个一个 work_id，附 playbook 路径 + 输出契约）
子 agent: get_homework_materials → 自己分析 → apply_homework_edits
          → upload_homework_attachment → prepare_homework_submission → 返回契约 JSON
          ⛔ 子 agent 禁止调用 submit_homework / batch_submit（连 confirm=false 都不许）
主 agent: list_prepared_submissions → 把成品清单原文摆给用户 → ✋ 停下等命令

【阶段二：用户的下一条命令】
用户说「重修」→ 主 agent 自己灵活改文件（这一段不用 MCP）；
                改完要提交则重新 prepare（文件变了，旧 objectid 失效）
用户说「提交」→ 主 agent 串行 submit_homework(work_id, confirm=true)，一次一个、逐个转述 summary
```

`scripts/harvest_submit_pages.py` 两种模式（都不需要借用用户标签页，因此不会弹借用确认）：
`--from-tabs`（扫已打开的作答页标签页）／`--url <入口链接>`（用任务标签页打开读最终 URL）；
收尾一律先 `window.isContentChanged=false`，否则 `beforeunload` 原生弹窗会把关闭卡住（实测踩过）。

**现实前提**：① 真·待做作业的作答页路径尚未验证（已打通的是"已交作业"）；
② ~~新版 `enc` 只能浏览器收割~~ → **已解决且全自动**：课程菜单 `enc/openc` →
   `/mooc-ans/mycourse/studentcourse?…&v=2&fromMiddle=1`（页面里带 `#workEnc`）→ `/mooc2/work/list`
   （每个作业自带 `enc`+`answerId`）。**零用户输入、不用浏览器**（见 `docs/findings-enc-auto.md`）；
   `register_study_page` 仅作兜底；
③ 已截止的作业站点只给 `work/view` 查看页，没有作答页 → 这类作业只能停在"取材料/写回"，
上传与提交做不了，脚本会如实上报。
