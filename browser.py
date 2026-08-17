import os
import re
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

_STATE_HOME = os.environ.get("GPT_TOOLS_HOME") or os.path.dirname(os.path.abspath(__file__))
USER_DATA_DIR = os.path.join(_STATE_HOME, "chatgpt_profile")
DEBUG_DIR = os.path.join(_STATE_HOME, "debug")
# ponytail: opt-in CDP attach to a real, already-logged-in Chrome instead of the
# isolated profile below. Set GPT_TOOLS_CDP_URL (e.g. http://localhost:9222) and
# launch Chrome with --remote-debugging-port=9222 first. Unset = old behavior.
CDP_URL = os.environ.get("GPT_TOOLS_CDP_URL")
# ponytail: go straight to chatgpt.com once logged in — the HHS SSO gateway
# (go.hhs.gov/chatgpt) re-prompts account selection on every visit, which
# breaks unattended tool calls. Only login.py needs the gateway URL, to
# establish the SSO session in the first place.
CHAT_URL = os.environ.get("GPT_TOOLS_CHAT_URL") or "https://chatgpt.com/?model=auto"
SSO_LOGIN_URL = "https://go.hhs.gov/chatgpt"
PROMPT_SELECTORS = [
    "#prompt-textarea",
    '[data-testid="prompt-textarea"]',
    'div[contenteditable="true"][role="textbox"]',
    'div[contenteditable="true"]',
    'textarea[placeholder*="Ask"]',
]
# The reasoning-effort slider's 5 stops, in ascending order — confirmed live
# by walking a fresh slider from Home (0) to End (4) and reading its label.
EFFORT_LEVELS = ["instant", "medium", "high", "extra high", "pro"]
RATE_LIMIT_TITLE = "too many requests"
RATE_LIMIT_SNIPPETS = [
    "making requests too quickly",
    "temporarily limited access to your conversations",
    "please wait a few minutes before trying again",
]
RATE_LIMIT_REQUIRED_SNIPPETS = 2


class ChatGPTRateLimitError(RuntimeError):
    pass


def _debug_path(name: str) -> str:
    os.makedirs(DEBUG_DIR, exist_ok=True)
    return os.path.join(DEBUG_DIR, name)


async def _get_rate_limit_message(page) -> str | None:
    try:
        return await page.evaluate(
            '''
            ({ title, snippets, requiredSnippets }) => {
                const bodyText = document.body?.innerText || '';
                const normalized = bodyText.toLowerCase();
                const snippetHits = snippets.filter(snippet => normalized.includes(snippet)).length;
                if (!normalized.includes(title) && snippetHits < requiredSnippets) return null;

                const excludeSel = '[data-message-author-role], #prompt-textarea, [data-testid="prompt-textarea"]';
                const candidates = [
                    ...document.querySelectorAll('[role="dialog"], [aria-modal="true"], div')
                ].filter(el => !el.closest(excludeSel) && !el.querySelector(excludeSel))
                 .map(el => (el.innerText || '').trim())
                 .filter(text => {
                    const lower = text.toLowerCase();
                    const localHits = snippets.filter(snippet => lower.includes(snippet)).length;
                    return lower.includes(title) || localHits >= requiredSnippets;
                 })
                 .sort((a, b) => a.length - b.length);

                return candidates.length ? candidates[0].replace(/\\s+/g, ' ') : null;
            }
            ''',
            {
                "title": RATE_LIMIT_TITLE,
                "snippets": RATE_LIMIT_SNIPPETS,
                "requiredSnippets": RATE_LIMIT_REQUIRED_SNIPPETS,
            },
        )
    except Exception:
        return None


async def _dismiss_rate_limit_if_present(page, debug_name: str) -> str | None:
    message = await _get_rate_limit_message(page)
    if not message:
        return None

    shot_path = _debug_path(debug_name)
    try:
        await page.screenshot(path=shot_path)
    except Exception:
        pass

    dismissed = False
    buttons = [
        page.get_by_role("button", name="Got it"),
        page.get_by_role("button", name="OK"),
        page.locator('button:has-text("Got it")'),
        page.locator('button').filter(has_text="Got it"),
    ]
    for button in buttons:
        try:
            if await button.count() and await button.first.is_visible():
                await button.first.click()
                dismissed = True
                break
        except Exception:
            continue

    if not dismissed:
        raise ChatGPTRateLimitError(f"ChatGPT rate limit detected, but the dialog could not be dismissed: {message}. Screenshot: {shot_path}")

    await page.wait_for_timeout(1000)
    if await _get_rate_limit_message(page):
        raise ChatGPTRateLimitError(f"ChatGPT rate limit dialog persisted after dismissal: {message}. Screenshot: {shot_path}")

    return message


async def _attempt_recovery(page) -> bool:
    """Best-effort recovery from an unknown stuck state: dismiss whatever's
    blocking (a leftover overlay, a stray dialog) without ever submitting or
    navigating anything. Returns True if it did something, so the caller
    knows whether a retry is worth attempting.

    ponytail: deterministic and narrow on purpose — only dismiss actions
    (Escape, closing known dialog buttons), nothing that could misfire on a
    real authenticated account. If this proves insufficient for a stuck
    state it can't name, that's the signal to add a vision-model fallback
    (screenshot + AI-chosen action) rather than widening this blindly.
    """
    acted = False
    try:
        await page.keyboard.press("Escape")
        acted = True
    except Exception:
        pass
    for text in ("Got it", "OK", "Close", "Dismiss"):
        button = page.get_by_role("button", name=text)
        try:
            if await button.count() and await button.first.is_visible():
                await button.first.click()
                acted = True
        except Exception:
            continue
    return acted


async def _find_prompt_locator(page, timeout_ms: int = 30000):
    deadline = timeout_ms
    while deadline > 0:
        await _dismiss_rate_limit_if_present(page, "debug_rate_limit_prompt.png")
        for selector in PROMPT_SELECTORS:
            locator = page.locator(selector).first
            try:
                if await locator.count() and await locator.is_visible():
                    return locator
            except Exception:
                continue
        await page.wait_for_timeout(500)
        deadline -= 500
    raise TimeoutError(f"Failed to find visible ChatGPT prompt input using selectors: {', '.join(PROMPT_SELECTORS)}")


class ChatGPTBrowser:
    def __init__(self, headless=False):
        self.headless = headless
        self.playwright = None
        self.browser = None  # only set in CDP mode; we don't own it, never close it
        self.context = None
        self._closed = False

    async def start(self):
        self.playwright = await async_playwright().start()
        if CDP_URL:
            self.browser = await self.playwright.chromium.connect_over_cdp(CDP_URL)
            self.context = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()
        else:
            self.context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=USER_DATA_DIR,
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
        self.context.on("close", self._on_context_close)
        self._closed = False

    def _on_context_close(self, *_args):
        self._closed = True

    @property
    def is_alive(self) -> bool:
        return self.context is not None and not self._closed

    async def _new_page(self):
        if not self.is_alive:
            raise RuntimeError("Browser context is not alive; call start() first.")
        try:
            return await self.context.new_page()
        except Exception as e:
            self._closed = True
            raise RuntimeError(f"Failed to open new page (browser likely disconnected): {e}")

    async def new_session(self):
        page = await self._new_page()
        await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=60000)
        try:
            await _find_prompt_locator(page, timeout_ms=30000)
        except ChatGPTRateLimitError:
            await page.close()
            raise
        except Exception as e:
            shot_path = _debug_path("debug_session_init.png")
            try:
                await page.screenshot(path=shot_path)
            except Exception:
                pass
            await page.close()
            raise Exception(f"Failed to find chat input on new session. Screenshot: {shot_path}. Error: {e}")
        return ChatGPTSession(page)

    async def _api_get(self, path: str, token: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = await self.context.request.get(f"https://chatgpt.com{path}", headers=headers)
        if not resp.ok:
            raise Exception(f"ChatGPT API GET {path} failed: {resp.status} {await resp.text()}")
        return await resp.json()

    async def _get_access_token(self) -> str:
        # Cookie-authenticated (no bearer needed for this one endpoint itself).
        data = await self._api_get("/api/auth/session")
        token = data.get("accessToken")
        if not token:
            raise Exception("No accessToken in /api/auth/session response — not logged in?")
        return token

    async def find_project_conversation_id(self, project_name: str, index: int = 0) -> str:
        """Look up a ChatGPT project ("gizmo") by name and return the id of
        one of its conversations (index 0 = most recent; the list comes back
        pre-sorted by recency). Uses ChatGPT's own internal API — no sidebar
        clicking — via the browser's already-authenticated session.

        ponytail: DOM/sidebar automation (clicking through the "search
        chats" UI to find a project's most recent thread) was tried first
        and dropped — no stable search modal exists in the current UI, and
        failed attempts left stray draft text sitting in the live composer,
        one accidental Enter away from being sent into a real chat. This API
        path has none of that risk. It's unofficial/reverse-engineered
        (not a published OpenAI API), so it can break on a backend change —
        same risk class as the DOM selectors elsewhere in this file.
        """
        token = await self._get_access_token()
        sidebar = await self._api_get("/backend-api/gizmos/snorlax/sidebar", token=token)
        match = next(
            (
                item["gizmo"] for item in sidebar.get("items", [])
                if item.get("gizmo", {}).get("display", {}).get("name", "").lower() == project_name.lower()
            ),
            None,
        )
        if match is None:
            raise Exception(f"No ChatGPT project named '{project_name}' found in your sidebar.")
        gizmo_id = match["id"]

        conversations = await self._api_get(f"/backend-api/gizmos/{gizmo_id}/conversations?cursor=", token=token)
        items = conversations.get("items", [])
        if not items or index >= len(items):
            raise Exception(f"Project '{project_name}' has no conversation at index {index} (found {len(items)}).")
        return items[index]["id"]

    async def resume_conversation(self, conversation_url_or_id: str):
        """Continue an existing ChatGPT conversation instead of starting a
        fresh one. Accepts a full chatgpt.com/c/<id> URL or just the <id>.
        """
        conv_id = conversation_url_or_id.strip()
        if conv_id.startswith("http"):
            url = conv_id
        else:
            conv_id = conv_id.rstrip("/").rsplit("/", 1)[-1]
            url = f"https://chatgpt.com/c/{conv_id}"

        page = await self._new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            await _find_prompt_locator(page, timeout_ms=30000)
        except ChatGPTRateLimitError:
            await page.close()
            raise
        except Exception as e:
            shot_path = _debug_path("debug_resume_conversation.png")
            try:
                await page.screenshot(path=shot_path)
            except Exception:
                pass
            await page.close()
            raise Exception(f"Failed to resume conversation '{conversation_url_or_id}'. Screenshot: {shot_path}. Error: {e}")
        return ChatGPTSession(page)

    async def close(self):
        if self.context and self.browser is None:
            # CDP mode: this context is the user's real browser — never close it,
            # just disconnect below.
            try:
                await self.context.close()
            except Exception:
                pass
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
        self._closed = True


class ChatGPTSession:
    def __init__(self, page):
        self.page = page

    async def close(self):
        try:
            await self.page.close()
        except Exception:
            pass

    async def set_effort(self, level: str):
        """Set the reasoning-effort slider before sending. `level` is one of
        EFFORT_LEVELS (case-insensitive): instant, medium, high, extra high, pro.

        Confirmed live: it's a 5-stop slider (role="slider", aria-valuenow
        0-4), not a click-menu of discrete options — opened via the
        composer's model/effort pill (`.__composer-pill`). Keyboard control
        (Home to reset to 0, then ArrowRight N times) is reliable; dragging
        would be needlessly fragile by comparison.
        """
        normalized = level.strip().lower()
        if normalized not in EFFORT_LEVELS:
            raise ValueError(f"Unknown effort level {level!r}. Choose one of: {', '.join(EFFORT_LEVELS)}")
        target = EFFORT_LEVELS.index(normalized)

        pill = self.page.locator(".__composer-pill").first
        await pill.click(timeout=10000)
        await self.page.wait_for_timeout(400)

        slider = self.page.locator('[role="slider"]').first
        await slider.wait_for(state="visible", timeout=10000)
        await slider.focus()
        await self.page.keyboard.press("Home")
        for _ in range(target):
            await self.page.keyboard.press("ArrowRight")
        await self.page.wait_for_timeout(200)

        actual = await slider.get_attribute("aria-valuenow")
        await self.page.keyboard.press("Escape")
        if actual != str(target):
            shot_path = _debug_path("debug_effort_mismatch.png")
            try:
                await self.page.screenshot(path=shot_path)
            except Exception:
                pass
            raise Exception(f"Effort slider landed on {actual!r}, expected {target!r} ({level}). Screenshot: {shot_path}")

    async def attach_file(self, file_path: str, timeout_ms: int = 60000):
        """Attach a file to the prompt by simulating a drag-and-drop onto the
        composer, and wait for the upload to finish before returning.

        ponytail: the composer's "Add files and more" button was tried first
        (menu scan, force-click, expect_file_chooser) and produced no
        observable effect in testing — no menu, no native dialog. Drop-zone
        simulation is the standard Playwright workaround for exactly this
        case and was confirmed working live: dispatching synthetic
        dragenter/dragover/drop events with a DataTransfer built from the
        file's own bytes (base64-round-tripped through the page).
        """
        import base64

        data = base64.b64encode(Path(file_path).read_bytes()).decode()
        filename = os.path.basename(file_path)
        prompt = await _find_prompt_locator(self.page, timeout_ms=30000)

        await prompt.evaluate(
            '''
            (el, { data, filename }) => {
                const binary = atob(data);
                const bytes = new Uint8Array(binary.length);
                for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
                const file = new File([bytes], filename);
                const dt = new DataTransfer();
                dt.items.add(file);
                const opts = { bubbles: true, cancelable: true, dataTransfer: dt };
                el.dispatchEvent(new DragEvent("dragenter", opts));
                el.dispatchEvent(new DragEvent("dragover", opts));
                el.dispatchEvent(new DragEvent("drop", opts));
                // Close out the drag state — without a matching dragleave/dragend,
                // the "drop files here" overlay stays up and can swallow the
                // Enter keypress meant to send the message.
                el.dispatchEvent(new DragEvent("dragleave", opts));
                el.dispatchEvent(new DragEvent("dragend", opts));
            }
            ''',
            {"data": data, "filename": filename},
        )

        # Wait for the file chip's upload-in-progress spinner to clear.
        async def _upload_done(budget_ms: int) -> bool:
            remaining = budget_ms
            while remaining > 0:
                uploading = await self.page.locator('[class*="animate-spin"], [aria-label*="uploading" i]').count()
                if not uploading:
                    return True
                await self.page.wait_for_timeout(500)
                remaining -= 500
            return False

        if await _upload_done(timeout_ms):
            return

        # Stuck — try dismissing whatever's blocking (e.g. the drop-zone
        # overlay staying up) and give it a shorter second window.
        if await _attempt_recovery(self.page) and await _upload_done(15000):
            return

        shot_path = _debug_path("debug_attach_timeout.png")
        try:
            await self.page.screenshot(path=shot_path)
        except Exception:
            pass
        raise Exception(f"Timed out waiting for file upload to finish. Screenshot: {shot_path}")

    async def _send_prompt(self, message: str):
        last_error = None
        for _ in range(3):
            prompt = await _find_prompt_locator(self.page, timeout_ms=30000)
            try:
                await prompt.fill(message, timeout=5000)
                await prompt.press("Enter", timeout=5000)
            except Exception as e:
                last_error = e
                continue
            await self.page.wait_for_timeout(250)

            # ponytail: Enter doesn't always submit right after a drag-drop
            # attach — the composer's internal "attachment ready" state can
            # lag the visible DOM when the file arrived via synthetic events
            # instead of a real user drop. If the text is still sitting
            # there unsent, fall back to clicking the actual send button.
            still_there = (await prompt.inner_text()).strip() == message.strip()
            if still_there:
                send_button = self.page.locator('[data-testid="send-button"], button[aria-label*="send" i]').first
                if await send_button.count() and await send_button.is_visible():
                    await send_button.click()
                    await self.page.wait_for_timeout(250)

            return await _dismiss_rate_limit_if_present(self.page, "debug_rate_limit_send.png")
        raise Exception(f"Failed to fill the ChatGPT prompt input after 3 attempts (input kept going stale): {last_error}")

    async def stream_message(self, message: str, raw_output: bool = False, timeout_ms: int = 480000):
        elements_before = await self.page.query_selector_all('div[data-message-author-role="assistant"]')
        count_before = len(elements_before)

        rate_limit_message = await self._send_prompt(message)

        async def _wait_for_message() -> bool:
            waited = 0
            while waited < 30000:
                nonlocal rate_limit_message
                rate_limit_message = await _dismiss_rate_limit_if_present(self.page, "debug_rate_limit_text_wait.png") or rate_limit_message
                current_count = len(await self.page.query_selector_all('div[data-message-author-role="assistant"]'))
                if current_count > count_before:
                    return True
                await self.page.wait_for_timeout(500)
                waited += 500
            return False

        if not await _wait_for_message():
            # Stuck — try a deterministic recovery (dismiss whatever's
            # blocking, e.g. a leftover overlay) and resend once before
            # giving up. Covers exactly the class of stuck state hit
            # repeatedly during development today (a stale drop-zone
            # overlay swallowing the send).
            if await _attempt_recovery(self.page):
                rate_limit_message = await self._send_prompt(message) or rate_limit_message
                sent_ok = await _wait_for_message()
            else:
                sent_ok = False

            if not sent_ok:
                shot_path = _debug_path("debug_send.png")
                try:
                    await self.page.screenshot(path=shot_path)
                except Exception:
                    pass
                if rate_limit_message:
                    raise ChatGPTRateLimitError(f"ChatGPT rate limit dialog was dismissed, but no assistant response appeared: {rate_limit_message}. Screenshot: {shot_path}")
                raise Exception(f"Timeout waiting for assistant message to appear (recovery attempted). Screenshot: {shot_path}")

        last_text = ""
        stable_count = 0
        elapsed = 0
        while True:
            if elapsed >= timeout_ms:
                shot_path = _debug_path("debug_text_timeout.png")
                try:
                    await self.page.screenshot(path=shot_path)
                except Exception:
                    pass
                raise Exception(f"Timed out waiting for the response to finish streaming after {timeout_ms}ms. Screenshot: {shot_path}")
            await self.page.wait_for_timeout(500)
            elapsed += 500
            rate_limit_message = await _dismiss_rate_limit_if_present(self.page, "debug_rate_limit_text_stream.png") or rate_limit_message
            elements = await self.page.query_selector_all('div[data-message-author-role="assistant"]')
            if not elements:
                continue

            current_text = await elements[-1].inner_text()

            if current_text == last_text and current_text.strip() != "":
                 stable_count += 1
            else:
                 stable_count = 0

            last_text = current_text

            if stable_count >= 5:
                class_attr = await elements[-1].get_attribute("class")
                is_streaming = "result-streaming" in (class_attr or "")

                stop_btn = await self.page.query_selector('[data-testid="stop-button"]')
                aria_stop = await self.page.query_selector('[aria-label="Stop generating"]')

                stop_visible = await stop_btn.is_visible() if stop_btn else False
                aria_visible = await aria_stop.is_visible() if aria_stop else False

                if not is_streaming and not stop_visible and not aria_visible:
                    break
                else:
                    stable_count = 3

        elements = await self.page.query_selector_all('div[data-message-author-role="assistant"]')
        if not elements:
            raise Exception("Could not locate the response in the DOM.")

        last_response = elements[-1]

        if raw_output:
            raw_payload = await last_response.evaluate('''
                (el) => {
                    const markdownEl = el.querySelector('.markdown') || el;
                    const codeBlocks = [...markdownEl.querySelectorAll('pre code')]
                        .map(node => (node.innerText || node.textContent || '').trim())
                        .filter(Boolean);

                    if (codeBlocks.length === 1) {
                        return codeBlocks[0];
                    }

                    return (markdownEl.innerText || markdownEl.textContent || el.innerText || '').trim();
                }
            ''')
            final_raw = (raw_payload or "").strip()
            if rate_limit_message and not final_raw:
                raise ChatGPTRateLimitError(f"ChatGPT rate limit dialog was dismissed, but the assistant response was empty: {rate_limit_message}")

            yield {"type": "final", "content": final_raw, "sources": []}
            return

        html_payload = await last_response.evaluate('''
            (el) => {
                let cloned = el.cloneNode(true);

                cloned.querySelectorAll('svg').forEach(x => x.remove());

                let sources = [];
                let refs = cloned.querySelectorAll('.citation, a, button, sup');

                refs.forEach(node => {
                    let isCitation = node.tagName === 'BUTTON' || node.tagName === 'SUP' || (node.classList && node.classList.contains('citation'));
                    let isLink = node.tagName === 'A' && node.href;

                    if (isLink) {
                        let link = node.href;
                        if (link.startsWith('http') && !link.includes('chatgpt.com/c/')) {
                            if (!sources.includes(link)) {
                                sources.push(link);
                            }
                            if (node.classList.length > 2 || node.textContent.length < 25) {
                                isCitation = true;
                            }
                        }
                    }

                    if (isCitation) {
                        if (isLink) {
                            let num = sources.indexOf(node.href) + 1;
                            let span = document.createElement('span');
                            span.textContent = ` [${num}]`;
                            node.parentNode.replaceChild(span, node);
                        } else {
                            node.remove();
                        }
                    }
                });

                cloned.querySelectorAll('details, .search-results').forEach(x => x.remove());

                let markdownEl = cloned.querySelector('.markdown');
                let clean_html = markdownEl ? markdownEl.innerHTML : cloned.innerHTML;

                return {html: clean_html, sources: sources};
            }
        ''')

        from markdownify import markdownify
        final_markdown = markdownify(html_payload["html"], heading_style="ATX").strip()
        if rate_limit_message and not final_markdown:
            raise ChatGPTRateLimitError(f"ChatGPT rate limit dialog was dismissed, but the assistant response was empty: {rate_limit_message}")

        yield {"type": "final", "content": final_markdown, "sources": html_payload["sources"]}

    async def stream_image_message(self, message: str, timeout_ms: int = 480000):
        elements_before = await self.page.query_selector_all('div[data-message-author-role="assistant"]')
        count_before = len(elements_before)

        rate_limit_message = await self._send_prompt(message)

        appeared = False
        appear_wait = 0
        while appear_wait < 30000:
            rate_limit_message = await _dismiss_rate_limit_if_present(self.page, "debug_rate_limit_image_wait.png") or rate_limit_message
            current_count = len(await self.page.query_selector_all('div[data-message-author-role="assistant"]'))
            if current_count > count_before:
                appeared = True
                break
            await self.page.wait_for_timeout(500)
            appear_wait += 500
        if not appeared:
            shot_path = _debug_path("debug_image_send.png")
            await self.page.screenshot(path=shot_path)
            if rate_limit_message:
                raise ChatGPTRateLimitError(f"ChatGPT rate limit dialog was dismissed, but no image-gen response appeared: {rate_limit_message}. Screenshot: {shot_path}")
            raise Exception(f"Timeout waiting for assistant message to appear after image-gen prompt. Screenshot: {shot_path}")

        completed = False
        stable_after_done = 0
        last_url_count = 0
        elapsed = 0
        while elapsed < timeout_ms:
            await self.page.wait_for_timeout(2000)
            elapsed += 2000
            rate_limit_message = await _dismiss_rate_limit_if_present(self.page, "debug_rate_limit_image_stream.png") or rate_limit_message

            stop_btn = await self.page.query_selector('[data-testid="stop-button"]')
            aria_stop = await self.page.query_selector('[aria-label="Stop generating"]')
            stop_visible = False
            if stop_btn:
                try:
                    stop_visible = await stop_btn.is_visible()
                except Exception:
                    pass
            if not stop_visible and aria_stop:
                try:
                    stop_visible = await aria_stop.is_visible()
                except Exception:
                    pass

            urls = await self.page.evaluate('''
                () => {
                    const root = document.querySelector('main') || document.body;
                    const imgs = root.querySelectorAll('img');
                    const valid = [];
                    const seen = new Set();
                    const patterns = ['backend-api/estuary/content', 'oaiusercontent.com', 'backend-api/files', 'chatgpt.com/backend-api/'];
                    for (const img of imgs) {
                        const src = img.src || img.currentSrc || '';
                        if (!src.startsWith('http')) continue;
                        if (!img.complete) continue;
                        if (img.naturalWidth < 200) continue;
                        if (!patterns.some(p => src.includes(p))) continue;
                        const alt = (img.alt || '').toLowerCase();
                        if (!alt.includes('generated')) continue;
                        if (seen.has(src)) continue;
                        seen.add(src);
                        valid.push(src);
                    }
                    return valid;
                }
            ''')

            if not stop_visible:
                if len(urls) > 0 and len(urls) == last_url_count:
                    stable_after_done += 1
                    if stable_after_done >= 1:
                        completed = True
                        break
                elif len(urls) == 0 and last_url_count == 0:
                    stable_after_done += 1
                    if stable_after_done >= 2:
                        completed = True
                        break
                last_url_count = len(urls)
            else:
                stable_after_done = 0
                last_url_count = len(urls)

        if not completed:
            shot_path = _debug_path("debug_image_timeout.png")
            await self.page.screenshot(path=shot_path)
            if rate_limit_message:
                raise ChatGPTRateLimitError(f"ChatGPT rate limit dialog was dismissed, but image generation did not complete: {rate_limit_message}. Screenshot: {shot_path}")
            raise Exception(f"Image generation timed out after {timeout_ms}ms. Screenshot: {shot_path}")

        final_data = await self.page.evaluate('''
            () => {
                const root = document.querySelector('main') || document.body;
                const imgs = root.querySelectorAll('img');
                const valid_urls = [];
                const seen = new Set();
                const patterns = ['backend-api/estuary/content', 'oaiusercontent.com', 'backend-api/files', 'chatgpt.com/backend-api/'];
                for (const img of imgs) {
                    const src = img.src || img.currentSrc || '';
                    if (!src.startsWith('http')) continue;
                    if (!img.complete) continue;
                    if (img.naturalWidth < 200) continue;
                    if (!patterns.some(p => src.includes(p))) continue;
                    const alt = (img.alt || '').toLowerCase();
                    if (!alt.includes('generated')) continue;
                    if (seen.has(src)) continue;
                    seen.add(src);
                    valid_urls.push(src);
                }
                const lastMsg = [...document.querySelectorAll('div[data-message-author-role="assistant"]')].pop();
                let text = '';
                if (lastMsg) {
                    const cloned = lastMsg.cloneNode(true);
                    cloned.querySelectorAll('img, svg, button').forEach(x => x.remove());
                    text = (cloned.innerText || '').trim();
                }
                return { urls: valid_urls, text };
            }
        ''')
        if rate_limit_message and not final_data["urls"] and not final_data["text"]:
            raise ChatGPTRateLimitError(f"ChatGPT rate limit dialog was dismissed, but image generation returned no images or text: {rate_limit_message}")

        image_blobs = []
        for url in final_data['urls']:
            try:
                resp = await self.page.request.get(url)
                if resp.ok:
                    content_type = (resp.headers.get('content-type') or 'image/png').split(';')[0].strip()
                    image_blobs.append({
                        'bytes': await resp.body(),
                        'mime': content_type,
                        'url': url,
                    })
            except Exception:
                continue

        return {
            'images': image_blobs,
            'text': final_data['text'],
        }
