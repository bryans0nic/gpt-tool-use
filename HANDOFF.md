# Handoff — Windows tailoring of gpt-tool-use

**Purpose of this repo (as we're using it):** an MCP server that lets **Claude
Code act as the orchestrator** and offload heavy research/reasoning to an
**in-browser ChatGPT** session driven by Playwright — reusing the ChatGPT
subscription instead of the OpenAI API. This is a fork of
[`kev489/gpt-tool-use`](https://github.com/kev489/gpt-tool-use).

This file is a handoff from the remote (cloud) session that set the fork up, to
a **local session running on the Windows PC** where the browser, the ChatGPT
login, and Claude Code actually live. The remote session cannot run a real
browser against a logged-in ChatGPT, so steps that need the live site are left
for the local session (you) to do.

---

## Decisions already made (don't relitigate)

- **Backend:** browser automation of ChatGPT web (uses the subscription), *not*
  the OpenAI API. Chosen by the user with the ToS/fragility tradeoff understood.
- **Transport:** MCP server, invoked by Claude Code.
- **Why this repo over alternatives:** evaluated `cbusillo/chatgpt-automation-mcp`
  (more mature but **archived**, README says "OUT OF DATE", and needs a
  keep-your-real-Chrome-alive-over-CDP setup). This one is purpose-built for
  "Claude Code offloads to ChatGPT," is leaner, and uses an **isolated
  persistent profile** (`chatgpt_profile/`) instead of hijacking the main
  browser. Verified `browser.py` is cross-platform in its hot path — the only
  macOS-specific thing upstream is the optional `launchd` autostart.
- **Target OS:** Windows 10/11.

## Open question for the user (confirm before finalizing docs)

- Should setup target **Claude Code CLI only**, or **also Claude Desktop** on
  the PC? The wiring differs slightly (`claude mcp add` vs the Desktop
  `claude_desktop_config.json`). Default assumption if unanswered: **Claude Code
  CLI**, with a short Claude Desktop appendix.

---

## What this fork already provides (verified by reading the code)

- **Entry points** (`pyproject.toml [project.scripts]`):
  - `gpt-tools` → `mcp_server:main` — the MCP server.
  - `gpt-tools-login` → `login:main` — one-time interactive ChatGPT login.
- **Dependencies:** `mcp`, `playwright`, `markdownify`, `json-repair`.
- **MCP tools** (`mcp_server.py`): `gpt_search`, `gpt_search_batch`,
  `gpt_image_gen`, `gpt_image_gen_batch`. Search results can be saved to disk to
  keep Claude's context light; batch variants run concurrent ChatGPT tabs.
- **Profile / state:** `browser.py` uses
  `USER_DATA_DIR = $GPT_TOOLS_HOME/chatgpt_profile` (falls back to next to the
  code if `GPT_TOOLS_HOME` is unset). `launch_persistent_context`, headed by
  default, `--disable-blink-features=AutomationControlled`.
- **ChatGPT DOM handling** (`browser.py`) — this is the fragile surface:
  - Prompt input: tries `#prompt-textarea`,
    `[data-testid="prompt-textarea"]`, `div[contenteditable="true"][role="textbox"]`,
    `textarea[placeholder*="Ask"]` in order (`PROMPT_SELECTORS`).
  - Assistant messages: `div[data-message-author-role="assistant"]`.
  - Completion detection: new-assistant-node count + `[data-testid="stop-button"]`
    / `[aria-label="Stop generating"]` disappearing.
  - Dismisses onboarding/"Got it"/"OK" dialogs; has a rate-limit dialog path
    (`ChatGPTRateLimitError`) that screenshots on failure.

---

## Task list for the local session

Work top to bottom. Commit as you go; push to `main` of this fork
(`origin` = `https://github.com/bryans0nic/gpt-tool-use`).

### 1. Install & smoke-test locally (needs the real PC + browser) — DONE
- [x] venv created at `.venv`, `pip install -e .`, `playwright install chromium`.
- [x] `GPT_TOOLS_HOME` set via `setx` to `%USERPROFILE%\.gpt-tools`.
- [x] Server starts under stdio without import errors (after the `mcp<2.0.0`
      pin below).
- [x] `gpt-tools-login` / isolated-profile login — **superseded**, see below.

### 2. Auth: isolated profile didn't fit this deployment — pivoted to CDP
The isolated persistent-profile plan (decided in the "Decisions already made"
section above) hit a real-world wall: this ChatGPT deployment is behind HHS
SSO (`https://go.hhs.gov/chatgpt`, PIV-card auth), and the isolated profile
has no path to complete that login noninteractively or attended-once in a way
that stuck cleanly the first few tries.

Landed instead on **CDP attach to a dedicated Chrome profile** (not the
literal isolated `chatgpt_profile/` dir, not the user's live daily-driver
Chrome either — a middle path):
- `browser.py` now supports `GPT_TOOLS_CDP_URL` — when set, `ChatGPTBrowser`
  connects via `playwright.chromium.connect_over_cdp()` instead of
  `launch_persistent_context()`, and never closes the attached context/browser
  (only disconnects).
- Chrome **refuses** `--remote-debugging-port` on the literal default profile
  directory (a real, current security restriction — confirmed by testing, not
  assumed). So the working setup is: `robocopy` the default profile (minus
  caches) into `%GPT_TOOLS_HOME%\chrome_cdp_profile`, launch Chrome with the
  debug flag pointed at *that* copy, sign in once (PIV/MFA) in that window.
  Session persists there afterward — one-time cost, not per-run.
- Copied cookies alone were **not** sufficient to skip sign-in (Entra ID/PIV
  sessions are device-bound, don't survive a file copy) — a real interactive
  login in the debug-profile window was required regardless of the copy.
- Explicitly declined and did not build: (a) attaching CDP to the user's
  actual live default-profile Chrome (Chrome blocks this by design; a
  symlink/junction workaround was requested and refused), (b) reading/
  decrypting the live Chrome `Cookies` SQLite DB directly (equivalent to a
  credential-theft technique), (c) loading raw session-token cookies the user
  pasted into chat (live auth credentials — same category as a password).
- Full working end-to-end smoke test confirmed 2026-08-17 (see below).

### 3. Selector sanity-check against the *current* ChatGPT site — DONE
- [x] `gpt_search.py "In one sentence, what is the capital of France?"` →
      returned a clean one-line answer end-to-end via the CDP path. Selectors
      in `browser.py` did **not** need patching — they still match current
      ChatGPT DOM.
- [x] Found and fixed a real gotcha instead: the SSO gateway URL
      (`go.hhs.gov/chatgpt`) re-prompts **account selection** on every fresh
      navigation, even with a valid session cookie already present — it works
      once for interactive login but breaks unattended repeat tool calls.
      Fixed by adding `CHAT_URL`/`SSO_LOGIN_URL` split in `browser.py`:
      `login.py` still goes through the SSO gateway (needed to establish the
      session), but `ChatGPTBrowser.new_session()` now defaults to navigating
      straight to `https://chatgpt.com/?model=auto` (overridable via
      `GPT_TOOLS_CHAT_URL`), which reuses the already-established session
      cookie without re-triggering the picker.
- [ ] Model selection not yet confirmed — this fork doesn't switch models in
      the search path; whatever this account's default is answers.

### 4. Dependency drift found and fixed — DONE
- [x] The installed `mcp` package had moved to `2.0.0` (legitimate — verified
      against the real PyPI record and GitHub org, not a supply-chain issue,
      just newer than this repo's code expects) and renamed `FastMCP` to
      `MCPServer` under `mcp.server.mcpserver`, breaking `mcp_server.py`'s
      import (`mcp.server.fastmcp` no longer exists in 2.x). Pinned
      `mcp<2.0.0` in `pyproject.toml` and `requirements.txt` rather than
      rewriting `mcp_server.py` against the new API. `python -m pytest
      tests/ -q` passes (15 tests) with the pin in place.

### 5. Windows-tailor the docs — DONE
- [x] Added a **Windows setup** section to `README.md` covering venv install,
      `playwright install chromium`, `GPT_TOOLS_HOME` via `setx`,
      `gpt-tools-login`, and Claude Code MCP wiring.
- [x] Added an **SSO auth (enterprise ChatGPT via CDP)** subsection covering
      the profile-copy + debug-Chrome + `GPT_TOOLS_CDP_URL` flow, with an
      explicit warning against pointing it at a live daily-driver Chrome.
- [x] Marked the "Running as a long-lived service" section header as macOS-only
      rather than deleting it.
- [ ] Claude Desktop config block — not added; user only asked about Claude
      Code CLI so far (open question below still stands if that changes).

### 6. Optional: Windows autostart (only if the user asks)
- [ ] Replace the launchd approach with either a **Task Scheduler** entry or
      **NSSM**-wrapped service — but for Claude Code's stdio transport this is
      usually unnecessary (Claude Code spawns `gpt-tools` itself). Don't build
      this unless requested. Not done — not requested.

### 7. Finalize
- [ ] Commit + push to `main`.

---

## Environment notes / gotchas

- **This is a personal fork with no upstream LICENSE.** Keep attribution to
  `kev489/gpt-tool-use` in the README; don't strip authorship.
- **The persistent profile holds a live ChatGPT session** — treat
  `chatgpt_profile/` like a credential. It's gitignored upstream; keep it that
  way. Never commit it or paste its contents.
- **Headed vs headless:** login and normal runs are headed by default. Cloudflare
  tends to block headless — don't flip `headless=True` to "clean up" the UX
  without testing that ChatGPT still loads.
- **Speed tradeoff is expected:** browser automation is slower than an API call.
  That's fine for the "kick off, come back to a result" workflow.

## State at handoff (updated 2026-08-17, local session)

- Installed, tested, and working end-to-end on this Windows PC via CDP attach
  (see task 2 above) — `gpt_search.py` round-trips through the real HHS-SSO
  ChatGPT deployment.
- Code changes from the original fork: `browser.py` (`GPT_TOOLS_CDP_URL`,
  `CHAT_URL`/`SSO_LOGIN_URL` split), `login.py` (uses `SSO_LOGIN_URL`),
  `pyproject.toml`/`requirements.txt` (`mcp<2.0.0` pin). Not yet committed —
  see task 7.
- `README.md` has a Windows setup section including the CDP/SSO flow.
- Still open: Claude Desktop config (not requested yet), Windows autostart
  (not requested), commit + push.
