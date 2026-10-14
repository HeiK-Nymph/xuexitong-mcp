# 实测记录：课程列表 / 作业列表 / 未完成作业（2026-10-13）

测试账号：已登录（AES 登录路径），未记录任何凭据。

## 1. 课程列表 —— 可用

`GET https://mooc1-api.chaoxing.com/mycourse/backclazzdata`

- 实测：**36 门课**，字段路径 `channelList[].content.course.data[]`，`content.id` = class_id，`content.cpi` = cpi
- 同一 courseId 会出现多次（不同 class_id），属正常：如「大数据技术与应用」course_id=666000666
  出现 class_id=153520002 / 153520001 两条
- 无分页参数，一次返回全部

## 2. 课程菜单 → 作业入口 —— 可用

`GET https://mooc1-2.chaoxing.com/visit/stucoursemiddle?courseid=&clazzid=&vc=1&cpi=`

- "作业"项 `href="javascript:void(0)"`，真实 URL 在 `data` 属性：
  `/work/getAllWork?classId=..&courseId=..&isdisplaytable=2&mooc=1&ut=s&enc=<32hex>&cpi=..&openc=<32hex>`
- 菜单里也有别的入口（如"待批作业" → `/mooc-ans/work/evaluation?...`）

## 3. 作业列表页 —— 技术上可解析，但数据不全

真实 DOM（`state/dump/*.html`）：

```html
<div class="ulDiv"><ul><li>
  <a class="inspectTask" data="44400044" data2="44400088" data3="1" title="需求分析说明书">
  <span class="pt5"><span class="fl">开始时间：</span>2026-10-12 08:34</span>
  <span class="pt5"><span class="fl">截止时间：</span>2026-10-18 23:59</span>
  <span class="pt5"><span class="fl">作业状态：</span><strong>待批阅</strong></span>
</li></ul></div>
```

- 作业项必须以 `a.inspectTask` 判定。参考实现的 `li.find("a")` 退化分支会把导航项
  （首页/任务/统计）当成作业 —— 5 门课实测混进 56 条垃圾
- 状态值在 `<strong>` 内，外层是「作业状态：」前缀

### 实测数据（全量 36 门课）

- 作业总数 **9 条**：`待批阅` × 8、`已过期` × 1
- **「待做」0 条**；严格口径未完成 = 0
- 第 35 门（Java核心技术）触发反爬：HTTP 202 `/antispiderShowVerify.ac`，扫描中断
  （反爬时我们显式报错，不返回空列表）

### status / pageNum 参数实测无效

页面 JS 里有筛选与分页：

```js
function checkIt(ele, value) {
  location.href = "/mooc-ans/work/getAllWork?classId=..&courseId=..&status=" + value
                  + "&sw=" + sw + "&isdisplaytable=2&mooc=1&ut=s";
}
function changePage(pageNum) {
  location.href = "/mooc-ans/work/getAllWork?courseId=..&classId=..&pageNum=" + pageNum ...
}
```

但同一门课（软件工程 111000111/111222333）实测：

| status | 结果 |
|---|---|
| 不带 | 1 条，`待批阅` |
| -1 / 0 / 1 / 2 | 全部同样 1 条，`待批阅` |

`pageNum=1` 与 `pageNum=2` 结果也完全相同。页面自身 `page.showPage(1, 1, ...)` 显示"共 1 页"。

## 4. 结论：PC 页面 ≠ App 的「未完成」列表

用户在 App/新版界面看到的是 14/17 未完成（例：`实验十六 综合大作业（系统文档）`、`实验十六 综合大作业（代码）`、`实验六 需求分析`，状态文案是**「未交」**），而这些作业名在 36 门课的 PC 作业页里**一条都没有出现**。

所以「未完成作业」**不能**从 `/work/getAllWork` 得到，需要换数据源：

- 候选：App/新版 web 用的接口（可能在 `mobilelearn.chaoxing.com`，chaoxing_mcp 声明了这个域名但从未使用）
- 待办：抓一次真实请求（浏览器 DevTools 或 App 抓包），拿到「未完成」筛选对应的 URL 与响应

## 5. 关于「未交」的待验证假设

用户假设「未交可能是过期作业」。目前证据：PC 页面把过期未交的标为 **`已过期`**（实测 1 条），
而 App 标为「未交」。若成立，则 App 的「未完成」= 未提交（含已过期），与 PC 的语义并不等价，
规则表（待做/待批阅/已完成）在 App 文案下会全部落到 UNKNOWN。
