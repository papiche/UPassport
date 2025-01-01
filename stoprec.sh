#!/bin/bash
##################################################################### stoprec.sh
# Author: Fred (DsEx1pS33vzYZg4MroyBV9hCw98j1gtHEhwiZ5tK7ech)
# Version: 1.0
# License: AGPL-3.0 (https://choosealicense.com/licenses/agpl-3.0/)
###############################################################################
MY_PATH="`dirname \"$0\"`"              # relative
MY_PATH="`( cd \"$MY_PATH\" && pwd )`"  # absolutized and normalized
ME="${0##*/}"
MOATS=$(date -u +"%Y%m%d%H%M%S%4N")
export PATH=$HOME/.local/bin:$PATH

source ${MY_PATH}/.env

# Vérifier le nombre d'arguments
if [ "$#" -ne 1 ]; then
    echo "Usage: $0 <email>"
    exit 1
fi

# Récupération des arguments
PLAYER=$1
echo "${PLAYER} willing make OBS STOP"

## Is it a local PLAYER
[[ ! -d ~/.zen/game/players/${PLAYER} ]] \
    && echo "UNKNOWN PLAYER ${PLAYER}" \
    && exit 1

## STOP RECORDING
obs-cmd --websocket obsws://127.0.0.1:4455/${OBSkey} recording stop
output=$(obs-cmd --websocket obsws://127.0.0.1:4455/${OBSkey} recording stop)
echo "$output"
filepath=$(echo "$output" | grep -oP '(?<=Result: Ok\(")[^"]+')
directory=$(dirname "$filepath")
filename=$(basename "$filepath")
echo "Le nom du fichier est : $filename"
fname=$(echo "${filename}" | sed -r 's/\<./\U&/g' | sed 's/ //g')

$(~/.zen/Astroport.ONE/tools/search_for_this_email_in_players.sh ${PLAYER} | tail -n 1)

## RECORD INTO TW
# ex: /home/$YOU/Astroport/${PLAYER}/... TyPE(film, youtube, mp3, video, pdf)/ REFERENCE /
mkdir -p ~/Astroport/${PLAYER}/video/${MOATS}/
mv "$filepath" ~/Astroport/${PLAYER}/video/${MOATS}/$fname \
    && directory=$HOME/Astroport/${PLAYER}/video/${MOATS}

## IPFS SWALLOW : new_file_in_astroport.sh
~/.zen/Astroport.ONE/tools/new_file_in_astroport.sh "$directory" "$fname" "$ASTROG1" "$PLAYER"
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


