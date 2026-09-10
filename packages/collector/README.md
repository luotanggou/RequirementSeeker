# RequirementSeeker Collector

`rs-collect` collects public video comments from Bilibili and Douyin through a visible,
ephemeral Chromium session. Login is completed by the user in that visible window; the
collector does not persist the browser context, cookies, tokens, headers, passwords, or
storage state.

All raw artifacts, temporary generations, challenge evidence, and run data stay local under
`.local-data/m2-real` by default. A supervised challenge attempt requires live confirmation,
is limited to one visible mouse action, and may save masked page screenshots plus a safe action
record. It does not capture keyboard input or use third-party CAPTCHA services.

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
