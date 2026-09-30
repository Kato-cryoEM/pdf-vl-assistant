"""ローカル LLM (llama.cpp) の検出・導入・起動。

このアプリはページ画像を LLM に見せて解析するので、視覚対応 (VL) のモデルが要る。
起動時に導入済みの GGUF を探し、無ければ推奨リストからダウンロードして
llama-server を立ち上げられるようにする。
"""
import asyncio
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).parent
MODELS_DIR = Path(os.environ.get("PDFVL_MODELS_DIR", BASE_DIR / "models"))

# GGUF を探す場所 (インストーラはこの順で MODELS_DIR に置く)
MODEL_SEARCH_DIRS = [
    MODELS_DIR,
    Path.home() / "models",
    Path.home() / ".cache" / "llama.cpp",
    Path.home() / ".cache" / "huggingface" / "hub",
]

# llama-server を探す場所
LLAMA_SEARCH_PATHS = [
    BASE_DIR / "vendor" / "llama.cpp" / "build" / "bin" / "llama-server",
    BASE_DIR / "vendor" / "llama.cpp" / "llama-server",
    Path.home() / ".local" / "bin" / "llama-server",
    Path.home() / "app" / "llama.cpp" / "build" / "bin" / "llama-server",
]

# 推奨モデル。すべて mmproj 付き (視覚入力に対応) の GGUF。
#   model_file / mmproj_file : リポジトリ内のパス (サブフォルダ可)
#   parts                    : 分割 GGUF の分割数 (単一ファイルなら省略)
#   size_gb                  : 本体 + mmproj のおおよその合計
# モデルごとにサブフォルダへ入れる。mmproj-F16.gguf のように名前が衝突するため。
CATALOG = [
    {
        "id": "qwen3.5-2b",
        "name": "Qwen3.5 2B (Q4_K_M)",
        "repo": "unsloth/Qwen3.5-2B-GGUF",
        "model_file": "Qwen3.5-2B-Q4_K_M.gguf",
        "mmproj_file": "mmproj-F16.gguf",
        "size_gb": 2.0,
        "min_ram_gb": 6,
        "note": "一番軽い。まず動かしてみる用。訳の質は控えめ。",
    },
    {
        "id": "qwen3.5-4b",
        "name": "Qwen3.5 4B (Q4_K_M)",
        "repo": "unsloth/Qwen3.5-4B-GGUF",
        "model_file": "Qwen3.5-4B-Q4_K_M.gguf",
        "mmproj_file": "mmproj-F16.gguf",
        "size_gb": 3.4,
        "min_ram_gb": 8,
        "note": "軽量。ノートPCでも動く。",
    },
    {
        "id": "qwen3.5-9b",
        "name": "Qwen3.5 9B (Q4_K_M)",
        "repo": "unsloth/Qwen3.5-9B-GGUF",
        "model_file": "Qwen3.5-9B-Q4_K_M.gguf",
        "mmproj_file": "mmproj-F16.gguf",
        "size_gb": 6.6,
        "min_ram_gb": 12,
        "note": "実用の下限あたり。8〜12GB クラスの GPU 向け。",
    },
    {
        "id": "gemma4-12b",
        "name": "Gemma 4 12B Instruct (Q4_K_M)",
        "repo": "unsloth/gemma-4-12b-it-GGUF",
        "model_file": "gemma-4-12b-it-Q4_K_M.gguf",
        "mmproj_file": "mmproj-F16.gguf",
        "size_gb": 7.3,
        "min_ram_gb": 14,
        "note": "Qwen 以外の選択肢。日本語の訳し方の癖が違う。",
    },
    {
        "id": "qwen3.5-27b",
        "name": "Qwen3.5 27B (Q4_K_M)",
        "repo": "unsloth/Qwen3.5-27B-GGUF",
        "model_file": "Qwen3.5-27B-Q4_K_M.gguf",
        "mmproj_file": "mmproj-F16.gguf",
        "size_gb": 17.7,
        "min_ram_gb": 24,
        "note": "24GB クラスの GPU 向け。訳がこなれる。",
    },
    {
        "id": "qwen3.8-27b",
        "name": "Qwen3.8 27B (Q4_K_M)",
        "repo": "ggml-org/Qwen3.8-27B-GGUF",
        "model_file": "Qwen3.8-27B-Q4_K_M.gguf",
        "mmproj_file": "mmproj-Qwen3.8-27B-BF16.gguf",
        "size_gb": 19.9,
        "min_ram_gb": 26,
        "note": "この規模では最新世代。精度重視ならこれ。",
    },
    {
        "id": "qwen3.5-35b-a3b",
        "name": "Qwen3.5 35B-A3B (Q4_K_M, MoE)",
        "repo": "unsloth/Qwen3.5-35B-A3B-GGUF",
        "model_file": "Qwen3.5-35B-A3B-Q4_K_M.gguf",
        "mmproj_file": "mmproj-F16.gguf",
        "size_gb": 22.9,
        "min_ram_gb": 30,
        "note": "MoE なので大きい割に速い。メモリに余裕があるなら。",
    },
    {
        "id": "qwen3.8-flash-next",
        "name": "Qwen3.8 Flash-Next (UD-Q3_K_XL, MoE 125B)",
        "repo": "unsloth/Qwen3.8-Flash-Next-GGUF",
        "model_file": "UD-Q3_K_XL/Qwen3.8-Flash-Next-UD-Q3_K_XL.gguf",
        "mmproj_file": "mmproj-BF16.gguf",
        "parts": 3,
        "size_gb": 90.9,
        "min_ram_gb": 100,
        "note": "最上位。128GB 級の統合メモリ / 大容量 VRAM 向け。分割ファイル。",
    },
]

HF_BASE = "https://huggingface.co"


# ---------------------------------------------------------------- llama-server

def find_llama_server() -> Path | None:
    """llama-server の実行ファイルを探す。"""
    env = os.environ.get("LLAMA_SERVER_BIN")
    if env and Path(env).is_file():
        return Path(env)
    which = shutil.which("llama-server")
    if which:
        return Path(which)
    for p in LLAMA_SEARCH_PATHS:
        if p.is_file():
            return p
    # ~/app/llama.cpp* のような派生ディレクトリも見る
    for parent in (Path.home() / "app", Path.home()):
        if not parent.is_dir():
            continue
        try:
            for d in sorted(parent.glob("llama.cpp*")):
                cand = d / "build" / "bin" / "llama-server"
                if cand.is_file():
                    return cand
        except OSError:
            continue
    return None


# ---------------------------------------------------------------- local models

def _is_mmproj(name: str) -> bool:
    return "mmproj" in name.lower()


def scan_local_models() -> list[dict]:
    """導入済みの GGUF を探し、mmproj (視覚エンコーダ) と対にして返す。"""
    found: dict[str, dict] = {}
    for d in MODEL_SEARCH_DIRS:
        if not d.is_dir():
            continue
        try:
            files = list(d.rglob("*.gguf"))
        except OSError:
            continue
        mmprojs = [f for f in files if _is_mmproj(f.name)]
        for f in files:
            if _is_mmproj(f.name):
                continue
            # 分割ファイルは先頭だけを候補にする
            m = re.search(r"-(\d{5})-of-(\d{5})\.gguf$", f.name)
            if m and m.group(1) != "00001":
                continue
            key = str(f.resolve())
            if key in found:
                continue
            mm = _match_mmproj(f, mmprojs)
            size = _total_size(f, m)
            found[key] = {
                "path": key,
                "name": f.stem,
                "dir": str(f.parent),
                "size_gb": round(size / 1e9, 2),
                "mmproj": str(mm.resolve()) if mm else None,
                "vision": mm is not None,
            }
    return sorted(found.values(), key=lambda x: (not x["vision"], x["name"].lower()))


def _total_size(first: Path, split_match) -> int:
    """分割 GGUF は全パートの合計を返す (先頭パートだけだと中身が無い)。"""
    try:
        if not split_match:
            return first.stat().st_size
        total = 0
        base = first.name[:split_match.start()]
        for p in first.parent.glob(f"{base}-*-of-*.gguf"):
            total += p.stat().st_size
        return total or first.stat().st_size
    except OSError:
        return 0


def _match_mmproj(model: Path, mmprojs: list[Path]) -> Path | None:
    """モデルに対応する mmproj を探す。同じディレクトリのものを優先し、
    無ければ 1 つ上のディレクトリも見る (量子化ごとにサブフォルダを切り、
    mmproj は親に 1 つだけ置く配置がよくある)。"""
    same_dir = [m for m in mmprojs if m.parent == model.parent]
    if not same_dir:
        same_dir = [m for m in mmprojs if m.parent == model.parent.parent]
    if not same_dir:
        return None
    if len(same_dir) == 1:
        return same_dir[0]
    stem = model.stem.lower()

    def score(m: Path) -> int:
        s = m.stem.lower().replace("mmproj-", "").replace("mmproj_", "")
        n = 0
        for i in range(min(len(s), len(stem))):
            if s[i] != stem[i]:
                break
            n += 1
        return n

    return max(same_dir, key=score)


def entry_files(entry: dict) -> list[str]:
    """カタログ 1 件分のダウンロード対象 (リポジトリ内パス)。
    分割 GGUF は -00001-of-0000N.gguf … に展開する。"""
    n = int(entry.get("parts") or 1)
    if n <= 1:
        model = [entry["model_file"]]
    else:
        base = entry["model_file"][:-len(".gguf")]
        model = [f"{base}-{i:05d}-of-{n:05d}.gguf" for i in range(1, n + 1)]
    return model + [entry["mmproj_file"]]


def entry_dir(entry: dict) -> Path:
    """モデルごとの置き場所。mmproj-F16.gguf のように名前が衝突するので分ける。"""
    return MODELS_DIR / entry["id"]


def entry_paths(entry: dict) -> tuple[Path, Path]:
    """(llama-server に渡すモデルのパス, mmproj のパス)。
    分割ファイルの場合、先頭パートを渡せば llama.cpp が残りも読む。"""
    d = entry_dir(entry)
    return d / Path(entry_files(entry)[0]).name, d / Path(entry["mmproj_file"]).name


def catalog_with_state() -> list[dict]:
    """推奨リストに「導入済みかどうか」を付けて返す。"""
    out = []
    for e in CATALOG:
        model_path, mmproj_path = entry_paths(e)
        out.append({**e,
                    "installed": model_path.is_file() and mmproj_path.is_file(),
                    "path": str(model_path),
                    "mmproj": str(mmproj_path)})
    return out


# ---------------------------------------------------------------- download

class Downloader:
    """推奨モデルのダウンロード。1 度に 1 件だけ走らせる。"""

    def __init__(self):
        self.task: asyncio.Task | None = None
        self.state: dict = {"status": "idle"}

    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self, entry: dict) -> None:
        entry_dir(entry).mkdir(parents=True, exist_ok=True)
        self.state = {
            "status": "downloading", "id": entry["id"], "name": entry["name"],
            "done_bytes": 0, "total_bytes": 0, "file": "", "error": None,
            "started_at": time.time(),
        }
        self.task = asyncio.create_task(self._run(entry))

    async def _run(self, entry: dict) -> None:
        try:
            files = entry_files(entry)
            dest_dir = entry_dir(entry)
            for i, fname in enumerate(files, 1):
                self.state["file"] = f"{Path(fname).name} ({i}/{len(files)})"
                await self._fetch(entry["repo"], fname, dest_dir)
            self.state["status"] = "done"
        except asyncio.CancelledError:
            self.state["status"] = "cancelled"
            raise
        except Exception as e:
            self.state["status"] = "error"
            self.state["error"] = f"{type(e).__name__}: {e}"

    async def _fetch(self, repo: str, fname: str, dest_dir: Path) -> None:
        # リポジトリ内はサブフォルダでも、手元は 1 つのフォルダに平らに置く
        dest = dest_dir / Path(fname).name
        if dest.is_file():
            return
        part = dest.with_suffix(dest.suffix + ".part")
        pos = part.stat().st_size if part.is_file() else 0
        url = f"{HF_BASE}/{repo}/resolve/main/{fname}"
        headers = {"Range": f"bytes={pos}-"} if pos else {}
        timeout = httpx.Timeout(60.0, read=600.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
            async with c.stream("GET", url, headers=headers) as r:
                if r.status_code == 416:  # 既に全部ある
                    part.rename(dest)
                    return
                r.raise_for_status()
                total = int(r.headers.get("content-length") or 0) + pos
                self.state["total_bytes"] = total
                self.state["done_bytes"] = pos
                mode = "ab" if pos else "wb"
                with open(part, mode) as f:
                    async for chunk in r.aiter_bytes(1 << 20):
                        f.write(chunk)
                        self.state["done_bytes"] += len(chunk)
        part.rename(dest)


# ---------------------------------------------------------------- run server

class LlamaProcess:
    """このアプリが起動した llama-server。"""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.info: dict = {}

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if not self.running():
            self.proc = None
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None
        self.info = {}

    def start(self, model_path: str, mmproj: str | None, alias: str = "pdfvl",
              ctx: int = 32768, port: int | None = None,
              log_path: Path | None = None) -> dict:
        bin_path = find_llama_server()
        if bin_path is None:
            raise RuntimeError(
                "llama-server が見つかりません。install.sh を実行するか "
                "LLAMA_SERVER_BIN に実行ファイルのパスを設定してください。")
        if not Path(model_path).is_file():
            raise RuntimeError(f"モデルが見つかりません: {model_path}")
        self.stop()
        port = port or _free_port()
        cmd = [str(bin_path), "-m", model_path, "-a", alias,
               "-ngl", "999", "-c", str(ctx),
               "--host", "127.0.0.1", "--port", str(port),
               "--jinja", "--no-webui"]
        if mmproj:
            cmd += ["--mmproj", mmproj]
        log_path = log_path or (BASE_DIR / "llama-server.log")
        log = open(log_path, "ab")
        log.write(f"\n=== {time.strftime('%F %T')} {' '.join(cmd)}\n".encode())
        log.flush()
        self.proc = subprocess.Popen(cmd, stdout=log, stderr=log)
        self.info = {"model_path": model_path, "mmproj": mmproj, "alias": alias,
                     "port": port, "url": f"http://127.0.0.1:{port}",
                     "bin": str(bin_path), "log": str(log_path),
                     "started_at": time.time()}
        return dict(self.info)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def wait_ready(url: str, timeout_s: float = 600.0) -> bool:
    """llama-server がリクエストを受けられるようになるまで待つ。"""
    deadline = time.time() + timeout_s
    async with httpx.AsyncClient(timeout=5.0) as c:
        while time.time() < deadline:
            try:
                r = await c.get(f"{url}/health")
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(2.0)
    return False


async def probe(url: str) -> dict:
    """接続先の状態を調べる。"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{url}/v1/models")
            r.raise_for_status()
            names = [m["id"] for m in r.json().get("data", [])]
            return {"ok": True, "models": names}
    except Exception as e:
        return {"ok": False, "models": [], "error": f"{type(e).__name__}: {e}"}
