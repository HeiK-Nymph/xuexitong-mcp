# 提交 payload 的两个坑（实测复盘 2026-10-14）

一次真实提交（示例课程（2026 秋）→ 示例实验·VUE(一)，work_id 44400077）踩了两个坑，
都是"站点回 success、但内容不对/发不出去"这种**静默失败**，最难查。这里把结论和**可复用的
诊断方法**一起记下来。

## 结论一：`answerwqbid` 必须带，且值形如 `"题目id,题目id,"`

- 现象：提交 POST 返回 `{"status": false, "msg": "无效的参数：code-1！"}`，作业状态仍是「待做」。
- 根因：本项目的 `prepare_payload` **从来没组装过 `answerwqbid`**（全仓库只有测试 fixture 里出现过
  这个字段名）。站点自己的提交/保存 JS 在 `ready2Submit()` 里会写：

  ```js
  function ready2Submit() {
      var qids = "777000777,";
      $("#questionIds").val(qids);   // ← name=answerwqbid
  ```

- 修复：`xuexitong/submit.py::prepare_payload`

  ```python
  if page.question_ids:
      fields["answerwqbid"] = ",".join(page.question_ids) + ","
  ```

- 注意它是**提交和保存都要带**的（`saveAnswer()` 与 `confirmSubmitWork()` 都会 serialize 它），
  所以在 `prepare_payload` 里统一设置最省事。

## 结论二：`answers` 的键是**题目 id**，不是表单字段名

- 现象：提交 POST 返回 `{"status": true, "msg": "success!"}`，作业状态也变成「待批阅」，
  但**答案正文是空的**（附件上传了却不在正文里）。
- 根因：`submit_homework()` 里写了
  `prepare_payload(page, answers=prepared["fields_full"])`。
  `prepare_payload` 的 `answers` 语义是 `{题目id: 答案HTML}`，实现是
  `fields[f"answer{qid}"] = content`；传进来的是 17 个**字段名**，于是：

  | 传入的键 | 被写成的字段 | 后果 |
  | --- | --- | --- |
  | `courseId` | `answercourseId` | 垃圾字段，站点忽略 |
  | `answer777000777` | `answeranswer777000777` | **真正的 `answer777000777` 没被覆盖** |

  提交时字段数 **17 → 34**，就是这一条的外在表现。

- 修复：`xuexitong/tasks.py::submit_homework` 改用 `extra_fields=prepared["fields_full"]`
  （直接覆盖字段），并在 `prepare_payload` 里加了**硬校验**：`answers` 的键不在
  `page.question_ids` 里就直接 `SubmitError`，不允许"静默发垃圾字段"。

## 可复用的诊断方法（比结论更重要）

1. **页面自己的 JS 是参数的权威来源。** 别猜字段：在作答页 HTML 里搜
   `#submitForm`、`ready2Submit`、`submitAnswer`、`serialize()`，看站点**实际**发什么。
   本次两个 bug 都是这么定位的。
2. **用"只保存草稿"做 A/B 对照。** 想验证某一组字段能不能过，用 `pyFlag=1`（保存）而不是
   `pyFlag=3`（提交）：
   - 保存可逆、不消耗提交次数、失败也只是拒一次；
   - 一次只改一个变量。本次对照：

     | `answerwqbid` | 站点响应 |
     | --- | --- |
     | `""` | `{"status": false, "msg": "无效的参数：code-1！"}` |
     | `"777000777,"` | `{"status": true, "msg": "保存成功！"}` |

   （做完记得把对照写入的草稿清掉，否则正式提交时会把它当已有答案 prepend 进去，出现重复附件。）
3. **和上一次成功的 payload 对比字段集合。** `state/submit/<work_id>.payload.json` 留了历史，
   差集里往往直接就是答案。
4. **字段数异常 = 告警。** 审阅时 17 个、提交时 34 个，这个数字对不上就该停下来查，
   而不是看 `"msg": "success!"`。
5. **`success` 不等于内容对。** 提交后要**回读站点侧的答案字段**核对（本项目用
   `tasks._resolve_submit_page` 再抓一页，看 `answer<qid>` 里有没有附件 iframe / objectid）。
6. **失败不超过两次就停下报告。** 不要在提交这种不可逆动作上连续试错；宁可让用户手动交。

## 防回归

- `tests/test_submit.py::PreparePayloadTest::test_payload_fills_answerwqbid_with_question_ids`
  （提交/保存两种模式都断言 `"777,"`）
- `tests/test_submit.py::PreparePayloadTest::test_answers_keys_must_be_question_ids`
  （传字段名必须抛 `SubmitError`）

## 相关

- 站点对 `answers` 里附件的表达：`<div class="editor-iframe"><iframe objectid=".." filename="..">`
  —— 与附件区、与"题目内嵌附件"是同一套形状，别搞混（见 `findings-prompt-attachments.md`）。
- 提交次数守卫：页面 `#addTimes` / `#limitWorkSubmitTimes`（本次实测 `addTimes` 提交前后都是 0）。
