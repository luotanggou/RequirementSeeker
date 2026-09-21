# 24 视频评论采集验收记录

- 日期：2026-09-21。
- 状态：原始评论采集阶段完成；可以进入脱敏标准化与标注材料阶段。
- Collector 分支：`codex/data-collector`，验收基线 `4adbf70`。
- 批次清单 SHA-256：`9A50A0EAA9FED72C23E2340B0A0C2B32D4E7F316A8EDC263981450E70515D542`。

## 1. 验收结果

用户批准的 24 个视频全部生成可用三文件产物：`video.json`、`comments.jsonl` 和 `collection.json`。批准批次共采集 5,090 条唯一评论：

| 平台 | 视频数 | 评论数 |
|---|---:|---:|
| Bilibili | 12 | 515 |
| 抖音 | 12 | 4,575 |
| 合计 | 24 | 5,090 |

本地汇总位于 `.local-data/m2-real/candidates/approved-batch-results.md`。其中 `partial` 表示已保留有效评论，但分页停止时未达到动态目标；它不是损坏或未校验产物。所有 24 个批准目录均至少有一条评论和一个成功页面，可以进入下一阶段的质量门与动态采样。

2026-09-21 的独立结构复核确认：

- 批准清单 24 项与批准批次目录一一对应，无缺失项。
- 24 个目录均具备精确的三文件结构。
- `comments.jsonl` 行数与各自 `collection.json` 记录值一致。
- 批准批次聚合数为 Bilibili 515、抖音 4,575、总计 5,090。
- 批次清单文件摘要与本记录中的 SHA-256 一致。

## 2. 实现与最终门禁

完成阶段的最后三个修复提交为：

- `aca6fab fix(collector): skip empty Douyin comments`
- `39ec519 fix(collector): support Douyin note metadata`
- `4adbf70 fix(collector): ignore unrelated Douyin pace data`

完成会话报告的最终门禁为 Collector 827 项通过、18 项跳过，Agent 57 项通过，Ruff、格式和 mypy 全部通过。24 个批准产物目录完整，评论行数与采集记录一致。

Collector 使用平台隔离的 Chrome/Edge 专用持久登录目录，只监听页面自身产生的受支持响应。产物和审计中不得导出 Cookie、Token、密码、请求头或完整响应。真实评论和平台原始 ID 继续只保存在 `.local-data/`，不得进入 Git。

## 3. 已知输入边界

`.local-data/m2-real/raw/` 当前共有 26 个视频目录：24 个批准批次目录和 2 个早期双平台试点目录。后续数据工具必须把 `approved-manifest.json` 的 24 个 `(platform, video_key)` 作为严格允许列表：

- 只读取、脱敏和拆分批准清单中的 24 个目录。
- 批准项缺失、平台或视频键不匹配时关闭式失败。
- 清单外目录不进入数据集，并在安全汇总中只记录排除数量，不记录原始键或文本。
- 不移动、修改或删除原始目录。

## 4. 阶段边界与下一步

当前只完成原始采集。尚未创建 `packages/dataset-tools`，也尚未生成：

- 脱敏伪 ID 数据集与 `sampling-manifest.json`；
- 稳定的 10/7/7 视频拆分；
- 空白人工标注模板；
- 两人独立标注和争议裁决；
- 可用于真实模型评测的脱敏金标。

下一步执行 `docs/superpowers/plans/2026-09-09-dataset-preparation.md`。工具实现和人工 fixture 测试可以立即开始；处理真实 24 视频前，必须在当前进程环境中提供至少 32 字节的数据集 HMAC 秘密，并保证重跑时使用同一秘密。秘密值不得写入命令行参数、日志、文档或 Git。

真实模型有限冒烟仍需至少 12 个脱敏且完成初标的视频；完整真实评测仍需 24 个视频完成独立标注和必要裁决。仅有 24 个原始采集目录不满足这两个模型评测门槛。
