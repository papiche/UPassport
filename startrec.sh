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

## Is it a local NOSTR Card
[[ ! -d ~/.zen/game/player/${PLAYER} ]] \
    && echo "UNKNOWN PLAYER ${PLAYER}" \
    && exit 1

process_video() {
    local video_file=$1
    local astrog1=$2

    filename=$(basename "$video_file")
    echo "Le nom du fichier est : $filename"
    fname=$(echo "${filename}" | sed -r 's/\<./\U&/g' | sed 's/ //g')
    ## RECORD INTO TW
    # ex: /home/$YOU/Astroport/${PLAYER}/... TyPE(film, youtube, mp3, video, pdf)/ REFERENCE /
    mkdir -p ~/Astroport/${PLAYER}/video/${MOATS}/
    mv "$video_file" ~/Astroport/${PLAYER}/video/${MOATS}/$fname \
        && directory=$HOME/Astroport/${PLAYER}/video/${MOATS}

    ## IPFS SWALLOW : new_file_in_astroport.sh
    ~/.zen/Astroport.ONE/tools/new_file_in_astroport.sh "$directory" "$fname" "$astrog1" "$PLAYER"
    ## LOG RESULT
    cat $HOME/Astroport/${PLAYER}/video/${MOATS}/VIDEO_${MOATS}.dragdrop.json | jq -rc

    ###################################################################################
    if [[ -s $HOME/Astroport/${PLAYER}/video/${MOATS}/VIDEO_${MOATS}.dragdrop.json ]]; then
        ## ADD TIDDLER TO TW
        (
        $MY_PATH/tools/import_tiddler.sh ~/.zen/game/players/${PLAYER}/ipfs/moa/index.html $HOME/Astroport/${PLAYER}/video/${MOATS}/VIDEO_${MOATS}.dragdrop.json
        ###############################
        IPFSPOP=$(ipfs add -rwq ~/.zen/game/players/${PLAYER}/ipfs/moa/index.html | tail -n 1)
        ipfs --timeout 120s name publish -k ${PLAYER} /ipfs/${IPFSPOP}
        ) &
        echo "% PUBLISHING ${PLAYER} ${myIPFS}/ipfs/${IPFSPOP}"
        exit 0
    else
        echo "Astroport Swallowing failed"
        exit 1
    fi
}


$(~/.zen/Astroport.ONE/tools/search_for_this_email_in_players.sh ${PLAYER} | tail -n 1)
echo "${PLAYER} /REC =========================== $ASTROG1 "
OUTPUT_DIR="$HOME/Astroport/$PLAYER/video/$MOATS"
mkdir -p "$OUTPUT_DIR"

# Traitement du lien YouTube ou du fichier uploadé
if [[ "$2" =~ ^link=(.*)$ ]]; then
    VIDEO_LINK="${BASH_REMATCH[1]}"
    echo "Received video link: $VIDEO_LINK"
    (
    yt-dlp -f 'bv[height<=720]+ba[language=fr]/b[height<=720]' -o "$OUTPUT_DIR/%(title)s.%(ext)s" "$VIDEO_LINK"

    if [ ! $? -eq 0 ]; then
        yt-dlp -f 'b[height<=360]+ba[language=fr]/best[height<=360]' -o "$OUTPUT_DIR/%(title)s.%(ext)s" "$VIDEO_LINK"
    fi

    UPLOADED_FILE="$(yt-dlp --get-filename -o "%(title)s.%(ext)s" "$VIDEO_LINK")"
    echo "Video downloaded and saved to: $UPLOADED_FILE"

    process_video "$OUTPUT_DIR/$UPLOADED_FILE" "$ASTROG1"
    ) &

elif [[ "$2" =~ ^upload=(.*)$ ]]; then
    UPLOADED_FILE="${BASH_REMATCH[1]}"
    echo "Received uploaded file: $UPLOADED_FILE"

    cp "$UPLOADED_FILE" "$OUTPUT_DIR"
    process_video "$OUTPUT_DIR/$(basename "$UPLOADED_FILE")" "$ASTROG1"

elif [[ "$2" =~ ^blob=(.*)$ ]]; then
    BLOB_URL="${BASH_REMATCH[1]}"
    echo "Received blob URL: $BLOB_URL"

    process_video "$BLOB_URL" "$ASTROG1"

else
    echo "No video link or uploaded file provided - OBS Recording - "
    exit 0
fi

echo "PROCESSING VIDEO PIPELINE..."
exit 0
