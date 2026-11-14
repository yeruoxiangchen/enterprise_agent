#!/usr/bin/env bash
set -euo pipefail
ROOT=/data/zjr/enterprise_kb_agent
export TMPDIR="$ROOT/tmp" PIP_CACHE_DIR="$ROOT/cache/pip" PYTHONDONTWRITEBYTECODE=1
mkdir -p "$TMPDIR" "$PIP_CACHE_DIR" "$ROOT/tools/tesseract/packages"
"$ROOT/venv-agentpack/bin/python" -m pip --require-virtualenv install 'Pillow>=10,<13'
cd "$ROOT/tools/tesseract/packages"
OCR_LIBRARY="$(apt-cache depends tesseract-ocr | sed -n 's/.*Depends: \(libtesseract[0-9]*\)$/\1/p' | head -1)"
test -n "$OCR_LIBRARY"
apt-get download tesseract-ocr "$OCR_LIBRARY" liblept5 libwebpmux3 libopenjp2-7 tesseract-ocr-eng tesseract-ocr-chi-sim tesseract-ocr-osd
for package in ./*.deb; do
  dpkg-deb -x "$package" "$ROOT/tools/tesseract"
done
export LD_LIBRARY_PATH="$ROOT/tools/tesseract/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
"$ROOT/tools/tesseract/usr/bin/tesseract" --version
