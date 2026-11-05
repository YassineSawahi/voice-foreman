#!/bin/sh
# Download the Kokoro text-to-speech model (about 350 MB) into models/kokoro, where the speech
# service mounts it. Whisper and the end-of-speech model download by themselves.
set -e
P=$(cd "$(dirname "$0")/.." && pwd)
D=$P/models/kokoro
BASE=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0
mkdir -p "$D"
for f in kokoro-v1.0.onnx voices-v1.0.bin; do
  [ -s "$D/$f" ] && { echo "have $f"; continue; }
  curl -fL --progress-bar -o "$D/$f.part" "$BASE/$f" && mv "$D/$f.part" "$D/$f"
done
ls -la "$D"
