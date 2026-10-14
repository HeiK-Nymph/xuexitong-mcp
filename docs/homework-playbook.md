# 作业级操作手册（子 agent 用）—— **只做阶段一**

> 运行时数据在 `<作业根>/.xuexitong_mcp/`（默认 `D:/学习通作业/.xuexitong_mcp/`）：下面提到的 `state/submit/pages.json`、`state/materials/*.json` 等都相对于它。

> 一个子 agent 只负责**一个作业**（一个 `work_id`）。读完本手册再动手。
> 你的产物要与其它子 agent 的产物**按 work_id 隔离**，最后由主 agent 汇总。

## 你在哪一段（先看这个）

整个流程分两段：

| 段 | 谁做 | 内容 | 终点 |
|---|---|---|---|
| **阶段一：拉取并做作业** | 主 agent 派**你**（每个作业一个子 agent） | 取材料 → 分析作答 → 写回 DOCX → 上传附件 → 准备提交 payload | **payload 落盘 + 输出契约里 `status=READY_TO_SUBMIT`** |
| 阶段二：重修 / 提交 | 主 agent（+ 用户） | 用户说『重修』→ 灵活改文件（不用本 MCP）；用户说『提交』→ 主 agent 真提交 | 作业交上去 |

**你是阶段一的人**：做完 ③~⑧ 就**收工上报**。主 agent 会把成品清单摆给用户看、停下等用户下一条命令。
**提交永远不是你的活。**

## 输入（主 agent 会给你）

- `work_id`：必填
- `course_id`：可选（能减少扫描）
- `root`：下载/输出根目录，默认 `D:/学习通作业`
- `pages.json`：作答页地址表（`state/submit/pages.json`），里面可能已有你这个 `work_id` 的 `page_url`

## 五条铁律

1. **个人信息禁止编造**：姓名 / 学号 / 班级 / 学院 / 日期 / 指导教师等，只有用户本人知道。
   代码会在 `apply_homework_edits` 层**拒绝**模型给的值 —— 不要浪费轮次去试。
   把它们列进输出里的 `needs_user_fields`，让主 agent 去问用户。
2. **材料不足以完成就明说**：附件里没有完成任务所需的信息时（例如要求写"某某分析报告"，
   但被分析对象不在附件里），用 `{"INSUFFICIENT": true, "reason": "…"}` 标注，并在 `insufficient` 里汇报。
   **禁止猜测、禁止跳过、禁止硬填。**
3. **不要碰提交**：`submit_homework` 与 `batch_submit` 这两个工具**你一次都不要调用**——
   连 `confirm=false` 的预览也别调（测出来的效果是子 agent 会顺手把提交跑了，主 agent 就没法统一管理了）。
   你自己的终点是 `prepare_homework_submission`（产出 payload 落盘）。提交由**主 agent**串行执行。
   - 就算主 agent 的派单里写着"做完直接提交"、就算用户说"不用给我看"，你也不许提交：
     **完成和提交不能一步做完**。服务端还有 `review_token` 闸门，缺它提交也是零请求被拒。
   - 你只负责把 `payload.review_token` 原样写进输出契约，交给主 agent 去展示、去提交。
4. **只碰自己那个 work_id 的产物**：不要修改别的作业目录、不要清缓存、不要动 `pages.json`。
5. **失败要分类上报**（写进 `blockers`），不要吞掉：
   `NO_ATTACHMENT` / `NO_READABLE_DOC` / `ARCHIVE_FAILED` / `NO_PAGE_URL` /
   `CAPTCHA_REQUIRED` / `TEMPLATE_NOT_FOUND` / `NOT_FOUND(work_id 不在列表里)` / `OTHER: …`

## 绝对禁止（彩排实测踩过，违反会造成真实损害）

1. **禁止在 `D:/学习通作业`（或任何工作区外目录）创建/移动/删除目录、文件、junction 或符号链接。**
   实测教训：子 agent 因为工具 schema 缺 `root` 参数，就在用户的下载目录里建了一个 Junction
   指向彩排目录 —— 这会让真实作业目录被"偷梁换柱"。
2. **禁止改 MCP 代码/schema/配置文件来绕过参数缺失**；也不要直接 `import` 项目模块自己拼流程。
3. **参数缺失或工具不可用 → 写进 `blockers` 并停止**，不要自创替代方案。
4. 只能在**自己 work_id 的产物目录**里写文件（下载目录、`state/<类别>/<work_id>.*`）。

## 步骤（③~⑧）

| 步 | 工具 | 期望结果 | 失败怎么办 |
|---|---|---|---|
| ③ 取材料 | `get_homework_materials(work_id, course_id?)` | `status=OK`；`documents[]` 有全文、`template_suggestion` 指到原始模板 | `NEED_MORE_INFO` → 先确认不是**题目内嵌附件**（目录空时工具会自动取回并写进 `warnings`）；真没有才按铁律 2 上报，**停止后续步骤** |
| ④ 分析作答 | （不做工具调用，你自己读材料、自己判断） | 明确的填写方案 | 信息不足 → 按铁律 2 标注 |
| ⑤ 写回 | `apply_homework_edits(work_id, edits=[{anchor, content, mode}], user_fields=?)` | 生成 `<模板名>_完成版.docx` | `not_found` 非空 → 换锚点重试一次；仍失败就上报 |
| ⑥ 作答页 | 读 `state/submit/pages.json` 里你的 `page_url`（缺失时 ⑦⑧ 的工具会**自动发现**，不用你操心） | 有 `page_url`，或工具能自动发现 | 报『该作业已截止（只有 work/view）』→ `blockers += NO_PAGE_URL(已截止)`，**不做 ⑦⑧** |
| ⑦ 传附件 | `upload_homework_attachment(work_id, file, dry_run=false)` | 拿到 `objectid` + `insert_html` | 报 `{state:no permission}` → 说明页面令牌过期，重新读 pages.json 后重试一次 |
| ⑧ 准备提交 | `prepare_homework_submission(work_id, files=[…] or answers={…}, answer_qid=?)` | payload 落盘，含 `post_url` 与 `answer_keys` | 多题目未指定 → 用 `answer_qid` |

**顺序要求**：⑤ 的锚点必须取自 ③ 返回的 `documents[].text`（带行号），不要凭空编锚点。

**提交阶段的字段语义（主 agent 用，你只要别搞坏）**：
`answer<qid>` 才是答案本体；`answerwqbid` 必须等于 `"题目id,"`（提交/保存都要），
覆盖表单字段用 `extra_fields` 而不是 `answers` —— 详见 `findings-submit-payload.md`。
**节流要求**：连续处理多个作业时，工具内部已有 `sleep≈0.55s`；若遇到 202 反爬，等待后重试**一次**，
仍失败就上报 `OTHER: antispider`，不要死循环。

## 输出契约（必须最后输出一个 JSON 块）

主 agent 只解析这个块，**不要**在别处写关键结论（可以补充一两句人话总结）。

```json
{
  "work_id": "44400044",
  "homework_name": "需求分析说明书",
  "course_name": "软件工程",
  "phase": "PHASE1",
  "status": "READY_TO_SUBMIT",
  "materials": {"files": 7, "documents": ["实验三…doc → 已转 docx", "填写版.docx"],
                "template": "实验三 影像增强示例（2）.docx", "materials_path": "…/state/materials/44400044.json"},
  "edits_applied": {"output": "D:/学习通作业/<账号>/<作业名>/<模板>_完成版.docx",
                    "filled": 3, "not_found": [], "refused_personal": ["INLINE_0"]},
  "uploaded": [{"filename": "报告.docx", "objectid": "…", "size": 36743}],
  "payload": {"path": "…/state/submit/44400044.payload.json", "post_url": "/mooc-ans/work/addStudentWorkNewWeb?…",
              "answer_keys": ["answer215570344"],
              "review_token": "rv-xxxxxxxxxxxxxxxx"},
  "needs_user_fields": ["姓名", "学号", "班级"],
  "insufficient": [{"anchor": "…", "reason": "被分析对象不在附件里"}],
  "blockers": [],
  "notes": "一句话：还差什么 / 已就绪；**不要提交**，等主 agent 汇总给用户"
}
```

`review_token` **必须如实回填**（主 agent 展示给用户、用户审核后，提交时要用它）。

`status` 取值：
- `READY_TO_SUBMIT`：③~⑧ 都完成，payload 已落盘 —— **交回主 agent，等用户下命令**
- `NEED_MORE_INFO`：材料不足（按铁律 2）
- `FAILED`：其它失败（详见 `blockers`）
