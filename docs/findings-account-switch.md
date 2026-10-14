# 实测记录：切换账号时的缓存/会话污染（2026-10-13）

场景：用户在 `.env` 里把账号从 A（`13800000000`，uid 100000001）换成 B（`13900000000`，uid 100000002）。
两个账号同属一个学校（fid=1880），**课程高度重叠** —— 这是最危险的场景。

## 1. 抓到的真 bug：旧会话被静默沿用

修复前实测：

| 指标 | 结果 |
| --- | --- |
| `.env` 配置账号 | `13900000000`（B） |
| `whoami` 返回 | **uid=100000001**（A！） |
| `list_courses` 返回 | **36 门**（A 的课程） |

根因：`ensure_session` 第一步读 `state/cookies.json`；A 的会话在服务端**仍然有效** →
`probe()` 只回答"我现在登录着吗"，不回答"我是谁" → 通过 → 直接复用。
`.env` 里的新账号被完全忽略，**且不报任何错**。

### 修复：会话与缓存都绑定账号

| 文件 | 改动 |
| --- | --- |
| `xuexitong/session.py` | 新增 `StateFile`（cookies + `account` + saved_at）；`save_cookies(..., account=)` 落盘账号；`load_state()` 读取 |
| `xuexitong/login.py` | `ensure_session` 读状态文件后校验归属：`state.account != expected` → 忽略并重新登录（无 account 标记的旧文件也忽略）；`_uid_of_blob()` 支持只用 cookie 串时按 blob 的 `_uid` 识别 |
| `xuexitong/store.py`（新增） | 菜单缓存升级为 `{account, saved_at, entries}`，账号不匹配或有旧扁平格式 → 整份作废重建 |
| `xuexitong/tasks.py` / `scripts/download_attachments.py` | 统一走 `store` 的账号级缓存 |

其中 `expected = 配置的 username，为空时取 cookie 串里的 _uid`。

### 修复后实测（同一台机器、同一个 `.env`）

| 指标 | 修复前 | 修复后 |
| --- | --- | --- |
| uid | 100000001（A） | **100000002（B）** |
| 课程数 | 36 | **33** |
| cookies.json 的 account | （无） | **13900000000** |
| menu_cache.json | 旧扁平格式 36 条 | **新格式、account=13900000000、按需重建** |
| 日志 | 无提示 | `cookie 属于账号 未知，当前配置 13900000000，已忽略` / `菜单缓存是旧格式（无账号标记），已作废重建` |

## 2. 顺手抓到第二个问题：200 OK 的风控页被当成"0 条作业"

修复过程中出现过一次：同一门课、同一会话，`list_homeworks` 能解析出 5 条，
紧接着 `download_homework_attachments` 却报「扫描 1 门课 / 0 条作业」，
随后重跑又恢复正常 —— 典型的风控/验证中转页以 **HTTP 200** 返回。

原来的判断只看 `202` 和 URL 里的 `antispiderShowVerify`，于是这种页面被解析成空列表，
对外表现成「这门课没有作业」（假阴性）。

### 修复：区分「真的没有作业」与「拿到的不是作业列表页」

- `courses._guard`：额外检查 **响应体前 4KB** 是否含 `antispiderShowVerify` → 抛 `AntispiderError`
- `homework.fetch_homework_list`：解析出 0 条时检查页面是否含 `ulDiv` 容器
  - 含 `ulDiv`（可能是空的）→ 空列表 = **真的没有作业**
  - 不含 `ulDiv` → 抛 `FetchError`，附 HTTP 状态码、字节数、最终 URL、正文开头 160 字

已用 `httpx.MockTransport` 钉了 3 条用例：验证页报错 / 真空列表不报错 / 200+风控标记报 `AntispiderError`。

## 3. 遗留风险（未改，需要产品决策）

**下载根目录没有按账号隔离**：`D:/学习通作业/<作业名>/` 是按用户要求设计的，
但两个账号若存在同名作业，A 的文件会被 B 覆盖（或因为大小相同被跳过）。
可选方案：`root` 传带账号的子目录（如 `D:/学习通作业/13900000000`），
或加一个 `XUEXITONG_DOWNLOAD_ROOT` 环境变量统一指定。

## 4. 现在的行为契约

1. 换账号 → 旧 cookie/旧菜单缓存**自动失效**，日志明确写出原因
2. 会话能复用就复用（同账号零登录）；**不能确认归属就重新登录**
3. 抓到的页面不符合预期结构 → **报错**，绝不静默返回空
4. 所有失败进 `errors` 数组，`summary` 里体现，agent 必须转述

## 5. 落盘路径隔离（已实施）

原「遗留风险」已按方案 1 落地：落盘统一为 `<root>/<账号 uid>/<作业名>/<附件名>`。

- 账号目录名优先 **uid**（避免手机号进文件路径），退化到配置账号名，都取不到则不加这一级
- `resolved_root()` 幂等：调用方已传 `root=D:/学习通作业/<uid>` 时不会叠两层
- 实测（账号 B）：`D:\学习通作业\100000002\影像增强示例（2）\` 两个附件全部下全
  （`.rar` 走直链、`.docx` 走 btnDown 中转，都正常）
- 顺带修掉两个真问题：
  1. `store.account_dir_name` 账号未知时曾返回 `"未命名"`（`safe_name("")` 的回落），
     会造出 `D:/学习通作业/未命名/` —— 已改为返回空串
  2. `config.load_dotenv` 把「显式设为空的环境变量」当成未设置，导致 `.env` 覆盖它 ——
     已改为 `key not in os.environ`，与"环境变量优先"的承诺一致
- **历史遗留**：改动之前下载的文件仍在旧路径（`D:\学习通作业\<作业名>\`），
  与新的账号目录并存；需要人工决定迁移或清理
