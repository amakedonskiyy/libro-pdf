"""
main.py — мікросервіс Libro PDF Builder (FastAPI).

Ендпоінти:
  GET  /health             — перевірка, що сервіс живий.
  POST /translate-pdf-sync — завантажуєш PDF, у відповідь одразу готовий PDF.
                             Для ШВИДКОГО ТЕСТУ на 5-сторінковому файлі.
  POST /preview            — повертає блоки + переклад у JSON (для майбутнього
                             екрана редагування перекладу перед збіркою).
  POST /translate-pdf      — АСИНХРОННО: запускає фонову задачу, пише прогрес у
                             таблицю Supabase `translations`, заливає результат
                             у bucket `results`. Фронт опитує статус (як зараз).

ENV (для асинхронного режиму та заливки результату):
  SUPABASE_URL              напр. https://xxxx.supabase.co
  SUPABASE_SERVICE_ROLE_KEY service_role ключ (тільки на сервері, НЕ на фронті!)
"""
import os
import io
import traceback
import requests
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse

import pdf_translator as P

app = FastAPI(title="Libro PDF Builder")

# дозволяємо виклики з фронта (звузь до свого домена в проді)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPA_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPA_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")


# ---------------------------------------------------------------- Supabase helpers
def _supa_headers(extra=None):
    h = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}"}
    if extra:
        h.update(extra)
    return h


def supa_update(translation_id, **fields):
    """PATCH рядка translations (status, progress, result_url, error...)."""
    if not (SUPA_URL and SUPA_KEY):
        return
    try:
        requests.patch(
            f"{SUPA_URL}/rest/v1/translations?id=eq.{translation_id}",
            headers=_supa_headers({"Content-Type": "application/json",
                                   "Prefer": "return=minimal"}),
            json=fields, timeout=15,
        )
    except Exception as e:
        print("supa_update failed:", e)


def supa_upload_result(path, pdf_bytes):
    """Заливає PDF у bucket results і повертає підписаний URL (7 днів)."""
    if not (SUPA_URL and SUPA_KEY):
        return None
    # upload (upsert)
    requests.post(
        f"{SUPA_URL}/storage/v1/object/results/{path}",
        headers=_supa_headers({"Content-Type": "application/pdf",
                               "x-upsert": "true"}),
        data=pdf_bytes, timeout=60,
    ).raise_for_status()
    # signed url
    r = requests.post(
        f"{SUPA_URL}/storage/v1/object/sign/results/{path}",
        headers=_supa_headers({"Content-Type": "application/json"}),
        json={"expiresIn": 60 * 60 * 24 * 7}, timeout=15,
    )
    r.raise_for_status()
    return SUPA_URL + "/storage/v1" + r.json()["signedURL"]


# ---------------------------------------------------------------- endpoints
@app.get("/health")
def health():
    return {"ok": True, "supabase_configured": bool(SUPA_URL and SUPA_KEY)}


@app.post("/translate-pdf-sync")
async def translate_sync(
    file: UploadFile = File(...),
    api_key: str = Form(...),
    model: str = Form("llama-3.3-70b-versatile"),
    src: str = Form("ru"),
    dst: str = Form("uk"),
):
    """Синхронно: PDF -> готовий PDF у відповіді. Тільки для невеликих файлів/тесту."""
    pdf_bytes = await file.read()
    try:
        out = P.translate_pdf(pdf_bytes, api_key, model=model, src=src, dst=dst)
    except Exception as e:
        raise HTTPException(500, f"translate error: {e}")
    name = (file.filename or "book").rsplit(".", 1)[0] + "_uk.pdf"
    return StreamingResponse(
        io.BytesIO(out), media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.post("/preview")
async def preview(
    file: UploadFile = File(...),
    api_key: str = Form(...),
    model: str = Form("llama-3.3-70b-versatile"),
    src: str = Form("ru"),
    dst: str = Form("uk"),
):
    """Повертає блоки з оригіналом і перекладом (JSON). Для екрана редагування."""
    pdf_bytes = await file.read()
    pages = P.extract_blocks(pdf_bytes, ocr_lang=P._LANG_OCR.get(src, "rus"))
    flat = [(b["id"], b["text"]) for blocks in pages for b in blocks]
    translated = P.translate_blocks([t for _, t in flat], api_key,
                                    model=model, src=src, dst=dst)
    tmap = {flat[i][0]: translated[i] for i in range(len(flat))}
    result = [
        {"id": b["id"], "page": b["page"], "size": b["size"],
         "original": b["text"], "translated": tmap.get(b["id"], "")}
        for blocks in pages for b in blocks
    ]
    return JSONResponse({"blocks": result})


def _run_job(pdf_bytes, translation_id, api_key, model, src, dst, filename):
    """Фонова задача: переклад + збірка + заливка + оновлення прогресу."""
    try:
        supa_update(translation_id, status="processing", progress=1)

        def cb(done, total):
            supa_update(translation_id,
                        progress=max(1, min(95, int(done / max(total, 1) * 95))))

        pages = P.extract_blocks(pdf_bytes, ocr_lang=P._LANG_OCR.get(src, "rus"))
        flat = [(b["id"], b["text"]) for blocks in pages for b in blocks]
        translated = P.translate_blocks([t for _, t in flat], api_key,
                                        model=model, src=src, dst=dst, progress_cb=cb)
        tmap = {flat[i][0]: translated[i] for i in range(len(flat))}
        out = P.build_pdf(pdf_bytes, pages, tmap)

        supa_update(translation_id, progress=97)
        base = (filename or "book").rsplit(".", 1)[0]
        url = supa_upload_result(f"{translation_id}/{base}_uk.pdf", out)
        supa_update(translation_id, status="done", progress=100, result_url=url)
    except Exception as e:
        traceback.print_exc()
        supa_update(translation_id, status="error", error=str(e)[:500])


@app.post("/translate-pdf")
async def translate_async(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    translation_id: str = Form(...),
    api_key: str = Form(...),
    model: str = Form("llama-3.3-70b-versatile"),
    src: str = Form("ru"),
    dst: str = Form("uk"),
):
    """Асинхронно: одразу повертає 202, далі пише прогрес у Supabase."""
    if not (SUPA_URL and SUPA_KEY):
        raise HTTPException(500, "Supabase не налаштований (ENV SUPABASE_URL / "
                                 "SUPABASE_SERVICE_ROLE_KEY). Для тесту: /translate-pdf-sync")
    pdf_bytes = await file.read()
    background.add_task(_run_job, pdf_bytes, translation_id, api_key,
                        model, src, dst, file.filename)
    return JSONResponse({"status": "started", "translation_id": translation_id},
                        status_code=202)
