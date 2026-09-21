# 脱敏标准化与标注材料实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付独立 `rs-dataset` 工具，把通过原始 Schema 校验的双平台数据转换为稳定伪 ID、PII 已处理、按视频稳定拆分且可供人工标注的 M2 数据集。

**Architecture:** `packages/dataset-tools` 只通过文件契约读取采集结果，不导入采集器或 Agent。流水线先验证原始目录，再以环境变量中的秘密完成类型域隔离 HMAC、确定性文本处理、目录级写入和 10/7/7 拆分；标注工具只生成空白模板并验证两份独立标注和裁决，不自动产生语义结论。

**Tech Stack:** Python 3.12、Pydantic 2、pytest、jsonschema、Ruff、mypy strict、uv、Hatchling、Python 标准库 `hmac`/`hashlib`/`unicodedata`。

---

## 前置门禁

开始本计划前，原始采集器必须产生至少两个通过 Schema 的本地试点目录。测试只使用人工 fixture，真实原始数据继续留在 `.local-data/`。

### 2026-09-21 真实批次输入修订

原始采集阶段已完成，验收记录见 `docs/development/2026-09-21-comment-collection-acceptance.md`。批准清单包含 24 个视频和 5,090 条评论；`.local-data/m2-real/raw/` 另有 2 个早期试点目录。

数据工具必须以 `approved-manifest.json` 的 24 个 `(platform, video_key)` 为严格允许列表，而不是枚举并处理 `raw/` 下的全部目录。清单内目录缺失、平台或键不匹配时关闭式失败；清单外目录不进入输出，只在安全摘要中记录排除数量。测试需覆盖“24 个批准目录加 2 个试点目录仍只输出 24 个视频”，且不得在摘要中写出被排除目录的原始键。

实现 Task 1–4 不需要真实秘密。首次运行真实脱敏前，操作者必须通过环境变量提供至少 32 字节的 HMAC 秘密，并保证后续重跑使用同一值；不得在命令行、日志、测试 fixture 或文档中保存秘密值。

## 文件职责图

| 路径 | 职责 |
|---|---|
| `packages/dataset-tools/pyproject.toml` | 独立包、CLI、依赖和质量门。 |
| `packages/dataset-tools/src/requirementseeker_dataset/contracts.py` | 脱敏记录、SamplingManifest、报告、拆分和标注契约。 |
| `packages/dataset-tools/src/requirementseeker_dataset/source.py` | 严格读取并验证原始三文件目录。 |
| `packages/dataset-tools/src/requirementseeker_dataset/identifiers.py` | 类型域隔离稳定 HMAC。 |
| `packages/dataset-tools/src/requirementseeker_dataset/text.py` | Unicode/空白标准化和 PII 规则。 |
| `packages/dataset-tools/src/requirementseeker_dataset/sanitize.py` | 目录转换、摘要、复核项和安全写入。 |
| `packages/dataset-tools/src/requirementseeker_dataset/split.py` | 24 视频稳定 10/7/7 拆分。 |
| `packages/dataset-tools/src/requirementseeker_dataset/labels.py` | 空白模板导出和人工标注校验。 |
| `packages/dataset-tools/src/requirementseeker_dataset/cli.py` | `sanitize`、`export-labels`、`validate-labels`。 |
| `packages/dataset-tools/tests/` | 契约、秘密安全、文本、流水线、拆分和标签测试。 |

### Task 1: 建立独立数据工具包与契约

**Files:**
- Create: `packages/dataset-tools/pyproject.toml`
- Create: `packages/dataset-tools/src/requirementseeker_dataset/__init__.py`
- Create: `packages/dataset-tools/src/requirementseeker_dataset/contracts.py`
- Create: `packages/dataset-tools/tests/test_contracts.py`

- [x] **Step 1: Write the failing test**

```python
def test_sanitization_report_rejects_raw_identifiers() -> None:
    data = report_data()
    data["raw_video_id"] = "raw-1"
    with pytest.raises(ValidationError):
        SanitizationReport.model_validate(data)


def test_label_template_uses_explicit_unlabeled_values() -> None:
    item = CommentLabel.unlabeled("comment_abc")
    assert item.need_signal == "unlabeled"
    assert item.signal_kind == "unlabeled"
    assert item.normalized_need is None
```

- [x] **Step 2: Run test to verify it fails**

Run: `uv run --project packages/dataset-tools pytest packages/dataset-tools/tests/test_contracts.py -q`

Expected: package import fails.

- [x] **Step 3: Write minimal implementation**

Create an independent Hatchling project with only Pydantic runtime dependency. Define strict `SanitizedVideo`, `SanitizedComment`, `SamplingManifest`, `SanitizationReport`, `ReviewItem`, `DatasetSplit`, `CommentLabel`, `ClusterLabel`, `AnnotationFile`, and `AdjudicationFile`. Reuse field names from raw output, but their ID values must match `^(video|author|comment)_[0-9a-f]{32}$`. `SamplingManifest` mirrors the approved M2 1.0 file fields without importing Agent code.

```python
NeedSignal = Literal["yes", "no", "uncertain", "unlabeled"]
SignalKind = Literal["pain", "need", "alternative", "product_defect", "not_applicable", "unlabeled"]
NoiseKind = Literal["none", "praise", "joke", "advertising", "meaningless", "unclear", "other", "unlabeled"]
VideoReception = Literal["positive", "negative", "mixed", "none", "unclear", "unlabeled"]


class CommentLabel(Contract):
    comment_id: PseudonymousCommentId
    need_signal: NeedSignal
    signal_kind: SignalKind
    normalized_need: str | None
    noise_kind: NoiseKind
    video_reception: VideoReception

    @classmethod
    def unlabeled(cls, comment_id: str) -> Self:
        return cls(comment_id=comment_id, need_signal="unlabeled", signal_kind="unlabeled", normalized_need=None, noise_kind="unlabeled", video_reception="unlabeled")


class SamplingManifest(Contract):
    sampling_schema_version: Literal["1.0"]
    manifest_id: Identifier
    platform: Literal["bilibili", "douyin"]
    video_id: PseudonymousVideoId
    captured_at: Timestamp
    reported_total: NonNegativeInt | None
    collection_target: PositiveInt
    collected_total: NonNegativeInt
    pages_requested: PositiveInt
    pages_succeeded: NonNegativeInt
    available_strata: set[Literal["top", "recent", "replies", "long_tail"]]
    direction: Literal["software_tool", "tutorial_workflow", "life_service", "ecommerce_marketing", "entertainment_culture", "unknown"]
    author_id_present: NonNegativeInt
    distinct_author_count: NonNegativeInt
    exact_duplicate_count: NonNegativeInt
    normalized_duplicate_count: NonNegativeInt
    video_metrics: VideoMetrics | None
    candidate_comment_ids: list[PseudonymousCommentId]
    stratum_comment_ids: dict[Literal["top", "recent", "replies", "long_tail"], list[PseudonymousCommentId]]
```

Copy M2's cross-field invariants into this file-contract model and test them: successful pages cannot exceed requested pages; distinct known authors and duplicate counts cannot exceed their totals; candidate IDs and per-stratum IDs are unique; strata keys equal `available_strata`; every stratum ID belongs to the candidate pool.

- [x] **Step 4: Run tests and quality checks**

Run: `uv sync --project packages/dataset-tools && uv run --project packages/dataset-tools pytest packages/dataset-tools/tests/test_contracts.py -q && uv run --project packages/dataset-tools ruff check packages/dataset-tools && uv run --project packages/dataset-tools mypy --config-file packages/dataset-tools/pyproject.toml packages/dataset-tools/src`

Expected: contract tests pass; package has no dependency on collector or Agent.

- [x] **Step 5: Commit**

```bash
git add packages/dataset-tools
git commit -m "feat(dataset): add sanitized and annotation contracts"
```

### Task 2: 严格读取原始目录并拒绝未验证输入

**Files:**
- Create: `packages/dataset-tools/src/requirementseeker_dataset/source.py`
- Create: `packages/dataset-tools/tests/fixtures/raw/valid/`
- Create: `packages/dataset-tools/tests/fixtures/raw/invalid/`
- Create: `packages/dataset-tools/tests/test_source.py`

- [x] **Step 1: Write the failing test**

```python
def test_source_reads_exact_three_file_directory() -> None:
    source = read_raw_video(FIXTURES / "raw/valid/bilibili/BVfake")
    assert source.video.raw_video_id == "BVfake"
    assert source.collection.collected_total == len(source.comments)


@pytest.mark.parametrize("case", ["missing-file", "unknown-field", "duplicate-comment", "count-mismatch", "self-parent"])
def test_invalid_raw_directory_is_rejected(case: str) -> None:
    with pytest.raises(RawDatasetError):
        read_raw_video(FIXTURES / "raw/invalid" / case)
```

- [x] **Step 2: Run test to verify it fails**

Run: `uv run --project packages/dataset-tools pytest packages/dataset-tools/tests/test_source.py -q`

Expected: source reader import fails.

- [x] **Step 3: Write minimal implementation**

Copy the approved raw JSON Schemas into test resources or define equivalent private input models; do not import `packages/collector`. Require exactly `video.json`, `comments.jsonl`, `collection.json`; validate extra fields, counts, unique IDs, parent self-reference, platform directory and filename encoding before returning immutable `RawVideoBundle`.

```python
def read_raw_video(directory: Path) -> RawVideoBundle:
    required = {"video.json", "comments.jsonl", "collection.json"}
    if {item.name for item in directory.iterdir() if item.is_file()} != required:
        raise RawDatasetError("raw_directory_file_set_invalid")
    video = RawVideoInput.model_validate_json((directory / "video.json").read_text("utf-8"))
    comments = tuple(read_comment_lines(directory / "comments.jsonl"))
    collection = CollectionInput.model_validate_json((directory / "collection.json").read_text("utf-8"))
    validate_bundle(video, comments, collection, directory)
    return RawVideoBundle(video, comments, collection)
```

- [x] **Step 4: Run source tests**

Run: `uv run --project packages/dataset-tools pytest packages/dataset-tools/tests/test_source.py -q`

Expected: valid fixture loads; all invalid fixtures fail with specific safe codes.

- [x] **Step 5: Commit**

```bash
git add packages/dataset-tools/src/requirementseeker_dataset/source.py packages/dataset-tools/tests/fixtures packages/dataset-tools/tests/test_source.py
git commit -m "feat(dataset): validate raw collector inputs"
```

### Task 3: 实现稳定 HMAC 且不泄漏映射

**Files:**
- Create: `packages/dataset-tools/src/requirementseeker_dataset/identifiers.py`
- Create: `packages/dataset-tools/tests/test_identifiers.py`

- [x] **Step 1: Write the failing test**

```python
def test_hmac_is_stable_and_type_scoped() -> None:
    ids = IdentifierPseudonymizer(b"local-test-secret-at-least-32-bytes")
    assert ids.video("123") == ids.video("123")
    assert ids.video("123") != ids.comment("123")
    assert ids.author("123").startswith("author_")


def test_secret_is_not_in_repr_or_error() -> None:
    marker = b"never-print-this-secret-value-1234"
    ids = IdentifierPseudonymizer(marker)
    assert marker.decode() not in repr(ids)


def test_short_secret_is_rejected_without_value() -> None:
    marker = b"tiny-secret-value"
    with pytest.raises(SecretConfigurationError, match="dataset_secret_too_short") as error:
        IdentifierPseudonymizer(marker)
    assert marker.decode() not in str(error.value)
```

- [x] **Step 2: Run test to verify it fails**

Run: `uv run --project packages/dataset-tools pytest packages/dataset-tools/tests/test_identifiers.py -q`

Expected: identifier module import fails.

- [x] **Step 3: Write minimal implementation**

```python
class IdentifierPseudonymizer:
    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise SecretConfigurationError("dataset_secret_too_short")
        self._secret = secret

    def _make(self, kind: Literal["video", "author", "comment"], raw_id: str) -> str:
        digest = hmac.new(self._secret, f"{kind}\0{raw_id}".encode(), hashlib.sha256).hexdigest()[:32]
        return f"{kind}_{digest}"

    def video(self, raw_id: str) -> str:
        return self._make("video", raw_id)

    def author(self, raw_id: str) -> str:
        return self._make("author", raw_id)

    def comment(self, raw_id: str) -> str:
        return self._make("comment", raw_id)

    def __repr__(self) -> str:
        return "IdentifierPseudonymizer(<redacted>)"
```

No function returns or writes a raw-to-pseudonymous mapping. Missing environment variable errors mention only the variable name.

- [x] **Step 4: Run identifier tests**

Run: `uv run --project packages/dataset-tools pytest packages/dataset-tools/tests/test_identifiers.py -q`

Expected: stable/type-scoped/security tests pass.

- [x] **Step 5: Commit**

```bash
git add packages/dataset-tools/src/requirementseeker_dataset/identifiers.py packages/dataset-tools/tests/test_identifiers.py
git commit -m "feat(dataset): add stable scoped pseudonymous IDs"
```

### Task 4: 实现确定性文本标准化、PII 替换与复核项

**Files:**
- Create: `packages/dataset-tools/src/requirementseeker_dataset/text.py`
- Create: `packages/dataset-tools/tests/test_text.py`

- [x] **Step 1: Write the failing test**

```python
@pytest.mark.parametrize(
    ("source", "expected"),
    [("联系 13800138000", "联系 [PHONE]"), ("发到 a@example.com", "发到 [EMAIL]"), ("微信 abc_123", "[HANDLE]")],
)
def test_direct_identifiers_are_replaced(source: str, expected: str) -> None:
    assert sanitize_text(source).text == expected


def test_semantic_content_and_internal_spaces_are_preserved() -> None:
    result = sanitize_text("  我需要  一个离线工具\r\n第二行  ")
    assert result.text == "我需要  一个离线工具\n第二行"


def test_ambiguous_address_becomes_review_item_not_deleted() -> None:
    result = sanitize_text("在幸福路 18 号见")
    assert result.text
    assert result.review_reasons == ["possible_precise_address"]


def test_explicit_full_address_and_identity_number_are_replaced() -> None:
    result = sanitize_text("地址：上海市幸福路18号，身份证 110101199001011234")
    assert result.text == "[ADDRESS]，身份证 [IDENTIFIER]"
```

- [x] **Step 2: Run test to verify it fails**

Run: `uv run --project packages/dataset-tools pytest packages/dataset-tools/tests/test_text.py -q`

Expected: text module import fails.

- [x] **Step 3: Write minimal implementation**

Normalize Unicode to NFC, convert CRLF/CR to LF and strip only outer whitespace. Apply ordered compiled patterns for email, mainland phone, mainland identity number, explicitly introduced private handles and addresses introduced by `地址：`. Replace with `[EMAIL]`, `[PHONE]`, `[IDENTIFIER]`, `[HANDLE]`, `[ADDRESS]`; record category replacement counts. Other address-like phrases add `possible_precise_address` review without erasing the text.

```python
EMAIL_PATTERN = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+(?![\w.-])")
PHONE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
IDENTITY_PATTERN = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
PRIVATE_HANDLE_PATTERN = re.compile(r"(?i)(?:微信|wx|vx|v信|qq|私信)[:：\s]*[a-z0-9_-]{5,32}")
EXPLICIT_ADDRESS_PATTERN = re.compile(r"地址[:：]\s*[^，,。\n]{4,80}(?=[，,。\n]|$)")
PRECISE_ADDRESS_CANDIDATE = re.compile(r"[\u4e00-\u9fff]{2,}(?:路|街|巷)\s*\d+号")

RULES = (
    RedactionRule("email", EMAIL_PATTERN, "[EMAIL]"),
    RedactionRule("phone", PHONE_PATTERN, "[PHONE]"),
    RedactionRule("identity", IDENTITY_PATTERN, "[IDENTIFIER]"),
    RedactionRule("handle", PRIVATE_HANDLE_PATTERN, "[HANDLE]"),
    RedactionRule("address", EXPLICIT_ADDRESS_PATTERN, "[ADDRESS]"),
)


def sanitize_text(source: str) -> TextResult:
    text = unicodedata.normalize("NFC", source.replace("\r\n", "\n").replace("\r", "\n")).strip()
    counts: Counter[str] = Counter()
    for rule in RULES:
        text, count = rule.pattern.subn(rule.replacement, text)
        counts[rule.name] += count
    reviews = ["possible_precise_address"] if PRECISE_ADDRESS_CANDIDATE.search(text) else []
    return TextResult(text, dict(counts), reviews)
```

- [x] **Step 4: Run text tests**

Run: `uv run --project packages/dataset-tools pytest packages/dataset-tools/tests/test_text.py -q`

Expected: replacements, preservation and conservative review tests pass.

- [x] **Step 5: Commit**

```bash
git add packages/dataset-tools/src/requirementseeker_dataset/text.py packages/dataset-tools/tests/test_text.py
git commit -m "feat(dataset): redact direct identifiers deterministically"
```

### Task 5: 实现脱敏流水线、摘要与稳定拆分

**Files:**
- Create: `packages/dataset-tools/src/requirementseeker_dataset/sanitize.py`
- Create: `packages/dataset-tools/src/requirementseeker_dataset/split.py`
- Create: `packages/dataset-tools/tests/test_sanitize.py`
- Create: `packages/dataset-tools/tests/test_split.py`

- [x] **Step 1: Write the failing test**

```python
def test_sanitize_writes_no_raw_ids_or_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "local-test-secret-at-least-32-bytes"
    monkeypatch.setenv("RS_DATASET_TEST_SECRET", secret)
    result = sanitize_root(RAW_FIXTURE, PLAN_FIXTURE, tmp_path, "RS_DATASET_TEST_SECRET")
    output = "".join(path.read_text("utf-8") for path in result.output_files if path.suffix != ".png")
    assert "BVfake" not in output
    assert "raw-author" not in output
    assert secret not in output


def test_sanitize_emits_m2_sampling_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RS_DATASET_TEST_SECRET", "local-test-secret-at-least-32-bytes")
    result = sanitize_root(RAW_FIXTURE, PLAN_FIXTURE, tmp_path, "RS_DATASET_TEST_SECRET")
    manifest = SamplingManifest.model_validate_json(result.sampling_manifests[0].read_text("utf-8"))
    assert manifest.direction == "software_tool"
    assert manifest.video_id.startswith("video_")
    assert all(item.startswith("comment_") for values in manifest.stratum_comment_ids.values() for item in values)


def test_24_videos_split_10_7_7_and_keep_replies_together() -> None:
    split = stable_split(video_ids(24))
    assert [len(split.development), len(split.calibration), len(split.holdout)] == [10, 7, 7]
    assert len(set(split.development + split.calibration + split.holdout)) == 24
```

- [x] **Step 2: Run test to verify it fails**

Run: `uv run --project packages/dataset-tools pytest packages/dataset-tools/tests/test_sanitize.py packages/dataset-tools/tests/test_split.py -q`

Expected: sanitize and split imports fail.

- [x] **Step 3: Write minimal implementation**

Read exactly the raw video bundles named by the approved collection plan, pseudonymize video/author/comment/parent IDs, sanitize title/description/comment text, preserve nullable metrics and collection metadata, and write the equivalent directory plus `sanitization.json` and `sampling-manifest.json`. Do not process other directories found beside approved inputs; report only their count. Hash canonical input/output JSON with SHA-256; reports contain only pseudonymous IDs, counts, rule version, hashes and review reasons. Derive the target from the approved collection formula, author counts from sanitized comments, normalized duplicates as the sum of all repeated normalized-text occurrences beyond the first, exact duplicates from `exact_duplicate_merged` collection errors, and stratum IDs from each comment's accepted source. Set SamplingManifest `collected_total` to unique comment count plus exact duplicate occurrences, matching M2's pre-ID-dedup meaning. Translate `software_tools -> software_tool` and `life_services -> life_service`; reject unknown mappings.

```python
def stable_split(video_ids: Sequence[str]) -> DatasetSplit:
    ranked = sorted(video_ids, key=lambda value: (sha256(value.encode()).digest(), value))
    if len(ranked) == 24:
        return DatasetSplit(development=ranked[:10], calibration=ranked[10:17], holdout=ranked[17:])
    development = ceil(len(ranked) * 0.4)
    calibration = floor(len(ranked) * 0.3)
    return DatasetSplit(development=ranked[:development], calibration=ranked[development:development + calibration], holdout=ranked[development + calibration:])
```

Use sibling staging and backup directories with the same restore behavior as the collector, implemented independently. Reject an approved video whose raw directory is missing or has a mismatched platform/video key. A video with zero comments or zero successful pages writes a sanitization exclusion report, emits no SamplingManifest, and is added to the replacement-candidate report. Never modify raw files.

- [x] **Step 4: Run pipeline tests and checks**

Run: `uv run --project packages/dataset-tools pytest packages/dataset-tools/tests/test_sanitize.py packages/dataset-tools/tests/test_split.py -q && uv run --project packages/dataset-tools ruff check packages/dataset-tools && uv run --project packages/dataset-tools mypy --config-file packages/dataset-tools/pyproject.toml packages/dataset-tools/src`

Expected: raw IDs/secrets are absent and stable split passes.

- [x] **Step 5: Commit**

```bash
git add packages/dataset-tools/src/requirementseeker_dataset/sanitize.py packages/dataset-tools/src/requirementseeker_dataset/split.py packages/dataset-tools/tests/test_sanitize.py packages/dataset-tools/tests/test_split.py
git commit -m "feat(dataset): sanitize and split collected videos"
```

### Task 6: 生成空白标注材料并验证独立裁决

**Files:**
- Create: `packages/dataset-tools/src/requirementseeker_dataset/labels.py`
- Create: `packages/dataset-tools/tests/test_labels.py`

- [ ] **Step 1: Write the failing test**

```python
def test_export_contains_no_semantic_prefill(tmp_path: Path) -> None:
    annotation = export_labels(SANITIZED_FIXTURE, tmp_path)
    assert all(item.need_signal == "unlabeled" for item in annotation.comments)
    assert annotation.clusters == []


def test_cross_video_cluster_is_rejected() -> None:
    with pytest.raises(LabelValidationError, match="cross_video_cluster_member"):
        validate_labels(annotation_with_cross_video_cluster())


def test_dispute_requires_two_independent_annotations_and_adjudication() -> None:
    with pytest.raises(LabelValidationError, match="dispute_not_adjudicated"):
        validate_labels(disputed_without_decision())
    assert validate_labels(adjudicated_dispute()).evaluation_eligible is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project packages/dataset-tools pytest packages/dataset-tools/tests/test_labels.py -q`

Expected: labels module import fails.

- [ ] **Step 3: Write minimal implementation**

`export_labels` reads only sanitized IDs/text and the matching `sampling-manifest.json`, then emits one annotation file per video with explicit `unlabeled` fields and an empty cluster list. `validate_labels` rejects unknown enum values through Pydantic, raw-ID patterns, duplicate comments, cluster members outside the file video, duplicate cluster membership, incomplete required fields, and disputed records without two distinct annotator IDs plus an adjudicator decision.

```python
def validate_cluster(annotation: AnnotationFile, cluster: ClusterLabel) -> None:
    known = {item.comment_id for item in annotation.comments}
    if any(comment_id not in known for comment_id in cluster.comment_ids):
        raise LabelValidationError("cross_video_cluster_member")
    if len(cluster.comment_ids) != len(set(cluster.comment_ids)):
        raise LabelValidationError("duplicate_cluster_member")


def evaluation_eligible(annotation: AnnotationFile, adjudication: AdjudicationFile | None) -> bool:
    if not annotation.disputed:
        return annotation.is_complete
    return adjudication is not None and len(set(adjudication.annotator_ids)) == 2 and adjudication.decision is not None
```

- [ ] **Step 4: Run label tests**

Run: `uv run --project packages/dataset-tools pytest packages/dataset-tools/tests/test_labels.py -q`

Expected: blank export and closed validation tests pass.

- [ ] **Step 5: Commit**

```bash
git add packages/dataset-tools/src/requirementseeker_dataset/labels.py packages/dataset-tools/tests/test_labels.py
git commit -m "feat(dataset): export and validate human labels"
```

### Task 7: 接入 CLI、完成安全门禁并生成试点材料

**Files:**
- Create: `packages/dataset-tools/src/requirementseeker_dataset/cli.py`
- Create: `packages/dataset-tools/tests/test_cli.py`
- Create: `packages/dataset-tools/README.md`
- Local only: `.local-data/m2-real/sanitized/`
- Local only: `.local-data/m2-real/labels/`
- Local only: `docs/execution/2026-09-09.md`

- [ ] **Step 1: Write the failing test**

```python
def test_cli_reads_secret_by_environment_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    marker = "local-test-secret-at-least-32-bytes"
    monkeypatch.setenv("RS_DATASET_TEST_SECRET", marker)
    assert main(["sanitize", "--raw", str(RAW_FIXTURE), "--plan", str(PLAN_FIXTURE), "--output", str(tmp_path), "--secret-env", "RS_DATASET_TEST_SECRET"]) == 0
    assert marker not in capsys.readouterr().out


def test_cli_never_accepts_secret_value_argument(parser: ArgumentParser) -> None:
    help_text = parser.format_help()
    assert "--secret" not in help_text
    assert "--secret-env" in help_text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project packages/dataset-tools pytest packages/dataset-tools/tests/test_cli.py -q`

Expected: CLI import fails.

- [ ] **Step 3: Write minimal implementation**

Add the exact three commands from the approved spec. All commands emit ASCII summary JSON containing paths, counts, versions and safe error codes only. README documents environment-variable setup without sample secret values, local-only outputs, manual review, split semantics and the prohibition on automatic gold labels.

- [ ] **Step 4: Run the complete package gate**

Run: `uv run --project packages/dataset-tools pytest packages/dataset-tools/tests -q && uv run --project packages/dataset-tools ruff check packages/dataset-tools && uv run --project packages/dataset-tools ruff format --check packages/dataset-tools && uv run --project packages/dataset-tools mypy --config-file packages/dataset-tools/pyproject.toml packages/dataset-tools/src && uv lock --project packages/dataset-tools --check && uv build --project packages/dataset-tools && git diff --check`

Expected: all tests, lint, format, types, lock and build pass; package imports neither collector nor Agent.

- [ ] **Step 5: Commit**

```bash
git add packages/dataset-tools
git commit -m "feat(dataset): expose safe preparation CLI"
```

- [ ] **Step 6: Run the tool on the two approved local pilot directories**

Set the dataset secret only in the current process environment, run `sanitize`, then `export-labels`. Do not print the environment value. Open sanitized `video.json`, `comments.jsonl`, `sanitization.json` and label template in visible VS Code.

- [ ] **Step 7: Record the pilot result**

Write counts, replacement categories, review-item counts, split assignment, failures and next action to `docs/execution/2026-09-09.md`; do not include raw IDs, raw text or the secret.

- [ ] **Step 8: Run the approved 24-video batch**

After both platform pilots and the complete package gate pass, run `sanitize` and `export-labels` on the exact 24-entry approved manifest. Verify 24 sanitized directories, a stable 10/7/7 split, 5,090 input comment records accounted for, no raw IDs or secret values in output, and an explicit `unlabeled` annotation template for every sanitized comment. Record only aggregate counts, review categories, safe failure codes and output hashes in the local execution log.
