# 候选视频发现与覆盖规划实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在原始采集器通过双平台试点后，交付可审计的 `rs-collect discover` 与 `validate-plan`，从显式公开入口生成候选清单并验证 24 视频覆盖要求。

**Architecture:** 候选发现复用 `packages/collector` 的可见浏览器、挑战处理和平台响应边界，但使用独立候选适配器，不读取完整评论。默认保持临时 Chromium；真实发现可显式选择现有的 `--browser chrome|edge --reuse-login` 平台隔离专用 profile，不读取或导出其中的登录凭据。候选清单是严格版本化文件；覆盖规划只报告缺口和确定性建议，不自动批准或调用批量采集。

**Tech Stack:** Python 3.12、Pydantic 2、Playwright Python、pytest、Ruff、mypy strict、uv。

---

## 前置门禁

开始本计划前，`2026-09-09-raw-comment-collector.md` 的 Task 1–8 必须完成，且双平台试点至少各产生一个结构有效样例。本计划不修改原始输出契约。

## 文件职责图

| 路径 | 职责 |
|---|---|
| `packages/collector/src/requirementseeker_collector/candidates/contracts.py` | 候选与发现报告契约；复用已有批量清单。 |
| `packages/collector/src/requirementseeker_collector/candidates/adapters.py` | 双平台候选响应/DOM 元数据解析。 |
| `packages/collector/src/requirementseeker_collector/candidates/discovery.py` | 显式查询或入口的候选发现编排。 |
| `packages/collector/src/requirementseeker_collector/candidates/coverage.py` | 24 视频平台、方向、规模档覆盖验证。 |
| `packages/collector/src/requirementseeker_collector/cli.py` | 增加 `discover` 和 `validate-plan`。 |
| `packages/collector/schemas/candidate-video.schema.json` | 固定候选 Schema。 |
| `packages/collector/tests/candidates/` | 候选、覆盖、CLI 与浏览器集成测试。 |

### Task 1: 定义候选与版本化清单契约

**Files:**
- Create: `packages/collector/src/requirementseeker_collector/candidates/__init__.py`
- Create: `packages/collector/src/requirementseeker_collector/candidates/contracts.py`
- Create: `packages/collector/tests/candidates/test_contracts.py`
- Create: `packages/collector/schemas/candidate-video.schema.json`

- [x] **Step 1: Write the failing test**

```python
def test_candidate_requires_a_public_platform_url() -> None:
    with pytest.raises(ValidationError, match="url_platform_mismatch"):
        CandidateVideo(**candidate_data(platform="bilibili", url="https://www.douyin.com/video/1"))


def test_manifest_rejects_duplicate_platform_video_key() -> None:
    item = ManifestVideo(**manifest_video_data())
    with pytest.raises(ValidationError, match="duplicate_platform_video_key"):
        CollectionManifest(manifest_version="1.0", videos=[item, item], unavailable_platforms=[])


def test_manifest_rejects_credentials_in_url() -> None:
    with pytest.raises(ValidationError):
        ManifestVideo(**manifest_video_data(url="https://user:pass@bilibili.com/video/BVfake"))
```

- [x] **Step 2: Run test to verify it fails**

Run: `uv run --project packages/collector pytest packages/collector/tests/candidates/test_contracts.py -q`

Expected: candidate contract import fails.

- [x] **Step 3: Write minimal implementation**

```python
class CandidateVideo(Contract):
    platform: Platform
    video_key: Identifier
    url: PublicHttpUrl
    title: str
    reported_comment_count: NonNegativeInt | None
    direction: Direction
    query: str | None
    source_page: PublicHttpUrl
    source_rank: PositiveInt
    discovered_at: Timestamp

    @model_validator(mode="after")
    def platform_url(self) -> Self:
        if not url_matches_platform(str(self.url), self.platform):
            raise ValueError("url_platform_mismatch")
        return self
```

Import `CollectionManifest`, `Direction` and `ManifestVideo` from the collector root contracts without redefining them. Export `CandidateVideo.model_json_schema()` to the checked-in candidate schema; keep the existing collection manifest schema unchanged.

- [x] **Step 4: Run contract tests and checks**

Run: `uv run --project packages/collector pytest packages/collector/tests/candidates/test_contracts.py -q && uv run --project packages/collector mypy packages/collector/src`

Expected: contract tests and mypy pass.

- [x] **Step 5: Commit**

```bash
git add packages/collector/src/requirementseeker_collector/candidates packages/collector/tests/candidates/test_contracts.py packages/collector/schemas/candidate-video.schema.json
git commit -m "feat(collector): add candidate manifest contracts"
```

### Task 2: 解析双平台候选列表而不读取完整评论

**Files:**
- Create: `packages/collector/src/requirementseeker_collector/candidates/adapters.py`
- Create: `packages/collector/tests/candidates/test_adapters.py`
- Create: `packages/collector/tests/fixtures/bilibili/candidates.json`
- Create: `packages/collector/tests/fixtures/douyin/candidates.json`

- [x] **Step 1: Write the failing test**

```python
def test_bilibili_candidates_keep_page_rank_and_query() -> None:
    items = parse_bilibili_candidates(load("bilibili/candidates.json"), "效率工具", SOURCE, NOW)
    assert [(item.video_key, item.source_rank) for item in items] == [("BVfake1", 1), ("BVfake2", 2)]
    assert all(item.query == "效率工具" for item in items)


def test_douyin_candidates_do_not_contain_comment_text() -> None:
    dumped = [item.model_dump() for item in parse_douyin_candidates(load("douyin/candidates.json"), None, SOURCE, NOW)]
    assert all("comments" not in item and "text" not in item for item in dumped)


def test_unknown_candidate_shape_closes() -> None:
    with pytest.raises(CandidateShapeChanged):
        parse_bilibili_candidates({"data": {"unknown": []}}, "query", SOURCE, NOW)
```

- [x] **Step 2: Run test to verify it fails**

Run: `uv run --project packages/collector pytest packages/collector/tests/candidates/test_adapters.py -q`

Expected: candidate adapter import fails.

- [x] **Step 3: Write minimal implementation**

Recognize only the candidate arrays observed in the artificial fixtures. Map stable video key, public URL, title, reported comment count and current page rank. Reject missing key/title and unknown successful shapes. Never traverse or retain payload keys named `comments`, `replies`, `comment_list`, or `reply_list`.

```python
FORBIDDEN_CANDIDATE_KEYS = frozenset({"comments", "replies", "comment_list", "reply_list"})


def reject_comment_payload(value: object) -> None:
    if isinstance(value, Mapping):
        if any(str(key).lower() in FORBIDDEN_CANDIDATE_KEYS for key in value):
            raise CandidateContainsComments("candidate_payload_contains_comments")
        for child in value.values():
            reject_comment_payload(child)
    elif isinstance(value, list):
        for child in value:
            reject_comment_payload(child)
```

- [x] **Step 4: Run adapter tests**

Run: `uv run --project packages/collector pytest packages/collector/tests/candidates/test_adapters.py -q`

Expected: both platforms parse and forbidden comment payloads close.

- [x] **Step 5: Commit**

```bash
git add packages/collector/src/requirementseeker_collector/candidates/adapters.py packages/collector/tests/candidates/test_adapters.py packages/collector/tests/fixtures
git commit -m "feat(collector): parse public candidate listings"
```

### Task 3: 实现可审计候选发现运行

**Files:**
- Create: `packages/collector/src/requirementseeker_collector/candidates/discovery.py`
- Create: `packages/collector/tests/candidates/test_discovery.py`

- [ ] **Step 1: Write the failing test**

```python
def test_discovery_deduplicates_and_preserves_first_rank(tmp_path: Path) -> None:
    result = discover(request(), browser=two_pages_with_duplicate(), output_root=tmp_path)
    assert [(item.video_key, item.source_rank) for item in result.candidates] == [("v1", 1), ("v2", 2), ("v3", 2)]


def test_discovery_writes_manifest_but_never_calls_batch(tmp_path: Path) -> None:
    batch = Mock()
    result = discover(request(), browser=one_page(), output_root=tmp_path, batch_runner=batch)
    assert result.manifest_path.exists()
    batch.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project packages/collector pytest packages/collector/tests/candidates/test_discovery.py -q`

Expected: discovery import fails.

- [ ] **Step 3: Write minimal implementation**

`DiscoveryRequest` requires exactly one of query or source URL, plus platform and direction. Open a visible page through `BrowserSession`; default to ephemeral Chromium and allow only the existing explicit dedicated `chrome|edge + reuse_login` combinations. Collect supported candidate responses or metadata DOM rows, stop at requested page/result cap, deduplicate by `(platform, video_key)` while preserving first discovery, and write `candidates/<run-id>/manifest.json` plus `discovery.json` through safe audit utilities.

```python
def deduplicate_candidates(items: Iterable[CandidateVideo]) -> list[CandidateVideo]:
    result: list[CandidateVideo] = []
    seen: set[tuple[Platform, str]] = set()
    for item in items:
        key = (item.platform, item.video_key)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result
```

- [ ] **Step 4: Run discovery tests**

Run: `uv run --project packages/collector pytest packages/collector/tests/candidates/test_discovery.py -q && uv run --project packages/collector ruff check packages/collector/src packages/collector/tests`

Expected: deterministic discovery and no-auto-batch tests pass.

- [ ] **Step 5: Commit**

```bash
git add packages/collector/src/requirementseeker_collector/candidates/discovery.py packages/collector/tests/candidates/test_discovery.py
git commit -m "feat(collector): add audited candidate discovery"
```

### Task 4: 实现 24 视频覆盖验证与建议

**Files:**
- Create: `packages/collector/src/requirementseeker_collector/candidates/coverage.py`
- Create: `packages/collector/tests/candidates/test_coverage.py`

- [ ] **Step 1: Write the failing test**

```python
def test_complete_plan_has_no_gaps() -> None:
    report = validate_coverage(complete_manifest())
    assert report.valid is True
    assert report.gaps == []


@pytest.mark.parametrize(
    ("mutation", "code"),
    [(remove_video, "video_total"), (move_direction, "direction_distribution"), (move_scale, "scale_distribution"), (drop_platform, "platform_distribution")],
)
def test_coverage_reports_exact_gap(mutation: Callable[[CollectionManifest], CollectionManifest], code: str) -> None:
    report = validate_coverage(mutation(complete_manifest()))
    assert code in [gap.code for gap in report.gaps]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project packages/collector pytest packages/collector/tests/candidates/test_coverage.py -q`

Expected: coverage import fails.

- [ ] **Step 3: Write minimal implementation**

Validate exact targets: 24 total; platform 12/12 unless a missing platform is explicitly listed; direction counts 6/6/4/4/4; scale counts 8/8/8. If a platform is listed unavailable, report `platform_unavailable` and keep `valid=False`; never redistribute its quota. Suggestions select unchosen candidates by missing direction, then missing scale, then source rank and video key; they do not mutate the manifest.

```python
DIRECTION_TARGETS = {"software_tools": 6, "tutorial_workflow": 6, "life_services": 4, "entertainment_culture": 4, "ecommerce_marketing": 4}
SCALE_TARGETS = {"up_to_200": 8, "201_to_2000": 8, "over_2000": 8}
PLATFORM_TARGETS = {"bilibili": 12, "douyin": 12}
```

- [ ] **Step 4: Run coverage tests**

Run: `uv run --project packages/collector pytest packages/collector/tests/candidates/test_coverage.py -q`

Expected: all exact gap reports pass.

- [ ] **Step 5: Commit**

```bash
git add packages/collector/src/requirementseeker_collector/candidates/coverage.py packages/collector/tests/candidates/test_coverage.py
git commit -m "feat(collector): validate pilot coverage plan"
```

### Task 5: 接入 CLI 并验证不自动批量

**Files:**
- Modify: `packages/collector/src/requirementseeker_collector/cli.py`
- Modify: `packages/collector/README.md`
- Create: `packages/collector/tests/candidates/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
def test_discover_requires_exactly_one_source() -> None:
    assert main(["discover", "--platform", "bilibili", "--direction", "software_tools"]) == 2


def test_validate_plan_returns_nonzero_for_gap(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write_manifest(tmp_path, incomplete_manifest())
    assert main(["validate-plan", str(path)]) == 1
    assert '"valid": false' in capsys.readouterr().out.lower()


def test_discover_does_not_expose_batch_confirmation_flag(parser: ArgumentParser) -> None:
    assert "--auto-batch" not in parser.format_help()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project packages/collector pytest packages/collector/tests/candidates/test_cli.py -q`

Expected: commands are not registered.

- [ ] **Step 3: Write minimal implementation**

Add `discover` with mutually exclusive required `--query`/`--source-url`, required platform/direction, bounded `--max-results` default 50, and the same validated `--browser`/`--reuse-login` options as `pilot`. Add `validate-plan PATH`; output ASCII summary JSON and return 0 only when coverage is complete. README shows candidate review as a distinct manual step before `batch`.

- [ ] **Step 4: Run CLI and complete collector gate**

Run: `uv run --project packages/collector pytest packages/collector/tests -q && uv run --project packages/collector ruff check packages/collector && uv run --project packages/collector ruff format --check packages/collector && uv run --project packages/collector mypy packages/collector/src && uv lock --project packages/collector --check && git diff --check`

Expected: all collector tests pass; no CLI path can chain discovery into batch.

- [ ] **Step 5: Commit**

```bash
git add packages/collector
git commit -m "feat(collector): expose candidate discovery and coverage CLI"
```

### Task 6: 生成真实候选清单并等待人工确认

**Files:**
- Local only: `.local-data/m2-real/candidates/`
- Local only: `docs/execution/2026-09-09.md`

- [ ] **Step 1: Collect candidates from user-confirmed public queries or source pages**

Run one headed `discover` command per confirmed platform/direction source. Record exact commands and outcomes in the local execution log; keep screenshots and discovery reports under `.local-data/`.

The user-confirmed queries are `AI工具推荐`, `AI教程工作流`, `AI生活助手`, `AI娱乐创作`, and `AI电商营销`, mapped in order to the five required directions. Use the dedicated Chrome profile for each platform during real discovery.

- [ ] **Step 2: Assemble a 24-video manifest from discovered candidates**

Use only explicit candidate IDs/URLs and preserve discovery provenance. Do not infer a missing platform by copying another platform's entries.

- [ ] **Step 3: Validate the manifest**

Run: `uv run --project packages/collector rs-collect validate-plan .local-data/m2-real/candidates/approved-manifest.json`

Expected: exit 0 only with exact 24, 12/12 platform, 6/6/4/4/4 direction and 8/8/8 scale coverage. Otherwise keep the precise gap report.

- [ ] **Step 4: Open manifest and discovery report in visible VS Code**

Show sources, ranks, reported comment counts, planned scale buckets and all unavailable fields.

- [ ] **Step 5: Ask for explicit approval before batch**

Do not execute `rs-collect batch` until the user approves the displayed manifest.
