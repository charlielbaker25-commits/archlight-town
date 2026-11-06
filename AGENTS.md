# Public development notes

This is a fictional showcase snapshot. Keep real records and credentials out.

- Use temporary instances and fake clients in tests. Never send, call or publish.
- Every outside action requires an approved action row. Never bypass or weaken
  approval, content binding, the Guard, go-live checks or the kill switch.
- The Guard's hard rules are plain code. Models may add flags, never clear blocks.
- Reaper only ever calls the operator. No AI calls to prospects.
- `engine/` never imports `packs/`. Limits and model routes belong in pack config.
- Sonnet/Opus use the official `claude -p` CLI; remove `ANTHROPIC_API_KEY` from
  that subprocess. Never read or handle subscription credentials.
- Secrets stay local and untracked. The example config is not a live deployment.
- Playbook writes require approval and a passing before/after test-set gate.
- Run `pytest -m "not hq_browser"` for ordinary changes. Browser checks also need
  Chromium and `npm ci --prefix hq`. Explain changes and test results plainly.
- Preserve bundled third-party notices. Original code is all rights reserved.
