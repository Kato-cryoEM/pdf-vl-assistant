#!/usr/bin/env bash
# PDF VL Assistant セットアップ
#   - Python 仮想環境と依存パッケージ
#   - llama.cpp (配布バイナリ優先、無ければソースからビルド)
# モデルは起動後にブラウザの「モデル管理」から入れられる。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR="$HERE/vendor"
LLAMA_DIR="$VENDOR/llama.cpp"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SKIP_LLAMA="${SKIP_LLAMA:-0}"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- Python
say "Python 環境を用意します"
command -v "$PYTHON_BIN" >/dev/null || die "$PYTHON_BIN が見つかりません"
PY_VER=$("$PYTHON_BIN" -c 'import sys;print("%d.%d"%sys.version_info[:2])')
case "$PY_VER" in
  3.1[0-9]|3.[2-9][0-9]) ;;
  *) die "Python 3.10 以上が必要です (検出: $PY_VER)" ;;
esac

if [ ! -d "$HERE/.venv" ]; then
  "$PYTHON_BIN" -m venv "$HERE/.venv"
fi
# shellcheck disable=SC1091
source "$HERE/.venv/bin/activate"
python -m pip install --upgrade pip >/dev/null
say "依存パッケージを入れます"
pip install -r "$HERE/requirements.txt"

# ---------------------------------------------------------------- llama.cpp
if [ "$SKIP_LLAMA" = "1" ]; then
  warn "SKIP_LLAMA=1 のため llama.cpp の導入をとばします"
elif command -v llama-server >/dev/null 2>&1; then
  say "llama-server は既に入っています: $(command -v llama-server)"
elif [ -x "$LLAMA_DIR/build/bin/llama-server" ] || [ -x "$LLAMA_DIR/llama-server" ]; then
  say "llama-server は既に $LLAMA_DIR にあります"
else
  mkdir -p "$VENDOR"
  OS="$(uname -s)"; ARCH="$(uname -m)"
  ASSET=""
  # 配布バイナリがある組み合わせだけ選ぶ (無ければソースビルドに落とす)
  case "$OS/$ARCH" in
    Linux/x86_64)  ASSET="ubuntu-x64" ;;
    Darwin/arm64)  ASSET="macos-arm64" ;;
    Darwin/x86_64) ASSET="macos-x64" ;;
  esac
  GOT_BINARY=0
  if [ -n "$ASSET" ]; then
    say "llama.cpp の配布バイナリを探します ($OS/$ARCH)"
    URL=$(curl -fsSL https://api.github.com/repos/ggml-org/llama.cpp/releases/latest \
          | grep -o "https://[^\"]*llama-[^\"]*bin-${ASSET}[^\"]*\.zip" | head -1 || true)
    if [ -n "$URL" ]; then
      say "取得: $URL"
      if curl -fSL "$URL" -o "$VENDOR/llama.zip"; then
        rm -rf "$LLAMA_DIR" && mkdir -p "$LLAMA_DIR"
        if unzip -q -o "$VENDOR/llama.zip" -d "$LLAMA_DIR"; then
          rm -f "$VENDOR/llama.zip"
          # zip の中の llama-server を掘り出して置き場所を揃える
          FOUND=$(find "$LLAMA_DIR" -name llama-server -type f | head -1 || true)
          if [ -n "$FOUND" ]; then
            chmod +x "$FOUND"
            mkdir -p "$LLAMA_DIR/build/bin"
            [ "$FOUND" != "$LLAMA_DIR/build/bin/llama-server" ] && cp "$FOUND" "$LLAMA_DIR/build/bin/llama-server"
            GOT_BINARY=1
            warn "配布バイナリは GPU が使えないことがあります。遅い場合は SKIP_LLAMA=0 FORCE_BUILD=1 で入れ直してください"
          fi
        fi
      fi
    fi
    [ "$GOT_BINARY" = "0" ] && warn "配布バイナリが取得できませんでした。ソースからビルドします"
  else
    say "この環境 ($OS/$ARCH) の配布バイナリは無いのでソースからビルドします"
  fi

  if [ "$GOT_BINARY" = "0" ] || [ "${FORCE_BUILD:-0}" = "1" ]; then
    command -v git >/dev/null || die "git が必要です"
    command -v cmake >/dev/null || die "cmake が必要です (例: sudo apt install cmake build-essential)"
    if [ ! -d "$LLAMA_DIR/.git" ]; then
      rm -rf "$LLAMA_DIR"
      say "llama.cpp を取得します"
      git clone --depth 1 https://github.com/ggml-org/llama.cpp "$LLAMA_DIR"
    fi
    CMAKE_FLAGS="-DLLAMA_CURL=OFF -DCMAKE_BUILD_TYPE=Release"
    if command -v nvcc >/dev/null 2>&1; then
      say "CUDA を検出。GPU 版でビルドします"
      CMAKE_FLAGS="$CMAKE_FLAGS -DGGML_CUDA=ON"
    elif [ "$(uname -s)" = "Darwin" ]; then
      say "macOS。Metal 版でビルドします"
      CMAKE_FLAGS="$CMAKE_FLAGS -DGGML_METAL=ON"
    else
      warn "GPU が見つかりません。CPU 版でビルドします (解析はかなり遅くなります)"
    fi
    say "ビルド中… 環境によっては 10〜20 分かかります"
    # shellcheck disable=SC2086
    cmake -S "$LLAMA_DIR" -B "$LLAMA_DIR/build" $CMAKE_FLAGS
    cmake --build "$LLAMA_DIR/build" --config Release -j"$(nproc 2>/dev/null || sysctl -n hw.ncpu)" --target llama-server
  fi
fi

mkdir -p "$HERE/models" "$HERE/jobs"

say "完了しました"
echo
echo "  起動:  ./run.sh"
echo "  その後 http://localhost:8090 を開き、右上の「🧠 モデル管理」から"
echo "  視覚対応モデルを 1 つ入れて「起動」を押してください。"
