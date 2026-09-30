# PDF VL Assistant

英語の論文 PDF を、**ローカルの視覚対応 LLM** でページごとに読み解き、原文と日本語訳を左右に並べて読むツールです。外部サービスには一切送りません。

Read English research papers with a **local vision-capable LLM**. Each page is analysed as an image, then shown side by side: the original PDF on the left, a Japanese translation on the right. Nothing leaves your machine.

---

## 概要（日本語）

📖 **詳しい説明は [README.ja.md](README.ja.md) をご覧ください。**

ページを画像ごと LLM に見せて解析するので、文字を拾うだけでは分からない段組み・脚注・行番号に強いのが特徴です。

- **文単位の対訳** — 訳文をクリックすると、対応する原文が PDF 側の中央に表示される
- **図・表をクリックで表示** — 本文中の「Fig. 3」「Table 2」を押すと、その図表と和訳済みキャプションが出る。写真だけでなくベクター図も切り出し、2 段組や複数ページに跨る表にも対応
- **数式はそのまま画像で表示** — 文字として取り出すと項ごとにバラバラになるため、元ページから切り出して貼る
- **サプリメンタル PDF の追加解析** — 本編と番号空間を分け、「Supplementary Fig. 2」から正しく引ける
- **解析中でも読める** — 終わったページから順に表示。中断・再開・リロードに耐える
- **文へのコメント**、**文書内チャット**

### 使い方

```bash
./install.sh   # Python 環境と llama.cpp を用意
./run.sh       # http://localhost:8090 を開く
```

初回は画面右上の「🧠 モデル管理」から、推奨リストの視覚対応モデルを 1 つ選んでダウンロードし、「起動」を押してください。このマシンに既に入っている GGUF も自動で探して一覧に出します。

用意しているモデルは Qwen3.5（2B〜35B）、Qwen3.8 27B、Gemma 4 12B、そして **Qwen3.8 Flash-Next（MoE 125B）** です。容量と必要メモリの目安は[下の表](#recommended-models)にあります。

---

## What it does

- **Analyses the page image, not just the text** — so it copes with two-column layouts, footnotes and line numbers
- **Sentence-level alignment** — hover a translated sentence and the matching region lights up in the PDF
- **Click a figure or table reference** — "Fig. 3" or "Table 2" in the text opens that figure together with its **translated caption**
  - Captures **vector figures** (plots, diagrams), not only embedded photos
  - Crops to the figure itself, so a two-column layout does not drag in the neighbouring column
  - **Stitches tables that run across pages** into one image
  - Handles the layout where a figure fills one page and its legend sits on the next
- **Equations are shown as images** — extracted text breaks them into scattered fragments, so the region is cropped from the original page instead
- **Supplementary PDFs** — numbered separately from the main text, so "Supplementary Fig. 2" resolves correctly
- **Readable while it runs** — pages appear as they finish; survives interruption, resume and browser reloads
- **Comments on sentences** — saved to disk
- **Chat about the document**

## Requirements

- Python 3.10+
- A GPU is recommended (CPU works, but is much slower)
- Disk space for a model (from 2.3GB; larger models translate better)

## Install

```bash
git clone <this repository> pdf-vl-assistant
cd pdf-vl-assistant
./install.sh
```

`install.sh` will:

1. create `.venv` and install the Python dependencies
2. set up `llama.cpp` — a released binary when one exists for your platform, otherwise a source build (CUDA / Metal detected automatically)

Use `FORCE_BUILD=1 ./install.sh` to force a source build if you want GPU support for certain. An existing `llama-server` on your `PATH` is used as is.

## Run

```bash
./run.sh
```

Open `http://localhost:8090` (also reachable from other machines on your LAN).

**Set up a model first.** Click "🧠 モデル管理" (Models) in the top right to see

- the LLM you are currently connected to
- GGUF models already on this machine, with whether each supports vision
- a list of recommended models you can add

Pick one, press download, and press start when it finishes.

<a id="recommended-models"></a>

| Model | Size | RAM guide |
|---|---|---|
| Qwen3.5 2B | 2.0GB | 6GB |
| Qwen3.5 4B | 3.4GB | 8GB |
| Qwen3.5 9B | 6.6GB | 12GB |
| Gemma 4 12B | 7.3GB | 14GB |
| Qwen3.5 27B | 17.7GB | 24GB |
| Qwen3.8 27B | 19.9GB | 26GB |
| Qwen3.5 35B-A3B (MoE) | 22.9GB | 30GB |
| Qwen3.8 Flash-Next (MoE 125B) | 90.9GB | 100GB |

Then choose a PDF and start the analysis. The first page becomes readable in about a minute; the rest follow one by one.

Add a supplementary PDF with "📎 サプリ追加" once the main document has been analysed.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8090` | Web UI port |
| `HOST` | `0.0.0.0` | Bind address; use `127.0.0.1` to keep it local |
| `LLM_URL` | `http://127.0.0.1:8004` | Connect to a llama-server you already run |
| `PDFVL_MODEL` | — | Default model name |
| `PDFVL_MODELS_DIR` | `./models` | Where models are stored |
| `LLAMA_SERVER_BIN` | — | Path to `llama-server` |

## Where data lives

- `jobs/<id>/` — analysis results (page images, translations, figures, comments) in `state.json`; they survive restarts
- `models/` — downloaded models

Both are gitignored.

## How it works

1. PyMuPDF extracts text, coordinates and font attributes page by page
2. The page image goes to the LLM, which decides sentence boundaries and structure (headings, captions, body)
3. Paragraphs are translated; sentences split across a page break are joined first
4. Figures, tables and equations are cropped from the original page using their coordinates
5. Results stream to the browser as they are produced (SSE)

## Development

`check.py` is a small separate server for inspecting sentence-boundary decisions (`python check.py` → `http://localhost:8081`). It is not needed to run the app.

## License

MIT
