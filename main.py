"""
main.py — мікросервіс Libro PDF Builder (FastAPI).

Ендпоінти:
  GET  /health              — перевірка.
  POST /translate-pdf-sync  — миттєва відповідь PDF. ТІЛЬКИ дрібні файли (~5 стор.).
  POST /jobs                — фонова задача БЕЗ Supabase: завантажуєш PDF, отримуєш
                              job_id. provider=gemini|groq, proofread=true|false.
  GET  /jobs/{id}           — статус (queued/processing/done/error + progress).
  GET  /jobs/{id}/download  — завантажити готовий PDF, коли status=done.
  POST /preview             — блоки+переклад у JSON.
  POST /translate-pdf       — фон із записом у Supabase (для інтеграції з Lovable).

api_key = ключ обраного провайдера (Gemini: AIza...  /  Groq: gsk_...).
"""
import os
import io
import re
import uuid
import traceback
import requests
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse

import pdf_translator as P

app = FastAPI(title="Libro PDF Builder")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

SUPA_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPA_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

JOBS = {}
JOB_DIR = "/tmp/libro_jobs"
os.makedirs(JOB_DIR, exist_ok=True)


# ---------------------------------------------------------------- Supabase helpers
def _supa_headers(extra=None):
    h = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}"}
    if extra:
        h.update(extra)
    return h


def supa_update(translation_id, **fields):
    if not (SUPA_URL and SUPA_KEY):
        return
    try:
        requests.patch(
            f"{SUPA_URL}/rest/v1/translations?id=eq.{translation_id}",
            headers=_supa_headers({"Content-Type": "application/json",
                                   "Prefer": "return=minimal"}),
            json=fields, timeout=15)
    except Exception as e:
        print("supa_update failed:", e)


def supa_upload_result(path, pdf_bytes):
    if not (SUPA_URL and SUPA_KEY):
        return None
    requests.post(f"{SUPA_URL}/storage/v1/object/results/{path}",
                  headers=_supa_headers({"Content-Type": "application/pdf",
                                         "x-upsert": "true"}),
                  data=pdf_bytes, timeout=60).raise_for_status()
    r = requests.post(f"{SUPA_URL}/storage/v1/object/sign/results/{path}",
                      headers=_supa_headers({"Content-Type": "application/json"}),
                      json={"expiresIn": 60 * 60 * 24 * 7}, timeout=15)
    r.raise_for_status()
    return SUPA_URL + "/storage/v1" + r.json()["signedURL"]


ENGINE_VERSION = "2026-06-13-reflow-v3"


def _safe_err(e, limit=200):
    """Текст помилки для статусів/логів: ключі з URL-ів вирізаються (правило 10)."""
    return re.sub(r"key=[A-Za-z0-9_\-\.]+", "key=***", str(e))[:limit]


# ---------------------------------------------------------------- endpoints
@app.get("/health")
def health():
    return {"ok": True, "version": ENGINE_VERSION,
            "supabase_configured": bool(SUPA_URL and SUPA_KEY)}


@app.post("/translate-pdf-sync")
def translate_sync(file: UploadFile = File(...), api_key: str = Form(...),
                   provider: str = Form("gemini"), model: str = Form(""),
                   src: str = Form("ru"), dst: str = Form("uk"),
                   proofread: bool = Form(False)):
    """Синхронно: PDF -> готовий PDF. ТІЛЬКИ дрібні файли (~5 стор.)."""
    pdf_bytes = file.file.read()
    try:
        out = P.translate_pdf(pdf_bytes, api_key, provider=provider,
                              model=model or None, src=src, dst=dst,
                              proofread=proofread)
    except Exception as e:
        raise HTTPException(500, f"translate error: {e}")
    name = (file.filename or "book").rsplit(".", 1)[0] + "_uk.pdf"
    return StreamingResponse(io.BytesIO(out), media_type="application/pdf",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ----------- ФОНОВА ЧЕРГА без Supabase (для великих книг через браузер) -----------
def _reflow_cover_png(job_id, pdf_bytes, api_key, provider, model, src, dst,
                      glossary, cover_mode, log):
    """PNG обкладинки для reflow-книги. cover_mode: generate (дефолт reflow,
    бо чистій книзі — чиста обкладинка) | replace (vision/inpaint на місці) |
    original. Явний вибір користувача важливіший за дефолт. Будь-який збій ->
    рендер оригінальної обкладинки (гірше за оригінал не робимо)."""
    try:
        orig = P.render_cover_png(pdf_bytes)
    except Exception as e:
        log(f"reflow cover: render original failed: {_safe_err(e)}")
        return None
    mode = (cover_mode or "generate").lower()
    if mode not in ("generate", "replace", "original"):
        mode = "generate"
    if mode == "original" or provider != "gemini":
        if mode != "original":
            log("reflow cover: provider != gemini -> оригінальна обкладинка")
        return orig
    try:
        if mode == "generate":
            blocks, reasons = P.read_cover_blocks(orig, api_key,
                                                  glossary=glossary, src=src,
                                                  dst=dst, model=model or None,
                                                  pdf_bytes=pdf_bytes)
            png = P.generate_cover(blocks, orig, 0, 0)
        else:                                  # replace: vision/inpaint
            res = P.translate_cover_vision(orig, api_key, glossary=glossary,
                                           src=src, dst=dst,
                                           model=model or None,
                                           pdf_bytes=pdf_bytes)
            png, reasons = res["png"], res["reasons"]
        JOBS[job_id]["cover_status"] = "doubtful" if reasons else "ok"
        if reasons:
            JOBS[job_id]["cover_reasons"] = reasons[:8]
        log(f"reflow cover: {mode} ok")
        return png
    except Exception as e:
        JOBS[job_id]["cover_status"] = "failed"
        JOBS[job_id]["cover_reasons"] = [_safe_err(e)]
        log(f"reflow cover {mode} failed -> оригінальна: {_safe_err(e)}")
        return orig


def _run_local_job(job_id, pdf_bytes, api_key, provider, model, src, dst,
                   proofread, filename, vision=False, vision_model="",
                   cover_vision=False, layout_mode="preserve", cover_mode="",
                   reuse_tr=None):
    import sys
    def log(m):
        print(f"[job {job_id}] {m}", flush=True); sys.stdout.flush()
    try:
        JOBS[job_id].update(status="processing", progress=1)
        log(f"start [{ENGINE_VERSION}]: {len(pdf_bytes)//1024}KB, provider={provider}, model={model!r}, src={src}, vision={vision}, cover_vision={cover_vision}, layout={layout_mode}")

        def cb(done, total):
            JOBS[job_id]["progress"] = max(1, min(99, int(done / max(total, 1) * 99)))

        # ---------------- REFLOW: пересборка книги «з нуля» чистим PDF ----------------
        if layout_mode == "reflow":
            log("reflow: extract lines + paragraphs...")
            flow, _wh, _tp = P.build_reflow_flow(
                pdf_bytes, ocr_lang=P._LANG_OCR.get(src, "rus"))
            tidx = [i for i, el in enumerate(flow)
                    if el["kind"] in ("h1", "h2", "h3", "para")]
            if not tidx:
                raise RuntimeError("reflow: у книзі не знайдено тексту "
                                   "(справжній скан без OCR-шару?)")
            # биті блоки (битий cmap, OCR-рятунок не допоміг) у переклад НЕ
            # шлемо: build_pdf_reflow їх однаково відкине (правило 4). Так не
            # палимо токени на кашу і не даємо LLM щось «вигадати» з неї.
            good = [i for i in tidx if not flow[i].get("garbled")]
            ng = len(tidx) - len(good)
            nimg = sum(1 for el in flow if el["kind"] == "image")
            npimg = sum(1 for el in flow if el["kind"] == "pageimg")
            log(f"reflow: text elements={len(tidx)} (битих відкинуто={ng}), "
                f"inflow images={nimg}, full-page images={npimg}")
            texts = [flow[i]["text"] for i in good]
            log("building glossary (term consistency)...")
            glossary = P.build_glossary(texts, api_key, provider=provider,
                                        model=model or None, src=src, dst=dst)
            log(f"glossary: {len(glossary)} terms")
            # rebuild: переклади вихідного job у пам'яті -> не палимо API двічі
            pre = {}
            if reuse_tr:
                def _norm(t):
                    return re.sub(r"[\s\-­]+", "", t or "").lower()
                rmap = {_norm(o): u for o, u in reuse_tr.items()
                        if (o or "").strip() and (u or "").strip()}
                for i in good:
                    u = rmap.get(_norm(flow[i]["text"]))
                    if u:
                        flow[i]["uk"] = u
                        pre[i] = True
                log(f"reflow: reuse {len(pre)}/{len(good)} перекладів")
            need = [i for i in good if i not in pre]
            if need:
                log(f"translate_blocks: {len(need)} elements...")
                tr = P.translate_blocks([flow[i]["text"] for i in need],
                                        api_key, provider=provider,
                                        model=model or None, src=src, dst=dst,
                                        proofread=proofread, progress_cb=cb,
                                        glossary=glossary)
                for k, i in enumerate(need):
                    flow[i]["uk"] = tr[k]
            log("reflow: title page meta...")
            meta = P.reflow_title_meta(pdf_bytes, api_key, glossary, src, dst,
                                       model or None)
            cover_png = _reflow_cover_png(job_id, pdf_bytes, api_key, provider,
                                          model, src, dst, glossary,
                                          cover_mode, log)
            log("build_pdf_reflow...")
            out = P.build_pdf_reflow(pdf_bytes, flow, meta, cover_png=cover_png)
            JOBS[job_id]["src_texts"] = {flow[i]["text"]: flow[i].get("uk", "")
                                         for i in good}
            log(f"done: {len(out)//1024}KB")
            path = os.path.join(JOB_DIR, f"{job_id}.pdf")
            with open(path, "wb") as f:
                f.write(out)
            JOBS[job_id].update(status="done", progress=100, path=path)
            return

        recipe = {}
        if vision:
            JOBS[job_id]["vision"] = "pending"
            log("vision: analyzing first pages...")
            recipe = P.analyze_book(pdf_bytes, api_key, provider=provider,
                                    model=(vision_model or None))
            JOBS[job_id]["vision"] = "ok" if recipe else "failed"
            log(f"vision recipe: {recipe}")

        log("extract_blocks...")
        pages = P.extract_blocks(pdf_bytes, ocr_lang=P._LANG_OCR.get(src, "rus"))
        nb = sum(len(p) for p in pages)
        # Маршрут (скан чи текст) вирішуємо за РЕАЛЬНИМ текстовим шаром.
        # Зір часто помилково каже "скан" для звичайних текстових книг —
        # тому його підказку беремо лише коли тексту майже немає (справжній скан).
        scanned = P.looks_scanned(pages)
        if not scanned and recipe.get("scanned"):
            nchars = sum(len(b["text"]) for p in pages for b in p)
            if nchars < 60 * max(len(pages), 1):
                scanned = True
        log(f"extracted: pages={len(pages)}, blocks={nb}, scanned={scanned}")
        if scanned:
            log("scanned mode -> OCR all pages...")
            out = P.translate_scanned_pdf(pdf_bytes, api_key, provider=provider,
                                          model=model or None, src=src, dst=dst,
                                          proofread=proofread, progress_cb=cb)
        else:
            flat = [(b["id"], b["text"]) for blk in pages for b in blk]
            log("building glossary (term consistency)...")
            glossary = P.build_glossary([t for _, t in flat], api_key,
                                        provider=provider, model=model or None,
                                        src=src, dst=dst)
            log(f"glossary: {len(glossary)} terms")
            log(f"translate_blocks: {len(flat)} blocks...")
            tr = P.translate_blocks([t for _, t in flat], api_key, provider=provider,
                                    model=model or None, src=src, dst=dst,
                                    proofread=proofread, progress_cb=cb,
                                    glossary=glossary)
            log("build_pdf...")
            tmap = {flat[i][0]: tr[i] for i in range(len(flat))}
            # переклади лишаються в пам'яті процесу: /jobs/{id}/rebuild
            # перевикористає їх і не палитиме API вдруге
            JOBS[job_id]["src_texts"] = {flat[i][1]: tr[i]
                                         for i in range(len(flat))}
            out = P.build_pdf(pdf_bytes, pages, tmap, recipe=recipe)
            # ОБКЛАДИНКА: якщо 1-ша сторінка — суцільна картинка (0 текстових
            # блоків), на текстовому шляху вона лишилась би мовою оригіналу.
            # Пересоберемо її з перекладеною назвою (назву беремо з тексту
            # титульної сторінки, не з OCR обкладинки). Безпечно: будь-яка
            # помилка -> лишаємо оригінальну обкладинку, задача не падає.
            if (pages and len(pages[0]) == 0
                    and not (recipe or {}).get("keep_original_cover")):
                cov = None
                # Ланцюжок обкладинки: vision (за прапорцем) -> OCR-на-місці ->
                # оригінал. Будь-яка помилка кроку = перехід до наступного,
                # задача НЕ падає (правило 6).
                if cover_vision and provider == "gemini":
                    try:
                        log("cover: vision mode (read -> translate -> inpaint)...")
                        png = P.render_cover_png(pdf_bytes)
                        res = P.translate_cover_vision(png, api_key,
                                                       glossary=glossary,
                                                       src=src, dst=dst,
                                                       model=model or None,
                                                       pdf_bytes=pdf_bytes)
                        cov = res["png"]
                        # сигнал якості для кнопки на фронті; рішення за людиною
                        JOBS[job_id]["cover_status"] = res["status"]
                        if res["reasons"]:
                            JOBS[job_id]["cover_reasons"] = res["reasons"][:8]
                        log(f"cover: vision {res['status']} {res['reasons']}")
                    except Exception as ce:
                        cov = None
                        JOBS[job_id]["cover_status"] = "failed"
                        JOBS[job_id]["cover_reasons"] = [_safe_err(ce)]
                        log(f"cover vision failed -> OCR fallback: {_safe_err(ce)}")
                elif cover_vision:
                    log("cover_vision підтримує лише provider=gemini -> OCR fallback")
                if cov is None:
                    try:
                        log("cover is image -> trying in-place title replacement...")
                        cov = P.make_cover(pdf_bytes, api_key, provider=provider,
                                           model=model or None, src=src, dst=dst,
                                           title=None, author="", recipe=recipe,
                                           glossary=glossary, allow_generate=False)
                    except Exception as ce:
                        cov = None
                        log(f"cover skipped (kept original): {_safe_err(ce)}")
                rep = P.replace_first_page_image(out, cov) if cov else None
                if rep:
                    out = rep
                    # vision впав, але OCR-фолбек таки замінив обкладинку:
                    # це "doubtful" (перевір оком), а не "failed" (= оригінал)
                    if JOBS[job_id].get("cover_status") == "failed":
                        JOBS[job_id]["cover_status"] = "doubtful"
                        JOBS[job_id].setdefault("cover_reasons", []).append(
                            "vision не спрацював; обкладинку замінено OCR-фолбеком")
                    log("cover: replaced")
                else:
                    # failed строго = лишилась оригінальна обкладинка
                    if cover_vision and provider == "gemini":
                        JOBS[job_id]["cover_status"] = "failed"
                    log("cover: could not replace cleanly -> kept original")
        log(f"done: {len(out)//1024}KB")
        path = os.path.join(JOB_DIR, f"{job_id}.pdf")
        with open(path, "wb") as f:
            f.write(out)
        JOBS[job_id].update(status="done", progress=100, path=path)
    except Exception as e:
        traceback.print_exc()
        log(f"ERROR: {e}")
        JOBS[job_id].update(status="error", error=str(e)[:500])


@app.post("/jobs")
async def create_job(background: BackgroundTasks,
                     file: UploadFile = File(...), api_key: str = Form(...),
                     provider: str = Form("gemini"), model: str = Form(""),
                     src: str = Form("ru"), dst: str = Form("uk"),
                     proofread: bool = Form(False),
                     vision: bool = Form(False), vision_model: str = Form(""),
                     cover_vision: bool = Form(False),
                     layout_mode: str = Form("preserve"),
                     cover_mode: str = Form("")):
    """Запускає фонову задачу. Одразу повертає job_id (без таймауту).
    cover_vision=true -> обкладинку читає зір (Gemini) і перемальовує.
    layout_mode: preserve (як є, дефолт) | reflow (пересборка чистою книгою —
    запасний режим для сканів і вбитої верстки).
    cover_mode (для reflow): generate (дефолт) | replace | original."""
    if layout_mode not in ("preserve", "reflow"):
        raise HTTPException(400, "layout_mode: preserve | reflow")
    pdf_bytes = await file.read()
    job_id = uuid.uuid4().hex[:12]
    base = (file.filename or "book").rsplit(".", 1)[0]
    suffix = "_uk_reflow.pdf" if layout_mode == "reflow" else "_uk.pdf"
    JOBS[job_id] = {"status": "queued", "progress": 0, "name": f"{base}{suffix}",
                    "params": {"provider": provider, "model": model, "src": src,
                               "dst": dst, "proofread": proofread,
                               "layout_mode": layout_mode}}
    # вихідник на диск: /jobs/{id}/rebuild читає його звідси (і після рестарту)
    try:
        with open(os.path.join(JOB_DIR, f"{job_id}.src.pdf"), "wb") as f:
            f.write(pdf_bytes)
    except Exception as e:
        print("src save failed:", e)
    background.add_task(_run_local_job, job_id, pdf_bytes, api_key, provider,
                        model, src, dst, proofread, file.filename, vision,
                        vision_model, cover_vision, layout_mode, cover_mode)
    return JSONResponse({
        "job_id": job_id,
        "status_url": f"/jobs/{job_id}",
        "download_url": f"/jobs/{job_id}/download",
    }, status_code=202)


@app.post("/jobs/{job_id}/rebuild")
async def rebuild_job(job_id: str, background: BackgroundTasks,
                      api_key: str = Form(...), model: str = Form(""),
                      cover_mode: str = Form("")):
    """Пересборка ГОТОВОГО перекладу чистим reflow-PDF: новий job у режимі
    reflow з ТОГО Ж вихідника. Якщо переклади вихідного job ще в пам'яті
    процесу — вони перевикористовуються (API вдруге не палиться); після
    рестарту сервера — чесний повний прогін. Відповідь — як у POST /jobs."""
    src_path = os.path.join(JOB_DIR, f"{job_id}.src.pdf")
    j = JOBS.get(job_id) or {}
    if not os.path.exists(src_path):
        raise HTTPException(404, "вихідний PDF цього job не знайдено (старий "
                                 "job або диск очищено) — завантаж книгу "
                                 "заново через POST /jobs із layout_mode=reflow")
    with open(src_path, "rb") as f:
        pdf_bytes = f.read()
    params = j.get("params", {})
    new_id = uuid.uuid4().hex[:12]
    base = (j.get("name") or "book_uk.pdf").split("_uk")[0]
    JOBS[new_id] = {"status": "queued", "progress": 0,
                    "name": f"{base}_uk_reflow.pdf", "rebuild_of": job_id,
                    "params": dict(params, layout_mode="reflow")}
    try:
        with open(os.path.join(JOB_DIR, f"{new_id}.src.pdf"), "wb") as f:
            f.write(pdf_bytes)
    except Exception as e:
        print("src save failed:", e)
    reuse = j.get("src_texts") or None
    background.add_task(_run_local_job, new_id, pdf_bytes, api_key,
                        params.get("provider", "gemini"),
                        model or params.get("model", ""),
                        params.get("src", "ru"), params.get("dst", "uk"),
                        params.get("proofread", False), j.get("name"),
                        False, "", False, "reflow", cover_mode, reuse)
    return JSONResponse({
        "job_id": new_id,
        "status_url": f"/jobs/{new_id}",
        "download_url": f"/jobs/{new_id}/download",
        "reused_translations": bool(reuse),
    }, status_code=202)


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    j = JOBS.get(job_id)
    if not j:
        return JSONResponse({"status": "unknown"}, status_code=404)
    return {k: v for k, v in j.items()
            if k not in ("path", "src_texts", "params")}


@app.get("/jobs/{job_id}/download")
def job_download(job_id: str):
    j = JOBS.get(job_id)
    if not j or j.get("status") != "done" or not j.get("path"):
        raise HTTPException(404, "ще не готово або немає такої задачі")
    return FileResponse(j["path"], media_type="application/pdf", filename=j["name"])


@app.post("/cover")
def cover(file: UploadFile = File(...), api_key: str = Form(...),
          provider: str = Form("gemini"), model: str = Form(""),
          src: str = Form("ru"), dst: str = Form("uk"),
          title: str = Form(""), author: str = Form(""),
          vision: bool = Form(False), vision_model: str = Form("")):
    """Генерує обкладинку з перекладеною назвою (PNG). vision=true -> зір вирішує
    'замінити на місці' чи 'генерувати'. title/author можна задати вручну."""
    pdf_bytes = file.file.read()
    try:
        recipe = (P.analyze_book(pdf_bytes, api_key, provider=provider,
                                 model=(vision_model or None), n=3) if vision else {})
        png = P.make_cover(pdf_bytes, api_key, provider=provider, model=model or None,
                           src=src, dst=dst, title=title or None, author=author or None,
                           recipe=recipe)
    except Exception as e:
        raise HTTPException(500, f"cover error: {e}")
    return StreamingResponse(io.BytesIO(png), media_type="image/png",
                             headers={"Content-Disposition": 'attachment; filename="cover.png"'})


@app.post("/cover/generate")
def cover_generate(file: UploadFile = File(...), api_key: str = Form(...),
                   provider: str = Form("gemini"), model: str = Form(""),
                   src: str = Form("ru"), dst: str = Form("uk")):
    """Обкладинка З НУЛЯ (чиста типографіка на палітрі оригіналу).
    ТІЛЬКИ за явним запитом користувача — в автоматичний ланцюжок обкладинки
    генерація НЕ вбудована (правило 6). Працює лише з Gemini (зір)."""
    if provider != "gemini":
        raise HTTPException(400, "cover/generate працює лише з provider=gemini")
    pdf_bytes = file.file.read()
    try:
        png = P.render_cover_png(pdf_bytes)
        blocks, _reasons = P.read_cover_blocks(png, api_key, glossary=None,
                                               src=src, dst=dst,
                                               model=model or None,
                                               pdf_bytes=pdf_bytes)
        out = P.generate_cover(blocks, png, 0, 0)
    except P._ClientError as e:
        # 4xx від LLM (ключ/модель) — помилка користувача, без ретраїв (правило 7)
        raise HTTPException(400, f"cover generate error: {e}")
    except Exception as e:
        print("cover generate error:", _safe_err(e, 500))  # повний текст — лише в лог
        raise HTTPException(500, f"cover generate error: {type(e).__name__}")
    return StreamingResponse(io.BytesIO(out), media_type="image/png",
                             headers={"Content-Disposition":
                                      'attachment; filename="cover_generated.png"'})


@app.post("/preview")
def preview(file: UploadFile = File(...), api_key: str = Form(...),
            provider: str = Form("gemini"), model: str = Form(""),
            src: str = Form("ru"), dst: str = Form("uk"),
            proofread: bool = Form(False)):
    pdf_bytes = file.file.read()
    pages = P.extract_blocks(pdf_bytes, ocr_lang=P._LANG_OCR.get(src, "rus"))
    flat = [(b["id"], b["text"]) for blocks in pages for b in blocks]
    tr = P.translate_blocks([t for _, t in flat], api_key, provider=provider,
                            model=model or None, src=src, dst=dst, proofread=proofread)
    tmap = {flat[i][0]: tr[i] for i in range(len(flat))}
    return JSONResponse({"blocks": [
        {"id": b["id"], "page": b["page"], "size": b["size"],
         "original": b["text"], "translated": tmap.get(b["id"], "")}
        for blocks in pages for b in blocks]})


# ----------- ФОНОВА ЧЕРГА із Supabase (для інтеграції з Lovable) -----------
def _run_job(pdf_bytes, translation_id, api_key, provider, model, src, dst,
             proofread, filename):
    try:
        supa_update(translation_id, status="processing", progress=1)

        def cb(done, total):
            supa_update(translation_id,
                        progress=max(1, min(95, int(done / max(total, 1) * 95))))

        pages = P.extract_blocks(pdf_bytes, ocr_lang=P._LANG_OCR.get(src, "rus"))
        if P.looks_scanned(pages):
            out = P.translate_scanned_pdf(pdf_bytes, api_key, provider=provider,
                                          model=model or None, src=src, dst=dst,
                                          proofread=proofread, progress_cb=cb)
        else:
            flat = [(b["id"], b["text"]) for blocks in pages for b in blocks]
            tr = P.translate_blocks([t for _, t in flat], api_key, provider=provider,
                                    model=model or None, src=src, dst=dst,
                                    proofread=proofread, progress_cb=cb)
            tmap = {flat[i][0]: tr[i] for i in range(len(flat))}
            out = P.build_pdf(pdf_bytes, pages, tmap)
        supa_update(translation_id, progress=97)
        base = (filename or "book").rsplit(".", 1)[0]
        url = supa_upload_result(f"{translation_id}/{base}_uk.pdf", out)
        supa_update(translation_id, status="done", progress=100, result_url=url)
    except Exception as e:
        traceback.print_exc()
        supa_update(translation_id, status="error", error=str(e)[:500])


@app.post("/translate-pdf")
async def translate_async(background: BackgroundTasks,
                          file: UploadFile = File(...), translation_id: str = Form(...),
                          api_key: str = Form(...),
                          provider: str = Form("gemini"), model: str = Form(""),
                          src: str = Form("ru"), dst: str = Form("uk"),
                          proofread: bool = Form(False)):
    if not (SUPA_URL and SUPA_KEY):
        raise HTTPException(500, "Supabase не налаштований. Для тесту: /jobs")
    pdf_bytes = await file.read()
    background.add_task(_run_job, pdf_bytes, translation_id, api_key, provider,
                        model, src, dst, proofread, file.filename)
    return JSONResponse({"status": "started", "translation_id": translation_id},
                        status_code=202)
