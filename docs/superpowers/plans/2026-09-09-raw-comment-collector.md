# 双平台原始评论采集器实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付独立、可审计的 `rs-collect pilot` 与 `rs-collect batch`，通过可见临时 Playwright 浏览器采集抖音和 Bilibili 视频元数据及评论，并安全写入本地原始数据目录。

**Architecture:** `packages/collector` 使用严格 Pydantic 契约隔离平台载荷与文件输出；平台适配器只做纯解析，浏览器层只负责页面动作和响应正文转交。采集规划、幂等合并、目录级提交、挑战审计和运行审计分别实现，真实平台探测只补充经离线测试固定的适配器形状。

**Tech Stack:** Python 3.12、Pydantic 2、Playwright Python、pytest、jsonschema、Ruff、mypy strict、uv、Hatchling。

---

## 文件职责图

| 路径 | 职责 |
|---|---|
| `packages/collector/pyproject.toml` | 独立包、CLI、依赖与质量门。 |
| `packages/collector/src/requirementseeker_collector/contracts.py` | 原始视频、评论、采集记录与批量清单的严格契约。 |
| `packages/collector/src/requirementseeker_collector/planning.py` | 数量公式、最大余数配额、停止状态。 |
| `packages/collector/src/requirementseeker_collector/merge.py` | 评论 ID 幂等合并与冲突报告。 |
| `packages/collector/src/requirementseeker_collector/artifacts.py` | JSON/JSONL 校验、暂存、目录提交和中断恢复。 |
| `packages/collector/src/requirementseeker_collector/audit.py` | 安全运行事件与敏感值拒绝。 |
| `packages/collector/src/requirementseeker_collector/adapters/*.py` | Bilibili、抖音响应识别和纯载荷解析。 |
| `packages/collector/src/requirementseeker_collector/browser.py` | 非持久可见浏览器会话和页面动作。 |
| `packages/collector/src/requirementseeker_collector/challenges.py` | 用户监督挑战、截图和一次鼠标模拟。 |
| `packages/collector/src/requirementseeker_collector/runner.py` | 单视频及跨平台批量编排。 |
| `packages/collector/src/requirementseeker_collector/cli.py` | `pilot` 与 `batch` 命令。 |
| `packages/collector/schemas/*.schema.json` | 从契约导出的固定 JSON Schema。 |
| `packages/collector/tests/` | 纯单元、模拟页面集成、CLI 与安全测试。 |

### Task 1: 建立独立包与严格输出契约

**Files:**
- Create: `packages/collector/pyproject.toml`
- Create: `packages/collector/src/requirementseeker_collector/__init__.py`
- Create: `packages/collector/src/requirementseeker_collector/contracts.py`
- Create: `packages/collector/src/requirementseeker_collector/schema.py`
- Create: `packages/collector/tests/contract/test_contracts.py`
- Create: `packages/collector/tests/contract/test_schema.py`

- [ ] **Step 1: Write the failing test**

```python
def test_video_rejects_unknown_fields() -> None:
    data = video_data()
    data["cookie"] = "secret"
    with pytest.raises(ValidationError):
        RawVideo.model_validate(data)


def test_comment_keeps_missing_values_explicit() -> None:
    comment = RawComment.model_validate(comment_data(raw_author_id=None, published_at=None))
    assert comment.raw_author_id is None
    assert comment.published_at is None


def test_collection_rejects_inverted_times() -> None:
    with pytest.raises(ValidationError, match="collection_finished_before_started"):
        CollectionRecord.model_validate(collection_data(
            collection_started_at="2026-09-09T02:00:00Z",
            collection_finished_at="2026-09-09T01:00:00Z",
        ))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project packages/collector pytest packages/collector/tests/contract/test_contracts.py -q`

Expected: collection fails because `requirementseeker_collector.contracts` does not exist.

- [ ] **Step 3: Write minimal implementation**

Create a Hatchling project matching `packages/agent`, with dependencies `pydantic>=2.12,<3` and `playwright>=1.55,<2`, and dev dependencies `pytest`, `jsonschema`, `ruff`, and `mypy`. Define `Contract` with `extra="forbid"`, strict non-negative integers, RFC 3339 timezone validation, and these public models:

```python
Platform = Literal["bilibili", "douyin"]
Stratum = Literal["top", "recent", "replies", "long_tail"]


class RawVideo(Contract):
    platform: Platform
    raw_video_id: Identifier
    raw_author_id: Identifier
    title: str
    description: str
    published_at: Timestamp | None
    duration_seconds: NonNegativeInt | None
    total_comment_count: NonNegativeInt | None
    view_count: NonNegativeInt | None
    like_count: NonNegativeInt | None
    favorite_count: NonNegativeInt | None
    share_count: NonNegativeInt | None
    author_follower_count: NonNegativeInt | None
    captured_at: Timestamp


class RawComment(Contract):
    raw_comment_id: Identifier
    raw_author_id: Identifier | None
    raw_parent_comment_id: Identifier | None
    text: Annotated[str, Field(min_length=1, pattern=r"\S")]
    published_at: Timestamp | None
    collected_at: Timestamp
    like_count: NonNegativeInt | None
    reply_count: NonNegativeInt | None
    is_video_author: bool | None
    source_stratum: Stratum
    source_page_or_rank: PositiveInt


class CollectionError(Contract):
    category: Identifier
    occurred_at: Timestamp
    stage: Identifier
    description: Annotated[str, Field(min_length=1, max_length=500)]
    raw_comment_id: Identifier | None = None
    conflict_fields: list[Literal["raw_author_id", "text"]] = Field(default_factory=list)


class CollectionRecord(Contract):
    reported_total: NonNegativeInt | None
    collected_total: NonNegativeInt
    pages_requested: NonNegativeInt
    pages_succeeded: NonNegativeInt
    sort_modes: list[Identifier]
    collection_started_at: Timestamp
    collection_finished_at: Timestamp
    collection_errors: list[CollectionError]

    @model_validator(mode="after")
    def integrity(self) -> Self:
        if self.pages_succeeded > self.pages_requested:
            raise ValueError("pages_succeeded_exceeds_requested")
        if self.collection_finished_at < self.collection_started_at:
            raise ValueError("collection_finished_before_started")
        return self


Direction = Literal["software_tools", "tutorial_workflow", "life_services", "entertainment_culture", "ecommerce_marketing"]
CommentScale = Literal["up_to_200", "201_to_2000", "over_2000"]


class ManifestVideo(Contract):
    platform: Platform
    video_key: Identifier
    url: PublicHttpUrl
    direction: Direction
    comment_scale: CommentScale


class CollectionManifest(Contract):
    manifest_version: Literal["1.0"]
    videos: list[ManifestVideo]
    unavailable_platforms: list[Platform]

    @model_validator(mode="after")
    def unique_keys(self) -> Self:
        keys = [(item.platform, item.video_key) for item in self.videos]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate_platform_video_key")
        return self
```

`PublicHttpUrl` accepts HTTPS only, rejects username/password, and validates Bilibili/Douyin host consistency in `ManifestVideo`. Export schemas with `model_json_schema()` and check in `video.schema.json`, `comment.schema.json`, `collection.schema.json`, and `collection-manifest.schema.json`.

- [ ] **Step 4: Run tests and static checks**

Run: `uv sync --project packages/collector && uv run --project packages/collector pytest packages/collector/tests/contract -q && uv run --project packages/collector ruff check packages/collector && uv run --project packages/collector mypy packages/collector/src`

Expected: contract tests pass; Ruff and mypy report no errors.

- [ ] **Step 5: Commit**

```bash
git add packages/collector
git commit -m "feat(collector): add strict raw data contracts"
```

### Task 2: 实现数量公式与确定性分层

**Files:**
- Create: `packages/collector/src/requirementseeker_collector/planning.py`
- Create: `packages/collector/tests/test_planning.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.parametrize(("total", "expected"), [(0, 0), (200, 200), (201, 200), (2000, 500), (2001, 500), (10000, 1000)])
def test_collection_target_boundaries(total: int, expected: int) -> None:
    assert collection_target(total).target == expected


def test_unknown_total_uses_audited_pilot_cap() -> None:
    assert collection_target(None) == TargetDecision(200, False, "reported_total_unavailable")


def test_quota_rounding_is_deterministic() -> None:
    assert allocate_quotas(7) == {"top": 3, "recent": 2, "replies": 1, "long_tail": 1}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project packages/collector pytest packages/collector/tests/test_planning.py -q`

Expected: import fails because `planning.py` is absent.

- [ ] **Step 3: Write minimal implementation**

```python
WEIGHTS = {"top": 35, "recent": 25, "replies": 20, "long_tail": 20}
FILL_ORDER = ("long_tail", "recent", "top", "replies")


@dataclass(frozen=True)
class TargetDecision:
    target: int
    complete_claim_allowed: bool
    reason: str | None


def collection_target(total: int | None) -> TargetDecision:
    if total is None:
        return TargetDecision(200, False, "reported_total_unavailable")
    if total <= 200:
        return TargetDecision(total, True, None)
    if total <= 2000:
        return TargetDecision(min(500, max(200, ceil(total * 0.25))), True, None)
    return TargetDecision(min(1000, max(500, ceil(10 * sqrt(total)))), True, None)


def allocate_quotas(target: int) -> dict[Stratum, int]:
    floors = {name: target * weight // 100 for name, weight in WEIGHTS.items()}
    remaining = target - sum(floors.values())
    ranked = sorted(WEIGHTS, key=lambda name: (-(target * WEIGHTS[name] % 100), name))
    for name in ranked[:remaining]:
        floors[name] += 1
    return cast(dict[Stratum, int], floors)
```

Add `select_comments(candidates, target)` that visits strata in `top,recent,replies,long_tail` order, sorts each by `source_page_or_rank` then ID, keeps one ID, and fills shortages in `FILL_ORDER` without relabeling the first accepted stratum.

- [ ] **Step 4: Run tests and static checks**

Run: `uv run --project packages/collector pytest packages/collector/tests/test_planning.py -q && uv run --project packages/collector ruff check packages/collector/src packages/collector/tests && uv run --project packages/collector mypy packages/collector/src`

Expected: planning tests pass; quality checks pass.

- [ ] **Step 5: Commit**

```bash
git add packages/collector/src/requirementseeker_collector/planning.py packages/collector/tests/test_planning.py
git commit -m "feat(collector): add deterministic collection planning"
```

### Task 3: 实现幂等合并与冲突关闭式失败

**Files:**
- Create: `packages/collector/src/requirementseeker_collector/merge.py`
- Create: `packages/collector/tests/test_merge.py`

- [ ] **Step 1: Write the failing test**

```python
def test_same_comment_is_updated_once() -> None:
    result = merge_comments([comment("c1", like_count=1)], [comment("c1", like_count=3)])
    assert [item.raw_comment_id for item in result.comments] == ["c1"]
    assert result.comments[0].like_count == 3


@pytest.mark.parametrize("field", ["raw_author_id", "text"])
def test_identity_conflict_keeps_previous(field: str) -> None:
    old = comment("c1")
    new = old.model_copy(update={field: "changed"})
    result = merge_comments([old], [new])
    assert result.comments == [old]
    assert result.conflicts[0].conflict_fields == [field]


def test_conflict_inside_one_run_rejects_generation() -> None:
    with pytest.raises(CurrentRunConflict, match="c1"):
        merge_current_run([comment("c1"), comment("c1", text="changed")])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project packages/collector pytest packages/collector/tests/test_merge.py -q`

Expected: import fails because `merge.py` is absent.

- [ ] **Step 3: Write minimal implementation**

Implement `MergeResult(comments, conflicts, exact_duplicate_count)`, compare only `raw_author_id` and `text` for immutable identity conflicts, update mutable metadata from a consistent rerun, and sort output by `raw_comment_id`. Identical duplicate records increment `exact_duplicate_count`; the runner adds that many `exact_duplicate_merged` audit entries without copying content. Conflicting duplicates raise `CurrentRunConflict` before files are written.

```python
def conflict_fields(old: RawComment, new: RawComment) -> list[ConflictField]:
    return [name for name in ("raw_author_id", "text") if getattr(old, name) != getattr(new, name)]


def merge_comments(previous: Sequence[RawComment], current: Sequence[RawComment]) -> MergeResult:
    merged = {item.raw_comment_id: item for item in previous}
    conflicts: list[MergeConflict] = []
    for item in merge_current_run(current):
        old = merged.get(item.raw_comment_id)
        fields = conflict_fields(old, item) if old is not None else []
        if fields:
            conflicts.append(MergeConflict(item.raw_comment_id, fields))
        else:
            merged[item.raw_comment_id] = item
    return MergeResult(sorted(merged.values(), key=attrgetter("raw_comment_id")), conflicts)
```

- [ ] **Step 4: Run tests**

Run: `uv run --project packages/collector pytest packages/collector/tests/test_merge.py -q`

Expected: all merge and conflict cases pass.

- [ ] **Step 5: Commit**

```bash
git add packages/collector/src/requirementseeker_collector/merge.py packages/collector/tests/test_merge.py
git commit -m "feat(collector): add idempotent comment merging"
```

### Task 4: 实现安全审计与目录级提交恢复

**Files:**
- Create: `packages/collector/src/requirementseeker_collector/audit.py`
- Create: `packages/collector/src/requirementseeker_collector/artifacts.py`
- Create: `packages/collector/tests/test_audit.py`
- Create: `packages/collector/tests/test_artifacts.py`

- [ ] **Step 1: Write the failing test**

```python
def test_audit_rejects_sensitive_keys(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "run.jsonl")
    with pytest.raises(SensitiveAuditValue, match="cookie"):
        audit.write("response", {"cookie": "secret-marker"})
    assert not (tmp_path / "run.jsonl").exists()


def test_invalid_generation_does_not_replace_valid_result(tmp_path: Path) -> None:
    target = valid_generation(tmp_path, text="old")
    with pytest.raises(ArtifactValidationError):
        commit_generation(invalid_generation(tmp_path), target)
    assert read_comments(target)[0].text == "old"


def test_interrupted_backup_is_recovered(tmp_path: Path) -> None:
    paths = interrupted_commit(tmp_path)
    assert recover_interrupted_commit(paths) is True
    assert paths.target.exists()
    assert not paths.backup.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project packages/collector pytest packages/collector/tests/test_audit.py packages/collector/tests/test_artifacts.py -q`

Expected: imports fail because audit and artifact modules are absent.

- [ ] **Step 3: Write minimal implementation**

`AuditLog.write` recursively rejects case-insensitive key fragments `cookie`, `token`, `authorization`, `password`, `request_headers`, `response_body`, `storage_state`, then appends sorted ASCII JSON. `ArtifactWriter` must write all three files under `.staging`, validate every model plus cross-file invariants, move an existing target to `.backup`, move staging to target, restore backup on failure, and recover a backup with no target on the next invocation.

```python
def commit_generation(paths: CommitPaths) -> None:
    validate_generation(paths.staging)
    paths.backup.parent.mkdir(parents=True, exist_ok=True)
    if paths.target.exists():
        paths.target.replace(paths.backup)
    try:
        paths.staging.replace(paths.target)
    except OSError:
        if paths.backup.exists() and not paths.target.exists():
            paths.backup.replace(paths.target)
        raise


def validate_generation(directory: Path) -> None:
    video = RawVideo.model_validate_json((directory / "video.json").read_text("utf-8"))
    comments = read_jsonl(directory / "comments.jsonl", RawComment)
    collection = CollectionRecord.model_validate_json((directory / "collection.json").read_text("utf-8"))
    if collection.collected_total != len(comments):
        raise ArtifactValidationError("collected_total_mismatch")
    ids = [item.raw_comment_id for item in comments]
    if len(ids) != len(set(ids)):
        raise ArtifactValidationError("duplicate_comment_id")
    if any(item.raw_parent_comment_id == item.raw_comment_id for item in comments):
        raise ArtifactValidationError("self_parent_comment")
    if video.platform not in ("bilibili", "douyin"):
        raise ArtifactValidationError("invalid_platform")
```

- [ ] **Step 4: Run tests and static checks**

Run: `uv run --project packages/collector pytest packages/collector/tests/test_audit.py packages/collector/tests/test_artifacts.py -q && uv run --project packages/collector ruff check packages/collector && uv run --project packages/collector mypy packages/collector/src`

Expected: audit and recovery tests pass; no sensitive marker is written.

- [ ] **Step 5: Commit**

```bash
git add packages/collector/src/requirementseeker_collector/audit.py packages/collector/src/requirementseeker_collector/artifacts.py packages/collector/tests/test_audit.py packages/collector/tests/test_artifacts.py
git commit -m "feat(collector): add safe artifact commits and audit"
```

### Task 5: 实现双平台纯响应适配器

**Files:**
- Create: `packages/collector/src/requirementseeker_collector/adapters/__init__.py`
- Create: `packages/collector/src/requirementseeker_collector/adapters/base.py`
- Create: `packages/collector/src/requirementseeker_collector/adapters/bilibili.py`
- Create: `packages/collector/src/requirementseeker_collector/adapters/douyin.py`
- Create: `packages/collector/tests/fixtures/bilibili/video.json`
- Create: `packages/collector/tests/fixtures/bilibili/comments.json`
- Create: `packages/collector/tests/fixtures/douyin/video.json`
- Create: `packages/collector/tests/fixtures/douyin/comments.json`
- Create: `packages/collector/tests/adapters/test_bilibili.py`
- Create: `packages/collector/tests/adapters/test_douyin.py`

- [ ] **Step 1: Write the failing test**

```python
def test_bilibili_parses_top_level_and_reply() -> None:
    page = BilibiliAdapter().parse_comment_response(load_fixture("bilibili/comments.json"), "top", 1, video_author_id="42")
    assert [(item.raw_comment_id, item.raw_parent_comment_id) for item in page.comments] == [("11", None), ("12", "11")]
    assert page.comments[0].is_video_author is True


def test_douyin_parses_nullable_author_and_cursor() -> None:
    page = DouyinAdapter().parse_comment_response(load_fixture("douyin/comments.json"), "recent", 1, video_author_id="author")
    assert page.comments[0].raw_author_id is None
    assert page.has_more is True


@pytest.mark.parametrize("adapter", [BilibiliAdapter(), DouyinAdapter()])
def test_unknown_success_shape_closes(adapter: PlatformAdapter) -> None:
    with pytest.raises(ResponseShapeChanged):
        adapter.parse_comment_response({"unexpected": []}, "top", 1, video_author_id="author")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project packages/collector pytest packages/collector/tests/adapters -q`

Expected: adapter imports fail.

- [ ] **Step 3: Write minimal implementation**

Define a `PlatformAdapter` protocol returning `ParsedVideo` and `ParsedCommentPage`. Recognize only Bilibili `/x/web-interface/view`, `/x/v2/reply/wbi/main`, `/x/v2/reply/reply` and Douyin `/aweme/v1/web/aweme/detail`, `/aweme/v1/web/comment/list`, `/aweme/v1/web/comment/list/reply` response families. Parse artificial fixtures using these exact field mappings:

```python
def bilibili_comment(reply: Mapping[str, Any], stratum: Stratum, rank: int, video_author_id: str, parent: str | None = None) -> RawComment:
    member = mapping(reply.get("member"))
    content = mapping(reply.get("content"))
    author_id = optional_identifier(member.get("mid"))
    return RawComment(
        raw_comment_id=str(required(reply, "rpid")),
        raw_author_id=author_id,
        raw_parent_comment_id=parent,
        text=str(required(content, "message")),
        published_at=from_unix(reply.get("ctime")),
        collected_at=utc_now(),
        like_count=optional_int(reply.get("like")),
        reply_count=optional_int(reply.get("rcount")),
        is_video_author=(author_id == video_author_id) if author_id is not None else None,
        source_stratum=stratum,
        source_page_or_rank=rank,
    )


def douyin_comment(comment: Mapping[str, Any], stratum: Stratum, rank: int, video_author_id: str, parent: str | None = None) -> RawComment:
    user = mapping(comment.get("user"))
    author_id = optional_identifier(user.get("sec_uid") or user.get("uid"))
    return RawComment(
        raw_comment_id=str(required(comment, "cid")),
        raw_author_id=author_id,
        raw_parent_comment_id=parent or optional_identifier(comment.get("reply_id")),
        text=str(required(comment, "text")),
        published_at=from_unix(comment.get("create_time")),
        collected_at=utc_now(),
        like_count=optional_int(comment.get("digg_count")),
        reply_count=optional_int(comment.get("reply_comment_total")),
        is_video_author=(author_id == video_author_id) if author_id is not None else None,
        source_stratum=stratum,
        source_page_or_rank=rank,
    )
```

Nested Bilibili replies use the containing top-level `rpid`; Douyin reply responses use the requested parent ID. The parser receives the validated video author ID and deterministically fills `is_video_author` for known authors. Missing required ID/text raises `ResponseShapeChanged`; optional unavailable values remain `None`.

- [ ] **Step 4: Run adapter tests**

Run: `uv run --project packages/collector pytest packages/collector/tests/adapters -q`

Expected: both fixture suites pass, including unknown shape rejection.

- [ ] **Step 5: Commit**

```bash
git add packages/collector/src/requirementseeker_collector/adapters packages/collector/tests/adapters packages/collector/tests/fixtures
git commit -m "feat(collector): add bilibili and douyin response adapters"
```

### Task 6: 实现临时可见浏览器与监督式挑战处理

**Files:**
- Create: `packages/collector/src/requirementseeker_collector/browser.py`
- Create: `packages/collector/src/requirementseeker_collector/challenges.py`
- Create: `packages/collector/tests/test_browser.py`
- Create: `packages/collector/tests/test_challenges.py`

- [ ] **Step 1: Write the failing test**

```python
def test_browser_launch_is_headed_and_not_persistent(fake_playwright: FakePlaywright) -> None:
    with BrowserSession(fake_playwright) as session:
        assert session.page is fake_playwright.page
    assert fake_playwright.launch_kwargs == {"headless": False}
    assert fake_playwright.persistent_context_calls == 0


@pytest.mark.parametrize(("stratum", "label"), [("top", "最热"), ("recent", "最新"), ("replies", "展开 2 条回复")])
def test_stratum_action_uses_visible_controls(stratum: Stratum, label: str) -> None:
    page = fake_page_with_control(label)
    perform_stratum_action(page, stratum)
    assert page.clicked_labels == [label]


def test_challenge_requires_live_confirmation(tmp_path: Path) -> None:
    page = fake_challenge_page()
    handler = ChallengeHandler(tmp_path, confirm=lambda: False)
    result = handler.attempt(page, DragAction((10, 20), (80, 20), 750))
    assert result.status == "not_confirmed"
    assert page.mouse.drags == []


def test_challenge_attempts_once_and_writes_safe_audit(tmp_path: Path) -> None:
    page = fake_challenge_page()
    handler = ChallengeHandler(tmp_path, confirm=lambda: True)
    assert handler.attempt(page, DragAction((10, 20), (80, 20), 750)).status == "attempted"
    assert len(page.mouse.drags) == 1
    assert not hasattr(handler, "retry")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project packages/collector pytest packages/collector/tests/test_browser.py packages/collector/tests/test_challenges.py -q`

Expected: browser and challenge imports fail.

- [ ] **Step 3: Write minimal implementation**

Use `sync_playwright`; call only `browser_type.launch(headless=False)` and `browser.new_context()`, never `launch_persistent_context`, `storage_state`, request headers, cookies, or local user-data directories. Register response callbacks before `page.goto` and pass only `response.json()` payload plus URL to the adapter. `perform_stratum_action` uses visible accessible labels for `最热|热门`, `最新|按时间`, and `展开|查看 ... 回复`; a missing control records that stratum as unavailable instead of guessing a hidden selector. `long_tail` advances the actual stable page/scroll order and never claims random selection.

`ChallengeHandler.attempt` masks password inputs with DOM styling before page-only screenshots, writes `before.png`, performs one visible `DragAction` or `ClickAction`, writes `after.png`, and appends an action object containing only time, action type, coordinates or element category, duration and result. Confirmation is a blocking CLI prompt that requires exact `yes`; browser keyboard events are never observed.

- [ ] **Step 4: Run browser unit tests**

Run: `uv run --project packages/collector pytest packages/collector/tests/test_browser.py packages/collector/tests/test_challenges.py -q && uv run --project packages/collector mypy packages/collector/src`

Expected: headed/ephemeral and single-attempt guarantees pass.

- [ ] **Step 5: Commit**

```bash
git add packages/collector/src/requirementseeker_collector/browser.py packages/collector/src/requirementseeker_collector/challenges.py packages/collector/tests/test_browser.py packages/collector/tests/test_challenges.py
git commit -m "feat(collector): add supervised headed browser session"
```

### Task 7: 编排单视频与平台隔离批量运行

**Files:**
- Create: `packages/collector/src/requirementseeker_collector/runner.py`
- Create: `packages/collector/src/requirementseeker_collector/cli.py`
- Create: `packages/collector/tests/test_runner.py`
- Create: `packages/collector/tests/test_cli.py`
- Create: `packages/collector/README.md`

- [ ] **Step 1: Write the failing test**

```python
def test_pilot_writes_three_valid_files(tmp_path: Path) -> None:
    result = run_pilot(pilot_request(), fake_browser_result(), output_root=tmp_path)
    target = tmp_path / "raw" / "bilibili" / "BVfake"
    assert result.status == "success"
    assert sorted(path.name for path in target.iterdir()) == ["collection.json", "comments.jsonl", "video.json"]


def test_batch_stops_only_failed_platform(tmp_path: Path) -> None:
    result = run_batch(mixed_manifest(), collector=fails_bilibili_after_first(), output_root=tmp_path)
    assert result.platforms["bilibili"].status == "stopped"
    assert result.platforms["douyin"].videos_succeeded == 2


def test_cli_rejects_source_url_credentials_without_echo(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["pilot", "--platform", "bilibili", "--url", "https://user:pass@example.invalid/video"]) == 2
    assert "pass" not in capsys.readouterr().out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project packages/collector pytest packages/collector/tests/test_runner.py packages/collector/tests/test_cli.py -q`

Expected: runner and CLI imports fail.

- [ ] **Step 3: Write minimal implementation**

Add the `rs-collect` entry point. `pilot` accepts platform, public URL, optional video key and `--output-root` defaulting to `.local-data/m2-real`. `batch` reads a versioned manifest and processes entries in stable file order. Both emit only compact summary JSON. `run_pilot` computes target, gathers supported strata, merges prior valid comments, records conflicts, and commits artifacts. Fatal platform states are `login_failed`, `challenge_unresolved`, `access_restricted`, and `response_shape_changed`; batch skips later videos only for the same platform.

```python
def run_batch(manifest: CollectionManifest, collector: VideoCollector, output_root: Path) -> BatchResult:
    stopped: set[Platform] = set()
    results: list[PilotResult] = []
    for item in manifest.videos:
        if item.platform in stopped:
            results.append(PilotResult.skipped(item, "platform_stopped"))
            continue
        result = collector.collect(item, output_root)
        results.append(result)
        if result.status in FATAL_PLATFORM_STATUSES:
            stopped.add(item.platform)
    return BatchResult.from_results(results)
```

README must state visible login, ephemeral state, local output, challenge supervision, supported response families, commands, and the two-platform pilot gate.

- [ ] **Step 4: Run CLI tests and build**

Run: `uv run --project packages/collector pytest packages/collector/tests/test_runner.py packages/collector/tests/test_cli.py -q && uv build --project packages/collector`

Expected: runner/CLI tests pass and wheel contains `requirementseeker_collector` plus `rs-collect` metadata.

- [ ] **Step 5: Commit**

```bash
git add packages/collector
git commit -m "feat(collector): add pilot and isolated batch commands"
```

### Task 8: 用本地模拟页面完成 Playwright 集成门禁

**Files:**
- Create: `packages/collector/tests/integration/site/index.html`
- Create: `packages/collector/tests/integration/test_playwright_collection.py`
- Modify: `packages/collector/src/requirementseeker_collector/runner.py`
- Modify: `packages/collector/README.md`

- [ ] **Step 1: Write the failing integration test**

```python
def test_page_flow_collects_response_and_dom_metadata(local_site: str, tmp_path: Path) -> None:
    result = collect_from_page(local_site, BilibiliAdapter(), output_root=tmp_path, headless=True)
    assert result.video.title == "Synthetic Video"
    assert [item.raw_comment_id for item in result.comments] == ["11", "12"]
    assert result.collection.pages_requested == 1


def test_page_flow_records_unavailable_sort_and_keeps_actual_order(local_site_without_recent: str, tmp_path: Path) -> None:
    result = collect_from_page(local_site_without_recent, BilibiliAdapter(), output_root=tmp_path, headless=True)
    assert "recent" not in result.collection.sort_modes
    assert any(error.category == "stratum_unavailable" for error in result.collection.collection_errors)


def test_unknown_response_shape_stops_without_commit(local_unknown_site: str, tmp_path: Path) -> None:
    with pytest.raises(ResponseShapeChanged):
        collect_from_page(local_unknown_site, BilibiliAdapter(), output_root=tmp_path, headless=True)
    assert not (tmp_path / "raw").exists()
```

- [ ] **Step 2: Run test to verify it fails for missing page driver behavior**

Run: `uv run --project packages/collector playwright install chromium && uv run --project packages/collector pytest packages/collector/tests/integration/test_playwright_collection.py -q`

Expected: tests fail because the runner does not yet drive the synthetic scroll/response flow.

- [ ] **Step 3: Implement the minimal page driver behavior**

Serve the checked-in synthetic HTML from a pytest local HTTP server. It exposes title/description meta tags, emits one artificial supported JSON response after scroll, and has a deterministic end marker. Extend the runner to scroll until target or end marker, wait for response parsing, read only metadata DOM fields, and stop on an unknown successful response shape.

```python
while len(collected) < target and not adapter.page_exhausted(page):
    pages_requested += 1
    page.mouse.wheel(0, 900)
    page.wait_for_timeout(250)
    pages_succeeded += drain_parsed_pages(response_queue, collected)
```

- [ ] **Step 4: Run the complete package gate**

Run: `uv run --project packages/collector pytest packages/collector/tests -q && uv run --project packages/collector ruff check packages/collector && uv run --project packages/collector ruff format --check packages/collector && uv run --project packages/collector mypy packages/collector/src && uv lock --project packages/collector --check && uv build --project packages/collector && git diff --check`

Expected: all tests and checks pass with no warnings or tracked local data.

- [ ] **Step 5: Commit**

```bash
git add packages/collector/tests/integration packages/collector/README.md packages/collector/src/requirementseeker_collector/runner.py
git commit -m "test(collector): verify browser collection flow"
```

### Task 9: 执行双平台可见试点并固定真实结构变化

**Files:**
- Modify if observed shape differs: `packages/collector/src/requirementseeker_collector/adapters/bilibili.py`
- Modify if observed shape differs: `packages/collector/src/requirementseeker_collector/adapters/douyin.py`
- Modify if observed shape differs: `packages/collector/tests/adapters/test_bilibili.py`
- Modify if observed shape differs: `packages/collector/tests/adapters/test_douyin.py`
- Local only: `.local-data/m2-real/`
- Local only: `docs/execution/2026-09-09.md`

- [ ] **Step 1: Ask the user to confirm one public Bilibili URL and run the headed pilot**

Construct `uv run --project packages/collector rs-collect pilot --platform bilibili --url URL` with `URL` taken verbatim from the active user response. Do not place the real URL in a tracked file; record the executed command with its URL reduced to the video key in the local execution log.

Expected: a visible browser opens; login and challenge actions remain supervised; the run either produces three valid files or a structured closed failure.

- [ ] **Step 2: If a real response shape differs, first add a sanitized structural fixture and failing test**

Store only artificial field-compatible values in the tracked fixture. Keep the real response and screenshots under `.local-data/`. Run the one adapter test and confirm `ResponseShapeChanged` before changing the parser.

- [ ] **Step 3: Implement only the observed compatible mapping and rerun the Bilibili pilot**

Expected: schema-valid `video.json`, `comments.jsonl`, `collection.json`, plus local run audit.

- [ ] **Step 4: Repeat Steps 1–3 for one user-confirmed public Douyin URL**

Construct the same command with `--platform douyin` and the exact URL from the active user response; keep the full URL out of tracked files.

Expected: schema-valid Douyin sample or a precise stopped state requiring user action.

- [ ] **Step 5: Show samples and gate batch execution**

Open both platforms' `video.json`, `comments.jsonl`, `collection.json`, and run audits in visible VS Code. Record actual counts, nullable fields, strata, conflicts, challenge activity, and platform changes in `docs/execution/2026-09-09.md`. Do not run `batch` until the user explicitly accepts both samples.
