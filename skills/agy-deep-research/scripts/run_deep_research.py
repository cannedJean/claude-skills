import argparse
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

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

    // which research the extracted panel belongs to (read in the same evaluate as the report itself)
    const researchIdentity = () => ({
        chipTitles: Array.from(document.querySelectorAll('immersive-entry-chip .card-title'))
            .map(e => (e.innerText || '').trim().replace(/\s+/g, ' ')).filter(Boolean),
        panelTitle: ((document.querySelector('deep-research-immersive-panel h2.title-text, deep-research-immersive-panel .title-text') || {}).innerText || '')
            .trim().replace(/\s+/g, ' ')
    });

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

    return { markdown: md, sources: sourceLinks, ...researchIdentity() };
}
"""


def log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# --------------------------------------------------------------------------- conversation identity
# The Gemini desktop app has ONE page, shared by every driver process connected over CDP. Another job's driver
# (or the user) can move it to a different conversation at any time, so nothing is read, clicked or extracted
# unless the page is on this step's conversation, and the report panel shows this conversation's research.
# The same url parser lives in relay_step.py and deep_research.py (tests/test_conversation_identity.py keeps
# the three in step).
_CONVERSATION_PATH_RE = re.compile(r"(?:^|/)app/([A-Za-z0-9_-]+)(?:/|$)", re.IGNORECASE)
_GEMINI_HOSTS = ("gemini.google.com",)
PROVENANCE_PREFIX = "[REPORT_PROVENANCE] "
CONVERSATION_PREFIX = "[CONVERSATION] "
IDENTITY_GRACE_POLLS = 3   # consecutive polls a foreign report panel / missing research chip is tolerated
USER_QUERIES_JS = r"""() => Array.from(document.querySelectorAll('user-query'))
    .map(q => (q.innerText || '').trim().replace(/\s+/g, ' '))"""


class ConversationMismatch(RuntimeError):
    """The page shows another conversation (or another research's report panel) than this step's."""


def conversation_id_from_url(url):
    """'https://gemini.google.com/app/<id>' (also /u/<n>/app/<id>, ?query, #fragment, trailing /) -> '<id>', else None.
    Only the Gemini app's own host counts."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or (parts.hostname or "") not in _GEMINI_HOSTS:
        return None
    m = _CONVERSATION_PATH_RE.search(parts.path)
    return m.group(1).lower() if m else None


def _page_url(page):
    url = getattr(page, "url", "")
    return url if isinstance(url, str) else ""


def assert_conversation(page, expected_id, where):
    """Fail closed when the page is not on expected_id (nothing pinned yet -> no check). Returns the page's id."""
    actual = conversation_id_from_url(_page_url(page))
    if expected_id and actual != expected_id:
        raise ConversationMismatch(
            f"Conversation mismatch ({where}): expected {expected_id}, page is on {actual or _page_url(page) or 'unknown'}. "
            "Another job or the user moved the shared Gemini page; nothing from it was used.")
    return actual


def bind_conversation(page, resume_url):
    """Settle the page on the step's conversation and return its id. 'current' pins whatever conversation is open."""
    if resume_url == "current":
        cid = conversation_id_from_url(_page_url(page))
        if not cid:
            raise ConversationMismatch(
                f"Conversation mismatch (resume): --resume-url current, but the page is on no conversation ({_page_url(page) or 'unknown'}).")
        log(f"Using the conversation that is open: {cid}")
        return cid
    want = conversation_id_from_url(resume_url)
    if not want:
        raise ConversationMismatch(f"Conversation mismatch (resume): --resume-url names no conversation: {resume_url!r}")
    if conversation_id_from_url(_page_url(page)) != want:
        log(f"Navigating to conversation: {resume_url}")
        page.goto(resume_url, wait_until="domcontentloaded")
        time.sleep(2.5)
    assert_conversation(page, want, "after navigation")
    return want


def _norm_title(title):
    return " ".join(str(title or "").split()).casefold()


def check_research_identity(state, research_title):
    """-> (research_title, problem or None). The research is this conversation's when its chip is on the page and
    the open report panel shows that research. The first chip title seen on the verified conversation is pinned.
    States without title fields (older UI, no chip yet) are not judged."""
    chips = [c for c in (state.get("chipTitles") or []) if _norm_title(c)]
    panel = state.get("panelTitle") or ""
    if not research_title:
        if not chips:
            return research_title, None
        research_title = chips[-1]
    want = _norm_title(research_title)
    if chips and want not in {_norm_title(c) for c in chips}:
        return research_title, f"the conversation no longer shows the research '{research_title}' (chips on the page: {chips})"
    if _norm_title(panel) and _norm_title(panel) != want:
        return research_title, f"the report panel shows the research '{panel}', not this conversation's '{research_title}'"
    return research_title, None


def panel_unverified(state, research_title):
    """True while a report panel is open but this conversation shows no research chip to match it against: the panel
    lives in the app shell and may still show another conversation's research (older UI without title fields: False)."""
    if research_title or "chipTitles" not in state:
        return False
    chips = [c for c in (state.get("chipTitles") or []) if _norm_title(c)]
    return not chips and bool(_norm_title(state.get("panelTitle")))


def open_research_panel(page, research_title):
    """Click this conversation's research chip so the (global) report panel shows it."""
    try:
        chip = page.locator("immersive-entry-chip gem-processing-card", has_text=research_title).last
        if chip.count() > 0:
            chip.click(force=True)
            time.sleep(1.5)
    except Exception:
        pass


def format_report_provenance(result):
    """The stdout line the relay turns into result.json provenance; printed right before the report block."""
    return PROVENANCE_PREFIX + json.dumps({k: result.get(k) for k in ("conversation_id", "url", "report_title")}, ensure_ascii=False)


def format_conversation_line(conversation_id, url):
    """The stdout line the relay persists (relay/conversation.json) the moment the step's conversation is bound,
    so a later collect finds the research even when this run fails or dies."""
    return CONVERSATION_PREFIX + json.dumps({"conversation_id": conversation_id, "url": url}, ensure_ascii=False)


def assert_fresh_new_chat(page):
    """Right before a new chat's prompt is typed the page must be on no conversation (/app): otherwise the prompt
    would continue whatever conversation another job (or the user) left on the shared page."""
    cid = conversation_id_from_url(_page_url(page))
    if cid:
        raise ConversationMismatch(
            f"Conversation mismatch (new chat): the page is on conversation {cid}, not on a new chat, before the prompt "
            "was sent; nothing was typed.")


def _normalized_prompt(prompt_text):
    """The whole prompt, whitespace-collapsed: composed prompts share their opening template, so a head is not enough."""
    return " ".join(str(prompt_text or "").split())


def verify_new_chat_conversation(page, prompt_text, where="new chat"):
    """A new chat owns only the conversation its own prompt created: the page's conversation must hold exactly one
    user query, this run's prompt. Another job's conversation (the shared page moved before ours got its id) fails
    closed here, before anything on it is clicked. -> the conversation id"""
    cid = conversation_id_from_url(_page_url(page))
    if not cid:
        raise ConversationMismatch(f"Conversation mismatch ({where}): the page has no conversation id ({_page_url(page) or 'unknown'}).")
    queries = evaluate_with_navigation_retry(page, USER_QUERIES_JS) or []
    assert_conversation(page, cid, where)
    full = _normalized_prompt(prompt_text)
    if len(queries) != 1 or (full and full not in queries[0]):
        first = (queries[0] if queries else "")[:80]
        raise ConversationMismatch(
            f"Conversation mismatch ({where}): conversation {cid} is not this run's new chat "
            f"({len(queries)} user queries; first: {first!r}); nothing on it was touched.")
    return cid


REQUIRED_XML_TAGS = (
    "research_objective",
    "context_and_scope",
    "investigation_axes",
    "source_governance",
    "output_format",
)


def has_full_xml_prompt_structure(text: str) -> bool:
    import re
    clean = (text or "").strip()
    return all(
        re.search(rf"<{tag}\b[^>]*>.*?</{tag}>", clean, re.S | re.I)
        for tag in REQUIRED_XML_TAGS
    )


def build_structured_xml_prompt(
    objective: str,
    context_scope: str | None = None,
    axes: list[str] | None = None,
    sources_policy: str | None = None,
    output_spec: str | None = None,
    workspace_sources: str | None = None,
) -> str:
    """Wrap a topic/objective into the 5-block XML Deep Research architecture."""
    import re
    obj_clean = (objective or "").strip()
    if not re.match(
        r"^(?:compare|analyze|synthesize|evaluate|investigate|conduct|design|benchmark|분석|비교|종합|평가|조사|검증)",
        obj_clean,
        re.I,
    ):
        obj_block = (
            "Conduct a comprehensive, multi-dimensional technical analysis and comparative synthesis on the following topic:\n"
            f"{obj_clean}"
        )
    else:
        obj_block = obj_clean

    ctx_lines = []
    if context_scope and str(context_scope).strip():
        ctx_lines.append(str(context_scope).strip())
    else:
        ctx_lines.append(
            "- Scope: Self-contained multi-source investigation covering 2022–2026 production architectures, peer-reviewed literature, and empirical benchmarks.\n"
            "- Exclusions: Skip introductory textbook definitions and superficial marketing overview pages. Do not ask clarifying questions; execute the investigation directly under these declared scope bounds."
        )
    if workspace_sources and str(workspace_sources).strip():
        ctx_lines.append(
            f"- Google Workspace Data Layer Integration: Cross-reference authenticated Google Workspace entities (Drive documents/folders, Gmail threads, Google Chat) matching: {str(workspace_sources).strip()}"
        )
    ctx_block = "\n".join(ctx_lines)

    if axes:
        axes_items = [str(x).strip() for x in axes if str(x).strip()]
    else:
        axes_items = [
            "1. Core Architecture & Algorithmic Mechanics: Analyze underlying data structures, system invariants, and implementation trade-offs.",
            "2. Empirical Performance & Resource Benchmarks: Compare throughput, tail latency (p50/p95/p99), compute/memory footprint, and scalability limits.",
            "3. Operational Resilience & Edge Cases: Evaluate failure modes, recovery protocols, security/compliance implications, and production bottlenecks.",
        ]
    axes_numbered = []
    for idx, item in enumerate(axes_items, 1):
        if re.match(r"^\d+[\.)]\s+", item):
            axes_numbered.append(item)
        else:
            axes_numbered.append(f"{idx}. {item}")
    axes_numbered.append(
        "Conditional Depth-First Trigger: When primary sources or benchmarks report conflicting quantitative metrics, trace the underlying hardware specifications, dataset distributions, and measurement methodologies in the primary text to explain the discrepancy."
    )
    axes_block = "\n".join(axes_numbered)

    if sources_policy and str(sources_policy).strip():
        custom_src = str(sources_policy).strip()
        if "검증 가능한 공개 근거 불충분" not in custom_src:
            src_block = (
                f"{custom_src}\n"
                "- Epistemic Calibration: Strictly separate confirmed empirical facts from architectural hypotheses. If verified public evidence is unavailable for any metric or claim, explicitly state '검증 가능한 공개 근거 불충분 (Insufficient verified evidence)' rather than extrapolating or fabricating numbers."
            )
        else:
            src_block = custom_src
    else:
        src_block = (
            "- Prioritize Tier-1 primary sources: peer-reviewed papers (with DOI/arXiv links), official engineering specifications, standard bodies, and reproducible benchmark repositories.\n"
            "- Do not rely on unverified secondary blogs or vendor marketing claims as sole evidence.\n"
            "- Provide numbered inline citations with publication/verification dates and direct URLs.\n"
            "- Epistemic Calibration: Strictly separate confirmed empirical facts from architectural hypotheses. If verified public evidence is unavailable for any metric or claim, explicitly state '검증 가능한 공개 근거 불충분 (Insufficient verified evidence)' rather than extrapolating or fabricating numbers."
        )

    if output_spec and str(output_spec).strip():
        out_block = (
            f"{str(output_spec).strip()}\n"
            "- Mandatory Gate Invariant: Output MUST be valid GitHub-Flavored Markdown (GFM) of at least 2,500 characters (strictly >= 400 characters) with explicit top-level `#` and section `##` Markdown headings. If structured JSON is requested, embed it inside a fenced ```json code block under a dedicated `##` Markdown section."
        )
    else:
        out_block = (
            "- Format: Comprehensive GitHub-Flavored Markdown (GFM) technical report (>= 2,500 characters; strictly >= 400 characters).\n"
            "- Mandatory Heading Hierarchy:\n"
            "  # [Report Title]\n"
            "  ## 1. Executive Summary & Architectural Verdict\n"
            "  ## 2. Deep-Dive Technical Analysis by Investigation Axis\n"
            "  ## 3. Quantitative Comparison Matrix (Markdown Table with fixed metric columns)\n"
            "  ## 4. Risk Analysis, Edge Cases & Production Recommendations\n"
            "  ## 참고 출처 (Sources)"
        )

    return (
        f"<research_objective>\n{obj_block}\n</research_objective>\n\n"
        f"<context_and_scope>\n{ctx_block}\n</context_and_scope>\n\n"
        f"<investigation_axes>\n{axes_block}\n</investigation_axes>\n\n"
        f"<source_governance>\n{src_block}\n</source_governance>\n\n"
        f"<output_format>\n{out_block}\n</output_format>"
    )


def validate_edit_instructions(edit_prompt: str, allow_questions: bool = False) -> str:
    import re
    clean = (edit_prompt or "").strip()
    if not clean:
        raise ValueError("--edit-prompt cannot be empty.")
    if not allow_questions:
        patterns = (
            re.compile(r"\?\s*$", re.M),
            re.compile(r"^\s*(?:why|how\s+come|what\s+is\s+the\s+reason|can\s+you\s+explain|could\s+you\s+explain)\b", re.I | re.M),
            re.compile(r"(?:왜\s+[^\n]*?(?:인가요|입니까|했나요|선택했나요|포함했나요)|이유가\s+무엇|설명해\s*주세|답변해\s*주세)", re.M),
        )
        for pat in patterns:
            if pat.search(clean):
                raise ValueError(
                    "Edit feedback contains conversational questions (e.g. '?'). Asking questions during plan editing "
                    "causes Gemini to reply with prose instead of generating a revised Research Plan card (PLAN_CHANGED / PLAN_TIMEOUT). "
                    "Use imperative delta instructions only (Add, Remove, Reorder, Pin)."
                )
    return clean


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


_SHOW_THINKING_MARKERS = ("과정 표시", "생각 표시", "show thinking", "hide thinking", "과정 숨기기")


def is_thinking_level_control(label):
    """True only for the composer's thinking-level picker, never for the per-response
    'show thinking' toggle, which shares 'thinking'/'생각' in its label."""
    low = (label or "").strip().lower()
    if not low or any(m in low for m in _SHOW_THINKING_MARKERS):
        return False
    return any(k in low for k in ("thinking level", "사고 수준", "high", "medium", "low", "높음", "중간", "보통", "낮음"))


def log_current_model_mode(page):
    """Read-only: report the model shown in an existing conversation. Never clicks.
    The model was locked when the chat was created (plan/auto); a reload may display the
    app default (e.g. 'Flash'), and switching it now would alter the conversation."""
    want = os.environ.get("GEMINI_DR_MODE", "Pro").strip() or "Pro"
    shown = ""
    try:
        for sel in ('[data-test-id*="mode-menu"]', 'button[aria-label*="Model selector"]',
                    'button[aria-label*="모델 선택"]', '[data-test-id="bard-mode-menu-button"]',
                    'bard-mode-switcher button', 'gem-menu-button button'):
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                shown = str(loc.inner_text() or loc.get_attribute("aria-label") or "").strip()
                break
    except Exception as e:
        log(f"Notice: model mode read skipped ({e})")
    log(f"[MODEL_OBSERVED] shown='{shown}' locked_target='{want}' (read-only; menu not touched)")
    if shown and want.lower() not in shown.lower():
        log(f"Warning: composer shows '{shown}' but job was locked to '{want}' at plan time. "
            "Not switching mid-conversation; verify the conversation's model in the app.")
    return shown


def ensure_pro_mode(page):
    """Ensure the wanted model mode and thinking level are selected. Default 'Pro' / 'high';
    GEMINI_DR_MODE selects model, GEMINI_DR_THINKING selects reasoning intensity."""
    target = os.environ.get("GEMINI_DR_MODE", "Pro").strip() or "Pro"
    thinking_target = os.environ.get("GEMINI_DR_THINKING", "high").strip() or "high"
    mode_selectors = [
        '[data-test-id*="mode-menu"]',
        'button[aria-label*="Model selector"]',
        'button[aria-label*="모델 선택"]',
        'gem-menu-button button',
        'gem-menu-button',
        'button[aria-label*="Gemini"]',
        '[data-test-id="bard-mode-menu-button"]',
        'bard-mode-switcher button',
    ]
    combined_selector = ", ".join(mode_selectors)
    try:
        page.wait_for_selector(combined_selector, timeout=10000)
    except Exception:
        pass

    mode_btn = None
    for sel in mode_selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible():
                mode_btn = loc
                break
        except Exception:
            continue

    if not mode_btn:
        mode_btn = page.locator(combined_selector).first

    if mode_btn.count() > 0:
        current_mode = str(mode_btn.inner_text() or mode_btn.get_attribute("aria-label") or "").strip()
        if target.lower() not in current_mode.lower():
            log(f"Current model mode is '{current_mode}'. Switching to '{target}'...")
            mode_btn.evaluate("el => el.click()")
            time.sleep(0.8)
            switched = page.evaluate(
                """(target) => {
                    const targetLower = target.toLowerCase();
                    const items = Array.from(document.querySelectorAll(
                        'button[role="menuitemradio"], button[role="menuitem"], [role="menuitemradio"], [role="menuitem"], mat-option'
                    ));
                    const proItem = items.find(el => {
                        const txt = (el.innerText || '').trim().toLowerCase();
                        return txt.includes(targetLower) && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
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
                new_mode = str(mode_btn.inner_text() or mode_btn.get_attribute("aria-label") or "").strip()
                log(f"Switched model mode to: {new_mode}")
            else:
                log(f"Warning: Could not find enabled '{target}' option in mode menu.")
                page.keyboard.press("Escape")
        else:
            log(f"Model mode already set to: {current_mode}")

    # Select thinking level if UI controls are present
    thinking_selectors = [
        '[data-test-id*="thinking"]',
        'button[aria-label*="Thinking"]',
        'button[aria-label*="사고"]',
        'button[aria-label*="thinking"]',
        '[aria-label*="Thinking level"]',
        '[aria-label*="사고 수준"]',
    ]
    try:
        thinking_btn = None
        for sel in thinking_selectors:
            loc = page.locator(sel).first
            try:
                if loc.count() > 0 and loc.is_visible():
                    label = str(loc.inner_text() or loc.get_attribute("aria-label") or "")
                    if not is_thinking_level_control(label):
                        continue  # e.g. the '생각하는 과정 표시' toggle on a response
                    thinking_btn = loc
                    break
            except Exception:
                continue

        if thinking_btn and thinking_btn.count() > 0:
            current_thinking = str(thinking_btn.inner_text() or thinking_btn.get_attribute("aria-label") or "").strip()
            thinking_map = {
                "high": ["high", "높음"],
                "medium": ["medium", "중간", "보통"],
                "low": ["low", "낮음"],
            }
            targets = thinking_map.get(thinking_target.lower(), [thinking_target.lower()])
            already_match = any(t in current_thinking.lower() for t in targets)
            if not already_match:
                log(f"Current thinking level is '{current_thinking}'. Switching to '{thinking_target}'...")
                thinking_btn.evaluate("el => el.click()")
                time.sleep(0.8)
                switched_th = page.evaluate(
                    """(targets) => {
                        const items = Array.from(document.querySelectorAll(
                            'button[role="menuitemradio"], button[role="menuitem"], [role="menuitemradio"], [role="menuitem"], mat-option, [role="option"]'
                        ));
                        const matchItem = items.find(el => {
                            const txt = (el.innerText || '').trim().toLowerCase();
                            return targets.some(t => txt.includes(t)) && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
                        });
                        if (matchItem) {
                            (matchItem.querySelector('button') || matchItem).click();
                            return true;
                        }
                        return false;
                    }""", targets
                )
                if switched_th:
                    time.sleep(0.5)
                    log(f"Switched thinking level to: {thinking_target}")
                else:
                    log(f"Notice: Thinking option '{thinking_target}' not found in menu.")
                    page.keyboard.press("Escape")
            else:
                log(f"Thinking level already set to: {current_thinking}")
    except Exception as e:
        log(f"Notice: Thinking level selection check skipped ({e})")


def activate_deep_research_mode(page):
    page.wait_for_selector('[role="textbox"]', timeout=30000)
    ensure_pro_mode(page)

    deselect_btn = page.locator(
        '[data-test-id="deselect-drawer-item-gem-button"], button[aria-label*="Deep Research 선택 해제"], button[aria-label*="Deselect Deep Research"]'
    )
    dr_enabled_btn = page.locator(
        'button:is(:has-text("Deep Research"), :has-text("심층 리서치"), :has-text("딥리서치")):not([aria-label*="선택 해제"]):not([aria-label*="Deselect"]):not([disabled]):not([aria-disabled="true"])'
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

    send_btn = page.locator(
        'button[aria-label="메시지 보내기"], button[aria-label*="Send"], '
        'button[aria-label*="보내기"], [data-test-id*="send"], '
        'button:has(mat-icon[fonticon="send"]), button:has(mat-icon[data-mat-icon-name="send"]), '
        'button:has(mat-icon[data-mat-icon-name="arrow_upward"])'
    ).first
    submitted = False
    if send_btn.count() > 0 and send_btn.is_visible():
        try:
            send_btn.click(force=True)
            time.sleep(0.5)
            submitted = True
        except Exception:
            pass

    # Ensure Angular/Quill form submission fires if real click failed or input remains
    uq = page.locator("user-query")
    if not submitted or uq.count() == 0 or (textbox.count() > 0 and (textbox.inner_text() or "").strip()):
        textbox.focus()
        page.keyboard.press("Enter")
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


def is_generation_in_progress(page):
    try:
        stop_btn = page.locator(
            'button[aria-label*="중지"], button[aria-label*="Stop"], '
            'button[aria-label*="대답 생성 중지"], [data-mat-icon-name="stop"]'
        ).first
        if stop_btn.count() > 0 and stop_btn.is_visible():
            return True
        thinking = page.locator('thinking-overlay, [data-test-id="thinking"]')
        if thinking.count() > 0 and thinking.first.is_visible():
            return True
        return False
    except Exception:
        return False


def wait_for_new_plan(page, prev_plan_count=0, timeout_sec=300, new_chat_prompt=None):
    """Wait until a new Research Plan card with an active confirm button appears. The conversation is pinned the
    first time the url carries an id (a new chat gets one after the prompt is sent); the plan is read only there.
    For a new chat (new_chat_prompt) that conversation must hold this run's prompt before the plan is touched."""
    start_time = time.time()
    last_reload_at = 0
    conversation_id = conversation_id_from_url(_page_url(page))
    while time.time() - start_time < timeout_sec:
        elapsed = time.time() - start_time
        if conversation_id:
            assert_conversation(page, conversation_id, "plan wait")
        else:
            conversation_id = conversation_id_from_url(_page_url(page))

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
            '[data-test-id="confirm-button"]:not(.gem-button-disabled), '
            'button[aria-label="연구 시작"]:not([disabled]), '
            'button[aria-label="Start research"]:not([disabled]), '
            'button:has-text("연구 시작"):not([disabled]), '
            'button:has-text("Start research"):not([disabled])'
        )

        if current_plan_count > prev_plan_count and active_confirm.count() > 0:
            time.sleep(0.8)
            # identity before the countdown cancel / plan expand: never touch another job's plan card
            if new_chat_prompt is not None:
                conversation_id = verify_new_chat_conversation(page, new_chat_prompt, "plan read")
            else:
                assert_conversation(page, conversation_id, "plan read")
            plan_text = stop_auto_confirm_fuse_and_expand_plan(page)
            assert_conversation(page, conversation_id, "plan read")
            return {
                "status": "plan_ready",
                "url": page.url,
                "plan": plan_text,
                "plan_count": current_plan_count,
            }

        # When Gemini.exe runs hidden (--hide), revised Research Plan widgets on follow-up turns
        # may stay visually on '조사 계획 생성 중' until the conversation page is reloaded.
        # Do not reload while generation/thinking is actively in progress!
        generating = is_generation_in_progress(page)
        if not generating and elapsed >= 30 and elapsed - last_reload_at >= 45 and "/app/" in page.url:
            last_reload_at = elapsed
            log(f"Refreshing conversation state ({int(elapsed)}s elapsed, generation idle)...")
            try:
                page.reload(wait_until="domcontentloaded")
                prevent_background_throttling(page)
                page.wait_for_selector("model-response", timeout=15000)
                time.sleep(4.0)
            except Exception as e:
                log(f"Reload recovery notice: {e}")
            continue

        time.sleep(1.5)

    raise TimeoutError(f"Research Plan confirmation button did not appear within {timeout_sec}s.")


def apply_plan_edit(page, edit_feedback, timeout_sec=300, conversation_id=None):
    """Click '계획 수정' (edit-button), wait for edit prompt readiness, submit modification instructions, and wait for the revised plan.
    With conversation_id, the page must be on that conversation before the click and before the feedback is typed."""
    log_current_model_mode(page)  # model is locked at plan time; never switch mid-conversation
    assert_conversation(page, conversation_id, "before edit click")
    prev_plan_count = page.locator('[data-test-id="research-steps"], .research-steps').count()
    prev_resp_count = page.locator("model-response").count()

    edit_selectors = [
        '[data-test-id="edit-button"]:not(.gem-button-disabled) button:not([disabled])',
        '[data-test-id="edit-button"]:not(.gem-button-disabled)',
        'button[aria-label="Edit plan"]:not([disabled])',
        'button[aria-label="연구 계획 수정"]:not([disabled])',
        '[data-test-id="edit-button"]',
        'button:has-text("계획 수정"):not([disabled])',
        'button:has-text("Edit plan"):not([disabled])',
    ]
    edit_btn = page.locator(", ".join(edit_selectors)).last

    if edit_btn.count() > 0:
        log("Clicking '계획 수정' (edit-button)...")
        edit_btn.evaluate("el => el.click()")
        # Clicking '계획 수정' triggers a canned turn ("연구 계획 수정" -> "현재 계획이에요. 변경하고 싶은 부분이 있나요?")
        # Wait for that intermediate response to complete (.response-footer.complete) before submitting our modification prompt.
        wait_for_response_complete(page, expected_min_responses=prev_resp_count + 1, timeout_sec=45)
        time.sleep(0.8)

    assert_conversation(page, conversation_id, "before typing feedback")
    log(f"Submitting plan modification request: {edit_feedback}")
    submit_prompt(page, edit_feedback, raw_prompt=True)
    log("Waiting for revised Research Plan generation...")
    return wait_for_new_plan(page, prev_plan_count=prev_plan_count, timeout_sec=timeout_sec)


def confirm_current_plan(page):
    """Click '연구 시작' (confirm-button) on the latest Research Plan."""
    confirm_selectors = [
        '[data-test-id="confirm-button"]:not(.gem-button-disabled) button:not([disabled])',
        '[data-test-id="confirm-button"]:not(.gem-button-disabled)',
        'button[aria-label="연구 시작"]:not([disabled])',
        'button[aria-label="Start research"]:not([disabled])',
        'button:has-text("연구 시작"):not([disabled])',
        'button:has-text("Start research"):not([disabled])',
    ]
    confirm_btn = page.locator(", ".join(confirm_selectors)).last
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


_NAVIGATION_ERROR_MARKERS = ("Execution context was destroyed", "most likely because of a navigation",
                             "Cannot find context with specified id", "Target page, context or browser has been closed")


def evaluate_with_navigation_retry(page, script, arg=None, attempts=5):
    """page.evaluate that tolerates the transient error raised when a poll lands while the
    page is reloading/navigating (our own 60s freeze-recovery reload or Gemini's own)."""
    for i in range(attempts):
        try:
            return page.evaluate(script, arg) if arg is not None else page.evaluate(script)
        except Exception as e:
            if not any(m in str(e) for m in _NAVIGATION_ERROR_MARKERS) or i == attempts - 1:
                raise
            log(f"Page navigating during evaluate; retrying ({i + 1}/{attempts - 1})...")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            time.sleep(2.0)


def wait_for_report_completion(page, timeout_sec=1200, expected_conversation_id=None):
    """Wait for this conversation's Deep Research report and extract it. Fails closed (ConversationMismatch) when the
    shared page leaves the conversation or the report panel shows another research; without expected_conversation_id
    the conversation open at the start is pinned."""
    log("Deep Research is running in the background. Monitoring progress...")
    conversation_id = expected_conversation_id or conversation_id_from_url(_page_url(page))
    research_title = ""
    identity_misses = 0
    start_time = time.time()
    last_status = ""
    stable_ticks = 0
    last_len = 0
    last_reload_time = time.time()

    while time.time() - start_time < timeout_sec:
        assert_conversation(page, conversation_id, "before poll")
        state = evaluate_with_navigation_retry(page, r"""() => {
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

            const chipTitles = Array.from(document.querySelectorAll('immersive-entry-chip .card-title'))
                .map(e => (e.innerText || '').trim().replace(/\s+/g, ' ')).filter(Boolean);
            const panelTitleEl = document.querySelector('deep-research-immersive-panel h2.title-text, deep-research-immersive-panel .title-text');
            const panelTitle = panelTitleEl ? (panelTitleEl.innerText || '').trim().replace(/\s+/g, ' ') : '';

            return {
                isCardCompleted,
                hasWorkingIndicator: !!workingEl,
                statusText,
                chipText,
                isChipStillWriting,
                hasImmersiveChip: !!immersiveChipBtn,
                hasImmersivePanel: !!immersivePanel,
                reportTextLen,
                chipTitles,
                panelTitle
            };
        }""")
        assert_conversation(page, conversation_id, "after poll")

        pinned_before = research_title
        research_title, problem = check_research_identity(state, research_title)
        if research_title and not pinned_before:
            log(f"Watching research '{research_title}' in conversation {conversation_id or 'unknown'}")
        if problem:
            identity_misses += 1
            if identity_misses >= IDENTITY_GRACE_POLLS:
                raise ConversationMismatch(f"Conversation mismatch (report panel) in conversation {conversation_id}: {problem}")
            log(f"Notice: {problem}; reopening this conversation's research ({identity_misses}/{IDENTITY_GRACE_POLLS - 1})...")
            stable_ticks = 0
            open_research_panel(page, research_title)
            time.sleep(2.0)
            continue
        identity_misses = 0
        # never count a panel this conversation's chip cannot vouch for; keep waiting (and recovering) for the chip
        unverified = panel_unverified(state, research_title)

        status_line = f"{state['chipText']} | {state['statusText']}".strip(" |")
        if status_line and status_line != last_status:
            last_status = status_line
            elapsed = int(time.time() - start_time)
            log(f"[{elapsed}s] Progress: {status_line}")

        if state["hasImmersiveChip"] and not state["hasImmersivePanel"]:
            if research_title:
                open_research_panel(page, research_title)
            else:
                try:
                    chip = page.locator(
                        'immersive-entry-chip gem-processing-card, button[data-test-id="immersive-entry-chip-view-report-button"], immersive-entry-chip'
                    ).first
                    if chip.count() > 0:
                        chip.click(force=True)
                        time.sleep(1.5)
                except Exception:
                    pass

        if not unverified and state["reportTextLen"] > 400 and (state["isCardCompleted"] or not state["isChipStillWriting"]):
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
            if time.time() - last_reload_time > 60:
                last_reload_time = time.time()
                log("Refreshing conversation state from server (60s background freeze recovery)...")
                try:
                    page.reload(wait_until="domcontentloaded")
                    prevent_background_throttling(page)
                except Exception as e:
                    log(f"Reload recovery notice: {e}")
                time.sleep(3.0)

        time.sleep(2.0)
    else:
        raise TimeoutError(f"Deep Research did not finish within {timeout_sec} seconds.")

    assert_conversation(page, conversation_id, "before extraction")
    extracted = evaluate_with_navigation_retry(page, HTML_TO_MARKDOWN_JS)
    assert_conversation(page, conversation_id, "after extraction")
    research_title, problem = check_research_identity(extracted, research_title)
    if not problem and panel_unverified(extracted, research_title):
        problem = "the report panel is open but this conversation shows no research chip to match it"
    if problem:
        raise ConversationMismatch(f"Conversation mismatch (extraction) in conversation {conversation_id}: {problem}")
    markdown = extracted.get("markdown", "").strip()
    sources = extracted.get("sources", [])

    if sources:
        markdown += "\n\n---\n\n## 참고 출처 (Sources)\n\n"
        for idx, src in enumerate(sources, 1):
            markdown += f"{idx}. [{src['title']}]({src['url']})\n"

    return {
        "status": "completed",
        "url": page.url,
        "conversation_id": conversation_id,
        "report_title": research_title or extracted.get("panelTitle") or "",
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
    parser.add_argument(
        "--compose-xml",
        action="store_true",
        help="Automatically wrap a plain topic/prompt into the 5-block XML Deep Research architecture (<research_objective>, <context_and_scope>, <investigation_axes>, <source_governance>, <output_format>).",
    )
    parser.add_argument("--context-scope", type=str, help="Custom <context_and_scope> content when --compose-xml is used.")
    parser.add_argument("--axes", action="append", help="Investigation axis item(s) for <investigation_axes> (repeatable).")
    parser.add_argument("--sources-policy", type=str, help="Custom <source_governance> rules when --compose-xml is used.")
    parser.add_argument("--output-spec", type=str, help="Custom <output_format> rules when --compose-xml is used.")
    parser.add_argument("--workspace-sources", type=str, help="Google Workspace (Drive/Gmail/Chat) entities to include in <context_and_scope>.")
    parser.add_argument("--allow-questions", action="store_true", help="Bypass interrogative question check in --edit-prompt.")
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
    if args.step == "edit" and args.edit_prompt:
        try:
            args.edit_prompt = validate_edit_instructions(args.edit_prompt, allow_questions=bool(args.allow_questions))
        except ValueError as e:
            parser.error(str(e))
    if args.step in ("auto", "plan") and not args.prompt and not args.resume_url:
        parser.error("Either --prompt or --resume-url must be provided.")
    if args.prompt and args.compose_xml and not args.raw_prompt and not has_full_xml_prompt_structure(args.prompt):
        args.prompt = build_structured_xml_prompt(
            objective=args.prompt,
            context_scope=args.context_scope,
            axes=args.axes,
            sources_policy=args.sources_policy,
            output_spec=args.output_spec,
            workspace_sources=args.workspace_sources,
        )
        log("Composed 5-block XML Deep Research prompt.")

    app_info = ensure_gemini_running_with_cdp(port=CDP_PORT, hide=not args.show)
    log(f"Local Gemini app ready via CDP ({app_info['cdp_url']}, status={app_info['status']})")

    if sync_playwright is None:
        raise RuntimeError("Playwright is not installed. Run: pip install playwright")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(app_info["cdp_url"])
        page = get_gemini_page(browser)
        prevent_background_throttling(page)

        # Conversation identity: an existing conversation is bound before anything is read or clicked;
        # a new chat (plan / auto) is bound once Gemini gives it an id (wait_for_new_plan).
        conversation_id = bind_conversation(page, args.resume_url) if args.resume_url else None
        if conversation_id:
            print(format_conversation_line(conversation_id, _page_url(page)), flush=True)

        # Model lock: the model/thinking level is selected exactly once, in the new chat
        # (activate_deep_research_mode -> ensure_pro_mode) before the prompt is sent.
        # On an existing conversation (approve / collect / edit) only observe it.
        if args.resume_url:
            log_current_model_mode(page)

        # Step mode 1: Edit an existing plan and return the revised plan to the caller agent
        if args.step == "edit":
            revised = apply_plan_edit(page, args.edit_prompt, conversation_id=conversation_id)
            assert_conversation(page, conversation_id, "after edit")
            print("\n=== [PLAN_REVIEW_HOOK_START] ===", flush=True)
            print(json.dumps({"event": "on_plan_revised", **revised}, ensure_ascii=False, indent=2), flush=True)
            print("=== [PLAN_REVIEW_HOOK_END] ===\n", flush=True)
            return

        confirm_locators = (
            '[data-test-id="confirm-button"]:not(.gem-button-disabled) button:not([disabled]), '
            '[data-test-id="confirm-button"]:not(.gem-button-disabled), '
            'button[aria-label="연구 시작"]:not([disabled]), '
            'button[aria-label="Start research"]:not([disabled]), '
            'button:has-text("연구 시작"):not([disabled]), '
            'button:has-text("Start research"):not([disabled])'
        )

        # Step mode 2: Approve the currently displayed plan and execute Deep Research
        if args.step == "approve":
            confirm_btn = page.locator(confirm_locators).last
            if confirm_btn.count() > 0:
                assert_conversation(page, conversation_id, "before start click")
                confirm_current_plan(page)
        elif args.resume_url:
            confirm_btn = page.locator(confirm_locators).last
            if confirm_btn.count() > 0:
                assert_conversation(page, conversation_id, "before start click")
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
            new_chat_clicked = False
            new_chat_loc = page.locator('button[aria-label*="새 채팅"], button[aria-label*="New chat"], a[aria-label*="새 채팅"], a[aria-label*="New chat"]')
            for i in range(new_chat_loc.count()):
                el = new_chat_loc.nth(i)
                try:
                    if el.is_visible():
                        log("Clicking '새 채팅' (New chat)...")
                        el.click()
                        time.sleep(2.0)
                        new_chat_clicked = True
                        break
                except Exception:
                    pass

            if not new_chat_clicked and page.url.rstrip("/") != "https://gemini.google.com/app":
                log("Opening new chat in local Gemini app...")
                page.goto("https://gemini.google.com/app", wait_until="domcontentloaded")
                time.sleep(3.0)
            activate_deep_research_mode(page)
            assert_fresh_new_chat(page)
            submit_prompt(page, args.prompt, raw_prompt=args.raw_prompt)
            plan_res = wait_for_new_plan(page, prev_plan_count=0, new_chat_prompt=args.prompt)
            new_conversation_id = conversation_id_from_url(plan_res.get("url"))
            if new_conversation_id:   # verified to hold this run's prompt: persist the binding now
                print(format_conversation_line(new_conversation_id, plan_res.get("url")), flush=True)

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

            # The new chat is this run's conversation from here on (auto): pin it before starting the research
            conversation_id = conversation_id_from_url(plan_res.get("url"))
            if not conversation_id:
                raise ConversationMismatch(
                    f"Conversation mismatch (new chat): the plan's page has no conversation id ({plan_res.get('url')!r}); not starting the research.")
            assert_conversation(page, conversation_id, "before start click")

            # If --review-plan or --plan-hook is enabled, enter the interactive verification/revision loop
            if args.review_plan or args.plan_hook:
                run_plan_review_hook_loop(page, plan_res, plan_hook_cmd=args.plan_hook)
            else:
                confirm_current_plan(page)

        result = wait_for_report_completion(page, timeout_sec=args.timeout, expected_conversation_id=conversation_id)
        if args.output:
            out_path = os.path.abspath(args.output)
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(result["markdown"])
            log(f"Saved Markdown report ({len(result['markdown'])} chars) to: {out_path}")

        # provenance first, then the report: the relay accepts a report only with this line from the same run
        print(format_report_provenance(result), flush=True)
        print("\n=== DEEP RESEARCH REPORT START ===\n")
        print(result["markdown"])
        print("\n=== DEEP RESEARCH REPORT END ===\n")


if __name__ == "__main__":
    main()
