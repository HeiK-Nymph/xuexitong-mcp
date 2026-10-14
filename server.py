r"""xuexitong_mcp —— 学习通作业助手 MCP 服务（stdio，按行分隔的 JSON-RPC 2.0）。

能力：登录会话维护（AES + cookie 复用）→ 课程/作业列表 → 附件下载。
正式功能：下载**未完成（待做）**作业的附件，落盘 <root>/<作业名>/<附件名>（默认 root = D:/学习通作业）。

只依赖标准库 + httpx + cryptography + beautifulsoup4。
凭据只从环境变量读取（MCP env 注入），兜底顺序：项目根 .env → <作业根>/.xuexitong_mcp/.env；
**绝不作为工具参数传入**。

手动注册（Codex）：
    codex mcp add xuexitong -- python D:\Python\python.exe D:\myproject\McpAndSkill\mcp\xuexitong_mcp\server.py

推荐注册时用 --env 注入；不设则回落到 .env 兜底（项目根 .env → <作业根>/.xuexitong_mcp/.env）：
    XUEXITONG_USERNAME / XUEXITONG_PASSWORD / XUEXITONG_COOKIE / XUEXITONG_STATE_PATH / XUEXITONG_INSECURE

工具：
    whoami                              会话状态（只读）
    list_courses                        课程列表（只读）
    list_homeworks                      作业列表，可筛 any/unfinished/completed（只读）
    download_unfinished_attachments     【正式功能】下载全部未完成(待做)作业的附件（写盘）
    download_homework_attachments       下载指定 work_id 作业的附件（写盘）
    download_random_completed_attachments  测试用：随机 N 个已完成作业（写盘）
    list_prepared_submissions           阶段一产物清点（只读；主 agent 收尾时摆给用户看）
    register_study_page                 注册新版课程页 URL（每门课一次）→ 作业 enc 自动发现
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from xuexitong import tasks  # noqa: E402
from xuexitong.attachments import DEFAULT_ROOT  # noqa: E402
from xuexitong.courses import AntispiderError, FetchError, SessionExpired  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "xuexitong", "version": "0.1.0"}

SERVER_INSTRUCTIONS = (
    "本服务操作学习通（超星）：维护登录会话、列课程与作业、下载作业附件。"
    "凭据由服务端从环境变量读取（兜底 <作业根>/.xuexitong_mcp/.env；不随项目分发，换机器要重配），"
    "工具参数里不要也不需要传账号密码。"
    "正式功能是 download_unfinished_attachments —— 下载全部**未完成(待做)**作业的附件，"
    "落盘根目录默认 D:/学习通作业，每个作业一个同名子文件夹。"
    "规则：调用任何 download_* 之后，必须把返回里 summary 字段的原文转述给用户"
    "（包含扫描了多少门课/多少条作业、下载了几个文件、多少 KB、失败几个、落到哪个目录）；"
    "summary 里出现『未找到未完成作业』时也要如实转述，并说明扫描了多少门课，"
    "不要把它说成『下载成功』。返回里 errors 非空时必须一并提示用户（例如触发反爬被跳过的课程）。"
    "【工作流分两段，不许越段】"
    "① 阶段一＝拉取并做作业：取材料 → 分析作答 → 写回 DOCX → 上传附件 → 准备提交 payload，"
    "终点是 payload 落盘（list_prepared_submissions 里的 PHASE1_DONE）。**到此为止**："
    "用 list_prepared_submissions 把成品清单（完成版 docx 路径、将随 payload 一起交的附件、"
    "需要用户自己填的个人信息、待补处）原文摆给用户看，然后停下等他的下一条命令 —— 不要自动提交、"
    "不要顺手多做一步。② 阶段二＝用户的下一条命令：说『重修 / 再改 / 改一下』→ 那是灵活任务，"
    "**不要调用本服务的任何工具**，由当前窗口自己读文件、自己改；说『提交』→ 由**主 agent**"
    "用 submit_homework / batch_submit（confirm=true）逐个真提交，每提交一个都把 summary 原文转述给用户。"
    "【不可协商】**完成和提交永远不能一步做完**：即使用户说『做完就直接提交』『不用给我看』，"
    "也必须先跑完阶段一、把成品清单（review_checklist + list_prepared_submissions）原文展示给用户，"
    "等用户审核回复之后才提交。submit_homework/batch_submit 需要 prepare 阶段产出的 review_token，"
    "缺 token 一律零请求拒绝 —— 这就是这条纪律的技术闸门。"
    "**子 agent 只做阶段一**：绝对不允许调用 submit_homework / batch_submit（连 confirm=false 也不要调），"
    "子 agent 的终点是 prepare_homework_submission。提交一律由主 agent 串行执行，以便用户管理。"
    "本服务只做四件事：查询课程/作业、下载附件、**解压压缩包**、把材料交给窗口读写；"
    "**不调用任何 LLM、不做语义分析、不预设待填字段**。"
    "分析作业的流程：先 get_homework_materials —— 它会把该作业所有文件（含压缩包内）的清单和逐份文档全文交给你，"
    "你自己阅读、自己判断、自己作答；然后 apply_homework_edits 用 anchor+content 把内容写回模板。"
    "三条硬规矩：① 个人信息（姓名/学号/班级/日期…）**禁止编造**，要么让用户用 user_fields 提供，"
    "要么留空并在回复里明确告诉用户哪几处需要他自己填；② 材料不足以完成任务时（例如要求写『某某分析报告』"
    "但被分析对象不在附件里）**必须明确告知用户缺什么**，禁止猜测、禁止跳过、禁止硬填；"
    "③ get_homework_materials 返回 status=NEED_MORE_INFO、或任何返回里 warnings 非空时，都必须把 summary/reasons 原文转述给用户。"
    "apply_homework_edits 之后必须把 summary 原文转述（含输出路径、填充处数、需用户手填的字段、待补处）。"
    "本机可用的解压/转换工具可通过 probe_homework_files 查询（缺工具时要如实告诉用户装 WinRAR/7-Zip 或 Word）。"
    "提交链路：open_homework_submit_page → upload_homework_attachment → prepare_homework_submission"
    "（产出 POST URL + 全部字段并落盘 state/submit/<work_id>.payload.json，供用户审阅）；"
    "submit_homework **不可逆**，只有 confirm=true 才会真的 POST，未给 confirm 时只回显 payload、零请求。"
    "提交前会做本地守卫（提交次数超限=站点要图形验证码 → 拒绝，让你手动提交）。调用后必须把 summary 原文转述给用户。"
    "注意三点：① 新版作答页的 enc **由服务全自动发现**（课程菜单 enc/openc → 体验新版页 → /mooc2/work/list，"
    "纯 HTTP、不用浏览器、不用用户给 URL），发现结果写进 state/submit/pages.json；"
    "极少数课程菜单缺 openc 时，才用 register_study_page 给一次新版课程页 URL；"
    "② upload_homework_attachment 只是上传，不挂载/不保存/不提交；"
    "③ 保存与提交的判别字段是 pyFlag（保存=1，提交=3）。"
    "上述三个工具调用后都必须把 summary 原文转述给用户。"
    "download_random_completed_attachments 是测试用工具（账号没有待做作业时验证下载链路），"
    "调用时必须向用户说明这不是正式功能。list_homeworks 返回的 unrecognized_status_items "
    "是状态文案没被规则表识别的作业（会被当作非待做），需要提示用户核查。"
)

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "whoami",
        "description": "只读：检查当前登录会话是否有效（返回 uid、凭据配置状态、cookie 文件路径），不发起业务请求。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_courses",
        "description": "只读：列出当前账号的全部课程（course_id / class_id / cpi / 名称）。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_homeworks",
        "description": "只读：列出作业。status=unfinished 只列未完成(待做)，completed 只列已完成，"
                       "any 列全部。同时返回原始状态文案分布与『状态未识别』的作业名（供核查）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["any", "unfinished", "completed"],
                           "description": "筛选：any(默认) / unfinished(待做) / completed(已完成)"},
                "course_id": {"type": "string", "description": "可选：只查这门课程"},
                "max_courses": {"type": "integer", "description": "可选：只扫描前 N 门课（默认 0=全部）"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "download_unfinished_attachments",
        "description": "【正式功能】下载全部未完成(待做)作业的附件，落盘 <root>/<账号>/<作业名>/<附件名>"
                       "（账号用 uid，避免两个账号同名作业互相覆盖），默认 root=D:/学习通作业。调用后必须把返回的 summary 原文转述给用户。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "落盘根目录（服务端会自动在其下加账号子目录），默认 D:/学习通作业"},
                "dry_run": {"type": "boolean", "description": "只列出会下载哪些作业，不真正下载（默认 false）"},
                "course_ids": {"type": "array", "items": {"type": "string"},
                               "description": "可选：只在这些课程里找"},
                "max_courses": {"type": "integer", "description": "可选：只扫描前 N 门课（默认 0=全部）"},
                "overwrite": {"type": "boolean", "description": "同名同大小已存在时是否覆盖（默认 false=跳过）"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "download_homework_attachments",
        "description": "下载指定 work_id 作业的附件（可补下已完成/待批阅作业），落盘 <root>/<账号>/<作业名>/。"
                       "调用后必须转述 summary。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "work_id": {"type": "string", "description": "作业 ID（从 list_homeworks 获得）"},
                "course_id": {"type": "string", "description": "可选：限定课程，可减少扫描"},
                "root": {"type": "string", "description": "落盘根目录，默认 D:/学习通作业"},
                "overwrite": {"type": "boolean", "description": "默认 false=同名同大小跳过"},
            },
            "required": ["work_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_homework_materials",
        "description": "【取材料】把一个作业的**所有相关文件**交给当前窗口：目录清单 + 压缩包解压 + "
                       ".doc 自动转 .docx + 逐份文档的全文（带行号）。本服务**不做分析、不调用任何 LLM、"
                       "不预设待填字段** —— 分析由你自己读完材料后完成。"
                       "若作业没有附件、或附件里没有可读文档，会返回 status=NEED_MORE_INFO，"
                       "此时必须把 reasons/summary 原文转述给用户，禁止自行编造内容。",
        "inputSchema": {
            "type": "object",
            "properties": {
            "root": {"type": "string", "description": "落盘根目录（默认 D:/学习通作业；彩排/沙箱可指向别处）"},
                "work_id": {"type": "string", "description": "作业 ID（从 list_homeworks 获得）"},
                "course_id": {"type": "string", "description": "可选：限定课程可减少扫描"},
                "refresh": {"type": "boolean", "description": "true 时重新扫目录（默认复用缓存）"},
                "max_chars_per_doc": {"type": "integer", "description": "单份文档返回的字符上限，默认 30000"},
            },
            "required": ["work_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "probe_homework_files",
        "description": "只读诊断：列出该作业目录里的文件、压缩包内的条目、以及本机可用的解压/转换工具。"
                       "当用户问『为什么读不到附件』时用。",
        "inputSchema": {
            "type": "object",
            "properties": {
            "root": {"type": "string", "description": "落盘根目录（默认 D:/学习通作业；彩排/沙箱可指向别处）"},
                "work_id": {"type": "string"},
                "course_id": {"type": "string", "description": "可选：限定课程"},
            },
            "required": ["work_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "apply_homework_edits",
        "description": "【写回】按**锚点**把内容写进模板，生成 <原模板名>_完成版.docx，落盘 <root>/<账号>/<作业名>/。"
                       "edits: [{\"anchor\": \"模板里的原文片段\", \"content\": \"内容\", "
                       "\"mode\": \"after|replace|append\"}]（anchor 取自 get_homework_materials 返回的文档原文）。"
                       "个人信息段（姓名/学号/班级/日期…）会**拒绝**模型给的值，只接受 user_fields。"
                       "调用后必须把返回的 summary 原文转述给用户（含输出路径、需用户手工填写的字段、待补处）。",
        "inputSchema": {
            "type": "object",
            "properties": {
            "root": {"type": "string", "description": "落盘根目录（默认 D:/学习通作业；彩排/沙箱可指向别处）"},
                "work_id": {"type": "string"},
                "edits": {"type": "string", "description": "JSON 数组字符串（见描述）"},
                "template": {"type": "string", "description": "可选：指定要写入的模板文件（默认用材料里的建议模板）"},
                "user_fields": {"type": "string",
                                "description": '可选：用户本人提供的个人信息 JSON，如 {"姓名":"张三","班级":"计科241"}'},
                "output_dir": {"type": "string", "description": "可选：输出目录（默认与模板同目录）"},
                "overwrite": {"type": "boolean", "description": "默认 false=已存在则加时间戳另存"},
            },
            "required": ["work_id", "edits"],
            "additionalProperties": False,
        },
    },
    {
        "name": "open_homework_submit_page",
        "description": "只读：打开作业的**新版作答页**(mooc2 dowork)，回显上传令牌、题目 id、提交次数、表单 action。"
                       "新版作答页需要 page_url 或 enc —— enc 由新版页面自己下发（用老版作业列表页的 enc 会 HTTP 403），"
                       "通常需要先用浏览器打开该作业作答页、复制地址栏 URL 传入一次（之后会缓存复用）。",
        "inputSchema": {
            "type": "object",
            "properties": {
            "root": {"type": "string", "description": "落盘根目录（默认 D:/学习通作业；彩排/沙箱可指向别处）"},
                "work_id": {"type": "string"},
                "course_id": {"type": "string", "description": "可选：限定课程"},
                "page_url": {"type": "string", "description": "作答页完整 URL（含 answerId/standardEnc/enc）"},
                "enc": {"type": "string", "description": "可选：新版 enc（与 course_id/work_id 一起用）"},
            },
            "required": ["work_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "register_study_page",
        "description": "【兜底，一般用不到】**注册新版学生课程页 URL**（mooc2-ans.chaoxing.com/…/mycourse/stu?…&enc=…&t=…）。"
                       "正常情况下作业的 enc 已经是**全自动**的（课程菜单 enc/openc → 体验新版页 → /mooc2/work/list，"
                       "零用户输入）；只有个别课程菜单里没有「体验新版」入口时才需要人工给一次。"
                       "它会顺便探测每个作业能不能作答，并把可作答的写进 state/submit/pages.json："
                       "已截止的作业站点只给 /work/view 查看页，会被如实标成 view（不能上传/提交）。"
                       "调用后必须把 summary 原文转述给用户。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "stu_url": {"type": "string", "description": "新版课程页地址栏 URL（含 mycourse/stu 与 enc/t）"},
                "course_id": {"type": "string", "description": "可选：不传则从 URL 解析"},
                "class_id": {"type": "string", "description": "可选：不传则从 URL 解析"},
                "cpi": {"type": "string", "description": "可选：不传则从 URL 解析"},
                "probe": {"type": "boolean", "description": "默认 true=顺便探测每个作业是 dowork 还是已截止"},
            },
            "required": ["stu_url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "upload_homework_attachment",
        "description": "把本地文件传到作业附件接口，返回 objectid 与可直接插入富文本的 insert_html。"
                       "**只上传：不挂到作答、不保存、不提交**。dry_run=true 只回显将要发送的请求。"
                       "同内容文件服务端会去重（返回同一 objectid）。调用后必须把 summary 原文转述给用户。",
        "inputSchema": {
            "type": "object",
            "properties": {
            "root": {"type": "string", "description": "落盘根目录（默认 D:/学习通作业；彩排/沙箱可指向别处）"},
                "work_id": {"type": "string"},
                "file": {"type": "string", "description": "本地文件绝对路径"},
                "course_id": {"type": "string", "description": "可选：限定课程"},
                "page_url": {"type": "string", "description": "可选：作答页 URL（首次需要，用于取上传令牌）"},
                "enc": {"type": "string", "description": "可选：新版 enc"},
                "dry_run": {"type": "boolean", "description": "只回显请求，不真上传（默认 false）"},
            },
            "required": ["work_id", "file"],
            "additionalProperties": False,
        },
    },
    {
        "name": "prepare_homework_submission",
        "description": "【只准备，不提交】把附件拼进答案正文，产出将要提交的完整 payload（POST URL + 全部表单字段）供审阅。"
                       "files 里的文件会被**真实上传**（只是上传）；save_only=true 用 pyFlag=1（保存）否则 pyFlag=3（提交）。"
                       "**本服务不会发送提交请求** —— 要不要真提交由用户决定。调用后必须把 summary 原文转述给用户。",
        "inputSchema": {
            "type": "object",
            "properties": {
            "root": {"type": "string", "description": "落盘根目录（默认 D:/学习通作业；彩排/沙箱可指向别处）"},
                "work_id": {"type": "string"},
                "files": {"type": "array", "items": {"type": "string"},
                          "description": "要上传并写进答案的本地文件路径列表"},
                "answers": {"type": "object", "description": "可选：{题目id: 答案HTML} 直接覆盖"},
                "answer_qid": {"type": "string", "description": "可选：多题目时指定写进哪一题"},
                "save_only": {"type": "boolean", "description": "true=按保存(pyFlag=1)组装，默认 false=按提交(pyFlag=3)"},
                "course_id": {"type": "string", "description": "可选：限定课程"},
                "page_url": {"type": "string", "description": "可选：作答页 URL"},
                "enc": {"type": "string", "description": "可选：新版 enc"},
            },
            "required": ["work_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "submit_homework",
        "description": "【不可逆｜**只允许主 agent 调用，子 agent 一律禁止**】真正提交作业"
                       "（对应手动路径的「点提交 → 确认提交」最后一下）。"
                       "**必须显式 confirm=true 才会发送**；否则只返回将要提交的 payload（不发请求）。"
                       "可选 files 会先上传并写进答案正文。提交前本服务会做一次本地守卫："
                       "若提交次数已超上限（站点会要求图形验证码），会直接拒绝并让你手动提交。"
                       "调用后必须把返回的 summary 原文转述给用户。",
        "inputSchema": {
            "type": "object",
            "properties": {
            "root": {"type": "string", "description": "落盘根目录（默认 D:/学习通作业；彩排/沙箱可指向别处）"},
                "work_id": {"type": "string"},
                "confirm": {"type": "boolean", "description": "必须为 true 才真的提交（默认 false=只预览）"},
                "files": {"type": "array", "items": {"type": "string"}, "description": "可选：要一起提交的本地文件"},
                "answers": {"type": "object", "description": "可选：{题目id: 答案HTML}"},
                "answer_qid": {"type": "string", "description": "可选：多题目时指定写进哪一题"},
                "course_id": {"type": "string", "description": "可选：限定课程"},
                "page_url": {"type": "string", "description": "可选：作答页 URL"},
                "enc": {"type": "string", "description": "可选：新版 enc"},
                "review_token": {"type": "string",
                                 "description": "**confirm=true 时必填**：prepare_homework_submission "
                                                "产出的 review_token（当前 payload 的指纹）。它证明你已经把成品"
                                                "展示给用户看过 —— 本服务禁止「做完作业 + 提交」一步完成；"
                                                "缺 token / token 过期一律零请求拒绝。"},
            },
            "required": ["work_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "batch_prepare_materials",
        "description": "批量取材料：逐个作业解压/转格式/读全文并**落盘** state/materials/<work_id>.json，"
                       "只返回清单+路径+警告（**不返回全文**，避免上下文爆炸）。适合配合子 agent："
                       "主 agent 一次准备好，各子 agent 各自读自己那份。",
        "inputSchema": {"type": "object", "properties": {
            "root": {"type": "string", "description": "落盘根目录（默认 D:/学习通作业；彩排/沙箱可指向别处）"},
            "work_ids": {"type": "array", "items": {"type": "string"}, "description": "作业 id 列表"},
            "course_id": {"type": "string"}, "refresh": {"type": "boolean"}},
            "required": ["work_ids"], "additionalProperties": False},
    },
    {
        "name": "batch_apply_edits",
        "description": "批量把各作业的 edits 回填成 DOCX。items=[{work_id, edits, user_fields?, template?}]；"
                       "个人信息仍会被逐个拒绝，需用户手填的字段在各行 needs_user 里。调用后必须转述 summary。",
        "inputSchema": {"type": "object", "properties": {
            "root": {"type": "string", "description": "落盘根目录（默认 D:/学习通作业；彩排/沙箱可指向别处）"},
            "items": {"type": "array", "items": {"type": "object"},
                      "description": "[{work_id, edits, user_fields?, template?}]"}},
            "required": ["items"], "additionalProperties": False},
    },
    {
        "name": "batch_prepare_submissions",
        "description": "批量组装提交 payload 并落盘 state/submit/<work_id>.payload.json（**不发提交请求**），"
                       "files_by_work={work_id: [文件路径]} 会先上传附件。这是批量流程的准备终点（不会发送提交请求）。",
        "inputSchema": {"type": "object", "properties": {
            "root": {"type": "string", "description": "落盘根目录（默认 D:/学习通作业；彩排/沙箱可指向别处）"},
            "work_ids": {"type": "array", "items": {"type": "string"}},
            "files_by_work": {"type": "object"}, "answer_qid": {"type": "string"},
            "save_only": {"type": "boolean", "description": "true=按保存组装(pyFlag=1)"},
            "course_id": {"type": "string"}},
            "required": ["work_ids"], "additionalProperties": False},
    },
    {
        "name": "batch_submit",
        "description": "【不可逆｜**只允许主 agent 调用，子 agent 一律禁止**】批量提交。"
                       "confirm 缺省/为 false 时**零请求**，只回显将提交什么；"
                       "confirm=true 才逐个真提交。建议先 batch_prepare_submissions 审阅后再调用。"
                       "调用后必须把 summary 原文转述给用户。",
        "inputSchema": {"type": "object", "properties": {
            "root": {"type": "string", "description": "落盘根目录（默认 D:/学习通作业；彩排/沙箱可指向别处）"},
            "work_ids": {"type": "array", "items": {"type": "string"}},
            "confirm": {"type": "boolean", "description": "必须 true 才真提交"},
            "review_tokens": {"type": "object",
                              "description": "{work_id: review_token}，逐个来自 prepare_homework_submission；"
                                             "缺 token 的项只报错不提交"},
            "course_id": {"type": "string"}},
            "required": ["work_ids"], "additionalProperties": False},
    },
    {
        "name": "list_prepared_submissions",
        "description": "只读：清点**阶段一**已经做好的产物（payload 落盘路径、payload 里会带上的附件、"
                       "完成版 docx 路径），不联网、不提交、不写盘。主 agent 在阶段一收尾时用它把成品摆给用户看，"
                       "然后等用户下一条命令（『重修』=不用本服务、『提交』=主 agent 真提交）。"
                       "调用后必须把 summary 原文转述给用户。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "落盘根目录（默认 D:/学习通作业）"},
                "work_ids": {"type": "array", "items": {"type": "string"},
                             "description": "可选：只清点这些作业；不传则列出 state/submit 下全部 payload"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "download_random_completed_attachments",
        "description": "【测试用，非正式功能】随机挑 N 个已完成作业下载附件（同样落 <root>/<账号>/），用于验证下载链路"
                       "（例如账号当前没有待做作业）。调用时必须向用户说明这不是正式功能。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "description": "随机取几个，默认 3"},
                "root": {"type": "string", "description": "落盘根目录，默认 D:/学习通作业"},
                "overwrite": {"type": "boolean", "description": "默认 false=同名同大小跳过"},
            },
            "additionalProperties": False,
        },
    },
]


class ToolError(Exception):
    """工具层的可预期错误。"""


def _as_bool(args: Dict[str, Any], key: str, default: bool = False) -> bool:
    return bool(args.get(key, default))


def _as_int(args: Dict[str, Any], key: str, default: int = 0) -> int:
    v = args.get(key, default)
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ToolError(f"{key} 必须是整数，收到 {v!r}") from None


def _as_str_list(args: Dict[str, Any], key: str) -> Optional[List[str]]:
    v = args.get(key)
    if v is None:
        return None
    if not isinstance(v, list) or any(not isinstance(x, (str, int)) for x in v):
        raise ToolError(f"{key} 必须是字符串数组")
    return [str(x) for x in v]


def _as_answers(args: Dict[str, Any], key: str) -> Optional[Dict[str, Any]]:
    """answers 允许传 dict，也允许传 JSON 字符串。

    有些客户端会把对象序列化成字符串传进来，静默丢掉就等于"没传 answers"
    （实测踩到：prepare 结果两次逐字节相同，token 都不变，很难发现）。
    """
    raw = args.get(key)
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise ToolError(f"{key} 传了字符串但不是合法 JSON 对象")
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}
        raise ToolError(f"{key} 必须是 {{题目id: 答案HTML}} 形式的对象")
    return None


def _root(args: Dict[str, Any]) -> str:
    r = args.get("root") or DEFAULT_ROOT
    if not isinstance(r, str):
        raise ToolError("root 必须是字符串路径")
    return r


def _handlers() -> Dict[str, Any]:
    return {
        "whoami": lambda a: tasks.whoami(),
        "list_courses": lambda a: tasks.list_courses(),
        "list_homeworks": lambda a: tasks.list_homeworks(
            course_id=(a.get("course_id") or None),
            status=str(a.get("status") or "any"),
            max_courses=_as_int(a, "max_courses", 0),
        ),
        "download_unfinished_attachments": lambda a: tasks.download_unfinished(
            root=_root(a),
            dry_run=_as_bool(a, "dry_run"),
            course_ids=_as_str_list(a, "course_ids"),
            max_courses=_as_int(a, "max_courses", 0),
            overwrite=_as_bool(a, "overwrite"),
        ),
        "download_homework_attachments": lambda a: tasks.download_specific(
            work_id=str(a.get("work_id") or ""),
            course_id=str(a.get("course_id") or ""),
            root=_root(a),
            overwrite=_as_bool(a, "overwrite"),
        ),
        "get_homework_materials": lambda a: tasks.get_homework_materials(
            work_id=str(a.get("work_id") or ""),
            course_id=str(a.get("course_id") or ""), root=_root(a), refresh=_as_bool(a, "refresh"),
            max_chars_per_doc=_as_int(a, "max_chars_per_doc", 30000),
        ),
        "probe_homework_files": lambda a: tasks.probe_homework_files(
            work_id=str(a.get("work_id") or ""),
            course_id=str(a.get("course_id") or ""),
            root=_root(a),
        ),
        "apply_homework_edits": lambda a: tasks.apply_homework_edits(
            work_id=str(a.get("work_id") or ""),
            edits=a.get("edits"), root=_root(a),
            template=str(a.get("template") or ""),
            user_fields=a.get("user_fields"),
            output_dir=str(a.get("output_dir") or ""),
            overwrite=_as_bool(a, "overwrite"),
        ),
        "register_study_page": lambda a: tasks.register_study_page(
            course_id=str(a.get("course_id") or ""),
            stu_url=str(a.get("stu_url") or ""),
            class_id=str(a.get("class_id") or ""),
            cpi=str(a.get("cpi") or ""),
            probe=_as_bool(a, "probe", True),
        ),
        "open_homework_submit_page": lambda a: tasks.open_homework_submit_page(
            work_id=str(a.get("work_id") or ""),
            course_id=str(a.get("course_id") or ""),
            page_url=str(a.get("page_url") or ""),
            enc=str(a.get("enc") or ""),
        ),
        "upload_homework_attachment": lambda a: tasks.upload_homework_attachment(
            work_id=str(a.get("work_id") or ""),
            file=str(a.get("file") or ""), root=_root(a),
            course_id=str(a.get("course_id") or ""),
            page_url=str(a.get("page_url") or ""),
            enc=str(a.get("enc") or ""),
            dry_run=_as_bool(a, "dry_run"),
        ),
        "prepare_homework_submission": lambda a: tasks.prepare_homework_submission(
            work_id=str(a.get("work_id") or ""),
            files=_as_str_list(a, "files"), root=_root(a),
            answers=_as_answers(a, "answers"),
            answer_qid=str(a.get("answer_qid") or ""),
            save_only=_as_bool(a, "save_only"),
            course_id=str(a.get("course_id") or ""),
            page_url=str(a.get("page_url") or ""),
            enc=str(a.get("enc") or ""),
        ),
        "submit_homework": lambda a: tasks.submit_homework(
            work_id=str(a.get("work_id") or ""),
            confirm=_as_bool(a, "confirm"), root=_root(a),
            review_token=str(a.get("review_token") or ""),
            files=_as_str_list(a, "files"),
            answers=_as_answers(a, "answers"),
            answer_qid=str(a.get("answer_qid") or ""),
            course_id=str(a.get("course_id") or ""),
            page_url=str(a.get("page_url") or ""),
            enc=str(a.get("enc") or ""),
        ),
        "batch_prepare_materials": lambda a: tasks.batch_prepare_materials(
            work_ids=_as_str_list(a, "work_ids") or [], course_id=str(a.get("course_id") or ""),
            refresh=_as_bool(a, "refresh")),
        "batch_apply_edits": lambda a: tasks.batch_apply_edits(
            items=a.get("items") if isinstance(a.get("items"), list) else []),
        "batch_prepare_submissions": lambda a: tasks.batch_prepare_submissions(
            work_ids=_as_str_list(a, "work_ids") or [],
            files_by_work=a.get("files_by_work") if isinstance(a.get("files_by_work"), dict) else None,
            answer_qid=str(a.get("answer_qid") or ""), save_only=_as_bool(a, "save_only"),
            course_id=str(a.get("course_id") or "")),
        "batch_submit": lambda a: tasks.batch_submit(
            work_ids=_as_str_list(a, "work_ids") or [], confirm=_as_bool(a, "confirm"),
            review_tokens=a.get("review_tokens") if isinstance(a.get("review_tokens"), dict) else None,
            course_id=str(a.get("course_id") or "")),
        "list_prepared_submissions": lambda a: tasks.list_prepared_submissions(
            root=_root(a), work_ids=_as_str_list(a, "work_ids")),
        "download_random_completed_attachments": lambda a: tasks.download_random_completed(
            n=_as_int(a, "n", 3),
            root=_root(a),
            overwrite=_as_bool(a, "overwrite"),
        ),
    }


def call_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    handler = _handlers().get(name)
    if handler is None:
        raise ToolError(f"未知工具: {name}")
    return handler(args or {})


def _log(message: str) -> None:
    """日志写 stderr；stdout 是协议通道。"""
    print(f"[xuexitong] {message}", file=sys.stderr, flush=True)


def _text_result(payload: Any, is_error: bool = False) -> Dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    content = []
    if isinstance(payload, dict) and isinstance(payload.get("summary"), str):
        content.append({"type": "text", "text": payload["summary"]})
    content.append({"type": "text", "text": text})
    return {"content": content, "isError": is_error}


def handle_message(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        _log(f"notification: {method}")
        return None

    response: Dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    try:
        params = message.get("params") or {}
        if method == "initialize":
            requested = params.get("protocolVersion")
            protocol = requested if isinstance(requested, str) and requested.count("-") == 2 else PROTOCOL_VERSION
            _log(f"initialize from {params.get('clientInfo')}, protocol={protocol}")
            response["result"] = {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": SERVER_INSTRUCTIONS,
            }
        elif method == "tools/list":
            response["result"] = {"tools": TOOLS}
        elif method == "tools/call":
            name = params.get("name") or ""
            arguments = params.get("arguments") or {}
            try:
                response["result"] = _text_result(call_tool(name, arguments))
            except (ToolError, tasks.TaskError, FetchError, SessionExpired, AntispiderError) as exc:
                _log(f"tool {name} 失败: {exc}")
                response["result"] = _text_result(str(exc), is_error=True)
        elif method == "ping":
            response["result"] = {}
        else:
            response["error"] = {"code": -32601, "message": f"Method not found: {method}"}
    except Exception as exc:  # noqa: BLE001
        _log(f"处理 {method} 异常: {exc}\n{traceback.format_exc()}")
        response["error"] = {"code": -32603, "message": f"内部错误: {exc}"}
    return response


def main() -> None:
    _log(f"started, python={sys.executable}")
    for raw_line in sys.stdin.buffer:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _log(f"忽略非法消息: {exc}")
            continue
        reply = handle_message(message)
        if reply is None:
            continue
        sys.stdout.buffer.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
