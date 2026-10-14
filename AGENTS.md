# xuexitong_mcp 约定（学习通作业助手）

本目录的规矩。**给别人装/用请看 `USAGE.md`**（写给 AI 的操作说明：配置、19 个工具、硬规则、排错）；
细节见 `docs/main-agent-orchestration.md` 与 `docs/homework-playbook.md`。
只在操作**学习通/超星作业**时生效；改动也请限制在本目录内（`D:\myproject\McpAndSkill` 下面是多个项目，
不要往上层目录写文件）。

## 两条硬规则

1. **作业流程分两段，中间必须停。**
   - 阶段一（用户说「拉取并做作业」）：取材料 → 分析 → 回填 DOCX → 上传附件 → 准备 payload。
     终点是 payload 落盘，然后**停下来**：用 `list_prepared_submissions` 把成品清单**原文**摆给用户看，
     等用户下一条命令。
   - 阶段二：用户说「提交」→ 由**主 agent** 逐个 `submit_homework(confirm=true, review_token=…)`；
     用户说「重修 / 再改」→ 主 agent 自己灵活改文件，**这一段不用 MCP 工具**。
2. **完成与提交永远不能一步做完。**
   即使用户说「做完直接提交」「不用给我看」，也必须先展示成品、等用户审核后再提交。
   技术闸门：提交工具除 `confirm=true` 外还必须带 `prepare_homework_submission` 产出的
   `review_token`，缺 token / token 过期一律**零请求**拒绝。

## 其它约定

- 子 agent 只做阶段一（一个子 agent 一个 `work_id`），**禁止**调用 `submit_homework` / `batch_submit`。
- 个人信息（姓名/学号/班级/日期…）与材料不足处**禁止编造**：留给用户填，或明确告知缺什么。
- 落盘按账号隔离：`<作业根>/<账号uid>/<作业名>/`（默认作业根 `D:/学习通作业`）。
- **运行时数据一律不写进项目目录**：状态（cookie/缓存/payload/作答页地址表）在 `<作业根>/.xuexitong_mcp/`，
  凭据在 `<state>/.env`；项目里只允许出现代码、文档、测试 —— 这样才能整包分享。
- 作答页 `enc`：**全自动**（课程菜单 enc/openc → 体验新版页 → `/mooc2/work/list`），不需要用户给 URL；
  `register_study_page` 只是个别课程没有「体验新版」入口时的兜底；
  已截止的作业站点只给 `/work/view` 查看页，如实上报，不要硬凑。
- 凭据只在本目录 `.env` 或环境变量里，不要写进工具参数、不要写进回答。
- 取材料会自动补：目录里没有可读文档时，`get_homework_materials` 会把**题目内嵌附件**取回本地；
  `node_modules` 等依赖/缓存目录不算材料（会写进 warnings）。
- **提交链路的两个硬语义**（2026-10 实测踩过）：① `answerwqbid` 必须带上（空值站点回
  「无效的参数：code-1！」）；② 覆盖答案用 `extra_fields`，`answers` 的键只能是题目 id
  （否则垃圾键泛滥、真正的 `answer<qid>` 不被覆盖 → 提交成功但正文为空）。
  详见 `docs/findings-submit-payload.md` 与 `docs/findings-prompt-attachments.md`。
- 调用 `download_*` / `update_*` 类工具后，把返回的 `summary` 原文转述给用户。
