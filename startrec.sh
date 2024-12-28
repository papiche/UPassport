#!/bin/bash
################################################################### startrec.sh
################################################################################
# Author: Fred (DsEx1pS33vzYZg4MroyBV9hCw98j1gtHEhwiZ5tK7ech)
# Version: 1.2
# License: AGPL-3.0 (https://choosealicense.com/licenses/agpl-3.0/)
###############################################################################
MY_PATH="`dirname \"$0\"`"              # relative
MY_PATH="`( cd \"$MY_PATH\" && pwd )`"  # absolutized and normalized
ME="${0##*/}"
MOATS=$(date -u +"%Y%m%d%H%M%S%4N")
export PATH=$HOME/.local/bin:$PATH

source ${MY_PATH}/.env

# Vérifier le nombre d'arguments
if [ "$#" -lt 1 ] || [ "$#" -gt 3 ]; then
    echo "Usage: $0 <email> [link=<video_link>] [upload=<file_path>]"
    exit 1
fi

# Récupération des arguments
PLAYER=$1

## Is it a local PLAYER
[[ ! -d ~/.zen/game/players/${PLAYER} ]] \
    && echo "UNKNOWN PLAYER ${PLAYER}" \
    && exit 1

echo "${PLAYER} /REC ================================= "
OUTPUT_DIR="$HOME/Astroport/$PLAYER/REC/$MOATS"
mkdir -p "$OUTPUT_DIR"

# Fonction pour détecter les silences et découper la vidéo
segment_video() {
    local input_file=$1
    local output_dir=$2

    # Détection des silences
    ffmpeg -i "$input_file" -af silencedetect=n=-30dB:d=2 -f null - 2>&1 | grep silence_end | awk '{print $5 " " $8}' > "$output_dir/silence.txt"

    # Découpage de la vidéo basé sur les silences détectés
    python3 ${MY_PATH}/split_video.py "$input_file" "$output_dir/silence.txt" "$output_dir"
}

# Fonction pour transcrire un segment vidéo avec Whisper
transcribe_segment() {
    local input_file=$1
    local output_file=$2
    whisper "$input_file" --model medium --output_dir "$(dirname "$output_file")" --output_format txt
}

process_video() {
    local video_file=$1
    local output_dir=$2

    # Découpage de la vidéo basé sur les silences
    segment_video "$video_file" "$output_dir"

    # Transcription de chaque segment
    for segment in "$output_dir"/segment_*.mp4; do
        transcribe_segment "$segment" "${segment%.*}.txt"
    done

    # Concaténation des transcriptions
    cat "$output_dir"/*.txt > "$output_dir/full_transcription.txt"
}

# Traitement du lien YouTube ou du fichier uploadé
if [[ "$2" =~ ^link=(.*)$ ]]; then
    VIDEO_LINK="${BASH_REMATCH[1]}"
    echo "Received video link: $VIDEO_LINK"

    yt-dlp -f 'bv[height<=720]+ba[language=fr]/b[height<=720]' -o "$OUTPUT_DIR/%(title)s.%(ext)s" "$VIDEO_LINK"

    if [ ! $? -eq 0 ]; then
        yt-dlp -f 'b[height<=360]+ba[language=fr]/best[height<=360]' -o "$OUTPUT_DIR/%(title)s.%(ext)s" "$VIDEO_LINK"
    fi

    UPLOADED_FILE="$(yt-dlp --get-filename -o "%(title)s.%(ext)s" "$VIDEO_LINK")"
    echo "Video downloaded and saved to: $UPLOADED_FILE"

    process_video "$OUTPUT_DIR/$UPLOADED_FILE" "$OUTPUT_DIR"

elif [[ "$2" =~ ^upload=(.*)$ ]]; then
    UPLOADED_FILE="${BASH_REMATCH[1]}"
    echo "Received uploaded file: $UPLOADED_FILE"

    cp "$UPLOADED_FILE" "$OUTPUT_DIR"
    process_video "$OUTPUT_DIR/$(basename "$UPLOADED_FILE")" "$OUTPUT_DIR"

else
    echo "No video link or uploaded file provided - OBS Recording - "
    exit 0
fi

echo "PROCESSING VIDEO PIPELINE..."
exit 0
