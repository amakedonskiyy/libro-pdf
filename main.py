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
import time
import json
import uuid
import queue
import hashlib
import threading
import traceback
import requests
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException, Request
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


ENGINE_VERSION = "2026-06-30-no-hallucination-v1"

# --- захист від зависань: потолок часу + детект «немає прогресу» ---
# (переоприділяється env-змінними; у тестах ставимо малі значення)
JOB_TOTAL_LIMIT = int(os.environ.get("LIBRO_JOB_TOTAL_LIMIT", 60 * 60))   # 60 хв (фолбек)
JOB_STALL_LIMIT = int(os.environ.get("LIBRO_JOB_STALL_LIMIT", 8 * 60))    # 8 хв без прогресу
JOB_WATCH_INTERVAL = float(os.environ.get("LIBRO_JOB_WATCH_INTERVAL", 5))  # такт watchdog
# --- адаптивний потолок часу: масштаб від числа сторінок, з підлогою/стелею ---
# (велику книгу фіксований ліміт рубив на ~98% попри прогрес — звідси масштаб)
JOB_TIME_PER_100P = int(os.environ.get("LIBRO_JOB_MIN_PER_100P", 20)) * 60   # сек на 100 стор.
JOB_TIME_FLOOR = int(os.environ.get("LIBRO_JOB_TIME_FLOOR", 1800))           # підлога 30 хв
JOB_TIME_CAP = int(os.environ.get("LIBRO_JOB_TIME_CAP", 4 * 60 * 60))        # стеля 4 год
# --- серверний потолок розміру завантаження (захист контейнера) ---
MAX_UPLOAD_BYTES = int(os.environ.get("LIBRO_MAX_UPLOAD_MB", 500)) * 1024 * 1024
_ACTIVE = ("queued", "processing")        # статуси «задача жива» (для дублів)

# --- пул воркерів: паралельні переклади з лімітом (захист контейнера) ---
# MAX_PARALLEL_JOBS=1 -> строго послідовно (як було, прод не ламається);
# =2 -> дві книги одночасно (кожна на своєму ключі — ключ передається у
# задачу, стан не ділиться). Більше за ліміт -> чекають у черзі.
MAX_PARALLEL_JOBS = max(1, int(os.environ.get("MAX_PARALLEL_JOBS", 2)))

# --- Уровень 2: порог доли «подозрительных на кашу» строк для флага качества ---
QUALITY_GARBLE_PCT = float(os.environ.get("QUALITY_GARBLE_PCT", 5.0))

# --- чекпойнт/resume: добивать книгу за несколько подходов несмотря на 503 ---
AUTO_RESUME_MAX = int(os.environ.get("AUTO_RESUME_MAX", 5))      # 0 = только вручную
AUTO_RESUME_DELAY = float(os.environ.get("AUTO_RESUME_DELAY", 90))  # пауза перед авто-resume


def _job_time_budget(pdf_bytes):
    """Адаптивний потолок часу: масштаб від числа сторінок, підлога/стеля.
    Сторінки не зчитались -> старий фіксований JOB_TOTAL_LIMIT (сумісність)."""
    try:
        import fitz
        d = fitz.open(stream=pdf_bytes, filetype="pdf")
        n = d.page_count
        d.close()
        budget = JOB_TIME_FLOOR + (JOB_TIME_PER_100P * n) // 100
        return max(JOB_TIME_FLOOR, min(JOB_TIME_CAP, budget))
    except Exception:
        return JOB_TOTAL_LIMIT


def _ckpt_path(job_id):
    return os.path.join(JOB_DIR, f"{job_id}.ckpt.json")


def _ckpt_load(job_id):
    """{str(block_id): перевод} уже готовых блоков (состояние resume)."""
    try:
        with open(_ckpt_path(job_id), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _ckpt_save(job_id, data):
    """Атомарно (tmp+rename), чтобы обрыв во время записи не бил чекпойнт."""
    try:
        p = _ckpt_path(job_id)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception as e:
        print(f"ckpt save failed: {e}")


def _ckpt_clear(job_id):
    for ext in (".ckpt.json", ".ckpt.json.tmp"):
        try:
            os.remove(os.path.join(JOB_DIR, f"{job_id}{ext}"))
        except Exception:
            pass


def _translate_with_ckpt(job_id, ids, texts, api_key, provider, model, src, dst,
                         proofread, glossary, abort_cb):
    """Перевод texts (с глобальными id) с ЧЕКПОЙНТОМ: после каждого батча
    готовое сохраняется на диск по job_id. При обрыве (503-шторм -> timeout)
    уже переведённое НЕ теряется; resume перезапускает _run_local_job, ckpt-
    файл подхватывается и переводится только остаток. Возвращает {id: перевод}
    для ВСЕХ (готовые из чекпойнта + новые). Перевод-логику не меняет."""
    ckpt = _ckpt_load(job_id)                          # {str(id): перевод}
    total = len(ids)
    JOBS[job_id]["total"] = total
    need = [k for k in range(total) if str(ids[k]) not in ckpt]
    JOBS[job_id]["translated"] = total - len(need)
    if need:
        need_ids = [str(ids[k]) for k in need]
        need_texts = [texts[k] for k in need]
        base = len(ckpt)

        def prog(done, _tw):
            JOBS[job_id]["last_progress"] = time.time()   # watchdog: прогресс есть
            n = min(total, base + done)
            JOBS[job_id]["progress"] = max(1, min(99, int(n / max(total, 1) * 99)))

        def ckpt_cb(localmap):                            # {local_idx: перевод}
            for li, tr in localmap.items():
                ckpt[need_ids[li]] = tr
            _ckpt_save(job_id, ckpt)
            JOBS[job_id]["translated"] = len(ckpt)

        new = P.translate_blocks(need_texts, api_key, provider=provider,
                                 model=model or None, src=src, dst=dst,
                                 proofread=proofread, progress_cb=prog,
                                 glossary=glossary, abort_cb=abort_cb,
                                 checkpoint_cb=ckpt_cb)
        for li, tr in enumerate(new):
            ckpt[need_ids[li]] = tr
        _ckpt_save(job_id, ckpt)
        JOBS[job_id]["translated"] = len(ckpt)
    return {ids[k]: ckpt.get(str(ids[k]), texts[k]) for k in range(total)}


def _maybe_auto_resume(job_id):
    """Опционально: после stalled — авто-resume до AUTO_RESUME_MAX раз с паузой
    (даём 503-шторму утихнуть). Ключ берём из памяти задачи (в ответах API он
    отфильтрован, правило 10). 0 -> только ручной resume."""
    if AUTO_RESUME_MAX <= 0:
        return
    j = JOBS.get(job_id) or {}
    if j.get("resume_attempts", 0) >= AUTO_RESUME_MAX or not j.get("_api_key"):
        return
    def later():
        time.sleep(AUTO_RESUME_DELAY)
        jj = JOBS.get(job_id)
        if not jj or jj.get("status") != "stalled":
            return                                  # уже возобновили вручную / готово
        jj["resume_attempts"] = jj.get("resume_attempts", 0) + 1
        print(f"[job {job_id}] auto-resume #{jj['resume_attempts']}", flush=True)
        _resume_enqueue(job_id, jj["_api_key"], jj.get("params", {}).get("model", ""))
    threading.Thread(target=later, daemon=True).start()


def _attach_quality(job_id, out):
    """УРОВЕНЬ 2: поверх готового PDF считаем кашу и кладём честный флаг.
    Перевод НЕ блокируется и НЕ падает — книга отдаётся как есть. Любая
    ошибка сканера -> просто без флага."""
    try:
        q = P.quality_scan(out, threshold_pct=QUALITY_GARBLE_PCT)
        JOBS[job_id]["quality_warning"] = q
        if q["has_issues"]:
            print(f"[job {job_id}] quality_warning: {q['garbled_percent']}% "
                  f"підозрілих рядків, сторінки {q['suspect_pages']}", flush=True)
    except Exception as e:
        print(f"[job {job_id}] quality_scan skipped: {e}", flush=True)


def _safe_err(e, limit=200):
    """Текст помилки для статусів/логів: ключі з URL-ів вирізаються (правило 10)."""
    return re.sub(r"key=[A-Za-z0-9_\-\.]+", "key=***", str(e))[:limit]


# --- ключ Gemini: з СЕРВЕРА (env), користувач нічого не вводить ---
# Слот паралелі визначає воркер: воркер 0 -> GEMINI_API_KEY, воркер 1 ->
# GEMINI_API_KEY_2 (якщо заданий). Явний api_key із запиту має пріоритет
# (обратна сумісність зі старим фронтом), але працює і БЕЗ нього.
_worker_slot = threading.local()


def _resolve_key(api_key, provider="gemini"):
    """Повертає робочий ключ: явний api_key із запиту (якщо непорожній) ->
    інакше серверний env-ключ. Для gemini слот воркера обирає GEMINI_API_KEY
    або GEMINI_API_KEY_2. Правило 10: ключ нікуди не логуємо/не віддаємо."""
    if api_key and api_key.strip():
        return api_key.strip()                 # явний із запиту (обратна сумісність)
    if provider == "groq":
        return os.environ.get("GROQ_API_KEY", "")
    slot = getattr(_worker_slot, "slot", 0)
    if slot >= 1 and os.environ.get("GEMINI_API_KEY_2"):
        return os.environ["GEMINI_API_KEY_2"]  # правий слот паралелі
    return os.environ.get("GEMINI_API_KEY", "")


def _ping_provider(api_key, provider, model):
    """Короткий пінг ключа перед важкою роботою: 4xx (поганий ключ/модель/
    доступ) -> повертає текст помилки -> задача падає одразу (правило 7), а не
    після сотні висячих запитів. Мережеві/5xx тут НЕ валять (далі ретраї)."""
    try:
        P._llm_call(provider, api_key, model or P._default_model(provider),
                    "You are a translator.", "ping")
        return None
    except P._ClientError as e:
        return str(e)
    except Exception:
        return None


def _find_active_job(file_hash, layout_mode):
    """job_id живої задачі по тому ж файлу (хеш вмісту) і режиму — щоб не
    плодити дублі на повторному POST (перезавантаження сторінки)."""
    for jid, j in JOBS.items():
        if (j.get("file_hash") == file_hash
                and (j.get("params") or {}).get("layout_mode") == layout_mode
                and j.get("status") in _ACTIVE):
            return jid
    return None


def _start_watchdog(job_id):
    """Сторожовий потік: якщо немає прогресу > JOB_STALL_LIMIT АБО загальний
    час > JOB_TOTAL_LIMIT — помічає задачу timeout і просить воркер спинитись
    (cancel+abort_reason). Воркер припиняє палити API на найближчій межі
    батчу. Демон, гине разом із процесом / по завершенні задачі."""
    def run():
        while True:
            time.sleep(JOB_WATCH_INTERVAL)
            j = JOBS.get(job_id)
            if not j or j.get("status") not in _ACTIVE or j.get("cancel"):
                return
            now = time.time()
            total = now - j.get("started", now)
            stall = now - j.get("last_progress", now)
            lim = j.get("total_limit", JOB_TOTAL_LIMIT)
            if total > lim or stall > JOB_STALL_LIMIT:
                why = ("перевищено час задачі" if total > lim
                       else "переклад не рухається")
                j["abort_reason"] = "timeout"
                j["cancel"] = True
                j["status"] = "timeout"
                j["error"] = (f"timeout: прогін перервано ({why}) — "
                              "перевірте ключ і мережу")
                return
    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- worker pool
_JOB_QUEUE = queue.Queue()
_WORKERS_STARTED = False
_WORKERS_LOCK = threading.Lock()


def _worker_loop(slot=0):
    """Воркер: бере задачу з черги і виконує _run_local_job. slot — індекс
    слота паралелі (0/1) для вибору серверного ключа (GEMINI_API_KEY /
    GEMINI_API_KEY_2). Падіння однієї задачі НЕ валить воркер (ловимо) і не
    чіпає інші — кожна ізольована (свій JOBS[id], свій watchdog, своя відміна)."""
    _worker_slot.slot = slot
    while True:
        args = _JOB_QUEUE.get()
        try:
            _run_local_job(*args)
        except Exception as e:                # _run_local_job сам ловить усе;
            traceback.print_exc()             # це лише страховка від падіння воркера
            jid = args[0] if args else None
            if jid in JOBS and JOBS[jid].get("status") in _ACTIVE:
                JOBS[jid].update(status="error", error=_safe_err(e, 500))
        finally:
            _JOB_QUEUE.task_done()


def _ensure_workers():
    """Лінивий старт пулу: MAX_PARALLEL_JOBS воркер-потоків (один раз)."""
    global _WORKERS_STARTED
    with _WORKERS_LOCK:
        if _WORKERS_STARTED:
            return
        for i in range(MAX_PARALLEL_JOBS):
            threading.Thread(target=_worker_loop, args=(i,), daemon=True).start()
        _WORKERS_STARTED = True
        print(f"job pool: {MAX_PARALLEL_JOBS} worker(s) started", flush=True)


def _enqueue_job(*args):
    """Поставити задачу в чергу. Воркер візьме її, щойно звільниться слот
    (понад ліміт — чекає у статусі 'queued', не запускається одразу)."""
    _ensure_workers()
    _JOB_QUEUE.put(args)


# ---------------------------------------------------------------- endpoints
@app.get("/health")
def health():
    return {"ok": True, "version": ENGINE_VERSION,
            "supabase_configured": bool(SUPA_URL and SUPA_KEY)}


@app.post("/translate-pdf-sync")
def translate_sync(file: UploadFile = File(...), api_key: str = Form(""),
                   provider: str = Form("gemini"), model: str = Form(""),
                   src: str = Form("ru"), dst: str = Form("uk"),
                   proofread: bool = Form(False)):
    """Синхронно: PDF -> готовий PDF. ТІЛЬКИ дрібні файли (~5 стор.)."""
    pdf_bytes = file.file.read()
    api_key = _resolve_key(api_key, provider)        # явний -> інакше серверний env
    if not api_key:
        raise HTTPException(400, "Gemini-ключ не налаштований (env GEMINI_API_KEY)")
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

    def abort_cb():
        """Між батчами: cancel користувача або timeout/stall від watchdog ->
        _Aborted, переклад спиняється і API більше не палиться."""
        j = JOBS.get(job_id, {})
        if j.get("cancel"):
            if j.get("abort_reason") == "timeout":
                raise P._Aborted("timeout", j.get("error")
                                 or "timeout: прогін перервано")
            raise P._Aborted("cancelled", "задачу скасовано користувачем")

    try:
        now = time.time()
        JOBS[job_id].update(status="processing", progress=1,
                            started=now, last_progress=now)
        JOBS[job_id]["total_limit"] = _job_time_budget(pdf_bytes)  # адаптивний потолок часу
        if AUTO_RESUME_MAX > 0:               # зберігаємо ВИХІДНИЙ api_key (часто
            JOBS[job_id]["_api_key"] = api_key  # порожній у env-режимі) для авто-resume
        _start_watchdog(job_id)
        log(f"start [{ENGINE_VERSION}]: {len(pdf_bytes)//1024}KB, provider={provider}, model={model!r}, src={src}, vision={vision}, cover_vision={cover_vision}, layout={layout_mode}")

        abort_cb()                            # відмінили ще в черзі -> не палимо ключ
        # ключ: явний із запиту -> інакше серверний env (слот воркера). Резолвимо
        # ТУТ (у воркері), щоб слот паралелі обрав GEMINI_API_KEY / _2.
        api_key = _resolve_key(api_key, provider)
        if not api_key:
            log("no key: ні api_key у запиті, ні GEMINI_API_KEY на сервері")
            JOBS[job_id].update(status="error",
                error="Gemini-ключ не налаштований на сервері (env GEMINI_API_KEY)")
            return
        kerr = _ping_provider(api_key, provider, model)
        if kerr:
            log(f"key ping failed: {_safe_err(kerr)}")
            JOBS[job_id].update(status="error",
                                error="перевірте API-ключ: " + _safe_err(kerr))
            return

        def cb(done, total):
            pct = max(1, min(99, int(done / max(total, 1) * 99)))
            if pct != JOBS[job_id].get("progress"):
                JOBS[job_id]["last_progress"] = time.time()   # прогрес рухається
            JOBS[job_id]["progress"] = pct

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
                ck0 = len(_ckpt_load(job_id))
                log(f"translate_blocks: {len(need)} elements "
                    f"({ck0} из чекпойнта, перевожу остаток)...")
                rmap = _translate_with_ckpt(job_id, need,
                                            [flow[i]["text"] for i in need],
                                            api_key, provider, model, src, dst,
                                            proofread, glossary, abort_cb)
                for i in need:
                    flow[i]["uk"] = rmap[i]
            log("reflow: title page meta...")
            meta = P.reflow_title_meta(pdf_bytes, api_key, glossary, src, dst,
                                       model or None)
            cover_png = _reflow_cover_png(job_id, pdf_bytes, api_key, provider,
                                          model, src, dst, glossary,
                                          cover_mode, log)
            abort_cb()                        # не зберігати скасовану/таймаут
            log("build_pdf_reflow...")
            out = P.build_pdf_reflow(pdf_bytes, flow, meta, cover_png=cover_png)
            JOBS[job_id]["src_texts"] = {flow[i]["text"]: flow[i].get("uk", "")
                                         for i in good}
            _attach_quality(job_id, out)      # Уровень 2: флаг качества (не блокирует)
            abort_cb()
            log(f"done: {len(out)//1024}KB")
            path = os.path.join(JOB_DIR, f"{job_id}.pdf")
            with open(path, "wb") as f:
                f.write(out)
            JOBS[job_id].update(status="done", progress=100, path=path)
            _ckpt_clear(job_id)               # книга готова -> чекпойнт не нужен
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
                                          proofread=proofread, progress_cb=cb,
                                          abort_cb=abort_cb)
        else:
            flat = [(b["id"], b["text"]) for blk in pages for b in blk]
            log("building glossary (term consistency)...")
            glossary = P.build_glossary([t for _, t in flat], api_key,
                                        provider=provider, model=model or None,
                                        src=src, dst=dst)
            log(f"glossary: {len(glossary)} terms")
            ids = [bid for bid, _ in flat]
            texts = [t for _, t in flat]
            ck0 = len(_ckpt_load(job_id))
            log(f"translate_blocks: {len(flat)} blocks "
                f"({ck0} из чекпойнта, перевожу остаток)...")
            tmap = _translate_with_ckpt(job_id, ids, texts, api_key, provider,
                                        model, src, dst, proofread, glossary,
                                        abort_cb)
            abort_cb()                        # не будувати скасований PDF
            log("build_pdf...")
            # переклади лишаються в пам'яті процесу: /jobs/{id}/rebuild
            # перевикористає їх і не палитиме API вдруге
            JOBS[job_id]["src_texts"] = {texts[i]: tmap[ids[i]]
                                         for i in range(len(ids))}
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
        _attach_quality(job_id, out)          # Уровень 2: флаг качества (не блокирует)
        abort_cb()                            # не зберігати скасовану/таймаут
        log(f"done: {len(out)//1024}KB")
        path = os.path.join(JOB_DIR, f"{job_id}.pdf")
        with open(path, "wb") as f:
            f.write(out)
        JOBS[job_id].update(status="done", progress=100, path=path)
        _ckpt_clear(job_id)                   # книга готова -> чекпойнт не нужен
    except P._Aborted as ab:
        log(f"ABORTED [{ab.status}]: {ab.msg}")
        if ab.status == "cancelled":          # юзер відмінив -> прогрес не тримаємо
            _ckpt_clear(job_id)
            JOBS[job_id].update(status="cancelled", error=ab.msg, progress=0)
        else:                                 # timeout (503-шторм)
            ck = _ckpt_load(job_id)
            if ck:                            # є прогрес -> STALLED, не губимо
                JOBS[job_id].update(
                    status="stalled", translated=len(ck),
                    error="503-шторм: переклад призупинено, прогрес збережено — "
                          "виклич POST /jobs/{id}/resume пізніше")
                log(f"STALLED: {len(ck)} блоків збережено; resume дотисне лишок")
                _maybe_auto_resume(job_id)
            else:
                JOBS[job_id].update(status="timeout", error=ab.msg, progress=0)
    except Exception as e:
        traceback.print_exc()
        log(f"ERROR: {_safe_err(e)}")
        JOBS[job_id].update(status="error", error=_safe_err(e, 500))


@app.post("/jobs")
async def create_job(request: Request,
                     file: UploadFile = File(...), api_key: str = Form(""),
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
    cl = request.headers.get("content-length")   # дешевий чек ДО читання тіла в пам'ять
    if cl and int(cl) > int(MAX_UPLOAD_BYTES * 1.05):
        raise HTTPException(413, f"Файл завеликий (ліміт {MAX_UPLOAD_BYTES // (1024*1024)} МБ)")
    pdf_bytes = await file.read()
    if len(pdf_bytes) > MAX_UPLOAD_BYTES:        # реальний розмір (Content-Length міг збрехати)
        raise HTTPException(413, f"Файл завеликий (ліміт {MAX_UPLOAD_BYTES // (1024*1024)} МБ)")
    # захист від дублів: той самий файл (хеш вмісту) у тому ж режимі вже
    # обробляється -> не плодимо другу задачу, повертаємо наявну (перезавантаження
    # сторінки / подвійний клік не запускають повторний прогін і не палять API)
    file_hash = hashlib.sha256(pdf_bytes).hexdigest()
    dup = _find_active_job(file_hash, layout_mode)
    if dup:
        return JSONResponse({
            "job_id": dup, "status_url": f"/jobs/{dup}",
            "download_url": f"/jobs/{dup}/download", "reused": True,
            "note": "цей файл уже перекладається — повертаю наявну задачу",
        }, status_code=200)
    job_id = uuid.uuid4().hex[:12]
    base = (file.filename or "book").rsplit(".", 1)[0]
    suffix = "_uk_reflow.pdf" if layout_mode == "reflow" else "_uk.pdf"
    JOBS[job_id] = {"status": "queued", "progress": 0, "name": f"{base}{suffix}",
                    "file_hash": file_hash,
                    "params": {"provider": provider, "model": model, "src": src,
                               "dst": dst, "proofread": proofread,
                               "layout_mode": layout_mode, "cover_vision": cover_vision,
                               "cover_mode": cover_mode, "vision": vision,
                               "vision_model": vision_model}}
    _ckpt_clear(job_id)                       # свіжий job -> жодного старого чекпойнту
    # вихідник на диск: /jobs/{id}/rebuild читає його звідси (і після рестарту)
    try:
        with open(os.path.join(JOB_DIR, f"{job_id}.src.pdf"), "wb") as f:
            f.write(pdf_bytes)
    except Exception as e:
        print("src save failed:", e)
    _enqueue_job(job_id, pdf_bytes, api_key, provider,
                 model, src, dst, proofread, file.filename, vision,
                 vision_model, cover_vision, layout_mode, cover_mode)
    return JSONResponse({
        "job_id": job_id,
        "status_url": f"/jobs/{job_id}",
        "download_url": f"/jobs/{job_id}/download",
    }, status_code=202)


@app.post("/jobs/{job_id}/rebuild")
async def rebuild_job(job_id: str,
                      api_key: str = Form(""), model: str = Form(""),
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
    _enqueue_job(new_id, pdf_bytes, api_key,
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


def _resume_enqueue(job_id, api_key, model_override=""):
    """Перезапуск _run_local_job для ТОГО Ж job_id: чекпойнт на диску
    подхватится сам, переведётся только остаток. Источник — сохранённый
    {job_id}.src.pdf, параметры — из JOBS[job_id]['params']."""
    j = JOBS.get(job_id) or {}
    src_path = os.path.join(JOB_DIR, f"{job_id}.src.pdf")
    if not os.path.exists(src_path):
        return False
    with open(src_path, "rb") as f:
        pdf_bytes = f.read()
    p = j.get("params", {})
    j["status"] = "queued"
    j["cancel"] = False
    j.pop("abort_reason", None)
    _enqueue_job(job_id, pdf_bytes, api_key, p.get("provider", "gemini"),
                 model_override or p.get("model", ""), p.get("src", "ru"),
                 p.get("dst", "uk"), p.get("proofread", False), j.get("name"),
                 p.get("vision", False), p.get("vision_model", ""),
                 p.get("cover_vision", False), p.get("layout_mode", "preserve"),
                 p.get("cover_mode", ""))
    return True


@app.post("/jobs/{job_id}/resume")
def resume_job(job_id: str, api_key: str = Form(""), model: str = Form("")):
    """Продовжити перерваний (stalled/timeout/error) переклад з ПЕРШОГО
    неперекладеного блоку. Уже перекладене лежить у чекпойнті на диску — API
    на нього вдруге НЕ палиться. Якщо Gemini ще в 503 — знову stalled, прогрес
    знову збережено, можна дёрнути resume пізніше. Відповідь — як у POST /jobs."""
    j = JOBS.get(job_id)
    if not j:
        return JSONResponse({"status": "unknown"}, status_code=404)
    if j.get("status") in ("queued", "processing", "cancelling"):
        return {"status": j["status"], "note": "вже виконується"}
    if j.get("status") == "done":
        return {"status": "done", "note": "вже готово"}
    if not os.path.exists(os.path.join(JOB_DIR, f"{job_id}.src.pdf")):
        raise HTTPException(404, "вихідний PDF не знайдено (диск очищено) — "
                                 "завантаж книгу заново через POST /jobs")
    j["resume_attempts"] = 0                  # ручной resume сбрасывает авто-счётчик
    if not _resume_enqueue(job_id, api_key, model):
        raise HTTPException(404, "не вдалося відновити — завантаж книгу заново")
    return JSONResponse({
        "job_id": job_id, "status_url": f"/jobs/{job_id}",
        "download_url": f"/jobs/{job_id}/download", "resumed": True,
        "translated": j.get("translated", 0), "total": j.get("total", 0),
    }, status_code=202)


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    """Скасувати задачу: воркер припиняє обробку і перестає палити API на
    найближчій межі батчу. Фронт має слати це при закритті/перезавантаженні
    сторінки, щоб задача не молотила у фоні. Повертає поточний статус
    ('cancelling', доки воркер не дійшов до межі; потім /jobs/{id} = 'cancelled')."""
    j = JOBS.get(job_id)
    if not j:
        return JSONResponse({"status": "unknown"}, status_code=404)
    if j.get("status") not in _ACTIVE:
        return {"status": j.get("status"), "final": True}
    j["cancel"] = True
    j["abort_reason"] = "cancelled"
    j["status"] = "cancelling"
    return {"status": "cancelling"}


@app.get("/jobs")
def list_jobs():
    """Список усіх задач із прогресом — щоб фронт показав кілька книг разом
    (наприклад, дві паралельні). Без службових/секретних полів (ключ у JOBS
    не зберігається — правило 10). active = скільки задач живі, queue_len —
    скільки чекає на вільний слот пулу."""
    jobs = [{"job_id": jid, "status": j.get("status"),
             "progress": j.get("progress", 0), "name": j.get("name"),
             "cover_status": j.get("cover_status"),
             "quality_issues": bool((j.get("quality_warning") or {}).get("has_issues")),
             "error": j.get("error")}
            for jid, j in JOBS.items()]
    active = sum(1 for j in JOBS.values() if j.get("status") in _ACTIVE)
    return {"max_parallel": MAX_PARALLEL_JOBS, "active": active,
            "queue_len": _JOB_QUEUE.qsize(), "jobs": jobs}


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    j = JOBS.get(job_id)
    if not j:
        return JSONResponse({"status": "unknown"}, status_code=404)
    return {k: v for k, v in j.items()
            if k not in ("path", "src_texts", "params", "file_hash",
                         "started", "last_progress", "cancel", "abort_reason",
                         "_api_key")}


@app.get("/jobs/{job_id}/download")
def job_download(job_id: str):
    j = JOBS.get(job_id)
    if not j or j.get("status") != "done" or not j.get("path"):
        raise HTTPException(404, "ще не готово або немає такої задачі")
    return FileResponse(j["path"], media_type="application/pdf", filename=j["name"])


@app.post("/cover")
def cover(file: UploadFile = File(...), api_key: str = Form(""),
          provider: str = Form("gemini"), model: str = Form(""),
          src: str = Form("ru"), dst: str = Form("uk"),
          title: str = Form(""), author: str = Form(""),
          vision: bool = Form(False), vision_model: str = Form("")):
    """Генерує обкладинку з перекладеною назвою (PNG). vision=true -> зір вирішує
    'замінити на місці' чи 'генерувати'. title/author можна задати вручну."""
    pdf_bytes = file.file.read()
    api_key = _resolve_key(api_key, provider)
    if not api_key:
        raise HTTPException(400, "Gemini-ключ не налаштований (env GEMINI_API_KEY)")
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
def cover_generate(file: UploadFile = File(...), api_key: str = Form(""),
                   provider: str = Form("gemini"), model: str = Form(""),
                   src: str = Form("ru"), dst: str = Form("uk")):
    """Обкладинка З НУЛЯ (чиста типографіка на палітрі оригіналу).
    ТІЛЬКИ за явним запитом користувача — в автоматичний ланцюжок обкладинки
    генерація НЕ вбудована (правило 6). Працює лише з Gemini (зір)."""
    if provider != "gemini":
        raise HTTPException(400, "cover/generate працює лише з provider=gemini")
    pdf_bytes = file.file.read()
    api_key = _resolve_key(api_key, provider)
    if not api_key:
        raise HTTPException(400, "Gemini-ключ не налаштований (env GEMINI_API_KEY)")
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
def preview(file: UploadFile = File(...), api_key: str = Form(""),
            provider: str = Form("gemini"), model: str = Form(""),
            src: str = Form("ru"), dst: str = Form("uk"),
            proofread: bool = Form(False)):
    pdf_bytes = file.file.read()
    api_key = _resolve_key(api_key, provider)
    if not api_key:
        raise HTTPException(400, "Gemini-ключ не налаштований (env GEMINI_API_KEY)")
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
        api_key = _resolve_key(api_key, provider)    # явний -> інакше серверний env
        if not api_key:
            supa_update(translation_id, status="error",
                        error="Gemini-ключ не налаштований (env GEMINI_API_KEY)")
            return
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
                          api_key: str = Form(""),
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
