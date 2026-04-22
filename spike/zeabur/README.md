# Zeabur M-1 Spike

One-shot container that answers Q2 ("does the Max plan OAuth still work when
copied to a different host?") by running 10 concurrent `claude -p` calls from
a Zeabur-hosted container.

## How to run

### 1. Encode your OAuth file (on your laptop, PowerShell)

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("$env:USERPROFILE\.claude.json")) | Set-Clipboard
```

The full base64 string is now on your clipboard.

### 2. Push this repo to GitHub

Commit `spike/zeabur/` and push. Zeabur will pull it.

### 3. Create a Zeabur service

- New service → "Deploy from GitHub" → select this repo
- Under "Build Configuration", set **root directory** to `spike/zeabur`
  (so Zeabur builds the Dockerfile in this folder, not the repo root)
- Under "Environment Variables", add:
  - `CLAUDE_AUTH_B64` — paste the base64 from step 1
  - `N` — optional, defaults to 10. Set to `3` for a lighter first run.

### 4. Deploy and read the logs

Zeabur builds the container, runs `spike.sh`, and leaves it sleeping. Open the
service's **Logs** tab. You should see:

- `auth file written: 33191 bytes`
- `claude --version: 2.1.104 (Claude Code)`
- `=== N concurrent Claudes ===`
- per-agent result table
- `=== summary: N passed, 0 failed ===`

### 5. Interpret

| Result | Meaning |
|---|---|
| All `rc=0`, real times 4-10s, wall time ~8-15s | ✅ Q2 answered: OAuth transfers cleanly, concurrency holds. |
| `rc≠0` with `401`/`403` | ❌ Anthropic flagged the anomalous login. Rethink auth strategy. |
| `rc≠0` with `429` | ⚠️ Max plan rate-limiting. Retry or reduce N. |
| Wall ≈ N × 4s | ⚠️ Requests serializing server-side. |
| `FATAL: base64 decode...` | env var wasn't pasted correctly. Re-copy from step 1. |

### 6. Clean up

Delete the Zeabur service. Container exits, no state persists.

## Notes

- **Why sleep infinity?** Zeabur treats exited containers as crashed and
  restart-loops them. Keeping the container alive after the test means the
  logs stay readable. Delete the service when done.
- **Why base64?** `.claude.json` contains JSON with newlines; Zeabur env var
  UIs choke on multi-line values. Base64 is one line.
- **Security**: the OAuth file is in an env var and in `/root/.claude.json`
  inside the container. Delete the service promptly after reading results.
  Rotate the token (`claude logout && claude login` on your laptop) if you
  suspect the env var leaked.
