# "作业没有附件"往往是假象：题目内嵌附件（实测 2026-10-14）

## 现象

`get_homework_materials` 直接返回 `NEED_MORE_INFO`：

> ⚠️ 无法继续《示例实验·VUE(一)》：该作业**没有任何附件**（含压缩包）也不能据此完成任务。
> （目录：D:\\学习通作业\\100000002\\示例实验·VUE(一)，共 0 个文件）

但这个作业**是有题目的** —— 题目正文就是题目里嵌的一份 docx
（`示例实验·VUE(一).docx`，18.5 KB）。它不在"作业附件区"，所以目录扫描永远看不到它。

## 题目附件在哪

新版作答页（`/mooc-ans/mooc2/work/dowork?...`）的**题干**里：

```html
<div class="questionLi" id="question777000777">
  <h3 class="mark_name">
    1. <span class="colorShallow">(简答题)</span>
    <p><br></p>
    <div class="editor-iframe">
      <iframe objectid="0123456789abcdef0123456789abcdef"
              filename="示例实验·VUE(一).docx" filetype="docx" filesize="18.50K"></iframe>
    </div>
  </h3>
  <div class="stem_answer">  ← 作答区，这里也会有 editor-iframe！
    ...
  </div>
</div>
```

下载链路（站点自己的 `insertCloud.html → downloadAttach()` 就是这么做的）：

```
GET /ananas/status/<objectid>
→ {"status": "success", "filename": "示例实验·VUE(一).docx",
   "download": "http://d0.cldisk.com/download/<objectid>?at_=..&ak_=..&ad_=.."}
→ 下载 download 直链
```

objectid 过期/无权限时返回的 JSON 里没有 `download` 字段。

## 判别法：题干 vs 作答区

**一定只取题干（`h3.mark_name`）里的附件。** 作答区 `.stem_answer` 里同样是 `editor-iframe`，
一起抓的话会把自己**已经交上去的附件**当成题目材料再下一遍（本次清草稿时差点被这个坑到）。
实现见 `xuexitong/attachments.py::parse_prompt_attachments`。

## 现状：已经自动化

`get_homework_materials` 在"目录里没有可读文档"时会**自动**：

1. 打开作答页（`_resolve_submit_page`，enc 全自动发现，不需要用户给 URL）；
2. 解析题干内嵌附件（`parse_prompt_attachments`）；
3. 用 `/ananas/status` 直链下载到作业目录（失败退回 `read?objectId=` 中转页）；
4. 重新扫描目录，并把结果写进 `prompt_attachments` / `warnings` / `summary`。

全程 **best-effort**：任何异常只记一条 note，绝不拖垮"取材料"本身。
实测（空目录 → 自动取回）：文件 1 个、可读文档 1 份、`prompt_attachments` 1 条、status OK。

## 方法论

1. **看到"没有附件"先别急着让用户补文件**：先怀疑"题目在题目里"。
   去作答页抓原始 HTML，看 `mark_name` 里有没有 `editor-iframe`。
2. **别自己造轮子抓页面**：本项目已经有会话与 enc 自动发现，走
   `_resolve_submit_page` + `parse_prompt_attachments` 比手写 HTTP 稳。
3. **同一套附件形状出现在三个地方**（题目题干、作答正文、作业附件区），
   处理前先问"这是哪一处"，否则很容易把用户自己交的东西当材料、或者把材料当成品。
