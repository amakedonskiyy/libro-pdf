"""
pdf_translator.py — ядро Libro PDF Builder.

Логіка:
  1) extract_blocks()  — дістає текстові БЛОКИ з координатами, розміром і стилем
                          (PyMuPDF get_text("dict")). Зображення НЕ чіпаємо.
  2) translate_blocks() — перекладає блоки через Groq (батчами, з перевіркою
                          кількості сегментів і fallback по одному).
  3) build_pdf()       — РЕДАКЦІЯ (apply_redactions) фізично ВИДАЛЯЄ оригінальний
                          текст із PDF (а не малює білий прямокутник зверху),
                          зображення лишаються, потім вставляємо переклад на ті ж
                          координати з автопідбором розміру шрифту.

Результат — справжній PDF з НАСТОЯЩИМ текстом (виділяється, шукається, малий
розмір файлу), а не картинка.
"""
import io
import os
import re
import json
import time
import requests
import fitz  # PyMuPDF

# ---------------------------------------------------------------- шрифти
# DejaVu має повну кирилицю. Підбираємо варіант під стиль оригіналу.
_FONT_DIR = "/usr/share/fonts/truetype/dejavu"
_FONTS = {
    ("sans", False, False): f"{_FONT_DIR}/DejaVuSans.ttf",
    ("sans", True,  False): f"{_FONT_DIR}/DejaVuSans-Bold.ttf",
    ("sans", False, True):  f"{_FONT_DIR}/DejaVuSans-Oblique.ttf",
    ("sans", True,  True):  f"{_FONT_DIR}/DejaVuSans-BoldOblique.ttf",
    ("serif", False, False): f"{_FONT_DIR}/DejaVuSerif.ttf",
    ("serif", True,  False): f"{_FONT_DIR}/DejaVuSerif-Bold.ttf",
    ("serif", False, True):  f"{_FONT_DIR}/DejaVuSerif-Italic.ttf",
    ("serif", True,  True):  f"{_FONT_DIR}/DejaVuSerif-BoldItalic.ttf",
}
# дозволяємо перевизначити каталог шрифтів (напр. покласти свій TTF у проект)
_FONT_DIR = os.environ.get("LIBRO_FONT_DIR", _FONT_DIR)


def _pick_font(flags: int, size: float):
    """flags з PyMuPDF span: bit1=italic(2), bit2=serif(4), bit4=bold(16)."""
    bold = bool(flags & 16) or size >= 15        # великий кегль ≈ заголовок
    italic = bool(flags & 2)
    serif = bool(flags & 4)
    fam = "serif" if serif else "sans"
    path = _FONTS[(fam, bold, italic)]
    fontname = os.path.basename(path).replace(".ttf", "").replace("-", "")
    return fontname, path


# ------- OCR-рятувalка для PDF з пошкодженим текстовим шаром -------
# Деякі книжки мають "битий" шрифт у заголовках/змісті/курсиві: текст
# витягується як абракадабра ("Гла)а 1. ,)еде-ие" замість "Глава 1. Введение").
# Такі блоки ми розпізнаємо OCR-ом (рендеримо як фото і читаємо очима).
_LANG_OCR = {"ru": "rus", "uk": "ukr", "ua": "ukr", "en": "eng"}


def _looks_garbled(text: str) -> bool:
    """Ознака битого тексту: цифра/дужка/кома, вклеєна ВСЕРЕДИНУ слова,
    або слово, що починається з цифри (напр. '1ГЛОР(23' = «ГЛОРИЯ»)."""
    if not text:
        return False
    letters = sum(ch.isalpha() for ch in text)
    if letters < 2:
        return False
    anomalies = len(re.findall(
        r"[А-Яа-яЇїІіЄєҐґA-Za-z][\)\(\,\d][А-Яа-яЇїІіЄєҐґA-Za-z]", text))
    anomalies += len(re.findall(r"\d[А-Яа-яЇїІіЄєҐґ]{2,}", text))  # '1ГЛОР'
    # короткий заголовок, у якому є і дужка, і цифра поряд із КИРИЛИЦЕЮ
    # (напр. 'ЧА)9Ь 1'); латинські посилання типу '(Spoerl, 1975)' не чіпаємо
    if (len(text) <= 40 and re.search(r"[\)\(]", text) and re.search(r"\d", text)
            and re.search(r"[А-Яа-яЇїІіЄєҐґ]", text)):
        return True
    return anomalies >= 2 or (anomalies >= 1 and len(text) <= 80)


def _ocr_region(page, bbox, lang="rus"):
    """Рендерить ділянку сторінки у ~300dpi і читає її Tesseract-ом."""
    try:
        import io
        import pytesseract
        from PIL import Image
        pix = page.get_pixmap(clip=fitz.Rect(bbox), matrix=fitz.Matrix(4, 4))
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        raw = pytesseract.image_to_string(img, lang=lang)
        # чистимо: крапки-заповнювачі змісту й зайві пробіли
        raw = re.sub(r"\.{2,}.*$", "", raw, flags=re.M)   # геть "....... 25"
        raw = re.sub(r"\s+", " ", raw).strip(" .")
        return raw
    except Exception as e:
        print("OCR недоступний/помилка:", e)
        return ""


def looks_scanned(pages) -> bool:
    """True, якщо книга схожа на СКАН: сторінки-зображення майже без тексту.
    Такі книги поточний рушій не перекладе (немає текстового шару)."""
    if not pages:
        return False
    n = len(pages)
    total_chars = sum(len(b["text"]) for pg in pages for b in pg)
    return n >= 3 and total_chars < 25 * n


# ---------------------------------------------------------------- 1. extract
def extract_blocks(pdf_bytes: bytes, ocr_lang: str = "rus"):
    """
    Повертає список сторінок. Кожна сторінка = list блоків:
      {id, page, bbox, text, size, color, fontname, fontfile}
    Якщо блок витягнувся "битим" (пошкоджений шрифт) — рятуємо його OCR-ом.
    Працюємо на рівні БЛОКУ (≈ абзац): краще для якості перекладу
    (цілі речення) і для розкладки (insert_textbox сам переносить рядки).
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = []
    gid = 0
    for pno, page in enumerate(doc):
        blocks = []
        data = page.get_text("dict")
        for b in data["blocks"]:
            if b.get("type") != 0:          # 0 = текст, 1 = зображення (пропускаємо)
                continue
            spans = [s for ln in b["lines"] for s in ln["spans"] if s["text"].strip()]
            if not spans:
                continue
            # текст блоку: рядки через \n, склейка переносів типу "сло-\nво"
            lines_txt = []
            for ln in b["lines"]:
                lt = "".join(s["text"] for s in ln["spans"]).rstrip()
                if lt:
                    lines_txt.append(lt)
            text = "\n".join(lines_txt)
            text = re.sub(r"-\n(?=[а-яёіїєґ])", "", text)   # перенос слова
            text = text.replace("\n", " ").strip()
            if not text:
                continue
            # якщо текст битий — рятуємо OCR-ом по цій же ділянці
            if _looks_garbled(text):
                fixed = _ocr_region(page, b["bbox"], ocr_lang)
                if fixed and not _looks_garbled(fixed):
                    text = fixed
            # домінуючий стиль = найбільший span
            main = max(spans, key=lambda s: s["size"])
            col = main.get("color", 0)
            rgb = ((col >> 16) & 255) / 255, ((col >> 8) & 255) / 255, (col & 255) / 255
            fontname, fontfile = _pick_font(main.get("flags", 0), main["size"])
            blocks.append({
                "id": gid,
                "page": pno,
                "bbox": list(b["bbox"]),
                "text": text,
                "size": round(main["size"], 1),
                "color": rgb,
                "fontname": fontname,
                "fontfile": fontfile,
            })
            gid += 1
        pages.append(blocks)
    doc.close()
    return pages


# ---------------------------------------------------------------- 2. translate
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_DELIM = "\n<<<§>>>\n"


class _ClientError(Exception):
    """4xx від провайдера (поганий ключ/модель/доступ) — повторювати марно."""
    pass

_SYS_PROMPT = (
    "Ти — професійний літературний перекладач книжок.\n"
    "Перекладай з {src} на {dst}.\n"
    "ЖОРСТКІ ПРАВИЛА:\n"
    "1. Виводь ВИКЛЮЧНО {dst} мовою. Жодного російського слова чи русизму, "
    "навіть якщо оригінал містить кальки. Перевіряй кожне слово.\n"
    "2. Не додавай і не прибирай речень. Зберігай зміст, тон і стиль автора.\n"
    "3. Власні імена транслітеруй за нормами {dst} мови.\n"
    "4. НЕ перекладай: бренди, абревіатури, формули, бібліопосилання "
    "типу (Spoerl, 1975), email, URL.\n"
    "5. Жодних коментарів, пояснень, лапок-обгорток — лише сам переклад.\n"
    "6. Якщо сегмент — число, символ або вже {dst} мовою — поверни без змін.\n"
    "7. Особливо пильнуй слова, що звучать майже однаково в обох мовах "
    "(міжмовні пастки): завжди обирай питомо {dst} відповідник, а не схожу "
    "кальку. Напр. рос. 'уделяется'→'приділяється', 'утешение'→'розрада', "
    "'горничная'→'покоївка', 'благоразумный'→'розважливий'.\n"
    "8. ТОЧНІСТЬ ФАКТІВ понад усе. Не змінюй факти, числа, назви, посилання. "
    "НЕ «локалізуй»: якщо в оригіналі Росія, РФ, «Федеральний закон», російське "
    "видання чи «російськомовні» — лишай саме так, НЕ заміняй на Україну."
)

_EDITOR_PROMPT = (
    "Ти — досвідчений літературний редактор-коректор. Тобі дають пари: "
    "ОРИГІНАЛ ({src}) і ЧЕРНЕТКА перекладу ({dst}). Для КОЖНОЇ пари поверни "
    "ВИПРАВЛЕНУ версію {dst} мовою.\n"
    "Виправ: русизми й кальки (став питоме {dst} слово); граматику, відмінки, "
    "узгодження роду й числа; зроби текст природним і живим.\n"
    "КРИТИЧНО: зміст МАЄ точно відповідати оригіналу — нічого не вигадуй і не "
    "пропускай. НЕ «локалізуй»: РФ/Росія/«Федеральний закон»/«російськомовні» "
    "лишаються як в оригіналі.\n"
    "Якщо чернетка вже бездоганна — поверни її без змін. "
    "Жодних коментарів — лише виправлений переклад."
)

_LANG = {"ru": "російської", "en": "англійської", "uk": "українську", "ua": "українську"}


def _groq_call(api_key, model, system, user, timeout=120, max_retries=8):
    """Виклик Groq з терпеливим повтором при лімітах (429) і збоях сервера (5xx).
    Поважає заголовок Retry-After, інакше — експоненційна затримка."""
    delay = 3.0
    last_err = None
    for attempt in range(max_retries):
        try:
            r = requests.post(
                _GROQ_URL,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={
                    "model": model,
                    "temperature": 0.2,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
                timeout=timeout,
            )
            if r.status_code == 429 or r.status_code >= 500:
                ra = r.headers.get("retry-after")
                wait = float(ra) if ra else delay
                print(f"Groq {r.status_code}, чекаю {wait:.0f}с (спроба {attempt+1})")
                time.sleep(min(wait, 90))
                delay = min(delay * 2, 90)
                continue
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except requests.RequestException as e:
            last_err = e
            time.sleep(delay)
            delay = min(delay * 2, 90)
    raise RuntimeError(f"Groq не відповів після {max_retries} спроб: {last_err}")


def _gemini_call(api_key, model, system, user, timeout=120, max_retries=5):
    """Виклик Gemini (Google AI). 4xx (крім 429) — одразу падаємо з причиною."""
    url = f"{_GEMINI_BASE}/{model}:generateContent"
    delay = 3.0
    last_err = None
    for attempt in range(max_retries):
        try:
            r = requests.post(
                url,
                params={"key": api_key},
                headers={"Content-Type": "application/json",
                         "x-goog-api-key": api_key},
                json={
                    "systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": user}]}],
                    "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192},
                },
                timeout=timeout,
            )
            if r.status_code == 429 or r.status_code >= 500:
                ra = r.headers.get("retry-after")
                wait = float(ra) if ra else delay
                print(f"Gemini {r.status_code}, чекаю {wait:.0f}с (спроба {attempt+1})")
                time.sleep(min(wait, 30))
                delay = min(delay * 2, 30)
                continue
            if r.status_code >= 400:
                raise _ClientError(f"Gemini {r.status_code}: {r.text[:300]}")
            cands = r.json().get("candidates", [])
            if not cands:
                raise _ClientError("Gemini: порожня відповідь (фільтр безпеки?)")
            parts = cands[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts)
        except requests.RequestException as e:
            last_err = e
            time.sleep(delay)
            delay = min(delay * 2, 30)
    raise RuntimeError(f"Gemini не відповів (мережа): {last_err}")


def _llm_call(provider, api_key, model, system, user):
    if provider == "gemini":
        return _gemini_call(api_key, model, system, user)
    return _groq_call(api_key, model, system, user)


def _default_model(provider):
    return "gemini-2.5-flash" if provider == "gemini" else "llama-3.3-70b-versatile"


def _make_batches(items_len, length_of, char_budget):
    """Групує індекси у батчі за сумарною довжиною."""
    batches, cur, cur_len = [], [], 0
    for i in range(items_len):
        L = length_of(i)
        if cur and cur_len + L > char_budget:
            batches.append(cur)
            cur, cur_len = [], 0
        cur.append(i)
        cur_len += L + len(_DELIM)
    if cur:
        batches.append(cur)
    return batches


def translate_blocks(texts, api_key, provider="gemini", model=None,
                     src="ru", dst="uk", char_budget=2500, proofread=False,
                     progress_cb=None):
    """
    Перекладає список рядків. Батчить, перевіряє кількість сегментів,
    fallback по одному. Якщо proofread=True — другий прохід-редактор.
    Прогрес рахується на обидві фази (переклад + редактор).
    """
    model = model or _default_model(provider)
    system = _SYS_PROMPT.format(src=_LANG.get(src, src), dst=_LANG.get(dst, dst))
    out = [None] * len(texts)
    total = len(texts)
    phases = 2 if proofread else 1
    total_work = max(total * phases, 1)
    work = [0]

    def report(n):
        work[0] += n
        if progress_cb:
            progress_cb(work[0], total_work)

    batches = _make_batches(total, lambda i: len(texts[i]), char_budget)
    for batch in batches:
        segs = [texts[i] for i in batch]
        user = ("Переклади кожен сегмент окремо. Сегменти розділені рядком <<<§>>>. "
                "Поверни переклади в тому ж порядку, розділені тим самим рядком <<<§>>>. "
                "Кількість сегментів МАЄ збігатися.\n\n" + _DELIM.join(segs))
        ok = False
        try:
            resp = _llm_call(provider, api_key, model, system, user)
            parts = [p.strip() for p in resp.split("<<<§>>>")]
            if len(parts) == len(batch):
                for k, idx in enumerate(batch):
                    out[idx] = parts[k] or texts[idx]
                ok = True
        except _ClientError:
            raise
        except Exception as e:
            print("batch error:", e)
        if not ok:
            for idx in batch:
                try:
                    out[idx] = _llm_call(provider, api_key, model, system,
                                         texts[idx]).strip() or texts[idx]
                except _ClientError:
                    raise
                except Exception as e:
                    print("single error:", e)
                    out[idx] = texts[idx]
        report(len(batch))
        time.sleep(0.2)

    if proofread:
        out = proofread_blocks(texts, out, api_key, provider, model, src, dst,
                               char_budget=char_budget, on_done=report)
    return out


def proofread_blocks(originals, drafts, api_key, provider, model, src, dst,
                     char_budget=2500, on_done=None):
    """Другий прохід: редактор виправляє чернетку, звіряючи з оригіналом."""
    system = _EDITOR_PROMPT.format(src=_LANG.get(src, src), dst=_LANG.get(dst, dst))
    out = list(drafts)
    n = len(drafts)

    def pair(i):
        return f"ОРИГІНАЛ:\n{originals[i]}\n\nЧЕРНЕТКА:\n{drafts[i]}"

    batches = _make_batches(n, lambda i: len(originals[i]) + len(drafts[i]), char_budget)
    for batch in batches:
        user = ("Виправ кожну пару. Пари розділені рядком <<<§>>>. Поверни лише "
                "виправлені переклади в тому ж порядку, розділені тим самим рядком "
                "<<<§>>>. Кількість МАЄ збігатися.\n\n"
                + _DELIM.join(pair(i) for i in batch))
        ok = False
        try:
            resp = _llm_call(provider, api_key, model, system, user)
            parts = [p.strip() for p in resp.split("<<<§>>>")]
            if len(parts) == len(batch):
                for k, idx in enumerate(batch):
                    if parts[k]:
                        out[idx] = parts[k]
                ok = True
        except Exception as e:
            print("proofread batch error:", e)
        if not ok:
            for idx in batch:
                try:
                    fixed = _llm_call(provider, api_key, model, system,
                                      pair(idx) + "\n\nПоверни лише виправлений переклад.")
                    if fixed.strip():
                        out[idx] = fixed.strip()
                except Exception as e:
                    print("proofread single error:", e)  # лишаємо чернетку
        if on_done:
            on_done(len(batch))
        time.sleep(0.2)
    return out


# ---------------------------------------------------------------- 3. build
def _place_text(page, bbox, text, fontname, fontfile, color, size):
    """Вставляє text у прямокутник bbox. Спершу зменшує шрифт, потім, якщо
    треба, нарощує висоту прямокутника вниз. Повертає True, якщо вмістив."""
    x0, y0, x1, y1 = bbox
    page_r = page.rect
    width = max(x1, x0 + 40)
    for extra in (0, 6, 14, 26, 44, 70):
        rect = fitz.Rect(x0 - 1, y0 - 1,
                         min(width + 2, page_r.x1 - 2),
                         min(y1 + 3 + extra, page_r.y1 - 2))
        sz = size
        while sz >= 5:
            rc = page.insert_textbox(rect, text, fontsize=sz, fontname=fontname,
                                     fontfile=fontfile, color=color, align=0)
            if rc >= 0:
                return True
            sz -= 0.5
    # зовсім не влізло — вставляємо найменшим, обрізане (краще ніж нічого)
    page.insert_textbox(fitz.Rect(x0 - 1, y0 - 1, page_r.x1 - 2, page_r.y1 - 2),
                        text, fontsize=5, fontname=fontname, fontfile=fontfile,
                        color=color, align=0)
    return False


def build_pdf(pdf_bytes: bytes, pages_blocks, translations: dict,
              keep_image_bg=True):
    """
    pdf_bytes      — оригінальний PDF.
    pages_blocks   — результат extract_blocks().
    translations   — {block_id: "переклад"}.
    Повертає bytes готового PDF.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    for blocks in pages_blocks:
        if not blocks:
            continue
        page = doc[blocks[0]["page"]]
        page_r = page.rect
        # 3a. РЕДАКЦІЯ: позначаємо й фізично видаляємо оригінальний текст
        for b in blocks:
            r = fitz.Rect(b["bbox"])
            r.x0 -= 1; r.y0 -= 1; r.x1 += 1; r.y1 += 1
            r = r & page_r                      # обов'язково в межах сторінки!
            if r.is_empty:
                continue
            # якщо текст лежить поверх зображення — підбираємо колір заливки під фон,
            # щоб не лишалося білої плями (інакше fill=біле)
            fill = (1, 1, 1)
            page.add_redact_annot(r, fill=fill)
        # зображення НЕ чіпаємо
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

        # 3b. Вставляємо переклад на ті самі координати
        for b in blocks:
            tgt = translations.get(b["id"], b["text"])
            if not tgt:
                continue
            _place_text(page, b["bbox"], tgt, b["fontname"], b["fontfile"],
                        b["color"], b["size"])

    out = io.BytesIO()
    doc.save(out, garbage=4, deflate=True)      # стиснення + дедуп шрифтів
    doc.close()
    return out.getvalue()


# ---------------------------------------------------------------- orchestrate
def translate_pdf(pdf_bytes, api_key, provider="gemini", model=None,
                  src="ru", dst="uk", proofread=False, progress_cb=None):
    """Повний цикл: extract → translate (+редактор) → build. Повертає bytes PDF."""
    pages = extract_blocks(pdf_bytes, ocr_lang=_LANG_OCR.get(src, "rus"))
    flat = [(b["id"], b["text"]) for blocks in pages for b in blocks]
    ids = [i for i, _ in flat]
    texts = [t for _, t in flat]
    translated = translate_blocks(texts, api_key, provider=provider, model=model,
                                  src=src, dst=dst, proofread=proofread,
                                  progress_cb=progress_cb)
    tmap = {ids[k]: translated[k] for k in range(len(ids))}
    return build_pdf(pdf_bytes, pages, tmap)


# ================================================================
#                  OCR-РЕЖИМ ДЛЯ СКАН-КНИГ (Варіант А)
# ================================================================
def _ocr_lines(page, lang="rus", dpi=200, min_conf=35):
    """OCR сторінки -> рядки [{text, bbox(в пунктах сторінки), bg(rgb 0..1)}]."""
    import pytesseract
    from PIL import Image
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    data = pytesseract.image_to_data(img, lang=lang,
                                     output_type=pytesseract.Output.DICT)
    groups = {}
    for i in range(len(data["text"])):
        txt = data["text"][i].strip()
        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1
        if not txt or conf < min_conf:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        x, y, w, h = (data["left"][i], data["top"][i],
                      data["width"][i], data["height"][i])
        g = groups.setdefault(key, {"w": [], "x0": 1e9, "y0": 1e9, "x1": 0, "y1": 0})
        g["w"].append(txt)
        g["x0"] = min(g["x0"], x); g["y0"] = min(g["y0"], y)
        g["x1"] = max(g["x1"], x + w); g["y1"] = max(g["y1"], y + h)
    lines = []
    for g in groups.values():
        # домінантний колір ділянки = фон (парчмент/папір)
        try:
            crop = img.crop((g["x0"], g["y0"], g["x1"], g["y1"]))
            cs = crop.getcolors(maxcolors=200000)
            bg = max(cs, key=lambda c: c[0])[1] if cs else (255, 255, 255)
        except Exception:
            bg = (255, 255, 255)
        lines.append({
            "text": " ".join(g["w"]),
            "bbox": (g["x0"] / zoom, g["y0"] / zoom, g["x1"] / zoom, g["y1"] / zoom),
            "bg": (bg[0] / 255, bg[1] / 255, bg[2] / 255),
        })
    lines.sort(key=lambda l: (round(l["bbox"][1] / 5), l["bbox"][0]))
    return lines


def translate_scanned_pdf(pdf_bytes, api_key, provider="gemini", model=None,
                          src="ru", dst="uk", proofread=False, progress_cb=None):
    """Скан-книга: OCR кожної сторінки -> переклад -> накладання поверх скану."""
    lang = _LANG_OCR.get(src, "rus")
    src_doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    # --- 1. OCR усіх сторінок (тримаємо лише текст+координати, не картинки) ---
    page_lines, texts, index = [], [], []
    for pno in range(src_doc.page_count):
        lines = _ocr_lines(src_doc[pno], lang)
        page_lines.append(lines)
        for li, l in enumerate(lines):
            texts.append(l["text"]); index.append((pno, li))

    # --- 2. переклад усіх рядків (той самий рушій, провайдер, редактор) ---
    translated = translate_blocks(texts, api_key, provider=provider, model=model,
                                  src=src, dst=dst, proofread=proofread,
                                  progress_cb=progress_cb)
    for k, (pno, li) in enumerate(index):
        page_lines[pno][li]["uk"] = translated[k]

    # --- 3. збірка: оригінальний скан як фон + накладений переклад ---
    fontfile = _FONTS[("serif", False, False)]
    out_doc = fitz.open()
    for pno in range(src_doc.page_count):
        rect = src_doc[pno].rect
        newp = out_doc.new_page(width=rect.width, height=rect.height)
        newp.show_pdf_page(rect, src_doc, pno)        # оригінальний скан як фон
        for l in page_lines[pno]:
            uk = l.get("uk", "").strip()
            if not uk:
                continue
            x0, y0, x1, y1 = l["bbox"]
            box = fitz.Rect(x0, y0, x1, y1)
            newp.draw_rect(box, color=None, fill=l["bg"])   # замазати оригінал фоном
            # вписуємо переклад, зменшуючи кегль, поки влізе
            grow = fitz.Rect(x0, y0, x1 + (x1 - x0) * 0.8, y1 + (y1 - y0) * 1.2)
            size = max(6.0, (y1 - y0) * 0.85)
            for _ in range(6):
                rc = newp.insert_textbox(grow, uk, fontfile=fontfile, fontname="ocr",
                                         fontsize=size, color=(0, 0, 0), align=0)
                if rc >= 0:
                    break
                size *= 0.85
    result = out_doc.tobytes(deflate=True, garbage=4)
    src_doc.close(); out_doc.close()
    return result


# ================================================================
#            ГЕНЕРАЦІЯ ОБКЛАДИНКИ З ПЕРЕКЛАДЕНОЮ НАЗВОЮ
# ================================================================
def _lum(c):
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


def _dominant_colors(img, k=6):
    from PIL import Image
    small = img.convert("RGB").resize((80, 120))
    q = small.quantize(colors=k, method=Image.MEDIANCUT).convert("RGB")
    cs = sorted(q.getcolors(80 * 120) or [], key=lambda c: -c[0])
    return [c[1] for c in cs] or [(40, 60, 80)]


def _guess_title_author(doc, ocr_lang="rus"):
    """Витягуємо назву й автора з ОБКЛАДИНКИ (OCR) — найбільший текст = назва."""
    lines = [l for l in _ocr_lines(doc[0], ocr_lang) if 1 < len(l["text"]) < 60]
    if not lines:
        return ("Без назви", "")
    for l in lines:
        l["h"] = l["bbox"][3] - l["bbox"][1]
    maxh = max(l["h"] for l in lines)
    big = sorted([l for l in lines if l["h"] >= 0.7 * maxh], key=lambda l: l["bbox"][1])
    title = " ".join(l["text"] for l in big)
    rest = sorted([l for l in lines if l["h"] < 0.7 * maxh], key=lambda l: -l["h"])
    author = rest[0]["text"] if rest else ""
    return (title[:120], author[:60])


def make_cover(pdf_bytes, api_key, provider="gemini", model=None,
               src="ru", dst="uk", title=None, author=None):
    """Нова обкладинка у стилі оригіналу + перекладена назва/автор + акценти."""
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    import textwrap, random
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pix = doc[0].get_pixmap(matrix=fitz.Matrix(1.2, 1.2))
    cover = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    pal = _dominant_colors(cover, 6)
    if not title:
        title, a2 = _guess_title_author(doc, _LANG_OCR.get(src, "rus"))
        author = author or a2
    src_lines = [title] + ([author] if author else [])
    tr = translate_blocks(src_lines, api_key, provider=provider, model=model, src=src, dst=dst)
    t_title = tr[0].strip()
    t_author = (tr[1].strip() if author else "")
    aspect = cover.height / cover.width if cover.width else 1.4
    doc.close()

    W = 1000
    H = max(1200, min(int(W * aspect), 1500))
    # фон-градієнт між двома виразними кольорами палітри
    c1 = max(pal[:3], key=lambda c: abs(_lum(c) - 110))
    c2 = pal[1] if len(pal) > 1 else c1
    base = Image.new("RGB", (W, H), c1)
    top = Image.new("RGB", (W, H), c2)
    mask = Image.new("L", (W, H))
    md = ImageDraw.Draw(mask)
    for y in range(H):
        md.line([(0, y), (W, y)], fill=int(255 * y / H))
    base = Image.composite(base, top, mask).convert("RGBA")
    # м'які painterly-форми у палітрі (окремі елементи)
    blob = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bd = ImageDraw.Draw(blob)
    random.seed(len(pdf_bytes))
    for i in range(6):
        col = pal[i % len(pal)]
        r = random.randint(W // 4, W // 2)
        x = random.randint(-r // 3, W); y = random.randint(-r // 3, H)
        bd.ellipse([x - r, y - r, x + r, y + r], fill=(col[0], col[1], col[2], 70))
    base = Image.alpha_composite(base, blob.filter(ImageFilter.GaussianBlur(70))).convert("RGB")
    draw = ImageDraw.Draw(base)

    txt_col = (255, 255, 255) if _lum(c1) < 140 else (25, 25, 25)
    accent = max(pal, key=lambda c: (max(c) - min(c)))
    fb = _FONTS[("sans", True, False)]

    # заголовок: підбираємо кегль і переносимо
    title_up = t_title.upper()
    size = 88
    wrapped = [title_up]
    while size > 30:
        font = ImageFont.truetype(fb, size)
        maxchars = max(6, int((W - 150) / max(font.getlength("М"), 1)))
        wrapped = textwrap.wrap(title_up, width=maxchars)
        if len(wrapped) * size * 1.15 < H * 0.5 and len(wrapped) <= 6:
            break
        size -= 4
    y = H * 0.20
    for line in wrapped:
        w = font.getlength(line)
        draw.text(((W - w) / 2, y), line, font=font, fill=txt_col)
        y += size * 1.15
    # акцент-лінія
    ly = y + 28
    draw.rectangle([W * 0.36, ly, W * 0.64, ly + 7], fill=accent)
    # автор
    if t_author:
        af = ImageFont.truetype(fb, 42)
        aw = af.getlength(t_author.upper())
        draw.text(((W - aw) / 2, H * 0.83), t_author.upper(), font=af, fill=txt_col)

    out = io.BytesIO(); base.save(out, "PNG")
    return out.getvalue()
