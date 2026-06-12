"""
tests/smoke.py — смоук-тест ядра Libro PDF Builder. БЕЗ СЕТИ, < 30 сек.

Что делает:
  1. Генерирует tests/sample_5p.pdf: 5 страниц, русский текст абзацами,
     один крупный заголовок, одна картинка (PyMuPDF + DejaVu).
  2. Мокает pdf_translator._llm_call — фиксированный «перевод», без сети.
  3. Гоняет полный пайплайн: extract_blocks → build_glossary →
     translate_blocks → build_pdf.
  4. Проверяет: PDF собрался (5 страниц), картинка на месте, 0 битых блоков
     в выходе, 0 текстовых блоков за пределами страниц, перевод реально
     вставлен. Плюс калибровочные кейсы _looks_garbled (правило 3 CLAUDE.md).

Запуск из корня репозитория:  .venv/bin/python tests/smoke.py
Зелёный = "SMOKE OK" и код выхода 0.
"""
import io
import os
import sys
import time

_T0 = time.time()
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import fitz  # noqa: E402
import pdf_translator as P  # noqa: E402

SAMPLE = os.path.join(_ROOT, "tests", "sample_5p.pdf")
IMG_PAGE = 1          # на этой странице (0-based) живёт картинка
UK_MARK = "Тестовий переклад"   # маркер мок-перевода в готовом PDF

RU_PARA = (
    "В 1990-х годах исследователи уделяли особое внимание развитию "
    "когнитивных навыков у детей школьного возраста. Телефон лаборатории: "
    "8 (495) 703-73-93. Результаты опубликованы в сборнике, ББК 88.5, "
    "УДК 159.9. Каждый абзац этой страницы написан обычным русским языком "
    "и не должен считаться битым."
)


def fail(msg):
    print(f"SMOKE FAIL: {msg}")
    sys.exit(1)


# ---------------------------------------------------------------- 0. калибровка
def check_garbled_calibration():
    """Правило 3 CLAUDE.md: телефоны, годы, ББК/УДК проходят, мусор ловится."""
    must_pass = [
        "В 1990-х годах исследователи уделяли внимание памяти.",
        "Тел.: 8 (495) 703-73-93",
        "ББК 88.5",
        "УДК 159.9",
        "Глава 1. Введение",
        "слова как-то, где-то и когда-то",
    ]
    must_catch = [
        "34567",                  # глифовый мусор битого cmap (баг «34567»)
        "ЧА)9Ь 1",                # дужка+цифра в коротком кириллическом заголовке
        "Гла)а 1. ,)еде-ие",      # классический битый заголовок
    ]
    for t in must_pass:
        if P._looks_garbled(t):
            fail(f"_looks_garbled ложно сработал на: {t!r}")
    for t in must_catch:
        if not P._looks_garbled(t):
            fail(f"_looks_garbled пропустил мусор: {t!r}")


# ---------------------------------------------------------------- 1. sample PDF
def make_sample():
    serif = P._FONTS[("serif", False, False)]
    serif_bold = P._FONTS[("serif", True, False)]
    if not os.path.exists(serif):
        fail(f"нет шрифта DejaVu: {serif} (положи TTF в .fonts/dejavu "
             f"или задай LIBRO_FONT_DIR)")

    doc = fitz.open()
    for pno in range(5):
        page = doc.new_page(width=420, height=595)  # ~A5
        y = 50
        if pno == 0:
            page.insert_textbox(fitz.Rect(40, y, 380, y + 40),
                                "ГЛАВА ПЕРВАЯ", fontsize=24,
                                fontname="smokebold", fontfile=serif_bold)
            y += 60
        if pno == IMG_PAGE:
            # картинка: градиентный PNG 120x80 через Pillow
            from PIL import Image
            img = Image.new("RGB", (120, 80))
            img.putdata([(x * 2, yy * 3, 120) for yy in range(80) for x in range(120)])
            buf = io.BytesIO()
            img.save(buf, "PNG")
            page.insert_image(fitz.Rect(150, y, 270, y + 80), stream=buf.getvalue())
            y += 100
        for _ in range(3):
            rc = page.insert_textbox(fitz.Rect(40, y, 380, y + 120), RU_PARA,
                                     fontsize=10, fontname="smoke",
                                     fontfile=serif)
            if rc < 0:
                fail(f"sample: абзац не влез на страницу {pno}")
            y += 135
    doc.save(SAMPLE)
    doc.close()


# ---------------------------------------------------------------- 2. мок LLM
def make_mock():
    """Фиксированный «перевод» без сети. Отвечает и на глоссарий (JSON),
    и на батч-перевод (то же число сегментов через <<<§>>>)."""
    def mock_llm(provider, api_key, model, system, user):
        if "термінолог" in system or "JSON-об" in system:
            return '{"когнитивный": "когнітивний", "глава": "розділ"}'
        parts = user.split("<<<§>>>")
        outs = []
        for p in parts:
            # длина «перевода» ~ как у оригинала (украинский длиннее на ~10%)
            n = max(1, int(len(p) * 1.1) // 55)
            outs.append(" ".join([f"{UK_MARK} абзацу українською мовою."] * n))
        return "\n<<<§>>>\n".join(outs)
    return mock_llm


# ---------------------------------------------------------------- 3. пайплайн
def run_pipeline(pdf_bytes):
    P._llm_call = make_mock()  # мок: никакой сети
    pages = P.extract_blocks(pdf_bytes, ocr_lang="rus")
    nb = sum(len(p) for p in pages)
    if len(pages) != 5 or nb < 10:
        fail(f"extract: страниц={len(pages)}, блоков={nb} (ожидалось 5 и >=10)")
    if any(b["garbled"] for blk in pages for b in blk):
        fail("extract: чистый sample дал «битые» блоки")

    flat = [(b["id"], b["text"]) for blk in pages for b in blk]
    glossary = P.build_glossary([t for _, t in flat], api_key="mock")
    if not isinstance(glossary, dict) or not glossary:
        fail(f"глоссарий не построился: {glossary!r}")

    tr = P.translate_blocks([t for _, t in flat], api_key="mock",
                            glossary=glossary)
    if len(tr) != len(flat) or any(not (t or "").strip() for t in tr):
        fail("translate_blocks: число/пустота сегментов")

    tmap = {flat[i][0]: tr[i] for i in range(len(flat))}
    return P.build_pdf(pdf_bytes, pages, tmap), pages


# ---------------------------------------------------------------- 4. проверки
def check_output(out_bytes):
    doc = fitz.open(stream=out_bytes, filetype="pdf")
    if doc.page_count != 5:
        fail(f"выход: {doc.page_count} страниц вместо 5")
    if not doc[IMG_PAGE].get_images(full=True):
        fail(f"выход: картинка пропала со страницы {IMG_PAGE}")

    got_mark, garbled, outside = False, [], []
    for pno, page in enumerate(doc):
        pr = page.rect
        for b in page.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            text = " ".join(s["text"] for ln in b["lines"]
                            for s in ln["spans"]).strip()
            if not text:
                continue
            if UK_MARK in text:
                got_mark = True
            if P._looks_garbled(text):
                garbled.append((pno, text[:60]))
            bb = fitz.Rect(b["bbox"])
            eps = 1.0
            if (bb.x0 < pr.x0 - eps or bb.y0 < pr.y0 - eps
                    or bb.x1 > pr.x1 + eps or bb.y1 > pr.y1 + eps):
                outside.append((pno, list(bb)))
    doc.close()
    if garbled:
        fail(f"выход: битые блоки: {garbled}")
    if outside:
        fail(f"выход: блоки за пределами страницы: {outside}")
    if not got_mark:
        fail("выход: перевод не найден в готовом PDF")


def check_generate_cover():
    """generate_cover — чистая функция (без сети): палитра k-means, шаблон
    по числу элементов, PNG нужного размера. Не должна падать."""
    from PIL import Image, ImageDraw
    src = Image.new("RGB", (300, 450), (228, 222, 205))
    ImageDraw.Draw(src).rectangle([0, 0, 300, 120], fill=(60, 30, 25))
    buf = io.BytesIO()
    src.save(buf, "PNG")
    blocks = [
        {"text": "Заглавие", "uk": "НАЗВА КНИГИ", "role": "title",
         "bbox_pct": [10, 30, 80, 12], "color": "#202020"},
        {"text": "автор", "uk": "Імʼя Автора", "role": "author",
         "bbox_pct": [10, 10, 80, 6], "color": "#202020"},
        {"text": "подзаголовок", "uk": "Тестовий підзаголовок книги",
         "role": "subtitle", "bbox_pct": [10, 50, 80, 8], "color": "#202020"},
        {"text": "серия", "uk": "Серія «Тест»", "role": "other",
         "bbox_pct": [10, 80, 80, 5], "color": "#202020"},
    ]
    png = P.generate_cover(blocks, buf.getvalue(), 0, 0)
    out = Image.open(io.BytesIO(png))
    if out.size != (300, 450):
        fail(f"generate_cover: размер {out.size} вместо (300, 450)")


def check_cover_truth_correction():
    """Сверка vision-чтения обложки с текстовым слоем титула (без сети):
    «НАП» (дизайнерская Л≈А) -> «НЛП» по известному слову; далёкие слова не
    трогаются; известный перевод подменяет рендер."""
    if P._levenshtein("нап", "нлп") != 1 or P._levenshtein("кот", "нлп") != 3:
        fail("_levenshtein считает неверно")
    wm = {"нлп": "НЛП", "яростное": "ЯРОСТНОЕ", "пелехатый": "Пелехатый"}
    fixed, ch = P._correct_text_by_words("НАП", wm)
    if fixed != "НЛП" or not ch:
        fail(f"коррекция НАП->НЛП не сработала: {fixed!r} changed={ch}")
    # точный заголовок не трогаем
    if P._correct_text_by_words("ЯРОСТНОЕ", wm) != ("ЯРОСТНОЕ", False):
        fail("ложная коррекция точного слова ЯРОСТНОЕ")
    # далёкое короткое слово не подменяем
    if P._correct_text_by_words("КОТ", wm)[1]:
        fail("ложная коррекция далёкого слова КОТ")
    # известный перевод для рендера
    if P._override_uk_by_truth("Михаил Пелехатый",
                               [{"orig": "Михаил Пелехатый",
                                 "uk": "Михайло Пелехатий"}]) != "Михайло Пелехатий":
        fail("_override_uk_by_truth не подменил известный перевод")


def main():
    check_garbled_calibration()
    check_cover_truth_correction()
    make_sample()
    with open(SAMPLE, "rb") as f:
        pdf_bytes = f.read()
    out_bytes, _ = run_pipeline(pdf_bytes)
    check_output(out_bytes)
    check_generate_cover()
    dt = time.time() - _T0
    if dt >= 30:
        fail(f"слишком долго: {dt:.1f} c (лимит 30)")
    print(f"SMOKE OK ({dt:.1f} c)")


if __name__ == "__main__":
    main()
