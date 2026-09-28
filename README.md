# PDF VL Assistant

Read English research papers with a **local vision-capable LLM**. Each page is analysed as an image, then shown side by side: the original PDF on the left, a Japanese translation on the right. Nothing leaves your machine.

［[日本語](README.ja.md)］

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

| Model | Size | RAM guide |
|---|---|---|
| Qwen3-VL 2B | 2.3GB | 6GB |
| Qwen2.5-VL 3B | 2.8GB | 6GB |
| Gemma 3 4B | 3.3GB | 8GB |
| Qwen2.5-VL 7B | 5.5GB | 10GB |
| Gemma 3 12B | 8.2GB | 14GB |
| Gemma 3 27B | 17.4GB | 24GB |
| Qwen2.5-VL 32B | 20.6GB | 28GB |
| Qwen3-VL 30B-A3B (MoE) | 33.2GB | 40GB |

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
