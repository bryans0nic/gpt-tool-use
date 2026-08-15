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

### 1. Install & smoke-test locally (needs the real PC + browser)
- [ ] `pip install .` (or `pip install -e .`) in a venv, then
      `playwright install chromium`.
- [ ] Set `GPT_TOOLS_HOME` to a writable dir, e.g.
      `setx GPT_TOOLS_HOME %USERPROFILE%\.gpt-tools` (new shell after `setx`).
- [ ] `gpt-tools-login` → sign into ChatGPT in the window that opens, then close
      it. Confirm `%GPT_TOOLS_HOME%\chatgpt_profile\` was created and persists.
- [ ] Run the server once (`gpt-tools`) to confirm it starts under stdio without
      import/entry-point errors.

### 2. Selector sanity-check against the *current* ChatGPT site
- [ ] With the logged-in profile, run one `gpt_search` end-to-end and confirm a
      clean markdown result comes back.
- [ ] If it hangs or returns empty, the DOM selectors in `browser.py` have
      drifted. Check, in order: the prompt input selectors (`PROMPT_SELECTORS`),
      the `data-message-author-role="assistant"` container, and the
      `stop-button` completion signal. Patch the ones that moved; keep the
      fallback-list pattern rather than hard-coding a single selector.
- [ ] Note model selection: confirm the ChatGPT account's default model is the
      "thinking" one you want the offload to use (this fork doesn't switch models
      in the search path — the web UI's current default is what answers).

### 3. Windows-tailor the docs (safe to do without the live site)
- [ ] The upstream `README.md` "Running as a long-lived service" section is
      **macOS-only** (`launchd.plist.template`, TCC warnings). Add a **Windows
      Setup** section (or a `docs/WINDOWS.md`) covering: venv + `pip install`,
      `playwright install chromium`, `GPT_TOOLS_HOME` via `setx`,
      `gpt-tools-login`, and the Claude Code wiring below. Mark the launchd
      section clearly as macOS-only rather than deleting it.
- [ ] Add the **Claude Code CLI wiring** verbatim:
      ```
      claude mcp add gpt-tools -- gpt-tools
      ```
      or the `.mcp.json` / `~/.claude.json` block:
      ```json
      {
        "mcpServers": {
          "gpt-tools": {
            "command": "gpt-tools",
            "args": [],
            "env": { "GPT_TOOLS_HOME": "C:\\Users\\<you>\\.gpt-tools" }
          }
        }
      }
      ```
      (Use the full path to the `gpt-tools` script if it isn't on `PATH` — e.g.
      the venv's `Scripts\gpt-tools.exe`.)
- [ ] If the user wants **Claude Desktop** too (see open question), add the
      equivalent `claude_desktop_config.json` block.
- [ ] Add a one-line **orchestration note**: Claude Code decides *what* to
      offload and calls `gpt_search`; ChatGPT does the searching/reasoning and
      returns clean markdown. Keep the ToS/fragility caveat from upstream.

### 4. Optional: Windows autostart (only if the user asks)
- [ ] Replace the launchd approach with either a **Task Scheduler** entry or
      **NSSM**-wrapped service — but for Claude Code's stdio transport this is
      usually unnecessary (Claude Code spawns `gpt-tools` itself). Don't build
      this unless requested.

### 5. Finalize
- [ ] Update `README.md` so Windows is a first-class path.
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

## State at handoff

- Fork created and cloned; upstream code unmodified except this `HANDOFF.md`.
- No Windows docs written yet, no selector check run yet (both need the PC).
- Nothing installed or configured on the target machine.
