# Libro PDF Builder — Python мікросервіс

Замінює оригінальний текст PDF на переклад, **зберігаючи зображення, верстку
і стилі**. Вихід — справжній PDF з виділюваним текстом (не картинка).

## Чому саме так
Старий підхід (pdf-lib / jsPDF у браузері) малював білі прямокутники поверх
тексту — але текстовий шар оригіналу рендериться зверху, тому видно обидва
тексти. Тут використано **PyMuPDF redaction** (`apply_redactions`), який
**фізично видаляє** оригінальний текст із PDF, а зображення лишає на місці
(`images=PDF_REDACT_IMAGE_NONE`). Потім переклад вставляється на ті самі
координати з автопідбором розміру шрифту.

Supabase Edge Functions тут не підходять: вони працюють на Deno (Python не
підтримується), а ліміти ~150 МБ RAM / 60 с уб'ють обробку книги на 272 стор.
Тому — окремий Python-сервіс.

## Файли
- `pdf_translator.py` — ядро: extract → translate (Groq) → build (redaction).
- `main.py` — FastAPI: `/health`, `/translate-pdf-sync`, `/preview`, `/translate-pdf`.
- `Dockerfile`, `requirements.txt`.

## Локальний запуск
```bash
pip install -r requirements.txt          # потрібні шрифти DejaVu у системі
uvicorn main:app --reload --port 8000
```

## Швидкий тест на 5-сторінковому файлі (без Supabase)
```bash
curl -X POST http://localhost:8000/translate-pdf-sync \
  -F "file=@terapia_test_5pages.pdf" \
  -F "api_key=ВАШ_GROQ_КЛЮЧ" \
  -F "src=ru" -F "dst=uk" \
  -o result.pdf
```
Відкрий `result.pdf` — текст український, зображення на місці, текст виділяється.

## Деплой на Railway (або Render)
1. Заливаєш ці файли в GitHub-репозиторій.
2. Railway → New Project → Deploy from GitHub repo. Dockerfile підхопиться сам.
3. Variables (тільки на сервері!):
   - `SUPABASE_URL` = `https://xxxx.supabase.co`
   - `SUPABASE_SERVICE_ROLE_KEY` = service_role ключ із Supabase → Settings → API
4. Отримуєш публічний URL, напр. `https://libro-pdf.up.railway.app`.

> **Важливо:** для книги ~272 стор. переклад через Groq триває довго не через
> процесор, а через ліміт токенів/хв на безкоштовному тарифі (~6000 TPM →
> ~20–40 хв). Тому використовується асинхронний `/translate-pdf` з прогресом.

## Інтеграція з Lovable (встав цей промпт)

> Replace the broken PDF Builder. Stop generating PDFs on the client (remove
> jsPDF / pdf-lib PDF code). Instead call an external Python microservice.
>
> Add a setting `PDF_SERVICE_URL` (value: my Railway URL).
>
> **PDF Builder flow:**
> 1. User uploads the original PDF on the PDF Builder screen.
> 2. Insert/update a row in `translations` table: status `uploading`, then `queued`.
> 3. POST multipart/form-data to `${PDF_SERVICE_URL}/translate-pdf` with fields:
>    `file` (the PDF), `translation_id` (the row id), `api_key` (user's Groq key
>    from `user_settings`), `src` (`ru` or `en`), `dst` (`uk`).
> 4. The service returns 202 immediately. Then poll the `translations` row every
>    5 seconds: show `progress` in the progress bar; when `status = done`, show a
>    Download button that opens `result_url`; if `status = error`, show `error`.
>
> Do NOT send the Groq key or service role key anywhere public. Keep all existing
> design and colors unchanged.

## Виправлення бага зі скачуванням (.txt не качався)
Причина — приватний bucket + dev-режим без авторизованого користувача.
Сервіс уже повертає **signed URL** (діє 7 днів) у `result_url` — качай прямо
по ньому, авторизація не потрібна. Якщо колись робитимеш кнопку качання
вручну на фронті — використовуй `supabase.storage.from('results')
.createSignedUrl(path, 604800)`, а не публічний `getPublicUrl`.

## Якість перекладу
Промпт у `pdf_translator.py` (`_SYS_PROMPT`) уже жорстко забороняє російські
слова. Якщо `llama3-70b-8192` лишає русизми — у `model` постав
`llama-3.3-70b-versatile` (сильніша для української; перевір актуальну назву в
консолі Groq). Альтернатива з минулих ітерацій — Gemini `gemini-1.5-flash`.

## Прод-авторизація
Зараз dev-режим. Перед продом: увімкни Magic Link, переконайся що RLS на
`translations` і `user_settings` справді обмежує `auth.uid() = user_id`, і що
маршрути захищені (редірект на /login без сесії).
