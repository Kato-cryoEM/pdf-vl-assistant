"""確認用スタンドアロン: PDF 1ページ目をVLに渡して文境界を検証。
pysbd の分割と VL の分割を並べて表示する。SSE で進捗ライブ配信。
起動: python check.py  → http://<host>:8081/
"""
import asyncio
import base64
import io
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import AsyncIterator

import fitz
import httpx
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse
from PIL import Image
import pysbd


LLM_URL = os.environ.get("LLM_URL", "http://127.0.0.1:8004")  # llama-server (OpenAI 互換)
DEFAULT_MODEL = "qwen3.8-flash-next"
PAGE_RENDER_DPI = 150
MAX_IMG_EDGE = 1600
TMP_DIR = Path("/tmp/boundary_check")
TMP_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Boundary Check")

_SEG_EN = pysbd.Segmenter(language="en", clean=False, char_span=True)
_SEG_JA = pysbd.Segmenter(language="ja", clean=False, char_span=True)
_EXTRA_ABBRS = ['Fig', 'Figs', 'Eq', 'Eqs', 'Sec', 'Secs', 'Ref', 'Refs',
                'Vol', 'Vols', 'No', 'Nos', 'pp', 'Ch', 'Chs', 'App',
                'Prop', 'Thm', 'Lem', 'Def', 'Cor', 'approx', 'ca']
_ABBR_RE = re.compile(r'\b(' + '|'.join(_EXTRA_ABBRS) + r')\.')
_INITIAL_RE = re.compile(r'\b([A-Z])\.(?=\s+[a-z])')

JOBS: dict[str, dict] = {}


def _detect_lang(text):
    for ch in text[:200]:
        if '぀' <= ch <= 'ヿ' or '一' <= ch <= '鿿':
            return "ja"
    return "en"


def _protect_abbrs(text):
    t = _ABBR_RE.sub(lambda m: m.group(1) + '\x00', text)
    t = _INITIAL_RE.sub(lambda m: m.group(1) + '\x00', t)
    return t


def pysbd_ranges(text):
    if not text.strip():
        return []
    protected = _protect_abbrs(text)
    seg = _SEG_JA if _detect_lang(text) == "ja" else _SEG_EN
    try:
        spans = seg.segment(protected)
    except Exception:
        return [(0, len(text))]
    result = []
    for sp in spans:
        s, e = getattr(sp, "start", None), getattr(sp, "end", None)
        if s is None or e is None:
            continue
        if text[s:e].strip():
            result.append([s, e])
    return result


def render_page_image(page):
    zoom = PAGE_RENDER_DPI / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def pil_to_b64(img):
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    m = max(w, h)
    if m > MAX_IMG_EDGE:
        s = MAX_IMG_EDGE / m
        img = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode()


def extract_paragraphs(page):
    """段落の組み立ては server.py と同じ処理を使う。

    PyMuPDF のブロック境界だけに頼ると、行送りの広い投稿原稿 (1.5〜2 行送り) では
    1 行ごとにブロックが分かれ、段落が 1 行ずつ分断されてしまう。行の座標・行送り・
    書体・文の続き方から段落を組み立て直す。
    """
    import server as _srv

    d = page.get_text("dict")
    all_blocks = d.get("blocks", [])
    all_lefts = []
    for block in all_blocks:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            txt = "".join((s.get("text") or "") for s in line.get("spans", []))
            if not txt.strip():
                continue
            if re.match(r"^\s*\d{1,4}\s*$", txt):
                continue
            lb = line.get("bbox") or (0, 0, 0, 0)
            all_lefts.append(lb[0])
    body_left_x = sorted(all_lefts)[len(all_lefts) // 2] if all_lefts else 0.0

    page_lines = []
    for pmb_idx, block in enumerate(all_blocks):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            lb = line.get("bbox") or (0, 0, 0, 0)
            txt = "".join((s.get("text") or "") for s in line.get("spans", []))
            if not txt.strip():
                continue
            # 行番号 (本文左端より明確に左にある数字だけの行) は除外
            if lb[0] < body_left_x - 15 and re.match(r"^\s*\d{1,4}\s*$", txt):
                continue
            spans = [s for s in line.get("spans", []) if (s.get("text") or "").strip()]
            sizes = [float(s.get("size") or 10) for s in spans]
            total_chars = sum(len(s.get("text") or "") for s in spans)
            bold_chars = sum(len(s.get("text") or "") for s in spans
                             if "bold" in (s.get("font") or "").lower())
            page_lines.append({
                "text": txt,
                "bbox": tuple(lb),
                "size": sum(sizes) / len(sizes) if sizes else 10.0,
                "bold": total_chars > 0 and bold_chars / total_chars > 0.6,
                "italic": False,
                "block_idx": pmb_idx,
            })

    paras = []
    for column in _srv._group_lines_into_columns(page_lines, page.rect.width):
        for seg in _srv._segment_column_into_paragraphs(column):
            emitted = []
            _srv._emit_sub_block(seg, emitted)
            for sb in emitted:
                paras.append({"i": len(paras), "text": sb["joined"],
                              "block_idx": seg[0].get("block_idx", 0)})
    return paras


PROMPT_TEMPLATE = """You are a sentence boundary detector for academic PDFs.

For each block below, copy the block text verbatim and insert the character `|` at every sentence boundary. Do not add or remove any other characters. Do not translate. Do not fix typos.

Rules:
- Never split at a decimal point (e.g. 3.14).
- Never split at abbreviations (Fig., Eq., Ref., Sec., Vol., No., pp., Ch., et al., e.g., i.e., etc., approx., Dr., Prof., Mr., Mrs., Ms.).
- Never split at species/name initials (S. sanguinis, E. coli, J. Smith).
- Never split inside URLs, emails, or file paths.
- Never split inside citation numbers like <sup>15</sup>.
- Do not split short headings that are just 1–3 words.
- Do not insert `|` at the beginning or end of a block.

Output format (STRICT):
===BLOCK 0===
<block 0 text with | inserted between sentences>
===BLOCK 1===
<block 1 text with | inserted between sentences>
===END===

Do not write any preamble, explanation, or code fences. Only the block sections.

Input:
{blocks_txt}
===END===

Output:
"""


def _push(job_id: str, event: dict):
    q: asyncio.Queue | None = JOBS.get(job_id, {}).get("queue")
    if q:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


async def process_job(job_id: str, pdf_bytes: bytes, model: str, page_no: int = 1):
    """バックグラウンドで走る処理本体。各段階で SSE イベントを push。"""
    try:
        _push(job_id, {"type": "log", "msg": "PDFを開いています..."})
        pdf_path = TMP_DIR / f"{job_id}.pdf"
        pdf_path.write_bytes(pdf_bytes)
        doc = fitz.open(pdf_path)
        if len(doc) == 0:
            _push(job_id, {"type": "error", "msg": "空のPDFです"})
            return
        _push(job_id, {"type": "log", "msg": f"総ページ数 {len(doc)}"})
        _push(job_id, {"type": "total_pages", "total": len(doc)})
        if page_no < 1 or page_no > len(doc):
            _push(job_id, {"type": "error", "msg": f"ページ番号が範囲外: {page_no} (1〜{len(doc)})"})
            return
        _push(job_id, {"type": "log", "msg": f"ページ {page_no} を解析中"})
        page = doc[page_no - 1]

        _push(job_id, {"type": "log", "msg": "ページ画像をレンダリング中..."})
        img = render_page_image(page)
        img_path = TMP_DIR / f"{job_id}.jpg"
        img.save(img_path, "JPEG", quality=85)
        _push(job_id, {
            "type": "image_ready",
            "url": f"/img/{img_path.name}",
            "width": img.width,
            "height": img.height,
        })

        _push(job_id, {"type": "log", "msg": "PyMuPDFで段落抽出中..."})
        paras = extract_paragraphs(page)
        _push(job_id, {"type": "log", "msg": f"段落 {len(paras)} 個を抽出"})

        _push(job_id, {"type": "log", "msg": "pysbdで文分割中..."})
        pysbd_result = {p["i"]: pysbd_ranges(p["text"]) for p in paras}
        _push(job_id, {"type": "prepared", "paras": paras, "pysbd": pysbd_result})

        # VL 呼び出し
        _push(job_id, {"type": "log", "msg": f"VLモデル ({model}) に画像+テキストを送信中..."})
        img_b64 = pil_to_b64(img)
        blocks_txt = "\n\n".join(f"===BLOCK {p['i']}===\n{p['text']}" for p in paras)
        prompt = PROMPT_TEMPLATE.format(blocks_txt=blocks_txt)
        messages = [
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                {"type": "text", "text": prompt},
            ]},
        ]
        # max_tokens: 出力長 ≈ 入力長 + マーカー分。char/tokenを ~0.4 で見積り、余裕2倍。
        total_input_chars = sum(len(p['text']) for p in paras)
        max_tokens = min(12000, max(1500, int(total_input_chars * 0.8)))
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "stop": ["===END==="],
            "chat_template_kwargs": {"enable_thinking": False},
        }
        _push(job_id, {"type": "log", "msg": f"VL応答をストリーミング受信中 (max_tokens={max_tokens})..."})
        vl_start = time.time()
        buf = []
        tok_count = 0
        last_progress = time.time()
        tmo = httpx.Timeout(connect=30.0, read=1800.0, write=60.0, pool=None)
        try:
            async with httpx.AsyncClient(timeout=tmo) as c:
                async with c.stream("POST", f"{LLM_URL}/v1/chat/completions", json=payload) as r:
                    r.raise_for_status()
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        choice = (obj.get("choices") or [{}])[0]
                        piece = (choice.get("delta") or {}).get("content") or ""
                        if piece:
                            buf.append(piece)
                            tok_count += 1
                            _push(job_id, {"type": "vl_chunk", "text": piece})
                            # 500ms ごとに進捗ログ
                            now = time.time()
                            if now - last_progress >= 0.5:
                                elapsed_s = now - vl_start
                                rate = tok_count / elapsed_s if elapsed_s > 0 else 0
                                _push(job_id, {"type": "log",
                                    "msg": f"{tok_count}トークン受信 / {elapsed_s:.1f}s / {rate:.1f}tok/s"})
                                last_progress = now
                        reason = choice.get("finish_reason")
                        if reason:
                            timings = obj.get("timings") or {}
                            stats = {
                                "reason": reason,
                                "eval_count": timings.get("predicted_n"),
                                "eval_ms": round(timings.get("predicted_ms") or 0),
                                "total_ms": round((timings.get("prompt_ms") or 0) + (timings.get("predicted_ms") or 0)),
                            }
                            if reason == "length":
                                _push(job_id, {"type": "log",
                                    "msg": f"⚠️ 上限で打ち切り (max_tokens={max_tokens})。全ブロックに到達しなかった可能性あり",
                                    })
                            _push(job_id, {"type": "log", "msg": f"VL完了 stats={stats}"})
                            break
        except Exception as e:
            _push(job_id, {"type": "error", "msg": f"VLエラー: {type(e).__name__}: {e}"})
            return

        elapsed = round(time.time() - vl_start, 2)
        raw = "".join(buf)
        # サーバコンソールにも生応答を出力(デバッグ)
        print(f"[check {job_id}] raw({len(raw)}chars):\n{raw}\n[/check {job_id}]", flush=True)
        _push(job_id, {"type": "log", "msg": f"生応答パース中 ({len(raw)}文字, {elapsed}s)"})

        # ===BLOCK N=== セクションを抽出し、'|' の位置を開始オフセットに変換
        vl_result = {}
        block_re = re.compile(r"===\s*BLOCK\s*(\d+)\s*===\s*\n?(.*?)(?=\n===\s*(?:BLOCK|END)|$)", re.S | re.I)
        matches = list(block_re.finditer(raw))
        _push(job_id, {"type": "log", "msg": f"===BLOCK=== セクション検出数: {len(matches)}"})

        parsed_blocks = {}
        for m in matches:
            try:
                idx = int(m.group(1))
                content = m.group(2).rstrip()
                parsed_blocks[idx] = content
            except Exception:
                continue

        for p in paras:
            text = p["text"]
            content = parsed_blocks.get(p["i"], "")
            if not content:
                vl_result[p["i"]] = []
                continue
            # VL は文間のスペースを `|` に置換していることがあるので、
            # `|` で分割した各文の先頭 N 文字を元テキスト内で検索して正しい offset を得る
            parts = [s for s in content.split("|") if s.strip()]
            offsets: list[int] = []
            search_from = 0
            text_len = len(text)
            for idx, part in enumerate(parts):
                # 先頭の空白は無視
                stripped_head = part.lstrip()
                if not stripped_head:
                    continue
                # 検索用のプレフィックス(空白を1つに正規化して先頭20〜30文字)
                probe = re.sub(r"\s+", " ", stripped_head[:30]).strip()
                if not probe:
                    continue
                # まず厳密検索
                pos = text.find(probe, search_from)
                if pos < 0:
                    # 空白正規化して緩やか検索 (原文の空白と VL 空白の違いを吸収)
                    norm_text = re.sub(r"\s+", " ", text[search_from:])
                    idx2 = norm_text.find(probe)
                    if idx2 >= 0:
                        # search_from からのオフセットに変換(空白差の分ずれ得るが概ね一致)
                        pos = search_from + idx2
                if pos < 0:
                    # 最後の手段: 先頭5文字だけ
                    if len(probe) >= 5:
                        pos = text.find(probe[:5], search_from)
                if pos < 0:
                    continue
                # 1番目の文は必ず 0 とする
                if idx == 0:
                    pos = 0
                offsets.append(pos)
                search_from = pos + max(1, len(probe))

            clean = sorted(set(o for o in offsets if 0 <= o < text_len))
            if 0 not in clean:
                clean.insert(0, 0)
            ranges = []
            for k in range(len(clean)):
                s = clean[k]
                e = clean[k + 1] if k + 1 < len(clean) else text_len
                if e > s and text[s:e].strip():
                    ranges.append([s, e])
            vl_result[p["i"]] = ranges

        _push(job_id, {"type": "vl_done", "vl": vl_result,
                       "elapsed": elapsed, "raw": raw})
        _push(job_id, {"type": "done"})
    except Exception as e:
        _push(job_id, {"type": "error", "msg": f"{type(e).__name__}: {e}"})


@app.post("/api/upload")
async def upload(file: UploadFile = File(...),
                 model: str = Form(DEFAULT_MODEL),
                 page: int = Form(1)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "PDFのみ")
    job_id = uuid.uuid4().hex[:12]
    pdf_bytes = await file.read()
    JOBS[job_id] = {"queue": asyncio.Queue(maxsize=1024)}
    asyncio.create_task(process_job(job_id, pdf_bytes, model, page))
    return {"job_id": job_id}


@app.post("/api/rerun/{job_id}")
async def rerun(job_id: str, page: int = Form(1), model: str = Form(DEFAULT_MODEL)):
    """既にアップロード済みPDFの別ページを解析"""
    pdf_path = TMP_DIR / f"{job_id}.pdf"
    if not pdf_path.exists():
        raise HTTPException(404, "PDFファイルなし。再アップロードしてください")
    pdf_bytes = pdf_path.read_bytes()
    new_job_id = uuid.uuid4().hex[:12]
    # PDFをコピー(process_jobが再度書き込むため)
    (TMP_DIR / f"{new_job_id}.pdf").write_bytes(pdf_bytes)
    JOBS[new_job_id] = {"queue": asyncio.Queue(maxsize=1024)}
    asyncio.create_task(process_job(new_job_id, pdf_bytes, model, page))
    return {"job_id": new_job_id}


@app.get("/api/job/{job_id}/stream")
async def stream(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404)
    q: asyncio.Queue = JOBS[job_id]["queue"]

    async def gen() -> AsyncIterator[bytes]:
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=30.0)
            except asyncio.TimeoutError:
                yield b": keepalive\n\n"
                continue
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode()
            if ev.get("type") in ("done", "error"):
                break
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/img/{name}")
async def get_img(name: str):
    p = TMP_DIR / name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/jpeg")


@app.get("/api/models")
async def models():
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"{LLM_URL}/v1/models")
            r.raise_for_status()
            return {"models": [m["id"] for m in r.json().get("data", [])],
                    "default": DEFAULT_MODEL}
    except Exception:
        return {"models": [], "default": DEFAULT_MODEL}


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(Path(__file__).parent.joinpath("static/check.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})


if __name__ == "__main__":
    import os
    import uvicorn
    uvicorn.run(app, host="0.0.0.0",
                port=int(os.environ.get("PORT", "8081")),
                log_level="info")
