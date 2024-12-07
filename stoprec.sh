#!/bin/bash
################################################################################
# RECEIVE SEND SEN COMMAND --- 1ST need ZenCard "AstroID" + PASS succesful scan
################################################################################
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
filename=$(basename "$filepath")
echo "Le nom du fichier est : $filename"

exit 0
