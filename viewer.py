#!/usr/bin/env python3
"""PDF VL Assistant の閲覧専用版 (LLM 不要)。

本体 (server.py) で解析・翻訳した結果を、LLM の無い別の PC で読むためのサーバー。
画面は本体と同じ static/index.html を「閲覧モード」で表示する。
解析・再翻訳・チャットなど LLM が要る機能は使えないが、対訳表示・図・コメント (追加・編集も可) は使える。

Python 3.9 以上だけで動く (追加のインストール不要)。

    python viewer.py                 # http://127.0.0.1:8095 をブラウザで開く
    python viewer.py --data DIR      # 解析データの置き場所 (既定: このフォルダの jobs/)
    python viewer.py --host 0.0.0.0  # 他の PC からも見られるようにする
    python viewer.py --no-browser    # ブラウザを自動で開かない

解析データの持ってき方:
  - 本体の画面で文書を開き「📦 書き出し」→ できた .zip を、閲覧版の画面の「📥 読み込み」で取り込む
  - または、本体の jobs/<ジョブID>/ フォルダを、閲覧版の jobs/ の下にそのままコピーする
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import re
import shutil
import tempfile
import threading
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
MAX_IMPORT_BYTES = 4 * 1024 ** 3   # 読み込む zip の上限

# 訳文の言語の表示名 (server.py の LANGUAGES と同じ並び。閲覧版は LLM 用の設定を持たないので名前だけ)
LANGUAGES = [("ja", "日本語"), ("en", "English"), ("zh", "中文（简体）"), ("ko", "한국어"),
             ("de", "Deutsch"), ("fr", "Français"), ("es", "Español")]
DEFAULT_LANG = "ja"

JOB_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_lock = threading.Lock()           # state.json の書き換えを 1 本ずつにする
DATA_DIR: Path = HERE / "jobs"


# ---------------------------------------------------------------- 解析データ

def job_dir(job_id: str) -> Path | None:
    if not JOB_ID_RE.match(job_id or ""):
        return None
    d = DATA_DIR / job_id
    return d if (d / "state.json").is_file() else None


def load_state(job_id: str) -> dict | None:
    d = job_dir(job_id)
    if not d:
        return None
    try:
        state = json.loads((d / "state.json").read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[viewer] {job_id}/state.json を読めません: {e}", flush=True)
        return None
    state["id"] = job_id
    # 解析途中で書き出されたデータは、閲覧版では続きが走らないので「中断」として見せる
    if state.get("status") in ("queued", "running", "cancelling"):
        state["status"] = "cancelled"
    if state.get("supplement_status") in ("queued", "processing", "cancelling"):
        state["supplement_status"] = "cancelled"
    for p in state.get("pages", []):
        if p.get("status") == "processing":
            p["status"] = "pending"
    return state


def list_jobs() -> list[dict]:
    items = []
    if DATA_DIR.is_dir():
        for d in DATA_DIR.iterdir():
            if not d.is_dir():
                continue
            j = load_state(d.name)
            if not j:
                continue
            items.append({k: j.get(k) for k in (
                "id", "filename", "status", "current_page", "total_pages",
                "created_at", "finished_at", "supplement_status")} | {"lang": j.get("lang", DEFAULT_LANG)})
    items.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return items


def set_comment(job_id: str, body: dict) -> tuple[int, dict]:
    """文へのコメントを付ける・消す (本体の /api/job/{id}/comment と同じ)。"""
    d = job_dir(job_id)
    if not d:
        return 404, {"detail": "job not found"}
    with _lock:
        path = d / "state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        pages = state.get("pages", [])
        page_no, i = int(body.get("page", 0)), body.get("i")
        if page_no < 1 or page_no > len(pages):
            return 400, {"detail": "invalid page"}
        block = next((b for b in pages[page_no - 1].get("blocks", []) if b.get("i") == i), None)
        if not block:
            return 404, {"detail": "sentence not found"}
        text = str(body.get("text") or "").strip()
        if text:
            block["comment"] = text
        else:
            block.pop("comment", None)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    return 200, {"ok": True}


def import_zip(src: Path) -> tuple[int, dict]:
    """本体の「書き出し」で作った zip を data フォルダに展開する。同じジョブがあれば置き換える。"""
    try:
        zf = zipfile.ZipFile(src)
    except zipfile.BadZipFile:
        return 400, {"detail": "zip ファイルではありません"}
    with zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        states = [n for n in names if n.split("/")[-1] == "state.json" and n.count("/") <= 1]
        if len(states) != 1:
            return 400, {"detail": "PDF VL の書き出しデータではありません (state.json が見つかりません)"}
        prefix = states[0][: -len("state.json")]           # "<ジョブID>/" または ""
        try:
            state = json.loads(zf.read(states[0]).decode("utf-8"))
        except Exception:
            return 400, {"detail": "state.json を読めません"}
        job_id = state.get("id") or prefix.rstrip("/")
        if not JOB_ID_RE.match(job_id or ""):
            return 400, {"detail": "ジョブ ID が不正です"}
        dest = DATA_DIR / job_id
        work = Path(tempfile.mkdtemp(prefix=f".import-{job_id}-", dir=DATA_DIR))
        try:
            for n in names:
                if not n.startswith(prefix):
                    continue
                rel = Path(n[len(prefix):])
                # zip の中の ../ や絶対パスで data フォルダの外に書かせない
                if rel.is_absolute() or ".." in rel.parts or not rel.parts:
                    return 400, {"detail": f"不正なパスを含みます: {n}"}
                out = work / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(n) as fsrc, open(out, "wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst)
            with _lock:
                if dest.exists():
                    shutil.rmtree(dest)
                work.rename(dest)
        finally:
            if work.exists():
                shutil.rmtree(work, ignore_errors=True)
    print(f"[viewer] 読み込み: {job_id} {state.get('filename', '?')}", flush=True)
    return 200, {"ok": True, "job_id": job_id, "filename": state.get("filename")}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "PDFVLViewer"

    def log_message(self, fmt, *args):
        pass

    def _json(self, code: int, obj) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path: Path, no_cache: bool = False) -> None:
        if not path.is_file():
            return self._json(404, {"detail": "not found"})
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(path.stat().st_size))
        if no_cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    @staticmethod
    def _safe_join(base: Path, rel: str) -> Path | None:
        p = (base / unquote(rel)).resolve()
        return p if p.is_relative_to(base.resolve()) else None

    def _read_body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
            # 画面に閲覧モードであることを知らせる (LLM が要る機能を隠す)
            html = html.replace("<head>", "<head>\n<script>window.PDFVL_VIEWER = true;</script>", 1)
            data = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return self.wfile.write(data)
        if path.startswith("/static/"):
            p = self._safe_join(STATIC_DIR, path[len("/static/"):])
            return self._file(p) if p else self._json(404, {"detail": "not found"})
        if path.startswith("/jobs/"):
            p = self._safe_join(DATA_DIR, path[len("/jobs/"):])
            return self._file(p) if p else self._json(404, {"detail": "not found"})
        if path == "/api/jobs":
            return self._json(200, {"jobs": list_jobs()})
        if path == "/api/languages":
            return self._json(200, {"languages": [{"code": c, "name": n} for c, n in LANGUAGES],
                                    "default": DEFAULT_LANG})
        if path == "/api/viewer":
            return self._json(200, {"viewer": True, "data_dir": str(DATA_DIR)})
        m = re.fullmatch(r"/api/job/([^/]+)", path)
        if m:
            state = load_state(m.group(1))
            return self._json(200, state) if state else self._json(404, {"detail": "job not found"})
        return self._json(404 if not path.startswith("/api/") else 403,
                          {"detail": "閲覧モードでは使えません (LLM が必要な機能です)"})

    def do_POST(self):
        path = urlparse(self.path).path
        m = re.fullmatch(r"/api/job/([^/]+)/comment", path)
        if m:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"detail": "invalid json"})
            return self._json(*set_comment(m.group(1), body))
        if path == "/api/import":
            size = int(self.headers.get("Content-Length") or 0)
            if size <= 0 or size > MAX_IMPORT_BYTES:
                return self._json(400, {"detail": "zip の大きさが不正です"})
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=DATA_DIR, prefix=".upload-", suffix=".zip", delete=False) as tmp:
                remaining = size
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    tmp.write(chunk)
                    remaining -= len(chunk)
            try:
                return self._json(*import_zip(Path(tmp.name)))
            finally:
                Path(tmp.name).unlink(missing_ok=True)
        self._read_body()
        return self._json(403, {"detail": "閲覧モードでは使えません (LLM が必要な機能です)"})

    def do_DELETE(self):
        m = re.fullmatch(r"/api/job/([^/]+)", urlparse(self.path).path)
        d = job_dir(m.group(1)) if m else None
        if not d:
            return self._json(404, {"detail": "job not found"})
        with _lock:
            shutil.rmtree(d)
        return self._json(200, {"ok": True})


def main() -> None:
    global DATA_DIR
    ap = argparse.ArgumentParser(description="PDF VL Assistant の閲覧専用版 (LLM 不要)")
    ap.add_argument("--data", default=str(HERE / "jobs"), help="解析データの置き場所 (既定: このフォルダの jobs/)")
    ap.add_argument("--host", default="127.0.0.1", help="待ち受けアドレス。他の PC からも見るなら 0.0.0.0")
    ap.add_argument("--port", type=int, default=8095)
    ap.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    a = ap.parse_args()
    DATA_DIR = Path(a.data).expanduser().resolve()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    url = f"http://{'127.0.0.1' if a.host in ('0.0.0.0', '') else a.host}:{a.port}/"
    print(f"PDF VL 閲覧版: {url}  (解析データ: {DATA_DIR}, {len(list_jobs())} 件)  Ctrl+C で終了", flush=True)
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
