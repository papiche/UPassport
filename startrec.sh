#!/bin/bash
###################################################################### startrec.sh
# RECEIVE SEND SEN COMMAND --- 1ST need ZenCard "AstroID" + PASS succesful scan
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
echo "${PLAYER} /REC ================================= "

# Vérifier si un lien est fourni
if [[ "$2" =~ ^link=(.*)$ ]]; then
  VIDEO_LINK="${BASH_REMATCH[1]}"
  echo "Received video link: $VIDEO_LINK"

  # Validation du lien
    if [[ "$VIDEO_LINK" =~ ^(http|https):// ]]; then

      # Créer le répertoire de destination s'il n'existe pas
       OUTPUT_DIR="$HOME/Astroport/$PLAYER/REC"
        mkdir -p "$OUTPUT_DIR"

        # Utiliser yt-dlp pour télécharger la vidéo
        yt-dlp -o "$OUTPUT_DIR/%(title)s.%(ext)s" "$VIDEO_LINK"

        if [ $? -eq 0 ]; then
          echo "Video downloaded successfully to $OUTPUT_DIR."
        else
          echo "Failed to download video using yt-dlp."
        fi
        exit 0

    else
    echo "Invalid video link format."
    exit 1
  fi
fi

# Vérifier si un fichier uploadé est fourni
if [[ "$2" =~ ^upload=(.*)$ ]]; then
  UPLOADED_FILE="${BASH_REMATCH[1]}"
    echo "Received uploaded file: $UPLOADED_FILE"

   # Verifier que le fichier existe
    if [[ -f "$UPLOADED_FILE" ]]; then
       # Créer le répertoire de destination s'il n'existe pas
       OUTPUT_DIR="$HOME/Astroport/$PLAYER/REC"
       mkdir -p "$OUTPUT_DIR"

      # Déplacer le fichier vers le répertoire de destination
        cp "$UPLOADED_FILE" "$OUTPUT_DIR"
        if [ $? -eq 0 ]; then
          echo "Video uploaded successfully to $OUTPUT_DIR."
        else
            echo "Failed to move the uploaded video."
        fi
        exit 0
    else
       echo "Uploaded file not found"
       exit 1
  fi
fi


## Is it a local PLAYER
[[ ! -d ~/.zen/game/players/${PLAYER} ]] \
    && echo "UNKNOWN PLAYER ${PLAYER}" \
    && exit 1

## SEARCHING FOR SWARM REGISTERED PLAYER
#~ ~/.zen/Astroport.ONE/tools/search_for_this_email_in_players.sh ${PLAYER}

exit 0
