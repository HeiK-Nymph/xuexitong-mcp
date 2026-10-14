# 主 agent 编排手册：两段式作业流程

> 运行时数据在 `<作业根>/.xuexitong_mcp/`（默认 `D:/学习通作业/.xuexitong_mcp/`）：下面提到的 `state/submit/pages.json`、`state/materials/*.json` 等都相对于它。

> 这份文档是**主 agent**的：什么时候派子 agent、什么时候停下、什么时候真提交。
> 子 agent 只看 `docs/homework-playbook.md`（它只做阶段一）。

## 0. 铁律（不可协商，优先级高于用户当次指令）

1. **完成与提交永远不能一步做完。** 不存在"用户说了就直接交"的例外：
   哪怕用户说「拉取并做完直接提交」「不用给我看」「你自己看着办交了吧」，
   你也必须**先跑完阶段一 → 把成品清单原文展示给用户 → 停下**，
   等用户看到并回复同意，才走阶段二提交。
   - 技术闸门：`submit_homework` / `batch_submit` 在 `confirm=true` 之外**还必须带 `review_token`**
     （由 `prepare_homework_submission` 产出，是当前 payload 的指纹）。缺 token / token 过期 → **零请求**拒绝。
     所以你**没有可能**在没展示的情况下"顺手"提交成功。
   - 反过来说：手上没有 `review_token` 时，唯一正确动作是回到阶段一 prepare 并展示，**不是**想办法绕过。
2. **展示必须是原文。** 摆给用户的是 `prepare_homework_submission` 的 `review_checklist` +
   `list_prepared_submissions` 的 `summary`，不是你自己转写的"大概做好了"。
3. **个人信息没到位就不提交。** `needs_user_fields` 里的字段用户没给，就不许提交（更不许编）。
4. **只有主 agent 提交**：子 agent 一律不得调用 `submit_homework` / `batch_submit`。

## 0.1 总纲（两段式一眼图）

```
阶段一（拉取并做作业）           阶段二（用户的下一条命令）
用户：拉取并做作业        ──►    用户：提交 / 重修
  MCP + 子 agent                  主 agent（提交时用 MCP；重修时不用）
  终点：payload 落盘               终点：作业交出去 / 成品改好
  ✋ 然后必须停下等用户            必须逐个如实汇报
```

**铁律：阶段一做干净就停。** 做完阶段一**不要**顺手提交，哪怕看起来万事俱备。
把成品摆给用户看，然后明确地说：「成品在这里，你说『提交』我就提交；说『重修』我就改。」

## 1. 阶段一：拉取并做作业

触发语：「拉取并做作业」「把待做作业做了」「拉一下作业并完成」「作业帮我做了」等。

步骤：

1. `whoami` —— 确认会话有效、账号 uid 是谁（落盘要按账号隔离）。
2. `list_homeworks(status="unfinished")` —— 拿到待做 `work_id` 列表；
   顺带看一眼 `unrecognized_status_items` 与状态分布，**原文转述给用户**。
   - 没有待做作业 → 如实说「扫描了 N 门课、M 条作业，没有待做」，**不要**说成"已下载/已完成"。
3. 用户要的是「全部待做」时：对**每个 work_id 派一个子 agent**（并行），把 `docs/homework-playbook.md` 的路径、
   `work_id`、`course_id`、`root` 一起给它。子 agent 之间不许互相碰对方目录。
4. 子 agent 交回 JSON 块后，主 agent 只做汇总：
   - 作答页地址现在是**全自动**的（需要时自动发现并写进 `state/submit/pages.json`），
     **不需要让用户做任何注册**；**已截止的作业会被标成 `view`**（站点只给 `/work/view` 查看页），
     这类作业 ⑦⑧ 做不了 —— 如实告诉用户，不要硬凑。
   - `list_prepared_submissions(root=...)` —— 这一步是**给用户看的成品清单**。
5. **停下**，把下面这些**原文**摆给用户（`list_prepared_submissions` 的 summary + 每个作业的要点）：

```
阶段一已完成（还没提交）：
· 44400044 需求分析说明书（软件工程）
  成品：D:/学习通作业/100000002/需求分析说明书/…_完成版.docx
  附件：…_完成版.docx（objectid eefe3fc8…，36.45K）
  提交 payload：state/submit/44400044.payload.json（17 字段，pyFlag=3）
  ⚠️ 需要你手工确认/填写：姓名、学号、班级
  ⚠️ 以下内容材料不足、我没编：…
你回『提交』我就逐个提交；回『重修』告诉我要改哪里。
```

## 2. 阶段二：用户的下一步命令

### 2a. 用户说「提交」

**由主 agent 串行执行**（不要派子 agent 去提交，提交要集中管理、方便回溯）：

1. 前置检查（任一不过就停下问用户，不要硬提交）：
   - `needs_user_fields` 里的个人信息，用户是否已提供？**没提供就不要提交**（提交上去的东西不能有编造信息）。
   - 成品文件是否比 `state/submit/<work_id>.payload.json` 新（改过文件）？改过 → 附件 objectid 已失效，
     **必须重新 `prepare_homework_submission`**（重新上传）再提交。
   - payload 的**字段数**与作答页字段数一致吗？出现 `answer<字段名>`（如 `answercourseId`）＝
     该 bug 复发 → 拒绝提交，见 `docs/findings-submit-payload.md`。
   - `answer<qid>` 里**真的带着**要交的附件吗（数 iframe / objectid）？空正文提交上去也是 `success`。
   - `answerwqbid` 是不是 `"题目id,"`？空值 → 站点回「无效的参数：code-1！」。
   - `list_prepared_submissions` 里 `phase` 是不是 `PHASE1_DONE`？不是 → 先回阶段一补。
2. 提交前把「将要提交什么」如实回显给用户（`submit_homework` 不带 confirm 时就是干这个的，零请求）。
3. **一次一个作业**：`submit_homework(work_id=…, confirm=true, review_token=…)`。
   - `review_token` 来自该作业 `prepare_homework_submission` 的输出（payload 文件里也存着 `review_token`）。
   - 报"缺少 review_token"或"不匹配" → 说明还没展示过 / 成品或附件变过：
     **重新 prepare、重新展示、重新等用户审核**，不要绕过。
   每个作业调用完，立刻把返回的 `summary` 原文转述给用户，再说下一个。
   **提交后回读核对**：再抓一次作答页，确认 `answer<qid>` 里附件 iframe / objectid 在不在 ——
   别只看 `msg=success`（实测：success 但正文是空的）。
   批量用 `batch_submit(work_ids=[…], confirm=true)` 只在用户明确说"全部提交"时用，同样如实汇报每一行。
4. 失败处理：`作业已截止/无法提交`、提交次数超限（站点要图形验证码）→ **停下报告，让用户手动提交**；
   不要改接口、不要绕过验证码、不要重试超过一次。

### 2b. 用户说「重修 / 再改 / 改一下」

**这一段不是 MCP 的活** —— MCP 只提供固定锚点式回填与提交准备，不处理灵活改写。

- 主 agent 自己读文件、自己改（docx 用 docx 相关技能/工具，压缩包自己解，图片自己看）。
- 改完之后如果用户还要提交：**文件变了 → 重新走一次⑦⑧**（`upload_homework_attachment` +
  `prepare_homework_submission`），拿到新 payload 再按 2a 提交。
  （学习通的附件接口按内容去重：内容没变会返回同一个 objectid，可以放心重传。）

## 3. 若干实测事实（省得再踩）

- 测试账号**当前没有待做作业**：要验证下载/上传链路，用 `download_random_completed_attachments`
  （测试用工具，必须向用户说明这不是正式功能），或彩排目录 `state/rehearsal/<uid>/`。
- **`enc` 是每个作业独立的**，不能跨作业复用（同课程另一个作业会 403）；老版作业列表页的 enc 也不行。
  现在**全自动**：课程菜单 enc/openc → 体验新版页(`v=2&fromMiddle=1`) → `/mooc2/work/list`，
  纯 HTTP 拿每个作业的 enc（`docs/findings-enc-auto.md`）。
- **作业已截止**：站点对作答页只给 `/mooc2/work/view` 查看页 → 没有作答页，上传/提交都做不了；
  注册课程时会标成 `view`，要如实报给用户。
- 落盘按账号隔离：`<root>/<账号uid>/<作业名>/`。
- 提交不可逆；`#pyFlag` 是保存(1)/提交(3)的唯一判别字段。
