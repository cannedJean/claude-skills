"""ocr_ref.py - build a sealed, LLM-free reference for one slide image.

The reference is everything the evaluator can know about an image WITHOUT understanding it, computed
before (or independently of) the agy analysis and stored next to the image's sha256 so it cannot be
contaminated by the analyst's output:

  * local OCR text with word boxes and confidences (engines: tesseract, winocr; one or both)
  * cross-engine agreement -> per-word weight high | mid | low
  * layout statistics (ink density, ink outside text boxes = graphics, ruling lines, table-like word
    grids, a title band, a perceptual hash)

    build_reference(image_path, engines=None) -> dict          (see REF_VERSION for the layout)
    load_or_build(image_path, ref_path, engines=None) -> dict  (reuses ref_path when the sha256 matches)
    available_engines() -> list[Engine]
    pdf_page_image(pdf_path, page_no) -> PIL.Image | None      (scanned PDFs: the embedded page image)
    pdf_page_text(pdf_path, page_no) -> str                    (born-digital pages: the text layer)

Engines
  tesseract  `tesseract IMG stdout -l kor+eng --psm 3 tsv` through subprocess; word confidences 0-100.
             No Python dependency. Binary: PATH, TESSERACT_BIN, or the default install folders.
  winocr     Windows.Media.Ocr via the `winsdk` (or `winrt`) pip package; Windows only; no confidences
             (words get weight mid, or high when another engine agrees). Language: OCR_LANG (default ko).

Requires Pillow; numpy is optional (layout statistics are skipped without it).
"""
from __future__ import annotations

import hashlib
import json
import os
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
from textnorm import bigrams, fuzzy_in, norm  # noqa: E402

REF_VERSION = 2
HEADER_BAND = 0.13   # top share of the image treated as slide-template decoration for graphics statistics
FOOTER_BAND = 0.07   # bottom share, likewise
IS_WINDOWS = sys.platform == "win32"
ENGINE_ORDER = ("winocr", "tesseract")  # preference when several are available; first = primary


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


# --------------------------------------------------------------------------- engines
class Engine:
    name = "base"

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

    def __init__(self, lang=None):
        self.lang = lang or os.environ.get("OCR_LANG", "ko")
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

    def available(self) -> bool:
        return self._engine() is not None

    def info(self) -> dict:
        return {"engine": self.name, "lang": self.lang, "error": self._err}

    def run(self, image: Path) -> dict:
        import asyncio
        m = self._load()
        eng = self._engine()
        if not eng:
            raise RuntimeError(self._err or "winocr unavailable")
        max_dim = int(getattr(eng, "max_image_dimension", 0) or 2600)
        scale, path, tmp = 1.0, Path(image), None
        with Image.open(image) as im:
            w, h = im.size
            if max(w, h) > max_dim or im.mode not in ("RGB", "RGBA", "L") \
                    or path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".bmp"):
                scale = min(1.0, max_dim / max(w, h))
                tmp = Path(tempfile.mkdtemp()) / "winocr.png"
                im.convert("RGB").resize((max(1, int(w * scale)), max(1, int(h * scale)))).save(tmp)
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
        return {"engine": self.name, "lang": self.lang, "lines": lines}


ENGINES = {"tesseract": TesseractEngine, "winocr": WinOcrEngine}


def make_engines(names=None) -> list:
    """names: iterable of engine names, or None/'auto' for every engine in ENGINE_ORDER."""
    if not names or names == "auto" or list(names) == ["auto"]:
        names = ENGINE_ORDER
    out = []
    for n in names:
        if n not in ENGINES:
            raise ValueError(f"unknown OCR engine {n!r}; choose from {sorted(ENGINES)}")
        out.append(ENGINES[n]())
    return out


def available_engines(names=None) -> list:
    return [e for e in make_engines(names) if e.available()]


# --------------------------------------------------------------------------- weights
def _weight(conf, agreed: bool) -> str:
    if agreed:
        return "high" if (conf is None or conf >= 40) else "mid"
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
    its words are absent from the primary text (something the primary missed)."""
    texts = {r["engine"]: norm(" ".join(l["text"] for l in r["lines"])) for r in results}
    bg = {k: bigrams(v) for k, v in texts.items()}
    lines, n = [], 0
    for i, r in enumerate(results):
        others = [e for e in texts if e != r["engine"]]
        for ln in r["lines"]:
            for w in ln["words"]:
                nw = norm(w["text"])
                agreed = bool(nw) and any(fuzzy_in(nw, texts[o], bg[o], 0.75) for o in others)
                # one-character "words" are mostly bullets, plot markers and box edges read as 0 / o / l;
                # they never carry evidence on their own
                w["weight"] = _weight(w["conf"], agreed) if len(nw) >= 2 else "low"
                w["agreed"] = agreed
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
    groups, prev = 0, -10
    for i in hits:
        if i - prev > 3:
            groups += 1
        prev = i
    return groups


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


def layout_stats(img, words: list, word_groups=None) -> dict:
    """Cheap, LLM-free structure signals. `words` are OCR words with boxes in image coordinates (used for the
    text mask); `word_groups` are per-engine word lists for the grid detection (default: `words` as one group)."""
    if np is None:
        return {"available": False}
    g = img.convert("L")
    W, H = g.size
    s = min(1.0, 1200 / max(W, H))
    if s < 1.0:
        g = g.resize((max(1, int(W * s)), max(1, int(H * s))))
    a = np.asarray(g, dtype=np.uint8)
    h, w = a.shape
    med = float(np.median(a))
    dark_bg = med < 128
    ink = (a > min(255, med + 60)) if dark_bg else (a < max(0, med - 60))
    text_mask = np.zeros_like(ink)
    heights = []
    for wd in words:
        if not wd.get("box"):
            continue
        l, t, bw, bh = wd["box"]
        heights.append(bh)
        pad = int(bh * 0.4)
        y0, y1 = max(0, int((t - pad) * s)), min(h, int((t + bh + pad) * s))
        x0, x1 = max(0, int((l - pad) * s)), min(w, int((l + bw + pad) * s))
        text_mask[y0:y1, x0:x1] = True
    non_text = ink & ~text_mask
    # slide templates carry decorated header/footer bands; they are not content graphics
    y_lo, y_hi = int(h * HEADER_BAND), int(h * (1 - FOOTER_BAND))
    non_text[:y_lo, :] = False
    non_text[y_hi:, :] = False
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
        layout = layout_stats(im, [w for w in words if w["weight"] != "low"], groups)
    n_high = sum(w["weight"] == "high" for w in words)
    n_mid = sum(w["weight"] == "mid" for w in words)
    confs = [w["conf"] for w in words if w["conf"] is not None]
    return {"version": REF_VERSION, "file": image_path.name, "sha256": sha256_file(image_path), "size": size,
            "engines": status, "primary": results[0]["engine"] if results else None,
            "lines": lines, "text": texts, "layout": layout,
            "quality": {"n_words": len(words), "n_high": n_high, "n_mid": n_mid,
                        "mean_conf": round(sum(confs) / len(confs), 1) if confs else None,
                        "usable": (n_high + 0.5 * n_mid) >= 5}}


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
            "primary": source, "lines": lines, "text": {source: norm(text)}, "layout": {"available": False},
            "quality": {"n_words": n, "n_high": n, "n_mid": 0, "mean_conf": 100.0, "usable": n >= 5}}


def load_or_build(image_path, ref_path, engines=None) -> dict:
    """Reuse ref_path when it was built from the same image bytes with the same engine set."""
    image_path, ref_path = Path(image_path), Path(ref_path)
    engines = engines if engines is not None else available_engines()
    want = sorted(e.name for e in engines)
    if ref_path.exists():
        try:
            ref = json.loads(ref_path.read_text(encoding="utf-8"))
            have = sorted(e["engine"] for e in ref.get("engines", []))
            if ref.get("version") == REF_VERSION and ref.get("sha256") == sha256_file(image_path) and have == want:
                return ref
        except (json.JSONDecodeError, OSError):
            pass
    ref = build_reference(image_path, engines)
    ref_path.parent.mkdir(parents=True, exist_ok=True)
    ref_path.write_text(json.dumps(ref, ensure_ascii=False, indent=1), encoding="utf-8")
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
    p.add_argument("--engine", help="comma list: winocr,tesseract (default: all available)")
    p.add_argument("--out", help="write the reference JSON here")
    p.add_argument("--list", action="store_true", help="only list engine availability")
    a = p.parse_args(argv)
    engines = make_engines(a.engine.split(",")) if a.engine else make_engines()
    if a.list or not a.image:
        print(json.dumps([{**e.info(), "available": e.available()} for e in engines], ensure_ascii=False, indent=2))
        return 0
    ref = build_reference(a.image, [e for e in engines if e.available()])
    if a.out:
        Path(a.out).write_text(json.dumps(ref, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"engines": ref["engines"], "quality": ref["quality"], "layout": ref["layout"],
                      "lines": [(l["id"], l["engine"], l["text"]) for l in ref["lines"]]}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
