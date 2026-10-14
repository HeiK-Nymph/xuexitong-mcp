# 实测记录：作业附件下载（2026-10-13）

测试账号 13800000000（**另一个账号**，无待做作业）。**MCP 的正式功能未改变：下载未完成(待做)作业的附件。**

## 1. 作业列表解析：参考实现的两个真 bug（已修）

### bug 1：把导航项当作业
参考实现：`li.find("a", class_="inspectTask") or li.find("a")` 的退化分支
→ 5 门课实测混进 **56 条垃圾**（首页/任务/统计）。

正解：**容器是 `div.ulDiv li`**（导航 li 在 ulDiv 之外，天然排除），
真项判据是 li 内含 `div.titTxt`。

### bug 2：只认 inspectTask，漏掉「已批阅/已完成」作业
实测存在**两种形态**：

| 形态 | 典型 HTML | 详情 URL |
|---|---|---|
| A 可编辑 | `<a class="inspectTask" data=55200010 data2=55200006 data3=1 title="影像分割示例（2）">` | 需拼 `/mooc-ans/work/doHomeWorkNew?...` |
| B 已批阅/已完成 | `<li class="lookLi">` 内 `<a href="/mooc-ans/work/selectWorkQuestionYiPiYue?courseId=..&classId=..&workId=55200009&workAnswerId=55200005&isdisplaytable=2&mooc=1&ut=s&enc=..&workSystem=0&cpi=.." title="影像分割示例（1）">` | **href 本身就是详情页** |

只认形态 A 时：医学影像示例只解析出 1 条（真实 5 条）。
修正后：医学影像示例 **5 条**、全量 36 门课 **139 条**（此前严格口径只有 9 条）。

### 状态取值位置
`<span class="pt5"><span class="fl">作业状态：</span><strong>待批阅</strong></span>`
→ 必须取 `<strong>` 内的纯值并剥掉「作业状态：」前缀。

## 2. 附件下载链路（实测可用）

```
详情页 HTML：<div class="editor-iframe"><iframe objectid=".." filename="..">
→ GET  {mooc1}/mooc-ans/ueditorupload/read?objectId=<objectid>
   · 对 .docx/.doc/.pdf 该页只是中转页，需再取 <a class="btnDown" href=".."> 的真实地址
→ 下载字节流
```

### 实测下载结果（随机 3 个已完成作业）

| 作业 | 附件 | 结果 |
|---|---|---|
| 实验四 简单查询 | 实验四  简单查询.docx / 实验四 简单查询.sql / 实验四  简单查询_1.docx | ✅ 3/3 |
| 《自我意识和情绪意志》章节测验24-25（1） | 0 个 | 该作业本身没有附件（已 dump HTML 备查） |
| 实验1 通过Windows了解操作系统的主要功能 | 实验1 …报告纸.doc / 示例班级1黄家熙.doc | ✅ 2/2 |

共 **5 个文件 / 1349.5 KB**，落盘 `D:\学习通作业\<作业名>\<附件名>`。

**字节完整性校验**（按魔数，而不是只看大小）：
`.doc` = `d0cf11e0`（OLE）、`.docx` = `PK\x03\x04`（ZIP/Office）、`.sql` = 文本，
**没有一个是 HTML 错误页**。

## 3. 会话与反爬观察

- 会话复用生效：第二次运行 `来源 state-cookie`，未重新登录
- uid 从 cookie `_uid` 兜底解析成功：`uid=100000001`（SSO 接口没给）
- 反爬实测触发两次：扫描到第 31 门（Java核心技术）时 HTTP 202 `/antispiderShowVerify.ac`
  - 程序行为：**停止扫描 + 明确报错 + 保留已收集结果**，不静默返回空
  - 建议：批量扫描时 sleep ≥ 0.7s，并用课程菜单缓存（`state/menu_cache.json`）减少请求

## 4. 遗留待验证

1. **待做作业的附件路径未在真实数据上验证**：本账号 0 条待做（实测 139 条里
   已过期 3 / 待批阅 17 / 已完成 113 / 状态空 6）。需要用有未完成作业的账号跑
   `python scripts/download_attachments.py --mode unfinished`。
2. **6 条「状态为空」的作业**（数据库原理 1、Java核心技术 3、数据结构 2）：
   这类项会被判为 UNKNOWN，从而不算"待做"。若其中有真·待做作业，
   现有规则会漏掉 —— 需要抓这几门的列表页 HTML 核查其真实 DOM。
3. 已知限制：`--mode unfinished` 目前逐门课扫描；若某门课的列表页触发反爬，
   该课程会被跳过并记录在 errors 里（不会静默）。
