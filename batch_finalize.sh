#!/bin/bash
set -e

SKILL_DIR="$HOME/.openclaw/skills/youtube-clipper"
CLIPS_DIR="$SKILL_DIR/clips"
CLIPS_JSON="$SKILL_DIR/downloads/IZDJ3jcO5UY.clips.json"
BG="$SKILL_DIR/background.png"

echo "=== Step 1: Generate covers ==="
for i in 1 2 3 4 5; do
  idx=$((i - 1))
  echo "--- Cover for clip $i (index $idx) ---"
  python3 "$SKILL_DIR/scripts/generate_cover.py" \
    --clips-json "$CLIPS_JSON" \
    --clip-index "$idx" \
    --bg "$BG" \
    --output "$CLIPS_DIR/IZDJ3jcO5UY_$(printf '%03d' $i)_cover.png"
done

echo ""
echo "=== Step 2: Refine subtitles (ASR → Chinese SRT) ==="
for i in 1 2 3 4 5; do
  num=$(printf '%03d' $i)
  echo "--- Subtitle $num ---"
  python3 "$SKILL_DIR/scripts/refine_subtitles.py" \
    --asr "$CLIPS_DIR/IZDJ3jcO5UY_${num}.asr.json" \
    --output "$CLIPS_DIR/IZDJ3jcO5UY_${num}.srt"
done

echo ""
echo "=== Step 3: Finalize (cover + subtitles) ==="
for i in 1 2 3 4 5; do
  num=$(printf '%03d' $i)
  echo "--- Finalize $num ---"
  python3 "$SKILL_DIR/scripts/finalize_video.py" \
    --video "$CLIPS_DIR/IZDJ3jcO5UY_${num}.mp4" \
    --cover "$CLIPS_DIR/IZDJ3jcO5UY_${num}_cover.png" \
    --srt "$CLIPS_DIR/IZDJ3jcO5UY_${num}.srt" \
    --output "$HOME/.openclaw/workspace/IZDJ3jcO5UY_${num}_final.mp4"
done

echo ""
echo "=== Done ==="
ls -lh $HOME/.openclaw/workspace/IZDJ3jcO5UY_*_final.mp4
