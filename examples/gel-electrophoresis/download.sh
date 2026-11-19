#!/bin/sh
# Download the example video from Wikimedia Commons (97 MB) and make an mp4 of it.
#
#   sh examples/gel-electrophoresis/download.sh
#
# The video is not committed: it is CC BY 3.0 and 97 MB. See README.md for the credit.
set -eu

DIR="$(cd "$(dirname "$0")" && pwd)/video"
URL="https://upload.wikimedia.org/wikipedia/commons/8/84/Agarose_Gel_Electrophoresis_-_Assembling_the_Rig_and_Loading-Running_the_Gel.webm"
SHA256="2602cbaf27d45204d331a4c88ecdb37199db32339d32beb93d5835380bc20cae"

mkdir -p "$DIR"
cd "$DIR"

if [ ! -f gel-electrophoresis.webm ]; then
	echo "downloading $URL"
	curl -fL --retry 3 -A "tva-example-download/1.0" -o gel-electrophoresis.webm.part "$URL"
	mv gel-electrophoresis.webm.part gel-electrophoresis.webm
fi

echo "$SHA256  gel-electrophoresis.webm" | shasum -a 256 -c -

# The app accepts the .webm as is. The mp4 is for the CLI scripts and for players
# that don't do VP8; 1280x720 is more than TVA uses (every frame becomes 224x224).
if [ ! -f gel-electrophoresis.mp4 ] && command -v ffmpeg >/dev/null; then
	echo "converting to mp4"
	ffmpeg -v error -i gel-electrophoresis.webm -c:v libx264 -crf 23 -preset fast \
		-an -movflags +faststart gel-electrophoresis.mp4
fi

ls -lh "$DIR"
