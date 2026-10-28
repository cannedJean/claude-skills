"""ocr_ref.py - build a sealed, LLM-free reference for one slide image.

The reference is everything the evaluator can know about an image WITHOUT understanding it, computed
before (or independently of) the agy analysis and stored next to the image's sha256 so it cannot be
contaminated by the analyst's output:

  * local OCR text with word boxes and confidences (engines: tesseract, winocr; one or both)
  * agreement -> per-word weight high | variant | mid | low: high = two different engines agree (or one is
    confident), variant = only upscaled passes of the same recognizer agree (exact OCR key; never 'high')
  * layout statistics (ink density, ink outside text boxes = graphics, ruling lines, table-like word
    grids, a title band, a perceptual hash; dark code panels and code-free graphics, see LAYOUT_VERSION)

    build_reference(image_path, engines=None) -> dict          (see REF_VERSION for the layout)
    load_or_build(image_path, ref_path, engines=None, rebuild=False) -> dict
                                                               (reuses ref_path when the sha256 matches; keeps a
                                                                stale ref whose slide has judgments or queued line
                                                                crops unless rebuild)
    ref_state(image_path, ref_path, engines, rebuild=False) -> (current | kept | build, ref | None)
    available_engines() -> list[Engine]                        (real engines only, never variants)
    with_variants(engines, scales=None) -> list[Engine]        (adds e.g. winocr_x2; scales default OCR_SCALES or 1,2)
    detect_code_panels(img, words=None) -> [[l, t, w, h]]      (dark filled code / terminal panels; with words,
                                                                only those whose light ink OCR text covers)
    layout_stats(img, words, word_groups=None, text_masked=None, panel_words=None) -> dict
    table_grid(words) / table_grid2(words, height) -> {"rows", "cols"} | None
    pdf_page_image(pdf_path, page_no) -> PIL.Image | None      (scanned PDFs: the embedded page image)
    pdf_page_text(pdf_path, page_no) -> str                    (born-digital pages: the text layer)

Reference (REF_VERSION 3) adds to version 2: words carry agreed_by (engine | variant | None); the ref carries
agreement (cross_engine | variants | single) and quality.n_variant; layout carries layout_version 3,
code_boxes, graphics_ratio_nocode (graphics with code panels painted out), table_grid2, ruled_table2 and
text_masked (False when no OCR word mask was applied, e.g. a PDF text-layer page). graphics_ratio,
table_candidate and ruled_table keep their version 2 meaning.

Engines
  tesseract  `tesseract IMG stdout -l kor+eng --psm 3 tsv` through subprocess; word confidences 0-100.
             No Python dependency. Binary: PATH, TESSERACT_BIN, or the default install folders.
  winocr     Windows.Media.Ocr via the `winsdk` (or `winrt`) pip package; Windows only; no confidences
             (words get weight mid, or high when another engine agrees). Language: OCR_LANG (default ko).
  winocr_xN  winocr on an N-times LANCZOS upscale (boxes in original coordinates). A second reading of the same
             recognizer: it confirms small text and adds what the base pass missed. with_variants() adds it.

Requires Pillow; numpy is optional (layout statistics are skipped without it).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None
try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
from textnorm import bigrams, fuzzy_in, norm, ocr_key  # noqa: E402

REF_VERSION = 3
LAYOUT_VERSION = 3
HEADER_BAND = 0.13   # top share of the image treated as slide-template decoration for graphics statistics
FOOTER_BAND = 0.07   # bottom share, likewise
IS_WINDOWS = sys.platform == "win32"
ENGINE_ORDER = ("winocr", "tesseract")  # preference when several are available; first = primary
DEFAULT_SCALES = "1,2"
WINOCR_MAX_DIM = 2600  # used only when the installed winrt does not report OcrEngine.max_image_dimension
PANEL_INK_RISE = 40    # light ink on a dark panel: this many grey levels above the panel's median
PANEL_EDGE = 8         # px of a panel's border band left out of its light ink (a lighter frame is not text)
PANEL_TEXT_COVER = 0.7  # share of a panel's light ink OCR words must cover for it to be a code panel
_VARIANT_RE = re.compile(r"^(.+)_x(\d+(?:\.\d+)?)$")


# --------------------------------------------------------------------------- helpers
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _union(boxes):
    l = min(b[0] for b in boxes)
    t = min(b[1] for b in boxes)
    r = max(b[0] + b[2] for b in boxes)
    b_ = max(b[1] + b[3] for b in boxes)
    return [l, t, r - l, b_ - t]


def _inside(box, rects, frac=0.6) -> bool:
    """At least `frac` of box lies inside one of rects (all [l, t, w, h])."""
    l, t, w, h = box
    a = max(1, w * h)
    for L, T, W_, H_ in rects:
        ix = max(0, min(l + w, L + W_) - max(l, L))
        iy = max(0, min(t + h, T + H_) - max(t, T))
        if ix * iy / a >= frac:
            return True
    return False


# --------------------------------------------------------------------------- engines
class Engine:
    name = "base"
    scale = 1
    scalable = False  # True: the class takes scale= and can run as an upscaled variant

    def available(self) -> bool:
        return False

    def info(self) -> dict:
        return {"engine": self.name}

    def run(self, image: Path) -> dict:
        """-> {"engine", "lang", "lines": [{"text", "conf", "box": [l, t, w, h], "words": [{"text", "conf", "box"}]}]}"""
        raise NotImplementedError


class TesseractEngine(Engine):
    name = "tesseract"

    def __init__(self, binary=None, langs=None, psm=3):
        self.binary = binary or os.environ.get("TESSERACT_BIN") or self._find()
        self.langs = langs or os.environ.get("TESSERACT_LANGS", "kor+eng")
        self.psm = psm
        self._langs_cache = None

    @staticmethod
    def _find():
        for name in ("tesseract", "tesseract.exe"):
            p = shutil.which(name)
            if p:
                return p
        cands = []
        if IS_WINDOWS:
            cands += [Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
                      Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR" / "tesseract.exe"]
        cands += [Path("/usr/bin/tesseract"), Path("/usr/local/bin/tesseract"), Path("/opt/homebrew/bin/tesseract")]
        for c in cands:
            if c.exists():
                return str(c)
        return None

    def installed_langs(self) -> list:
        if self._langs_cache is None:
            self._langs_cache = []
            if self.binary:
                try:
                    cp = subprocess.run([self.binary, "--list-langs"], capture_output=True, text=True, timeout=30)
                    self._langs_cache = [l.strip() for l in (cp.stdout + cp.stderr).splitlines()
                                         if l.strip() and not l.lower().startswith("list of")]
                except (OSError, subprocess.SubprocessError):
                    pass
        return self._langs_cache

    def available(self) -> bool:
        if not self.binary:
            return False
        have = set(self.installed_langs())
        return all(l in have for l in self.langs.split("+")) if have else True

    def info(self) -> dict:
        return {"engine": self.name, "binary": self.binary, "langs": self.langs, "installed": self.installed_langs()}

    def run(self, image: Path) -> dict:
        cmd = [self.binary, str(image), "stdout", "-l", self.langs, "--psm", str(self.psm), "tsv"]
        cp = subprocess.run(cmd, capture_output=True, timeout=300)
        if cp.returncode != 0:
            raise RuntimeError(f"tesseract rc={cp.returncode}: {cp.stderr.decode('utf-8', 'replace')[-300:]}")
        rows = cp.stdout.decode("utf-8", "replace").splitlines()
        lines, order = {}, []
        for row in rows[1:]:
            f = row.split("\t")
            if len(f) < 12 or f[0] != "5":
                continue
            text = f[11].strip()
            if not text:
                continue
            key = (f[2], f[3], f[4])
            box = [int(f[6]), int(f[7]), int(f[8]), int(f[9])]
            w = {"text": text, "conf": max(0.0, float(f[10])), "box": box}
            if key not in lines:
                lines[key] = []
                order.append(key)
            lines[key].append(w)
        out = []
        for key in order:
            ws = lines[key]
            out.append({"text": " ".join(w["text"] for w in ws),
                        "conf": round(sum(w["conf"] for w in ws) / len(ws), 1),
                        "box": _union([w["box"] for w in ws]), "words": ws})
        return {"engine": self.name, "lang": self.langs, "lines": out}


class WinOcrEngine(Engine):
    name = "winocr"
    scalable = True

    def __init__(self, lang=None, scale=1):
        self.lang = lang or os.environ.get("OCR_LANG", "ko")
        scale = float(scale)
        self.scale = int(scale) if scale.is_integer() else scale
        if self.scale <= 0:
            raise ValueError(f"scale must be positive, got {scale!r}")
        if self.scale != 1:
            self.name = f"winocr_x{self.scale:g}"
        self._mods = None
        self._err = None

    def _load(self):
        if self._mods is not None or self._err:
            return self._mods
        if not IS_WINDOWS:
            self._err = "not Windows"
            return None
        for root in ("winsdk", "winrt"):
            try:
                ocr = __import__(f"{root}.windows.media.ocr", fromlist=["OcrEngine"])
                glob = __import__(f"{root}.windows.globalization", fromlist=["Language"])
                img = __import__(f"{root}.windows.graphics.imaging", fromlist=["BitmapDecoder"])
                stor = __import__(f"{root}.windows.storage", fromlist=["StorageFile", "FileAccessMode"])
                self._mods = {"OcrEngine": ocr.OcrEngine, "Language": glob.Language,
                              "BitmapDecoder": img.BitmapDecoder, "StorageFile": stor.StorageFile,
                              "FileAccessMode": stor.FileAccessMode}
                self._err = None
                return self._mods
            except Exception as e:  # ImportError or a WinRT activation error
                self._err = f"{root}: {e!r}"
        return None

    def _engine(self):
        m = self._load()
        if not m:
            return None
        try:
            eng = m["OcrEngine"].try_create_from_language(m["Language"](self.lang))
        except Exception as e:
            self._err = repr(e)
            return None
        if eng is None:
            self._err = f"Windows OCR language pack for {self.lang!r} not installed"
        return eng

    def _max_dim(self) -> int:
        # a static property of the OcrEngine class (10000 on Windows 11); instances do not expose it
        try:
            return int(self._load()["OcrEngine"].max_image_dimension) or WINOCR_MAX_DIM
        except Exception:
            return WINOCR_MAX_DIM

    def available(self) -> bool:
        return self._engine() is not None

    def info(self) -> dict:
        d = {"engine": self.name, "lang": self.lang, "error": self._err}
        if self.scale != 1:
            d.update(scale=self.scale, variant_of="winocr")
        return d

    def run(self, image: Path) -> dict:
        import asyncio
        m = self._load()
        eng = self._engine()
        if not eng:
            raise RuntimeError(self._err or "winocr unavailable")
        max_dim = self._max_dim()
        scale, path, tmp = 1.0, Path(image), None
        with Image.open(image) as im:
            w, h = im.size
            scale = min(float(self.scale), max_dim / max(w, h))
            if scale != 1.0 or im.mode not in ("RGB", "RGBA", "L") \
                    or path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".bmp"):
                tmp = Path(tempfile.mkdtemp()) / "winocr.bmp"  # lossless like PNG, several times faster to write
                resample = Image.LANCZOS if scale > 1 else None
                im.convert("RGB").resize((max(1, int(w * scale)), max(1, int(h * scale))), resample).save(tmp)
                path = tmp

        async def go():
            f = await m["StorageFile"].get_file_from_path_async(str(path.resolve()))
            stream = await f.open_async(m["FileAccessMode"].READ)
            dec = await m["BitmapDecoder"].create_async(stream)
            bmp = await dec.get_software_bitmap_async()
            return await eng.recognize_async(bmp)

        try:
            res = asyncio.run(go())
        finally:
            if tmp:
                shutil.rmtree(tmp.parent, ignore_errors=True)
        lines = []
        for ln in res.lines:
            words = []
            for wd in ln.words:
                r = wd.bounding_rect
                words.append({"text": wd.text, "conf": None,
                              "box": [int(r.x / scale), int(r.y / scale), int(r.width / scale), int(r.height / scale)]})
            if words:
                lines.append({"text": ln.text, "conf": None, "box": _union([w["box"] for w in words]), "words": words})
        out = {"engine": self.name, "lang": self.lang, "lines": lines}
        if self.scale != 1:
            out.update(recognizer="winocr", scale=self.scale)
        return out


ENGINES = {"tesseract": TesseractEngine, "winocr": WinOcrEngine}


def _make_engine(name: str) -> Engine:
    if name in ENGINES:
        return ENGINES[name]()
    m = _VARIANT_RE.match(name)
    if m and getattr(ENGINES.get(m.group(1)), "scalable", False) and float(m.group(2)) > 0:
        return ENGINES[m.group(1)](scale=float(m.group(2)))
    raise ValueError(f"unknown OCR engine {name!r}; choose from {sorted(ENGINES)} (or winocr_xN for an upscaled pass)")


def make_engines(names=None) -> list:
    """names: iterable of engine names (winocr_x2 = an upscaled winocr pass), or None/'auto' for every engine in
    ENGINE_ORDER."""
    if not names or names == "auto" or list(names) == ["auto"]:
        names = ENGINE_ORDER
    return [_make_engine(n) for n in names]


def available_engines(names=None) -> list:
    return [e for e in make_engines(names) if e.available()]


def parse_scales(scales=None) -> list:
    """'1,2' / [1, 2] / None (env OCR_SCALES, default 1,2) -> [1, 2]; tokens that are not numbers in (0, 10] are
    ignored."""
    if scales is None:
        scales = os.environ.get("OCR_SCALES") or DEFAULT_SCALES
    if isinstance(scales, str):
        scales = scales.replace(";", ",").split(",")
    out = []
    for s in scales:
        try:
            v = float(str(s).strip().lower().lstrip("x"))
        except ValueError:
            continue
        if not 0 < v <= 10:  # also drops nan / inf
            continue
        v = int(v) if v.is_integer() else v
        if v not in out:
            out.append(v)
    return out


def with_variants(engines, scales=None) -> list:
    """engines plus an upscaled pass of every scalable engine (winocr) for each scale other than 1, appended after
    the real engines (the primary stays first). Meant for machines with one real engine: the variants give that
    engine a second reading to agree with. Names already present are not added twice."""
    out = list(engines)
    have = {e.name for e in out}
    for e in engines:
        if not getattr(e, "scalable", False) or e.scale != 1:
            continue
        for s in parse_scales(scales):
            if s == 1:
                continue
            v = type(e)(lang=getattr(e, "lang", None), scale=s)
            if v.name not in have:
                out.append(v)
                have.add(v.name)
    return out


def _recognizer(result: dict) -> str:
    """The recognizer behind an engine output: winocr_x2 -> winocr. Variants of one recognizer share its misreads."""
    if result.get("recognizer"):
        return result["recognizer"]
    m = _VARIANT_RE.match(result.get("engine") or "")
    return m.group(1) if m else result.get("engine")


# --------------------------------------------------------------------------- weights
def _weight(conf, agreed: bool, variant: bool = False) -> str:
    if agreed:
        if conf is not None and conf < 40:
            return "mid"
        return "variant" if variant else "high"
    if conf is None:
        return "mid"
    if conf >= 80:
        return "high"
    if conf >= 60:
        return "mid"
    return "low"


def merge_results(results: list) -> tuple:
    """results: engine outputs in preference order. Returns (lines, text_by_engine).
    The primary engine's lines carry the geometry; a secondary engine's line is appended only when most of
    its words are absent from the primary text (something the primary missed).
    Agreement: a different engine agrees when the word is fuzzily in its text (bigram overlap 0.75); a variant of
    the same recognizer (winocr_x2 for winocr) agrees only on an exact ocr_key token, because it repeats the
    recognizer's own misreads. Variant-only agreement gives weight 'variant', never 'high'."""
    texts = {r["engine"]: norm(" ".join(l["text"] for l in r["lines"])) for r in results}
    bg = {k: bigrams(v) for k, v in texts.items()}
    keys = {r["engine"]: {k for l in r["lines"] for w in l["words"] for k in (ocr_key(w["text"]),) if k} for r in results}
    rec = {r["engine"]: _recognizer(r) for r in results}
    lines, n = [], 0
    for i, r in enumerate(results):
        me = r["engine"]
        cross = [e for e in texts if e != me and rec[e] != rec[me]]
        same = set().union(*(keys[e] for e in texts if e != me and rec[e] == rec[me]))
        for ln in r["lines"]:
            for w in ln["words"]:
                nw = norm(w["text"])
                by = None
                if nw and any(fuzzy_in(nw, texts[o], bg[o], 0.75) for o in cross):
                    by = "engine"
                elif nw and ocr_key(w["text"]) in same:
                    by = "variant"
                # one-character "words" are mostly bullets, plot markers and box edges read as 0 / o / l;
                # they never carry evidence on their own
                w["weight"] = _weight(w["conf"], by is not None, by == "variant") if len(nw) >= 2 else "low"
                w["agreed"] = by is not None
                w["agreed_by"] = by
            if i > 0:  # secondary engine: keep only lines the primary mostly missed
                prim = results[0]["engine"]
                ws = [w for w in ln["words"] if norm(w["text"])]
                if not ws or sum(fuzzy_in(norm(w["text"]), texts[prim], bg[prim], 0.75) for w in ws) / len(ws) > 0.4:
                    continue
                for w in ln["words"]:
                    if w["weight"] == "high" and not w["agreed"]:
                        w["weight"] = "mid"  # unconfirmed extra from a secondary engine
            n += 1
            lines.append({"id": f"L{n:03d}", "engine": r["engine"], **ln})
    return lines, texts


# --------------------------------------------------------------------------- layout statistics
def _rule_count(mask, frac: float) -> int:
    """Number of separated rows in `mask` that contain one continuous ink run >= frac of the row."""
    return len(_rule_positions(mask, frac))


def _rule_positions(mask, frac: float) -> list:
    """Centre rows of the separated row groups in `mask` that hold one continuous ink run >= frac of the row."""
    need = int(frac * mask.shape[1])
    hits = []
    for i in range(mask.shape[0]):
        row = mask[i]
        if row.sum() < need:
            continue
        # longest run of True
        padded = np.concatenate(([False], row, [False]))
        edges = np.flatnonzero(padded[1:] != padded[:-1])
        if edges.size and (edges[1::2] - edges[::2]).max() >= need:
            hits.append(i)
    groups = []
    for i in hits:
        if groups and i - groups[-1][-1] <= 3:
            groups[-1].append(i)
        else:
            groups.append([i])
    return [sum(g) / len(g) for g in groups]


def table_grid(words: list):
    """Table-like word grid: >=3 short rows (2-6 words) whose words mostly sit on column starts shared by
    >=3 rows. Bulleted prose also aligns at the bullet and the text start, but its rows are long and most
    of their words land on no shared column, so it does not qualify. -> {"rows", "cols"} | None"""
    if not words:
        return None
    heights = sorted(wd["box"][3] for wd in words)
    med_h = heights[len(heights) // 2]
    rows = []
    for wd in sorted(words, key=lambda d: d["box"][1]):
        cy = wd["box"][1] + wd["box"][3] / 2
        if rows and abs(rows[-1]["cy"] - cy) <= max(6, med_h * 0.6):
            rows[-1]["xs"].append(wd["box"][0])
        else:
            rows.append({"cy": cy, "xs": [wd["box"][0]]})
    multi = [r for r in rows if 2 <= len(r["xs"]) <= 6]
    if len(multi) < 3:
        return None
    tol = max(10, med_h * 1.2)
    clusters = []
    for x in sorted(x for r in multi for x in r["xs"]):
        if clusters and x - clusters[-1][-1] <= tol:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    col_x = [sum(c) / len(c) for c in clusters if len(c) >= 3]
    aligned = 0
    for r in multi:
        on_col = sum(any(abs(x - cx) <= tol for cx in col_x) for x in r["xs"])
        if on_col >= 3 or on_col / len(r["xs"]) >= 0.6:
            aligned += 1
    return {"rows": aligned, "cols": len(col_x)} if len(col_x) >= 2 and aligned >= 3 else None


def table_grid2(words: list, height: int):
    """table_grid without its prose / TOC false positives: words in the header and footer bands are ignored, words
    closer than one text height merge into one cell (a table has gutters, prose has spaces), a row needs 2-8
    cells, a column needs >=3 cell starts, ends or centres within half a text height (left-aligned text,
    right-aligned or centred numbers), and a row is aligned when >=2 of its cells (and >=60%) sit on columns.
    -> {"rows", "cols"} | None"""
    ws = [w for w in words if w.get("box")
          and HEADER_BAND * height <= w["box"][1] + w["box"][3] / 2 <= (1 - FOOTER_BAND) * height]
    if not ws:
        return None
    hs = sorted(w["box"][3] for w in ws)
    med_h = hs[len(hs) // 2]
    rows = []
    for wd in sorted(ws, key=lambda d: d["box"][1]):
        cy = wd["box"][1] + wd["box"][3] / 2
        if rows and abs(rows[-1]["cy"] - cy) <= max(6, med_h * 0.6):
            rows[-1]["w"].append(wd)
        else:
            rows.append({"cy": cy, "w": [wd]})
    cell_rows = []
    for r in rows:
        cells = []
        for w in sorted(r["w"], key=lambda d: d["box"][0]):
            l, _, bw, _ = w["box"]
            if cells and l - cells[-1][1] <= med_h:
                cells[-1][1] = max(cells[-1][1], l + bw)
            else:
                cells.append([l, l + bw])
        if 2 <= len(cells) <= 8:
            cell_rows.append(cells)
    if len(cell_rows) < 3:
        return None
    tol = max(8, med_h * 0.5)
    edges = (lambda c: c[0], lambda c: c[1], lambda c: (c[0] + c[1]) / 2)  # start, end, centre
    cols = []  # (edge, x): a column is >= 3 cells sharing one edge
    for k, f in enumerate(edges):
        clusters = []
        for x in sorted(f(c) for r in cell_rows for c in r):
            if clusters and x - clusters[-1][-1] <= tol:
                clusters[-1].append(x)
            else:
                clusters.append([x])
        cols += [(k, sum(c) / len(c)) for c in clusters if len(c) >= 3]
    aligned, width = 0, 0
    for r in cell_rows:
        on = sum(any(abs(edges[k](c) - x) <= tol for k, x in cols) for c in r)
        if on >= 2 and on / len(r) >= 0.6:
            aligned += 1
            width = max(width, on)
    return {"rows": aligned, "cols": width} if aligned >= 3 else None


def _ruled_cells(ink, words: list) -> bool:
    """A ruled table holds words in >=2 rule-bounded columns of >=2 rule-bounded row bands; a frame or a memo
    border keeps all its text in one cell. ink: full-resolution mask; words: boxes in the same coordinates."""
    hp = _rule_positions(ink, 0.35)
    vp = _rule_positions(ink.T, 0.25)
    if len(hp) < 3 or len(vp) < 2:
        return False
    occupied = {}
    for w in words:
        if not w.get("box"):
            continue
        cx, cy = w["box"][0] + w["box"][2] / 2, w["box"][1] + w["box"][3] / 2
        ri = sum(1 for y in hp if y < cy)
        ci = sum(1 for x in vp if x < cx)
        if 0 < ri < len(hp) and 0 < ci < len(vp):
            occupied.setdefault(ri, set()).add(ci)
    return sum(1 for cs in occupied.values() if len(cs) >= 2) >= 2


def _text_like(words: list) -> list:
    """Boxed words shaped like text: not taller than 2.5x the median word and at most 1.3 heights per character.
    OCR reads a row of drawn boxes as one huge 'word' (口-口-口) that would otherwise cover a whole figure."""
    ws = [w for w in words if w.get("box") and w["box"][3] > 0 and str(w.get("text") or "").strip()]
    if not ws:
        return []
    hs = sorted(w["box"][3] for w in ws)
    med = hs[len(hs) // 2]
    return [w for w in ws if w["box"][3] <= 2.5 * med and w["box"][2] <= 1.3 * w["box"][3] * len(str(w["text"]).strip())]


def _panel_light(region, edge: int):
    """Light ink of a dark panel: 40 levels above its own median, a border band of `edge` px left out (panels
    often have a lighter frame)."""
    ink = region > float(np.median(region)) + PANEL_INK_RISE
    if edge > 0:
        ink[:edge, :] = ink[-edge:, :] = False
        ink[:, :edge] = ink[:, -edge:] = False
    return ink


def _word_mask(shape, words: list, s: float = 1.0, origin=(0, 0)):
    """Word boxes padded by 0.4 x their height, in an array of `shape` whose (0, 0) is image point `origin`, scale s."""
    h, w = shape
    m = np.zeros(shape, dtype=bool)
    ox, oy = origin
    for wd in words:
        l, t, bw, bh = wd["box"]
        p = int(bh * 0.4)
        y0, y1 = max(0, int((t - p - oy) * s)), min(h, int((t + bh + p - oy) * s))
        x0, x1 = max(0, int((l - p - ox) * s)), min(w, int((l + bw + p - ox) * s))
        if y1 > y0 and x1 > x0:
            m[y0:y1, x0:x1] = True
    return m


def detect_code_panels(img, dark=50, blk=6, min_w=0.12, min_h=24, min_fill=0.55, max_std=12, pad=0, words=None,
                       min_cover=PANEL_TEXT_COVER) -> list:
    """Dark filled panels (syntax-highlighted code, terminal captures) on a light page -> [[l, t, w, h]] in image
    coordinates (block-aligned, grown by `pad` px and clipped). A panel is a connected run of 6 px blocks at least
    half luma < `dark`, >= 12% of the page wide and >= 24 px tall, >= 55% dark inside its box, whose dark pixels are
    near-uniform (std <= 12; a photo is not). Luma 50 and no pad by default: a red mark drawn against a panel must
    stay graphics (a looser threshold swallows it, a 4 px pad paints part of it out). A dark slide (median luma
    < 128) has no panels.
    words: OCR words with boxes in image coordinates. When given, a dark region is a code panel only when text-shaped
    words cover >= min_cover of its light ink: a dark-theme diagram (outlined boxes, arrows) is a figure, not code."""
    if np is None:
        return []
    tl = _text_like(words) if words is not None else None
    g = np.asarray(img.convert("L"), dtype=np.uint8)
    H, W = g.shape
    gh, gw = H // blk, W // blk
    if not gh or not gw or float(np.median(g)) < 128:
        return []
    cells = (g[:gh * blk, :gw * blk] < dark).reshape(gh, blk, gw, blk).mean(axis=(1, 3)) >= 0.5
    seen = np.zeros_like(cells)
    out = []
    for y0, x0 in np.argwhere(cells):
        if seen[y0, x0]:
            continue
        stack, ys, xs = [(y0, x0)], [], []
        seen[y0, x0] = True
        while stack:
            y, x = stack.pop()
            ys.append(y)
            xs.append(x)
            for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= ny < gh and 0 <= nx < gw and cells[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        l, t, r, b = int(min(xs)) * blk, int(min(ys)) * blk, (int(max(xs)) + 1) * blk, (int(max(ys)) + 1) * blk
        if r - l < min_w * W or b - t < min_h:
            continue
        region = g[t:b, l:r]
        dk = region[region < dark]
        if dk.size < min_fill * region.size or float(dk.std()) > max_std:
            continue
        if tl is not None:
            ink = _panel_light(region, PANEL_EDGE)
            n = int(ink.sum())
            if n and float((ink & _word_mask(ink.shape, tl, 1.0, (l, t))).sum()) / n < min_cover:
                continue
        l, t, r, b = max(0, l - pad), max(0, t - pad), min(W, r + pad), min(H, b + pad)
        out.append([l, t, r - l, b - t])
    return out


def _scaled(g, s: float):
    if s < 1.0:
        g = g.resize((max(1, int(g.size[0] * s)), max(1, int(g.size[1] * s))))
    return g


def _ink(a) -> tuple:
    """-> (dark_background, ink mask): ink = 60 grey levels away from the page median."""
    med = float(np.median(a))
    dark_bg = med < 128
    return dark_bg, (a > min(255, med + 60)) if dark_bg else (a < max(0, med - 60))


def _text_mask(shape, words: list, s: float) -> tuple:
    h, w = shape
    mask = np.zeros(shape, dtype=bool)
    heights = []
    for wd in words:
        if not wd.get("box"):
            continue
        l, t, bw, bh = wd["box"]
        heights.append(bh)
        pad = int(bh * 0.4)
        y0, y1 = max(0, int((t - pad) * s)), min(h, int((t + bh + pad) * s))
        x0, x1 = max(0, int((l - pad) * s)), min(w, int((l + bw + pad) * s))
        mask[y0:y1, x0:x1] = True
    return mask, heights


def _graphics(ink, text_mask):
    """Ink outside text boxes and outside the template header/footer bands."""
    non_text = ink & ~text_mask
    h = non_text.shape[0]
    non_text[:int(h * HEADER_BAND), :] = False
    non_text[int(h * (1 - FOOTER_BAND)):, :] = False
    return non_text


def layout_stats(img, words: list, word_groups=None, text_masked=None, panel_words=None) -> dict:
    """Cheap, LLM-free structure signals. `words` are OCR words with boxes in image coordinates (used for the
    text mask); `word_groups` are per-engine word lists for the grid detection (default: `words` as one group).
    text_masked: whether OCR text was masked out of the graphics statistics (default: bool(words)); False tells
    the scorer that graphics_ratio counts text too (a PDF text-layer page) and cannot trigger on its own.
    panel_words: every OCR word, braces and one-character words included, as evidence that a dark region holds
    code (default: `words` plus `word_groups`)."""
    if np is None:
        return {"available": False}
    full = img.convert("L")
    W, H = full.size
    s = min(1.0, 1200 / max(W, H))
    g = _scaled(full, s)
    a = np.asarray(g, dtype=np.uint8)
    h, w = a.shape
    dark_bg, ink = _ink(a)
    text_mask, heights = _text_mask(a.shape, words, s)
    non_text = _graphics(ink, text_mask)
    out = {"available": True, "dark_background": dark_bg, "ink_ratio": round(float(ink.mean()), 4),
           "text_ink_ratio": round(float((ink & text_mask).mean()), 4),
           "graphics_ratio": round(float(non_text.mean()), 4)}
    out["h_rules"] = _rule_count(ink, 0.35)
    out["v_rules"] = _rule_count(ink.T, 0.25)

    # graphic regions: coarse grid cells dominated by non-text ink, largest connected cluster
    gy, gx = 18, 32
    cell = np.zeros((gy, gx))
    for yi in range(gy):
        for xi in range(gx):
            blk = non_text[yi * h // gy:(yi + 1) * h // gy, xi * w // gx:(xi + 1) * w // gx]
            cell[yi, xi] = blk.mean() if blk.size else 0
    on = cell > 0.04
    seen = np.zeros_like(on)
    best = []
    for yi in range(gy):
        for xi in range(gx):
            if on[yi, xi] and not seen[yi, xi]:
                stack, comp = [(yi, xi)], []
                seen[yi, xi] = True
                while stack:
                    y, x = stack.pop()
                    comp.append((y, x))
                    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        ny, nx = y + dy, x + dx
                        if 0 <= ny < gy and 0 <= nx < gx and on[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True
                            stack.append((ny, nx))
                if len(comp) > len(best):
                    best = comp
    if best:
        ys = [c[0] for c in best]
        xs = [c[1] for c in best]
        out["graphic_region"] = {"cells": len(best), "frac": round(len(best) / (gx * gy), 3),
                                 "box": [int(min(xs) * W / gx), int(min(ys) * H / gy),
                                         int((max(xs) - min(xs) + 1) * W / gx), int((max(ys) - min(ys) + 1) * H / gy)]}
    else:
        out["graphic_region"] = None

    boxed = [wd for wd in words if wd.get("box")]
    med_h = float(np.median(heights)) if heights else 0
    # table-like word grid: evaluated per engine (word_groups), because one engine may garble the very
    # region another reads cleanly; any engine finding a grid counts
    out["table_candidate"] = None
    for group in (word_groups or [boxed]):
        tc = table_grid([wd for wd in group if wd.get("box")])
        if tc:
            out["table_candidate"] = tc
            break
    out["ruled_table"] = bool(out["h_rules"] >= 3 and out["v_rules"] >= 2)

    top = [wd for wd in boxed if wd["box"][1] < H * 0.2]
    out["title_band"] = bool(top and med_h and max(wd["box"][3] for wd in top) >= 1.4 * med_h)

    # perceptual hash (dHash)
    small = np.asarray(g.resize((9, 8)), dtype=np.int16)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    out["dhash"] = "%016x" % int("".join("1" if b else "0" for b in bits), 2)

    # layout_version 3: code panels painted out with the page median, so their dark background is neither
    # graphics nor rules, and their words take no part in the table detection. Only a dark region OCR text
    # mostly covers is a panel; light ink on a panel that no word covers (a highlight box, an arrow) stays graphics
    all_words = [wd for wd in (panel_words if panel_words is not None else
                               boxed + [wd for grp in (word_groups or []) for wd in grp]) if wd.get("box")]
    ocr_ran = bool(words or word_groups or panel_words)
    panels = detect_code_panels(full, words=all_words if ocr_ran else None)  # None: no OCR ran (a text-layer page)
    fa = np.asarray(full, dtype=np.uint8)
    if panels:
        fa = fa.copy()
        fill = int(np.median(fa))
        for l, t, bw, bh in panels:
            fa[t:t + bh, l:l + bw] = fill
        am = np.asarray(_scaled(Image.fromarray(fa), s), dtype=np.uint8)
        keep = [wd for wd in boxed if not _inside(wd["box"], panels)]
        gfx = _graphics(_ink(am)[1], _text_mask(am.shape, keep, s)[0])
        marks = np.zeros(a.shape, dtype=bool)
        for l, t, bw, bh in panels:
            y0, y1, x0, x1 = int(t * s), int((t + bh) * s), int(l * s), int((l + bw) * s)
            if y1 > y0 and x1 > x0:
                marks[y0:y1, x0:x1] = _panel_light(a[y0:y1, x0:x1], max(1, int(PANEL_EDGE * s)))
        gfx |= _graphics(marks, _word_mask(a.shape, _text_like(all_words), s))
        out["graphics_ratio_nocode"] = round(float(gfx.mean()), 4)
    else:
        out["graphics_ratio_nocode"] = out["graphics_ratio"]
    groups = [[wd for wd in grp if wd.get("box") and not _inside(wd["box"], panels)] for grp in (word_groups or [boxed])]
    out["code_boxes"] = panels
    out["table_grid2"] = next((tg for tg in (table_grid2(grp, H) for grp in groups) if tg), None)
    out["ruled_table2"] = _ruled_cells(_ink(fa)[1], [wd for grp in groups for wd in grp])
    out["text_masked"] = bool(words) if text_masked is None else bool(text_masked)
    out["layout_version"] = LAYOUT_VERSION
    return out


# --------------------------------------------------------------------------- reference
def build_reference(image_path, engines=None) -> dict:
    """Run every given (or every available) engine on the image and assemble the reference."""
    image_path = Path(image_path)
    if Image is None:
        raise RuntimeError("Pillow is required for the OCR reference (python -m pip install pillow)")
    engines = engines if engines is not None else available_engines()
    results, status = [], []
    for e in engines:
        st = e.info()
        try:
            r = e.run(image_path)
            words = [w for l in r["lines"] for w in l["words"]]
            confs = [w["conf"] for w in words if w["conf"] is not None]
            st.update(ok=True, n_lines=len(r["lines"]), n_words=len(words),
                      mean_conf=round(sum(confs) / len(confs), 1) if confs else None)
            results.append(r)
        except Exception as ex:  # one engine failing must not lose the others
            st.update(ok=False, error=str(ex)[:300])
        status.append(st)
    lines, texts = merge_results(results)
    words = [w for l in lines for w in l["words"]]
    # text mask: every engine's confident words; grid detection: per engine, from its own full output
    # (the merged lines drop a secondary engine's lines where the primary agreed, which breaks rows)
    groups = [[w for l in r["lines"] for w in l["words"] if len(norm(w["text"])) >= 2] for r in results]
    with Image.open(image_path) as im:
        size = list(im.size)
        layout = layout_stats(im, [w for w in words if w["weight"] != "low"], groups, text_masked=bool(results),
                              panel_words=words)
    n = {k: sum(w["weight"] == k for w in words) for k in ("high", "variant", "mid")}
    confs = [w["conf"] for w in words if w["conf"] is not None]
    recs = {_recognizer(r) for r in results}
    agreement = "cross_engine" if len(recs) > 1 else ("variants" if len(results) > 1 else "single")
    return {"version": REF_VERSION, "file": image_path.name, "sha256": sha256_file(image_path), "size": size,
            "engines": status, "primary": results[0]["engine"] if results else None, "agreement": agreement,
            "lines": lines, "text": texts, "layout": layout,
            "quality": {"n_words": len(words), "n_high": n["high"], "n_mid": n["mid"], "n_variant": n["variant"],
                        "mean_conf": round(sum(confs) / len(confs), 1) if confs else None,
                        "usable": (n["high"] + n["variant"] + 0.5 * n["mid"]) >= 5}}


def text_reference(text: str, file_name: str, source: str = "pdf_text") -> dict:
    """A reference built from a trusted text layer (born-digital PDF page): one line per text line, weight high."""
    lines = []
    for i, t in enumerate(l for l in (text or "").splitlines() if l.strip()):
        lines.append({"id": f"L{i + 1:03d}", "engine": source, "text": t.strip(), "conf": 100.0, "box": None,
                      "words": [{"text": w, "conf": 100.0, "box": None, "weight": "high", "agreed": True}
                                for w in t.split()]})
    n = sum(len(l["words"]) for l in lines)
    return {"version": REF_VERSION, "file": file_name, "sha256": None, "size": None,
            "engines": [{"engine": source, "ok": True, "n_lines": len(lines), "n_words": n, "mean_conf": 100.0}],
            "primary": source, "agreement": "single", "lines": lines, "text": {source: norm(text)},
            "layout": {"available": False},
            "quality": {"n_words": n, "n_high": n, "n_mid": 0, "n_variant": 0, "mean_conf": 100.0, "usable": n >= 5}}


def _judged(ref_path: Path) -> bool:
    """Does the slide's result (<out>/results/<stem>.json next to <out>/ref/) name the ref's line ids: Claude
    judgments, or line crops still waiting in its queue? Those ids (<stem>__L007) are the ref's line ids, which a
    rebuild renumbers, so an answer would be bound to another line."""
    try:
        rec = json.loads((ref_path.parent.parent / "results" / f"{ref_path.stem}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(rec, dict):
        return False
    return bool(rec.get("judgments")) or any(isinstance(e, dict) and e.get("kind") == "line" for e in rec.get("queue") or [])


def ref_state(image_path, ref_path, engines, rebuild=False) -> tuple:
    """-> (state, stored ref | None) where state is current (same image bytes, engine set and REF_VERSION: reused),
    kept (stale, but the slide's judgments or queued line crops name its line ids and not rebuild: reused as it
    is) or build (missing, unreadable, another image, or stale and free to rebuild)."""
    image_path, ref_path = Path(image_path), Path(ref_path)
    if not ref_path.exists():
        return "build", None
    try:
        ref = json.loads(ref_path.read_text(encoding="utf-8"))
        have = sorted(e["engine"] for e in ref.get("engines", []))
        if ref.get("sha256") != sha256_file(image_path):
            return "build", None
        if ref.get("version") == REF_VERSION and have == sorted(e.name for e in engines):
            return "current", ref
        if not rebuild and _judged(ref_path):
            return "kept", ref
    except (ValueError, OSError, AttributeError, KeyError, TypeError):  # unreadable or malformed: rebuild
        pass
    return "build", None


def load_or_build(image_path, ref_path, engines=None, rebuild=False) -> dict:
    """Reuse ref_path when it was built from the same image bytes with the same engine set and REF_VERSION.
    A stale ref (older version or another engine set) of the same image bytes is kept as it is when the slide's
    stored judgments or queued line crops refer to its line ids, unless rebuild=True (rescore, which drops both).
    See ref_state."""
    image_path, ref_path = Path(image_path), Path(ref_path)
    engines = engines if engines is not None else available_engines()
    state, ref = ref_state(image_path, ref_path, engines, rebuild)
    if ref is not None:
        return ref
    ref = build_reference(image_path, engines)
    ref_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = ref_path.with_name(ref_path.name + ".tmp")  # a kill mid-write must not leave half a reference
    tmp.write_text(json.dumps(ref, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, ref_path)
    return ref


# --------------------------------------------------------------------------- PDF helpers
def pdf_page_image(pdf_path, page_no: int, dpi: int = 150):
    """Return a PIL image for a 1-based page: the largest embedded image (scanned PDFs, via pypdf), else a
    render through pymupdf when installed, else None."""
    try:
        import pypdf
        reader = pypdf.PdfReader(str(pdf_path))
        page = reader.pages[page_no - 1]
        best = None
        for im in page.images:
            try:
                pil = im.image
            except Exception:
                continue
            if pil is not None and (best is None or pil.size[0] * pil.size[1] > best.size[0] * best.size[1]):
                best = pil
        if best is not None and min(best.size) >= 200:
            return best.convert("RGB")
    except Exception:
        pass
    try:
        import fitz  # pymupdf
        doc = fitz.open(str(pdf_path))
        pix = doc[page_no - 1].get_pixmap(dpi=dpi)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    except Exception:
        return None


def pdf_page_text(pdf_path, page_no: int) -> str:
    try:
        import pypdf
        return pypdf.PdfReader(str(pdf_path)).pages[page_no - 1].extract_text() or ""
    except Exception:
        return ""


# --------------------------------------------------------------------------- CLI (debugging aid)
def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="build the OCR reference for one image and print a summary")
    p.add_argument("image", nargs="?")
    p.add_argument("--engine", help="comma list: winocr,tesseract,winocr_x2 (default: all available)")
    p.add_argument("--scales", help="add upscaled winocr passes, e.g. 1,2 (default: none)")
    p.add_argument("--out", help="write the reference JSON here")
    p.add_argument("--list", action="store_true", help="only list engine availability")
    a = p.parse_args(argv)
    engines = make_engines(a.engine.split(",")) if a.engine else make_engines()
    if a.scales:
        engines = with_variants(engines, a.scales)
    if a.list or not a.image:
        print(json.dumps([{**e.info(), "available": e.available()} for e in engines], ensure_ascii=False, indent=2))
        return 0
    ref = build_reference(a.image, [e for e in engines if e.available()])
    if a.out:
        Path(a.out).write_text(json.dumps(ref, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"engines": ref["engines"], "agreement": ref["agreement"], "quality": ref["quality"],
                      "layout": ref["layout"], "lines": [(l["id"], l["engine"], l["text"]) for l in ref["lines"]]},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
