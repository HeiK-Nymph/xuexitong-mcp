# 实测：作业 enc 的纯 HTTP 发现链路（全自动，零用户输入）

**结论**：新版作答页的 `enc` **完全不用浏览器、也不用用户给任何 URL** —— 三个已知的可抓页面就够：

```
⓪ 课程菜单页（MCP 本来就会抓）        → enc / openc / cpi
① /mooc-ans/mycourse/studentcourse?…&v=2&fromMiddle=1 → 页面里 #workEnc
② /mooc2/work/list?…&stuenc=<⓪enc>&enc=<#workEnc>     → 每个作业自己的 enc + answerId
③ 打开 ② 里的 task 链接 → dowork（可作答）或 view（已截止）
```

## 链路细节与关键参数

### ⓪ 课程菜单（已有能力）

```
GET https://mooc1-2.chaoxing.com/visit/stucoursemiddle?courseid&clazzid&vc=1&cpi
→ 菜单项形如 /work/getAllWork?classId&courseId&isdisplaytable=2&mooc=1&ut=s
             &enc=1111aaaa2222bbbb3333cccc4444dd07&cpi=100000003&openc=1111aaaa2222bbbb3333cccc4444dd08
```

`enc`（课程级）与 `openc` 都在这里，`xuexitong/worklist.py::parse_menu_params` 解析。

### ① 「体验新版」课程页 —— **必须带 `v=2&fromMiddle=1`**

```
GET https://mooc1.chaoxing.com/mooc-ans/mycourse/studentcourse
    ?courseId=111000111&clazzid=111222333&ut=s&enc=<⓪enc>&cpi=100000003&openc=<⓪openc>
    &v=2&fromMiddle=1
→ 99,078 字节，含 <input type="hidden" id="workEnc" value="1111aaaa2222bbbb3333cccc4444dd09">
```

同一路径**不带** `v=2&fromMiddle=1` 只有 20,190 字节的旧页（`workEnc` 找不到）。
这一步是整条链的关键发现。

### ② 作业列表页

```
GET https://mooc1.chaoxing.com/mooc2/work/list
    ?courseId&classId&cpi&ut=s&t=<now_ms>&stuenc=<⓪enc>&enc=<#workEnc>
→ 10,258 字节；每个作业一行：
  <a data="https://mooc1.chaoxing.com/mooc-ans/mooc2/work/task?courseId=…&classId=…&cpi=…
           &workId=44400055&answerId=55500022&enc=1111aaaa2222bbbb3333cccc4444dd10"
     onclick="goTask(this)">可行性分析报告</a>
```

- 路径是 **`/mooc2/work/list`**；`/mooc-ans/mooc2/work/list` 是 1149 字节「提示」页。
- `t` 用当前毫秒即可（实测有效）；`stuenc` 必须是与 `enc=<#workEnc>` 配对的那组。
- 44400044 拿到的 `enc=729a4c21…` 与浏览器里作答页地址栏**完全一致** → 就是 dowork 认的 enc。

### ③ task 链接的两种落点

| 最终 URL | 页面 | 含义 |
|---|---|---|
| `/mooc-ans/mooc2/work/dowork?…` | 86,834 字节，含 `uploadEnc`/`submitForm` | **可作答** ✔ |
| `/mooc-ans/mooc2/work/view?…` | 37,008 字节「作业详情」 | **作业已截止**，站点不给作答页 |

实测同课程两个作业：44400044（需求分析说明书）→ dowork；44400055（可行性分析报告，截止 09-27）→ view。

## 走过的弯路（都已被推翻，记下来省得再试）

1. `mooc2-ans.chaoxing.com/mooc2-ans/mycourse/stu?…&enc=…&t=…`：它的 `enc/t` 是页面票据，
   HTTP 侧造不出来（换 t → 783 字节提示页；缺参 → 781）。**一度以为必须让用户给一次**，
   后来发现 ① 用 `v=2&fromMiddle=1` 拿到的页面里已经带着 `#workEnc`，根本不需要这个页面。
2. 用课程菜单的 `enc` 直接当 `stuenc`+`enc` → 1149「提示」。
3. 老版作业列表页的 enc 去请求 dowork → HTTP 403（2334 字节提示页）。
4. 把 stucoursemiddle / studentcourse(旧) / getAllWork 里所有 32 位 hex 当候选 `enc` 逐个试 → 0 命中。
5. `backclazzdata`（28,665 字节课程列表 JSON）里没有这两张票据。
6. 老版行里点「查看」→ `selectWorkQuestionYiPiYue?…&evaluation=0`，全页没有 `dowork`/`standardEnc`。

## 用户给的 `i.chaoxing.com/base?ws=1&t=…` 有帮助吗

有帮助但不是必需品：它证明了课程入口在「个人空间 → 我学的课」（iframe 内），
点课程 → `studentcourse` 旧版页，点作业 → `getAllWork` 老列表 —— **这条路拿不到 enc**。
真正有用的是同一次点击里暴露出的「体验新版」URL 形态（`v=2&fromMiddle=1`），顺着它才拿到 `#workEnc`。

## 落地

- `xuexitong/worklist.py`：`parse_work_enc` / `parse_work_encs` / `build_work_list_url` /
  `build_studentcourse_url` / `parse_menu_params` / `classify_task_url` / `detect_work_kind` /
  `discover_course_encs`（菜单优先，`stu_url` 兜底）
- `tasks._auto_discover_enc`：需要 enc 时自动跑整条链，并写回 `state/submit/pages.json`。
- `register_study_page`：**降级为兜底**（个别课程菜单没有「体验新版」入口时才用），票据按账号缓存。
- 实测：清空 `state/submit/*.json`、`pages.json`、`stu_pages.json` 后，
  `open_homework_submit_page(44400044)` 仍然直接拿到「上传令牌=齐全、表单 action 已取到」；
  44400055 如实报「已截止」。
