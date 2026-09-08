# 自动化评论采集器设计

- 日期：2026-09-08。
- 状态：对话设计已批准，等待书面规格复核。
- 目标分支：`codex/data-collector`。
- 依据：`.local-data/m2-collector-task.md`、Agent M2 模型网关、需求信号与聚类设计、`docs/PRD.md`。

## 1. 目标与范围

交付一套可重复、可审计的网页版评论采集流程，覆盖抖音网页版和 Bilibili 网页版。采集器通过可见 Playwright 浏览器访问用户指定的公开视频，在当前浏览器会话内监听页面自身产生的响应，并把视频元数据、原始评论和采集审计信息写入 `.local-data/m2-real/raw/`。

本阶段先完成两个平台各一个视频的试点。试点自动化测试、样例输出和采集报告通过人工确认后，才执行 24 个视频的批量清单。

以下内容不属于本任务：

- 自动搜索热门视频、Tag 扩散或候选视频推荐；
- 绕过验证码、登录限制、访问控制、限流或平台保护；
- 保存、导出或复用 Cookie、Token、请求头、密码和浏览器配置；
- 脱敏、标准化、标注、模型分析或机会创建；
- 成为 `packages/agent` 的运行依赖；
- 把平台当前页面结构承诺为长期稳定接口。

## 2. 已批准决策

1. 使用可见 Playwright 浏览器。登录由用户在当前窗口完成，浏览器上下文不持久化。
2. 允许监听页面自身发出的评论接口响应，但只解析响应正文；不得读取、保存或输出请求头、Cookie、Token 或浏览器配置。
3. 网络响应是评论数据的首选来源，DOM 只用于响应无法覆盖的视频元数据、用户操作和明确的回退路径。
4. 抖音和 Bilibili 使用独立平台适配器，共享严格数据模型、目标数量计算、去重合并和审计写入。
5. 采集代码使用独立 Python 3.12/uv 项目，并在 `codex/data-collector` 分支开发。
6. 视频由显式清单提供，不实现热门池发现。清单先校验平台、方向和评论规模覆盖。
7. 先对每个平台各验证一个视频并展示结果；用户确认后才运行批量清单。
8. 原始数据、截图、暂存文件和运行日志只保存在 `.local-data/`，不得提交。

## 3. 工程边界与组件

采集器位于独立的 `packages/collector/`，拥有自己的 `pyproject.toml`、锁文件、源码、Schema、离线 fixture 和测试。`packages/collector` 不导入 `packages/agent`，`packages/agent` 也不依赖采集器。

主要组件：

| 组件 | 责任 | 不负责 |
|---|---|---|
| CLI | 校验清单、启动单视频试点或批量运行、选择输出根目录 | 自动发现视频或处理模型结果 |
| BrowserSession | 启动非无头临时 Chromium 上下文、等待用户登录、提供页面事件 | 持久化登录态或绕过平台保护 |
| PlatformAdapter | 识别本平台页面、响应形状和 DOM 回退，产出统一候选记录 | 跨平台去重或写文件 |
| CollectionPlanner | 根据报告总数计算目标，追踪分层配额和停止条件 | 声称平台不支持的随机或排序能力 |
| RecordMerger | 按原始评论 ID 去重，检测作者或正文冲突 | 静默覆盖冲突记录 |
| ArtifactWriter | 严格校验、暂存并以视频目录为单位提交三个正式文件，写运行报告 | 向 Git 跟踪目录写原始数据 |
| AuditLogger | 记录时间、计数、页数、模式版本和错误类别 | 记录请求正文、响应原文或敏感登录信息 |

## 4. 命令接口

采集器提供 `rs-collect` 命令：

```text
rs-collect validate-plan <manifest.json>
rs-collect pilot --platform <douyin|bilibili> --url <public-video-url>
rs-collect batch <manifest.json>
```

`validate-plan` 只读取清单并报告：

- 总视频数是否为 24；
- 两个平台是否尽量各 12 个，未覆盖平台是否被显式标记；
- 五个方向是否分别满足 6、6、4、4、4 个视频；
- 三个评论规模档是否分别满足 8 个视频；
- 视频键是否在平台内唯一，URL 是否与声明平台一致。

清单为每个视频提供 `platform`、`video_key`、`url`、`direction` 和计划的 `comment_scale`。计划规模只用于覆盖校验，采集时仍以页面报告总数计算实际目标；若实际规模档不同，运行报告记录偏差，批量汇总不得继续声称原计划覆盖已满足。

`pilot` 每次只处理一个视频。`batch` 按清单顺序逐个处理；一个平台失败不阻止另一平台继续，但相同平台遇到登录失效、验证码或结构未知后停止该平台剩余项目，避免重复触发保护。

## 5. 浏览器与平台数据流

1. CLI 创建运行 ID 和本地运行目录，启动非无头 Chromium 临时上下文。
2. 打开目标公开视频。若需要登录，程序保持窗口可见并等待用户完成；等待有明确超时且不自动重复登录。
3. 适配器在页面导航前注册响应监听，只处理白名单化的响应 URL 模式和可识别 JSON 形状。解析器接收响应正文，不接收请求头或浏览器存储。
4. 适配器从响应或 DOM 获取视频元数据和平台报告评论总数。DOM 回退只读取视频元数据和页面状态，不从渲染后的评论节点拼装评论记录。无法获得的统计值保存为 `null`，不能填零。
5. 采集器按平台实际能力切换高互动、最新、回复链和长尾入口，通过滚动、翻页或展开回复触发页面自身请求。
6. 适配器把平台载荷转换成统一候选评论，并记录真实来源层和页面/排名。无法可靠确定父评论时，父 ID 为 `null`，不伪造回复关系。
7. CollectionPlanner 去重候选、追踪配额和停止条件。达到目标、页面无更多结果、受控错误或预算上限时停止当前视频。
8. RecordMerger 与上一次有效结果合并。无冲突记录按评论 ID 更新采集元数据；作者或正文冲突时保留旧记录并写冲突审计。
9. ArtifactWriter 在运行目录中生成完整的视频暂存目录，运行 Schema 和跨文件一致性校验。全部通过后才以目录为单位提交；失败时保留上一次有效结果。
10. 关闭临时浏览器上下文，不写 storage state 或用户数据目录。

适配器只认可显式支持的载荷版本或字段组合。响应返回 HTTP 成功但形状未知时，归类为页面/响应结构变化，而不是猜测字段含义。

## 6. 数量目标与分层

平台报告总数为 `total` 时：

```text
total <= 200       -> target = total
201 <= total <= 2000 -> target = min(500, max(200, ceil(total * 0.25)))
total > 2000       -> target = min(1000, max(500, ceil(10 * sqrt(total))))
```

平台报告总数确实不可得时，`reported_total` 和 `video.total_comment_count` 为 `null`，以 200 条作为保守试点上限，并记录 `reported_total_unavailable`。该结果不得标记为完整采集。

目标分层为：高互动/靠前 35%、最新 25%、回复链 20%、长尾 20%。配额采用确定性最大余数法取整，总和必须等于目标。同一评论出现在多个层时只保存一次，保留首次被接受时的 `source_stratum`。缺额按长尾、最新、高互动、回复链的稳定顺序，用尚未选择的候选补齐。

长尾只使用平台实际分页顺序、时间顺序或稳定间隔抽取。平台不提供某类排序或无法到达足够深度时，保存实际顺序并记录缺失层和缺额，不能声称随机采样。

## 7. 输出契约

每个视频写入：

```text
.local-data/m2-real/raw/<platform>/<video-key>/
  video.json
  comments.jsonl
  collection.json
```

`video.json` 严格包含：

- `platform`、`raw_video_id`、`raw_author_id`；
- `title`、`description`、`published_at`、`duration_seconds`；
- `total_comment_count`、`view_count`、`like_count`、`favorite_count`、`share_count`、`author_follower_count`；
- `captured_at`。

无法获取的统计值使用 `null`。时间有可靠绝对时间时使用带时区 ISO 8601；只有平台相对时间且无法可靠还原时使用 `null`，不猜测日期。

其中 `platform` 为 `douyin|bilibili`，三个原始 ID 和文本字段为字符串，`raw_video_id` 与 `raw_author_id` 必须为非空字符串；`published_at`、`duration_seconds` 和所有统计值允许为 `null`，非空计数和时长必须是非负数；`captured_at` 必须是带时区时间。

`comments.jsonl` 每行严格包含：

- `raw_comment_id`；
- `raw_author_id` 或 `null`；
- `raw_parent_comment_id` 或 `null`；
- `text`；
- `published_at` 或 `null`；
- `collected_at`；
- `like_count`、`reply_count`、`is_video_author`，不可得时为 `null`；
- `source_stratum`；
- `source_page_or_rank`。

`raw_comment_id` 和 `text` 必须为非空字符串；`raw_author_id`、`raw_parent_comment_id` 和 `published_at` 允许为 `null`；计数为空或非负整数；`is_video_author` 为空或布尔值；`source_stratum` 为 `top|recent|replies|long_tail`；`source_page_or_rank` 是从 1 开始的实际页码或该入口下的稳定顺序号；`collected_at` 必须是带时区时间。

`collection.json` 严格包含：

- `reported_total`；
- `collected_total`；
- `pages_requested`、`pages_succeeded`；
- `sort_modes`；
- `collection_started_at`、`collection_finished_at`；
- `collection_errors`。

`reported_total` 为空或非负整数；其余数量为非负整数；`sort_modes` 是本次实际使用的去重模式字符串列表；起止时间必须带时区且结束时间不早于开始时间。

`collection_errors` 是结构化数组，错误至少含类别、发生时间、阶段和安全描述。重跑冲突额外记录评论 ID 与冲突字段名，不记录新旧正文或作者值。

每轮另写 `.local-data/m2-real/runs/<run-id>/run.json` 和可选截图，汇总平台、视频键、起止时间、请求/成功页数、最终数量、错误和最终状态。日志与报告不得包含完整响应、请求头、Cookie、Token、密码或浏览器配置。

## 8. 幂等、冲突与原子性

`raw_comment_id` 是单个平台视频内的幂等键。重跑行为如下：

- ID 首次出现：新增记录；
- ID 已存在且作者、正文相同：更新同一记录的可变采集字段，不创建重复行；
- ID 已存在但作者或正文改变：保留上一次有效记录，记录冲突，不静默覆盖；
- 同一运行中 ID 重复且内容一致：只输出一行，并按高互动、最新、回复链、长尾的既定采集顺序保留首次来源；
- 同一运行中 ID 重复且内容冲突：当前输出校验失败，上一次有效文件保持不变。

写入顺序为在 `.local-data/m2-real/.staging/<run-id>/<platform>/<video-key>/` 生成完整的 `video.json`、`comments.jsonl`、`collection.json`，再执行逐文件 Schema 校验和跨文件校验。跨文件校验至少确认平台一致、评论 ID 唯一、父 ID 不自指、计数一致、时间格式有效。全部通过后把旧视频目录移动到同一运行的备份目录，再把暂存目录移动到正式位置；第二步失败时立即恢复备份。启动时若发现上次中断的备份，先恢复到正式位置并记录 `interrupted_commit_recovered`，因此半成品目录不会被当成有效结果。

## 9. 失败、停止与恢复

以下情况立即停止当前平台并请求用户处理：

- 验证码或其他人工挑战；
- 登录失效或登录等待超时；
- 明确的访问限制；
- 页面或响应结构无法可靠识别；
- 需要绕过平台保护才能继续。

限流和暂时网络失败只允许有限重试，并使用有上限的退避；达到上限后保留已成功候选并把结果标记为部分采集。适配器解析或 Schema 校验错误不进行盲目重试。

批量运行中，某个平台停止后，另一平台仍可继续。已经通过校验并提交的视频结果保持有效；当前视频未通过校验的暂存内容不覆盖正式文件。重跑从视频边界恢复，不尝试恢复浏览器登录态或页面内部游标。

## 10. 测试与验收

### 10.1 自动化测试

- 数量公式：覆盖 0、200、201、2,000、2,001 以及上限截断。
- 分层配额：覆盖取整、重复候选、缺层和稳定补齐。
- 输出模型：覆盖完整记录、可空统计值、未知字段拒绝和时间格式。
- JSONL 与原子写入：覆盖成功提交、校验失败不覆盖和计数一致性。
- 幂等与冲突：覆盖同 ID 重跑、正常更新、作者冲突、正文冲突和单次运行冲突。
- 平台适配器：使用不含真实用户数据的人工 fixture 覆盖元数据、一级评论、二级评论、分页、字段缺失和结构变化。
- Playwright 集成：使用本地模拟网页验证响应监听、仅元数据的 DOM 回退、滚动/翻页停止和浏览器临时上下文。
- 日志安全：使用敏感标记值验证日志、异常和报告均不泄漏请求头、Cookie、Token 或密码。
- CLI：覆盖清单分布校验、平台隔离、受控停止和最终状态。

真实平台数据不进入测试 fixture，不提交截图或运行日志。平台试点用于验证当前页面兼容性，不替代离线回归测试。

### 10.2 试点验收

1. 全部离线测试、静态检查和构建通过。
2. Bilibili 选择一个公开视频，在可见浏览器中完成单视频采集。
3. 抖音选择一个公开视频，在可见浏览器中完成单视频采集。
4. 两个平台样例均产生字段完整且通过校验的 `video.json`、`comments.jsonl`、`collection.json` 和运行报告。
5. 在 VS Code 打开三个样例文件和执行日志，并向用户说明数量、缺失字段、实际分层、错误和平台变化。
6. 用户确认样例后，才执行 24 视频批量清单。

### 10.3 完成标准

代码完成指采集器、严格 Schema、离线 fixture、自动化测试和 CLI 可用，且两个平台试点流程已准备就绪。真实采集完成还需要用户在可见窗口中完成必要登录、提供或确认试点视频及 24 视频清单，并批准从试点进入批量。

24 视频采集结果必须报告实际平台、方向和评论规模覆盖；受阻平台独立标记，不能用另一平台补足后声称双平台覆盖完成。
