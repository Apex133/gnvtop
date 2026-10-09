#!/usr/bin/env bash
# gnvtop installer — no root needed. Override location with: PREFIX=... ./install.sh
set -euo pipefail
PREFIX="${PREFIX:-$HOME/.local}"
SRC="$(cd "$(dirname "$0")" && pwd)"
SHARE="$PREFIX/share/gnvtop"
BIN="$PREFIX/bin"

mkdir -p "$SHARE" "$BIN"
cp "$SRC/server.py" "$SRC/index.html" "$SHARE/"
printf '#!/usr/bin/env bash\nexec python3 "%s/server.py" "$@"\n' "$SHARE" > "$BIN/gnvtop"
chmod +x "$BIN/gnvtop"

echo "Installed: $BIN/gnvtop"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "NOTE: $BIN is not on your PATH. Add to ~/.bashrc:  export PATH=\"$BIN:\$PATH\"" ;;
esac
echo "Run 'gnvtop' and open http://127.0.0.1:8399 — Ctrl+C stops it."
