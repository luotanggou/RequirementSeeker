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

Run an approved version `1.0` manifest in file order:

```sh
uv run --project packages/collector rs-collect batch path/to/manifest.json
```

Both commands print only a compact JSON summary. A fatal login, unresolved challenge, access
restriction, or response-shape change stops later videos for that platform while allowing the
other platform to continue.

Before any 24-video batch, the acceptance gate is one successful Bilibili pilot and one
successful Douyin pilot, followed by user review and approval of both local outputs. A single
platform result must not be reported as two-platform coverage.
