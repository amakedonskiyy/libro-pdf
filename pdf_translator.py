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
# Каталог шукаємо: $LIBRO_FONT_DIR -> системний (Railway/Linux, як і раніше) ->
# локальний .fonts/dejavu у репозиторії (мак-розробка без brew/apt).
def _resolve_font_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.environ.get("LIBRO_FONT_DIR", ""),
              "/usr/share/fonts/truetype/dejavu",
              os.path.join(here, ".fonts", "dejavu")):
        if d and os.path.exists(os.path.join(d, "DejaVuSans.ttf")):
            return d
    return "/usr/share/fonts/truetype/dejavu"


_FONT_DIR = _resolve_font_dir()
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
    """Ознака битого тексту: цифра/дужка/кома, вклеєна ВСЕРЕДИНУ слова;
    слово, що починається з цифри ('1ГЛОР(23'); або дефіс упритул до іншого
    знака ',-' / '-.' (характерний глифовий мотлох стилізованих шрифтів)."""
    if not text:
        return False
    letters = sum(ch.isalpha() for ch in text)
    # 4+ цифр і майже без літер (<=1) — глифовий мотлох стилізованого заголовка
    # ('34567' / '34567 I' замість 'ЧАСТЬ' / 'ЯРОСТНОЕ НЛП'). Номери сторінок
    # (1-3 цифри) і короткі числа не чіпаємо. OCR-рятунок потім або прочитає
    # справжнє слово, або блок лишиться читабельним оригіналом.
    if sum(ch.isdigit() for ch in text) >= 4 and letters <= 1:
        return True
    if letters < 2:
        return False
    # дефіс упритул до коми/крапки/дужки (',-', '-.', '.-', ',)' ...) —
    # глифовий мотлох. У нормальному тексті такого нема: телефони '703-73',
    # слова 'як-то', роки '1990-х' мають дефіс між цифрами/літерами,
    # а не біля іншого розділового знака.
    if re.search(r"[\,\.\)\(]\-|\-[\,\.\)\(]", text):
        return True
    anomalies = len(re.findall(
        r"[А-Яа-яЇїІіЄєҐґA-Za-z][\)\(\,\d][А-Яа-яЇїІіЄєҐґA-Za-z]", text))
    anomalies += len(re.findall(r"\d[А-Яа-яЇїІіЄєҐґ]{2,}", text))  # '1ГЛОР'
    # короткий заголовок з дужкою+цифрою поряд із КИРИЛИЦЕЮ ('ЧА)9Ь 1');
    # латинські посилання '(Spoerl, 1975)' й телефони/адреси (багато цифр)
    # не чіпаємо
    if (len(text) <= 40 and re.search(r"[\)\(]", text) and re.search(r"\d", text)
            and re.search(r"[А-Яа-яЇїІіЄєҐґ]", text)
            and sum(ch.isdigit() for ch in text) <= 4):
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
            # якщо текст битий — рятуємо OCR-ом; не врятували -> позначимо garbled
            # (build_pdf не чіпатиме такий блок: лишить оригінал читабельним,
            #  а не вставить кашу типу 'я,-.тн-е 012')
            garbled = False
            if _looks_garbled(text):
                fixed = _ocr_region(page, b["bbox"], ocr_lang)
                if fixed and not _looks_garbled(fixed):
                    text = fixed
                else:
                    garbled = True
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
                "garbled": garbled,
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


class _Aborted(Exception):
    """Зовнішнє переривання задачі (cancel користувача або timeout/stall).
    Кидається з abort_cb між батчами — переклад припиняється, API більше не
    палиться. status: 'cancelled' | 'timeout'."""
    def __init__(self, status="cancelled", msg=""):
        self.status = status
        self.msg = msg
        super().__init__(msg or status)

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

_GLOSSARY_SYS = (
    "Ти — термінолог. Тобі дають фрагменти книги мовою {src}. Вибери до 40 "
    "КЛЮЧОВИХ повторюваних термінів, фахового жаргону та власних назв, що мають "
    "перекладатися ОДНАКОВО по всій книзі (НЕ загальновживані слова). Для кожного "
    "дай канонічний переклад на {dst}. Поверни ЛИШЕ JSON-обʼєкт виду "
    '{{"термін": "переклад"}} — без пояснень, без markdown, без ```.'
)


def build_glossary(texts, api_key, provider="gemini", model=None,
                   src="ru", dst="uk", sample_chars=8000):
    """Один LLM-прохід: витягує повторювані терміни/власні назви з рівномірної
    вибірки книги і дає канонічний переклад. Повертає {термін: переклад}.
    Будь-яка помилка -> порожній словник (переклад працює і без глосарію)."""
    try:
        model = model or _default_model(provider)
        joined, step, total = [], max(1, len(texts) // 120), 0
        for i in range(0, len(texts), step):
            t = (texts[i] or "").strip()
            if len(t) < 12:
                continue
            joined.append(t)
            total += len(t)
            if total >= sample_chars:
                break
        if not joined:
            return {}
        sys = _GLOSSARY_SYS.format(src=_LANG.get(src, src), dst=_LANG.get(dst, dst))
        raw = _llm_call(provider, api_key, model, sys, "\n".join(joined))
        m = re.search(r"\{.*\}", raw, re.S)
        gl = json.loads(m.group(0)) if m else {}
        if not isinstance(gl, dict):
            return {}
        out = {}
        for k, v in gl.items():
            k, v = str(k).strip(), str(v).strip()
            if not (1 < len(k) <= 60 and 0 < len(v) <= 80):
                continue
            if _looks_garbled(k) or _looks_garbled(v):   # мотлох у словник не пускаємо
                continue
            out[k] = v
        print(f"glossary: {len(out)} термінів")
        return dict(list(out.items())[:60])
    except Exception as e:
        print("build_glossary failed:", e)
        return {}


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
            # ключ ТІЛЬКИ в заголовку: в URL він потрапляє у тексти винятків
            # requests і далі в логи/статуси (правило 10)
            r = requests.post(
                url,
                headers={"Content-Type": "application/json",
                         "x-goog-api-key": api_key},
                json={
                    "systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": user}]}],
                    "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192,
                                         "thinkingConfig": {"thinkingBudget": 0}},
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
                     progress_cb=None, glossary=None, extra_sys="", abort_cb=None):
    """
    Перекладає список рядків. Батчить, перевіряє кількість сегментів,
    fallback по одному. Якщо proofread=True — другий прохід-редактор.
    glossary={термін:переклад} — для єдиної термінології по всій книзі.
    extra_sys — додаткові жорсткі правила в системний промпт (наприклад,
    для обкладинок). Прогрес рахується на обидві фази (переклад + редактор).
    abort_cb — викликається на початку кожного батчу; якщо кидає _Aborted
    (cancel/timeout), переклад негайно припиняється і API більше не палиться.
    """
    model = model or _default_model(provider)
    system = _SYS_PROMPT.format(src=_LANG.get(src, src), dst=_LANG.get(dst, dst))
    if glossary:
        gl = "; ".join(f"{k} → {v}" for k, v in glossary.items())
        system += ("\n9. ОБОВ'ЯЗКОВИЙ ГЛОСАРІЙ. Уживай САМЕ ці переклади термінів "
                   "усюди, де вони трапляються (відмінюй за контекстом): " + gl)
    if extra_sys:
        system += "\n" + extra_sys
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
        if abort_cb:
            abort_cb()                       # cancel/timeout -> _Aborted нагору
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
                               char_budget=char_budget, on_done=report,
                               glossary=glossary, abort_cb=abort_cb)
    return out


def proofread_blocks(originals, drafts, api_key, provider, model, src, dst,
                     char_budget=2500, on_done=None, glossary=None, abort_cb=None):
    """Другий прохід: редактор виправляє чернетку, звіряючи з оригіналом."""
    system = _EDITOR_PROMPT.format(src=_LANG.get(src, src), dst=_LANG.get(dst, dst))
    if glossary:
        gl = "; ".join(f"{k} → {v}" for k, v in glossary.items())
        system += ("\nДотримуйся глосарію (саме ці переклади термінів): " + gl)
    out = list(drafts)
    n = len(drafts)

    def pair(i):
        return f"ОРИГІНАЛ:\n{originals[i]}\n\nЧЕРНЕТКА:\n{drafts[i]}"

    batches = _make_batches(n, lambda i: len(originals[i]) + len(drafts[i]), char_budget)
    for batch in batches:
        if abort_cb:
            abort_cb()
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
def _place_text(page, bbox, text, fontname, fontfile, color, size,
                col_right=None, max_bottom=None):
    """Вставляє text у прямокутник. Тримає ОДИН розмір шрифту, переносить
    рядки В МЕЖАХ КОЛОНКИ і за потреби нарощує висоту вниз (але не нижче
    max_bottom — верху наступного блоку, щоб не налазити). Шрифт зменшує лише
    як крайній засіб. col_right — права межа колонки (щоб текст не ліз на
    сусідів і не вилазив за поле сторінки). Повертає True, якщо вмістив."""
    x0, y0, x1, y1 = bbox
    page_r = page.rect
    margin = 2.0
    left = max(x0, margin)
    # права межа: ширина оригінального блоку, але НІКОЛИ не за поле сторінки
    right = min(col_right if col_right else x1, page_r.x1 - margin)
    if right <= left + 20:                      # дуже вузький блок — дамо мінімум
        right = min(left + 130, page_r.x1 - margin)
    # низ: не нижче за верх наступного блоку (щоб не налазити), і не за сторінку
    bottom_cap = page_r.y1 - margin
    if max_bottom is not None:
        bottom_cap = min(bottom_cap, max(y1 + 2, max_bottom))
    min_sz = max(6.0, size * 0.7)               # не дрібнимо більш ніж на 30%
    # 1) тримаємо розмір, нарощуємо висоту вниз; 2) лише потім трохи зменшуємо
    sz = float(size)
    while sz >= min_sz:
        for extra in (0, 8, 18, 32, 52, 80, 120):
            bot = min(y1 + 2 + extra, bottom_cap)
            rect = fitz.Rect(left - 1, y0 - 1, right, bot)
            rc = page.insert_textbox(rect, text, fontsize=sz, fontname=fontname,
                                     fontfile=fontfile, color=color, align=0)
            if rc >= 0:
                return True
        sz -= 0.5
    # крайній випадок: у колонку до низу сторінки найменшим допустимим розміром
    page.insert_textbox(fitz.Rect(left - 1, y0 - 1, right, bottom_cap),
                        text, fontsize=min_sz, fontname=fontname,
                        fontfile=fontfile, color=color, align=0)
    return False


def _sample_bg(img, x0, y0, x1, y1, pad=14):
    """Надійний колір фону навколо боксу. Беремо широку рамку, КВАНТУЄМО
    кольори (крок 12), щоб шум текстури/градієнта злився в один тон, і
    повертаємо домінантний тон. Стійко до пергаменту/кремових/кольорових
    сторінок (не дає білих плям). rgb 0..255."""
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
            q = (col[0] // 12 * 12, col[1] // 12 * 12, col[2] // 12 * 12)
            cnt[q] = cnt.get(q, 0) + c
    if not cnt:
        return (255, 255, 255)
    best = max(cnt.items(), key=lambda kv: kv[1])[0]   # найчастіший тон рамки
    return (min(255, best[0] + 6), min(255, best[1] + 6), min(255, best[2] + 6))


# DejaVu через @font-face -> чітка кирилиця у потрібній гарнітурі (serif/sans,
# bold/italic). Кешуємо архів і CSS. Якщо шрифтів нема — (None, "") і build_pdf
# падає на вбудовані serif/sans-serif.
_FONT_ASSETS = "init"


def _font_assets():
    global _FONT_ASSETS
    if _FONT_ASSETS != "init":
        return _FONT_ASSETS
    try:
        import os
        d = _FONT_DIR
        files = [
            ("BookSerif", "normal", "normal", "DejaVuSerif.ttf"),
            ("BookSerif", "bold",   "normal", "DejaVuSerif-Bold.ttf"),
            ("BookSerif", "normal", "italic", "DejaVuSerif-Italic.ttf"),
            ("BookSerif", "bold",   "italic", "DejaVuSerif-BoldItalic.ttf"),
            ("BookSans",  "normal", "normal", "DejaVuSans.ttf"),
            ("BookSans",  "bold",   "normal", "DejaVuSans-Bold.ttf"),
            ("BookSans",  "normal", "italic", "DejaVuSans-Oblique.ttf"),
            ("BookSans",  "bold",   "italic", "DejaVuSans-BoldOblique.ttf"),
        ]
        css, found = [], False
        for fam, w, s, fn in files:
            if not os.path.exists(os.path.join(d, fn)):
                continue
            css.append("@font-face{font-family:'%s';src:url('%s');"
                       "font-weight:%s;font-style:%s;}" % (fam, fn, w, s))
            found = True
        _FONT_ASSETS = (fitz.Archive(d), "".join(css)) if found else (None, "")
    except Exception:
        _FONT_ASSETS = (None, "")
    return _FONT_ASSETS


def build_pdf(pdf_bytes: bytes, pages_blocks, translations: dict,
              keep_image_bg=True, recipe=None, inpaint_over_images=True):
    """
    pdf_bytes      — оригінальний PDF.
    pages_blocks   — результат extract_blocks().
    translations   — {block_id: "переклад"}.
    recipe         — правила від зору (напр. {"uniform_bg":true,"page_bg":[245,240,225]}).
    inpaint_over_images — для блоків ПОВЕРХ зображення стирати оригінал
                    інпейнтингом (маска літер + cv2.inpaint, як в обкладинках),
                    а не заливкою прямокутником; False -> стара заливка.
    Повертає bytes готового PDF.
    """
    import statistics
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    recipe = recipe or {}
    # глобальний колір фону від зору (якщо книга однотонна) — 0..1
    uni_bg = _rgb01(recipe["page_bg"]) if (recipe.get("uniform_bg") and recipe.get("page_bg")) else None

    for blocks in pages_blocks:
        if not blocks:
            continue
        page = doc[blocks[0]["page"]]
        page_r = page.rect

        # --- беремо лише блоки з НЕПОРОЖНІМ перекладом ---
        # (непереведені абзаци не чіпаємо: лишаємо чистий оригінал,
        #  без плашки і без подвійного тексту)
        live = [b for b in blocks
                if (translations.get(b["id"]) or "").strip()
                and not b.get("garbled")
                and not _looks_garbled(translations.get(b["id"]) or "")]
        if not live:
            continue

        # --- ОДИН рівний розмір основного тексту на сторінку ---
        # (мода розмірів блоків -> прибирає "стрибаючий шрифт")
        sizes = [max(1, round(b["size"])) for b in live]
        try:
            body = statistics.mode(sizes)
        except statistics.StatisticsError:
            body = round(statistics.median(sizes))
        body = max(6, min(int(body), 16))

        # --- блоки ПОВЕРХ зображення -> інпейнтинг замість заливки ---
        # (на фото/ілюстрації прямокутник фону = видима заплатка; правило 4 не
        #  зачіпається — биті блоки сюди не доходять, відсіялись у live)
        def _bbarea(bb):
            return max(0.0, bb[2] - bb[0]) * max(0.0, bb[3] - bb[1])
        img_boxes = []
        if inpaint_over_images:
            try:
                pa = page_r.width * page_r.height
                img_boxes = [im["bbox"] for im in page.get_image_info()
                             if _bbarea(im["bbox"]) > 0.05 * pa]
            except Exception:
                img_boxes = []

        def _over_image(bb):
            for ib in img_boxes:
                ox = min(bb[2], ib[2]) - max(bb[0], ib[0])
                oy = min(bb[3], ib[3]) - max(bb[1], ib[1])
                if ox > 0 and oy > 0 and ox * oy > 0.6 * max(_bbarea(bb), 1.0):
                    return True
            return False
        over = {b["id"]: _over_image(b["bbox"]) for b in live} if img_boxes else {}

        # --- рендеримо сторінку (раз) для підбору кольору фону ТА інпейнту ---
        # ЗАВЖДИ (не лише за наявності картинок): так кремові/пергаментні
        # сторінки заливаються своїм тоном, а не білим.
        pimg, zoom = None, 2.0
        if uni_bg is None or any(over.values()):
            try:
                from PIL import Image
                pm = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
                pimg = Image.open(io.BytesIO(pm.tobytes("png"))).convert("RGB")
            except Exception:
                pimg = None

        # інпейнт-чистка регіонів блоків поверх зображення (одна маска/inpaint)
        cleaned_pimg = None
        if pimg is not None and any(over.values()):
            regions = []
            for b in live:
                if over.get(b["id"]):
                    x0, y0, x1, y1 = b["bbox"]
                    cc = b["color"]
                    col = (int(cc[0] * 255), int(cc[1] * 255), int(cc[2] * 255))
                    regions.append((x0 * zoom, y0 * zoom, x1 * zoom, y1 * zoom, col))
            cleaned_pimg, _did = _inpaint_letters(pimg, regions)
            if not _did:
                cleaned_pimg = None       # нічого не стерто -> звичайна заливка

        # 3a. РЕДАКЦІЯ: фізично видаляємо оригінал ЛИШЕ під перекладеними блоками.
        #     Блоки поверх зображення -> без заливки (fill=False): потім кладемо
        #     інпейнт-патч. Решта -> заливка справжнім тоном фону.
        for b in live:
            r = fitz.Rect(b["bbox"])
            r.x0 -= 1.5; r.y0 -= 1.5; r.x1 += 1.5; r.y1 += 1.5
            r = r & page_r                      # обов'язково в межах сторінки!
            if r.is_empty:
                continue
            if cleaned_pimg is not None and over.get(b["id"]):
                page.add_redact_annot(r, fill=False)
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
        # текст видаляємо (default), зображення НЕ чіпаємо
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

        # інпейнт-патчі поверх зображення (натуральний фон замість прямокутника)
        if cleaned_pimg is not None:
            for b in live:
                if not over.get(b["id"]):
                    continue
                x0, y0, x1, y1 = b["bbox"]
                px = (max(0, int(x0 * zoom)), max(0, int(y0 * zoom)),
                      min(cleaned_pimg.width, int(x1 * zoom)),
                      min(cleaned_pimg.height, int(y1 * zoom)))
                if px[2] <= px[0] or px[3] <= px[1]:
                    continue
                buf = io.BytesIO()
                cleaned_pimg.crop(px).save(buf, "PNG")
                ir = fitz.Rect(x0, y0, x1, y1) & page_r
                if not ir.is_empty:
                    page.insert_image(ir, stream=buf.getvalue())

        # верх найближчого блоку нижче (по колонці) — щоб переклад не налазив униз
        def floor_for(bb):
            bx0, byt, bx1 = bb["bbox"][0], bb["bbox"][1], bb["bbox"][2]
            f = page_r.y1 - 2
            for o in live:
                if o is bb:
                    continue
                ox0, oy0, ox1, _ = o["bbox"]
                if oy0 > byt + 2 and ox1 > bx0 and ox0 < bx1:
                    f = min(f, oy0 - 2)
            return f

        # 3b. Вставляємо переклад через insert_htmlbox — ОФІЦІЙНИЙ метод PyMuPDF
        #     для цієї задачі: сам підбирає шрифт (зокрема для неперекладених
        #     шматків) і САМ зменшує текст, якщо не влазить. Розмір тримаємо
        #     рівним (body), заголовок — більший; рамку зажимаємо по ширині й до
        #     верху наступного блоку (щоб не вилазило за край і не налазило вниз).
        import html as _html
        _arch, _face_css = _font_assets()
        for b in live:
            tgt = translations.get(b["id"])
            is_head = (b["size"] >= body * 1.4 and len(tgt.strip()) <= 55)
            place_sz = min(b["size"], body * 2.2) if is_head else float(body)
            x0, y0, x1, y1 = b["bbox"]
            left = max(x0, 2.0)
            right = min(x1, page_r.x1 - 2.0)
            if right <= left + 20:
                right = min(left + 130, page_r.x1 - 2.0)
            bottom = floor_for(b)
            if bottom <= y0 + 8:                      # вироджений випадок: наступний
                bottom = min(y0 + max(y1 - y0, 12),    # блок майже впритул — дамо мінімум
                             page_r.y1 - 2.0)
            rect = fitz.Rect(left, y0 - 1, right, bottom)
            ff = b["fontfile"] or ""
            serif = "Serif" in ff
            fam = "'BookSerif',serif" if serif else "'BookSans',sans-serif"
            weight = "bold" if "Bold" in ff else "normal"
            style = "italic" if ("Italic" in ff or "Oblique" in ff) else "normal"
            r, g, bl = b["color"]
            hexc = "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(bl * 255))
            css = (_face_css +
                   "*{margin:0;padding:0;line-height:1.15;font-family:%s;color:%s;}"
                   % (fam, hexc))
            htmltxt = ('<div style="font-size:%.1fpx;font-weight:%s;font-style:%s">%s</div>'
                       % (place_sz, weight, style, _html.escape(tgt)))
            try:
                if _arch is not None:
                    page.insert_htmlbox(rect, htmltxt, css=css, archive=_arch)
                else:
                    page.insert_htmlbox(rect, htmltxt, css=css)
            except Exception:
                _place_text(page, b["bbox"], tgt, b["fontname"], b["fontfile"],
                            b["color"], place_sz, col_right=b["bbox"][2],
                            max_bottom=bottom)

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
                          src="ru", dst="uk", proofread=False, progress_cb=None,
                          abort_cb=None):
    """Скан-книга: OCR кожної сторінки -> переклад -> накладання поверх скану."""
    lang = _LANG_OCR.get(src, "rus")
    src_doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    # --- 1. OCR усіх сторінок (тримаємо лише текст+координати, не картинки) ---
    npages = max(src_doc.page_count, 1)
    page_lines, texts, index = [], [], []
    for pno in range(src_doc.page_count):
        if abort_cb:
            abort_cb()                        # cancel/timeout під час OCR-фази
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
                                  progress_cb=_shim, abort_cb=abort_cb)
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
            # рамка для перекладу: дозволяємо трохи ширше за оригінальний рядок,
            # але НІКОЛИ не за праве поле сторінки (інакше текст вилазить за край)
            page_w = newp.rect.width
            right = min(x1 + (x1 - x0) * 0.8, page_w - 2)
            grow = fitz.Rect(x0, y0, right, y1 + (y1 - y0) * 1.3)
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
    """OCR обкладинки -> (рядки назви, рядки автора) з координатами (в пунктах).
    Якщо OCR недоступний/впав — повертаємо порожньо (make_cover піде в режим
    генерації за назвою з титульної сторінки)."""
    try:
        lines = [l for l in _ocr_lines(doc[0], ocr_lang) if 1 < len(l["text"]) < 60]
    except Exception:
        return [], []
    if not lines:
        return [], []
    for l in lines:
        l["h"] = l["bbox"][3] - l["bbox"][1]
    maxh = max(l["h"] for l in lines)
    title = sorted([l for l in lines if l["h"] >= 0.7 * maxh], key=lambda l: l["bbox"][1])
    author = sorted([l for l in lines if l["h"] < 0.7 * maxh], key=lambda l: -l["h"])[:1]
    return title, author


def make_cover(pdf_bytes, api_key, provider="gemini", model=None,
               src="ru", dst="uk", title=None, author=None, recipe=None,
               glossary=None, allow_generate=True):
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
                          provider=provider, model=model, src=src, dst=dst,
                          glossary=glossary)
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

    # Не вдалося замінити текст НА МІСЦІ (фон не однотонний або OCR не знайшов
    # назву). У робочому потоці генерувати нову обкладинку НЕ хочемо
    # (краще лишити оригінал, ніж зробити уродця) -> повертаємо None.
    if not allow_generate:
        return None

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


def _page_images_b64(pdf_bytes, n=5, width=560):
    """Перші n сторінок як невеликі JPEG (щоб не перевищити ліміт розміру запиту)."""
    import base64
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    out = []
    for pno in range(min(n, doc.page_count)):
        pg = doc[pno]
        zoom = width / max(pg.rect.width, 1)
        pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
            buf = io.BytesIO(); img.save(buf, "JPEG", quality=70)
            data = buf.getvalue()
        except Exception:
            data = pix.tobytes("jpeg")
        out.append(base64.b64encode(data).decode())
    doc.close()
    return out


def _vision_call(provider, api_key, model, prompt, images_b64, timeout=120,
                 mime="image/jpeg", gen_config=None):
    if provider == "gemini":
        # thinkingBudget:0 обов'язковий для gemini-2.5-flash (правило 9):
        # без нього модель «думає» і обрізає відповідь.
        cfg = gen_config or {"temperature": 0.1, "maxOutputTokens": 1024,
                             "thinkingConfig": {"thinkingBudget": 0}}
        parts = [{"text": prompt}] + [{"inline_data": {"mime_type": mime, "data": b}}
                                      for b in images_b64]
        # ключ ТІЛЬКИ в заголовку (не в URL) — див. коментар у _gemini_call
        r = requests.post(
            f"{_GEMINI_BASE}/{model}:generateContent",
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            json={"contents": [{"role": "user", "parts": parts}],
                  "generationConfig": cfg},
            timeout=timeout)
        if r.status_code >= 400:
            raise _ClientError(f"Gemini vision {r.status_code}: {r.text[:300]}")
        cands = r.json().get("candidates", [])
        return "".join(p.get("text", "") for p in cands[0]["content"]["parts"]) if cands else ""
    # groq / openai-сумісний
    content = [{"type": "text", "text": prompt}] + [
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b}"}}
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


def analyze_book(pdf_bytes, api_key, provider="groq", model=None, n=5):
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


# ================================================================
#                    ОБКЛАДИНКА: helpers
# ================================================================
def _guess_title(pages):
    """Назва книги з ТЕКСТОВОЇ титульної сторінки (перша непорожня сторінка),
    щоб не залежати від ненадійного OCR стилізованої обкладинки.
    Назва = найбільші за кеглем блоки цієї сторінки (зверху вниз)."""
    for blk in pages[:5]:
        if not blk:
            continue                       # обкладинка-картинка -> пропускаємо
        mx = max(b["size"] for b in blk)
        big = [b for b in blk if b["size"] >= mx * 0.5 and 1 < len(b["text"]) < 70]
        if big:
            big.sort(key=lambda b: b["bbox"][1])
            return (" ".join(b["text"] for b in big)[:120]) or None
        return None
    return None


def replace_first_page_image(pdf_bytes, png_bytes):
    """Замінює ПЕРШУ сторінку PDF готовою картинкою обкладинки (на всю сторінку).
    Якщо щось не так — повертає None: виклика́ч лишає вихідний PDF і чесно
    виставляє cover_status (не «replaced», коли заміни не було)."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        if doc.page_count == 0:
            doc.close()
            return None
        rect = doc[0].rect
        doc.delete_page(0)
        page = doc.new_page(pno=0, width=rect.width, height=rect.height)
        page.insert_image(rect, stream=png_bytes)
        out = doc.tobytes(deflate=True, garbage=4)
        doc.close()
        return out
    except Exception:
        return None


# ================================================================
#     ОБКЛАДИНКА ЧЕРЕЗ ЗІР: читання -> переклад -> перемальовка
# ================================================================
# Контракт: будь-яка помилка на будь-якому кроці -> виняток НАГОРУ.
# Виклика́ч (main.py) ловить його і лишає оригінальну обкладинку
# (правило 6: результат гірший за оригінал недопустимий).

_COVER_VISION_PROMPT = (
    "You read the text printed on a book cover image.\n"
    "Return STRICTLY a JSON array, nothing else — no prose, no markdown fences:\n"
    '[{"text": str, "bbox_pct": [x, y, w, h], "color": "#RRGGBB", '
    '"role": "title"|"subtitle"|"author"|"other"}]\n'
    "Rules:\n"
    "- One element per LOGICAL text block (title, subtitle, author, series, "
    "publisher tagline...). Reading order: top to bottom.\n"
    "- If one logical text spans several lines (e.g. a multi-line title), it is "
    "ONE element: join the lines with spaces, bbox covers ALL its lines.\n"
    "- bbox_pct = [left, top, width, height] as PERCENTAGES (0-100) of the image "
    "dimensions, covering the FULL painted text including every letter edge.\n"
    "- color = colour of the LETTERS themselves (not the background), hex #RRGGBB.\n"
    "- Do NOT include page numbers, barcodes, logos without words, or decorative "
    "single letters.\n"
    "- If the cover has no readable text, return []."
)


def render_cover_png(pdf_bytes, scale=2.0):
    """ПЕРША сторінка PDF як PNG (для translate_cover_vision)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        if doc.page_count == 0:
            raise ValueError("порожній PDF")
        pix = doc[0].get_pixmap(matrix=fitz.Matrix(scale, scale))
        return pix.tobytes("png")
    finally:
        doc.close()


def _hex_to_rgb(h):
    h = str(h or "").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        raise ValueError(f"зір повернув поганий колір: {h!r}")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _frame_mean(img, x0, y0, x1, y1, fr=5):
    """Середній колір рамки завширшки fr px НАВКОЛО боксу (заливка під текст)."""
    from PIL import ImageStat
    W, H = img.size
    bands = [(x0 - fr, y0 - fr, x1 + fr, y0), (x0 - fr, y1, x1 + fr, y1 + fr),
             (x0 - fr, y0, x0, y1), (x1, y0, x1 + fr, y1)]
    tot, n = [0.0, 0.0, 0.0], 0
    for bx0, by0, bx1, by1 in bands:
        bx0, by0 = max(0, int(bx0)), max(0, int(by0))
        bx1, by1 = min(W, int(bx1)), min(H, int(by1))
        if bx1 <= bx0 or by1 <= by0:
            continue
        st = ImageStat.Stat(img.crop((bx0, by0, bx1, by1)).convert("RGB"))
        cnt = (bx1 - bx0) * (by1 - by0)
        for i in range(3):
            tot[i] += st.mean[i] * cnt
        n += cnt
    if not n:
        raise ValueError("bbox поза межами картинки")
    return tuple(int(t / n) for t in tot)


def _refine_bbox(img, x0, y0, x1, y1, text_rgb):
    """Зір дає РАМКИ ПРИБЛИЗНО (часто вужчі за текст -> лишаються «хвости»
    оригінальних літер). Уточнюємо рамку по самій картинці: в околиці шукаємо
    пікселі кольору літер (зір віддає і його) і беремо їх щільний bbox.
    Сумнівний результат -> повертаємо рамку зору як була."""
    from PIL import Image, ImageChops
    W, H = img.size
    bw, bh = x1 - x0, y1 - y0
    mx, my = max(6.0, bw * 0.50), max(6.0, bh * 0.35)
    sx0, sy0 = max(0, int(x0 - mx)), max(0, int(y0 - my))
    sx1, sy1 = min(W, int(x1 + mx)), min(H, int(y1 + my))
    if sx1 - sx0 < 4 or sy1 - sy0 < 4:
        return x0, y0, x1, y1
    crop = img.crop((sx0, sy0, sx1, sy1)).convert("RGB")
    diff = ImageChops.difference(crop, Image.new("RGB", crop.size, tuple(text_rgb)))
    mask = diff.convert("L").point(lambda v: 255 if v <= 48 else 0)
    mb = mask.getbbox()
    if not mb:
        return x0, y0, x1, y1
    npx = sum(1 for v in mask.getdata() if v)
    rx0, ry0, rx1, ry1 = sx0 + mb[0], sy0 + mb[1], sx0 + mb[2], sy0 + mb[3]
    # здоровий глузд: знайдене мусить перетинати рамку зору, бути не точковим
    # шумом і не роздуватися безмежно (літери кольору фону тощо)
    if (npx < 25
            or rx1 <= x0 or rx0 >= x1 or ry1 <= y0 or ry0 >= y1
            or (rx1 - rx0) * (ry1 - ry0) > 4.0 * max(bw * bh, 1.0)):
        return x0, y0, x1, y1
    return float(rx0), float(ry0), float(rx1), float(ry1)


def _wrap_by_width(font, text, max_w):
    """Жадібне перенесення слів за РЕАЛЬНОЮ шириною у пікселях."""
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if not cur or font.getlength(t) <= max_w:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _fit_cover_font(text, box_w, box_h, fontfile, min_size=12):
    """Підбирає кегль, щоб текст уліз у бокс за шириною (і висотою з невеликим
    допуском). Повертає (font, lines, size). На мінімумі повертає як є —
    краще трохи тісно, ніж зовсім без тексту."""
    from PIL import ImageFont
    size = max(min_size, int(box_h))
    while size >= min_size:
        f = ImageFont.truetype(fontfile, size)
        lines = _wrap_by_width(f, text, box_w)
        if (all(f.getlength(l) <= box_w for l in lines)
                and len(lines) * size * 1.15 <= box_h * 1.25):
            return f, lines, size
        size -= 2
    f = ImageFont.truetype(fontfile, min_size)
    return f, _wrap_by_width(f, text, box_w), min_size


def _color_mask(region, rgb, thr=48):
    """Маска пікселів кольору літер (зважена відстань, як у _refine_bbox).
    region — numpy uint8 HxWx3, повертає bool HxW."""
    import numpy as np
    d = region.astype(np.int16) - np.array(rgb, dtype=np.int16)
    dist = (0.299 * np.abs(d[..., 0]) + 0.587 * np.abs(d[..., 1])
            + 0.114 * np.abs(d[..., 2]))
    return dist <= thr


def _contrast_mask(region):
    """Фолбек-маска літер за КОНТРАСТОМ (Otsu по яскравості): коли колір від
    зору не зійшовся з картинкою. Літери = менша за площею сторона порога
    (текст майже ніколи не займає більшість боксу)."""
    import cv2
    gray = cv2.cvtColor(region, cv2.COLOR_RGB2GRAY)
    _t, bin_ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    m = bin_ > 0
    return m if float(m.mean()) <= 0.5 else ~m


def _inpaint_letters(page_pil, regions, max_share=0.5):
    """Стирає літери текстових регіонів з РАСТРОВОЇ сторінки через cv2.inpaint
    (той самий механізм, що в обкладинках) — для тексту ПОВЕРХ зображення,
    замість заливки прямокутником. regions: [(x0,y0,x1,y1,(r,g,b))] у пікселях
    page_pil (колір літер 0..255). Маска: колір (поріг 48 -> 90) -> контраст-
    фолбек. Повертає (PIL RGB, True), якщо щось стерто; інакше (вихідний, False).
    Будь-яка помилка cv2 / порожня / завелика (> max_share) маска -> (вихідний,
    False): виклика́ч лишається на звичайній заливці фоном."""
    try:
        import numpy as np
        import cv2
        from PIL import Image
        arr = np.asarray(page_pil.convert("RGB"), dtype=np.uint8)
        H, W = arr.shape[:2]
        mask = np.zeros((H, W), np.uint8)
        pad = 3
        touched = False
        for x0, y0, x1, y1, color in regions:
            ix0, iy0 = max(0, int(x0 - pad)), max(0, int(y0 - pad))
            ix1, iy1 = min(W, int(x1 + pad) + 1), min(H, int(y1 + pad) + 1)
            if ix1 <= ix0 or iy1 <= iy0:
                continue
            region = arr[iy0:iy1, ix0:ix1]
            sub = _color_mask(region, color)
            if float(sub.mean()) < 0.01:
                sub = _color_mask(region, color, thr=90)
            if float(sub.mean()) < 0.01:
                sub = _contrast_mask(region)
            mask[iy0:iy1, ix0:ix1][sub] = 255
            touched = True
        if not touched or float((mask > 0).mean()) > max_share:
            return page_pil, False
        kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        mask = cv2.dilate(mask, kern, iterations=1)
        clean = cv2.inpaint(np.ascontiguousarray(arr), mask, 4, cv2.INPAINT_TELEA)
        return Image.fromarray(clean), True
    except Exception as e:
        print("inpaint letters failed:", e)
        return page_pil, False


def _mask_lines(m):
    """Рядки тексту з маски літер: горизонтальна проєкція -> відрізки
    [(top, bottom)]. Розриви <=2 px зливаємо, шум <3 px викидаємо."""
    runs, start = [], None
    rows = list(m.any(axis=1)) + [False]
    for i, v in enumerate(rows):
        if v and start is None:
            start = i
        elif not v and start is not None:
            runs.append([start, i])
            start = None
    merged = []
    for a, b in runs:
        if merged and a - merged[-1][1] <= 2:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged if b - a >= 3]


def _layout_cover_text(text, box_w, box_h, fontfile, line_h, pitch_ratio,
                       min_size=12):
    """Багаторядкова розкладка перекладу в рамці так, щоб ЧОРНИЛЬНА висота
    рядка відповідала оригіналу: line_h з маски — це cap-height (~0.73 em у
    DejaVu), тому стартовий кегль = line_h / cap_ratio; shrink-to-fit нижче
    однаково захищає від вилазіння за рамку. Міжрядковий інтервал — пропорція
    оригіналу (pitch_ratio = крок/висота рядка).
    Повертає (font, lines, size, pitch_px, fitted).
    fitted=False -> навіть мінімальний кегль не вліз (сигнал doubtful)."""
    from PIL import ImageFont
    cb = ImageFont.truetype(fontfile, 100).getbbox("Н")
    cap_ratio = max(0.5, (cb[3] - cb[1]) / 100.0)
    size = max(min_size, int(line_h / cap_ratio))
    while size >= min_size:
        f = ImageFont.truetype(fontfile, size)
        lines = _wrap_by_width(f, text, box_w)
        block_h = size + size * pitch_ratio * (len(lines) - 1)
        if (all(f.getlength(l) <= box_w for l in lines)
                and block_h <= box_h * 1.3):
            return f, lines, size, size * pitch_ratio, True
        size -= 2
        if size < min_size and size + 2 != min_size:
            size = min_size      # тестуємо min_size явно, паритет не перескакує
    f = ImageFont.truetype(fontfile, min_size)
    return (f, _wrap_by_width(f, text, box_w), min_size,
            min_size * pitch_ratio, False)


def _parse_cover_json(raw, img_size=None):
    """Строгий розбір відповіді зору: чистимо ```-огорожі, шукаємо JSON-масив,
    валідуємо кожен елемент. Сміття -> ValueError (нагору).
    img_size = (w, h) картинки, яку бачив зір: якщо будь-яке значення bbox
    > 100 — зір віддав ПІКСЕЛІ замість відсотків (траплялося на реальних
    обкладинках), перераховуємо у відсотки і далі звичайна валідація."""
    s = (raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    m = re.search(r"\[.*\]", s, re.S)
    if not m:
        raise ValueError(f"зір не повернув JSON-масив: {s[:200]!r}")
    items = json.loads(m.group(0))
    if not isinstance(items, list):
        raise ValueError("зір повернув не масив")
    blocks = []
    for it in items:
        if not isinstance(it, dict):
            raise ValueError(f"елемент не обʼєкт: {it!r}")
        text = str(it.get("text", "")).strip()
        if len(text) < 2:                      # лого-літери/сміття пропускаємо
            continue
        bb = it.get("bbox_pct")
        if (not isinstance(bb, (list, tuple)) or len(bb) != 4
                or not all(isinstance(v, (int, float)) for v in bb)):
            raise ValueError(f"поганий bbox_pct: {bb!r}")
        x, y, w, h = (float(v) for v in bb)
        if img_size and any(v > 100 for v in (x, y, w, h)):
            iw, ih = img_size
            x, y = x / iw * 100.0, y / ih * 100.0
            w, h = w / iw * 100.0, h / ih * 100.0
        if not (0 <= x < 100 and 0 <= y < 100 and w > 0 and h > 0):
            raise ValueError(f"bbox_pct поза межами: {bb!r}")
        w = min(w, 100 - x)
        h = min(h, 100 - y)
        role = str(it.get("role", "other")).lower()
        if role not in ("title", "subtitle", "author", "other"):
            role = "other"
        blocks.append({"text": text, "bbox_pct": (x, y, w, h),
                       "color": _hex_to_rgb(it.get("color", "#000000")),
                       "role": role})
    if not blocks:
        raise ValueError("зір не знайшов тексту на обкладинці")
    return blocks


# Обкладинки: жорстке правило перекладу абревіатур («НЛП» -> «НАП» більше не
# повториться) — і в промпт, і identity-пінами в глосарій (подвійний захист).
_ABBR_RULE = (
    "ДОДАТКОВЕ ЖОРСТКЕ ПРАВИЛО ДЛЯ ОБКЛАДИНКИ: абревіатури та акроніми "
    "(послідовності ВЕЛИКИХ літер: НЛП, КПТ, ДНК, NLP, CBT) копіюй "
    "ПОСИМВОЛЬНО, без перекладу, без транслітерації і без заміни літер: "
    "«НЛП» лишається «НЛП».")


def _abbr_tokens(texts):
    """Кандидати-абревіатури з текстів обкладинки: латинські 2-5 ВЕЛИКИХ
    літер або кириличні 2-5 ВЕЛИКИХ без голосних (НЛП, КПТ, ДНК; слова типу
    КЛЮЧИ/МАГИЯ мають голосні й не чіпаються)."""
    out = set()
    for t in texts:
        for tok in re.findall(r"\b[А-ЯЁЇІЄҐA-Z]{2,5}\b", t or ""):
            if (re.fullmatch(r"[A-Z]{2,5}", tok)
                    or not re.search(r"[АЕЁИОУЫЭЮЯЇІЄ]", tok)):
                out.add(tok)
    return out


# ---- джерело істини: текстовий шар внутрішнього титулу + глосарій -----------
# Зір помилково ЧИТАЄ стилізований логотип («НЛП» бачить як «НАП»). Але
# всередині книги є чистий текстовий шар (титульна сторінка), де ті самі назва
# й автори стоять правильно. Звіряємо прочитане зором із цим джерелом.
def _levenshtein(a, b):
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if not la or not lb:
        return la or lb
    prev = list(range(lb + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[lb]


def _first_pages_blocks(pdf_bytes, max_pages=6):
    """Лёгке читання текстових блоків перших сторінок БЕЗ OCR (для джерела
    істини обкладинки): [{text, size}] по сторінці. Швидко навіть на 300+ стор."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    out = []
    for pno in range(min(max_pages, doc.page_count)):
        blocks = []
        for b in doc[pno].get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            txt = re.sub(r"\s+", " ",
                         " ".join(s["text"] for ln in b["lines"]
                                  for s in ln["spans"])).strip()
            if not txt:
                continue
            size = max((s["size"] for ln in b["lines"] for s in ln["spans"]),
                       default=0)
            blocks.append({"text": txt, "size": size})
        out.append(blocks)
    doc.close()
    return out


def _cover_reference(pdf_bytes, api_key, glossary, src, dst, model):
    """Назва й автори з текстового шару внутрішнього титулу + їх ПРАВИЛЬНИЙ
    переклад тим самим движком/глосарієм. Повертає (wordmap, strings):
    wordmap = {слово_lower: канонічна_форма_оригіналу}, strings = [{orig, uk}].
    Будь-яка помилка -> порожньо (корекція просто не застосується)."""
    wordmap, strings = {}, []
    try:
        pages = _first_pages_blocks(pdf_bytes)
    except Exception as e:
        print("cover reference: extract failed:", e)
        return wordmap, strings
    cand = []
    for blk in pages:
        if not blk:
            continue                       # обкладинка-картинка -> наступна
        for b in blk:                      # перший НЕПОРОЖНІЙ титул
            t = re.sub(r"\s+", " ", b["text"]).strip()
            if 2 <= len(t) <= 70 and not _looks_garbled(t):
                cand.append(t)
        break
    seen, uniq = set(), []
    for c in cand:
        if c.lower() not in seen:
            seen.add(c.lower()); uniq.append(c)
    uniq = uniq[:12]
    if not uniq:
        return wordmap, strings
    try:
        tr = translate_blocks(uniq, api_key, provider="gemini", model=model,
                              src=src, dst=dst, glossary=glossary,
                              extra_sys=_ABBR_RULE)
    except Exception as e:
        print("cover reference: translate failed:", e)
        tr = list(uniq)
    for i, c in enumerate(uniq):
        uk = ((tr[i] if i < len(tr) else c) or c).strip()
        strings.append({"orig": c, "uk": uk})
        for w in re.findall(r"\w+", c, re.U):
            if len(w) >= 2:
                wordmap.setdefault(w.lower(), w)
    return wordmap, strings


def _correct_text_by_words(text, wordmap):
    """Пословно править розпізнаний ОРИГІНАЛ за відомими словами титулу
    (Левенштейн<=2, із захистом від хибних замін на коротких словах).
    Регістр переносимо з токена (ВЕРХНІЙ лишається ВЕРХНІМ)."""
    if not wordmap:
        return text, False
    parts = re.split(r"(\W+)", text, flags=re.U)
    changed = False
    for k, tok in enumerate(parts):
        if not tok or not tok.strip() or len(tok) < 2:
            continue
        low = tok.lower()
        known = wordmap.get(low)
        if known is None:
            best, bestd = None, 99
            for lw, kw in wordmap.items():
                if abs(len(lw) - len(low)) > 2:
                    continue
                d = _levenshtein(low, lw)
                if d < bestd:
                    bestd, best = d, kw
            # поріг: dist 1 для слів >=2 літер; dist 2 лише для слів >=4 літер
            if best is not None and ((bestd == 1 and len(low) >= 2)
                                     or (bestd == 2 and len(low) >= 4)):
                known = best
        if known and known.lower() != low:
            parts[k] = known.upper() if tok.isupper() else known
            changed = True
    return "".join(parts), changed


def _override_uk_by_truth(text, strings):
    """Якщо блок майже збігається з відомим рядком титулу — повертаємо його
    КАНОНІЧНИЙ переклад (із чистого текстового шару), інакше None."""
    norm = re.sub(r"\s+", " ", text.strip()).lower()
    if len(norm) < 3:
        return None
    for s in strings:
        so = re.sub(r"\s+", " ", s["orig"].strip()).lower()
        if not so:
            continue
        if norm == so or (len(norm) >= 6 and _levenshtein(norm, so) <= 2):
            return s["uk"]
    return None


def read_cover_blocks(cover_png_bytes, api_key, glossary=None,
                      src="ru", dst="uk", model=None, pdf_bytes=None):
    """Зір (Gemini) читає обкладинку + переклад блоків (з глосарієм книги,
    якщо є). Повертає (blocks, reasons): blocks = [{text, uk, bbox_pct,
    color, role}], reasons — сигнали якості (наприклад, битий JSON з першої
    спроби). pdf_bytes (опц.) — звіряємо прочитане зором із текстовим шаром
    внутрішнього титулу: помилки розпізнавання стилізованих логотипів
    («НЛП»→«НАП») виправляються на канонічну форму. Будь-яка фатальна
    помилка -> виняток нагору."""
    import base64
    from PIL import Image

    model = model or "gemini-2.5-flash"
    img = Image.open(io.BytesIO(cover_png_bytes)).convert("RGB")
    W, H = img.size
    if W < 10 or H < 10:
        raise ValueError(f"замала картинка обкладинки: {W}x{H}")

    # зір: компактний JPEG (<=1200px), bbox у % працює на будь-якому масштабі
    vimg = img
    if max(W, H) > 1200:
        k = 1200 / max(W, H)
        vimg = img.resize((max(1, int(W * k)), max(1, int(H * k))))
    buf = io.BytesIO()
    vimg.save(buf, "JPEG", quality=88)
    img_b64 = [base64.b64encode(buf.getvalue()).decode()]
    cfg = {"temperature": 0.1, "maxOutputTokens": 4096,
           "thinkingConfig": {"thinkingBudget": 0}}
    reasons = []
    # модель зрідка віддає синтаксично битий JSON — одна повторна спроба з
    # нагадуванням; друга невдача -> виняток нагору (ланцюжок піде у фолбек)
    try:
        blocks = _parse_cover_json(_vision_call(
            "gemini", api_key, model, _COVER_VISION_PROMPT, img_b64,
            gen_config=cfg), img_size=vimg.size)
    except ValueError:
        reasons.append("зір повернув битий JSON (вдалося з 2-ї спроби)")
        blocks = _parse_cover_json(_vision_call(
            "gemini", api_key, model,
            _COVER_VISION_PROMPT + "\nYour previous output was NOT valid JSON. "
            "Return ONLY the syntactically valid JSON array.",
            img_b64, gen_config=cfg), img_size=vimg.size)

    # звірка з джерелом істини (текстовий шар титулу): виправляємо помилки
    # ЧИТАННЯ зором ДО перекладу, щоб правило абревіатур копіювало вже вірне
    wordmap, strings = ({}, [])
    if pdf_bytes is not None:
        wordmap, strings = _cover_reference(pdf_bytes, api_key, glossary,
                                            src, dst, model)
        for b in blocks:
            fixed, changed = _correct_text_by_words(b["text"], wordmap)
            if changed:
                print(f"cover: звірено з титулом {b['text']!r} -> {fixed!r}")
                b["text"] = fixed

    texts = [b["text"] for b in blocks]
    gl = dict(glossary or {})
    for tok in sorted(_abbr_tokens(texts)):
        gl.setdefault(tok, tok)               # identity-пін: НЛП -> НЛП
    tr = translate_blocks(texts, api_key, provider="gemini", model=model,
                          src=src, dst=dst, glossary=gl or None,
                          extra_sys=_ABBR_RULE)
    for b, t in zip(blocks, tr):
        b["uk"] = (t or "").strip() or b["text"]
    # відомий переклад із чистого титулу — пріоритетніший за переклад
    # (можливо спотвореного) тексту обкладинки
    if strings:
        for b in blocks:
            ov = _override_uk_by_truth(b["text"], strings)
            if ov:
                b["uk"] = ov
    return blocks, reasons


def translate_cover_vision(cover_png_bytes, api_key, glossary=None,
                           src="ru", dst="uk", model=None, pdf_bytes=None):
    """Зір читає обкладинку -> переклад -> ПРИБИРАННЯ оригінальних літер через
    inpainting (cv2.INPAINT_TELEA: фон ВІДНОВЛЮЄТЬСЯ, а не замальовується
    прямокутником) -> поверх переклад DejaVu (bold для title) кольором
    оригіналу; багаторядкові заголовки рендеряться багаторядково з міжрядковим
    інтервалом оригіналу.
    Повертає {"png": bytes, "status": "ok"|"doubtful", "reasons": [...]} —
    сигнал для кнопки на фронті, рішення завжди за людиною.
    Будь-яка фатальна помилка -> виняток нагору (виклика́ч ставить
    cover_status="failed" і лишає оригінал — правило 6)."""
    import numpy as np
    import cv2
    from PIL import Image, ImageDraw

    blocks, reasons = read_cover_blocks(cover_png_bytes, api_key,
                                        glossary=glossary, src=src, dst=dst,
                                        model=model, pdf_bytes=pdf_bytes)
    img = Image.open(io.BytesIO(cover_png_bytes)).convert("RGB")
    W, H = img.size
    arr = np.asarray(img, dtype=np.uint8)

    # --- 1. маска літер: усередині кожного bbox пікселі кольору тексту
    mask = np.zeros((H, W), dtype=np.uint8)
    boxes = []
    for b in blocks:
        x, y, w, h = b["bbox_pct"]
        x0, y0 = W * x / 100.0, H * y / 100.0
        x1, y1 = x0 + W * w / 100.0, y0 + H * h / 100.0
        x0, y0, x1, y1 = _refine_bbox(img, x0, y0, x1, y1, b["color"])
        pad = max(3.0, (y1 - y0) * 0.10)         # запас: антиаліасинг країв
        ix0, iy0 = max(0, int(x0 - pad)), max(0, int(y0 - pad))
        ix1, iy1 = min(W, int(x1 + pad) + 1), min(H, int(y1 + pad) + 1)
        if ix1 <= ix0 or iy1 <= iy0:
            raise ValueError(f"порожня рамка тексту: {b['text'][:30]!r}")
        region = arr[iy0:iy1, ix0:ix1]
        sub = _color_mask(region, b["color"])
        if float(sub.mean()) < 0.005:            # ширший допуск за відстанню
            sub = _color_mask(region, b["color"], thr=90)
        if float(sub.mean()) < 0.005:            # колір зовсім не зійшовся ->
            sub = _contrast_mask(region)         # фолбек: поріг за яскравістю
            reasons.append(f"колір зору не зійшовся, маска за контрастом: "
                           f"{b['text'][:24]!r}")
        runs = _mask_lines(sub)
        if float(sub.mean()) < 0.005 or not runs:
            # і контраст не дав літер: затирати наосліп і малювати кеглем
            # «на всю рамку» = знищити обкладинку. Виняток нагору ->
            # ланцюжок піде в OCR-на-місці / оригінал (правило 6).
            raise ValueError(f"маска літер порожня (ні колір, ні контраст "
                             f"не дали літер): {b['text'][:30]!r}")
        mview = mask[iy0:iy1, ix0:ix1]
        mview[sub] = 255
        heights = sorted(bb - aa for aa, bb in runs)
        line_h = heights[len(heights) // 2]
        if len(runs) > 1:
            pitch = (runs[-1][0] - runs[0][0]) / (len(runs) - 1)
            pitch_ratio = min(2.0, max(1.05, pitch / max(line_h, 1)))
        else:
            pitch_ratio = 1.15
        boxes.append((b, x0, y0, x1, y1, line_h, pitch_ratio))

    # --- 2. сигнал: уточнені рамки не мають перетинатися
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            _, ax0, ay0, ax1, ay1, _, _ = boxes[i]
            _, bx0, by0, bx1, by1, _, _ = boxes[j]
            ow = min(ax1, bx1) - max(ax0, bx0)
            oh = min(ay1, by1) - max(ay0, by0)
            if ow > 0 and oh > 0:
                amin = min((ax1 - ax0) * (ay1 - ay0),
                           (bx1 - bx0) * (by1 - by0))
                if ow * oh > 0.10 * max(amin, 1.0):
                    reasons.append(
                        f"рамки перетинаються: {boxes[i][0]['text'][:18]!r} "
                        f"і {boxes[j][0]['text'][:18]!r}")

    # --- 3. inpainting: розширюємо маску на 2-3 px і відновлюємо фон
    kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.dilate(mask, kern, iterations=1)
    mshare = float((mask > 0).mean())
    if mshare > 0.35:
        # inpaint на третині обкладинки — це вже не «прибрати літери», а
        # знищити арт. Виняток нагору -> OCR-на-місці / оригінал (правило 6).
        raise ValueError(f"маска inpaint {mshare:.0%} площі обкладинки (>35%)")
    # inpaint не інтерпретує порядок каналів — годуємо RGB і отримуємо RGB
    clean = cv2.inpaint(np.ascontiguousarray(arr), mask, 4, cv2.INPAINT_TELEA)
    work = Image.fromarray(clean)

    # --- 4. переклад поверх: кегль і міжрядковий інтервал від оригіналу
    draw = ImageDraw.Draw(work)
    for b, x0, y0, x1, y1, line_h, pitch_ratio in boxes:
        bold = b["role"] == "title"
        fontfile = _FONTS[("sans", bold, False)]
        if not os.path.exists(fontfile):
            raise RuntimeError(f"немає шрифту: {fontfile}")
        font, lines, size, pitch_px, fitted = _layout_cover_text(
            b["uk"], x1 - x0, y1 - y0, fontfile, line_h, pitch_ratio)
        if not fitted:
            reasons.append(f"текст не вліз у рамку: {b['uk'][:24]!r}")
        block_h = size + pitch_px * (len(lines) - 1)
        yy = y0 + max(0.0, ((y1 - y0) - block_h) / 2)
        for ln in lines:
            lw = font.getlength(ln)
            draw.text((x0 + ((x1 - x0) - lw) / 2, yy), ln,
                      font=font, fill=b["color"])
            yy += pitch_px
    out = io.BytesIO()
    work.save(out, "PNG")
    return {"png": out.getvalue(),
            "status": "doubtful" if reasons else "ok",
            "reasons": reasons}


# ================================================================
#   ГЕНЕРАТОР ОБКЛАДИНКИ З НУЛЯ — ТІЛЬКИ ЗА ЯВНИМ ЗАПИТОМ (правило 6)
# ================================================================
def _kmeans_palette(img, k=4):
    """3-4 домінантні кольори оригіналу: k-means по пікселях (cv2.kmeans).
    Повертає [(r,g,b)...] 0..255, відсортовані за часткою пікселів."""
    import numpy as np
    import cv2
    small = img.convert("RGB").resize((64, 96))
    data = np.asarray(small, dtype=np.float32).reshape(-1, 3)
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
    # kmeans++ бере стартові центри з глобального RNG OpenCV (thread-local):
    # без фіксації повторний виклик на тому ж файлі давав ІНШУ палітру
    cv2.setRNGSeed(7)
    _c, labels, centers = cv2.kmeans(data, k, None, criteria, 3,
                                     cv2.KMEANS_PP_CENTERS)
    counts = np.bincount(labels.ravel(), minlength=k)
    order = np.argsort(-counts)
    return [tuple(int(v) for v in centers[i]) for i in order]


def _contrast_fg(bg, palette):
    """Найконтрастніший до фону колір ПАЛІТРИ. Якщо палітра малоконтрастна —
    той самий колір темнимо/світлимо до читабельності (чужих кольорів не
    вигадуємо)."""
    cands = [c for c in palette if c != bg] or [(255, 255, 255)]
    base = max(cands, key=lambda c: abs(_lum(c) - _lum(bg)))
    target = 255 if _lum(bg) < 128 else 0
    c, t = base, 0.0
    while abs(_lum(c) - _lum(bg)) < 70 and t < 1.0:
        t += 0.2
        c = tuple(int(base[i] + (target - base[i]) * t) for i in range(3))
    return c


def generate_cover(vision_json, src_cover_png, w, h):
    """Нова обкладинка З НУЛЯ: ЧИСТА ТИПОГРАФІКА на палітрі оригіналу.
    Без градієнтних заглушок і випадкових прикрас. Викликається ТІЛЬКИ явним
    запитом користувача (окремий ендпоінт /cover/generate); в автоматичний
    ланцюжок обкладинки НЕ вбудовується (правило 6).

    vision_json — блоки read_cover_blocks (переклад у "uk", без нього "text").
    w/h — розміри результату; 0 -> розміри src_cover_png.
    Шаблон обирається за числом елементів: <=2 центрований, ==3 верхній блок,
    >=4 нижня плашка. Ієрархія кеглів: title > subtitle > author > other."""
    from PIL import Image, ImageDraw, ImageFont

    src = Image.open(io.BytesIO(src_cover_png)).convert("RGB")
    if not w or not h:
        w, h = src.size

    items = []
    for it in (vision_json or []):
        text = str(it.get("uk") or it.get("text") or "").strip()
        if not text:
            continue
        role = str(it.get("role", "other")).lower()
        if role not in ("title", "subtitle", "author", "other"):
            role = "other"
        items.append((role, text))
    if not items:
        raise ValueError("generate_cover: немає текстових блоків")

    pal = _kmeans_palette(src, 4)
    bg = pal[0]
    fg = _contrast_fg(bg, pal)
    accent = next((c for c in pal[1:] if c != fg
                   and abs(_lum(c) - _lum(bg)) >= 25), fg)

    n = len(items)                      # шаблон — за числом елементів зору
    template = "centered" if n <= 2 else ("top" if n == 3 else "band")
    title = " ".join(t for r, t in items if r == "title")
    if not title:                       # зір не дав title — перший блок ним стає
        title, items = items[0][1], items[1:]
    subtitles = [t for r, t in items if r == "subtitle"]
    authors = [t for r, t in items if r == "author"]
    others = [t for r, t in items if r == "other" and t != title]

    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)
    margin = int(w * 0.08)
    col_w = w - 2 * margin
    f_bold, f_reg = _FONTS[("sans", True, False)], _FONTS[("sans", False, False)]

    def fit(text, fontfile, start, max_lines=3, min_size=14):
        size = max(min_size, int(start))
        while size > min_size:
            f = ImageFont.truetype(fontfile, size)
            lines = _wrap_by_width(f, text, col_w)
            if (len(lines) <= max_lines
                    and all(f.getlength(l) <= col_w for l in lines)):
                return f, lines, size
            size -= 2
        f = ImageFont.truetype(fontfile, min_size)
        return f, _wrap_by_width(f, text, col_w), min_size

    def put(text, fontfile, start, y, color, max_lines=3):
        """Центрований багаторядковий блок від верхньої межі y -> нижня межа."""
        f, lines, sz = fit(text, fontfile, start, max_lines)
        for ln in lines:
            lw = f.getlength(ln)
            draw.text((margin + (col_w - lw) / 2, y), ln, font=f, fill=color)
            y += sz * 1.18
        return y

    t_f, t_lines, t_sz = fit(title.upper(), f_bold, h * 0.085)
    sub_sz, auth_sz, oth_sz = t_sz * 0.50, t_sz * 0.42, max(14, t_sz * 0.32)

    if template == "band":
        draw.rectangle([0, h * 0.86, w, h], fill=accent)

    # автори — завжди зверху (своя строка на кожного)
    y = h * (0.07 if template != "centered" else 0.10)
    for a in authors:
        y = put(a, f_reg, auth_sz, y, fg, max_lines=1) + h * 0.004

    # заголовок: центрований шаблон тримає оптичний центр, інші — верхній блок
    t_block = len(t_lines) * t_sz * 1.18
    ty = (h * 0.40 - t_block / 2) if template != "top" else h * 0.26
    ty = max(ty, y + h * 0.03)
    for ln in t_lines:
        lw = t_f.getlength(ln)
        draw.text((margin + (col_w - lw) / 2, ty), ln, font=t_f, fill=fg)
        ty += t_sz * 1.18
    # тонка акцентна лінія під назвою (палітра, без прикрас)
    ly = ty + h * 0.018
    draw.rectangle([w * 0.37, ly, w * 0.63, ly + max(3, int(h * 0.005))],
                   fill=accent if template != "band" else fg)
    y = ly + h * 0.035

    for s in subtitles:
        y = put(s, f_reg, sub_sz, y, fg, max_lines=3) + h * 0.008

    if others:
        if template == "band":
            oy = h * 0.885
            ocol = _contrast_fg(accent, pal)
        else:
            oy = h * 0.84
            ocol = fg
        for o in others:
            oy = put(o, f_reg, oth_sz, oy, ocol, max_lines=2) + h * 0.004

    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


# ================================================================
#        REFLOW: пересборка книги «с нуля» чистым PDF (v2)
# ================================================================
# Запасной режим для сканов и убитой вёрстки: текст добывается тем же
# конвейером (текстовый слой или OCR), склеивается в абзацы, переводится
# и НАБИРАЕТСЯ заново книжной типографикой (DejaVu Serif/Sans, поля,
# оглавление, номера страниц). Координаты оригинала не сохраняются.

# старий зміст оригіналу в потік не несемо — у книги буде свій новий
_TOC_TITLES = ("содержание", "оглавление", "contents", "зміст", "змiст")

# заголовок рівня частини/глави — завжди H1 (з нової сторінки, у зміст),
# навіть якщо на скані його кегль лише трохи більший за основний текст
_H1_KW = re.compile(r"^(?:частина|часть|розділ|раздел|глава|part|chapter)\b",
                    re.I)

# вихідні дані (копірайт-сторінка) — НЕ заголовок: бібліокоди УДК/ББК/ISBN/ISSN
# у будь-якому місці рядка АБО видавничий шифр на початку («Б 79», «К 48»).
# Битий «!ДК 82-94 ББК… Б 79» додатково ловиться _looks_garbled.
_IMPRINT = re.compile(
    r"\b(?:УДК|ББК|ISBN|ISSN|EAN)\b"
    r"|^\W*[А-ЯA-ZЁ]\s\d{2,4}\b", re.I)


def _is_old_toc_page(plines):
    """Сторінка старого змісту: верхній рядок — «Содержание/Оглавление/Зміст»
    АБО більшість рядків мають крапки-лідери з номером у кінці."""
    head = re.sub(r"\s+", "", " ".join(l["text"] for l in plines[:2])).lower()
    if any(head.startswith(t) for t in _TOC_TITLES):
        return True
    leaders = sum(1 for l in plines
                  if re.search(r"[.…]{2,}\s*\d{1,4}\s*$", l["text"]))
    return len(plines) >= 4 and leaders >= max(3, len(plines) // 2)


def _page_is_blank(page):
    """Майже однотонна сторінка (порожній/білий скан) — у потік НЕ йде, на
    відміну від справжньої ілюстрації-пластини."""
    try:
        pix = page.get_pixmap(matrix=fitz.Matrix(0.25, 0.25),
                              colorspace=fitz.csGRAY)
        data = pix.samples
        if not data:
            return True
        return (max(data) - min(data)) < 24
    except Exception:
        return False


def _reflow_extract(pdf_bytes, ocr_lang="rus"):
    """Сторінки -> рядки {text,x0,x1,y0,y1,size} + картинки.
    Текстовий шар є -> get_text("dict") по РЯДКАХ (працює і для сканів із
    вшитим OCR-шаром); немає -> OCR Tesseract-ом (як у translate_scanned_pdf).
    Повертає (pages_lines, fullpage_imgs:set, inflow_imgs:list, (W,H))."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if doc.page_count == 0:
        doc.close()
        raise ValueError("порожній PDF")
    W, H = doc[0].rect.width, doc[0].rect.height
    pages, fullpage, inflow = [], set(), []
    for pno, page in enumerate(doc):
        lines = []
        for b in page.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            for ln in b["lines"]:
                txt = "".join(s["text"] for s in ln["spans"]).strip()
                if not txt:
                    continue
                szs = [s["size"] for s in ln["spans"] if s["text"].strip()]
                x0, y0, x1, y1 = ln["bbox"]
                # битий cmap у текстовому шарі (як у preserve extract_blocks):
                # рятуємо рядок OCR-ом з пікселів. Не врятували -> лишаємо як є;
                # element-level guard у build_pdf_reflow його не поставить
                # (правило 4). _looks_garbled НЕ чіпаємо (правило 3).
                if _looks_garbled(txt):
                    fixed = _ocr_region(page, (x0, y0, x1, y1), ocr_lang)
                    if fixed and not _looks_garbled(fixed):
                        txt = fixed
                lines.append({"text": txt, "x0": x0, "x1": x1, "y0": y0,
                              "y1": y1, "size": max(szs) if szs else y1 - y0})
        nch = sum(len(l["text"]) for l in lines)
        if nch < 40:                       # справжній скан без шару -> OCR
            try:
                for l in _ocr_lines(page, ocr_lang):
                    x0, y0, x1, y1 = l["bbox"]
                    lines.append({"text": l["text"], "x0": x0, "x1": x1,
                                  "y0": y0, "y1": y1,
                                  "size": (y1 - y0) * 0.9})
                nch = sum(len(l["text"]) for l in lines)
            except Exception as e:
                print(f"reflow: OCR p{pno} недоступний: {e}")
        lines.sort(key=lambda l: (round(l["y0"] / 4), l["x0"]))
        pages.append(lines)
        # картинки сторінки
        try:
            infos = page.get_image_info(xrefs=True)
        except Exception:
            infos = []
        parea = page.rect.width * page.rect.height
        for im in infos:
            bx = im["bbox"]
            share = max(0.0, (bx[2] - bx[0])) * max(0.0, (bx[3] - bx[1])) / max(parea, 1)
            if share >= 0.5:
                # на всю сторінку — це фон-скан майже кожної сторінки книги.
                # Окрема растрова сторінка ЛИШЕ для справжньої ілюстрації-
                # пластини: тексту майже нема (короткий шмуцтитул має
                # реформатуватись як заголовок, а не лягти растром) І сторінка
                # не порожня (білий скан не несемо).
                if nch < 12 and not _page_is_blank(page):
                    fullpage.add(pno)
            elif share >= 0.02 and im.get("xref"):
                try:
                    raw = doc.extract_image(im["xref"])
                    inflow.append({"page": pno, "y": (bx[1] + bx[3]) / 2,
                                   "data": raw["image"],
                                   "w": bx[2] - bx[0], "h": bx[3] - bx[1]})
                except Exception:
                    pass
    doc.close()
    return pages, fullpage, inflow, (W, H)


def _reflow_body_size(pages):
    """Кегль основного тексту = мода розмірів довгих рядків (квант 0.5)."""
    from collections import Counter
    cnt = Counter()
    for p in pages:
        for l in p:
            if len(l["text"]) >= 25:
                cnt[round(l["size"] * 2) / 2] += 1
    return cnt.most_common(1)[0][0] if cnt else 11.0


def _reflow_artifacts(pages, page_h, body_size):
    """Артефакти вихідної верстки геть: частотні колонтитули (верх/низ),
    висячі номери сторінок, рядки старого змісту «текст …… 123».
    Заголовки захищені кеглем (>=1.25 body не чіпаємо)."""
    from collections import Counter

    def norm(t):
        return re.sub(r"\d+", "", t).strip().lower()

    cnt = Counter()
    npages = sum(1 for p in pages if p) or 1
    for p in pages:
        seen = set()
        for l in p:
            if l["y0"] < page_h * 0.12 or l["y1"] > page_h * 0.88:
                n = norm(l["text"])
                if 3 <= len(n) <= 60 and n not in seen:
                    seen.add(n)
                    cnt[n] += 1
    rep = {n for n, c in cnt.items() if c >= max(3, int(npages * 0.25))}
    out = []
    for p in pages:
        keep = []
        for l in p:
            t = l["text"].strip()
            zone = l["y0"] < page_h * 0.12 or l["y1"] > page_h * 0.88
            head = l["size"] >= body_size * 1.25
            if zone and not head and norm(t) in rep:
                continue                                   # колонтитул
            if zone and re.fullmatch(r"[\divxlcXVILC]{1,4}", t):
                continue                                   # голий номер
            if re.search(r"[.…]{2,}\s*\d{1,4}\s*$", t):
                continue                                   # рядок старого змісту
            keep.append(l)
        out.append(keep)
    return out


def _reflow_paragraphs(lines, body_size, set_left, set_right):
    """Рядки ОДНІЄЇ сторінки -> елементи [{kind, text, y0}].
    kind: h1/h2/h3 (за кеглем відносно моди) або para.
    Новий абзац: відступ першого рядка, порожній рядок (великий проміжок),
    попередній рядок коротший ~60% ширини набору + новий з великої літери.
    Переноси «магі-» + «єю» склеюються в «магією»."""
    els = []
    set_w = max(set_right - set_left, 1.0)
    gaps = [b["y0"] - a["y1"] for a, b in zip(lines, lines[1:])
            if b["y0"] > a["y1"]]
    med_gap = sorted(gaps)[len(gaps) // 2] if gaps else 4.0

    def level(sz, text=""):
        t = text.strip()
        # НЕ заголовок (звичайний потік або викид), навіть якщо кегль великий:
        # битий рядок, вихідні дані (УДК/ББК/ISBN/«Б 79»), або задовгий рядок.
        # _looks_garbled лише викликаємо — саму функцію не чіпаємо (правило 3).
        # Биті лишаться garbled і відсіються подвійним захистом у build_pdf_reflow.
        if len(t) > 80 or _looks_garbled(t) or _IMPRINT.search(t):
            return "para"
        kw_h1 = (_H1_KW.match(t) and len(t) < 50
                 and not re.search(r"[.!?]$", t))   # коротка назва, не речення
        if kw_h1 or sz >= body_size * 1.9:
            return "h1"
        if sz >= body_size * 1.55:
            return "h2"
        if sz >= body_size * 1.28:
            return "h3"
        return "para"

    cur, prev = None, None
    for ln in lines:
        k = level(ln["size"], ln["text"])
        if cur is None or k != cur["kind"]:
            new_par = True
        elif k != "para":
            # рядки заголовка зливаються; розрив лише на великому проміжку
            new_par = prev is not None and ln["y0"] - prev["y1"] > med_gap * 2.5 + 1
        else:
            indent = (ln["x0"] - set_left > set_w * 0.03
                      and prev is not None and ln["x0"] - prev["x0"] > 4)
            vgap = (prev is not None
                    and ln["y0"] - prev["y1"] > med_gap * 1.8 + 0.5)
            prev_short = (prev is not None
                          and (prev["x1"] - set_left) < set_w * 0.6
                          and bool(re.match(r"[«\"(]?\s*[—–-]?\s*[А-ЯЁЇІЄҐA-Z\d]",
                                            ln["text"])))
            new_par = indent or vgap or prev_short
        if new_par:
            if cur:
                els.append(cur)
            cur = {"kind": k, "text": ln["text"], "y0": ln["y0"]}
        else:
            # склейка переносу: «магі-» + «єю» -> «магією»
            if (re.search(r"[А-Яа-яЁёЇїІіЄєҐґA-Za-z]-$", cur["text"])
                    and re.match(r"[а-яёїієґa-z]", ln["text"])):
                cur["text"] = cur["text"][:-1] + ln["text"]
            else:
                cur["text"] += " " + ln["text"]
        prev = ln
    if cur:
        els.append(cur)
    return els


def build_reflow_flow(pdf_bytes, ocr_lang="rus", skip_pages=None):
    """Повний потік книги для reflow: [{kind, text|page|data, ...}].
    kind: h1|h2|h3|para|image|pageimg. skip_pages — сторінки, що НЕ йдуть у
    потік (обкладинка, старий титул). Повертає (flow, (W,H), title_page_no)."""
    pages, fullpage, inflow, (W, H) = _reflow_extract(pdf_bytes, ocr_lang)
    body = _reflow_body_size(pages)
    pages = _reflow_artifacts(pages, H, body)
    # старий титул: перша сторінка з текстом (без обкладинки) — його заміняє
    # наш новий титульний лист
    title_pno = next((i for i, p in enumerate(pages) if i > 0 and p), None)
    skip = set(skip_pages or ())
    skip.add(0)
    if title_pno is not None:
        skip.add(title_pno)
    # сторінки старого змісту цілком геть (у книги буде свій новий зміст)
    for i, p in enumerate(pages):
        if p and _is_old_toc_page(p):
            skip.add(i)

    flow = []
    for pno, plines in enumerate(pages):
        if pno in skip:
            continue
        if pno in fullpage:
            flow.append({"kind": "pageimg", "page": pno})
            continue
        if not plines:
            continue
        left = min(l["x0"] for l in plines)
        right = max(l["x1"] for l in plines)
        for el in _reflow_paragraphs(plines, body, left, right):
            el["page"] = pno
            flow.append(el)

    # злиття абзацу через сторінку: попередній не завершений і новий з малої
    merged = []
    for el in flow:
        if (merged and el["kind"] == "para"
                and merged[-1]["kind"] == "para"
                and el["page"] != merged[-1]["page"]
                and not re.search(r"[.!?…:»\")]\s*$", merged[-1]["text"])
                and re.match(r"[а-яёїієґa-z]", el["text"])):
            a = merged[-1]
            if (re.search(r"[А-Яа-яЁёЇїІіЄєҐґA-Za-z]-$", a["text"])
                    and re.match(r"[а-яёїієґa-z]", el["text"])):
                a["text"] = a["text"][:-1] + el["text"]
            else:
                a["text"] += " " + el["text"]
        else:
            merged.append(el)
    flow = merged

    # позначка битого блоку (після злиття абзаців): рядкове OCR-рятування вже
    # відпрацювало в _reflow_extract; те, що лишилось битим, не піде ні в
    # переклад, ні в книгу (правило 4)
    for el in flow:
        if el["kind"] in ("h1", "h2", "h3", "para"):
            el["garbled"] = _looks_garbled(el.get("text", ""))

    # дрібні картинки в потік: після найближчого абзацу своєї сторінки
    for img in sorted(inflow, key=lambda i: (i["page"], i["y"])):
        idx = None
        for k, el in enumerate(flow):
            if el.get("page") == img["page"] and el.get("y0", 0) <= img["y"]:
                idx = k
        item = {"kind": "image", "data": img["data"],
                "w": img["w"], "h": img["h"]}
        flow.insert(idx + 1 if idx is not None else len(flow), item)
    return flow, (W, H), title_pno


def reflow_title_meta(pdf_bytes, api_key, glossary, src, dst, model):
    """Назва й автори для нового титулу. Беремо ПЕРШУ ТЕКСТОВУ титульну
    сторінку, ПРОПУСКАЮЧИ обкладинку (сторінку 0 — у скан-книг там сміттєвий
    текст-шар на кшталт 'магия') і сторінки старого змісту. Найбільші за кеглем
    блоки = назва, дрібніші короткі рядки = автори; перекладаємо їх напряму тим
    самим движком/глосарієм. Помилки -> розумні заглушки."""
    title_uk, authors_uk = "", []
    try:
        pages = _first_pages_blocks(pdf_bytes)
        blk = []
        for i, p in enumerate(pages):
            if i == 0 or not p:                 # обкладинка / порожня
                continue
            if _is_old_toc_page(p):
                continue
            if sum(len(b["text"]) for b in p) >= 15:
                blk = p
                break
        if blk:
            mx = max(b["size"] for b in blk)
            title_src = [b["text"] for b in blk
                         if b["size"] >= mx * 0.7 and len(b["text"]) >= 2][:4]
            author_src = [b["text"] for b in blk
                          if b["size"] < mx * 0.7
                          and 3 <= len(b["text"]) <= 60][:3]
            src_all = title_src + author_src
            if src_all:
                tr = translate_blocks(src_all, api_key, provider="gemini",
                                      model=model, src=src, dst=dst,
                                      glossary=glossary, extra_sys=_ABBR_RULE)
                n = len(title_src)
                title_uk = " ".join(t.strip() for t in tr[:n] if t.strip())[:120]
                authors_uk = [t.strip() for t in tr[n:] if t.strip()]
    except Exception as e:
        print("reflow title meta failed:", e)
    return {"title_uk": title_uk or "Без назви", "authors_uk": authors_uk}


def _reflow_heading_pages(body_doc, heads):
    """Сторінка кожного заголовка в body: послідовний пошук тексту
    (надійніше за element_positions across версій PyMuPDF)."""
    out, start = [], 0
    for lvl, text in heads:
        needle = re.sub(r"\s+", " ", text.strip())[:40]
        found = None
        for pno in range(start, body_doc.page_count):
            try:
                if body_doc[pno].search_for(needle):
                    found = pno
                    break
            except Exception:
                break
        if found is None:
            found = start
        out.append((lvl, text, found))
        start = found
    return out


def build_pdf_reflow(pdf_bytes, flow, meta, cover_png=None,
                     toc_title="Зміст", pagenum_label=None):
    """Збирає ЧИСТУ книгу: [обкладинка][титул][зміст][тіло з номерами сторінок].
    flow — елементи з ПЕРЕКЛАДЕНИМ текстом у ключі "uk" (без нього "text").
    Розмір сторінки = оригінал; поля ~52pt боки / ~64pt верх-низ; тіло
    DejaVu Serif 11.5/1.4 по ширині з абзацним відступом; заголовки DejaVu
    Sans Bold; H1 з нової сторінки; зміст із РЕАЛЬНИМИ номерами сторінок
    тіла (нумерація друкується з першої сторінки тіла)."""
    import html as _html

    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    W, H = src[0].rect.width, src[0].rect.height
    ML = MR = 52.0
    MT, MB = 64.0, 64.0
    rect = fitz.Rect(ML, MT, W - MR, H - MB)

    arch, face_css = _font_assets()
    if arch is None:
        src.close()
        raise RuntimeError("DejaVu не знайдено: reflow потребує шрифтів")
    css = (face_css +
           "body{font-family:'BookSerif',serif;font-size:11.5px;"
           "line-height:1.4;text-align:justify;}"
           "p{margin:0;text-indent:18px;}"
           "p.noind{text-indent:0;}"
           "h1,h2,h3{font-family:'BookSans',sans-serif;font-weight:bold;"
           "line-height:1.25;text-align:left;}"
           "h1{font-size:21px;text-align:center;margin:96px 0 24px 0;}"
           "h2{font-size:15.5px;margin:20px 0 9px 0;}"
           "h3{font-size:12.5px;margin:13px 0 6px 0;}"
           "img{margin:9px 0;}")

    # сегменти: новий сегмент на h1 (свіжа сторінка) і на pageimg
    segments, cur = [], []
    for el in flow:
        if el["kind"] == "pageimg":
            if cur:
                segments.append(("story", cur))
                cur = []
            segments.append(("pageimg", el))
        elif el["kind"] == "h1" and cur:
            segments.append(("story", cur))
            cur = [el]
        else:
            cur.append(el)
    if cur:
        segments.append(("story", cur))

    body = fitz.open()
    heads = []                      # (lvl, text) у порядку появи
    img_n = 0
    set_w = W - ML - MR
    for kind, seg in segments:
        if kind == "pageimg":
            pg = body.new_page(width=W, height=H)
            spage = src[seg["page"]]
            z = 2.0
            from PIL import Image
            pm = spage.get_pixmap(matrix=fitz.Matrix(z, z))
            pimg = Image.open(io.BytesIO(pm.tobytes("png"))).convert("RGB")
            # стираємо оригінальний текст-шар з ілюстрації інпейнтингом (той
            # самий механізм, що в build_pdf / обкладинках) — щоб у чистій книзі
            # на картинці не лишалось напису мовою оригіналу замість плашки
            regions = []
            try:
                for b in spage.get_text("dict")["blocks"]:
                    if b.get("type") != 0:
                        continue
                    for ln in b["lines"]:
                        spans = [s for s in ln["spans"] if s["text"].strip()]
                        if not spans:
                            continue
                        col = max(spans, key=lambda s: len(s["text"])).get("color", 0)
                        rgb = ((col >> 16) & 255, (col >> 8) & 255, col & 255)
                        bx = ln["bbox"]
                        regions.append((bx[0]*z, bx[1]*z, bx[2]*z, bx[3]*z, rgb))
            except Exception:
                regions = []
            if regions:
                pimg, _ = _inpaint_letters(pimg, regions)
            buf = io.BytesIO()
            pimg.save(buf, "PNG")
            pg.insert_image(pg.rect, stream=buf.getvalue())
            continue
        parts, after_head = [], False
        for el in seg:
            if el["kind"] == "image":
                img_n += 1
                name = f"reflowimg{img_n}.png"
                arch.add(el["data"], name)
                iw = min(set_w, max(40.0, float(el["w"])))
                ih = iw * (float(el["h"]) / max(float(el["w"]), 1.0))
                parts.append(f'<img src="{name}" width="{iw:.0f}" '
                             f'height="{ih:.0f}">')
                continue
            raw = (el.get("uk") or el.get("text") or "").strip()
            if not raw:
                continue
            # ПОДВІЙНА ЗАХИСТ (правило 4, як у build_pdf): блок не ставимо,
            # якщо битий ОРИГІНАЛ або битий ПЕРЕКЛАД. el["garbled"] ловить і
            # ту кашу, що _looks_garbled пропускає (її виставили на джерелі).
            if (el.get("garbled") or _looks_garbled(el.get("text", ""))
                    or _looks_garbled(raw)):
                continue
            txt = _html.escape(raw)
            if el["kind"] in ("h1", "h2", "h3"):
                heads.append((int(el["kind"][1]), el.get("uk") or el["text"]))
                parts.append(f"<{el['kind']}>{txt}</{el['kind']}>")
                after_head = True
            else:
                cls = ' class="noind"' if after_head else ""
                parts.append(f"<p{cls}>{txt}</p>")
                after_head = False
        if not parts:
            continue
        story = fitz.Story(html="<body>" + "".join(parts) + "</body>",
                           user_css=css, archive=arch)
        buf = io.BytesIO()
        wr = fitz.DocumentWriter(buf)
        while True:
            dev = wr.begin_page(fitz.Rect(0, 0, W, H))
            more, _f = story.place(rect)
            story.draw(dev)
            wr.end_page()
            if not more:
                break
        wr.close()
        body.insert_pdf(fitz.open("pdf", buf.getvalue()))

    if body.page_count == 0:
        src.close()
        raise ValueError("reflow: порожнє тіло книги (не знайдено тексту)")

    # номери сторінок тіла: знизу по центру, з 1
    serif = _FONTS[("serif", False, False)]
    for i in range(body.page_count):
        body[i].insert_textbox(fitz.Rect(0, H - MB + 16, W, H - MB + 34),
                               str(i + 1), fontsize=9, fontname="pgn",
                               fontfile=serif, align=1)

    toc_entries = _reflow_heading_pages(body, [h for h in heads
                                               if h[0] in (1, 2)])

    out = fitz.open()
    f_serif = fitz.Font(fontfile=serif)
    f_sans_b = fitz.Font(fontfile=_FONTS[("sans", True, False)])

    def center_text(page, y, text, font, fsize, fname, ffile):
        tl = font.text_length(text, fontsize=fsize)
        page.insert_text((max(ML, (W - tl) / 2), y), text, fontsize=fsize,
                         fontname=fname, fontfile=ffile)

    # --- обкладинка
    if cover_png:
        pg = out.new_page(width=W, height=H)
        try:
            pg.insert_image(pg.rect, stream=cover_png)
        except Exception:
            out.delete_page(0)

    # --- титул: автори дрібно, назва великим Sans Bold, акцентна лінія
    pg = out.new_page(width=W, height=H)
    y = H * 0.20
    for a in meta.get("authors_uk", [])[:3]:
        center_text(pg, y, a, f_serif, 11.5, "tser", serif)
        y += 16
    ty = max(H * 0.40, y + 30)
    tsize = 24.0
    words = (meta.get("title_uk") or "Без назви").split()
    lines, cur = [], ""
    while tsize >= 14:
        lines, cur = [], ""
        for wd in words:
            t = (cur + " " + wd).strip()
            if f_sans_b.text_length(t, fontsize=tsize) <= W - 2 * ML or not cur:
                cur = t
            else:
                lines.append(cur)
                cur = wd
        if cur:
            lines.append(cur)
        if all(f_sans_b.text_length(l, fontsize=tsize) <= W - 2 * ML
               for l in lines) and len(lines) <= 4:
            break
        tsize -= 2
    for l in lines:
        center_text(pg, ty, l, f_sans_b, tsize,
                    "tsanb", _FONTS[("sans", True, False)])
        ty += tsize * 1.25
    pg.draw_rect(fitz.Rect(W * 0.40, ty + 10, W * 0.60, ty + 13),
                 color=None, fill=(0.15, 0.15, 0.15))

    # --- зміст: H1/H2 з реальними номерами сторінок тіла
    if toc_entries:
        pg = out.new_page(width=W, height=H)
        center_text(pg, MT + 24, toc_title, f_sans_b, 16,
                    "tsanb", _FONTS[("sans", True, False)])
        y = MT + 60
        dotw = f_serif.text_length(".", fontsize=11.5)
        for lvl, text, pno in toc_entries:
            if y > H - MB - 10:
                pg = out.new_page(width=W, height=H)
                y = MT + 20
            t = re.sub(r"\s+", " ", text.strip())[:90]
            num = str(pno + 1)
            indent = 0 if lvl == 1 else 16
            font = f_sans_b if lvl == 1 else f_serif
            fname, ffile = (("tsanb", _FONTS[("sans", True, False)])
                            if lvl == 1 else ("tser", serif))
            tw = font.text_length(t, fontsize=11.5)
            nw = f_serif.text_length(num, fontsize=11.5)
            avail = (W - MR) - (ML + indent) - tw - nw - 8
            dots = " " + "." * max(0, int(avail / max(dotw, 0.1))) if avail > dotw * 3 else " "
            pg.insert_text((ML + indent, y), t, fontsize=11.5,
                           fontname=fname, fontfile=ffile)
            pg.insert_text((ML + indent + tw, y), dots, fontsize=11.5,
                           fontname="tser", fontfile=serif)
            pg.insert_text((W - MR - nw, y), num, fontsize=11.5,
                           fontname="tser", fontfile=serif)
            y += 18 if lvl == 1 else 16

    out.insert_pdf(body)
    out.set_metadata({"title": meta.get("title_uk", ""),
                      "author": ", ".join(meta.get("authors_uk", []))})
    res = out.tobytes(deflate=True, garbage=4)
    out.close()
    body.close()
    src.close()
    return res
