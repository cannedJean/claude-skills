import argparse
import json
import os
import subprocess
import sys
import time
from playwright.sync_api import sync_playwright

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from patch_gemini_app import CDP_PORT, ensure_gemini_running_with_cdp


HTML_TO_MARKDOWN_JS = r"""
(rootSelector) => {
    const findReportRoot = () => {
        const immersiveMsg = document.querySelector(
            'deep-research-immersive-panel message-content, ' +
            'deep-research-immersive-panel [data-test-id="message-content"], ' +
            'deep-research-immersive-panel .markdown, ' +
            '.deep-research-content .markdown'
        );
        if (immersiveMsg && immersiveMsg.innerText.trim().length > 200) return immersiveMsg;
        const responses = Array.from(document.querySelectorAll('model-response message-content .markdown, model-response message-content'));
        if (responses.length > 0) return responses[responses.length - 1];
        return null;
    };

    const root = rootSelector ? document.querySelector(rootSelector) : findReportRoot();
    if (!root) return { markdown: '', sources: [] };

    const convertNode = (node, listDepth = 0) => {
        if (!node) return '';
        if (node.nodeType === Node.TEXT_NODE) {
            return node.nodeValue || '';
        }
        if (node.nodeType !== Node.ELEMENT_NODE) return '';

        const tag = node.tagName.toLowerCase();
        if (['script', 'style', 'mat-icon', 'gem-icon', 'button', 'remy-processing-state', 'thinking-overlay'].includes(tag)) {
            return '';
        }

        const childrenText = () => Array.from(node.childNodes).map(c => convertNode(c, listDepth)).join('');

        switch (tag) {
            case 'h1': return `\n\n# ${childrenText().trim()}\n\n`;
            case 'h2': return `\n\n## ${childrenText().trim()}\n\n`;
            case 'h3': return `\n\n### ${childrenText().trim()}\n\n`;
            case 'h4': return `\n\n#### ${childrenText().trim()}\n\n`;
            case 'h5': return `\n\n##### ${childrenText().trim()}\n\n`;
            case 'h6': return `\n\n###### ${childrenText().trim()}\n\n`;
            case 'p': return `\n\n${childrenText().trim()}\n\n`;
            case 'br': return '\n';
            case 'hr': return '\n\n---\n\n';
            case 'strong':
            case 'b': {
                const t = childrenText().trim();
                return t ? `**${t}**` : '';
            }
            case 'em':
            case 'i': {
                const t = childrenText().trim();
                return t ? `*${t}*` : '';
            }
            case 'code': {
                if (node.parentElement && node.parentElement.tagName.toLowerCase() === 'pre') {
                    return node.innerText || '';
                }
                const t = childrenText().trim();
                return t ? `\`${t}\`` : '';
            }
            case 'pre': {
                const codeEl = node.querySelector('code');
                const raw = (codeEl ? codeEl.innerText : node.innerText) || '';
                return `\n\n\`\`\`\n${raw.trim()}\n\`\`\`\n\n`;
            }
            case 'blockquote': {
                const lines = childrenText().trim().split('\n').map(l => `> ${l}`).join('\n');
                return `\n\n${lines}\n\n`;
            }
            case 'a': {
                const href = node.getAttribute('href') || '';
                const text = childrenText().trim() || href;
                if (href && href.startsWith('http')) {
                    return `[${text}](${href})`;
                }
                return text;
            }
            case 'ul': {
                const items = Array.from(node.children).filter(c => c.tagName.toLowerCase() === 'li').map(li => {
                    const indent = '  '.repeat(listDepth);
                    return `${indent}- ${convertNode(li, listDepth + 1).trim()}`;
                }).join('\n');
                return `\n${items}\n`;
            }
            case 'ol': {
                const items = Array.from(node.children).filter(c => c.tagName.toLowerCase() === 'li').map((li, idx) => {
                    const indent = '  '.repeat(listDepth);
                    return `${indent}${idx + 1}. ${convertNode(li, listDepth + 1).trim()}`;
                }).join('\n');
                return `\n${items}\n`;
            }
            case 'li': {
                return childrenText().replace(/^\n+|\n+$/g, '');
            }
            case 'table': {
                const rows = Array.from(node.querySelectorAll('tr'));
                if (rows.length === 0) return '';
                const matrix = rows.map(r =>
                    Array.from(r.querySelectorAll('th, td')).map(cell =>
                        (cell.innerText || '').trim().replace(/\s+/g, ' ').replace(/\|/g, '\\|')
                    )
                ).filter(r => r.length > 0);
                if (matrix.length === 0) return '';
                const colCount = Math.max(...matrix.map(r => r.length));
                const norm = matrix.map(r => {
                    while (r.length < colCount) r.push('');
                    return `| ${r.join(' | ')} |`;
                });
                const sep = `| ${Array(colCount).fill('---').join(' | ')} |`;
                norm.splice(1, 0, sep);
                return `\n\n${norm.join('\n')}\n\n`;
            }
            default:
                return childrenText();
        }
    };

    let md = convertNode(root)
        .replace(/\n{3,}/g, '\n\n')
        .trim();

    const sourceLinks = [];
    const seenUrls = new Set();
    const linkEls = Array.from(document.querySelectorAll('deep-research-immersive-panel a[href^="http"], sources-list a[href^="http"], [data-test-id*="source"] a[href^="http"], model-response a[href^="http"]'));
    for (const a of linkEls) {
        const href = a.getAttribute('href');
        if (!href || href.includes('google.com/search') || href.includes('support.google.com') || seenUrls.has(href)) continue;
        seenUrls.add(href);
        const title = (a.innerText || a.getAttribute('aria-label') || href).trim().replace(/\s+/g, ' ');
        sourceLinks.push({ title: title || href, url: href });
    }

    return { markdown: md, sources: sourceLinks };
}
"""


def log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def get_gemini_page(browser, timeout_sec=15):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        for ctx in browser.contexts:
            for pg in ctx.pages:
                if "gemini.google.com" in pg.url:
                    return pg
        time.sleep(0.5)
    raise RuntimeError("Could not find a gemini.google.com page in the local Gemini app.")


def prevent_background_throttling(page):
    try:
        cdp = page.context.new_cdp_session(page)
        cdp.send("Page.enable")
        cdp.send("Page.setWebLifecycleState", {"state": "active"})
        cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
    except Exception as e:
        log(f"Note: CDP focus emulation setup warning (non-fatal): {e}")


def ensure_pro_mode(page):
    """Ensure the wanted model mode is selected. Default 'Pro' (e.g. '3.8 Pro'); GEMINI_DR_MODE=Argon selects 'Argon'."""
    target = os.environ.get("GEMINI_DR_MODE", "Pro").strip() or "Pro"
    try:
        page.wait_for_selector('[data-test-id="bard-mode-menu-button"], bard-mode-switcher button', timeout=10000)
    except Exception:
        pass
    mode_btn = page.locator('[data-test-id="bard-mode-menu-button"], bard-mode-switcher button').first
    if mode_btn.count() > 0:
        current_mode = mode_btn.inner_text().strip()
        if target not in current_mode:
            log(f"Current model mode is '{current_mode}'. Switching to '{target}'...")
            mode_btn.evaluate("el => el.click()")
            time.sleep(0.8)
            switched = page.evaluate(
                """(target) => {
                    const items = Array.from(document.querySelectorAll(
                        'button[role="menuitemradio"], button[role="menuitem"], [role="menuitemradio"], [role="menuitem"], mat-option'
                    ));
                    const proItem = items.find(el => {
                        const txt = (el.innerText || '').trim();
                        return txt.includes(target) && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
                    });
                    if (proItem) {
                        (proItem.querySelector('button') || proItem).click();
                        return true;
                    }
                    return false;
                }""", target
            )
            if switched:
                time.sleep(1.0)
                log(f"Switched model mode to: {mode_btn.inner_text().strip()}")
            else:
                log(f"Warning: Could not find enabled '{target}' option in mode menu.")
                page.keyboard.press("Escape")
        else:
            log(f"Model mode already set to: {current_mode}")


def activate_deep_research_mode(page):
    page.wait_for_selector('[role="textbox"]', timeout=30000)
    ensure_pro_mode(page)

    deselect_btn = page.locator(
        '[data-test-id="deselect-drawer-item-gem-button"], button[aria-label*="Deep Research 선택 해제"], button[aria-label*="Deselect Deep Research"]'
    )
    dr_enabled_btn = page.locator(
        'button:has-text("Deep Research"):not([aria-label*="선택 해제"]):not([aria-label*="Deselect"]):not([disabled]):not([aria-disabled="true"])'
    )
    if deselect_btn.count() == 0 or not deselect_btn.first.is_visible():
        log("Activating Deep Research mode...")
        tools_selector = 'button[aria-label="업로드 및 도구"], button[aria-label="Upload and tools"]'
        page.wait_for_selector(tools_selector, timeout=20000)
        tools_btn = page.locator(tools_selector).first

        for _ in range(5):
            if deselect_btn.count() > 0 and deselect_btn.first.is_visible():
                break

            more_btn = page.locator('[data-test-id="more-tools-button"]').first
            dr_ready = dr_enabled_btn.count() > 0 and dr_enabled_btn.first.is_visible()
            more_visible = more_btn.count() > 0 and more_btn.is_visible()

            if not dr_ready and not more_visible:
                tools_btn.evaluate("el => el.click()")
                time.sleep(1.0)
                more_visible = more_btn.count() > 0 and more_btn.is_visible()

            if more_visible and not (dr_enabled_btn.count() > 0 and dr_enabled_btn.first.is_visible()):
                more_btn.evaluate("el => el.click()")
                time.sleep(1.0)

            if dr_enabled_btn.count() > 0 and dr_enabled_btn.first.is_visible():
                dr_enabled_btn.first.evaluate("el => el.click()")
                for _ in range(6):
                    time.sleep(0.5)
                    if deselect_btn.count() > 0 and deselect_btn.first.is_visible():
                        break
                if deselect_btn.count() > 0 and deselect_btn.first.is_visible():
                    log("Deep Research mode enabled successfully.")
                    break

            time.sleep(1.0)
        else:
            raise RuntimeError("Could not activate 'Deep Research' mode from the Tools menu.")
    else:
        log("Deep Research mode is already active.")


def submit_prompt(page, prompt_text, raw_prompt=False):
    mode_str = "exact/raw prompt" if raw_prompt else "prompt"
    log(f"Entering {mode_str}: {prompt_text[:80]}{'...' if len(prompt_text) > 80 else ''}")
    textbox = page.locator('[role="textbox"]').first
    textbox.click()
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    # Insert exact prompt string without any modification
    page.keyboard.insert_text(prompt_text)
    time.sleep(0.6)

    send_btn = page.locator('button[aria-label="메시지 보내기"], button[aria-label*="Send"]').first
    if send_btn.count() > 0:
        send_btn.evaluate("el => el.click()")
    else:
        textbox.press("Enter")
    log("Prompt submitted.")


def stop_auto_confirm_fuse_and_expand_plan(page):
    """Stop any countdown fuse on the confirmation card and expand '더보기' in research-steps."""
    cancel_fuse = page.locator('[data-test-id="cancel-fuse-button"] button, [data-test-id="cancel-fuse-button"]').last
    if cancel_fuse.count() > 0 and cancel_fuse.is_visible():
        try:
            cancel_fuse.click(force=True)
            time.sleep(0.3)
        except Exception:
            pass

    plan_el = page.locator('[data-test-id="research-steps"], .research-steps').last
    if plan_el.count() > 0:
        more_btns = plan_el.locator('button:has-text("더보기"), gem-button:has-text("더보기"), button:has-text("More")')
        for i in range(more_btns.count()):
            try:
                more_btns.nth(i).click(force=True)
                time.sleep(0.2)
            except Exception:
                pass
        return plan_el.inner_text().strip()
    return ""


def wait_for_response_complete(page, expected_min_responses, timeout_sec=60):
    """Wait until model-response count >= expected_min_responses and the last response has .response-footer.complete."""
    start = time.time()
    while time.time() - start < timeout_sec:
        done = page.evaluate(
            """(minResp) => {
                const resps = Array.from(document.querySelectorAll('model-response'));
                if (resps.length < minResp) return false;
                const last = resps[resps.length - 1];
                const footerComplete = !!last.querySelector('.response-footer.complete');
                const md = last.querySelector('.markdown');
                const ariaBusy = md ? md.getAttribute('aria-busy') : 'true';
                return footerComplete && ariaBusy === 'false';
            }""",
            expected_min_responses,
        )
        if done:
            return True
        time.sleep(0.8)
    return False


def wait_for_new_plan(page, prev_plan_count=0, timeout_sec=180):
    """Wait until a new Research Plan card with an active confirm button appears."""
    start_time = time.time()
    last_reload_at = 0
    while time.time() - start_time < timeout_sec:
        elapsed = time.time() - start_time

        resp_el = page.locator("model-response").last
        if resp_el.count() > 0:
            resp_txt = resp_el.inner_text().strip()
            if "도와드릴 수가 없습니다" in resp_txt or "단지 언어 모델일 뿐" in resp_txt:
                raise RuntimeError(
                    f"Gemini Web safety/keyword filter blocked the prompt: '{resp_txt}'. "
                    "Avoid sensitive political/election keywords (e.g., '민주주의', '선거', '정치')."
                )

        current_plan_count = page.locator('[data-test-id="research-steps"], .research-steps').count()
        active_confirm = page.locator(
            '[data-test-id="confirm-button"]:not(.gem-button-disabled) button:not([disabled]), '
            'button[aria-label="연구 시작"]:not([disabled])'
        )

        if current_plan_count > prev_plan_count and active_confirm.count() > 0:
            time.sleep(0.8)
            plan_text = stop_auto_confirm_fuse_and_expand_plan(page)
            return {
                "status": "plan_ready",
                "url": page.url,
                "plan": plan_text,
                "plan_count": current_plan_count,
            }

        # When Gemini.exe runs hidden (--hide), revised Research Plan widgets on follow-up turns
        # may stay visually on '조사 계획 생성 중' until the conversation page is reloaded.
        # Wait at least 60s before reloading so we never abort an in-flight StreamGenerate request.
        if elapsed - last_reload_at >= 60 and "/app/" in page.url:
            last_reload_at = elapsed
            log(f"Refreshing conversation state ({int(elapsed)}s elapsed)...")
            page.reload(wait_until="domcontentloaded")
            try:
                page.wait_for_selector("model-response", timeout=15000)
            except Exception:
                pass
            time.sleep(3.0)
            continue

        time.sleep(1.5)

    raise TimeoutError(f"Research Plan confirmation button did not appear within {timeout_sec}s.")


def apply_plan_edit(page, edit_feedback, timeout_sec=180):
    """Click '계획 수정' (edit-button), wait for edit prompt readiness, submit modification instructions, and wait for the revised plan."""
    ensure_pro_mode(page)
    prev_plan_count = page.locator('[data-test-id="research-steps"], .research-steps').count()
    prev_resp_count = page.locator("model-response").count()

    edit_btn = page.locator(
        '[data-test-id="edit-button"]:not(.gem-button-disabled) button:not([disabled]), '
        '[data-test-id="edit-button"]:not(.gem-button-disabled), '
        'button[aria-label="연구 계획 수정"]:not([disabled])'
    ).last

    if edit_btn.count() > 0:
        log("Clicking '계획 수정' (edit-button)...")
        edit_btn.evaluate("el => el.click()")
        # Clicking '계획 수정' triggers a canned turn ("연구 계획 수정" -> "현재 계획이에요. 변경하고 싶은 부분이 있나요?")
        # Wait for that intermediate response to complete (.response-footer.complete) before submitting our modification prompt.
        wait_for_response_complete(page, expected_min_responses=prev_resp_count + 1, timeout_sec=45)
        time.sleep(0.8)

    log(f"Submitting plan modification request: {edit_feedback}")
    submit_prompt(page, edit_feedback, raw_prompt=True)
    log("Waiting for revised Research Plan generation...")
    return wait_for_new_plan(page, prev_plan_count=prev_plan_count, timeout_sec=timeout_sec)


def confirm_current_plan(page):
    """Click '연구 시작' (confirm-button) on the latest Research Plan."""
    confirm_btn = page.locator(
        '[data-test-id="confirm-button"]:not(.gem-button-disabled) button:not([disabled]), '
        '[data-test-id="confirm-button"]:not(.gem-button-disabled), '
        'button[aria-label="연구 시작"]:not([disabled])'
    ).last
    if confirm_btn.count() == 0:
        raise RuntimeError("No active '연구 시작' (confirm-button) found on the current page.")
    log("Clicking '연구 시작' (Confirm Plan) to start Deep Research execution...")
    confirm_btn.evaluate("el => el.click()")
    time.sleep(2.0)


def parse_hook_decision(raw_response):
    """
    Parse decision from hook stdout or caller agent stdin.
    Accepted formats:
      - JSON: {"action": "approve"} or {"action": "edit", "feedback": "..."}
      - Text: "APPROVE" / "OK" / "YES"
      - Text: "EDIT: <modification instructions>"
      - Any other non-empty string is treated as modification instructions ("edit").
    """
    text = (raw_response or "").strip()
    if not text:
        return {"action": "approve", "feedback": ""}

    if text.startswith("{") and text.endswith("}"):
        try:
            obj = json.loads(text)
            act = str(obj.get("action", "")).lower().strip()
            fb = str(obj.get("feedback") or obj.get("edit_prompt") or obj.get("plan") or "").strip()
            if act in ("approve", "approved", "confirm", "start", "ok", "yes"):
                return {"action": "approve", "feedback": ""}
            return {"action": "edit", "feedback": fb}
        except Exception:
            pass

    upper = text.upper()
    if upper in ("APPROVE", "APPROVED", "CONFIRM", "START", "OK", "YES", "승인"):
        return {"action": "approve", "feedback": ""}

    if upper.startswith("EDIT:"):
        return {"action": "edit", "feedback": text[5:].strip()}
    if text.startswith("수정:"):
        return {"action": "edit", "feedback": text[3:].strip()}

    return {"action": "edit", "feedback": text}


def run_plan_review_hook_loop(page, initial_plan_info, plan_hook_cmd=None, timeout_sec=120):
    """
    Hook & Loop executed when a Research Plan is provided:
    1. Emits the generated plan to the caller agent (or executes --plan-hook command).
    2. Receives verification result:
       - If modification instructions are returned -> modifies the plan in Gemini, gets revised plan, and repeats verification.
       - If approved -> exits the loop and starts Deep Research execution.
    """
    current_plan = initial_plan_info
    iteration = 1

    while True:
        payload = {
            "event": "on_plan_generated",
            "iteration": iteration,
            "url": current_plan["url"],
            "plan": current_plan["plan"],
        }

        print("\n=== [PLAN_REVIEW_HOOK_START] ===", flush=True)
        print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
        print("=== [PLAN_REVIEW_HOOK_END] ===\n", flush=True)

        if plan_hook_cmd:
            log(f"Executing plan hook command (iteration {iteration}): {plan_hook_cmd}")
            proc = subprocess.run(
                plan_hook_cmd,
                input=json.dumps(payload, ensure_ascii=False),
                text=True,
                encoding="utf-8",
                shell=True,
                capture_output=True,
            )
            if proc.returncode != 0:
                log(f"Warning: plan hook exited with code {proc.returncode}: {proc.stderr}")
            decision = parse_hook_decision(proc.stdout)
        else:
            log(
                f"[Iteration {iteration}] Waiting for caller agent plan review on stdin "
                "(Send 'APPROVE' to start research, or 'EDIT: <수정 내용>' to revise the plan)..."
            )
            line = sys.stdin.readline()
            if not line:
                raise RuntimeError(
                    "Stdin closed while waiting for plan review decision. "
                    "Use manage_task(Action='send_input', Input='APPROVE\\n') or '--step plan/edit/approve'."
                )
            decision = parse_hook_decision(line)

        if decision["action"] == "approve":
            log(f"Plan approved at iteration {iteration}! Exiting review loop and starting research.")
            confirm_current_plan(page)
            return current_plan

        feedback = decision["feedback"]
        if not feedback:
            log("Empty edit feedback received; treating as approval.")
            confirm_current_plan(page)
            return current_plan

        log(f"[Iteration {iteration}] Plan revision requested by caller agent: {feedback}")
        current_plan = apply_plan_edit(page, feedback, timeout_sec=timeout_sec)
        iteration += 1


def wait_for_report_completion(page, timeout_sec=1200):
    log("Deep Research is running in the background. Monitoring progress...")
    start_time = time.time()
    last_status = ""
    stable_ticks = 0
    last_len = 0
    last_reload_time = time.time()

    while time.time() - start_time < timeout_sec:
        state = page.evaluate(r"""() => {
            const isCardCompleted = !!document.querySelector('immersive-entry-chip gem-processing-card.completed');
            const workingEl = document.querySelector('[data-test-id="remy-processing-state-agent-working"], remy-processing-state');

            const thoughtHeaders = Array.from(document.querySelectorAll('[data-test-id="thought-header"]')).map(e => (e.innerText || '').trim()).filter(Boolean);
            const thoughtBodies = Array.from(document.querySelectorAll('[data-test-id="thought-body"]')).map(e => (e.innerText || '').trim()).filter(Boolean);
            const latestThought = thoughtHeaders.length > 0
                ? `${thoughtHeaders[thoughtHeaders.length - 1]}: ${(thoughtBodies[thoughtBodies.length - 1] || '').slice(0, 80)}`
                : (thoughtBodies[thoughtBodies.length - 1] || '').slice(0, 100);

            const statusTextEl = document.querySelector('[data-test-id="remy-processing-state-text"], [data-test-id="processing-state-container-button"], [data-test-id="status-update-text"]');
            const statusText = (statusTextEl ? (statusTextEl.innerText || '').trim().replace(/\s+/g, ' ') : '') || latestThought;

            const chipEl = document.querySelector('immersive-entry-chip');
            const chipText = chipEl ? (chipEl.innerText || '').trim().replace(/\s+/g, ' ') : '';
            const isChipStillWriting = !isCardCompleted && (chipText.includes('작성 중') || chipText.includes('생성 중') || chipText.includes('Writing'));

            const immersiveChipBtn = document.querySelector('immersive-entry-chip gem-processing-card, button[data-test-id="immersive-entry-chip-view-report-button"], immersive-entry-chip [data-test-id="open-button"] button, immersive-entry-chip');
            const immersiveMsg = document.querySelector(
                'deep-research-immersive-panel message-content, ' +
                'deep-research-immersive-panel [data-test-id="message-content"], ' +
                'deep-research-immersive-panel .markdown, ' +
                '.deep-research-content .markdown'
            );
            const immersivePanel = document.querySelector('deep-research-immersive-panel');

            let reportTextLen = 0;
            if (immersiveMsg) {
                reportTextLen = (immersiveMsg.innerText || '').trim().length;
            } else if (immersivePanel && isCardCompleted) {
                reportTextLen = (immersivePanel.innerText || '').trim().length;
            }

            return {
                isCardCompleted,
                hasWorkingIndicator: !!workingEl,
                statusText,
                chipText,
                isChipStillWriting,
                hasImmersiveChip: !!immersiveChipBtn,
                hasImmersivePanel: !!immersivePanel,
                reportTextLen
            };
        }""")

        status_line = f"{state['chipText']} | {state['statusText']}".strip(" |")
        if status_line and status_line != last_status:
            last_status = status_line
            elapsed = int(time.time() - start_time)
            log(f"[{elapsed}s] Progress: {status_line}")

        if state["hasImmersiveChip"] and not state["hasImmersivePanel"]:
            try:
                chip = page.locator(
                    'immersive-entry-chip gem-processing-card, button[data-test-id="immersive-entry-chip-view-report-button"], immersive-entry-chip'
                ).first
                if chip.count() > 0:
                    chip.click(force=True)
                    time.sleep(1.5)
            except Exception:
                pass

        if state["reportTextLen"] > 400 and (state["isCardCompleted"] or not state["isChipStillWriting"]):
            if state["reportTextLen"] == last_len:
                stable_ticks += 1
            else:
                stable_ticks = 0
                last_len = state["reportTextLen"]

            if stable_ticks >= 1:
                log(f"Deep Research report complete! ({state['reportTextLen']} chars)")
                break
        else:
            stable_ticks = 0
            last_len = state["reportTextLen"]
            if time.time() - last_reload_time > 90:
                last_reload_time = time.time()
                log("Refreshing conversation state from server...")
                page.reload(wait_until="domcontentloaded")
                time.sleep(3.0)

        time.sleep(2.0)
    else:
        raise TimeoutError(f"Deep Research did not finish within {timeout_sec} seconds.")

    extracted = page.evaluate(HTML_TO_MARKDOWN_JS, None)
    markdown = extracted.get("markdown", "").strip()
    sources = extracted.get("sources", [])

    if sources:
        markdown += "\n\n---\n\n## 참고 출처 (Sources)\n\n"
        for idx, src in enumerate(sources, 1):
            markdown += f"{idx}. [{src['title']}]({src['url']})\n"

    return {
        "status": "completed",
        "url": page.url,
        "markdown": markdown,
        "sources_count": len(sources),
    }


def main():
    parser = argparse.ArgumentParser(description="Run Deep Research using the local Windows Gemini desktop app.")
    parser.add_argument("--prompt", type=str, help="Research prompt/topic to investigate.")
    parser.add_argument("--prompt-file", type=str, help="Path to a UTF-8 file containing the exact research prompt.")
    parser.add_argument(
        "--raw-prompt",
        action="store_true",
        help="Use the exact prompt string provided at invocation time without any modification.",
    )
    parser.add_argument("--output", type=str, help="Optional file path to save the resulting Markdown report.")
    parser.add_argument(
        "--review-plan",
        action="store_true",
        help="Enable the Plan Review Hook loop: returns the generated plan to the caller agent for verification/revision before starting research.",
    )
    parser.add_argument(
        "--plan-hook",
        type=str,
        help="Optional shell command to run when a plan is generated. Receives plan JSON on stdin and outputs 'APPROVE' or 'EDIT: <instructions>'.",
    )
    parser.add_argument(
        "--step",
        choices=["auto", "plan", "edit", "approve"],
        default="auto",
        help=(
            "Step-by-step loop control for caller agents: "
            "'plan' (generate plan and return to caller), "
            "'edit' (modify existing plan with --edit-prompt and return revised plan), "
            "'approve' (confirm current plan and execute Deep Research), "
            "'auto' (default full flow)."
        ),
    )
    parser.add_argument(
        "--edit-prompt",
        type=str,
        help="Modification instructions when using '--step edit'.",
    )
    parser.add_argument(
        "--edit-prompt-file",
        type=str,
        help="Path to a UTF-8 file containing modification instructions when using '--step edit'.",
    )
    parser.add_argument("--plan-only", action="store_true", help="Alias for '--step plan'.")
    parser.add_argument("--resume-url", type=str, help="Resume an existing Deep Research conversation URL (or 'current').")
    parser.add_argument("--timeout", type=int, default=1200, help="Max wait time in seconds for research completion (default: 1200).")
    parser.add_argument("--show", action="store_true", help="Show the local Gemini desktop window instead of running hidden.")
    args = parser.parse_args()

    if args.prompt_file:
        with open(args.prompt_file, "r", encoding="utf-8") as f:
            args.prompt = f.read().strip()

    if args.edit_prompt_file:
        with open(args.edit_prompt_file, "r", encoding="utf-8") as f:
            args.edit_prompt = f.read().strip()

    if args.plan_only:
        args.step = "plan"

    if args.step == "edit" and not args.edit_prompt:
        parser.error("--edit-prompt is required when --step edit is used.")
    if args.step in ("auto", "plan") and not args.prompt and not args.resume_url:
        parser.error("Either --prompt or --resume-url must be provided.")

    app_info = ensure_gemini_running_with_cdp(port=CDP_PORT, hide=not args.show)
    log(f"Local Gemini app ready via CDP ({app_info['cdp_url']}, status={app_info['status']})")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(app_info["cdp_url"])
        page = get_gemini_page(browser)
        prevent_background_throttling(page)

        if args.resume_url and args.resume_url != "current" and page.url != args.resume_url:
            log(f"Navigating to conversation: {args.resume_url}")
            page.goto(args.resume_url, wait_until="domcontentloaded")
            time.sleep(2.5)

        ensure_pro_mode(page)

        # Step mode 1: Edit an existing plan and return the revised plan to the caller agent
        if args.step == "edit":
            revised = apply_plan_edit(page, args.edit_prompt)
            print("\n=== [PLAN_REVIEW_HOOK_START] ===", flush=True)
            print(json.dumps({"event": "on_plan_revised", **revised}, ensure_ascii=False, indent=2), flush=True)
            print("=== [PLAN_REVIEW_HOOK_END] ===\n", flush=True)
            return

        # Step mode 2: Approve the currently displayed plan and execute Deep Research
        if args.step == "approve":
            confirm_btn = page.locator('[data-test-id="confirm-button"]:not(.gem-button-disabled) button:not([disabled])').last
            if confirm_btn.count() > 0:
                confirm_current_plan(page)
        elif args.resume_url:
            confirm_btn = page.locator('[data-test-id="confirm-button"]:not(.gem-button-disabled) button:not([disabled])').last
            if confirm_btn.count() > 0:
                if args.review_plan or args.plan_hook:
                    plan_text = stop_auto_confirm_fuse_and_expand_plan(page)
                    run_plan_review_hook_loop(
                        page,
                        {"status": "plan_ready", "url": page.url, "plan": plan_text},
                        plan_hook_cmd=args.plan_hook,
                    )
                else:
                    confirm_current_plan(page)
        else:
            # Start a new Deep Research chat
            if page.url.rstrip("/") != "https://gemini.google.com/app":
                log("Opening new chat in local Gemini app...")
                page.goto("https://gemini.google.com/app", wait_until="domcontentloaded")
                time.sleep(3.0)
            activate_deep_research_mode(page)
            submit_prompt(page, args.prompt, raw_prompt=args.raw_prompt)
            plan_res = wait_for_new_plan(page, prev_plan_count=0)

            print("\n--- [Research Plan] ---\n" + plan_res["plan"] + "\n-----------------------\n", flush=True)

            # If --step plan, emit the hook payload and return immediately to caller agent
            if args.step == "plan":
                print("\n=== [PLAN_REVIEW_HOOK_START] ===", flush=True)
                print(json.dumps({"event": "on_plan_generated", **plan_res}, ensure_ascii=False, indent=2), flush=True)
                print("=== [PLAN_REVIEW_HOOK_END] ===\n", flush=True)
                if args.output:
                    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
                    with open(args.output, "w", encoding="utf-8") as f:
                        f.write(f"# Research Plan\n\nURL: {plan_res['url']}\n\n{plan_res['plan']}\n")
                return

            # If --review-plan or --plan-hook is enabled, enter the interactive verification/revision loop
            if args.review_plan or args.plan_hook:
                run_plan_review_hook_loop(page, plan_res, plan_hook_cmd=args.plan_hook)
            else:
                confirm_current_plan(page)

        result = wait_for_report_completion(page, timeout_sec=args.timeout)
        if args.output:
            out_path = os.path.abspath(args.output)
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(result["markdown"])
            log(f"Saved Markdown report ({len(result['markdown'])} chars) to: {out_path}")

        print("\n=== DEEP RESEARCH REPORT START ===\n")
        print(result["markdown"])
        print("\n=== DEEP RESEARCH REPORT END ===\n")


if __name__ == "__main__":
    main()
