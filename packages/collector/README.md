# RequirementSeeker Collector

`rs-collect` collects public video comments from Bilibili and Douyin through a visible browser.
The default is an ephemeral bundled Chromium session. An explicit `--browser chrome|edge
--reuse-login` selection instead uses a dedicated profile under
`.local-data/m2-real/browser-profiles/<platform>/<browser>/` so the browser can retain login
state between runs. The collector never opens or copies a daily browser profile and does not
read or export cookies, tokens, headers, passwords, storage state, or profile contents.

All raw artifacts, temporary generations, challenge evidence, and run data stay local under
the current workspace's `.local-data/m2-real` boundary. The CLI rejects output roots outside
that directory.

After navigation, the CLI waits at most 120 seconds for the operator to report a fixed status.
By default, login and challenge handling are manual takeovers in the visible page; the collector
does not guess selectors or coordinates. An API caller may explicitly provide one click or drag
action. Only then does the collector invoke the supervised challenge handler once, save masked
page screenshots plus a safe action record, and ask for a separate exact `yes` before the mouse
action. Reporting `ready` only permits the collector to check the page again; it never authorizes
that action. The collector then asks the operator to confirm the resulting page state. It does not
capture browser keyboard events, repeat the action automatically, or use third-party CAPTCHA
services.

The response parser recognizes only these six current response families:

- Bilibili `/x/web-interface/view`
- Bilibili `/x/v2/reply/wbi/main`
- Bilibili `/x/v2/reply/reply`
- Douyin `/aweme/v1/web/aweme/detail`
- Douyin `/aweme/v1/web/comment/list`
- Douyin `/aweme/v1/web/comment/list/reply`

Pagination is driven only by visible page controls and fixed-distance scrolling; the collector
does not construct or replay platform API requests. After the initial sort-mode pass, each round
performs at most one visible reply expansion and one fixed scroll, then waits for response
processing. Progress is the number of unique comment IDs, while duplicate responses remain in
the current run for conflict validation. Collection stops at the target, explicit exhaustion,
three consecutive rounds without a new ID, or 100 rounds. The last two conditions return a
partial result with `pagination_stalled` or `pagination_round_limit` in the local audit record.

Run the offline Playwright gate against the checked-in synthetic loopback page:

```sh
uv run --project packages/collector playwright install chromium
uv run --project packages/collector pytest \
  packages/collector/tests/integration/test_playwright_collection.py -q
```

This gate launches an ephemeral Chromium context (headless only for the test), registers the
response listener before navigation, reads video metadata and page state from the DOM, and
parses comments only from an artificial JSON response. It rejects non-loopback page URLs and
does not contact Bilibili or Douyin. The production `pilot` flow remains visibly headed and
supervised.

Run one visible pilot:

```sh
uv run --project packages/collector rs-collect pilot \
  --platform bilibili \
  --url https://www.bilibili.com/video/BVexample \
  --video-key BVexample
```

To opt into a dedicated installed-Chrome profile for that platform:

```sh
uv run --project packages/collector rs-collect pilot \
  --platform bilibili \
  --url https://www.bilibili.com/video/BVexample \
  --browser chrome \
  --reuse-login
```

`--browser chrome|edge` requires `--reuse-login`, and `--reuse-login` cannot be combined with
the default `chromium`. Profile paths are fixed by the collector and rejected if they escape,
redirect through a symlink/junction/reparse point, or overlap artifact, run, or challenge trees.
Run reports contain only the browser enum and `ephemeral|dedicated` session mode, never a
profile path.

Discover candidate metadata from one explicit query for manual review:

```sh
uv run --project packages/collector rs-collect discover \
  --platform bilibili \
  --direction software_tools \
  --query "AI tools" \
  --max-pages 3 \
  --max-results 50
```

Discovery accepts exactly one of `--query` or `--source-url`. It writes a candidate manifest and
audit report under `.local-data/m2-real/candidates/<run-id>/`; it does not read comment bodies,
approve candidates, or start a batch. The default is ephemeral Chromium. As with `pilot`, only an
explicit `--browser chrome|edge --reuse-login` selection uses the platform's dedicated saved
profile.

Review the discovery files, assemble a separate collection manifest, and validate its exact
24-video coverage:

```sh
uv run --project packages/collector rs-collect validate-plan path/to/manifest.json
```

The validation command exits successfully only for exact 24-video, 12/12 platform,
6/6/4/4/4 direction, and 8/8/8 comment-scale coverage. It reports gaps but never changes or
approves the manifest.

Only after the candidate review, coverage validation, and explicit user approval, run the approved
version `1.0` manifest in file order:

```sh
uv run --project packages/collector rs-collect batch path/to/manifest.json
```

All commands print only a compact ASCII JSON summary. A fatal login, unresolved challenge, access
restriction, or response-shape change stops later videos for that platform while allowing the
other platform to continue.

Before any 24-video batch, the acceptance gate is one successful Bilibili pilot and one
successful Douyin pilot, followed by user review and approval of both local outputs. A single
platform result must not be reported as two-platform coverage.
