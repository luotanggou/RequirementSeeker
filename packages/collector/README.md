# RequirementSeeker Collector

`rs-collect` collects public video comments from Bilibili and Douyin through a visible,
ephemeral Chromium session. Login is completed by the user in that visible window; the
collector does not persist the browser context, cookies, tokens, headers, passwords, or
storage state.

All raw artifacts, temporary generations, challenge evidence, and run data stay local under
the current workspace's `.local-data/m2-real` boundary. The CLI rejects output roots outside
that directory.

After navigation, the CLI waits at most 120 seconds for the operator to report a fixed status.
By default, login and challenge handling are manual takeovers in the visible page; the collector
does not guess selectors or coordinates. An API caller may explicitly provide one click or drag
action. Only then does the collector invoke the supervised challenge handler once, save masked
page screenshots plus a safe action record, and ask the operator to confirm the resulting page
state. It does not capture browser keyboard events, repeat the action automatically, or use
third-party CAPTCHA services.

The response parser recognizes only these six current response families:

- Bilibili `/x/web-interface/view`
- Bilibili `/x/v2/reply/wbi/main`
- Bilibili `/x/v2/reply/reply`
- Douyin `/aweme/v1/web/aweme/detail`
- Douyin `/aweme/v1/web/comment/list`
- Douyin `/aweme/v1/web/comment/list/reply`

Run one visible pilot:

```sh
uv run --project packages/collector rs-collect pilot \
  --platform bilibili \
  --url https://www.bilibili.com/video/BVexample \
  --video-key BVexample
```

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
