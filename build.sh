#!/usr/bin/env bash
# build.sh — сборка InPulse v3 компонентов
#
# Использование:
#   ./build.sh sfu        — только Go SFU
#   ./build.sh rust       — только Rust Media Engine
#   ./build.sh all        — оба
#   ./build.sh            — оба (по умолчанию)
#
# Требования:
#   Go  >= 1.21   (go version)
#   Rust nightly/stable >= 1.75  (rustc --version)
#   FFmpeg dev libraries (для ffmpeg-next crate)
#   Windows: MSVC toolchain или MinGW

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SFU_DIR="$SCRIPT_DIR/sfu"
RUST_DIR="$SCRIPT_DIR/test_v2/media-engine"

TARGET="${1:-all}"

# ─── Цвета ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[OK]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; exit 1; }

# ─── Build Go SFU ─────────────────────────────────────────────────────────────
build_sfu() {
    echo ""
    echo "══════════════════════════════════════════"
    echo "  Building Go Pion SFU (sidecar.exe)"
    echo "══════════════════════════════════════════"

    command -v go >/dev/null 2>&1 || fail "Go не найден. Установи с https://go.dev/dl/"

    GO_VERSION=$(go version | awk '{print $3}' | sed 's/go//')
    echo "  Go version: $GO_VERSION"

    cd "$SFU_DIR"

    echo "  → go mod tidy ..."
    go mod tidy

    echo "  → go build ..."
    if [[ "${GOOS:-}" == "windows" ]] || [[ "$(uname -s)" == "MINGW"* ]]; then
        go build -ldflags="-s -w" -o sidecar.exe .
        ok "sidecar.exe собран: $SFU_DIR/sidecar.exe"
    else
        # Кросс-компиляция под Windows с Linux хоста
        GOOS=windows GOARCH=amd64 \
            go build -ldflags="-s -w" -o sidecar.exe .
        ok "sidecar.exe (windows/amd64) собран: $SFU_DIR/sidecar.exe"
    fi

    # Копируем в test_v2 рядом с Python
    cp "$SFU_DIR/sidecar.exe" "$SCRIPT_DIR/test_v2/sidecar.exe"
    ok "Скопирован в test_v2/sidecar.exe"
}

# ─── Build Rust Media Engine ──────────────────────────────────────────────────
build_rust() {
    echo ""
    echo "══════════════════════════════════════════"
    echo "  Building Rust Media Engine"
    echo "══════════════════════════════════════════"

    command -v cargo >/dev/null 2>&1 || fail "Rust/Cargo не найден. Установи с https://rustup.rs/"

    RUST_VERSION=$(rustc --version)
    echo "  Rust version: $RUST_VERSION"

    cd "$RUST_DIR"

    echo "  → cargo build --release ..."
    echo "  (первая сборка займёт 5-15 мин из-за webrtc-rs + ffmpeg-next)"

    cargo build --release 2>&1 | grep -E "^(error|warning|   Compiling|   Finished)" || true

    if [[ -f "target/release/inpulse-media-engine.exe" ]]; then
        cp "target/release/inpulse-media-engine.exe" \
           "$SCRIPT_DIR/test_v2/media-engine.exe"
        ok "media-engine.exe собран и скопирован в test_v2/"
    elif [[ -f "target/release/inpulse-media-engine" ]]; then
        cp "target/release/inpulse-media-engine" \
           "$SCRIPT_DIR/test_v2/media-engine"
        warn "Собран не-Windows бинарь (для тестирования без GPU)"
    else
        fail "Бинарь не найден после cargo build --release"
    fi
}

# ─── Main ─────────────────────────────────────────────────────────────────────
case "$TARGET" in
    sfu)  build_sfu  ;;
    rust) build_rust ;;
    all|*) build_sfu && build_rust ;;
esac

echo ""
ok "Сборка завершена. Компоненты:"
echo "   test_v2/sidecar.exe      — Go Pion SFU"
echo "   test_v2/media-engine.exe — Rust Media Engine"
echo ""
echo "Запуск (Python должен стартовать оба процесса автоматически):"
echo "   cd test_v2 && python run.py"
