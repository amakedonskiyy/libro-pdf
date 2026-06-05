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


def _groq_call(api_key, model, system, user, timeout=120, max_retries=5):
    """Виклик Groq. 429/5xx — терплячий повтор; 4xx (погана модель/ключ) — одразу падаємо."""
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
                time.sleep(min(wait, 30))
                delay = min(delay * 2, 30)
                continue
            if r.status_code >= 400:
                raise _ClientError(f"Groq {r.status_code}: {r.text[:300]}")
            return r.json()["choices"][0]["message"]["content"]
        except requests.RequestException as e:
            last_err = e
            time.sleep(delay)
            delay = min(delay * 2, 30)
    raise RuntimeError(f"Groq не відповів (мережа): {last_err}")


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


def _sample_bg(img, x0, y0, x1, y1, pad=6):
    """Домінантний колір РАМКИ навколо боксу (фон сторінки біля тексту), rgb 0..255."""
    W, H = img.size
    x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
    bands = [(x0 - pad, y0 - pad, x1 + pad, y0), (x0 - pad, y1, x1 + pad, y1 + pad),
             (x0 - pad, y0, x0, y1), (x1, y0, x1 + pad, y1)]
    cnt = {}
    for bx0, by0, bx1, by1 in bands:
        bx0, by0 = max(0, bx0), max(0, by0)
        bx1, by1 = min(W, bx1), min(H, by1)
        if bx1 <= bx0 or by1 <= by0:
            continue
        crop = img.crop((bx0, by0, bx1, by1)).convert("RGB")
        for c, col in (crop.getcolors(crop.width * crop.height) or []):
            cnt[col] = cnt.get(col, 0) + c
    return max(cnt.items(), key=lambda kv: kv[1])[0] if cnt else (255, 255, 255)


def build_pdf(pdf_bytes: bytes, pages_blocks, translations: dict,
              keep_image_bg=True, recipe=None):
    """
    pdf_bytes      — оригінальний PDF.
    pages_blocks   — результат extract_blocks().
    translations   — {block_id: "переклад"}.
    recipe         — правила від зору (напр. {"uniform_bg":true,"page_bg":[245,240,225]}).
    Повертає bytes готового PDF.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    recipe = recipe or {}
    # глобальний колір фону від зору (якщо книга однотонна) — 0..1
    uni_bg = _rgb01(recipe["page_bg"]) if (recipe.get("uniform_bg") and recipe.get("page_bg")) else None

    for blocks in pages_blocks:
        if not blocks:
            continue
        page = doc[blocks[0]["page"]]
        page_r = page.rect
        # рендеримо сторінку (раз) ЛИШЕ якщо немає глобального фону і є зображення —
        # щоб брати колір фону під блоком і не лишати білих плям на кольорі/картинці
        pimg, zoom = None, 2.0
        if uni_bg is None and page.get_images():
            try:
                from PIL import Image
                pm = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
                pimg = Image.open(io.BytesIO(pm.tobytes("png"))).convert("RGB")
            except Exception:
                pimg = None
        # 3a. РЕДАКЦІЯ: фізично видаляємо оригінальний текст, заливаючи ФОНОМ
        for b in blocks:
            r = fitz.Rect(b["bbox"])
            r.x0 -= 1; r.y0 -= 1; r.x1 += 1; r.y1 += 1
            r = r & page_r                      # обов'язково в межах сторінки!
            if r.is_empty:
                continue
            if uni_bg is not None:               # зір: один колір на всю книгу
                fill = uni_bg
            elif pimg is not None:               # підбір під фон сторінки
                x0, y0, x1, y1 = b["bbox"]
                c = _sample_bg(pimg, x0 * zoom, y0 * zoom, x1 * zoom, y1 * zoom)
                fill = (c[0] / 255, c[1] / 255, c[2] / 255)
            else:
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
        bg = _sample_bg(img, g["x0"], g["y0"], g["x1"], g["y1"], pad=8)
        lines.append({
            "text": " ".join(g["w"]),
            "bbox": (g["x0"] / zoom, g["y0"] / zoom, g["x1"] / zoom, g["y1"] / zoom),
            "bg": bg,   # rgb 0..255
        })
    lines.sort(key=lambda l: (round(l["bbox"][1] / 5), l["bbox"][0]))
    return lines


def translate_scanned_pdf(pdf_bytes, api_key, provider="gemini", model=None,
                          src="ru", dst="uk", proofread=False, progress_cb=None):
    """Скан-книга: OCR кожної сторінки -> переклад -> накладання поверх скану."""
    lang = _LANG_OCR.get(src, "rus")
    src_doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    # --- 1. OCR усіх сторінок (тримаємо лише текст+координати, не картинки) ---
    npages = max(src_doc.page_count, 1)
    page_lines, texts, index = [], [], []
    for pno in range(src_doc.page_count):
        lines = _ocr_lines(src_doc[pno], lang)
        page_lines.append(lines)
        for li, l in enumerate(lines):
            texts.append(l["text"]); index.append((pno, li))
        if progress_cb:                       # OCR займає шкалу 0..40%
            progress_cb(int(40 * (pno + 1) / npages), 100)

    # --- 2. переклад усіх рядків (той самий рушій, провайдер, редактор) ---
    def _shim(done, total):                   # переклад займає 40..98%
        if progress_cb:
            progress_cb(40 + int(58 * done / max(total, 1)), 100)
    translated = translate_blocks(texts, api_key, provider=provider, model=model,
                                  src=src, dst=dst, proofread=proofread,
                                  progress_cb=_shim)
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
            # переклад не вдався (= оригінал) → лишаємо оригінал, НЕ замазуємо
            if not uk or uk == l["text"].strip():
                continue
            x0, y0, x1, y1 = l["bbox"]
            bg = l["bg"]                                   # 0..255
            newp.draw_rect(fitz.Rect(x0, y0, x1, y1), color=None,
                           fill=(bg[0] / 255, bg[1] / 255, bg[2] / 255))
            # колір тексту під фон: темний фон → білий текст, світлий → темний
            tc = (1, 1, 1) if _lum(bg) < 130 else (0.1, 0.1, 0.1)
            grow = fitz.Rect(x0, y0, x1 + (x1 - x0) * 0.8, y1 + (y1 - y0) * 1.3)
            size = max(6.0, (y1 - y0) * 0.85)
            for _ in range(7):
                rc = newp.insert_textbox(grow, uk, fontfile=fontfile, fontname="ocr",
                                         fontsize=size, color=tc, align=0)
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


def _cover_lines(doc, ocr_lang="rus"):
    """OCR обкладинки -> (рядки назви, рядки автора) з координатами (в пунктах)."""
    lines = [l for l in _ocr_lines(doc[0], ocr_lang) if 1 < len(l["text"]) < 60]
    if not lines:
        return [], []
    for l in lines:
        l["h"] = l["bbox"][3] - l["bbox"][1]
    maxh = max(l["h"] for l in lines)
    title = sorted([l for l in lines if l["h"] >= 0.7 * maxh], key=lambda l: l["bbox"][1])
    author = sorted([l for l in lines if l["h"] < 0.7 * maxh], key=lambda l: -l["h"])[:1]
    return title, author


def make_cover(pdf_bytes, api_key, provider="gemini", model=None,
               src="ru", dst="uk", title=None, author=None, recipe=None):
    """Однотонний фон -> заміна назви НА МІСЦІ (оригінал лишається).
    Складний фон -> нова обкладинка у стилі оригіналу. Текст завжди наш (чіткий)."""
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    import textwrap, random
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    M = 2.0
    pix = doc[0].get_pixmap(matrix=fitz.Matrix(M, M))
    cover = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    pal = _dominant_colors(cover, 6)
    tlines, alines = _cover_lines(doc, _LANG_OCR.get(src, "rus"))
    title = title or (" ".join(l["text"] for l in tlines) if tlines else "Без назви")
    if author is None:
        author = alines[0]["text"] if alines else ""
    tr = translate_blocks([title] + ([author] if author else []), api_key,
                          provider=provider, model=model, src=src, dst=dst)
    t_title = (tr[0].strip() or title)
    t_author = (tr[1].strip() if author else "")
    doc.close()
    fb = _FONTS[("sans", True, False)]

    def fit_font(text, box_w, box_h, start):
        size = max(14, int(start))
        while size > 12:
            f = ImageFont.truetype(fb, size)
            mc = max(4, int(box_w / max(f.getlength("М"), 1)))
            wr = textwrap.wrap(text, width=mc) or [text]
            if len(wr) * size * 1.12 <= box_h and max(f.getlength(w) for w in wr) <= box_w:
                return f, wr, size
            size -= 3
        f = ImageFont.truetype(fb, 13)
        return f, (textwrap.wrap(text, width=max(4, int(box_w / max(f.getlength('М'), 1)))) or [text]), 13

    # однотонність фону: яку частку площі займає домінантний колір
    q = cover.resize((80, 120)).quantize(colors=6).convert("RGB")
    qc = sorted(q.getcolors(80 * 120) or [(1, (0, 0, 0))], key=lambda c: -c[0])
    uniform = qc[0][0] / (80 * 120) > 0.5
    cm = (recipe or {}).get("cover_mode")          # зір може перевизначити
    if cm == "replace":
        uniform = True
    elif cm == "generate":
        uniform = False

    # ============ РЕЖИМ ЗАМІНИ (однотонний фон) ============
    if uniform and tlines:
        work = cover.copy()
        wd = ImageDraw.Draw(work)

        def replace_block(group, text):
            if not group or not text:
                return
            x0 = min(l["bbox"][0] for l in group) * M
            y0 = min(l["bbox"][1] for l in group) * M
            x1 = max(l["bbox"][2] for l in group) * M
            y1 = max(l["bbox"][3] for l in group) * M
            bg = _sample_bg(work, x0, y0, x1, y1, pad=12)
            wd.rectangle([x0 - 4, y0 - 4, x1 + 4, y1 + 4], fill=bg)
            tc = (255, 255, 255) if _lum(bg) < 130 else (20, 20, 20)
            per_line = (y1 - y0) / max(len(group), 1)
            f, wr, sz = fit_font(text.upper(), (x1 - x0) * 1.15, (y1 - y0) * 1.4, per_line * 1.05)
            yy = y0
            for ln in wr:
                w = f.getlength(ln)
                wd.text((x0 + ((x1 - x0) - w) / 2, yy), ln, font=f, fill=tc)
                yy += sz * 1.12

        replace_block(tlines, t_title)
        replace_block(alines, t_author)
        out = io.BytesIO(); work.save(out, "PNG")
        return out.getvalue()

    # ============ РЕЖИМ ГЕНЕРАЦІЇ (складний фон) ============
    W = 1000
    H = max(1200, min(int(W * (cover.height / max(cover.width, 1))), 1500))
    c1 = max(pal[:3], key=lambda c: abs(_lum(c) - 110))
    c2 = pal[1] if len(pal) > 1 else c1
    base = Image.new("RGB", (W, H), c1)
    top = Image.new("RGB", (W, H), c2)
    mask = Image.new("L", (W, H)); md = ImageDraw.Draw(mask)
    for y in range(H):
        md.line([(0, y), (W, y)], fill=int(255 * y / H))
    base = Image.composite(base, top, mask).convert("RGBA")
    blob = Image.new("RGBA", (W, H), (0, 0, 0, 0)); bd = ImageDraw.Draw(blob)
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
    font, wrapped, size = fit_font(t_title.upper(), W - 150, H * 0.5, 88)
    y = H * 0.20
    for line in wrapped:
        w = font.getlength(line)
        draw.text(((W - w) / 2, y), line, font=font, fill=txt_col)
        y += size * 1.15
    ly = y + 28
    draw.rectangle([W * 0.36, ly, W * 0.64, ly + 7], fill=accent)
    if t_author:
        af = ImageFont.truetype(fb, 42)
        aw = af.getlength(t_author.upper())
        draw.text(((W - aw) / 2, H * 0.83), t_author.upper(), font=af, fill=txt_col)
    out = io.BytesIO(); base.save(out, "PNG")
    return out.getvalue()


# ================================================================
#         ЗІР: аналіз перших N сторінок -> правила (recipe)
# ================================================================
_VISION_PROMPT = (
    "You analyze pages of a book to set rendering parameters for a tool that ERASES "
    "the original text and writes translated text in its place. Look at page "
    "backgrounds and text colour. Return ONLY a JSON object, no prose, no markdown:\n"
    '{"uniform_bg": true|false, "page_bg": [r,g,b] or null, '
    '"text_color": [r,g,b] or null, "scanned": true|false, '
    '"cover_mode": "replace" or "generate", "cover_bg": [r,g,b] or null}\n'
    "Rules:\n"
    "- uniform_bg=true ONLY if the CONTENT pages share one consistent background colour "
    "(all white, or all the same cream/parchment/dark). page_bg = that colour in 0-255 RGB.\n"
    "- If backgrounds vary (white text pages mixed with coloured pages or photos), "
    "uniform_bg=false and page_bg=null.\n"
    "- text_color = the main body text colour in 0-255 RGB (usually dark).\n"
    "- scanned=true if pages are photographed/scanned images, not crisp digital text.\n"
    "- cover_mode='replace' if the FIRST page (cover) has a flat area where the title can be "
    "overwritten in place; 'generate' if the cover is a busy photo/illustration.\n"
    "- cover_bg = the cover's dominant background colour in 0-255 RGB."
)


def _page_images_b64(pdf_bytes, n=10, width=820):
    import base64
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    out = []
    for pno in range(min(n, doc.page_count)):
        pg = doc[pno]
        zoom = width / max(pg.rect.width, 1)
        pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        out.append(base64.b64encode(pix.tobytes("png")).decode())
    doc.close()
    return out


def _vision_call(provider, api_key, model, prompt, images_b64, timeout=120):
    if provider == "gemini":
        parts = [{"text": prompt}] + [{"inline_data": {"mime_type": "image/png", "data": b}}
                                      for b in images_b64]
        r = requests.post(
            f"{_GEMINI_BASE}/{model}:generateContent",
            params={"key": api_key},
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            json={"contents": [{"role": "user", "parts": parts}],
                  "generationConfig": {"temperature": 0.1, "maxOutputTokens": 1024}},
            timeout=timeout)
        if r.status_code >= 400:
            raise _ClientError(f"Gemini vision {r.status_code}: {r.text[:300]}")
        cands = r.json().get("candidates", [])
        return "".join(p.get("text", "") for p in cands[0]["content"]["parts"]) if cands else ""
    # groq / openai-сумісний
    content = [{"type": "text", "text": prompt}] + [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}}
        for b in images_b64]
    r = requests.post(
        _GROQ_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": model, "temperature": 0.1,
              "messages": [{"role": "user", "content": content}]},
        timeout=timeout)
    if r.status_code >= 400:
        raise _ClientError(f"Groq vision {r.status_code}: {r.text[:300]}")
    return r.json()["choices"][0]["message"]["content"]


def _default_vision_model(provider):
    return ("gemini-2.5-flash" if provider == "gemini"
            else "meta-llama/llama-4-scout-17b-16e-instruct")


def analyze_book(pdf_bytes, api_key, provider="groq", model=None, n=10):
    """Зір дивиться перші n сторінок -> правила (recipe) для всієї книги.
    На будь-якій помилці повертає {} (движок працює як без зору)."""
    import json, re
    model = model or _default_vision_model(provider)
    try:
        imgs = _page_images_b64(pdf_bytes, n=n)
        if not imgs:
            return {}
        raw = _vision_call(provider, api_key, model, _VISION_PROMPT, imgs)
        m = re.search(r"\{.*\}", raw, re.S)
        rec = json.loads(m.group(0)) if m else {}
        print("vision recipe:", rec)
        return rec if isinstance(rec, dict) else {}
    except Exception as e:
        print("analyze_book failed:", e)
        return {}


def _rgb01(c):
    return (c[0] / 255, c[1] / 255, c[2] / 255) if c else None
