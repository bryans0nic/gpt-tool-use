# GPT Tools MCP Server

MCP tools for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) that drive ChatGPT via Playwright browser automation. The tools:

- **`gpt_search`** — routes research queries through ChatGPT and returns clean markdown, or saves it directly to disk to keep the MCP client context light.
- **`gpt_image_gen`** — sends an image-gen prompt, saves the generated images to disk, and returns them so Claude can analyze the result.
- **`gpt_search_batch`** / **`gpt_image_gen_batch`** — run several prompts concurrently inside one MCP call, each in its own ChatGPT tab.

## Why

Claude Code can use MCP tools. This lets it delegate web research and image generation to ChatGPT, using your existing ChatGPT subscription instead of a separate API key.

### Token efficiency (search)

When Claude does web research natively, it burns tokens on search results, page fetches, and reasoning about what it found. With this tool, ChatGPT does all the thinking — it searches, reads sources, reasons through the answer, and uses its own thinking tokens. Claude just gets back a clean result.

### Design loop (image gen)

Image gen lets Claude iterate on visual designs without you having to shuttle screenshots manually. Generated images land in `./generated/` and are embedded in the tool response so Claude can critique, suggest changes, and re-prompt.

The tradeoff with both is speed. Browser automation is slower than a direct API call. If you're multitasking, it doesn't matter — Claude kicks off the call and you come back to a finished result.

## Setup

```bash
pip install git+https://github.com/kev489/gpt-tool-use.git
playwright install chromium
```

Or clone and install locally:

```bash
git clone https://github.com/kev489/gpt-tool-use.git
cd gpt-tool-use
pip install .
playwright install chromium
```

If you plan to run the launchd service, keep the clone outside `~/Desktop`, `~/Documents`, and `~/Downloads` — see the TCC warning under "One-time setup".

Run once to log into ChatGPT (opens a browser window — sign in, then close the window):

```bash
gpt-tools-login    # or: python login.py from a clone
```

Login state is stored in `chatgpt_profile/` next to the installed code (gitignored in a clone). With the pip install, set `GPT_TOOLS_HOME` to a writable directory (e.g. `~/.gpt-tools`) so the profile and debug screenshots don't land inside site-packages — set it both for the login command and in the MCP server's environment. You only need to log in once per machine.

Then add to your Claude Code MCP config (`~/.claude.json` under `mcpServers`, or via `claude mcp add`):

```json
{
  "mcpServers": {
    "gpt-tools": {
      "command": "gpt-tools",
      "args": []
    }
  }
}
```

This is the **stdio transport**: Claude Code spawns one MCP server subprocess per session. Simple, but if you run multiple Claude Code sessions at once, each spawns its own server, and they fight over the persistent ChatGPT profile lock — only one session can use image gen at a time. See "Running as a long-lived service" below for the multi-session setup.

## Windows setup

```powershell
git clone https://github.com/bryans0nic/gpt-tool-use.git
cd gpt-tool-use
python -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\python -m playwright install chromium
setx GPT_TOOLS_HOME "%USERPROFILE%\.gpt-tools"
```

Open a new shell (so `setx` takes effect), then log in:

```powershell
.venv\Scripts\gpt-tools-login
```

Claude Code MCP config (`~/.claude.json` or via `claude mcp add gpt-tools -- <path>\.venv\Scripts\gpt-tools.exe`):

```json
{
  "mcpServers": {
    "gpt-tools": {
      "command": "C:\\path\\to\\gpt-tool-use\\.venv\\Scripts\\gpt-tools.exe",
      "args": [],
      "env": { "GPT_TOOLS_HOME": "C:\\Users\\<you>\\.gpt-tools" }
    }
  }
}
```

### SSO auth (enterprise ChatGPT via CDP)

If your org's ChatGPT is behind SSO and the isolated-profile login (`gpt-tools-login`) can't reach it, attach to a real, already-logged-in Chrome via CDP instead:

1. Copy your Chrome profile once (only needed the first time — Chrome refuses `--remote-debugging-port` on the literal default profile dir, so a separate directory is required):
   ```powershell
   robocopy "$env:LOCALAPPDATA\Google\Chrome\User Data" "$env:GPT_TOOLS_HOME\chrome_cdp_profile" /E /XD Cache "Cache Cache" "Code Cache" GPUCache "Service Worker" GrShaderCache ShaderCache component_crx_cache extensions_crx_cache Sessions GPUPersistentCache
   ```
   (Close Chrome first so the copy isn't locked mid-file.)
2. Launch a debug-profile Chrome and sign in once (SSO/PIV/MFA — whatever your org requires):
   ```powershell
   & "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="$env:GPT_TOOLS_HOME\chrome_cdp_profile" "https://your-sso-gateway-url"
   ```
3. Point the tool at it: `setx GPT_TOOLS_CDP_URL "http://localhost:9222"`.

The debug-profile Chrome stays open in the background (it's a real Chrome window, not headless) — leave it running while using the tool. Sign-in persists in that profile going forward, so this is a one-time step per machine, not per session.

**Never point `GPT_TOOLS_CDP_URL` at your actual daily-driver Chrome** — Chrome blocks `--remote-debugging-port` on the default profile directory specifically to stop this. The debug profile above is a separate copy for a reason.

If your SSO gateway link re-prompts account selection on every visit (some IdPs do this by design), set `GPT_TOOLS_CHAT_URL` to the app's direct URL once you're signed in (e.g. `https://chatgpt.com/?model=auto`) — the gateway is only needed for the initial login, not every tool call. This is the default already; override only if your deployment's direct URL differs.

## Running as a long-lived service (macOS only — see Windows setup above for Windows)

If you have multiple Codex or Claude Code sessions open and want them all to use `gpt_image_gen` simultaneously, switch from stdio (one server per session) to HTTP (one shared server, all sessions are clients). The browser lives in the server, so there's only ever one Chromium accessing the profile.

### One-time setup

**1. Edit the launchd plist template.** `launchd.plist.template` ships with placeholder paths and a generic Label — replace each one with values for your machine:

- `Label` — `com.example.gpt-tools` → `com.YOURNAME.gpt-tools` (any reverse-DNS string; this is also the filename you'll use in step 2)
- `ProgramArguments[0]` — `/PATH/TO/python3` → your `python3` absolute path (find with `which python3`). If you upgrade Python later, update this path and reload the plist (`launchctl unload` + `launchctl load`); otherwise the service silently fails to start at next login (visible only as a non-zero exit code in `launchctl list | grep gpt-tools`).
- `ProgramArguments[1]` — `/PATH/TO/gpt_tool_use/mcp_server.py` → full path to `mcp_server.py`
- `WorkingDirectory`, `StandardOutPath`, `StandardErrorPath` — replace `/PATH/TO/gpt_tool_use` with the absolute path to this repo

**TCC warning:** the repo must live *outside* `~/Desktop`, `~/Documents`, and `~/Downloads`. macOS blocks launchd background jobs from those folders, so the service dies at spawn with exit code 78 (`EX_CONFIG`) and empty logs — Python never starts. If `launchctl list | grep gpt-tools` shows 78, it's this or a stale Python path.

**2. Install the plist.** Use whatever you set for `Label` as the filename:

```bash
cp launchd.plist.template ~/Library/LaunchAgents/com.YOURNAME.gpt-tools.plist
launchctl load ~/Library/LaunchAgents/com.YOURNAME.gpt-tools.plist
```

The server will now start at login and stay running. Logs go to `debug/launchd-stdout.log` and `debug/launchd-stderr.log` inside the repo.

**3. Switch your MCP config to HTTP.**

Codex (`~/.codex/config.toml`, and any alternate `CODEX_HOME` such as `~/.codex-app-alt/config.toml`):

```toml
[mcp_servers.gpt-tools]
url = "http://127.0.0.1:8788/mcp"
tool_timeout_sec = 1800
startup_timeout_sec = 120
```

Claude Code:

```json
{
  "mcpServers": {
    "gpt-tools": {
      "type": "http",
      "url": "http://127.0.0.1:8788/mcp"
    }
  }
}
```

Restart the MCP client. All sessions now share the long-lived server. Multiple sessions can call `gpt_image_gen` / `gpt_image_gen_batch` at the same time.

### Manual run (no launchd)

```bash
python mcp_server.py --transport http --port 8788
```

Useful for testing the HTTP path before installing as a service. Avoid `--headless` for ChatGPT automation unless you have verified the account is not being stopped by browser verification.

### Reverting to stdio

Unload the launchd agent (`launchctl unload ~/Library/LaunchAgents/com.YOURNAME.gpt-tools.plist`) and put back the original stdio config. No code changes needed — the server supports both transports.

## How `gpt_search` works

1. Your query is sent directly to ChatGPT as a prompt (no system prompt — you control the output). You can pass the prompt directly as `query`, or provide `prompt_file` to read the prompt from a text file.
2. Playwright waits for the response to finish streaming (up to 8 minutes)
3. JavaScript DOM evaluation strips citation buttons, SVGs, accordion dropdowns, and other UI artifacts
4. The cleaned HTML is converted to markdown via `markdownify`
5. Inline citation markers (`[1]`, `[2]`, etc.) are stripped before returning to Claude or writing to `output_file`

With `output_json=true`, the tool treats JSON cleanup as a best-effort post-processing step. It does not change the prompt, and it reads the response through the raw text path instead of the markdown conversion path so JSON string escapes survive. If `output_file` is provided, the raw ChatGPT output is saved first; then the tool tries to extract, repair, parse, and format valid JSON. On success it overwrites the file with normalized JSON. On failure it leaves the raw output in place and reports that JSON post-processing did not succeed.

Parameters:
- `query` (optional) — the full prompt to send to ChatGPT
- `prompt_file` (optional) — path to a text file containing the prompt. Use either `query` or `prompt_file`, not both.
- `output_file` (optional) — path where the cleaned markdown response should be saved. Parent directories are created automatically.
- `return_output` (optional) — when `False`, Claude only gets a short saved-path summary. Defaults to `True` unless `output_file` is provided; with `output_file`, it defaults to `False` to avoid filling Claude's context with the full response.
- `output_json` (optional) — when `True`, best-effort normalize the response to valid JSON after ChatGPT returns; if parsing/repair fails, leave the raw output unchanged.
- `conversation_url` (optional) — a `chatgpt.com/c/<id>` URL (or just the `<id>`) to continue an existing conversation instead of starting a new one. Get this by copying the URL from the ChatGPT tab while viewing the conversation.
- `project` (optional) — name of a ChatGPT project (as shown in the sidebar) to continue instead of starting a new chat. Resolves to that project's most recently active conversation via ChatGPT's own internal API (no sidebar clicking — see Notes below). Ignored if `conversation_url` is also given.
- `attach_zip` (optional) — when `True`, zip the current project directory (git-tracked + untracked-but-not-ignored files if it's a git repo, otherwise a plain walk minus junk dirs) and attach it to the prompt via drag-and-drop simulation before sending. Useful on the first message of a resumed chat, or whenever ChatGPT needs the current repo state instead of a manual copy-paste.
- `effort` (optional) — one of `instant`, `medium`, `high`, `extra high`, `pro` (case-insensitive), set on the composer's reasoning-effort slider before sending. Leaves whatever's currently selected if omitted. Use `instant` for quick low-stakes queries, `pro` for genuinely hard problems.

Relative file paths resolve from the MCP server process working directory. Use absolute paths if the server is running as a long-lived HTTP/launchd service.

Example:

```json
{
  "prompt_file": "prompts/research.txt",
  "output_file": "research/chatgpt-answer.md"
}
```

JSON output example:

```json
{
  "query": "List three current low-cost index funds as an array of objects with ticker, fund_name, and expense_ratio.",
  "output_file": "research/funds.json",
  "output_json": true
}
```

For multiple text prompts, use **`gpt_search_batch`**. Each request opens its own ChatGPT tab and runs concurrently inside one MCP call. This is the preferred way to batch text research because some MCP clients serialize multiple separate tool calls to the same server. The server runs at most 3 ChatGPT tabs at a time across all calls and sessions; larger batches queue internally. Each request also accepts an optional `label`, used as its heading in the combined response.

```json
{
  "requests": [
    {
      "prompt_file": "/absolute/path/prompts/topic-1.txt",
      "output_file": "/absolute/path/outputs/topic-1.md"
    },
    {
      "prompt_file": "/absolute/path/prompts/topic-2.txt",
      "output_file": "/absolute/path/outputs/topic-2.json",
      "output_json": true
    }
  ]
}
```

## How `gpt_image_gen` works

1. Your prompt is sent to a fresh ChatGPT chat (each call resets — no context contamination across iterations)
2. Playwright waits up to 8 minutes for the stop button to disappear AND `<img>` elements to settle
3. Image URLs are downloaded via the browser's authenticated session (signed URLs work)
4. Files are saved to `<cwd>/generated/<prefix>.png` (or `<prefix>-1.png`, `<prefix>-2.png`, ... for multiple)
5. The tool returns a text summary with paths, plus (by default) the image bytes inline so Claude can see them

**Concurrent calls** are supported. All tools share a single Chromium context held in module state; each individual request gets its own page (= its own fresh ChatGPT tab/chat). The server caps itself at 3 concurrent ChatGPT tabs across all calls and sessions; anything beyond that queues internally.

To actually run multiple image-gen prompts in parallel from Claude, use **`gpt_image_gen_batch`**. Issuing several separate `gpt_image_gen` tool calls from one assistant message gets serialized by the MCP harness — but a batch call fans out internally with `asyncio.gather`, running up to 3 tabs at a time and queuing the rest. Pass `requests=[{prompt, filename_prefix?, save_dir?}, ...]`. Account-level rate limits may apply at high concurrency.

If ChatGPT shows the "Too many requests" conversation-protection modal, the browser automation saves a debug screenshot under `debug/`, clicks "Got it", and continues waiting for the response. It raises `ChatGPTRateLimitError` only if the dialog cannot be dismissed, persists after dismissal, or no usable response arrives afterward. Batch tools report the affected item as failed while letting other in-flight items finish.

Parameters:
- `prompt` (required) — the full image-gen prompt
- `filename_prefix` (optional) — descriptive stem for saved files. Falls back to a hash if omitted.
- `save_dir` (optional) — overrides the default `<cwd>/generated/`
- `embed_images` (optional, default `True`) — when `False`, Claude only gets the paths back. Use this during long iteration loops where embedded image bytes would flood Claude's context.

## Files

| File | Purpose |
|------|---------|
| `mcp_server.py` | MCP server entry point (stdio or HTTP transport); registers the tools |
| `browser.py` | Playwright ChatGPT automation; both text-streaming and image-gen flows |
| `gpt_search.py` | Standalone CLI wrapper (text only) |
| `login.py` | First-run helper for ChatGPT login |
| `tests/test_mcp_server.py` | Unit tests for the JSON-normalization and prompt helpers |

## Notes

- The `chatgpt_profile/` directory stores your persistent Chromium session. It's gitignored — you need to log in on each machine.
- Browser runs headed by default so you can complete the ChatGPT login on first run, and so you can watch image gen progress when debugging.
- `gpt_image_gen` saves images relative to the Claude Code cwd, not the MCP server's source directory — so files land in whatever project Claude is working on.
- Failures raise: send timeouts, stream timeouts, and rate-limit dead ends surface as tool-call errors (with a debug screenshot path under `debug/`) instead of error text masquerading as a response.
- **File attach uses simulated drag-and-drop, not the "Add files and more" button.** That button was tried first (menu scan, force-click, `expect_file_chooser`) and produced no observable effect in live testing — no menu, no native dialog. Drag-and-drop (`ChatGPTSession.attach_file` in `browser.py`) dispatches synthetic `dragenter`/`dragover`/`drop`/`dragleave`/`dragend` events with a `DataTransfer` built from the file's own bytes, and is confirmed working. If a future ChatGPT UI change breaks this, revisit the button-click path rather than assuming drag-drop is the only option.
- **After a drag-drop attach, Enter doesn't always submit.** The composer's internal "attachment ready" state can lag the visible DOM when a file arrives via synthetic events instead of a real user drop. `_send_prompt` in `browser.py` checks whether the text is still sitting unsent after `Enter` and falls back to clicking the send button (`[data-testid="send-button"]`) if so — this applies to every `gpt_search`/`gpt_image_gen` call, not just attach, so it's a safety net rather than an attach-specific special case.
- **`project` resolves via ChatGPT's internal API, not sidebar clicking.** DOM automation (clicking through the sidebar/search UI to find a named project and open its latest thread) was tried first and dropped after repeated live-testing showed real fragility: an ambiguous "search chats" button, no stable search modal (Ctrl+K opens nothing in this UI version), and — the actual dealbreaker — failed attempts left stray draft text sitting in the live composer, one accidental `Enter` away from being sent into a real conversation. `find_project_conversation_id` in `browser.py` instead calls three of ChatGPT's own internal endpoints directly (via the browser's already-authenticated `context.request`, no page needed): `GET /api/auth/session` for a bearer token, `GET /backend-api/gizmos/snorlax/sidebar` to find the project ("gizmo") by name, then `GET /backend-api/gizmos/{id}/conversations` for its conversation list (pre-sorted by recency — index 0 is most recent). These are unofficial/reverse-engineered endpoints (not a published OpenAI API, documented by third-party ChatGPT-export tools on GitHub), so treat them as the same fragility class as the DOM selectors elsewhere in this file — they can break on a backend change, just less often, since JSON APIs don't have hover-flyouts or React state races. Confirmed working live end-to-end against a real project.

## Development

```bash
pip install -e ".[dev]"
python3 -m pytest tests/ -q
```

The tests cover the pure helpers (JSON extraction/repair, citation stripping, prompt-source validation). The browser flows have no automated coverage — verify those against real ChatGPT.
