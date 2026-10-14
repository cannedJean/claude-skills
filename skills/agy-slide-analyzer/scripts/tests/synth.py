"""synth.py - render synthetic lecture slides whose text is known exactly (used by the tests and as a
smoke input). Korean needs a system font: Malgun Gothic on Windows, falls back to any TTF found."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [r"C:\Windows\Fonts\malgun.ttf", "/System/Library/Fonts/AppleSDGothicNeo.ttc",
                   "/usr/share/fonts/truetype/nanum/NanumGothic.ttf", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"]
BOLD_CANDIDATES = [r"C:\Windows\Fonts\malgunbd.ttf"] + FONT_CANDIDATES
MONO_CANDIDATES = [r"C:\Windows\Fonts\consola.ttf", "/System/Library/Fonts/Menlo.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"]


def font(cands, size):
    for c in cands:
        if Path(c).exists():
            return ImageFont.truetype(c, size)
    return ImageFont.load_default()


def slide_main(path: Path):
    """Title, two sentences, one SQL line, a 3x3 ruled table, two shapes joined by a line, a page number."""
    img = Image.new("RGB", (1280, 720), "white")
    d = ImageDraw.Draw(img)
    d.text((60, 40), "3장. 기본 키와 외래 키", font=font(BOLD_CANDIDATES, 48), fill="black")
    body = font(FONT_CANDIDATES, 30)
    d.text((60, 140), "기본 키는 NULL을 허용하지 않는다.", font=body, fill="black")
    d.text((60, 190), "기본 키는 테이블당 하나만 존재한다. (2024년 기준 1,024개 행)", font=body, fill="black")
    d.text((60, 280), "SELECT id, name FROM students WHERE grade = 'A';", font=font(MONO_CANDIDATES, 26), fill="black")
    x0, y0 = 60, 360
    for i in range(4):
        d.line((x0, y0 + i * 45, x0 + 600, y0 + i * 45), fill="black", width=2)
    for j in range(4):
        d.line((x0 + j * 200, y0, x0 + j * 200, y0 + 135), fill="black", width=2)
    cells = [["구분", "파일 시스템", "DBMS"], ["중복", "높음", "최소화"], ["무결성", "응용 책임", "제약 조건"]]
    small = font(FONT_CANDIDATES, 22)
    for r, row in enumerate(cells):
        for c, v in enumerate(row):
            d.text((x0 + c * 200 + 10, y0 + r * 45 + 8), v, font=small, fill="black")
    d.ellipse((800, 380, 1000, 520), outline="black", width=4)
    d.rectangle((1050, 400, 1230, 500), outline="black", width=4)
    d.line((1000, 450, 1050, 450), fill="black", width=4)
    d.text((1180, 670), "12", font=body, fill="gray")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


def slide_title_only(path: Path):
    img = Image.new("RGB", (1280, 720), "white")
    d = ImageDraw.Draw(img)
    d.text((300, 300), "2부. 관계형 모델", font=font(BOLD_CANDIDATES, 64), fill="black")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


def slide_diagram(path: Path):
    """Mostly graphics: boxes and arrows with short labels (graphics trigger)."""
    img = Image.new("RGB", (1280, 720), "white")
    d = ImageDraw.Draw(img)
    d.text((60, 40), "ERD 예시", font=font(BOLD_CANDIDATES, 44), fill="black")
    for (x, y, label) in ((150, 250, "학생"), (560, 250, "수강"), (970, 250, "과목")):
        d.rectangle((x, y, x + 220, y + 160), fill=(200, 220, 255), outline="black", width=5)
        d.text((x + 60, y + 60), label, font=font(FONT_CANDIDATES, 36), fill="black")
    d.line((370, 330, 560, 330), fill="black", width=8)
    d.line((780, 330, 970, 330), fill="black", width=8)
    d.polygon([(560, 330), (530, 310), (530, 350)], fill="black")
    d.polygon([(970, 330), (940, 310), (940, 350)], fill="black")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


def make_deck(folder: Path) -> list:
    folder.mkdir(parents=True, exist_ok=True)
    return [slide_title_only(folder / "slide_001.png"), slide_main(folder / "slide_002.png"),
            slide_diagram(folder / "slide_003.png")]


if __name__ == "__main__":
    import sys
    print([str(p) for p in make_deck(Path(sys.argv[1] if len(sys.argv) > 1 else "synth_deck"))])
