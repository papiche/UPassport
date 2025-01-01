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

process_video() {
    local video_file=$1
    local output_dir=$2

    # Transcription avec le script Python
    python3 transcribe.whisper.py "$video_file" "$output_dir/transcription.txt"

    # Extraction d'images clés
    ffmpeg -i "$video_file" -vf fps=1 "$output_dir/frame%03d.jpg"

    # Sélection aléatoire de 3 images
    shuf -n 3 -e "$output_dir"/frame*.jpg > "$output_dir/selected_frames.txt"

    # Reconnaissance d'objets avec Ollama API pour les 3 images sélectionnées
    while IFS= read -r image; do
        curl http://localhost:11434/api/generate -d '{
            "model": "llava",
            "prompt": "Describe what you see in this image?",
            "images": ["'"$(base64 -w 0 "$image")"'"]
        }' | jq -r '.response' >> "$output_dir/objects.txt"
    done < "$output_dir/selected_frames.txt"

    # Génération de résumé et métadonnées
    transcription=$(cat "$output_dir/transcription.txt")
    objects=$(cat "$output_dir/objects.txt")
    curl http://localhost:11434/api/generate -d '{
        "model": "llama2",
        "prompt": "Based on the following transcription and recognized objects, generate a summary and metadata:\n\nTranscription: '"$transcription"'\n\nRecognized objects: '"$objects"'\n\nPlease provide:\n1. A brief summary\n2. Keywords\n3. Main topics\n4. Mood or tone"
    }' | jq -r '.response' > "$output_dir/summary_metadata.txt"

    # Nettoyage des fichiers temporaires
    rm "$output_dir/selected_frames.txt"
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

elif [[ "$2" =~ ^blob=(.*)$ ]]; then
    BLOB_URL="${BASH_REMATCH[1]}"
    echo "Received blob URL: $BLOB_URL"

    process_video "$BLOB_URL" "$OUTPUT_DIR"

else
    echo "No video link or uploaded file provided - OBS Recording - "
    exit 0
fi

echo "PROCESSING VIDEO PIPELINE..."
exit 0
