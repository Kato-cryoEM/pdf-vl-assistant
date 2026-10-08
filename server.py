import asyncio
import base64
import io
import json
import os
import re
import statistics
import time
import uuid
from pathlib import Path
from typing import AsyncIterator

import fitz  # PyMuPDF
import httpx
import pysbd
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel

import llm_manager

# 文分割エンジン(言語別)。学術英語の Fig./Eq./et al./e.g. などを内蔵で処理
_SEG_EN = pysbd.Segmenter(language="en", clean=False, char_span=True)
_SEG_JA = pysbd.Segmenter(language="ja", clean=False, char_span=True)


def _detect_lang(text: str) -> str:
    """簡易判定: 日本語文字を含むかで en/ja を切替。"""
    for ch in text[:200]:
        if '぀' <= ch <= 'ヿ' or '一' <= ch <= '鿿':
            return "ja"
    return "en"

# llama-server (OpenAI 互換 API)。既に動いているものに繋ぐか、アプリから起動する。
# 起動し直すと差し替わるので実行中に書き換わる (llm_manager 経由)。
LLM_URL = os.environ.get("LLM_URL", "http://127.0.0.1:8004")
DEFAULT_MODEL = os.environ.get("PDFVL_MODEL", "qwen3.8-flash-next")
# アプリが起動したモデルを止めたときに戻す先
_DEFAULT_LLM_URL = LLM_URL
_DEFAULT_MODEL_NAME = DEFAULT_MODEL
BASE_DIR = Path(__file__).parent
JOBS_DIR = BASE_DIR / "jobs"
STATIC_DIR = BASE_DIR / "static"
JOBS_DIR.mkdir(exist_ok=True)

PAGE_RENDER_DPI = 150
MIN_FIGURE_WIDTH = 200
MIN_FIGURE_HEIGHT = 150
MAX_IMG_EDGE = 1600
# 数式は文字として組み直せないので元ページから切り出して画像で貼る
FORMULA_RENDER_DPI = 300
FORMULA_CROP_PAD = 3.0  # PDF ポイント
# 1ページあたり LLM に渡すブロックテキスト量の上限(文字)
MAX_BLOCKS_CHARS = 6500
# 前後ページ文脈
CTX_CHARS = 700
# 全体要約用の翻訳文字数上限
MAX_OVERALL_CHARS = 45000

app = FastAPI(title="PDF VL Assistant")
JOBS: dict[str, dict] = {}


# JSON にできない実行時オブジェクト (保存・API 応答から除く)
_RUNTIME_KEYS = ("event_queue", "task", "supplement_task")


def _save_job_state(job_id: str) -> None:
    """ジョブの状態を state.json にディスク保存(再ロード用)。"""
    job = JOBS.get(job_id)
    if not job:
        return
    state = {k: v for k, v in job.items() if k not in _RUNTIME_KEYS}
    state_path = JOBS_DIR / job_id / "state.json"
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[save_state {job_id}] {type(e).__name__}: {e}", flush=True)


def _load_saved_jobs() -> None:
    """起動時に JOBS_DIR をスキャンして保存済みジョブを読み込む。"""
    if not JOBS_DIR.exists():
        return
    for job_dir in sorted(JOBS_DIR.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0):
        if not job_dir.is_dir():
            continue
        state_path = job_dir / "state.json"
        if not state_path.exists():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            job_id = state.get("id") or job_dir.name
            # 進行中扱いだった状態は "cancelled" に降格 (サーバ再起動時)。
            # 再起動をまたいで処理が続くことはないので、残っていたら必ず落とす。
            if state.get("status") in ("queued", "running", "cancelling"):
                state["status"] = "cancelled"
                state["error"] = state.get("error") or "サーバ再起動により中断"
            if state.get("supplement_status") in ("queued", "processing", "cancelling"):
                state["supplement_status"] = "cancelled"
            for p in state.get("pages", []):
                if p.get("status") == "processing":
                    p["status"] = "pending"
            state["event_queue"] = asyncio.Queue(maxsize=2048)
            JOBS[job_id] = state
            print(f"[load] {job_id} {state.get('filename','?')} status={state.get('status')} pages={state.get('total_pages','?')}",
                  flush=True)
        except Exception as e:
            print(f"[load {job_dir.name}] {type(e).__name__}: {e}", flush=True)


def pil_to_b64(img: Image.Image, fmt: str = "JPEG", quality: int = 88) -> str:
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    w, h = img.size
    m = max(w, h)
    if m > MAX_IMG_EDGE:
        s = MAX_IMG_EDGE / m
        img = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format=fmt, quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


def render_page_image(page: fitz.Page, dpi: int = PAGE_RENDER_DPI) -> tuple[Image.Image, float]:
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples), zoom


# pysbd がデフォで認識しない学術系略語 (末尾のピリオドを一時的に隠す)
_EXTRA_ABBRS = ['Fig', 'Figs', 'Eq', 'Eqs', 'Sec', 'Secs', 'Ref', 'Refs',
                'Vol', 'Vols', 'No', 'Nos', 'pp', 'Ch', 'Chs', 'App',
                'Prop', 'Thm', 'Lem', 'Def', 'Cor', 'approx', 'ca']
_ABBR_RE = re.compile(r'\b(' + '|'.join(_EXTRA_ABBRS) + r')\.')
# 単一大文字 + ピリオド + 空白 + 小文字 → 学名/イニシャル(例: S. sanguinis, J. Smith)
_INITIAL_RE = re.compile(r'\b([A-Z])\.(?=\s+[a-z])')


def _protect_abbrs(text: str) -> str:
    text = _ABBR_RE.sub(lambda m: m.group(1) + '\x00', text)
    text = _INITIAL_RE.sub(lambda m: m.group(1) + '\x00', text)
    return text


def _split_sentences_ranges(text: str) -> list[tuple[int, int]]:
    """pysbd で文境界を検出し、原文の (start, end) 文字インデックスの配列を返す。
    pysbd 未対応の学術略語(Fig./Eq./Ref.等)と単一大文字イニシャル(S. sanguinis等)を保護する。"""
    if not text.strip():
        return []
    protected = _protect_abbrs(text)
    seg = _SEG_JA if _detect_lang(text) == "ja" else _SEG_EN
    try:
        spans = seg.segment(protected)
    except Exception:
        return [(0, len(text))]
    ranges: list[tuple[int, int]] = []
    for sp in spans:
        s, e = getattr(sp, "start", None), getattr(sp, "end", None)
        if s is None or e is None:
            continue
        if text[s:e].strip():
            ranges.append((s, e))
    return ranges


_BOLD_RE = re.compile(r"[Bb]old|BOLD|Black|Heavy|Semibold|SemiBold|Demi")
_ITALIC_RE = re.compile(r"[Ii]talic|[Oo]blique")


def _span_styles(span: dict, base_size: float, line_y_mid: float) -> list[str]:
    """span から適用すべきスタイルの集合を返す。順序: 内側から外側。
    値: 'italic', 'bold', 'super', 'sub' の組み合わせ。"""
    text = (span.get("text") or "").strip()
    if not text:
        return []
    size = float(span.get("size") or base_size)
    bbox = span.get("bbox") or [0, 0, 0, 0]
    y_mid = (bbox[1] + bbox[3]) / 2.0
    flags = int(span.get("flags") or 0)
    font_name = (span.get("font") or "")

    styles: list[str] = []

    # italic (flags bit 1 または フォント名)
    if (flags & 2) or _ITALIC_RE.search(font_name):
        styles.append("italic")
    # bold (flags bit 4 または フォント名)
    if (flags & 16) or _BOLD_RE.search(font_name):
        styles.append("bold")
    # sub/super の判定 (排他)
    if flags & 1:
        styles.append("super")
    else:
        is_small = size < base_size * 0.78
        if is_small:
            offset = y_mid - line_y_mid
            if offset < -base_size * 0.10:
                styles.append("super")
            elif offset > base_size * 0.10:
                styles.append("sub")
    return styles


def _wrap_styles(text: str, styles: list[str]) -> str:
    if not styles:
        return text
    out = text
    if "italic" in styles:
        out = f"<i>{out}</i>"
    if "bold" in styles:
        out = f"<b>{out}</b>"
    if "super" in styles:
        out = f"<sup>{out}</sup>"
    if "sub" in styles:
        out = f"<sub>{out}</sub>"
    return out


# 後方互換用エイリアス (使ってる箇所があるため)
def _span_role(span: dict, base_size: float, line_y_mid: float) -> str:
    styles = _span_styles(span, base_size, line_y_mid)
    if "super" in styles: return "super"
    if "sub" in styles: return "sub"
    return "normal"


def _wrap_role(text: str, role: str) -> str:
    if role == "super": return f"<sup>{text}</sup>"
    if role == "sub": return f"<sub>{text}</sub>"
    return text


def _split_lines_to_sub_blocks(lines_meta: list[dict]) -> list[list[dict]]:
    """check.py と同じ方式: PyMuPDF ブロックをそのまま 1 段落として扱う。
    サブブロック分割はしない(閾値ベースの分割は誤発火が多く、page1/14 で両立できない)。"""
    return [lines_meta] if lines_meta else []


# 図表キャプションの先頭。番号の直後が区切り記号 (. : | 全角コロン) か行末であることを
# 求めることで、本文中の "Fig. 3 shows ..." のような文と区別する。
_CAPTION_HEAD_RE = re.compile(
    r"^\s*(?:<[^>]+>\s*)*"
    r"(?:supplementary\s+|suppl?\.?\s+|補足\s*|副\s*)?"
    r"(Fig(?:ure)?s?\.?|Scheme|Table|Tab\.?|図|表)\s*"
    r"(S?\d+[A-Za-z]?(?:[-–]S?\d+[A-Za-z]?)?)"
    # 番号の直後は区切り記号か、句読点なしで見出し語が続く形 ("Figure 6 Comparison of...")。
    # 本文は "Fig. 3 shows ...", "Fig. 4a to 4c are ..." と小文字の語が続くので分かれる。
    r"(?:\s*[.:：|]|\s+(?=(?-i:[A-Z]))|$)",
    re.IGNORECASE,
)


def _heuristic_label(sub_block_meta: dict, page_median_size: float) -> str | None:
    """フォント特性から確定的にラベル付けできるものを返す。
    無理そうなら None (VL/デフォルトに任せる)。"""
    size = sub_block_meta["size"]
    text = sub_block_meta["text_plain"]
    is_bold = sub_block_meta["bold"]
    n_chars = len(text.strip())
    if n_chars == 0:
        return None
    size_ratio = size / max(page_median_size, 0.1)
    text_stripped = text.strip()
    ends_with_period = text_stripped.endswith((".", "。", ":", "："))
    is_short = n_chars <= 80
    is_very_short = n_chars <= 60
    # 特有な見出し語(英日) — 複合語 (Materials and Methods 等) にも対応
    heading_keywords = re.compile(
        r"^(abstract|introduction|"
        r"materials?(?:\s+and\s+methods?)?|methods?(?:\s+and\s+materials?)?|"
        r"results?(?:\s+and\s+discussions?)?|discussions?(?:\s+and\s+conclusions?)?|"
        r"conclusions?|references?|bibliography|"
        r"background|related\s+work|acknowledg\w*|"
        r"appendix|supplement(?:ary\s+\w+)?|summary|preface|"
        r"experimental(?:\s+(?:methods?|procedures?|section))?|"
        r"data\s+availability|author\s+contributions?|"
        r"要約|要旨|序論|緒言|はじめに|序|材料(?:と方法)?|方法|手法|結果(?:と考察)?|考察|結論|参考文献|補足|付録|概要)"
        r"[\s\.:：]*$", re.IGNORECASE)
    is_heading_word = bool(heading_keywords.match(text_stripped))
    # 見出しキーワードは太字/サイズに関係なく最優先で判定
    if is_heading_word and is_short:
        if text_stripped.lower().startswith(("abstract", "要約", "要旨")):
            return "abstract_heading"
        return "heading"
    # キャプションは "Fig. 1." "Figure 6:" "Table S1." のように番号の直後が区切り記号。
    # 本文中の "Fig. 3 shows eight examples..." は番号の後に文が続くので除外する
    # (これを区別しないと Fig で始まる本文がキャプション書式で小さく表示される)。
    # サイズ判定より先に見る: 表のキャプションは小さなセルに囲まれてページ中央値より
    # 大きくなり、そのままではタイトル扱いされてしまうため。
    if _CAPTION_HEAD_RE.match(text_stripped):
        return "caption"
    # タイトル: 大きめ (5%以上) + 太字
    if size_ratio >= 1.05 and is_bold:
        return "title"
    # サイズが大きい (10%以上) だけでタイトル (太字でない場合)
    if size_ratio >= 1.10:
        return "title"
    # subheading: STRICT
    #   太字 + 非常に短い (<=60 char) + 終端句読点なし + 本文動詞パターンを含まない
    # これがないと、稀に短い本文一文 ("The gel was..." 等) が subheading 化する
    if is_bold and is_very_short and not ends_with_period:
        body_verb_re = re.compile(
            r"\b(was|were|is|are|has|have|had|will|would|can|could|may|might|should|"
            r"showed?|shows|reveal(?:ed|s)?|demonstrat(?:ed|es)|found|indicate[ds]?|"
            r"suggest(?:ed|s)?|report(?:ed|s)?|present(?:ed|s)?|describ(?:ed|es)|"
            r"observ(?:ed|es)|analyz(?:ed|es)|perform(?:ed|s)|use[ds]?|"
            r"contain(?:ed|s)?|includ(?:ed|es)|"
            r"do|does|did|being|been|得|示|見|検|測|行|用)\b",
            re.IGNORECASE)
        if not body_verb_re.search(text_stripped):
            return "subheading"
    # 連絡先: "Correspondence:", "Corresponding author:", 本文短めで email 含む
    if re.match(r"^(correspond(ence|ing)|to\s+whom\s+correspondence|\*\s*corresponding)",
                text_stripped, re.IGNORECASE):
        return "contact"
    if is_short and re.search(r"[\w.+-]+@[\w.-]+\.\w+", text_stripped):
        return "contact"
    # 所属機関: 先頭に**上付き数字 (¹²³) または記号 (*†‡§¶)** + 機関系キーワード
    # 通常の Arabic digit "1." "2." で始まるものは除外 (箇条書き本文の誤検出を防ぐ)
    aff_kw = re.compile(
        r"(university|univ\.?|institut[e]?|department|laborator|centre|center|"
        r"college|school|hospital|CNRS|Inserm|Max\s+Planck|大学|研究所|学部|学院|"
        r"UMR|UMS|Faculty|Faculté)", re.IGNORECASE)
    starts_with_aff_marker = bool(re.match(
        r"^([¹²³⁴⁵⁶⁷⁸⁹⁰]+|\*|†|‡|§|¶)\s*[A-Z]", text_stripped))
    if starts_with_aff_marker and aff_kw.search(text_stripped):
        return "affiliation"
    # 著者リスト: 極めて限定的な条件でのみ判定
    # (1) 上付き数字 (¹²³) が 3 つ以上
    # (2) カンマ 2 つ以上
    # (3) 動詞を含まない
    # (4) 大文字始まりトークン (名前候補) が全トークンの 60% 以上
    # (5) 文末が名前 or 上付き数字 (ピリオド終わりは名前リストでない可能性)
    has_supers = len(re.findall(r"[¹²³⁴⁵⁶⁷⁸⁹⁰]", text_stripped)) >= 3
    many_commas = text_stripped.count(",") >= 2
    if has_supers and many_commas and n_chars <= 400:
        body_verb_re = re.compile(
            r"\b(was|were|is|are|has|have|had|will|would|can|could|may|might|"
            r"showed?|shows|reveal(?:ed|s)?|demonstrat(?:ed|es)|found|indicate[ds]?|"
            r"suggest(?:ed|s)?|report(?:ed|s)?|present(?:ed|s)?|describ(?:ed|es)|"
            r"observ(?:ed|es)|analyz(?:ed|es)|perform(?:ed|s)|use[ds]?|"
            r"target(?:ed|s)?|occurs?|occurred|contain(?:ed|s)?|includ(?:ed|es)|"
            r"encode[ds]?|involve[ds]?|form(?:ed|s)?|"
            r"do|does|did|being|been)\b",
            re.IGNORECASE)
        if not body_verb_re.search(text_stripped):
            # トークンを空白/カンマで分割 (上付き数字は除去してから)
            tks = [t for t in re.split(r"[\s,]+",
                    re.sub(r"[¹²³⁴⁵⁶⁷⁸⁹⁰]+", "", text_stripped)) if t]
            if tks:
                cap_count = sum(1 for t in tks if re.match(r"^[A-Z]", t))
                cap_ratio = cap_count / len(tks)
                if cap_ratio >= 0.6:
                    return "authors"
    return None


_LINE_NUMBER_RE = re.compile(r"^\s*\d{1,4}\s*$")


def _compute_page_left_margin(blocks: list[dict]) -> float:
    """本文の左端の中央値を計算する(行番号候補は除外)。"""
    lefts: list[float] = []
    for block in blocks:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            txt = "".join((s.get("text") or "") for s in line.get("spans", []))
            if not txt.strip():
                continue
            # 行番号候補は本文の左端計算から除外
            if _LINE_NUMBER_RE.match(txt):
                continue
            lb = line.get("bbox") or (0, 0, 0, 0)
            lefts.append(lb[0])
    if not lefts:
        return 0.0
    lefts.sort()
    return lefts[len(lefts) // 2]


def _is_line_number(line: dict, body_left_x: float) -> bool:
    """左マージン領域にある純数字行を行番号とみなす。"""
    lb = line.get("bbox") or (0, 0, 0, 0)
    # body の左端より 15pt 以上左にあり、内容が数字のみ
    if lb[0] >= body_left_x - 15:
        return False
    txt = "".join((s.get("text") or "") for s in line.get("spans", []))
    return bool(_LINE_NUMBER_RE.match(txt))


def _group_lines_into_columns(lines: list[dict], page_width: float) -> list[list[dict]]:
    """行を段組みごとに分け、各列を読み順(上→下)に並べて返す。
    1 段組みなら 1 列だけを返す(手元の論文はすべて 1 段組み)。"""
    def _reading_order(ls: list[dict]) -> list[dict]:
        # 注: y を帯でまとめてから x 順に並べる方式も試したが、表の同じ行のセルが
        # 隣接して連結される害が大きかったため採用していない。
        return sorted(ls, key=lambda li: (round(li["bbox"][1], 1), li["bbox"][0]))

    if len(lines) < 10:
        return [_reading_order(lines)]
    xs = sorted(li["bbox"][0] for li in lines)
    clusters: list[list[float]] = [[xs[0]]]
    for x in xs[1:]:
        if x - clusters[-1][-1] <= 30:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    major = [c for c in clusters if len(c) >= max(5, len(lines) * 0.15)]
    if len(major) < 2 or (major[-1][0] - major[0][0]) < page_width * 0.3:
        return [_reading_order(lines)]
    centers = [sum(c) / len(c) for c in major]
    columns: list[list[dict]] = [[] for _ in centers]
    for li in lines:
        x = li["bbox"][0]
        idx = min(range(len(centers)), key=lambda k: abs(centers[k] - x))
        columns[idx].append(li)
    return [_reading_order(col) for col in columns if col]


def _segment_column_into_paragraphs(lines: list[dict]) -> list[list[dict]]:
    """1 列分の行を段落に区切る。

    PyMuPDF のブロック境界は「手がかり」として扱う。行送りの広い投稿原稿では
    PyMuPDF が 1 行ごとにブロックを分けてしまうため、ブロックが変わっても
    (a) 行送りが段落内の通常値、(b) 文字サイズ・太字が同じ、(c) 前の行が右端まで
    埋まっている、(d) 字下げで始まっていない、をすべて満たす場合は段落を続ける。
    """
    if not lines:
        return []
    # 行送りは「異なる y」だけで測る。PyMuPDF が 1 行を単語ごとに分けて返すページが
    # あり、同じ y の断片を含めると中央値が 0 側に引っ張られて誤判定するため。
    ys = sorted({round(li["bbox"][1], 1) for li in lines})
    pitches = [b - a for a, b in zip(ys, ys[1:]) if 0 < b - a < 80]
    med_pitch = statistics.median(pitches) if pitches else 0.0
    max_right = max(li["bbox"][2] for li in lines)
    min_left = min(li["bbox"][0] for li in lines)

    segments: list[list[dict]] = [[lines[0]]]
    for prev, curr in zip(lines, lines[1:]):
        prev_bb = prev["bbox"]
        curr_bb = curr["bbox"]
        gap = max(0.0, curr_bb[1] - prev_bb[3])
        line_h = max(prev_bb[3] - prev_bb[1], curr_bb[3] - curr_bb[1], 6.0)
        pitch = curr_bb[1] - prev_bb[1]
        size_ratio = curr["size"] / max(prev["size"], 0.1)
        size_break = size_ratio >= 1.18 or size_ratio <= 0.85
        prev_short = len(prev["text"].strip()) < 100
        curr_short = len(curr["text"].strip()) < 100
        bold_break = (prev["bold"] != curr["bold"]) and (prev_short or curr_short)
        # 段落内の通常の行送りか (広い行送りの原稿でも中央値を基準に判定できる)
        normal_pitch = med_pitch > 0 and 0 < pitch <= med_pitch * 1.25
        # 前の行が右端よりかなり手前で終わっている = 段落の最終行
        ends_paragraph = prev_bb[2] < max_right - max(24.0, line_h * 1.5)
        indent_break = curr_bb[0] > min_left + 6.0
        # 折り返した続きの行は、前の行と横方向の範囲が重なる。表のセルは列ごとに
        # 左右の位置が離れるので、行送りがたまたま本文と一致しても連結しない。
        overlap = min(prev_bb[2], curr_bb[2]) - max(prev_bb[0], curr_bb[0])
        narrower = min(prev_bb[2] - prev_bb[0], curr_bb[2] - curr_bb[0])
        overlap_ok = narrower <= 0 or overlap > 0.5 * narrower
        # 文章上の手がかり: 前の行が文の途中で終わり、次の行が小文字か開き括弧で
        # 始まっていれば同じ文の続き。参考文献一覧のように行末が揃わない箇所では
        # 座標だけでは段落末と区別できないため、この判定を優先する。
        prev_text = prev["text"].strip()
        curr_text = curr["text"].strip()
        continues = bool(
            not re.search(r"""[.!?:;。！？]['"”’\)\]]?\s*$""", prev_text)
            and re.match(r"^[a-z(\[]", curr_text)
            and (med_pitch <= 0 or 0 < pitch <= med_pitch * 2.2))
        # 数式の断片は本文と必ず分ける (断片どうしの結合は行わない。読み順に並べた
        # 隣接断片をまとめる方式は、上付き・下付きが混ざって順序が乱れたため不採用)。
        if prev.get("is_formula") != curr.get("is_formula"):
            segments.append([curr])
            continue
        # 議事次第などの項目: 前の行が句点で終わり、次の行が「（３）」「１．」のような
        # 全角の項目番号で始まるなら別の項目。全角数字のみを見るので英文には影響しない。
        starts_item = bool(re.match(r"^[（(]?[０-９]{1,2}[）)．、.]", curr_text))
        prev_ends_sentence = bool(re.search(r"[。！？]\s*$", prev_text))
        if prev.get("block_idx") != curr.get("block_idx"):
            # 別ブロック: 段落が続いていると確信できる場合だけ繋ぐ
            keep = (not size_break and not bold_break and overlap_ok
                    and not (prev_ends_sentence and starts_item)
                    and (continues
                         or (normal_pitch and not ends_paragraph and not indent_break)))
            new_para = not keep
        else:
            # 同一ブロック内: 従来どおり行間・サイズ・太字の変化で切る。
            # 行送りによる抑制はここでは行わない (行間がまちまちなページでは中央値が
            # 当てにならず、箇条書きの項目どうしを繋いでしまうため)。広い行送りの
            # 原稿は 1 行ごとに別ブロックになるので、上の分岐で処理される。
            new_para = (gap > 0.55 * line_h) or size_break or bold_break
        if new_para:
            segments.append([curr])
        else:
            segments[-1].append(curr)
    return segments


def _prepare_sub_blocks(page: fitz.Page, zoom: float) -> list[dict]:
    """check.py の extract_paragraphs と厳密に同じアルゴリズム。
    追加で: 行 bbox(rects_px用) + ラベル判定用メタ(size/bold/italic)を保持する。"""
    d = page.get_text("dict")
    all_blocks = d.get("blocks", [])
    # check.py と同一の body_left_x 計算(数字だけの行は除外)
    all_lefts: list[float] = []
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

    result: list[dict] = []
    # ページ全体の行をまとめて集める。段落の組み立ては PyMuPDF のブロック分割では
    # なく、行の座標と行送りから行う (_segment_column_into_paragraphs)。
    page_lines: list[dict] = []
    for pmb_idx, block in enumerate(all_blocks):
        if block.get("type") != 0:
            continue
        # 行の raw text と bbox を集める (check.py と同じフィルタ)
        for line in block.get("lines", []):
            lb = line.get("bbox") or (0, 0, 0, 0)
            txt = "".join((s.get("text") or "") for s in line.get("spans", []))
            if not txt.strip():
                continue
            # check.py と同じ行番号判定
            if lb[0] < body_left_x - 15 and re.match(r"^\s*\d{1,4}\s*$", txt):
                continue
            # ラベル判定用にサイズ・太字・斜体を集計 + spans_info (装飾復元用)
            span_sizes: list[float] = []
            bold_chars = 0; italic_chars = 0; total_chars = 0
            base_size = 10.0
            line_y_mid = (lb[1] + lb[3]) / 2.0
            spans_info_raw: list[dict] = []  # 正規化前の位置情報
            raw_pos = 0
            for span in line.get("spans", []):
                t = span.get("text") or ""
                if not t:
                    continue
                sz = float(span.get("size") or 10)
                styles = _span_styles(span, sz, line_y_mid)
                if t.strip():
                    span_sizes.append(sz)
                    base_size = sz
                    n = len(t)
                    total_chars += n
                    if "bold" in styles: bold_chars += n
                    if "italic" in styles: italic_chars += n
                spans_info_raw.append({
                    "raw_start": raw_pos,
                    "text_raw": t,
                    "styles": styles,
                })
                raw_pos += len(t)
            # 文字数で重み付けした平均サイズ (上付き/下付きの小さな文字が全体を歪めないように)
            # また sub/super とマークされた span は除外
            char_weighted_sum = 0.0
            char_weighted_n = 0
            for span in line.get("spans", []):
                t = span.get("text") or ""
                if not t.strip():
                    continue
                sz = float(span.get("size") or 10)
                st = _span_styles(span, sz, line_y_mid)
                if "sub" in st or "super" in st:
                    continue  # 上付き/下付きは平均から除外
                n = len(t.strip())
                char_weighted_sum += sz * n
                char_weighted_n += n
            if char_weighted_n > 0:
                avg_size = char_weighted_sum / char_weighted_n
            else:
                avg_size = sum(span_sizes) / len(span_sizes) if span_sizes else 10.0
            is_bold = total_chars > 0 and bold_chars / total_chars > 0.6
            is_italic = total_chars > 0 and italic_chars / total_chars > 0.6
            # 数式フォント (Cambria Math など) の割合。独立した数式の断片は 1.0 に
            # 近く、本文中のインライン数式は 0.3 以下に収まる。
            math_chars = sum(
                len(s.get("text") or "") for s in line.get("spans", [])
                if (s.get("text") or "").strip()
                and ("Math" in (s.get("font") or "") or "Symbol" in (s.get("font") or "")))
            page_lines.append({
                "text": txt,
                "bbox": tuple(lb),
                "size": avg_size,
                "bold": is_bold,
                "italic": is_italic,
                "spans_info_raw": spans_info_raw,
                "block_idx": pmb_idx,
                "is_formula": total_chars > 0 and math_chars / total_chars >= 0.6,
            })

    if not page_lines:
        return result

    # 段組みごとに読み順へ並べ、行送り・書体・ブロック境界から段落に区切る
    for column in _group_lines_into_columns(page_lines, page.rect.width):
        for seg_lines in _segment_column_into_paragraphs(column):
            _emit_sub_block(seg_lines, result)

    return result


def _emit_sub_block(line_infos: list[dict], result: list[dict]) -> None:
    if not line_infos:
        return
    # 行を連結する。行末がハイフンで終わり次の行が小文字で始まる場合は、
    # 単語が行で分断されているので空白を入れずに繋ぐ ("small-" + "molecule")。
    parts: list[str] = []
    for li in line_infos:
        t = li["text"]
        if not parts:
            parts.append(t)
            continue
        prev_t = parts[-1].rstrip()
        if (prev_t.endswith("-") and not prev_t.endswith("--")
                and len(prev_t) >= 2 and prev_t[-2].isalpha()
                and re.match(r"^[a-z]", t.lstrip())):
            parts[-1] = prev_t
            parts.append(t.lstrip())
        else:
            parts.append(" " + t)
    joined_raw = "".join(parts)
    joined = re.sub(r"\s+", " ", joined_raw).strip()
    if not joined:
        return

    line_ranges: list = []
    cursor = 0
    for li in line_infos:
        raw_text = li["text"]
        spans_raw = li.get("spans_info_raw", [])
        norm_chars: list[str] = []
        raw_to_norm: list[int | None] = []
        prev_was_space = False
        for c in raw_text:
            is_space = c in " \t\r\n"
            if is_space:
                if prev_was_space:
                    raw_to_norm.append(None)
                    continue
                norm_chars.append(" ")
                raw_to_norm.append(len(norm_chars) - 1)
                prev_was_space = True
            else:
                norm_chars.append(c)
                raw_to_norm.append(len(norm_chars) - 1)
                prev_was_space = False
        norm_full = "".join(norm_chars)
        lead = len(norm_full) - len(norm_full.lstrip())
        trail = len(norm_full) - len(norm_full.rstrip())
        line_norm = norm_full[lead:len(norm_full) - trail]
        def _shift(nidx, _lead=lead, _lnorm=line_norm):
            if nidx is None: return None
            v = nidx - _lead
            if v < 0 or v >= len(_lnorm): return None
            return v
        raw_to_norm_shifted = [_shift(x) for x in raw_to_norm]

        if not line_norm:
            continue
        pos = joined.find(line_norm, cursor)
        if pos < 0:
            pos = joined.find(line_norm[:20], cursor) if len(line_norm) >= 20 else -1
            if pos < 0:
                continue
        end = pos + len(line_norm)

        spans_in_line: list[dict] = []
        for sr in spans_raw:
            s_raw = sr["raw_start"]
            t_raw = sr["text_raw"]
            if not t_raw:
                continue
            norm_positions = []
            for k in range(len(t_raw)):
                ni = raw_to_norm_shifted[s_raw + k] if s_raw + k < len(raw_to_norm_shifted) else None
                if ni is not None:
                    norm_positions.append(ni)
            if not norm_positions:
                continue
            n_start = norm_positions[0]
            n_end = norm_positions[-1] + 1
            span_text_norm = line_norm[n_start:n_end]
            if not span_text_norm.strip():
                continue
            if not sr["styles"]:
                continue
            spans_in_line.append({
                "start": n_start,
                "end": n_end,
                "text": span_text_norm,
                "styles": sr["styles"],
            })

        line_ranges.append((pos, end, li["bbox"], {
            "size": li["size"], "bold": li["bold"], "italic": li["italic"],
        }, spans_in_line))
        cursor = end

    sizes_sorted = sorted(li["size"] for li in line_infos)
    block_base = sizes_sorted[len(sizes_sorted) // 2] if sizes_sorted else 10.0
    b_chars = sum(len(li["text"]) for li in line_infos if li["bold"])
    i_chars = sum(len(li["text"]) for li in line_infos if li["italic"])
    t_chars = sum(len(li["text"]) for li in line_infos)
    sub_meta = {
        "size": block_base,
        "bold": t_chars > 0 and b_chars / t_chars > 0.5,
        "italic": t_chars > 0 and i_chars / t_chars > 0.5,
        "text_plain": joined,
    }
    result.append({
        "joined": joined,
        "line_ranges": line_ranges,
        "sub_base": block_base,
        "sub_meta": sub_meta,
        "is_formula": bool(line_infos) and all(li.get("is_formula") for li in line_infos),
    })


def _apply_span_tags(plain_text: str, spans: list[dict], s_start_offset: int = 0) -> str:
    """文のプレーンテキストに span 装飾タグを適用する。
    spans: [{start, end, styles}] (start/end は文内位置)"""
    if not spans or not plain_text:
        return plain_text
    # 位置ソート + 有効範囲でフィルタ
    valid = []
    tlen = len(plain_text)
    for sp in spans:
        s = sp.get("start", 0)
        e = sp.get("end", 0)
        styles = sp.get("styles", [])
        if not styles: continue
        s = max(0, min(s, tlen))
        e = max(0, min(e, tlen))
        if e <= s: continue
        valid.append((s, e, styles))
    if not valid:
        return plain_text
    valid.sort(key=lambda x: (x[0], x[1]))
    # 重ならないよう、簡易的に順次適用 (重なる場合は先勝ち)
    parts: list[str] = []
    cursor = 0
    for s, e, styles in valid:
        if s < cursor:
            continue  # 重なりスキップ
        if s > cursor:
            parts.append(plain_text[cursor:s])
        parts.append(_wrap_styles(plain_text[s:e], styles))
        cursor = e
    if cursor < tlen:
        parts.append(plain_text[cursor:])
    return "".join(parts)


def _finalize_sentences(sub_blocks_data: list[dict], sentence_ranges_per_sub: list[list[tuple[int, int]]],
                        zoom: float) -> list[dict]:
    """サブブロックと文範囲リストから最終的な sentences 配列を作る。ヒューリスティックラベルも付与。"""
    sentences: list[dict] = []
    global_i = 0
    p_counter = 0
    for sb_idx, (sb, ranges) in enumerate(zip(sub_blocks_data, sentence_ranges_per_sub)):
        joined = sb["joined"]
        line_ranges = sb["line_ranges"]
        sub_base = sb["sub_base"]
        sub_meta = sb["sub_meta"]
        # Debug: page 1 の line_ranges と ranges を表示
        if sb_idx == 0 and len(joined) > 2000:
            print(f"[finalize P1] joined len={len(joined)}, {len(line_ranges)} lines, {len(ranges)} sentence ranges", flush=True)
            for k, lr in enumerate(line_ranges[:8]):
                ls, le = lr[0], lr[1]
                print(f"  line[{k}] ({ls}, {le}) text={joined[ls:le][:60]!r}", flush=True)
            for k, (a, b) in enumerate(ranges[:8]):
                print(f"  range[{k}] ({a}, {b}) text={joined[a:b][:60]!r}", flush=True)
        for s_start, s_end in ranges:
            plain_src = joined[s_start:s_end].strip()
            if not plain_src:
                continue
            rects: list[tuple[float, float, float, float]] = []
            sent_sizes: list[float] = []
            sent_bold_chars = 0; sent_italic_chars = 0; sent_total_chars = 0
            # spans_in_sentence: (start_in_sentence, end_in_sentence, styles)
            sentence_spans: list[dict] = []
            for lr in line_ranges:
                if len(lr) >= 3:
                    ls, le, lbb = lr[0], lr[1], lr[2]
                    line_font = lr[3] if len(lr) >= 4 else None
                    line_spans = lr[4] if len(lr) >= 5 else []
                else:
                    continue
                if le <= s_start or ls >= s_end:
                    continue
                overlap_start = max(s_start, ls)
                overlap_end = min(s_end, le)
                overlap_len = overlap_end - overlap_start
                line_len = le - ls
                if line_len <= 0:
                    continue
                bx0, by0, bx1, by1 = lbb
                line_width = bx1 - bx0
                frac_start = (overlap_start - ls) / line_len
                frac_end = (overlap_end - ls) / line_len
                eff_x0 = bx0 + frac_start * line_width
                eff_x1 = bx0 + frac_end * line_width
                min_w = max(2.0, line_width / max(1, line_len))
                if eff_x1 - eff_x0 < min_w:
                    eff_x1 = eff_x0 + min_w
                rects.append((eff_x0, by0, eff_x1, by1))
                if line_font and overlap_len > 0:
                    sent_sizes.append(line_font.get("size", 10))
                    sent_total_chars += overlap_len
                    if line_font.get("bold"): sent_bold_chars += overlap_len
                    if line_font.get("italic"): sent_italic_chars += overlap_len
                # spans を文内位置に変換
                line_overlap_start_in_line = overlap_start - ls
                line_overlap_end_in_line = overlap_end - ls
                sent_offset_of_this_line = overlap_start - s_start  # 文内での行開始位置
                for si in line_spans:
                    span_s = si["start"]
                    span_e = si["end"]
                    ov_s = max(span_s, line_overlap_start_in_line)
                    ov_e = min(span_e, line_overlap_end_in_line)
                    if ov_e <= ov_s:
                        continue
                    # 文内での位置
                    sent_s = sent_offset_of_this_line + (ov_s - line_overlap_start_in_line)
                    sent_e = sent_s + (ov_e - ov_s)
                    sentence_spans.append({
                        "start": sent_s,
                        "end": sent_e,
                        "styles": si["styles"],
                    })
            if not rects:
                if sb_idx == 0 and len(joined) > 2000:
                    print(f"[finalize P1 DROPPED] range=({s_start},{s_end}) text={src[:50]!r}", flush=True)
                continue
            rects_px = [[round(r[0] * zoom, 1), round(r[1] * zoom, 1),
                         round(r[2] * zoom, 1), round(r[3] * zoom, 1)] for r in rects]
            if sent_sizes:
                sizes_sorted = sorted(sent_sizes)
                s_size = sizes_sorted[len(sizes_sorted) // 2]
                s_bold = sent_total_chars > 0 and sent_bold_chars / sent_total_chars > 0.5
                s_italic = sent_total_chars > 0 and sent_italic_chars / sent_total_chars > 0.5
            else:
                s_size = sub_base
                s_bold = sub_meta.get("bold", False)
                s_italic = sub_meta.get("italic", False)
            sentence_font_size_px = round(s_size * zoom, 1)

            # tagged src を構築: plain_src に sentence_spans の tag を差し込む
            src_tagged = _apply_span_tags(plain_src, sentence_spans, s_start_offset=s_start)
            # プレーンから plain_start はさらに引く: sentence_spans は文内位置 (0-based)
            # を持つので、_apply_span_tags 側で使う

            sentences.append({
                "i": global_i,
                "orig_i": global_i,
                "p": p_counter,
                "src": src_tagged,
                "rects_px": rects_px,
                "ja": "",
                "font_size_px": sentence_font_size_px,
                "_sent_meta": {
                    "size": s_size,
                    "bold": s_bold,
                    "italic": s_italic,
                    "text_plain": plain_src,
                },
                "_sub_meta": sub_meta,
                "_is_formula": sb.get("is_formula", False),
            })
            global_i += 1
        p_counter += 1

    # ページ全体のフォントサイズ中央値で per-sentence ヒューリスティックラベル付与
    all_sizes = sorted(s["font_size_px"] for s in sentences)
    page_median_px = all_sizes[len(all_sizes) // 2] if all_sizes else 20.0
    for s in sentences:
        sent_meta = s.pop("_sent_meta", None)
        s.pop("_sub_meta", None)  # 未使用に
        if s.pop("_is_formula", False):
            s["label"] = "formula"  # 翻訳せず原文のまま表示する
            continue
        if not sent_meta:
            continue
        lbl = _heuristic_label({
            "size": s["font_size_px"],
            "bold": sent_meta["bold"],
            "italic": sent_meta["italic"],
            "text_plain": sent_meta["text_plain"],
        }, page_median_px)
        if lbl:
            s["label"] = lbl
    _spread_caption_label(sentences, page_median_px)
    return _merge_formula_runs(sentences)


def _spread_caption_label(sentences: list[dict], page_median_px: float) -> None:
    """legend 全体に caption ラベルを広げる。

    先頭の "Fig. N." の文にしか caption が付かないため、そのままだと legend の
    1 文目だけが小さい書式になり、残りは本文と同じ書式で表示されてしまう。
    legend は 1 段落なので同じ段落全体に広げる。さらに legend が本文より小さい
    文字で組まれているページでは、同じ大きさで続く段落も legend の続きなので
    取り込む (本文の段落は文字が大きいのでここで止まる)。
    """
    if not sentences:
        return
    order: list[int] = []
    by_para: dict[int, list[dict]] = {}
    for s in sentences:
        p = s.get("p")
        if p not in by_para:
            by_para[p] = []
            order.append(p)
        by_para[p].append(s)

    def para_size(p: int) -> float:
        sizes = sorted(s["font_size_px"] for s in by_para[p])
        return sizes[len(sizes) // 2]

    # 本文の段が占める横幅の目安 (ページ幅そのものは分からないので右端から推定)
    page_w = max((r[2] for s in sentences for r in (s.get("rects_px") or [])),
                 default=1.0)

    for idx, p in enumerate(order):
        if not any(s.get("label") == "caption" for s in by_para[p]):
            continue
        for s in by_para[p]:
            # 同じ段落の中はすべて legend。文字の大きさから付いた title/subheading は
            # 上書きする (表のキャプションは小さなセルに囲まれて大きく見えるため)。
            if s.get("label") in (None, "", "title", "subheading"):
                s["label"] = "caption"
        cap_size = para_size(p)
        # このページの本文の文字の大きさ (ラベルの付いていない段落のうち最大)
        body_sizes = [para_size(q) for q in order
                      if not any(s.get("label") for s in by_para[q])]
        body_size = max(body_sizes) if body_sizes else cap_size
        # 同じ大きさのまま続く段落を legend とみなせるのは、legend が本文より小さい
        # 文字で組まれているページだけ (同じ大きさなら本文と区別する手がかりが無い)
        same_size_ok = cap_size < body_size - 0.5
        for q in order[idx + 1:]:
            if any(s.get("label") for s in by_para[q]):
                break  # 見出しなど別のものが始まった
            qsize = para_size(q)
            if qsize > cap_size + 0.6:
                break  # 文字が大きくなった = 本文に戻った
            if abs(qsize - cap_size) <= 0.6:
                if not same_size_ok:
                    break
            elif _max_rect_width(by_para[q]) < page_w * 0.4:
                # キャプションより小さい文字。横いっぱいに流れる文章なら legend の
                # 補足説明だが、幅の狭い断片の集まりは表のセルなので取り込まない。
                break
            for s in by_para[q]:
                s["label"] = "caption"


def _max_rect_width(sents: list[dict]) -> float:
    """段落の中で一番横に長い行の幅。横いっぱいに流れる文章なら段幅に近くなり、
    表のセルは列の幅までしか広がらない。"""
    return max((r[2] - r[0] for s in sents for r in (s.get("rects_px") or [])),
               default=0.0)


def _union_rect(rects: list) -> tuple | None:
    """[[x0,y0,x1,y1], ...] を包含する矩形を返す。"""
    if not rects:
        return None
    return (min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[2] for r in rects), max(r[3] for r in rects))


def _boxes_near(a: tuple, b: tuple) -> bool:
    """2 つの矩形が同じ数式の一部とみなせる近さか。隙間は重なっていれば負になる。"""
    vgap = max(b[1] - a[3], a[1] - b[3])
    hgap = max(b[0] - a[2], a[0] - b[2])
    h = max(min(a[3] - a[1], b[3] - b[1]), 1.0)
    return vgap <= h * 0.8 and hgap <= h * 1.5


# 数式らしさの目印。これが 1 つも無い断片 (本文中の "coord." など、数式フォントで
# 組まれただけの普通の語) は数式扱いしない。
_MATH_HINT_RE = re.compile(r"[^\x00-\x7f]|[=+*/^<>]")


def _merge_formula_runs(sentences: list[dict]) -> list[dict]:
    """数式の断片を 1 ブロックにまとめる。

    PDF から文字を拾うと、分数の分子・分母、Σ の上下、上付き・下付きがそれぞれ
    別の行として取れるため、1 つの数式が項ごとの断片に割れる。断片の並びは元の
    式の読み順とも一致しないので文字としては組み直せない。ここでは位置が近い
    断片をまとめて 1 ブロックにし、その領域を後で元ページから画像として切り出す
    (attach_formula_images)。断片の間には別の行 ("(Eq. 1)" など) が挟まるので、
    リスト上の並びではなく座標の近さでまとめる。
    """
    # 数式断片を座標でクラスタリング (まとまりが育つと新たに隣接するので収束まで繰り返す)
    clusters: list[dict] = []
    for idx, s in enumerate(sentences):
        box = _union_rect(s.get("rects_px") or [])
        if s.get("label") != "formula" or box is None:
            continue
        clusters.append({"idxs": [idx], "box": box})
    merged_any = True
    while merged_any:
        merged_any = False
        for a in range(len(clusters)):
            for b in range(a + 1, len(clusters)):
                if _boxes_near(clusters[a]["box"], clusters[b]["box"]):
                    ba, bb = clusters[a]["box"], clusters[b]["box"]
                    clusters[a]["box"] = (min(ba[0], bb[0]), min(ba[1], bb[1]),
                                          max(ba[2], bb[2]), max(ba[3], bb[3]))
                    clusters[a]["idxs"] += clusters[b]["idxs"]
                    del clusters[b]
                    merged_any = True
                    break
            if merged_any:
                break

    drop: set[int] = set()
    replace: dict[int, dict] = {}
    for c in clusters:
        idxs = sorted(c["idxs"])
        head = sentences[idxs[0]]
        # src は画像の alt / 検索用に残す (表示は切り出し画像を使う)
        src = " ".join(sentences[k].get("src", "") for k in idxs
                       if sentences[k].get("src"))
        if len(idxs) == 1 and not _MATH_HINT_RE.search(re.sub(r"<[^>]+>", "", src)):
            head.pop("label", None)  # 数式ではなく普通の語 → 本文として扱う
            continue
        merged = dict(head)
        merged["src"] = src
        merged["ja"] = ""
        merged["rects_px"] = [[round(v, 1) for v in c["box"]]]
        replace[idxs[0]] = merged
        drop.update(idxs[1:])
    return [replace.get(i, s) for i, s in enumerate(sentences) if i not in drop]


def attach_formula_images(page: fitz.Page, blocks: list[dict], zoom: float,
                          job_id: str, page_no: int, subdir: str = "formulas") -> None:
    """label=formula のブロックの領域を元ページから切り出し、block['img'] に URL を入れる。
    数式は文字として組み直せないので、見た目そのままの画像として表示する。"""
    targets = [b for b in blocks if b.get("label") == "formula" and b.get("rects_px")]
    if not targets:
        return
    out_dir = JOBS_DIR / job_id / subdir
    out_dir.mkdir(parents=True, exist_ok=True)
    page_rect = page.rect
    for b in targets:
        box = _union_rect(b["rects_px"])
        if box is None:
            continue
        # PDF ポイント座標へ戻し、glyph が切れないよう少し余白を付ける
        pad = FORMULA_CROP_PAD
        x0 = max(page_rect.x0, box[0] / zoom - pad)
        y0 = max(page_rect.y0, box[1] / zoom - pad)
        x1 = min(page_rect.x1, box[2] / zoom + pad)
        y1 = min(page_rect.y1, box[3] / zoom + pad)
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue
        try:
            img = render_page_region_image(page, (x0, y0, x1, y1), dpi=FORMULA_RENDER_DPI)
            name = f"p{page_no:03d}_eq_{b['i']:04d}.png"
            img.save(out_dir / name, "PNG")
            b["img"] = f"/jobs/{job_id}/{subdir}/{name}"
            b["img_w"] = img.width
            b["img_h"] = img.height
        except Exception as e:
            print(f"[formula P{page_no} i={b['i']}] crop failed: {e}", flush=True)


def extract_page_sentences(page: fitz.Page, zoom: float) -> list[dict]:
    """同期版: pysbd で文分割 (プレビュー用の高速パス)。"""
    sub_blocks = _prepare_sub_blocks(page, zoom)
    ranges_per_sub = [_split_sentences_ranges(sb["joined"]) for sb in sub_blocks]
    return _finalize_sentences(sub_blocks, ranges_per_sub, zoom)


async def extract_page_sentences_llm(page: fitz.Page, zoom: float, model: str,
                                     job_id: str | None = None,
                                     page_no: int | None = None) -> list[dict]:
    """非同期版: LLM で文分割 (文脈判断で略語・小数点・URLに強い)。失敗時 pysbd フォールバック。
    ページ画像も VL に渡して視覚レイアウトを活用する (check.py と同挙動)。"""
    sub_blocks = _prepare_sub_blocks(page, zoom)
    if not sub_blocks:
        return []
    texts = [sb["joined"] for sb in sub_blocks]
    # ページ画像を base64 で用意 (VL に視覚情報として渡す)。
    # 構造検出・図番号の特定に送る pNNN.jpg (JPEG 品質 85) と同じバイト列にする:
    # 画像が同一なら LLM サーバーが読み込み済みの画像をキャッシュから再利用でき、その分の読み込みが省ける。
    try:
        image_b64 = _page_jpeg_b64(page)
    except Exception:
        image_b64 = None
    ranges_per_sub = await _llm_split_sentences_batch(
        texts, model, job_id=job_id, page_no=page_no, image_b64=image_b64
    )
    return _finalize_sentences(sub_blocks, ranges_per_sub, zoom)


# 後方互換エイリアス
def extract_page_blocks(page: fitz.Page, zoom: float) -> list[dict]:
    return extract_page_sentences(page, zoom)


def extract_page_figures(doc: fitz.Document, page_index: int) -> list[Image.Image]:
    """埋込ラスター画像を抽出。ベクター図は捕捉できない(→ extract_figure_captions で補完)。"""
    page = doc[page_index]
    figs: list[Image.Image] = []
    for img_info in page.get_images(full=True):
        xref = img_info[0]
        try:
            base = doc.extract_image(xref)
            img = Image.open(io.BytesIO(base["image"]))
            if img.width < MIN_FIGURE_WIDTH or img.height < MIN_FIGURE_HEIGHT:
                continue
            figs.append(img.copy())
        except Exception:
            continue
    return figs


# 図表キャプションの検出は _CAPTION_HEAD_RE と同じ条件 (番号の直後が区切り記号)。
# 空白を許してしまうと "Fig. 3 shows eight examples..." のような本文まで
# キャプションとみなし、その周囲を図として切り出してしまう。
_CAPTION_RE = _CAPTION_HEAD_RE


def _norm_kind(kind: str) -> str:
    """キャプション種別を "table" / "figure" に正規化する。"""
    return "table" if re.match(r"^(table|tab\.?|表)$", (kind or "").strip(), re.I) else "figure"


def extract_figure_captions(page: fitz.Page) -> list[dict]:
    """ページ上のテキストブロックから "Fig. N: ..." 形式のキャプションを検出。
    戻り値: [{kind, number, text, bbox}]"""
    d = page.get_text("dict")
    captions: list[dict] = []
    for block in d.get("blocks", []):
        if block.get("type") != 0:
            continue
        # ブロックの先頭数行のテキストを取得
        head_text = ""
        for line in block.get("lines", [])[:2]:
            for span in line.get("spans", []):
                head_text += (span.get("text") or "")
            head_text += " "
        head_text = head_text.strip()
        m = _CAPTION_RE.match(head_text)
        if m:
            # ブロック全体のテキストを取得(キャプション本文含む)
            full_text = ""
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    full_text += (span.get("text") or "")
                full_text += " "
            captions.append({
                "kind": _norm_kind(m.group(1)),
                "number": m.group(2).lower(),
                "text": full_text.strip(),
                "bbox": tuple(block.get("bbox") or (0, 0, 0, 0)),
            })
    return captions


def render_page_region_image(page: fitz.Page, bbox: tuple,
                              dpi: int = PAGE_RENDER_DPI) -> Image.Image:
    """PDFページの特定領域を画像化。bbox は PDF ポイント座標(x0,y0,x1,y1)。"""
    zoom = dpi / 72.0
    clip = fitz.Rect(*bbox)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False, clip=clip)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def compute_figure_regions(page: fitz.Page, captions: list[dict]) -> list[dict]:
    """キャプション位置を基準に「その上の領域全体」を figure 領域として切り出す。
    各キャプションに対して 1 つの領域を返す(サブパネル a/b/c が個別の埋込画像でも
    一体として一つの図画像になる)。
    戻り値: [{caption, bbox_pdf}] — bbox_pdf は (x0, y0, x1, y1) PDFポイント
    """
    page_rect = page.rect
    if not captions:
        return []
    # 上から下へ並べる
    caps_sorted = sorted(captions, key=lambda c: (c["bbox"][1], c["bbox"][0]))

    # ページ上のテキストブロック bbox (キャプション以外の本文が存在する y 領域を避ける)
    body_rects: list[fitz.Rect] = []
    cap_bboxes_set = {tuple(c["bbox"]) for c in caps_sorted}
    d = page.get_text("dict")
    for block in d.get("blocks", []):
        if block.get("type") != 0:
            continue
        bb = tuple(block.get("bbox") or (0, 0, 0, 0))
        if bb in cap_bboxes_set:
            continue
        # 短いテキストは無視 (行番号など)
        txt = " ".join(
            " ".join((s.get("text") or "") for s in ln.get("spans", []))
            for ln in block.get("lines", [])
        ).strip()
        if len(txt) < 10:
            continue
        body_rects.append(fitz.Rect(bb))

    regions: list[dict] = []
    prev_bottom = 0.0
    for cap in caps_sorted:
        cx0, cy0, cx1, cy1 = cap["bbox"]
        # y0 = 前キャプション or 前本文の bottom
        top_candidates = [prev_bottom]
        for br in body_rects:
            # 本文がキャプションの上にあり、そのキャプションと水平に重なるなら底辺 y1 を候補に
            if br.y1 <= cy0 and br.y0 >= prev_bottom - 1:
                # 水平方向にキャプションと近いか(または左右のカラムに跨るか)
                # 単純: bottom_edge が cy0 未満のブロックはすべて考慮
                top_candidates.append(br.y1)
        # 最も cy0 に近い top を選ぶ(=図領域を最小に抑える)
        fig_y0 = max(top_candidates) + 2
        fig_y1 = cy0 - 2  # キャプション直上まで
        if fig_y1 - fig_y0 < 30:
            # 図領域が薄すぎる場合はキャプション上限を後退させて拡張
            fig_y0 = max(prev_bottom + 2, cy0 - 500)
        # x 範囲は全幅マイナス余白 (カラム分割は難しいので単純化)
        fig_x0 = max(0, page_rect.x0 + 10)
        fig_x1 = page_rect.x1 - 10
        # キャプションが非常に狭い(例: "Fig 1" だけ右下)場合、その左右幅ではなく全幅を使う
        # キャプションが幅広なら図領域は同じ左右幅
        cap_width = cx1 - cx0
        page_width = page_rect.width
        if cap_width >= page_width * 0.4:
            fig_x0 = cx0
            fig_x1 = cx1
        # 図そのもの (画像・ベクター描画) の範囲が分かるならそれに合わせて切り詰める。
        # 2 段組では図が片方の段にしか無いので、全幅で切ると隣の段の本文が入ってしまう。
        content = _graphics_bbox(page, max(prev_bottom, 0.0) + 2, cy0 - 2)
        if content is not None and content[2] - content[0] > 30 and content[3] - content[1] > 30:
            fig_x0 = max(page_rect.x0, content[0] - 6)
            fig_x1 = min(page_rect.x1, content[2] + 6)
            fig_y0 = max(page_rect.y0, prev_bottom + 2, content[1] - 6)
            fig_y1 = min(cy0 - 2, content[3] + 6)
        regions.append({
            "caption": cap,
            "bbox_pdf": (fig_x0, fig_y0, fig_x1, fig_y1),
        })
        prev_bottom = cy1
    return regions


def _graphics_bbox(page: fitz.Page, top: float, bottom: float) -> tuple | None:
    """縦範囲にある図の中身 (埋込画像・ベクター描画) を囲む矩形。
    図に重なる文字 (軸ラベルや凡例) も取り込む。無ければ None。"""
    if bottom - top <= 0:
        return None
    page_area = max(page.rect.width * page.rect.height, 1.0)
    img_rects: list[tuple] = []
    draw_rects: list[tuple] = []
    for img in page.get_images(full=True):
        try:
            for r in page.get_image_rects(img[0]):
                if r.y1 > top and r.y0 < bottom:
                    img_rects.append((r.x0, r.y0, r.x1, r.y1))
        except Exception:
            continue
    for dr in page.get_drawings():
        r = dr["rect"]
        if r.get_area() >= page_area * 0.9:
            continue
        if r.y1 > top and r.y0 < bottom and (r.width > 2 or r.height > 2):
            draw_rects.append((r.x0, r.y0, r.x1, r.y1))
    # 埋込画像は 1 枚でも図とみなせるが、線 1〜2 本だけでは図と言えない
    if img_rects:
        rects = img_rects + draw_rects
    elif len(draw_rects) >= 3:
        rects = draw_rects
    else:
        return None
    box = [min(r[0] for r in rects), max(top, min(r[1] for r in rects)),
           max(r[2] for r in rects), min(bottom, max(r[3] for r in rects))]
    # 図に重なる文字ブロックを取り込む (軸ラベル・凡例)
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        b = block["bbox"]
        if b[3] <= top or b[1] >= bottom:
            continue
        if b[2] <= box[0] or b[0] >= box[2] or b[3] <= box[1] or b[1] >= box[3]:
            continue  # 図に重なっていない
        box[0] = min(box[0], b[0]); box[1] = max(top, min(box[1], b[1]))
        box[2] = max(box[2], b[2]); box[3] = min(bottom, max(box[3], b[3]))
    return tuple(box)


def leading_caption(doc: fitz.Document, page_index: int) -> dict | None:
    """指定ページの冒頭にあるキャプションを返す。無ければ None。
    figure が 1 ページを占有し、その legend が次ページの先頭に置かれている論文で、
    figure ページの番号を legend から取るために使う。"""
    if page_index < 0 or page_index >= len(doc):
        return None
    page = doc[page_index]
    caps = extract_figure_captions(page)
    if not caps:
        return None
    top_cap = min(caps, key=lambda c: c["bbox"][1])
    # ページ上部 (上から 40% 以内) に無ければ「そのページの冒頭の legend」ではない
    if top_cap["bbox"][1] > page.rect.y0 + page.rect.height * 0.4:
        return None
    return top_cap


def find_table_continuations(doc: fitz.Document, page_index: int, parent_box: tuple,
                             max_pages: int = 20) -> list[tuple[int, tuple]]:
    """表が次ページ以降に続いていれば、その (ページ番号, 領域) を並べて返す。
    続きのページはキャプションを持たず、中身がページ先頭から始まり、左右の範囲が
    元の表とほぼ一致する (同じ罫線の幅で組まれているため)。さらに、同じ幅の罫線を
    持つか中身が表らしいことも求める — 本文だけのページを巻き込まないため。"""
    out: list[tuple[int, tuple]] = []
    for j in range(page_index + 1, min(len(doc), page_index + 1 + max_pages)):
        page = doc[j]
        if extract_figure_captions(page):
            break  # 次の図表が始まった
        box = _content_bbox(page, page.rect.y0, page.rect.y1)
        if box is None:
            break
        if box[1] > page.rect.y0 + page.rect.height * 0.25:
            break  # 中身がページ先頭から始まっていない = 表の続きではない
        if abs(box[0] - parent_box[0]) > 20 or abs(box[2] - parent_box[2]) > 20:
            break  # 左右の幅が違う
        rules = _rules_bbox(page)
        same_rules = (rules is not None
                      and abs(rules[0] - parent_box[0]) <= 20
                      and abs(rules[2] - parent_box[2]) <= 20)
        if not same_rules and not _looks_like_table(page, page.rect.y0, page.rect.y1):
            break  # 罫線も表らしさも無い = ただの本文ページ
        out.append((j, (max(page.rect.x0, box[0] - 6), max(page.rect.y0, box[1] - 4),
                        min(page.rect.x1, box[2] + 6), min(page.rect.y1, box[3] + 4))))
    return out


def page_is_mostly_figure(doc: fitz.Document, page_index: int) -> bool:
    """そのページが「図のページ」か。埋込画像があるか、文字が少なくて
    ベクター描画が主体のページ (グラフ 1 枚で 1 ページ) なら True。"""
    if extract_page_figures(doc, page_index):
        return True
    page = doc[page_index]
    if len(page.get_text().strip()) >= 400:
        return False
    return region_has_graphics(page, tuple(page.rect))


def region_has_graphics(page: fitz.Page, bbox: tuple) -> bool:
    """領域に図が入っているか (埋込画像、またはベクター描画)。
    論文の図は写真とは限らず、線や塗りで描かれたグラフのことも多いので、
    埋込画像の有無だけで判定すると取りこぼす。キャプションだけが並ぶ
    「図の説明」ページを図と誤認しないための判定でもある。"""
    rect = fitz.Rect(*bbox)
    if rect.get_area() <= 0:
        return False
    for img in page.get_images(full=True):
        try:
            for r in page.get_image_rects(img[0]):
                if (r & rect).get_area() > rect.get_area() * 0.03:
                    return True
        except Exception:
            continue
    n_draw = 0
    for dr in page.get_drawings():
        r = dr["rect"]
        if r.get_area() >= page.rect.get_area() * 0.9:
            continue  # ページ全面の下地
        ir = r & rect
        if ir.width > 2 or ir.height > 2:
            n_draw += 1
    return n_draw >= 5


def _first_prose_top(page: fitz.Page, top: float, bottom: float) -> float | None:
    """縦範囲の中で最初に現れる「本文の段落」の上端。無ければ None。
    本文の段落 = 長い文章で、ページ幅いっぱいに広がり、列の区切りを持たないもの。"""
    page_w = max(page.rect.width, 1.0)
    for block in sorted((b for b in page.get_text("dict").get("blocks", [])
                         if b.get("type") == 0),
                        key=lambda b: b["bbox"][1]):
        bb = block["bbox"]
        if bb[1] < top or bb[1] >= bottom:
            continue
        line_texts = ["".join((s.get("text") or "") for s in ln.get("spans", []))
                      for ln in block.get("lines", [])]
        txt = " ".join(line_texts).strip()
        if len(txt) < 200 or (bb[2] - bb[0]) < page_w * 0.6:
            continue
        if sum(1 for t in line_texts if len(_COL_GAP_RE.findall(t)) >= 2) >= 2:
            continue  # 桁を空白で揃えた表の中身
        if _table_columns(page, bb[1], bb[3]) >= 3:
            continue  # 列が揃っている = 表の中身
        return bb[1]
    return None


def _rules_bbox(page: fitz.Page) -> tuple | None:
    """ページ上の罫線 (ページ全面の下地は除く) を囲む矩形。無ければ None。"""
    page_area = max(page.rect.width * page.rect.height, 1.0)
    rects = [(d["rect"].x0, d["rect"].y0, d["rect"].x1, d["rect"].y1)
             for d in page.get_drawings()
             if d["rect"].get_area() < page_area * 0.9]
    if not rects:
        return None
    return (min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[2] for r in rects), max(r[3] for r in rects))


def render_table_image(doc: fitz.Document, page_index: int, bbox_pdf: tuple,
                       dpi: int = 180) -> tuple[Image.Image, int]:
    """表の領域を画像化する。次ページ以降に続いていれば縦に繋げて 1 枚にする。
    戻り値: (画像, 繋げた続きページ数)"""
    imgs = [render_page_region_image(doc[page_index], bbox_pdf, dpi=dpi)]
    for j, box in find_table_continuations(doc, page_index, bbox_pdf):
        imgs.append(render_page_region_image(doc[j], box, dpi=dpi))
    if len(imgs) == 1:
        return imgs[0], 0
    width = max(im.width for im in imgs)
    scaled = [im if im.width == width
              else im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)
              for im in imgs]
    gap = 12
    total_h = sum(im.height for im in scaled) + gap * (len(scaled) - 1)
    out = Image.new("RGB", (width, total_h), "white")
    y = 0
    for im in scaled:
        out.paste(im, (0, y))
        y += im.height + gap
    return out, len(imgs) - 1


def _looks_like_table(page: fitz.Page, top: float, bottom: float) -> bool:
    """指定した縦範囲が表の中身らしいか。表はセル (短い文字列) の集まりなのに対し、
    本文は長い段落の集まりになる。本文中の「Supplementary Table S12 と S13 を参照」
    のような文をキャプションと誤検出したときに、その下の本文を表として切り出して
    しまうのを防ぐ。"""
    lens: list[int] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        by0, by1 = block["bbox"][1], block["bbox"][3]
        if by1 <= top or by0 >= bottom:
            continue
        txt = " ".join(
            " ".join((s.get("text") or "") for s in ln.get("spans", []))
            for ln in block.get("lines", [])
        ).strip()
        if txt:
            lens.append(len(txt))
    # 罫線の無い表は 1 ブロックにまとまってしまうので、桁の揃い方でも判定する
    if _table_columns(page, top, bottom) >= 3:
        return True
    if len(lens) < 3:
        return False
    return sum(1 for n in lens if n < 60) / len(lens) >= 0.5


_COL_GAP_RE = re.compile(r"\S {2,}\S")


def _column_gap_lines(page: fitz.Page, top: float, bottom: float) -> int:
    """縦範囲にある行のうち、列の区切り (2 個以上の連続空白) を 2 箇所以上持つ行の数。
    罫線を引かずに空白で桁を揃えた表を見分けるために使う。"""
    n = 0
    for lb, txt in _lines_in(page, top, bottom):
        if len(_COL_GAP_RE.findall(txt)) >= 2:
            n += 1
    return n


def _table_columns(page: fitz.Page, top: float, bottom: float) -> int:
    """縦範囲にある「列」の数 — 複数の段にわたって左端が揃っている位置の数。

    表はセルごとに別々の行として取れ、同じ列のセルは左端が揃う。均等割り付けされた
    本文も単語ごとに行が分かれることがあるが、そちらは単語の位置が段ごとにばらつく
    ので、揃っている位置は左マージンの 1 つだけになる。
    """
    cols: dict[int, set] = {}
    for lb, txt in _lines_in(page, top, bottom):
        if not txt.strip():
            continue
        band = int(round((lb[1] + lb[3]) / 2 / 4.0))  # 4pt 刻みで同じ高さをまとめる
        cols.setdefault(int(round(lb[0] / 8.0)), set()).add(band)
    return sum(1 for bands in cols.values() if len(bands) >= 3)


def _lines_in(page: fitz.Page, top: float, bottom: float):
    """縦範囲にある行を (bbox, テキスト) で返す。"""
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for ln in block.get("lines", []):
            lb = ln.get("bbox") or (0, 0, 0, 0)
            if lb[3] <= top or lb[1] >= bottom:
                continue
            yield lb, "".join((s.get("text") or "") for s in ln.get("spans", []))


def compute_table_regions(page: fitz.Page, captions: list[dict],
                          all_captions: list[dict] | None = None) -> list[dict]:
    """表のキャプションは表の「上」に置かれるので、キャプション直下から
    次のキャプション (または本文の再開・ページ下端) までを表の領域とする。
    表は罫線と文字で描かれ埋込画像を持たないため、図と違って
    extract_page_figures の有無では判定できない。
    戻り値: [{caption, bbox_pdf}] — compute_figure_regions と同じ形式。
    """
    if not captions:
        return []
    page_rect = page.rect
    others = sorted(all_captions if all_captions is not None else captions,
                    key=lambda c: c["bbox"][1])
    regions: list[dict] = []
    for cap in sorted(captions, key=lambda c: c["bbox"][1]):
        cx0, cy0, cx1, cy1 = cap["bbox"]
        # 下端: 次のキャプションの上端、なければページ下端
        bottom = page_rect.y1 - 10
        for oc in others:
            if oc is cap:
                continue
            if oc["bbox"][1] > cy1 + 2:
                bottom = min(bottom, oc["bbox"][1] - 2)
                break
        top = cy1 + 2
        # 表の下に本文の説明文が続く組版があるので、その手前で止める
        prose_top = _first_prose_top(page, top, bottom)
        if prose_top is not None and prose_top - top >= 30:
            bottom = prose_top - 2
        if bottom - top < 30:
            continue  # 表の中身が見当たらない (キャプションだけのページ等)
        if not _looks_like_table(page, top, bottom):
            continue
        # 実際に中身のある範囲まで切り詰める (ページ下部の余白を含めない)
        box = _content_bbox(page, top, bottom)
        if box is None:
            continue
        regions.append({
            "caption": cap,
            "bbox_pdf": (max(page_rect.x0, box[0] - 6), max(page_rect.y0, box[1] - 4),
                         min(page_rect.x1, box[2] + 6), min(page_rect.y1, box[3] + 4)),
        })
    return regions


def _content_bbox(page: fitz.Page, top: float, bottom: float) -> tuple | None:
    """縦範囲 [top, bottom] にある文字と罫線を囲む矩形。何も無ければ None。"""
    page_area = max(page.rect.width * page.rect.height, 1.0)
    rects: list[tuple] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        txt = "".join(
            "".join((s.get("text") or "") for s in ln.get("spans", []))
            for ln in block.get("lines", []))
        if not txt.strip():
            continue
        bb = block["bbox"]
        if bb[3] > top and bb[1] < bottom:
            rects.append(tuple(bb))
    for dr in page.get_drawings():
        r = dr["rect"]
        # ページ全面の下地矩形は無視 (これを含めると常に全面になる)
        if r.get_area() >= page_area * 0.9:
            continue
        if r.y1 > top and r.y0 < bottom:
            rects.append((r.x0, r.y0, r.x1, r.y1))
    if not rects:
        return None
    return (min(r[0] for r in rects), max(top, min(r[1] for r in rects)),
            max(r[2] for r in rects), min(bottom, max(r[3] for r in rects)))


def _to_openai_messages(messages: list[dict]) -> list[dict]:
    """{"content": str, "images": [b64]} 形式を OpenAI 互換の content 配列に変換。"""
    out = []
    for m in messages:
        images = m.get("images") or []
        if not images:
            out.append({"role": m["role"], "content": m["content"]})
            continue
        parts = [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                 for b64 in images]
        parts.append({"type": "text", "text": m["content"]})
        out.append({"role": m["role"], "content": parts})
    return out


def _llm_payload(messages: list[dict], model: str, stream: bool, temperature: float,
                 max_tokens: int | None, format_json: bool, stop: list[str] | None) -> dict:
    payload = {
        "model": model,
        "messages": _to_openai_messages(messages),
        "stream": stream,
        "temperature": temperature,
        # Qwen3.8-Flash-Next は既定で thinking するので明示的に切る
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if format_json:
        payload["response_format"] = {"type": "json_object"}
    if stop:
        payload["stop"] = stop
    return payload


async def llm_chat_stream(
    messages: list[dict],
    model: str = DEFAULT_MODEL,
    timeout: float = 1800.0,
    temperature: float = 0.2,
    max_tokens: int | None = None,
    format_json: bool = False,
    stop: list[str] | None = None,
) -> AsyncIterator[dict]:
    """llama-server /v1/chat/completions をストリーミング呼び出し。
    {"response": 追加テキスト, "done": bool} を順に yield する。"""
    payload = _llm_payload(messages, model, True, temperature, max_tokens, format_json, stop)
    tmo = httpx.Timeout(connect=30.0, read=timeout, write=60.0, pool=None)
    async with httpx.AsyncClient(timeout=tmo) as client:
        async with client.stream("POST", f"{LLM_URL}/v1/chat/completions", json=payload) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    yield {"response": "", "done": True}
                    return
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choice = (obj.get("choices") or [{}])[0]
                yield {
                    "response": (choice.get("delta") or {}).get("content") or "",
                    "done": choice.get("finish_reason") is not None,
                }


async def llm_chat(
    messages: list[dict],
    model: str = DEFAULT_MODEL,
    timeout: float = 600.0,
    temperature: float = 0.2,
    max_tokens: int | None = None,
    format_json: bool = False,
) -> str:
    """非ストリーミング版。応答本文を返す。"""
    payload = _llm_payload(messages, model, False, temperature, max_tokens, format_json, None)
    tmo = httpx.Timeout(connect=30.0, read=timeout, write=60.0, pool=None)
    async with httpx.AsyncClient(timeout=tmo) as c:
        r = await c.post(f"{LLM_URL}/v1/chat/completions", json=payload)
        r.raise_for_status()
        obj = r.json()
    content = ((obj.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    if format_json:
        # response_format を指定しても ```json ... ``` で囲んで返すことがある
        content = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", content)
    return content


SENT_SPLIT_PROMPT = """You are a sentence boundary detector for academic PDFs.

For each block below, copy the block text verbatim and insert the character `|` at every sentence boundary. Do not add or remove any other characters (except the `|` markers). Do not translate. Do not fix typos.

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


# 文境界検出の方式。
#   join (既定): 句読点の位置で候補の断片に切っておき、LLM には「区切りではない候補」の番号だけを答えさせる。
#                出力が数十トークンで済むので、本文を書き写させる copy より速い。
#   copy       : 本文を書き写させて `|` を挿入させる (従来の方式)。
SENT_SPLIT_MODE = os.environ.get("SENT_SPLIT_MODE", "join")

SENT_JOIN_PROMPT = """You are a sentence boundary detector for academic PDFs.

The text below has been cut into numbered pieces at every candidate sentence boundary (after `.`, `!`, `?` and similar). Most cuts are real sentence boundaries. Find the cuts that are NOT real boundaries: the piece must then be joined to the piece before it.

A cut is NOT a real boundary when it falls:
- after an abbreviation (Fig., Figs., Eq., Ref., Sec., Vol., No., pp., Ch., et al., e.g., i.e., etc., vs., approx., ca., Dr., Prof., Mr., Mrs., Ms., St., Inc., Ltd., Co.);
- after a species/name initial (S. sanguinis, E. coli, J. Smith);
- inside a URL, email address, file path, or a number;
- inside a citation such as <sup>15</sup>.
A short heading of 1-3 words that runs into the following text is its own sentence: keep that cut.

Output (STRICT, one line): `JOIN: ` followed by the numbers of the pieces to join to the previous piece, separated by commas, or `JOIN: none`.
Do not write anything else.

Pieces:
{pieces_txt}

Output:
"""

# 新しい図表キャプションの書き出し (Fig. 2 / Figure S1 / Table 3 / Supplementary Figure 4 / 図 5 / 表 1)
_CAPTION_START_RE = re.compile(
    r"(?:supplementary\s+|extended\s+data\s+)?(?:fig(?:ure)?s?\.?|table|図|表)\s*S?\d", re.I)

# 候補の切れ目: 文末記号 (+ 閉じ括弧・引用符・上付き引用) の後の空白、または空白なしで大文字が続く所。全角の文末記号はその直後。
_CAND_CUT_RE = re.compile(
    r"(?<=[.!?])[\"'”’)\]]*(?:<sup>[^<]{0,40}</sup>)?(?:</(?:i|b|sup|sub)>)?(?:\s+|(?=[A-Z]))"
    r"|(?<=[。！？])\s*")


def _candidate_cuts(text: str) -> list[int]:
    """文の開始になりうる位置 (0 を除く) の一覧。"""
    cuts = []
    for m in _CAND_CUT_RE.finditer(text):
        before = text[:m.start()]
        if before.endswith((".", "!", "?")):
            # イニシャル (A. / S.L. / J. Smith) は文末にならないので候補にしない
            if re.search(r"(?:^|[\s(\[.\-])[A-Z]\.$", before):
                continue
            # 空白なしで大文字が続く所は、小文字2文字以上の単語の後だけ (word.Next)
            if not re.search(r"\s", m.group()) and not re.search(r"[a-z]{2}[.!?]$", before):
                continue
        pos = m.end()
        if 0 < pos < len(text) and text[pos:].strip():
            cuts.append(pos)
    return sorted(set(cuts))


def _join_pieces(texts: list[str]) -> list[tuple[int, int, int]]:
    """各ブロックを候補の切れ目で断片に切る。戻り値 (block, start, end) の並び。番号は添字 + 1。"""
    pieces: list[tuple[int, int, int]] = []
    for bi, t in enumerate(texts):
        starts = [0] + _candidate_cuts(t)
        for k, s in enumerate(starts):
            e = starts[k + 1] if k + 1 < len(starts) else len(t)
            pieces.append((bi, s, e))
    return pieces


def _pieces_listing(texts: list[str], pieces: list[tuple[int, int, int]], unit: str) -> str:
    """LLM に見せる断片の一覧。ブロックごとに `=== {unit} N ===` の見出しを付ける。"""
    lines = []
    for n, (bi, s, e) in enumerate(pieces, 1):
        head = f"=== {unit} {bi} ===\n" if s == 0 else ""
        lines.append(f"{head}[{n}] {texts[bi][s:e].strip()}")
    return "\n".join(lines)


def _ranges_from_joins(texts: list[str], pieces: list[tuple[int, int, int]],
                       joins: set[int]) -> list[list[tuple[int, int]]]:
    """断片を文の範囲にまとめる。joins の番号の断片は前の文に結合する。"""
    result: list[list[tuple[int, int]]] = [[] for _ in texts]
    for n, (bi, s, e) in enumerate(pieces, 1):
        if s > 0 and n in joins and result[bi]:
            result[bi][-1] = (result[bi][-1][0], e)  # 前の文に結合
        elif s > 0 and result[bi] and not texts[bi][s:e].strip():
            result[bi][-1] = (result[bi][-1][0], e)  # 空白だけの断片も前に寄せる
        else:
            result[bi].append((s, e))
    return [r or _split_sentences_ranges(t) for r, t in zip(result, texts)]


async def _llm_split_sentences_join(texts: list[str], model: str,
                                    page_no: int | None = None,
                                    image_b64: str | None = None) -> list[list[tuple[int, int]]]:
    """join 方式の文境界検出。各ブロックを候補の断片に切り、LLM が「区切りではない」と答えた切れ目を消す。"""
    pieces = _join_pieces(texts)
    joins: set[int] = set()
    if len(pieces) > len(texts):                     # 候補の切れ目が無ければ LLM に聞かない
        user_msg = {"role": "user",
                    "content": SENT_JOIN_PROMPT.format(pieces_txt=_pieces_listing(texts, pieces, "block"))}
        if image_b64:
            user_msg["images"] = [image_b64]
        llm_start = time.time()
        try:
            raw = await llm_chat([user_msg], model=model, temperature=0.0, max_tokens=400)
        except Exception as e:
            print(f"[sent_split P{page_no}] LLM error: {e}", flush=True)
            return [_split_sentences_ranges(t) for t in texts]
        m = re.search(r"JOIN:\s*(.*)", raw, re.I)
        if not m:
            print(f"[sent_split P{page_no}] unparsable join output: {raw[:200]!r}", flush=True)
            return [_split_sentences_ranges(t) for t in texts]
        joins = {int(x) for x in re.findall(r"\d+", m.group(1))}
        if page_no is not None:
            print(f"[sent_split P{page_no}] {round(time.time() - llm_start, 2)}s (join), "
                  f"{len(pieces)} pieces, join={sorted(joins)}, {len(texts)}blocks", flush=True)
    return _ranges_from_joins(texts, pieces, joins)


# 1 ページの解析 (文境界 + 構造) を 1 回の LLM 呼び出しで行う。
# 文境界検出と構造検出を別々に呼ぶと、ページ画像 (~2,000 トークン) を 2 回読み込むことになる。
# 構造検出で使うのは exclude (ヘッダ・フッタ・ページ番号) と merge (段落の結合) だけなので
# (段落ラベルはヒューリスティックで付ける)、その2つと join をまとめて JSON で答えさせる。
#   PAGE_ANALYSIS=combined (既定) / separate (文境界検出と構造検出を別々に呼ぶ)
PAGE_ANALYSIS = os.environ.get("PAGE_ANALYSIS", "combined")

PAGE_ANALYSIS_PROMPT = """You analyze one page of an academic PDF. The page image is attached. Below, the page text is listed as numbered paragraphs (=== paragraph N ===), automatically extracted. Each paragraph has been cut into numbered pieces [n] at every candidate sentence boundary (after `.`, `!`, `?` and similar).

Answer three things:

1. "join": the numbers of the pieces whose cut before them is NOT a real sentence boundary (the piece must be joined to the piece before it). Most cuts are real boundaries. A cut is NOT a real boundary when it falls:
   - after an abbreviation (Fig., Figs., Eq., Ref., Sec., Vol., No., pp., Ch., et al., e.g., i.e., etc., vs., approx., ca., Dr., Prof., Mr., Mrs., Ms., St., Inc., Ltd., Co.);
   - after a species/name initial (S. sanguinis, E. coli, J. Smith);
   - inside a URL, email address, file path, or a number;
   - inside a citation such as <sup>15</sup>.
   A short heading of 1-3 words that runs into the following text is its own sentence: keep that cut.
2. "exclude": the paragraph numbers that are ONLY a running header, footer or page number (never exclude anything else). Usually [].
3. "merge": pairs [a, b] of ADJACENT paragraph numbers that are visibly one paragraph on the page image but were split. Usually [] - at most 1-2 pairs, never chain three or more.

Output ONLY a JSON object, no explanation, no code fences:
{{"join": [], "exclude": [], "merge": []}}

{pieces_txt}
"""


def _page_jpeg_b64(page: fitz.Page) -> str:
    """ページ画像 (pNNN.jpg と同じ JPEG 品質 85) の base64。"""
    page_img, _z = render_page_image(page)
    buf = io.BytesIO()
    page_img.convert("RGB").save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


async def analyze_page_llm(page: fitz.Page, zoom: float, model: str,
                           page_no: int | None = None) -> tuple[list[dict], dict | None]:
    """文境界と構造 (exclude / merge) を 1 回の LLM 呼び出しで求める。
    戻り値: (文の一覧, 構造の答え)。構造の答えは段落番号 = サブブロック番号 (= 文の "p") で、
    LLM が答えなかった・読めなかった時は None (呼び出し側は構造補正をしない)。"""
    sub_blocks = _prepare_sub_blocks(page, zoom)
    if not sub_blocks:
        return [], None
    texts = [sb["joined"] for sb in sub_blocks]
    pieces = _join_pieces(texts)
    user_msg = {"role": "user",
                "content": PAGE_ANALYSIS_PROMPT.format(pieces_txt=_pieces_listing(texts, pieces, "paragraph"))}
    try:
        user_msg["images"] = [_page_jpeg_b64(page)]
    except Exception:
        pass
    llm_start = time.time()
    obj = None
    try:
        raw = await llm_chat([user_msg], model=model, temperature=0.0, max_tokens=600, format_json=True)
        m = re.search(r"\{.*\}", raw, re.S)
        obj = json.loads(m.group(0)) if m else None
    except Exception as e:
        print(f"[page_analysis P{page_no}] LLM error: {e}", flush=True)
    elapsed = round(time.time() - llm_start, 2)
    if not isinstance(obj, dict):
        print(f"[page_analysis P{page_no}] no usable answer, falling back to pysbd", flush=True)
        return _finalize_sentences(sub_blocks, [_split_sentences_ranges(t) for t in texts], zoom), None
    joins = {int(x) for x in obj.get("join") or [] if str(x).isdigit()}
    print(f"[page_analysis P{page_no}] {elapsed}s, {len(pieces)} pieces, join={sorted(joins)}, "
          f"exclude={obj.get('exclude')}, merge={obj.get('merge')}, {len(texts)} paragraphs", flush=True)
    sentences = _finalize_sentences(sub_blocks, _ranges_from_joins(texts, pieces, joins), zoom)
    obj["_n_para"] = len(sub_blocks)
    obj["_elapsed"] = elapsed
    return sentences, obj


def _find_offsets_from_delimited(vl_content: str, original_text: str) -> list[int]:
    """VL の "|" 区切り出力から原文中の文開始オフセットを求める。
    VL は文と文の境界の空白を "|" で置き換える傾向があるため、単純に "|" を数えるのではなく
    各文の先頭を原文で文字列マッチさせる。"""
    parts = [s for s in vl_content.split("|") if s.strip()]
    offsets: list[int] = []
    search_from = 0
    text_len = len(original_text)
    for idx, part in enumerate(parts):
        stripped_head = part.lstrip()
        if not stripped_head:
            continue
        probe = re.sub(r"\s+", " ", stripped_head[:30]).strip()
        if not probe:
            continue
        # 1: 厳密検索
        pos = original_text.find(probe, search_from)
        if pos < 0:
            # 2: 空白正規化検索 (原文の空白と VL 空白のズレを吸収)
            norm_text = re.sub(r"\s+", " ", original_text[search_from:])
            idx2 = norm_text.find(probe)
            if idx2 >= 0:
                pos = search_from + idx2
        if pos < 0 and len(probe) >= 5:
            # 3: 先頭5文字だけ
            pos = original_text.find(probe[:5], search_from)
        if pos < 0:
            continue
        if idx == 0:
            pos = 0
        if 0 <= pos < text_len:
            offsets.append(pos)
            search_from = pos + max(1, len(probe))
    return offsets


async def _llm_split_sentences_batch(texts: list[str], model: str,
                                     job_id: str | None = None,
                                     page_no: int | None = None,
                                     image_b64: str | None = None) -> list[list[tuple[int, int]]]:
    """複数ブロックの文境界検出。VL に "|" 挿入させ、パーサで原文位置に対応付ける。
    image_b64 (ページ画像) を渡すと視覚レイアウトも考慮してくれる (check.py と同挙動)。
    失敗ブロックは pysbd フォールバック。戻り値: 各ブロックの (start, end) 範囲リスト。"""
    if not texts:
        return []
    if SENT_SPLIT_MODE == "join":
        return await _llm_split_sentences_join(texts, model, page_no=page_no, image_b64=image_b64)
    # check.py と同じ: 全ブロックを ===BLOCK N=== で連結
    blocks_txt = "\n\n".join(f"===BLOCK {i}===\n{t}" for i, t in enumerate(texts))
    prompt = SENT_SPLIT_PROMPT.format(blocks_txt=blocks_txt)
    user_msg = {"role": "user", "content": prompt}
    if image_b64:
        user_msg["images"] = [image_b64]
    buf: list[str] = []
    llm_start = time.time()
    try:
        async for chunk in llm_chat_stream([user_msg], model=model, temperature=0.2):
            if chunk["response"]:
                buf.append(chunk["response"])
            if chunk["done"]:
                break
    except Exception as e:
        print(f"[sent_split P{page_no}] LLM error: {e}", flush=True)
        return [_split_sentences_ranges(t) for t in texts]
    raw = "".join(buf)
    elapsed = round(time.time() - llm_start, 2)
    if page_no is not None:
        print(f"[sent_split P{page_no}] {elapsed}s, {len(raw)}chars, {len(texts)}blocks", flush=True)
        # デバッグ: page 1 だけ入力と出力の全文を出力
        if page_no == 1:
            print(f"[sent_split P1 INPUT joined len={len(texts[0]) if texts else 0}]:\n{texts[0] if texts else ''}\n[END INPUT]", flush=True)
            print(f"[sent_split P1 RAW OUTPUT]:\n{raw}\n[END OUTPUT]", flush=True)

    # ===BLOCK N=== セクション抽出 → 各ブロックの "|" 位置を原文にマップ
    block_re = re.compile(r"===\s*BLOCK\s*(\d+)\s*===\s*\n?(.*?)(?=\n===\s*(?:BLOCK|END)|$)", re.S | re.I)
    parsed_blocks: dict[int, str] = {}
    for m in block_re.finditer(raw):
        try:
            idx = int(m.group(1))
            parsed_blocks[idx] = m.group(2).rstrip()
        except Exception:
            continue

    result: list[list[tuple[int, int]]] = []
    for i, t in enumerate(texts):
        content = parsed_blocks.get(i, "")
        if not content:
            result.append(_split_sentences_ranges(t))
            continue
        offsets = _find_offsets_from_delimited(content, t)
        if page_no == 1 and i == 0:
            parts_dbg = [s for s in content.split("|") if s.strip()]
            print(f"[sent_split P1 PARSER] parts count={len(parts_dbg)}, offsets={offsets}", flush=True)
            for k, p in enumerate(parts_dbg[:20]):
                head = p.lstrip()[:60].replace("\n", "\\n")
                print(f"  part[{k}] head={head!r}", flush=True)
        text_len = len(t)
        clean = sorted(set(o for o in offsets if 0 <= o < text_len))
        if 0 not in clean:
            clean.insert(0, 0)
        ranges: list[tuple[int, int]] = []
        for k in range(len(clean)):
            start = clean[k]
            end = clean[k + 1] if k + 1 < len(clean) else text_len
            if end > start and t[start:end].strip():
                ranges.append((start, end))
        if not ranges:
            ranges = _split_sentences_ranges(t)
        result.append(ranges)
    return result


# 訳文の言語。既定は日本語。
#   script : その言語に特有の文字 (未翻訳の検出に使う)。ラテン文字の言語は None
#   headings : 見出しの訳例 (プロンプトに入れる)
LANGUAGES = {
    "ja": {"name": "日本語", "en": "Japanese",
           "script": r"[ぁ-んァ-ヴ一-龥]",
           "headings": "Abstract → 要旨、Introduction → 序論、Methods → 方法"},
    "en": {"name": "English", "en": "English", "script": None,
           "headings": "keep the original headings (Abstract, Introduction, Methods)"},
    "zh": {"name": "中文（简体）", "en": "Simplified Chinese",
           "script": r"[一-鿿]",
           "headings": "Abstract → 摘要, Introduction → 引言, Methods → 方法"},
    "ko": {"name": "한국어", "en": "Korean",
           "script": r"[가-힯]",
           "headings": "Abstract → 초록, Introduction → 서론, Methods → 방법"},
    "de": {"name": "Deutsch", "en": "German", "script": None,
           "headings": "Abstract → Zusammenfassung, Introduction → Einleitung"},
    "fr": {"name": "Français", "en": "French", "script": None,
           "headings": "Abstract → Résumé, Introduction → Introduction"},
    "es": {"name": "Español", "en": "Spanish", "script": None,
           "headings": "Abstract → Resumen, Introduction → Introducción"},
}
DEFAULT_LANG = "ja"


def lang_info(code: str | None) -> dict:
    return LANGUAGES.get(code or DEFAULT_LANG, LANGUAGES[DEFAULT_LANG])


TRANSLATE_PROMPT = """あなたは学術文書を英語→日本語に翻訳するアシスタントです。以下の入力ブロックを、指示された出力形式で日本語に翻訳してください。

## 翻訳規則
1. 数式・変数・化学式・単位・図表番号(Fig.1, Eq.(2))は原文のまま残す
2. 装飾タグ <b>, <i>, <sup>, <sub> が原文に含まれる場合、訳文の該当語句に同じ形で保持する
   例: <b>Abstract</b> → <b>要旨</b>、<i>S. sanguinis</i> → <i>S. sanguinis</i>、cell<sup>15</sup> → 細胞<sup>15</sup>
3. 固有名詞(人名・機関名・学名 <i>S. sanguinis</i>・製品名等)は原文の綴りをそのまま保持し、必要なら日本語補足を添える(例: 「Robin Anger氏」)
4. 著者リスト・所属機関・連絡先は形式を保ち、翻訳しない部分(人名・メールアドレス・URL・郵便番号)はそのまま残す
5. 見出しは見出しらしく短く訳す (Abstract → 要旨、Introduction → 序論、Methods → 方法)
6. 幻覚禁止: 原文にない情報(数値・年号・化合物名・引用)を追加しない。原文が短くても短い訳を返す
7. 前ページ末尾/次ページ冒頭は文脈参照用で、それ自体は訳出しない

## 出力形式
入力の各 `===INPUT BLOCK N===` に対応して `===BLOCK N===` を1つずつ出力する。前置き・説明・コードフェンスは書かない。

### 具体例
入力:
===INPUT BLOCK 0===
<b>Abstract</b>
===INPUT BLOCK 1===
The compound was heated to 3.14 K. Fig. 1 shows the crystal structure of <i>S. sanguinis</i> ScpH.
===INPUT BLOCK 2===
Robin Anger, Laetitia Pieulle
===INPUT BLOCK 3===
<b>Introduction</b>

出力:
===BLOCK 0===
<b>要旨</b>
===BLOCK 1===
化合物は3.14 Kに加熱された。図1は<i>S. sanguinis</i> ScpHの結晶構造を示す。
===BLOCK 2===
Robin Anger、Laetitia Pieulle
===BLOCK 3===
<b>序論</b>
===END===

## 文脈 (訳出対象外)
--- 前ページ末尾 ---
{prev_tail}
--- 次ページ冒頭 ---
{next_head}

## 翻訳対象
{blocks_txt}
"""

# 日本語以外に訳すときのプロンプト。日本語版 (TRANSLATE_PROMPT) は調整済みなので
# そのまま残し、他言語はこちらを使う。規則は同じで、訳例だけ言語に依存しない形にした。
TRANSLATE_PROMPT_OTHER = """You translate academic documents from the source language into {lang}.
Translate each input block below into {lang}, following the output format exactly.

## Rules
1. Keep formulas, variables, chemical formulas, units and figure/equation numbers (Fig.1, Eq.(2)) as they are
2. Keep the markup tags <b>, <i>, <sup>, <sub> on the corresponding words of the translation
   e.g. <b>Abstract</b> → <b>{abstract}</b>, <i>S. sanguinis</i> → <i>S. sanguinis</i>
3. Keep proper nouns (people, institutions, species names, product names) in their original spelling
4. Keep author lists, affiliations and contacts in the same shape; never translate names, e-mail addresses, URLs or postcodes
5. Translate headings as headings, short: {headings}
6. No hallucination: never add information (numbers, years, compound names, citations) that is not in the source. A short source gets a short translation
7. The previous page tail and next page head are context only — do not translate them
8. Output only {lang}. Do not add explanations or notes

## Output format
For every `===INPUT BLOCK N===` output exactly one `===BLOCK N===`, in order.
No preamble, no explanation, no code fences. Finish with `===END===`.

## Context (do not translate)
--- previous page tail ---
{prev_tail}
--- next page head ---
{next_head}

## To translate
{blocks_txt}
"""

STRUCTURE_PROMPT = """あなたはPDFページのレイアウト解析アシスタントです。添付のページ画像と、下記の段落リストを見比べて、各段落のラベルをJSONで出力してください。

## 必須: labels は全段落を網羅すること
下の段落一覧に含まれる**すべての**indexに対して1つラベルを付与してください。判定できない/普通の本文は "body" を付ける。空にしないこと。

## 使えるラベル
- title            : 論文/文書のタイトル
- authors          : 著者名の並び
- affiliation      : 所属機関・住所
- contact          : メールアドレス・電話・ORCID等
- abstract_heading : "Abstract" "要旨" などの見出し
- abstract_body    : アブストラクト本文
- keywords         : キーワード欄
- heading          : セクション見出し(Introduction, Methods, Results, Discussion, References 等)
- subheading       : サブセクション見出し
- body             : 本文段落
- caption          : 図表のキャプション(Fig. 1: ... 等)
- formula          : 独立した数式ブロック
- table            : 表のセル・内容
- list             : 箇条書き
- footnote         : 脚注
- references       : 参考文献リスト

## 補正 (基本は空 [] で良い)
- exclude: ヘッダ・フッタ・ページ番号のみ (それ以外は絶対に除外しない)
- **merge: 空配列 [] を強く推奨**。段落が既に PyMuPDF により正しく分割されているため、merge は使わない方がよい。もし必要なら「明らかに視覚的に1段落なのに分割されている**隣接する2つ**」のみを最大1〜2ペアだけ指定する。3つ以上を鎖でつなぐな。
- reorder: 空配列 [] を強く推奨。多段組みで明らかに順序が違う場合のみ。

## 絶対にやってはいけないこと
- 全段落を1つに merge するような大量のペアを返さないこと。
- 何も分からないからと全段落を exclude しないこと。
- 上のリストにあるラベルを1つも使わないこと。

## 規則
- 出力は**JSONオブジェクトのみ**。前置きや説明・コードフェンス(```)は書かない。
- indexは文字列キーで、下の一覧の [数字] と一致する。

出力例:
{{"exclude":[],"merge":[],"reorder":[],"labels":{{"0":"title","1":"title","2":"authors","3":"affiliation","4":"contact","5":"abstract_heading","6":"abstract_body","7":"heading","8":"body","9":"body","10":"caption"}}}}

段落一覧(自動抽出、[数字]がindex):
{para_list}
"""

FIGURE_PROMPT = """この画像は論文/資料中の図表です。以下を{lang}で丁寧に説明してください。

1. 図表の種類(グラフ/写真/模式図/表 等)
2. 何を示しているか(軸・凡例・要素)
3. 読み取れる主要な傾向・結論
4. 関連しそうな用語や背景(分かる範囲で)

簡潔かつ具体的に。推測は「〜と思われる」と明示。
"""


_BLOCK_RE = re.compile(r"===\s*BLOCK\s*(\d+)\s*===", re.IGNORECASE)


def _parse_delimited(text: str) -> dict[int, str]:
    """===BLOCK N=== 区切り出力を dict{i: text} にパース。
    ストリーミング途中(===END===未達)でも部分的に取り出せる。"""
    result: dict[int, str] = {}
    matches = list(_BLOCK_RE.finditer(text))
    for idx, m in enumerate(matches):
        try:
            i = int(m.group(1))
        except Exception:
            continue
        start = m.end()
        if idx + 1 < len(matches):
            end = matches[idx + 1].start()
        else:
            # 末尾: ===END=== を探し、無ければテキスト末まで
            end_m = re.search(r"===\s*END\s*===", text[start:], re.IGNORECASE)
            end = start + end_m.start() if end_m else len(text)
        # 未完了(次のBLOCKが来る前で終わっている)場合、末尾ブロックは不完全な可能性あり
        result[i] = text[start:end].strip()
    return result


def _parse_complete_blocks(text: str) -> tuple[dict[int, str], int]:
    """完了したブロックだけを返す(次の ===BLOCK n=== か ===END=== が来たブロックのみ)。
    戻り値: (完了ブロック辞書, 完了ブロック数)"""
    result: dict[int, str] = {}
    matches = list(_BLOCK_RE.finditer(text))
    # 最終マッチより後に別の ===BLOCK=== か ===END=== が来ていれば、そのブロックは完了
    # 「完了」とみなせるのは matches の最後を除く全て + (末尾ブロックが ===END=== を含めば含む)
    end_m = re.search(r"===\s*END\s*===", text, re.IGNORECASE)
    for idx, m in enumerate(matches):
        try:
            i = int(m.group(1))
        except Exception:
            continue
        is_last = (idx == len(matches) - 1)
        start = m.end()
        if not is_last:
            end = matches[idx + 1].start()
            result[i] = text[start:end].strip()
        elif end_m and end_m.start() > start:
            result[i] = text[start:end_m.start()].strip()
        # 最後のブロックで ===END=== がまだ来ていない場合は未完了として除外
    return result, len(result)


def _push(job_id: str, event: dict):
    try:
        JOBS[job_id]["event_queue"].put_nowait(event)
    except (asyncio.QueueFull, KeyError):
        pass


_HALLUCINATION_MARKERS = [
    "(ブロック", "ブロック0の翻訳", "翻訳のみ", "原文の再掲",
    "本研究では、新規の", "有機金属化合物", "空間群 P2", "磁性材料",
]


def _looks_like_hallucination(text: str, src_len: int) -> bool:
    """LLMが幻覚/プロンプトコピーを起こしたかを検出。"""
    if not text:
        return False
    t = text.strip()
    # プレースホルダーテキストの残骸
    for marker in _HALLUCINATION_MARKERS:
        if marker in t:
            return True
    # 原文が短いのに訳文が異常に長い(3倍以上)は幻覚の可能性
    if src_len > 0 and len(t) > src_len * 3 + 100:
        return True
    return False


async def _translate_paragraph(job_id: str, page_no: int,
                               sents: list[dict], prev_tail: str, next_head: str,
                               model: str, lang: str = DEFAULT_LANG
                               ) -> tuple[dict[int, str], float, int]:
    """1段落分の文を翻訳。出力ブロック数が少ないので信頼性が高い。
    b['_merged_src'] があれば src の代わりに使う(ページ跨ぎ文)。"""
    # 翻訳スキップ対象: references / footnote / authors / affiliation / contact
    # (原文のまま ja に入れる)
    _no_translate = {"references", "footnote", "authors", "affiliation", "contact", "formula"}
    for b in sents:
        if not b.get("ja") and b.get("label") in _no_translate:
            b["ja"] = b.get("src", "")
    to_translate = [b for b in sents if not b.get("ja")]
    if not to_translate:
        return {}, 0.0, 0
    parts = []
    used = 0
    total_src_chars = 0
    for b in to_translate:
        s = b.get("_merged_src") or b["src"]
        if len(s) > 2500:
            s = s[:2500] + "…"
        piece = f"===INPUT BLOCK {b['i']}===\n{s}"
        parts.append(piece)
        used += len(piece)
        total_src_chars += len(s)
        if used > MAX_BLOCKS_CHARS:
            break
    blocks_txt = "\n\n".join(parts)
    li = lang_info(lang)
    if (lang or DEFAULT_LANG) == "ja":
        prompt = TRANSLATE_PROMPT.format(
            prev_tail=prev_tail or "(なし)",
            next_head=next_head or "(なし)",
            blocks_txt=blocks_txt,
        )
    else:
        prompt = TRANSLATE_PROMPT_OTHER.format(
            lang=li["en"], headings=li["headings"], abstract="Abstract",
            prev_tail=prev_tail or "(none)",
            next_head=next_head or "(none)",
            blocks_txt=blocks_txt,
        )
    # 出力上限: 入力文字数の 2倍(日本語は英語の 1.2〜1.7倍程度、余裕を持って 2倍)
    # 最小 800 tok、最大 8000 tok で暴走時のセーフティ
    num_predict = min(8000, max(800, int(total_src_chars * 2 / 3)))
    llm_start = time.time()
    buf: list[str] = []
    try:
        async for chunk in llm_chat_stream([{"role": "user", "content": prompt}], model=model,
                                           temperature=0.2, max_tokens=num_predict):
            if chunk["response"]:
                buf.append(chunk["response"])
            if chunk["done"]:
                break
    except Exception as e:
        print(f"[translate P{page_no}] LLM error: {e}", flush=True)
        return {}, 0.0, 0
    raw = "".join(buf)
    elapsed = round(time.time() - llm_start, 2)
    parsed = _parse_delimited(raw)
    result: dict[int, str] = {}
    for b in to_translate:
        i = b["i"]
        ja = parsed.get(i, "")
        if not ja:
            continue
        # 幻覚検出: プレースホルダーコピーや原文と比べて異常に長い出力を弾く
        src_len = len(b.get("_merged_src") or b["src"])
        if _looks_like_hallucination(ja, src_len):
            print(f"[translate P{page_no}] hallucination detected on block i={i}, discarding", flush=True)
            continue
        result[i] = ja
    return result, elapsed, len(raw)


async def _translate_page(job_id: str, page_no: int, blocks: list[dict],
                          prev_tail: str, next_head: str, page_img_b64: str,
                          model: str, lang: str = DEFAULT_LANG
                          ) -> tuple[list[dict], float, int]:
    """段落単位でLLM呼び出しを分割して翻訳。各段落完了ごとに partial emit。
    戻り値: (blocks, elapsed, total_raw_chars)"""
    # 段落 (p フィールド) 単位でグループ化
    groups: dict[int, list[dict]] = {}
    order: list[int] = []
    for b in blocks:
        pid = b.get("p", b["i"])
        if pid not in groups:
            groups[pid] = []
            order.append(pid)
        groups[pid].append(b)

    _push(job_id, {"type": "stage", "page": page_no,
                   "msg": f"LLM翻訳開始 ({len(groups)}段落, {len(blocks)}文)"})
    total_start = time.time()
    total_chars = 0
    all_result: dict[int, str] = {}

    for para_idx, pid in enumerate(order):
        sents = groups[pid]
        pt = prev_tail if para_idx == 0 else ""
        nh = next_head if para_idx == len(order) - 1 else ""
        try:
            res, elapsed, raw_chars = await _translate_paragraph(
                job_id, page_no, sents, pt, nh, model, lang
            )
        except Exception as e:
            msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            _push(job_id, {"type": "error_soft", "page": page_no,
                           "msg": f"段落{para_idx}翻訳エラー: {msg}"})
            res = {}; elapsed = 0.0; raw_chars = 0
        total_chars += raw_chars
        new_blocks = []
        for b in sents:
            i = b["i"]
            if b.get("ja"):
                # 既に翻訳済み(ページ跨ぎで先に付与)は保持
                all_result[i] = b["ja"]
                continue
            if i in res and res[i]:
                all_result[i] = res[i]
                new_blocks.append({"i": i, "ja": res[i]})
            else:
                all_result[i] = "[翻訳失敗]"
                new_blocks.append({"i": i, "ja": all_result[i]})
        if new_blocks:
            _push(job_id, {
                "type": "page_translate_partial",
                "page": page_no,
                "blocks": new_blocks,
                "elapsed": round(time.time() - total_start, 1),
            })
        _push(job_id, {
            "type": "page_translate_progress",
            "page": page_no,
            "chars": total_chars,
            "completed": para_idx + 1,
            "total": len(order),
            "elapsed": round(time.time() - total_start, 1),
        })

    total_elapsed = round(time.time() - total_start, 2)
    return ([{"i": i, "ja": all_result.get(i, "")} for i in range(len(blocks))],
            total_elapsed, total_chars)


def _apply_structure(job_id: str, page_no: int, sentences: list[dict], obj: dict,
                     pids: list[int], new_to_orig: list[int], elapsed: float) -> tuple[list[dict], float]:
    """構造検出の答え (exclude / merge) を文の一覧に反映する。
    pids: 文に現れる段落番号 (p)、new_to_orig: LLM が答えた段落番号 → p の対応。"""
    total_paras = len(pids)
    # LLM 返却の index はリマップした 0-based。orig_pid へ戻す
    def _to_orig(x):
        try:
            n = int(x)
        except Exception:
            return None
        if 0 <= n < len(new_to_orig):
            return new_to_orig[n]
        return None

    exclude = set()
    for x in obj.get("exclude", []) or []:
        op = _to_orig(x)
        if op is not None:
            exclude.add(op)
    if len(exclude) > total_paras * 0.5:
        print(f"[structure P{page_no}] over-exclude ignored: {len(exclude)}/{total_paras}", flush=True)
        exclude = set()

    merge_pairs_raw = obj.get("merge", []) or []
    merge_pairs = []
    if isinstance(merge_pairs_raw, list) and total_paras > 0:
        tmp_parent = {}
        def _find(x):
            while tmp_parent.get(x, x) != x:
                tmp_parent[x] = tmp_parent.get(tmp_parent[x], tmp_parent[x])
                x = tmp_parent[x]
            return x
        def _union(a, b):
            ra, rb = _find(a), _find(b)
            if ra != rb:
                tmp_parent[max(ra, rb)] = min(ra, rb)
        valid_pairs = []
        for pair in merge_pairs_raw:
            if isinstance(pair, list) and len(pair) >= 2:
                a = _to_orig(pair[0]); b = _to_orig(pair[1])
                if a is not None and b is not None:
                    _union(a, b)
                    valid_pairs.append([a, b])
        roots = {_find(p) for p in pids}
        if len(roots) < total_paras * 0.5:
            print(f"[structure P{page_no}] over-merge ignored: paragraphs {total_paras} -> {len(roots)}",
                  flush=True)
            merge_pairs = []
        else:
            merge_pairs = valid_pairs
    labels_raw = obj.get("labels", {}) or {}
    labels: dict[int, str] = {}  # orig_pid -> label
    for k, v in labels_raw.items():
        op = _to_orig(k)
        if op is not None:
            labels[op] = str(v)
    print(f"[structure P{page_no}] resolved: exclude={sorted(exclude)}, merges={merge_pairs}, labels={dict(list(labels.items())[:10])}...", flush=True)

    # 適用: exclude → その段落の全文を落とす
    new_sents = [s for s in sentences if s.get("p", s["i"]) not in exclude]

    # merge → 各マージペアを最も小さいp値に統一
    parent: dict[int, int] = {}
    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            if ra < rb: parent[rb] = ra
            else: parent[ra] = rb
    for pair in merge_pairs:
        if isinstance(pair, list) and len(pair) >= 2:
            try:
                union(int(pair[0]), int(pair[1]))
            except Exception:
                continue
    # VL 構造検出のラベルは 1 PyMuPDF ブロック = 1 段落 の関係で、段落単位ラベルが
    # ブロック内の全文に伝染し誤分類が多いため、per-sentence ヒューリスティックのみを使う。
    for s in new_sents:
        orig_pid = s.get("p", s["i"])
        s["p"] = find(orig_pid)
        # ヒューリスティックラベルはそのまま維持 (見出しキーワード + font 特徴で判定済み)
        # VL labels は無視

    # 出力用に文indexを付け直す(0..N-1)
    for new_i, s in enumerate(new_sents):
        s["i"] = new_i
    _push(job_id, {
        "type": "structure_done", "page": page_no, "elapsed": elapsed,
        "excluded": len(exclude), "merged": len(merge_pairs),
    })
    return new_sents, elapsed


async def _detect_structure(job_id: str, page_no: int, sentences: list[dict],
                            page_img_b64: str, model: str) -> tuple[list[dict], float]:
    """アプローチC: VLに視覚レイアウト補正を依頼。sentences を書き換えて返す。
    戻り値: (補正済 sentences, elapsed)"""
    # 段落単位のプレビューを組み立て
    from collections import defaultdict
    para_map: dict[int, list[dict]] = defaultdict(list)
    for s in sentences:
        para_map[s.get("p", s["i"])].append(s)
    pids = sorted(para_map.keys())
    # LLM に渡すために 0-based 連番へリマップ (元の p は歯抜けや大きい値の場合があるため)
    new_to_orig = pids[:]  # index=new_id, value=orig_pid
    orig_to_new = {p: n for n, p in enumerate(pids)}
    para_list_lines = []
    for new_id, orig_pid in enumerate(pids):
        text = " ".join(s["src"] for s in para_map[orig_pid])
        preview = re.sub(r"<[^>]+>", "", text)
        max_len = 400 if len(pids) <= 20 else 220
        if len(preview) > max_len:
            preview = preview[:max_len] + "…"
        para_list_lines.append(f"[{new_id}] {preview}")
    para_list_txt = "\n".join(para_list_lines)
    print(f"[structure P{page_no}] sentences={len(sentences)}, unique p count={len(pids)}, orig_p sample={pids[:10]}", flush=True)
    prompt = STRUCTURE_PROMPT.format(para_list=para_list_txt)
    messages = [{"role": "user", "content": prompt, "images": [page_img_b64]}]
    _push(job_id, {"type": "structure_start", "page": page_no})
    start = time.time()
    buf: list[str] = []
    async for chunk in llm_chat_stream(messages, model=model):
        piece = chunk.get("response", "")
        if piece:
            buf.append(piece)
        if chunk.get("done"):
            break
    elapsed = round(time.time() - start, 2)
    raw = "".join(buf)
    print(f"[structure P{page_no}] raw({len(raw)}chars):\n{raw}\n[/structure P{page_no}]", flush=True)
    _push(job_id, {"type": "debug", "page": page_no,
                   "kind": "structure_raw", "text": raw[:2000]})
    # JSON パース (最外の {} を貪欲マッチ、失敗したら短いマッチも試す)
    obj = None
    for pat in (r"\{.*\}", r"\{[^{}]*\}"):
        try:
            m = re.search(pat, raw, re.S)
            if m:
                obj = json.loads(m.group(0))
                break
        except Exception:
            continue
    if obj is None:
        _push(job_id, {"type": "debug", "page": page_no,
                       "kind": "structure_parse_fail"})
        return sentences, elapsed
    _push(job_id, {"type": "debug", "page": page_no, "kind": "structure_json",
                   "text": json.dumps(obj, ensure_ascii=False)[:1000]})
    return _apply_structure(job_id, page_no, sentences, obj, pids, new_to_orig, elapsed)


_FIG_ID_PROMPT = """This is an image of a page from an academic paper that contains one or more figures.
Look at the page image and identify which figure number(s) are visible (e.g., "Figure 1", "Fig. 2", "Figure S1", "Supplementary Figure 3", "図 3", "図 S1").
The number label may appear anywhere on the page (top, bottom, corner, or inside the figure).

Output ONLY a JSON object with this exact schema. No markdown fences, no explanation:
{"figures":[{"num":"1"}]}

- "num" is the figure/table number as a string, sub-panel letters removed (e.g., "1" not "1a").
- Preserve the "S" prefix for supplementary figures. Examples: "S1", "S2", "S10".
- If multiple distinct figures are on this page, list them in reading order.
- If no figure number is visible or you cannot determine, return {"figures":[]}.
"""


async def _identify_figure_numbers(page_b64: str, model: str) -> list[str]:
    """1 ページの画像を VL に投げ、そのページに写っている figure 番号を推定する。"""
    messages = [{"role": "user", "content": _FIG_ID_PROMPT, "images": [page_b64]}]
    try:
        raw = await llm_chat(messages, model=model, timeout=180.0, temperature=0.1,
                             max_tokens=300, format_json=True)
        data = json.loads(raw)
        nums: list[str] = []
        for f in data.get("figures", []):
            n = str(f.get("num", "")).strip().lower()
            n = re.sub(r"[^0-9a-z]", "", n)
            if n:
                nums.append(n)
        return nums
    except Exception as e:
        print(f"[fig-id] error: {e}", flush=True)
        return []


def find_translated_legend(job: dict, num: str, kind: str = "figure") -> str | None:
    """処理済みページから 'Figure N:' / 'Table N:' で始まる文を検出し、同一段落 (p) の
    連続する翻訳済み文を結合して返す。翻訳が未完了なら src を使用。
    見つからなければ None。"""
    if not num:
        return None
    word = r"(?:Table|Tab\.?|表)" if kind == "table" else r"(?:Fig(?:ure)?\.?|図)"
    pre = r"(?:supplementary\s+|suppl?\.?\s+|補足\s*|副\s*)"
    sep = r"(?:\s*[.:：|]|\s+(?=(?-i:[A-Z]))|$)"
    n = str(num)
    if n.lower().startswith("s"):
        # 補足の番号。本体側が "Supplementary Figure 3." のように S 無しで書く場合が
        # あるので、前置詞が付いていれば S 無しも同じものとして扱う。
        digits = re.escape(n[1:])
        body = rf"(?:{pre}\s*{word}\s*S?{digits}|{word}\s*S{digits})"
    else:
        body = rf"{pre}?{word}\s*{re.escape(n)}"
    head_re = re.compile(rf"^{body}(?:[a-z])?{sep}", re.IGNORECASE)
    other_head_re = re.compile(rf"^{pre}?{word}\s*S?\d+(?:[a-z])?{sep}", re.IGNORECASE)
    for page in job.get("pages", []):
        blocks = page.get("blocks", [])
        for idx, b in enumerate(blocks):
            src_plain = re.sub(r"<[^>]+>", "", b.get("src", "")).strip()
            if head_re.match(src_plain):
                p_val = b.get("p")
                parts: list[str] = []
                parts.append(_strip_html(b.get("ja") or b.get("src", "")))
                for nb in blocks[idx + 1:]:
                    if nb.get("p") != p_val:
                        break
                    ns = re.sub(r"<[^>]+>", "", nb.get("src", "")).strip()
                    if other_head_re.match(ns):
                        break
                    parts.append(_strip_html(nb.get("ja") or nb.get("src", "")))
                return " ".join(x for x in parts if x).strip()
    return None


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def extract_all_figure_legends(doc: fitz.Document) -> dict[str, str]:
    """ドキュメント全体から "Figure N: caption text" 形式のキャプションを収集。
    Figure legends セクション (図と分離された脚注セクション) を含む全ページを対象。
    戻り値: {"figure:1" / "table:s2" ...: caption_text}
    (Figure 1 と Table 1 は別物なので種別込みのキーにする)"""
    legends: dict[str, str] = {}
    # supplementary 前置詞 + 番号に S を許容 (Fig S1, Supplementary Figure 2, 補足図 S3 等)
    _fig_re = re.compile(
        r"(?:supplementary\s+|suppl?\.?\s+|補足\s*|副\s*)?"
        r"(Fig(?:ure)?s?\.?|Scheme|Table|Tab\.?|図|表)\s*"
        r"(S?\d+[A-Za-z]?)"
        r"\s*[.:：]\s*",
        re.IGNORECASE,
    )
    for page in doc:
        d = page.get_text("dict")
        for block in d.get("blocks", []):
            if block.get("type") != 0:
                continue
            full = " ".join(
                " ".join((s.get("text") or "") for s in ln.get("spans", []))
                for ln in block.get("lines", [])
            ).strip()
            # ブロック内に複数キャプションがあり得る (Figure legends セクション)
            # まず先頭ブロック開始の "Figure N:" を検出
            for m in _fig_re.finditer(full):
                num = m.group(2).lower()
                # base 番号 (数字部分 + 存在すれば先頭 s) をキーに
                base = re.sub(r"[a-z]$", "", num) if not num.startswith("s") else num
                # サブパネル a-z を除去 (S1a → S1)
                if base.startswith("s"):
                    base = "s" + re.sub(r"[a-z]$", "", base[1:])
                start = m.start()
                nxt = _fig_re.search(full, m.end())
                end = nxt.start() if nxt else len(full)
                cap_text = full[start:end].strip()
                key = f"{_norm_kind(m.group(1))}:{base}"
                if key not in legends or len(cap_text) > len(legends[key]):
                    legends[key] = cap_text
    return legends


async def process_pdf(job_id: str, pdf_path: Path, max_pages: int, model: str,
                       preview_page1: dict | None = None):
    job = JOBS[job_id]
    job["status"] = "running"
    job["started_at"] = time.time()
    prefetch_tasks: dict[int, asyncio.Task] = {}     # ページ番号 → テキスト構造抽出の先読みタスク
    try:
        doc = fitz.open(pdf_path)
        total = min(len(doc), max_pages) if max_pages > 0 else len(doc)
        job["total_pages"] = total
        # preview がある場合はそのまま活用(再描画しない)
        job["pages"] = [preview_page1] if preview_page1 else []

        pages_dir = JOBS_DIR / job_id / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)

        # ページの器だけ先に用意する。テキスト構造の抽出 (LLM 文境界検出) は
        # 全ページ分をまとめてやらず、そのページを処理する直前に行う。
        # → 1 ページ目が読めるまでの待ち時間が全ページ分から 2 ページ分に縮む。
        zoom = PAGE_RENDER_DPI / 72.0
        all_pages_meta: list[dict] = []
        for i in range(total):
            page = doc[i]
            meta = {
                "index": i,
                "page_no": i + 1,
                "image_url": None,
                "image_width": int(round(page.rect.width * zoom)),
                "image_height": int(round(page.rect.height * zoom)),
                "blocks": [],
                "figures": [],
                "status": "pending",
                "elapsed": None,
                "llm_elapsed": None,
                "text_len": 0,
            }
            # preview_page1 は画像URL/サイズだけ流用(即時表示用)
            if i == 0 and preview_page1:
                meta["image_url"] = preview_page1["image_url"]
                meta["image_width"] = preview_page1["image_width"]
                meta["image_height"] = preview_page1["image_height"]
            all_pages_meta.append(meta)
        job["pages"] = all_pages_meta

        prescanned: set[int] = set()
        page_structure: dict[int, dict | None] = {}  # PAGE_ANALYSIS=combined: ページ → 構造の答え

        async def ensure_blocks(idx: int) -> None:
            """そのページのテキスト構造を(まだなら)抽出する。"""
            if idx < 0 or idx >= total or idx in prescanned:
                return
            prescanned.add(idx)
            pg = doc[idx]
            _push(job_id, {"type": "stage", "page": idx + 1, "msg": "LLM文境界検出中"})
            try:
                if PAGE_ANALYSIS == "combined":
                    # 文境界と構造を 1 回で。構造の答えはページを処理する時に反映する
                    blocks, page_structure[idx] = await analyze_page_llm(pg, zoom, model, page_no=idx + 1)
                else:
                    blocks = await extract_page_sentences_llm(pg, zoom, model, job_id=job_id,
                                                              page_no=idx + 1)
            except Exception as e:
                print(f"[prescan P{idx+1}] LLM sentence split failed: {e}, falling back to pysbd",
                      flush=True)
                blocks = extract_page_blocks(pg, zoom)
            attach_formula_images(pg, blocks, zoom, job_id, idx + 1)
            all_pages_meta[idx]["blocks"] = blocks
            all_pages_meta[idx]["text_len"] = sum(len(b["src"]) for b in blocks)

        def prefetch_blocks(idx: int) -> None:
            """そのページのテキスト構造の抽出を、まだなら裏で始める。"""
            if 0 <= idx < total and idx not in prefetch_tasks:
                prefetch_tasks[idx] = asyncio.create_task(ensure_blocks(idx))

        async def blocks_ready(idx: int) -> None:
            """そのページのテキスト構造が揃うまで待つ (始まっていなければ始める)。"""
            prefetch_blocks(idx)
            if idx in prefetch_tasks:
                await prefetch_tasks[idx]

        _push(job_id, {"type": "job_meta", "total_pages": total})
        # ドキュメント全体から figure legends (Figure N: ...) を先に収集
        # (レイアウトによっては図と分離された "Figure legends" セクションにキャプションがある)
        try:
            legend_map = extract_all_figure_legends(doc)
            print(f"[legends] collected {len(legend_map)} figure legends: keys={sorted(legend_map.keys())}", flush=True)
        except Exception as e:
            print(f"[legends] error: {e}", flush=True)
            legend_map = {}
        job["_legend_map"] = legend_map
        _save_job_state(job_id)  # ここまでで一覧・再接続ができるように保存

        # ページごとに (構造抽出 → レンダ → indexed 通知 → 翻訳 → 図)。
        # 終わったページから順に読める。
        for i in range(total):
            page = doc[i]
            entry = all_pages_meta[i]
            page_start_ts = time.time()
            img_path = pages_dir / f"p{i+1:03d}.jpg"

            await blocks_ready(i)
            # ページ跨ぎ文の結合と前後文脈に次ページのブロックが要るので 1 ページ先読み。
            # LLM サーバーは 2 本同時に処理できるので、このページの構造検出と並行して進め、
            # 実際に要る所 (ページ跨ぎ文の検出の直前) で待つ。
            prefetch_blocks(i + 1)

            # プレビュー済みのページ1はここで indexed を通知 (画像は既にある)
            if i == 0 and preview_page1:
                _push(job_id, {"type": "page_indexed", "page": entry})
            # ページ1がプレビュー済みならレンダリングをスキップ
            if not (i == 0 and preview_page1):
                _push(job_id, {"type": "stage", "page": i + 1, "msg": "ページ画像レンダリング中"})
                page_img, _z = render_page_image(page)
                page_img.convert("RGB").save(img_path, "JPEG", quality=85)
                entry["image_url"] = f"/jobs/{job_id}/pages/{img_path.name}"
                entry["image_width"] = page_img.width
                entry["image_height"] = page_img.height
                _push(job_id, {"type": "page_indexed", "page": entry})

            entry["status"] = "processing"
            job["current_page"] = i + 1
            _push(job_id, {"type": "page_start", "page": i + 1, "total": total,
                           "blocks": len(entry.get("blocks", [])),
                           "text_len": entry.get("text_len") or sum(len(b.get("src","")) for b in entry.get("blocks",[]))})

            with open(img_path, "rb") as f:
                page_b64 = base64.b64encode(f.read()).decode()

            # 構造検出 (アプローチC): VLに視覚レイアウト補正を依頼
            if entry["blocks"]:
                # デバッグ: 現在の p 分布
                _p_dist = {}
                for _b in entry["blocks"]:
                    _pv = _b.get("p", _b["i"])
                    _p_dist[_pv] = _p_dist.get(_pv, 0) + 1
                print(f"[proc P{i+1}] pre-structure sentences={len(entry['blocks'])}, p_distribution={dict(list(_p_dist.items())[:20])}", flush=True)
                try:
                    if i in page_structure:
                        # 文境界と一緒に答えてもらった構造を反映する (段落番号 = サブブロック番号 = p)
                        sobj = page_structure.pop(i)
                        if sobj is None:
                            corrected, _s_elapsed = entry["blocks"], 0.0
                        else:
                            pids = sorted({b.get("p", b["i"]) for b in entry["blocks"]})
                            corrected, _s_elapsed = _apply_structure(
                                job_id, i + 1, entry["blocks"], sobj, pids,
                                list(range(sobj["_n_para"])), sobj["_elapsed"])
                    else:
                        corrected, _s_elapsed = await _detect_structure(
                            job_id, i + 1, entry["blocks"], page_b64, model
                        )
                    entry["blocks"] = corrected
                    _p_dist2 = {}
                    for _b in corrected:
                        _pv = _b.get("p", _b["i"])
                        _p_dist2[_pv] = _p_dist2.get(_pv, 0) + 1
                    print(f"[proc P{i+1}] post-structure sentences={len(corrected)}, p_distribution={dict(list(_p_dist2.items())[:20])}", flush=True)
                    _push(job_id, {"type": "structure_done", "page": i + 1,
                                   "elapsed": _s_elapsed, "entry": entry})
                except Exception as e:
                    msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                    _push(job_id, {"type": "error_soft", "page": i + 1,
                                   "msg": f"構造検出エラー: {msg} (継続)"})

            # 参考文献セクション検出:
            # - "References" 見出しで開始 (job["_references_started"] = True)
            # - "Acknowledgments / Author Contributions / Competing Interests / Data Availability /
            #    Figure legends / Supplementary Information" 等の見出しで**終了** (False に戻す)
            # References セクション内のブロックのみ references ラベルにして翻訳をスキップ。
            # 直後の Acknowledgments 以降は通常翻訳される。
            _ref_start_re = re.compile(r"^(references?|bibliography|参考文献)[\s\.:：]*$", re.IGNORECASE)
            _ref_end_re = re.compile(
                r"^(acknowledg\w*|author\s+contributions?|competing\s+interests?|"
                r"conflict\s+of\s+interests?|data\s+availability|code\s+availability|"
                r"funding|ethics?\s+statement|"
                r"figure\s+legends?|figure\s+captions?|"
                # 参考文献の後ろに Methods が来る雑誌形式 (Nature 系など)。これが無いと
                # Methods 以降が丸ごと参考文献扱いになり、翻訳されない。
                # 完全一致なので 'Methods 19, 1116-1125 (2022).' のような引用行には反応しない。
                r"methods?(?:\s+and\s+materials?)?|materials?(?:\s+and\s+methods?)?|"
                r"online\s+methods|star\s*methods|"
                r"experimental\s+(?:section|procedures?|methods?)|"
                r"supplement(?:ary)?(?:\s+(?:information|material|data|figures?|tables?))?|"
                r"appendix|附録|補足|謝辞|利益相反|著者貢献|"
                r"方法|材料と方法|実験方法|実験手順)[\s\.:：]*$",
                re.IGNORECASE)
            if entry["blocks"]:
                for k, b in enumerate(entry["blocks"]):
                    plain = re.sub(r"<[^>]+>", "", b.get("src", "")).strip()
                    if job.get("_references_started"):
                        # 終了見出しに当たったら flag OFF
                        if _ref_end_re.match(plain):
                            job["_references_started"] = False
                            # この見出しブロック自身は heading のまま維持、以降は body へ
                            continue
                        # references セクション内 → references ラベル (caption/footnote は保護)
                        if b.get("label") not in ("caption", "footnote"):
                            b["label"] = "references"
                    else:
                        # references 開始?
                        if _ref_start_re.match(plain):
                            job["_references_started"] = True
                            # 見出し自身は heading のまま維持

            # ここから次ページのブロックが要る (並行して進めていた先読みを待つ)
            await blocks_ready(i + 1)

            # 前後の文脈(生テキスト、prescan で取得済)
            def _page_text(idx):
                if idx < 0 or idx >= total:
                    return ""
                return "\n".join(b["src"] for b in all_pages_meta[idx]["blocks"])
            prev_tail = _page_text(i - 1)[-CTX_CHARS:]
            next_head = _page_text(i + 1)[:CTX_CHARS]

            # ページ跨ぎ文の検出: 現ページの末尾文が文末記号で終わっていなければ、
            # 次ページ(まだ構造補正前だが prescan 済)の先頭文と結合して翻訳する。
            # 見出し系ラベル (heading/subheading/title/authors 等) は自然に句読点で終わらないため
            # 結合対象から除外する。次ページの先頭が見出し系の場合も同様。
            _NON_MERGE_LABELS = {
                "title", "heading", "subheading", "abstract_heading",
                "authors", "affiliation", "contact", "keywords",
                "caption", "references", "footnote",
            }
            if entry["blocks"] and i + 1 < total:
                last_sent = entry["blocks"][-1]
                last_label = last_sent.get("label") or "body"
                plain = re.sub(r"<[^>]+>", "", last_sent["src"]).rstrip()
                # 図表のキャプション (レジェンド) も、文の途中で次ページへ続く時は本文と同じく結合する。
                # 「Figure 2 | Overview」のような題だけの行は対象外にするため、
                # 小文字・数字・読点などで終わる (= 明らかに文の途中) 時に限る。
                caption_title = (_CAPTION_START_RE.match(plain) is not None
                                 and len(plain) < 100 and "," not in plain)
                caption_cont = (last_label == "caption" and not caption_title
                                and re.search(r"[a-z0-9,;:(\-–]$", plain) is not None)
                if (plain and plain[-1] not in ".!?。！？"
                        and (last_label not in _NON_MERGE_LABELS or caption_cont)):
                    next_blocks = all_pages_meta[i + 1]["blocks"]
                    if next_blocks:
                        first_next = next_blocks[0]
                        first_label = first_next.get("label") or "body"
                        first_plain = re.sub(r"<[^>]+>", "", first_next["src"]).lstrip()
                        # キャプションの続きは次ページで caption と判定されることがある。
                        # ただし新しい図表のキャプション (Fig. 2 / Table 1 / 図 3 ...) なら続きではない
                        next_ok = ((first_label not in _NON_MERGE_LABELS
                                    or (caption_cont and first_label == "caption"))
                                   and not (caption_cont and _CAPTION_START_RE.match(first_plain)))
                        if next_ok:
                            # 結合文を作成(既存の <sup>/<sub> タグも保持)
                            merged = last_sent["src"].rstrip() + " " + first_next["src"].lstrip()
                            last_sent["_merged_src"] = merged
                            last_sent["_cross_mate_page"] = i + 2
                            last_sent["_cross_mate_orig_i"] = first_next.get("orig_i", first_next["i"])
                            # メイト側にも印を付けておく(取り出し時に使う)
                            first_next["_cross_from_page"] = i + 1
                            first_next["_cross_from_orig_i"] = last_sent.get("orig_i", last_sent["i"])

            # 翻訳 (段落単位で複数回LLM呼び出し)
            try:
                if entry["blocks"]:
                    trans, llm_elapsed, raw_chars = await _translate_page(
                        job_id, i + 1, entry["blocks"], prev_tail, next_head, page_b64,
                        model, job.get("lang", DEFAULT_LANG)
                    )
                    for src_b, out_b in zip(entry["blocks"], trans):
                        # 既にjaがある場合(ページ跨ぎ)は保持
                        if not src_b.get("ja"):
                            src_b["ja"] = out_b["ja"]
                    # タイトル未翻訳の自動リトライ (LLM が verbatim 返す傾向への対策)
                    for src_b in entry["blocks"]:
                        if src_b.get("label") != "title":
                            continue
                        cur_ja = src_b.get("ja") or ""
                        cur_src = src_b.get("src") or ""
                        _lang = job.get("lang", DEFAULT_LANG)
                        _lname = lang_info(_lang)["name"]
                        if not cur_ja or not _looks_untranslated(cur_ja, cur_src, _lang):
                            continue
                        try:
                            new_ja = await _retranslate_sentence(
                                job_id, i + 1, src_b, entry["blocks"], model,
                                extra_hint=(
                                    f"この文は論文タイトルです。**必ず{_lname}に完全に翻訳** してください。"
                                    "学名 (斜体) や固有名詞のみ原綴りを残し、他はすべて訳出すること。"
                                    "原文の単語をそのまま残して出力することは厳禁。"
                                ),
                                lang=_lang,
                            )
                            if new_ja and not _looks_untranslated(new_ja, cur_src, _lang):
                                src_b["ja"] = new_ja
                                _push(job_id, {"type": "page_translate_partial",
                                               "page": i + 1,
                                               "blocks": [{"i": src_b["i"], "ja": new_ja}],
                                               "elapsed": 0})
                                print(f"[title retrans P{i+1}] recovered", flush=True)
                        except Exception as e:
                            print(f"[title retrans P{i+1}] error: {e}", flush=True)
                    entry["llm_elapsed"] = llm_elapsed
                    _push(job_id, {"type": "page_translate_done", "page": i + 1,
                                   "blocks": entry["blocks"], "elapsed": llm_elapsed,
                                   "raw_chars": raw_chars})
                else:
                    _push(job_id, {"type": "stage", "page": i + 1,
                                   "msg": "テキストブロックなし(画像ページの可能性)"})
            except Exception as e:
                msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                for b in entry["blocks"]:
                    b["ja"] = f"[翻訳エラー] {msg}"
                _push(job_id, {"type": "error_soft", "page": i + 1, "msg": msg})

            # ページ跨ぎ: 現ページで翻訳した結合文の ja を次ページのメイトへコピー
            if entry["blocks"] and i + 1 < total:
                for b in entry["blocks"]:
                    mate_page = b.get("_cross_mate_page")
                    mate_orig_i = b.get("_cross_mate_orig_i")
                    if mate_page and mate_orig_i is not None and b.get("ja"):
                        mate_page_idx = mate_page - 1
                        if 0 <= mate_page_idx < total:
                            for mb in all_pages_meta[mate_page_idx]["blocks"]:
                                if mb.get("orig_i") == mate_orig_i:
                                    mb["ja"] = b["ja"]
                                    _push(job_id, {
                                        "type": "page_translate_partial",
                                        "page": mate_page,
                                        "blocks": [{"i": mb["i"], "ja": mb["ja"]}],
                                        "elapsed": 0,
                                    })
                                    _push(job_id, {"type": "stage", "page": mate_page,
                                                    "msg": f"P{i+1} からのページ跨ぎ文を反映"})
                                    break

            # 図登録:
            # ページに埋込画像 (get_images) がある場合のみ「実際の図」があるとみなす。
            # 埋込画像なし かつ 「Figure N:」テキストのみ → figure legends ページなので skip。
            page_captions = extract_figure_captions(page)
            # 小さなロゴ/装飾は除外して有意な埋込画像だけ数える
            # 図ページ判定: 埋込画像、またはベクター描画中心で文字の少ないページ
            has_images = page_is_mostly_figure(doc, i)
            fig_dir = JOBS_DIR / job_id / "figures"
            fig_dir.mkdir(parents=True, exist_ok=True)

            # 図はキャプションの上に実際に図が描かれている場合だけ登録する
            # (埋込画像に限らずベクター描画の図も拾う。図の説明だけが並ぶページは除外)。
            # 表は罫線と文字で描かれるので別扱い。
            table_caps = [c for c in page_captions if c["kind"] == "table"]
            fig_caps = [c for c in page_captions if c["kind"] != "table"]
            fig_regions = [r for r in compute_figure_regions(page, fig_caps)
                           if region_has_graphics(page, r["bbox_pdf"])]
            fig_regions += compute_table_regions(page, table_caps, page_captions)

            for j, reg in enumerate(fig_regions):
                cap = reg["caption"]
                num = cap["number"]
                kind = cap["kind"]
                bbox_pdf = reg["bbox_pdf"]
                fig_path = fig_dir / f"p{i+1:03d}_{'tbl' if kind == 'table' else 'fig'}_{num}.jpg"
                try:
                    if kind == "table":
                        # 次ページ以降に続く表は 1 枚に繋げる
                        img, n_cont = render_table_image(doc, i, bbox_pdf, dpi=180)
                        if n_cont:
                            print(f"[table P{i+1}] {num}: 続き {n_cont} ページを連結", flush=True)
                    else:
                        img = render_page_region_image(page, bbox_pdf, dpi=180)
                    img.save(fig_path, "JPEG", quality=88)
                    fig_url = f"/jobs/{job_id}/figures/{fig_path.name}"
                    # 翻訳済みキャプションを優先 → legend_map (英原文) → 本ページの caption 冒頭
                    base_num = re.sub(r"[a-z]$", "", num)
                    cap_text = (
                        find_translated_legend(job, base_num, kind)
                        or job.get("_legend_map", {}).get(f"{kind}:{base_num}", "")
                        or cap["text"]
                    )
                    fig_entry = {
                        "url": fig_url,
                        "caption": cap_text,
                        "figure_number": num,
                        "kind": kind,
                        "source": "region",
                    }
                    entry["figures"].append(fig_entry)
                    _push(job_id, {"type": "figure_done", "page": i + 1,
                                   "fig_index": len(entry["figures"]),
                                   "url": fig_url,
                                   "caption": cap_text,
                                   "kind": kind,
                                   "figure_number": num})
                except Exception as e:
                    print(f"[fig-region P{i+1}] error for {num}: {e}", flush=True)

            # キャプションが1つも検出されなかったページ (図本体だけが載る図ページ) は
            # VL に番号を識別してもらう。番号が取れれば legend_map の該当キャプションを紐付ける。
            if has_images and not page_captions:
                figs = extract_page_figures(doc, i)
                if figs:
                    # 図が 1 ページを占め、legend が次ページの先頭にある組版では、
                    # その legend から番号が確実に取れる。VL の読み取りより信頼できる
                    # ので先に試す (VL はページ内のパネル記号を番号と誤読しやすい)。
                    next_cap = leading_caption(doc, i + 1)
                    if next_cap is not None and next_cap["kind"] != "table":
                        fig_nums = [next_cap["number"]]
                        print(f"[fig-id P{i+1}] 次ページの legend から: {fig_nums}", flush=True)
                    else:
                        _push(job_id, {"type": "stage", "page": i + 1,
                                       "msg": "図番号を VL 識別中"})
                        fig_nums = await _identify_figure_numbers(page_b64, model)
                        print(f"[fig-id P{i+1}] VL identified: {fig_nums}", flush=True)
                    # このページ全体を 1 枚の図として登録 (VL が返した番号を使用)
                    # 複数の異なる番号 (例: Fig 1 と Fig 2 が同一ページ) の場合は
                    # 別々の figure エントリを登録 (画像はページ全体を共有)
                    unique_nums = []
                    seen = set()
                    for n in fig_nums:
                        # S1a → S1, 1a → 1 のように末尾サブパネル文字だけ落とす
                        # (S 接頭辞は保持)
                        if n.startswith("s"):
                            base = "s" + re.sub(r"[a-z]$", "", n[1:])
                        else:
                            base = re.sub(r"[a-z]$", "", n)
                        if base and base not in seen:
                            seen.add(base)
                            unique_nums.append(base)
                    if not unique_nums:
                        unique_nums = [None]
                    for num in unique_nums:
                        fig_path = fig_dir / f"p{i+1:03d}_vlfig_{num or 'x'}.jpg"
                        # ページ全体を crop なしで使用
                        page_img_src = pages_dir / f"p{i+1:03d}.jpg"
                        try:
                            if page_img_src.exists():
                                fig_path.write_bytes(page_img_src.read_bytes())
                            else:
                                img, _z = render_page_image(page)
                                img.convert("RGB").save(fig_path, "JPEG", quality=85)
                            fig_url = f"/jobs/{job_id}/figures/{fig_path.name}"
                            cap_text = (
                                find_translated_legend(job, num or "")
                                or job.get("_legend_map", {}).get(f"figure:{num}", "")
                            )
                            fig_entry = {
                                "url": fig_url,
                                "caption": cap_text,
                                "figure_number": num,
                                "kind": "figure",
                                "source": "vl-page",
                            }
                            entry["figures"].append(fig_entry)
                            _push(job_id, {"type": "figure_done", "page": i + 1,
                                           "fig_index": len(entry["figures"]),
                                           "url": fig_url,
                                           "caption": cap_text,
                                           "kind": "figure",
                                           "figure_number": num})
                        except Exception as e:
                            print(f"[fig-vl P{i+1}] error for num={num}: {e}", flush=True)

            entry["status"] = "done"
            entry["elapsed"] = round(time.time() - page_start_ts, 2)
            _push(job_id, {"type": "page_done", "page": i + 1, "entry": entry})
            _save_job_state(job_id)  # ページ完了ごとに保存

        job["status"] = "done"
        job["finished_at"] = time.time()
        _push(job_id, {"type": "done"})
        _save_job_state(job_id)
    except asyncio.CancelledError:
        job["status"] = "cancelled"
        job["finished_at"] = time.time()
        _push(job_id, {"type": "cancelled", "message": "ユーザーが中止しました"})
        _save_job_state(job_id)
        return
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        _push(job_id, {"type": "error", "message": str(e)})
        _save_job_state(job_id)
    finally:
        for t in prefetch_tasks.values():           # 中止・エラー時に裏の先読みを残さない
            t.cancel()


@app.post("/api/upload")
async def upload_pdf(
    file: UploadFile = File(...),
    max_pages: int = Form(0),
    model: str = Form(""),
    lang: str = Form(DEFAULT_LANG),
):
    model = model or DEFAULT_MODEL   # 起動中のモデルは実行時に決まる
    lang = lang if lang in LANGUAGES else DEFAULT_LANG
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "PDFファイルのみ受け付けます")
    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = job_dir / "input.pdf"
    with pdf_path.open("wb") as f:
        f.write(await file.read())

    # 実行中ジョブを自動中止
    for _oid, other in list(JOBS.items()):
        if other.get("status") in ("queued", "running"):
            t = other.get("task")
            if t and not t.done():
                t.cancel()
                other["status"] = "cancelling"

    # 即時表示のため、ページ1だけを同期でレンダリング(通常 数百ms)
    preview: dict | None = None
    try:
        doc0 = fitz.open(pdf_path)
        total0 = min(len(doc0), max_pages) if max_pages > 0 else len(doc0)
        pages_dir = job_dir / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        zoom = PAGE_RENDER_DPI / 72.0
        img, _ = render_page_image(doc0[0])
        img_path = pages_dir / "p001.jpg"
        img.convert("RGB").save(img_path, "JPEG", quality=85)
        blocks = extract_page_blocks(doc0[0], zoom)
        attach_formula_images(doc0[0], blocks, zoom, job_id, 1)
        preview = {
            "index": 0, "page_no": 1,
            "image_url": f"/jobs/{job_id}/pages/{img_path.name}",
            "image_width": img.width, "image_height": img.height,
            "blocks": blocks, "figures": [], "status": "pending",
            "elapsed": None, "llm_elapsed": None,
            "text_len": sum(len(b["src"]) for b in blocks),
        }
        doc0.close()
    except Exception as e:
        preview = None

    JOBS[job_id] = {
        "id": job_id,
        "filename": file.filename,
        "status": "queued",
        "created_at": time.time(),
        "event_queue": asyncio.Queue(maxsize=2048),
        "model": model,
        "lang": lang,
        "max_pages": max_pages,
        "total_pages": total0 if preview else 0,
        "pages": [preview] if preview else [],
    }
    task = asyncio.create_task(process_pdf(job_id, pdf_path, max_pages, model, preview_page1=preview))
    JOBS[job_id]["task"] = task
    return {
        "job_id": job_id,
        "pdf_url": f"/jobs/{job_id}/input.pdf",
        "total_pages": total0 if preview else 0,
        "preview_page1": preview,
    }


@app.post("/api/job/{job_id}/upload_supplement")
async def upload_supplement(
    job_id: str,
    file: UploadFile = File(...),
    model: str = Form(""),
):
    model = model or DEFAULT_MODEL   # 起動中のモデルは実行時に決まる
    """既存ジョブにサプリ PDF を追加処理する。
    サプリのページは main_pages の末尾に append され、`is_supplement=True` マークが付く。
    番号衝突を避けるため、キャプション/legend の番号は自動的に "S" プレフィックスを付けて登録。"""
    if job_id not in JOBS:
        raise HTTPException(404, "job not found")
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "PDFファイルのみ受け付けます")
    job = JOBS[job_id]
    job_dir = JOBS_DIR / job_id
    supp_path = job_dir / "supplement.pdf"
    with supp_path.open("wb") as f:
        f.write(await file.read())
    job["supplement_filename"] = file.filename
    # 既存ジョブが running なら待たせる
    main_task = job.get("task")
    if main_task and not main_task.done():
        raise HTTPException(400, "本編処理が実行中です。完了後にサプリを追加してください。")
    # サプリ処理タスクを起動
    task = asyncio.create_task(process_supplement(job_id, supp_path, model))
    job["supplement_task"] = task
    return {"ok": True, "supplement_filename": file.filename}


async def process_supplement(job_id: str, supp_path: Path, model: str):
    """サプリ PDF を処理し、既存ジョブの pages に追加登録する。"""
    job = JOBS[job_id]
    prev_status = job.get("status")
    job["status"] = "running"
    job["supplement_status"] = "processing"
    try:
        doc = fitz.open(supp_path)
        total = len(doc)
        # 既にサプリを取り込んだジョブに入れ直したときは、前回分を捨てて差し替える
        # (そのまま追加するとページが二重に増える)
        if any(p.get("is_supplement") for p in job.get("pages", [])):
            job["pages"] = [p for p in job["pages"] if not p.get("is_supplement")]
            print(f"[supp] 前回のサプリ {len(job.get('pages', []))} ページ目以降を差し替え", flush=True)
        main_pages_count = len(job.get("pages", []))
        _push(job_id, {"type": "phase", "phase": "supplement",
                       "msg": f"サプリ {total} ページを解析中", "supp_total": total})
        pages_dir = JOBS_DIR / job_id / "supp_pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        zoom = PAGE_RENDER_DPI / 72.0

        # ページの器だけ用意。テキスト構造の抽出は処理直前に行う (本編と同じ)
        supp_pages_meta: list[dict] = []
        for i in range(total):
            page = doc[i]
            meta = {
                "index": main_pages_count + i,
                "page_no": main_pages_count + i + 1,
                "image_url": None,
                "image_width": int(round(page.rect.width * zoom)),
                "image_height": int(round(page.rect.height * zoom)),
                "blocks": [],
                "figures": [],
                "status": "pending",
                "elapsed": None,
                "llm_elapsed": None,
                "text_len": 0,
                "is_supplement": True,
            }
            supp_pages_meta.append(meta)
            job["pages"].append(meta)
        job["total_pages"] = main_pages_count + total
        _push(job_id, {"type": "job_meta", "total_pages": job["total_pages"]})

        prescanned: set[int] = set()

        async def ensure_blocks(idx: int) -> None:
            if idx < 0 or idx >= total or idx in prescanned:
                return
            prescanned.add(idx)
            pg = doc[idx]
            _push(job_id, {"type": "stage", "page": main_pages_count + idx + 1,
                           "msg": f"サプリ P{idx+1} LLM 文境界検出"})
            try:
                blocks = await extract_page_sentences_llm(
                    pg, zoom, model, job_id=job_id, page_no=main_pages_count + idx + 1)
            except Exception as e:
                print(f"[supp prescan P{idx+1}] LLM split failed: {e}", flush=True)
                blocks = extract_page_blocks(pg, zoom)
            attach_formula_images(pg, blocks, zoom, job_id,
                                  main_pages_count + idx + 1, subdir="supp_formulas")
            supp_pages_meta[idx]["blocks"] = blocks
            supp_pages_meta[idx]["text_len"] = sum(len(b["src"]) for b in blocks)

        # 補足 legend map (Figure S1: ... 等) を全ページ横断で収集し、既存の _legend_map にマージ
        try:
            supp_legends = extract_all_figure_legends(doc)
            print(f"[supp legends] collected: {sorted(supp_legends.keys())}", flush=True)
            legend_map = job.get("_legend_map", {})
            for k, v in supp_legends.items():
                # サプリで番号 "1" のキャプションは強制的に "s1" 扱いにする (本編と衝突しないように)
                kind, _, num = k.partition(":")
                k2 = f"{kind}:{num if num.startswith('s') else 's' + num}"
                # 既存より長ければ更新
                if k2 not in legend_map or len(v) > len(legend_map[k2]):
                    legend_map[k2] = v
            job["_legend_map"] = legend_map
        except Exception as e:
            print(f"[supp legends] error: {e}", flush=True)

        for i in range(total):
            page = doc[i]
            page_no = main_pages_count + i + 1
            entry = supp_pages_meta[i]
            entry["status"] = "processing"
            page_start_ts = time.time()

            await ensure_blocks(i)
            await ensure_blocks(i + 1)  # 前後文脈用に 1 ページ先読み

            # ページ画像
            _push(job_id, {"type": "stage", "page": page_no, "msg": "サプリ画像レンダリング"})
            page_img, _z = render_page_image(page)
            img_path = pages_dir / f"p{i+1:03d}.jpg"
            page_img.convert("RGB").save(img_path, "JPEG", quality=85)
            entry["image_url"] = f"/jobs/{job_id}/supp_pages/{img_path.name}"
            entry["image_width"] = page_img.width
            entry["image_height"] = page_img.height
            _push(job_id, {"type": "page_indexed", "page": entry})

            _push(job_id, {"type": "page_start", "page": page_no, "total": job["total_pages"],
                           "blocks": len(entry.get("blocks", [])),
                           "text_len": entry.get("text_len") or 0,
                           "supp_page": i + 1, "supp_total": total})

            with open(img_path, "rb") as f:
                page_b64 = base64.b64encode(f.read()).decode()

            # 構造検出
            if entry["blocks"]:
                try:
                    corrected, _s_elapsed = await _detect_structure(
                        job_id, page_no, entry["blocks"], page_b64, model
                    )
                    entry["blocks"] = corrected
                    _push(job_id, {"type": "structure_done", "page": page_no,
                                   "elapsed": _s_elapsed, "entry": entry})
                except Exception as e:
                    print(f"[supp struct P{i+1}] error: {e}", flush=True)

            # ラベル修正: caption は全て supplementary caption 扱いにする (S プレフィックス強制)
            for b in entry["blocks"]:
                src_plain = re.sub(r"<[^>]+>", "", b.get("src", "")).strip()
                # "Figure 1:" → "Figure S1:" 相当のマッピングを保持するためだけに使う (表示は変えない)
                # サプリの本文中に「Fig 1」と書いていても実質「Fig S1」を指すことが多い

            # 翻訳
            try:
                if entry["blocks"]:
                    def _page_text(idx):
                        if idx < 0 or idx >= len(job["pages"]):
                            return ""
                        return "\n".join(b["src"] for b in job["pages"][idx]["blocks"])
                    prev_tail = _page_text(page_no - 2)[-CTX_CHARS:]
                    next_head = _page_text(page_no)[:CTX_CHARS]
                    trans, llm_elapsed, raw_chars = await _translate_page(
                        job_id, page_no, entry["blocks"], prev_tail, next_head, page_b64,
                        model, job.get("lang", DEFAULT_LANG)
                    )
                    for src_b, out_b in zip(entry["blocks"], trans):
                        if not src_b.get("ja"):
                            src_b["ja"] = out_b["ja"]
                    entry["llm_elapsed"] = llm_elapsed
                    _push(job_id, {"type": "page_translate_done", "page": page_no,
                                   "blocks": entry["blocks"], "elapsed": llm_elapsed,
                                   "raw_chars": raw_chars})
            except Exception as e:
                msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                for b in entry["blocks"]:
                    b["ja"] = f"[翻訳エラー] {msg}"
                _push(job_id, {"type": "error_soft", "page": page_no, "msg": msg})

            # 図処理: サプリ内の figure は必ず "S" プレフィックス番号として登録
            page_captions = extract_figure_captions(page)
            # 図ページ判定: 埋込画像、またはベクター描画中心で文字の少ないページ
            has_images = page_is_mostly_figure(doc, i)
            fig_dir = JOBS_DIR / job_id / "figures"
            fig_dir.mkdir(parents=True, exist_ok=True)

            # 本編と同じ扱い (図は実際に描かれている領域だけ、表は別判定)
            table_caps = [c for c in page_captions if c["kind"] == "table"]
            fig_caps = [c for c in page_captions if c["kind"] != "table"]
            fig_regions = [r for r in compute_figure_regions(page, fig_caps)
                           if region_has_graphics(page, r["bbox_pdf"])]
            fig_regions += compute_table_regions(page, table_caps, page_captions)

            for j, reg in enumerate(fig_regions):
                cap = reg["caption"]
                num_raw = cap["number"]
                kind = cap["kind"]
                # "S" 接頭辞を強制付加 (既に付いていれば維持)
                if not num_raw.lower().startswith("s"):
                    num = "s" + num_raw
                else:
                    num = num_raw
                bbox_pdf = reg["bbox_pdf"]
                fig_path = fig_dir / f"supp_p{i+1:03d}_{'tbl' if kind == 'table' else 'fig'}_{num}.jpg"
                try:
                    if kind == "table":
                        # 次ページ以降に続く表は 1 枚に繋げる
                        img, n_cont = render_table_image(doc, i, bbox_pdf, dpi=180)
                        if n_cont:
                            print(f"[supp-table P{i+1}] {num}: 続き {n_cont} ページを連結", flush=True)
                    else:
                        img = render_page_region_image(page, bbox_pdf, dpi=180)
                    img.save(fig_path, "JPEG", quality=88)
                    fig_url = f"/jobs/{job_id}/figures/{fig_path.name}"
                    base_num = "s" + re.sub(r"[a-z]$", "", num[1:])
                    cap_text = (
                        find_translated_legend(job, base_num, kind)
                        or job.get("_legend_map", {}).get(f"{kind}:{base_num}", "")
                        or cap["text"]
                    )
                    fig_entry = {
                        "url": fig_url, "caption": cap_text,
                        "figure_number": num, "kind": kind,
                        "source": "supp-region",
                    }
                    entry["figures"].append(fig_entry)
                    _push(job_id, {"type": "figure_done", "page": page_no,
                                   "fig_index": len(entry["figures"]),
                                   "url": fig_url, "caption": cap_text,
                                   "kind": kind, "figure_number": num})
                except Exception as e:
                    print(f"[supp-fig P{i+1}] error for {num}: {e}", flush=True)

            # キャプション無し画像ページは VL で番号識別
            if has_images and not page_captions:
                # 次ページ先頭の legend から番号が取れるならそちらを優先 (本編と同じ)
                next_cap = leading_caption(doc, i + 1)
                if next_cap is not None and next_cap["kind"] != "table":
                    fig_nums = [next_cap["number"]]
                    print(f"[supp fig-id P{i+1}] 次ページの legend から: {fig_nums}", flush=True)
                else:
                    _push(job_id, {"type": "stage", "page": page_no, "msg": "サプリ図番号を VL 識別中"})
                    fig_nums = await _identify_figure_numbers(page_b64, model)
                    print(f"[supp fig-id P{i+1}] VL: {fig_nums}", flush=True)
                unique_nums = []
                seen = set()
                for n in fig_nums:
                    # S プレフィックスを強制
                    n_clean = n if n.startswith("s") else "s" + n
                    if n_clean.startswith("s"):
                        base = "s" + re.sub(r"[a-z]$", "", n_clean[1:])
                    else:
                        base = re.sub(r"[a-z]$", "", n_clean)
                    if base and base not in seen:
                        seen.add(base)
                        unique_nums.append(base)
                if not unique_nums:
                    unique_nums = [None]
                for num in unique_nums:
                    fig_path = fig_dir / f"supp_p{i+1:03d}_vl_{num or 'x'}.jpg"
                    try:
                        fig_path.write_bytes(img_path.read_bytes())
                        fig_url = f"/jobs/{job_id}/figures/{fig_path.name}"
                        cap_text = (
                            find_translated_legend(job, num or "")
                            or job.get("_legend_map", {}).get(f"figure:{num}", "")
                        )
                        fig_entry = {
                            "url": fig_url, "caption": cap_text,
                            "figure_number": num, "kind": "figure",
                            "source": "supp-vl-page",
                        }
                        entry["figures"].append(fig_entry)
                        _push(job_id, {"type": "figure_done", "page": page_no,
                                       "fig_index": len(entry["figures"]),
                                       "url": fig_url, "caption": cap_text,
                                       "kind": "figure", "figure_number": num})
                    except Exception as e:
                        print(f"[supp-fig-vl P{i+1}] error for num={num}: {e}", flush=True)

            entry["status"] = "done"
            entry["elapsed"] = round(time.time() - page_start_ts, 2)
            _push(job_id, {"type": "page_done", "page": page_no, "entry": entry,
                           "supp_page": i + 1, "supp_total": total})
            _save_job_state(job_id)

        job["supplement_status"] = "done"
        job["status"] = prev_status if prev_status == "done" else "done"
        _push(job_id, {"type": "supplement_done", "total_pages": job["total_pages"]})
        _save_job_state(job_id)
    except asyncio.CancelledError:
        job["supplement_status"] = "cancelled"
        _finish_supplement(job, prev_status)
        _push(job_id, {"type": "cancelled", "message": "サプリ処理を中止しました"})
        _save_job_state(job_id)
    except Exception as e:
        job["supplement_status"] = "error"
        job["supplement_error"] = str(e)
        _finish_supplement(job, prev_status)
        _push(job_id, {"type": "error", "message": f"サプリ処理エラー: {e}"})
        _save_job_state(job_id)


def _finish_supplement(job: dict, prev_status: str | None) -> None:
    """サプリ処理が途中で終わったときの後始末。
    本編の状態を元に戻し、処理中のまま残ったページを未処理に戻す。
    これをしないと画面が「実行中」のまま固まる。"""
    job["status"] = prev_status or "done"
    for p in job.get("pages", []):
        if p.get("status") == "processing":
            p["status"] = "pending"


@app.post("/api/job/{job_id}/cancel")
async def cancel_job(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, "job not found")
    job = JOBS[job_id]
    # 本編とサプリは別のタスクなので両方止める
    cancelled = []
    for key, label in (("task", "本編"), ("supplement_task", "サプリ")):
        t = job.get(key)
        if t and not t.done():
            t.cancel()
            cancelled.append(label)
    if cancelled:
        if "本編" in cancelled:
            job["status"] = "cancelling"
        if "サプリ" in cancelled:
            job["supplement_status"] = "cancelling"
        return {"ok": True, "status": "cancelling", "cancelled": cancelled}
    # タスクが残っていないのに実行中の表示が残っている場合 (再起動をまたいだ等) は
    # 表示だけを整えて返す。これをしないと UI が「実行中」から戻らない。
    changed = False
    if job.get("status") in ("queued", "running", "cancelling"):
        job["status"] = "cancelled"
        changed = True
    if job.get("supplement_status") in ("queued", "processing", "cancelling"):
        job["supplement_status"] = "cancelled"
        changed = True
    for p in job.get("pages", []):
        if p.get("status") == "processing":
            p["status"] = "pending"
            changed = True
    if changed:
        _save_job_state(job_id)
        _push(job_id, {"type": "cancelled", "message": "停止しました（処理は既に終了していました）"})
    return {"ok": True, "status": job.get("status"), "note": "not running"}


def _looks_untranslated(ja: str, src: str, lang: str = DEFAULT_LANG) -> bool:
    """英語原文と ja が事実上同じ (装飾差のみ) なら未翻訳とみなす。"""
    def strip_all(s: str) -> str:
        s = re.sub(r"<[^>]+>", "", s)
        s = re.sub(r"\s+", "", s)
        return s.lower()
    ja_s = strip_all(ja); src_s = strip_all(src)
    if not ja_s or not src_s:
        return False
    # 同一 or 8割一致
    if ja_s == src_s:
        return True
    # 短いテキストは前方一致で判定
    if len(src_s) >= 20:
        if ja_s[:min(len(ja_s), 40)] == src_s[:min(len(src_s), 40)]:
            return True
    # その言語に特有の文字が 1 つも無ければ未翻訳 (ラテン文字の言語は判定できないので
    # 原文と同一かどうかだけで見る)
    script = lang_info(lang).get("script")
    if script and not re.search(script, ja):
        return True
    return False


async def _retranslate_sentence(job_id: str, page_no: int, block: dict,
                                page_blocks: list[dict], model: str,
                                avoid_previous: bool = False,
                                glossary: str | None = None,
                                previous_ja: str | None = None,
                                extra_hint: str | None = None,
                                lang: str = DEFAULT_LANG) -> str:
    """1文を単独で翻訳(前後3文を文脈として渡す)。
    avoid_previous: 前回訳と異なる表現を促す
    glossary: 用語集(自由テキスト、各行 `英語 -> 日本語` 形式想定)
    previous_ja: 前回訳(avoid_previous 時にコピペ禁止として渡す)
    extra_hint: 追加の翻訳指示(title専用など内部呼び出し用)
    """
    idx = page_blocks.index(block)
    prev_ctx = " ".join(b["src"] for b in page_blocks[max(0, idx - 3):idx])[-800:]
    next_ctx = " ".join(b["src"] for b in page_blocks[idx + 1:idx + 4])[:800]
    src = block.get("_merged_src") or block["src"]
    if len(src) > 2500:
        src = src[:2500] + "…"
    blocks_txt = f"===INPUT BLOCK {block['i']}===\n{src}"
    li = lang_info(lang)
    if (lang or DEFAULT_LANG) == "ja":
        prompt = TRANSLATE_PROMPT.format(
            prev_tail=prev_ctx or "(なし)",
            next_head=next_ctx or "(なし)",
            blocks_txt=blocks_txt,
        )
    else:
        prompt = TRANSLATE_PROMPT_OTHER.format(
            lang=li["en"], headings=li["headings"], abstract="Abstract",
            prev_tail=prev_ctx or "(none)",
            next_head=next_ctx or "(none)",
            blocks_txt=blocks_txt,
        )
    # 追加指示を末尾に付加
    extra_parts: list[str] = []
    if glossary and glossary.strip():
        extra_parts.append(
            "## 用語集(以下の対応を優先。左が原文、右が訳。指定がないものは通常の判断で訳す)\n"
            + glossary.strip()
        )
    if avoid_previous:
        prev_hint = ""
        if previous_ja and previous_ja.strip():
            prev_hint = f"\n前回訳:\n{previous_ja.strip()}\n"
        extra_parts.append(
            "## 再翻訳指示\n"
            "前回訳とは**異なる表現・語彙・語順**で訳し直してください。"
            "意味は同じでも、より自然でわかりやすい別の言い回しを提示すること。"
            + prev_hint
        )
    if extra_hint and extra_hint.strip():
        extra_parts.append("## 追加指示\n" + extra_hint.strip())
    if extra_parts:
        prompt = prompt + "\n\n" + "\n\n".join(extra_parts)
    num_predict = min(4000, max(400, len(src) * 2 // 3))
    # avoid_previous 時は temperature を上げてバリエーションを生む
    temperature = 0.7 if avoid_previous else 0.2
    raw = await llm_chat([{"role": "user", "content": prompt}], model=model,
                         temperature=temperature, max_tokens=num_predict)
    parsed = _parse_delimited(raw)
    ja = parsed.get(block["i"], "")
    if not ja or _looks_like_hallucination(ja, len(src)):
        return ""
    return ja


class _RetranslateBody(BaseModel):
    page: int
    i: int  # sentence index
    avoid_previous: bool = False
    glossary: str | None = None

class _EditBody(BaseModel):
    page: int
    action: str  # "merge_prev" | "merge_next" | "split"
    i: int
    split_edited: str | None = None  # for split: text with '|' inserted at boundary

class _ChatBody(BaseModel):
    message: str
    page: int | None = None
    scope: str = "page"  # "page" or "all"
    web: bool = False  # if true, augment with web search results


class _CommentBody(BaseModel):
    page: int
    i: int  # sentence index
    text: str  # empty to delete


@app.post("/api/job/{job_id}/comment")
async def set_comment(job_id: str, body: _CommentBody):
    if job_id not in JOBS:
        raise HTTPException(404, "job not found")
    job = JOBS[job_id]
    if body.page < 1 or body.page > len(job.get("pages", [])):
        raise HTTPException(400, "invalid page")
    page = job["pages"][body.page - 1]
    block = next((b for b in page["blocks"] if b["i"] == body.i), None)
    if not block:
        raise HTTPException(404, "sentence not found")
    text = (body.text or "").strip()
    if text:
        block["comment"] = text
    else:
        block.pop("comment", None)
    _save_job_state(job_id)
    return {"ok": True}


@app.post("/api/job/{job_id}/retranslate")
async def retranslate(job_id: str, body: _RetranslateBody):
    if job_id not in JOBS:
        raise HTTPException(404, "job not found")
    job = JOBS[job_id]
    if body.page < 1 or body.page > len(job.get("pages", [])):
        raise HTTPException(400, "invalid page")
    page = job["pages"][body.page - 1]
    block = next((b for b in page["blocks"] if b["i"] == body.i), None)
    if not block:
        raise HTTPException(404, "sentence not found")
    model = job.get("model", DEFAULT_MODEL)
    try:
        ja = await _retranslate_sentence(
            job_id, body.page, block, page["blocks"], model,
            avoid_previous=body.avoid_previous,
            glossary=body.glossary,
            previous_ja=block.get("ja") if body.avoid_previous else None,
            lang=job.get("lang", DEFAULT_LANG),
        )
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if ja:
        block["ja"] = ja
        _push(job_id, {"type": "page_translate_partial", "page": body.page,
                       "blocks": [{"i": block["i"], "ja": ja}], "elapsed": 0})
        _save_job_state(job_id)
        return {"ok": True, "ja": ja}
    return {"ok": False, "error": "翻訳結果が空"}


@app.post("/api/job/{job_id}/edit")
async def edit_boundaries(job_id: str, body: _EditBody):
    if job_id not in JOBS:
        raise HTTPException(404, "job not found")
    job = JOBS[job_id]
    if body.page < 1 or body.page > len(job.get("pages", [])):
        raise HTTPException(400, "invalid page")
    page = job["pages"][body.page - 1]
    blocks: list[dict] = page["blocks"]
    idx = next((k for k, b in enumerate(blocks) if b["i"] == body.i), None)
    if idx is None:
        raise HTTPException(404, "sentence not found")
    cur = blocks[idx]
    model = job.get("model", DEFAULT_MODEL)

    if body.action == "merge_prev":
        if idx == 0:
            return {"ok": False, "error": "先頭のため結合不可"}
        prev = blocks[idx - 1]
        prev["src"] = prev["src"].rstrip() + " " + cur["src"].lstrip()
        prev["rects_px"] = list(prev.get("rects_px", [])) + list(cur.get("rects_px", []))
        prev["ja"] = ""
        blocks.pop(idx)
        target_idx = idx - 1
    elif body.action == "merge_next":
        if idx >= len(blocks) - 1:
            return {"ok": False, "error": "末尾のため結合不可"}
        nxt = blocks[idx + 1]
        cur["src"] = cur["src"].rstrip() + " " + nxt["src"].lstrip()
        cur["rects_px"] = list(cur.get("rects_px", [])) + list(nxt.get("rects_px", []))
        cur["ja"] = ""
        blocks.pop(idx + 1)
        target_idx = idx
    elif body.action == "split":
        edited = (body.split_edited or "").strip()
        if "|" not in edited:
            return {"ok": False, "error": "分割位置を | で指定してください"}
        parts = [s.strip() for s in edited.split("|") if s.strip()]
        if len(parts) < 2:
            return {"ok": False, "error": "分割位置が正しくありません"}
        # 元のrectsは分割しづらいので、その文の全rectsを各分割部に共有(視覚的にはやや粗いが動作する)
        orig_rects = list(cur.get("rects_px", []))
        # 現sentenceを最初の断片で置換、後続を挿入
        cur["src"] = parts[0]
        cur["ja"] = ""
        for k, part in enumerate(parts[1:], start=1):
            new_block = {
                "i": -1,  # 後で再割り当て
                "orig_i": -1,
                "p": cur["p"],
                "src": part,
                "rects_px": orig_rects[:],
                "ja": "",
                "font_size_px": cur.get("font_size_px", 20),
                "label": cur.get("label"),
            }
            blocks.insert(idx + k, new_block)
        target_idx = idx
    else:
        raise HTTPException(400, "unknown action")

    # i を全部再割り当て
    for k, b in enumerate(blocks):
        b["i"] = k
    _push(job_id, {"type": "structure_done", "page": body.page, "elapsed": 0, "entry": page})

    # 変更のあった段落 (target 付近) の未翻訳文を再翻訳
    target = blocks[target_idx]
    updated_jas = []
    try:
        for kk in range(len(blocks)):
            b = blocks[kk]
            if b["p"] != target["p"] or b.get("ja"):
                continue
            ja = await _retranslate_sentence(job_id, body.page, b, blocks, model,
                                             lang=job.get("lang", DEFAULT_LANG))
            if ja:
                b["ja"] = ja
                updated_jas.append({"i": b["i"], "ja": ja})
    except Exception as e:
        return {"ok": False, "error": f"再翻訳中エラー: {type(e).__name__}: {e}"}

    if updated_jas:
        _push(job_id, {"type": "page_translate_partial", "page": body.page,
                       "blocks": updated_jas, "elapsed": 0})
    _save_job_state(job_id)
    return {"ok": True, "updated": len(updated_jas)}


@app.post("/api/job/{job_id}/chat")
async def chat_endpoint(job_id: str, body: _ChatBody):
    if job_id not in JOBS:
        raise HTTPException(404, "job not found")
    job = JOBS[job_id]
    model = job.get("model", DEFAULT_MODEL)

    # 文脈の組み立て
    ctx_parts = []
    if body.scope == "all" or body.page is None:
        target_pages = job.get("pages", [])
    else:
        target_pages = job.get("pages", [])[max(0, body.page - 1):body.page]
    for p in target_pages:
        # 各文にインデックス付きで出す
        items = []
        for b in p.get("blocks", []):
            i = b.get("i")
            ja = re.sub(r"<[^>]+>", "", (b.get("ja") or "")).strip()
            src = re.sub(r"<[^>]+>", "", (b.get("src") or "")).strip()
            text = ja or src
            if text:
                items.append(f"[P{p['page_no']}S{i}] {text}")
        if items:
            ctx_parts.append("\n".join(items))
    context_txt = "\n\n".join(ctx_parts)
    # コンテキスト上限を拡張 (Qwen3 は 128k tokens 対応)
    # 論文全体の和訳 (~50k 字) を確実に収める
    CTX_MAX_CHARS = 120000
    truncated = False
    if len(context_txt) > CTX_MAX_CHARS:
        context_txt = context_txt[:CTX_MAX_CHARS] + "\n…(以下省略)"
        truncated = True
    print(f"[chat P{body.page} scope={body.scope}] context={len(context_txt)}chars pages={len(target_pages)} truncated={truncated}", flush=True)

    # オプション: Web検索で背景知識を補う
    web_txt = ""
    web_sources: list[dict] = []
    if body.web:
        try:
            # 新パッケージ ddgs (旧 duckduckgo_search はリネームされ返却 0 件になっていた)
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                results = list(ddgs.text(body.message, max_results=5))
            print(f"[chat web] DDGS returned {len(results)} results", flush=True)
            web_lines = []
            for k, r in enumerate(results, 1):
                title = r.get("title", "")
                url = r.get("href") or r.get("url", "")
                body_snip = (r.get("body") or "")[:400]
                web_lines.append(f"[W{k}] {title}\n{body_snip}\nURL: {url}")
                web_sources.append({"n": k, "title": title, "url": url, "snippet": body_snip})
            web_txt = "\n\n".join(web_lines)
            if len(web_txt) > 5000:
                web_txt = web_txt[:5000]
            print(f"[chat web] {len(results)} results, {len(web_txt)}chars", flush=True)
        except Exception as e:
            web_txt = f"[Web検索失敗: {type(e).__name__}: {e}]"
            print(f"[chat web] error: {e}", flush=True)

    _lname = lang_info(job.get("lang", DEFAULT_LANG))["name"]
    prompt = f"""あなたは学術文書アシスタントです。以下の文書内容 (原論文の{_lname}訳) に基づいて、ユーザーの質問に{_lname}で回答してください。

## 回答スタイル
- 質問に対する **必要十分な情報** を、簡潔だが省略なしで答える。
- 前置き (「以下に説明します」等) や結び (「参考になれば」等) は書かない。
- 内部思考や分析過程を書かない (回答本文だけを出力)。
- 文書に記載がない事項は「文書には記載がない」と述べる。文書外の推測はしない。
- 質問が「まとめて」「詳しく」等の場合は複数段落で構造化して説明してよい。単純な事実確認なら1〜2文で答える。

## 引用ルール (必ず守る)
- 各主張・段落の末尾に根拠となった文の ID を `[P<page>S<i>]` 形式で列挙。
- 引用 ID は下の文書内容の [P<page>S<i>] タグと一致させること。存在しない ID を作らない。
- 該当する根拠が文書中にない場合は引用を省く。

## 文書内容 (各文に ID が付いている)
{context_txt}
""" + (f"""
## 補助: Web検索結果 (背景知識、根拠として使う必要はない)
{web_txt}

Web検索結果を根拠として使った場合のみ末尾に `[W<n>]` を付ける。
""" if web_txt else "") + f"""
## ユーザーの質問
{body.message}

## 回答 ({_lname}で、引用付き)
"""
    async def gen():
        # 先頭に Web ソースのメタデータを送出 (クライアントは [WEB_META]...[/WEB_META] を検出して抽出)
        if web_sources:
            meta_line = "[WEB_META]" + json.dumps({"sources": web_sources}, ensure_ascii=False) + "[/WEB_META]\n"
            yield meta_line.encode("utf-8")
        # ストリーム途中で <think>...</think> が現れた場合は除去する
        # (thinking 無効化が効かず考察が本文に混ざるケースへの保険)
        in_think = False
        buf_carry = ""  # 前チャンクからの持越し (タグ検出用)
        def _filter_thinking(text: str) -> str:
            nonlocal in_think, buf_carry
            text = buf_carry + text
            buf_carry = ""
            out_parts: list[str] = []
            while text:
                if in_think:
                    end = text.find("</think>")
                    if end < 0:
                        # まだ考察中、全部捨てる (途中で切れた </think の可能性は保留)
                        if text.endswith("<") or text.endswith("</") or text.endswith("</t") \
                            or text.endswith("</th") or text.endswith("</thi") \
                            or text.endswith("</thin") or text.endswith("</think"):
                            buf_carry = text[-8:]
                        return "".join(out_parts)
                    text = text[end + len("</think>"):]
                    in_think = False
                else:
                    start = text.find("<think>")
                    if start < 0:
                        # <think 途中で切れているかチェック
                        tail = text[-7:]
                        if tail.endswith("<") or tail.endswith("<t") or tail.endswith("<th") \
                           or tail.endswith("<thi") or tail.endswith("<thin") or tail.endswith("<think"):
                            buf_carry = tail
                            text = text[:-len(tail)]
                        out_parts.append(text)
                        return "".join(out_parts)
                    out_parts.append(text[:start])
                    text = text[start + len("<think>"):]
                    in_think = True
            return "".join(out_parts)
        try:
            # 論文全体 (~50k 字) を渡すのでサーバ側コンテキストは 128k 以上で起動しておく
            async for chunk in llm_chat_stream([{"role": "user", "content": prompt}], model=model,
                                               temperature=0.3, max_tokens=4000):
                if chunk["response"]:
                    filtered = _filter_thinking(chunk["response"])
                    if filtered:
                        yield filtered.encode("utf-8")
                if chunk["done"]:
                    break
        except Exception as e:
            yield f"\n[エラー] {type(e).__name__}: {e}".encode()

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")


@app.get("/api/jobs")
async def list_jobs():
    items = []
    for jid, j in JOBS.items():
        items.append({
            "id": jid,
            "filename": j.get("filename"),
            "status": j.get("status"),
            "current_page": j.get("current_page"),
            "total_pages": j.get("total_pages"),
            "created_at": j.get("created_at"),
            "finished_at": j.get("finished_at"),
            "supplement_status": j.get("supplement_status"),
            "lang": j.get("lang", DEFAULT_LANG),
        })
    # 作成日時降順(新しい順)
    items.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return {"jobs": items}


@app.delete("/api/job/{job_id}")
async def delete_job(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404)
    # 実行中なら止める
    j = JOBS[job_id]
    t = j.get("task")
    if t and not t.done():
        t.cancel()
    JOBS.pop(job_id, None)
    # ディスクからも削除
    import shutil
    try:
        shutil.rmtree(JOBS_DIR / job_id)
    except Exception as e:
        print(f"[delete {job_id}] {e}", flush=True)
    return {"ok": True}


@app.get("/api/job/{job_id}")
async def get_job(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, "job not found")
    j = JOBS[job_id]
    return JSONResponse({k: v for k, v in j.items() if k not in _RUNTIME_KEYS})


@app.get("/api/job/{job_id}/stream")
async def stream_job(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, "job not found")

    async def gen() -> AsyncIterator[bytes]:
        q: asyncio.Queue = JOBS[job_id]["event_queue"]
        snapshot = {k: v for k, v in JOBS[job_id].items() if k not in _RUNTIME_KEYS}
        yield f"data: {json.dumps({'type': 'snapshot', 'job': snapshot}, ensure_ascii=False)}\n\n".encode()
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=30.0)
            except asyncio.TimeoutError:
                yield b": keepalive\n\n"
                continue
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode()
            if ev.get("type") in ("done", "error", "cancelled"):
                break

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/languages")
async def list_languages():
    """訳文に選べる言語。"""
    return {"languages": [{"code": c, "name": v["name"]} for c, v in LANGUAGES.items()],
            "default": DEFAULT_LANG}


@app.get("/api/models")
async def list_models():
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"{LLM_URL}/v1/models")
            r.raise_for_status()
            names = [m["id"] for m in r.json().get("data", [])]
            return {"models": names, "default": DEFAULT_MODEL}
    except Exception as e:
        return {"models": [], "default": DEFAULT_MODEL, "error": str(e)}


# ----------------------------------------------------------------- LLM 管理
_llama = llm_manager.LlamaProcess()
_downloader = llm_manager.Downloader()


class _StartLlmBody(BaseModel):
    path: str
    mmproj: str | None = None
    ctx: int = 32768


class _InstallBody(BaseModel):
    id: str


@app.get("/api/llm/status")
async def llm_status():
    """接続先の LLM の状態。アプリが起動したものか、外で動いているものかも返す。"""
    info = await llm_manager.probe(LLM_URL)
    managed = _llama.running()
    return {
        "url": LLM_URL,
        "connected": info["ok"],
        "models": info["models"],
        "default": DEFAULT_MODEL,
        "error": info.get("error"),
        "managed": managed,           # このアプリが起動した llama-server か
        "process": _llama.info if managed else None,
        "llama_server": str(llm_manager.find_llama_server() or ""),
        "models_dir": str(llm_manager.MODELS_DIR),
    }


@app.get("/api/llm/local")
async def llm_local():
    """導入済みの GGUF 一覧。"""
    return {"models": llm_manager.scan_local_models()}


@app.get("/api/llm/catalog")
async def llm_catalog():
    """追加できる推奨モデル一覧 (視覚対応)。"""
    return {"catalog": llm_manager.catalog_with_state(),
            "download": _downloader.state}


@app.post("/api/llm/start")
async def llm_start(body: _StartLlmBody):
    """指定した GGUF で llama-server を起動し、接続先を切り替える。"""
    global LLM_URL, DEFAULT_MODEL
    try:
        info = await asyncio.to_thread(
            _llama.start, body.path, body.mmproj, "pdfvl", body.ctx)
    except Exception as e:
        raise HTTPException(400, str(e))
    ready = await llm_manager.wait_ready(info["url"])
    if not ready:
        _llama.stop()
        raise HTTPException(
            500, f"llama-server が応答しませんでした。ログ: {info['log']}")
    LLM_URL = info["url"]
    DEFAULT_MODEL = info["alias"]
    return {"ok": True, **info, "model": DEFAULT_MODEL}


@app.post("/api/llm/stop")
async def llm_stop():
    """アプリが起動した llama-server を止め、接続先を既定に戻す。"""
    global LLM_URL, DEFAULT_MODEL
    await asyncio.to_thread(_llama.stop)
    LLM_URL = _DEFAULT_LLM_URL
    DEFAULT_MODEL = _DEFAULT_MODEL_NAME
    return {"ok": True, "url": LLM_URL}


@app.post("/api/llm/install")
async def llm_install(body: _InstallBody):
    """推奨モデルをダウンロードする (バックグラウンド)。"""
    if _downloader.busy():
        raise HTTPException(409, "別のモデルをダウンロード中です")
    entry = next((e for e in llm_manager.CATALOG if e["id"] == body.id), None)
    if entry is None:
        raise HTTPException(404, "不明なモデルです")
    _downloader.start(entry)
    return {"ok": True, "state": _downloader.state}


@app.get("/api/llm/install/status")
async def llm_install_status():
    return {"state": _downloader.state, "busy": _downloader.busy()}


@app.post("/api/llm/install/cancel")
async def llm_install_cancel():
    if _downloader.busy():
        _downloader.task.cancel()
    return {"ok": True}


app.mount("/jobs", StaticFiles(directory=str(JOBS_DIR)), name="jobs")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
async def _startup():
    _load_saved_jobs()


@app.get("/", response_class=HTMLResponse)
async def index():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    })


if __name__ == "__main__":
    import os
    import uvicorn
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8090"))
    uvicorn.run(app, host=host, port=port, log_level="info")
