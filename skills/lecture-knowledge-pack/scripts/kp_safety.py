"""kp_safety.py - slide text is course material, never instructions. Rendering helpers that keep it inert.

    scan_injection(text) -> [pattern ids]   instruction-like text (EN/KO); informs only, never alters content
    fence(text, lang="text") -> str         fenced block whose backtick run is longer than any run inside
    fence_lang(s) -> str                    a safe info string for fence() (letters, digits, + # . - _)
    split_fences(text) -> (blocks, lines)   fenced block contents, and the lines outside fences and front matter
    code_span(s) -> str                     inline code whose backtick run is longer than any run inside
    md_inline(s) -> str                     slide or model text outside a fence: one line, never a link,
                                            instruction-like text as a code span
    md_cell(s) -> str                       md_inline for a table cell (keeps line breaks as <br>, escapes '|')
    esc_cell(s) -> str                      Markdown table cell ('|' -> '\\|' with the backslashes before it
                                            doubled, newlines -> '<br>')
    one_line(s) -> str                      headings / table titles: newlines collapsed
    fm_line(key, value) -> str              front-matter line, value as JSON (also valid YAML)

LLM-course slides legitimately show prompts such as "You are a helpful assistant"; the flag only tells the
tutor to keep treating the text as content. Nothing is removed.
"""
from __future__ import annotations

import json
import re

# Lecture prose describes programs ignoring commands ("셸은 그 명령을 무시한다", "the CPU ignores all instructions
# after the branch", "System: Ubuntu 22.04"); only addressed, imperative or chat-turn forms are flagged.
_KO_IMP = r"하라|해라|하세요|하십시오|하시오|해\s*줘|해\s*주세요|해(?=$|[\s.!,~])"
INJECTION_PATTERNS = [
    ("ignore_instructions", r"\b(ignore|disregard|forget)\b[^\n]{0,40}\b(previous|prior|above|earlier|preceding|your)\b"
                            r"[^\n]{0,20}\b(instructions?|prompts?|rules?|directions?|guidelines?)\b"),
    ("system_prompt", r"\bsystem\s+prompt\b"),
    ("role_tag", r"<\s*/?\s*(system|assistant|user|instructions?)\s*>"),
    ("role_prefix", r"(?im)^\s*(assistant\s*:|system\s*:\s*(you|ignore|disregard|forget|from\s+now|act|pretend|"
                    r"너는|당신은|넌|이제|이전|모든)\b)"),
    ("you_are_now", r"\byou\s+are\s+now\s+(a|an|my|dan|no\s+longer|free|unrestricted|jailbroken|"
                    r"in\s+developer\s+mode|acting|playing)\b"),
    ("new_instructions", r"\bnew\s+instructions?\s*:"),
    ("ko_ignore", r"(이전|위의|위에서|앞의|앞에서|기존|지금까지)[^\n]{0,8}(지시|지침|명령|프롬프트)[^\n]{0,6}"
                  r"(무시(하고|" + _KO_IMP + r")|잊어)"),
    ("ko_ignore_direct", r"(지시|지침|명령|프롬프트)(을|를|은|는)?\s*(모두\s*|전부\s*|다\s*)?"
                         r"(무시(" + _KO_IMP + r")|잊어(라|버려|줘))"),
    ("ko_system_prompt", r"시스템\s*프롬프트"),
    ("ko_you_are_now", r"(너는|당신은|넌)\s*이제"),
]
_COMPILED = [(pid, re.compile(rx, re.I)) for pid, rx in INJECTION_PATTERNS]
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,})[^`]*$")


def scan_injection(text: str) -> list:
    if not text:
        return []
    return sorted({pid for pid, rx in _COMPILED if rx.search(text)})


def fence(text: str, lang: str = "text") -> str:
    text = "" if text is None else str(text)
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text}\n{ticks}"


def fence_lang(s) -> str:
    return re.sub(r"[^\w+#.-]", "", "" if s is None else str(s))[:24] or "text"


def split_fences(text: str):
    """([content of every fenced block], [lines outside fences]); a leading '---' front matter is in neither.
    A block closes on a line of at least as many backticks as opened it (CommonMark), which fence() guarantees
    no content line can be."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    if lines and lines[0] == "---" and "---" in lines[1:]:
        i = lines.index("---", 1) + 1
    blocks, outside, cur, ticks = [], [], None, 0
    for line in lines[i:]:
        if cur is None:
            m = _FENCE_OPEN.match(line)
            if m:
                cur, ticks = [], len(m.group(1))
            else:
                outside.append(line)
        elif re.fullmatch(r" {0,3}`{%d,} *" % ticks, line):
            blocks.append("\n".join(cur))
            cur = None
        else:
            cur.append(line)
    if cur is not None:
        blocks.append("\n".join(cur))
    return blocks, outside


def code_span(s) -> str:
    s = one_line(s)
    longest = max((len(m) for m in re.findall(r"`+", s)), default=0)
    ticks = "`" * (longest + 1)
    pad = " " if s[:1] == "`" or s[-1:] == "`" else ""
    return f"{ticks}{pad}{s}{pad}{ticks}"


def md_inline(s) -> str:
    s = one_line(s)
    return code_span(s) if scan_injection(s) else s.replace("](", "]\\(")


def md_cell(s) -> str:
    s = "" if s is None else str(s)
    return esc_cell(code_span(s) if scan_injection(s) else s.replace("](", "]\\("))


def esc_cell(s) -> str:
    s = "" if s is None else str(s)
    s = re.sub(r"(\\*)\|", lambda m: m.group(1) * 2 + "\\|", s)
    return s.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def one_line(s) -> str:
    return re.sub(r"\s*[\r\n]\s*", " ", "" if s is None else str(s)).strip()


def fm_line(key: str, value) -> str:
    return f"{key}: {json.dumps(value, ensure_ascii=False)}"
