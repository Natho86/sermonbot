#!/bin/bash

# Usage: ./normalize_convert.sh input.wav [output.m4a] [target_dB]
# Default target is -1 dB to avoid clipping.

INPUT="$1"
OUTPUT="${2:-${INPUT%.wav}.m4a}"
TARGET_DB="${3:--5}"

if [[ -z "$INPUT" || ! -f "$INPUT" ]]; then
  echo "Usage: $0 input.wav [output.m4a] [target_dB]"
  exit 1
fi

# Step 1: Analyze volume
MAX_VOLUME=$(ffmpeg -i "$INPUT" -af volumedetect -f null /dev/null 2>&1 | grep max_volume | awk '{print $5}' | sed 's/dB//')

if [[ -z "$MAX_VOLUME" ]]; then
  echo "Failed to detect max volume."
  exit 1
fi

# Step 2: Calculate gain
# e.g. If max_volume = -6.0 and target = -1.0, then gain = 5.0
GAIN=$(awk -v max="$MAX_VOLUME" -v target="$TARGET_DB" 'BEGIN { printf "%.2f", target - max }')

echo "Detected max volume: ${MAX_VOLUME} dB"
echo "Target volume: ${TARGET_DB} dB"
echo "Applying gain: ${GAIN} dB"

# Step 3: Convert with volume adjustment
ffmpeg -i "$INPUT" -af "volume=${GAIN}dB" -c:a aac -b:a 192k "$OUTPUT"
