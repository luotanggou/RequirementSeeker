# RequirementSeeker Dataset Tools

该包把已经通过 Collector Schema 校验且由用户批准的原始评论转换为脱敏数据，导出空白人工标注材料，并验证独立标注与争议裁决。它不依赖 Collector 或 Agent 的 Python 包。

## 安全边界

- 原始目录只读。`sanitize` 仅处理批准清单中的视频；清单外目录只计数，不记录原始键。
- HMAC 秘密只能通过 `--secret-env` 指定的环境变量读取。请在当前进程中用本机安全方式设置一个至少 32 字节且可稳定复用的值；不要把值写入命令参数、终端历史、文档、日志或 Git。
- `.local-data/` 下的 raw、sanitized、labels 和浏览器 profile 均为本地材料，不得提交上游。
- 自动化只生成显式 `unlabeled` 模板，不生成金标或模型语义预填。人工复核与必要裁决完成前，样本不能进入评测分母。

## 命令

```text
rs-dataset sanitize --raw <raw-root> --plan <manifest.json> --output <sanitized-root> --secret-env <name>
rs-dataset export-labels --sanitized <sanitized-root> --output <label-root>
rs-dataset validate-labels <label-root>
```

所有命令只向标准输出写一行 ASCII JSON 摘要。失败摘要只包含固定错误码，不回显原始评论、原始 ID 或秘密值。

## 产物与人工流程

`sanitize` 为每个可用视频写入脱敏后的 `video.json`、`comments.jsonl`、`collection.json`、`sanitization.json` 和 `sampling-manifest.json`。零评论或零成功页的视频只写安全排除报告，并进入 `replacement-candidates.json`。`dataset-split.json` 按伪视频 ID 稳定划分：批准的 24 个视频固定为开发 10、校准 7、留出 7；评论和回复不会跨集合。

`export-labels` 在 `<label-root>/<platform>/<video-id>/annotation.json` 写入主标注模板。每条记录包含脱敏正文和五类显式未标注字段，`clusters` 初始为空。人工填写后，将 `annotator_id` 和 `is_complete` 设置为真实状态。

无争议样本只使用 `annotation.json`。争议样本在同目录增加第二位标注者的 `annotation-secondary.json` 和裁决者的 `adjudication.json`。两位标注者必须不同，三份文件必须引用完全相同的脱敏评论集合；最终裁决不得继续标记为争议。

`validate-labels` 会报告标注文件数量和可进入评测的数量。空白模板可以通过结构校验，但可评测数量为零。未知枚举、重复评论、跨视频簇成员、重复簇成员、疑似原始 ID、虚假的完成状态或不完整争议裁决都会被拒绝。
