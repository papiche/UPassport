#!/bin/bash
################################################################### g1.sh
# Author: Fred (DsEx1pS33vzYZg4MroyBV9hCw98j1gtHEhwiZ5tK7ech)
# Version: 1.0
# License: AGPL-3.0 (https://choosealicense.com/licenses/agpl-3.0/)
################################################################################
# Output Zine Html web page and send it to email
echo "Usage: $0 <email> <lang> <salt> <pepper>"
MOATS=$(date -u +"%Y%m%d%H%M%S%4N")
MY_PATH="`dirname \"$0\"`"              # relative
MY_PATH="`( cd \"$MY_PATH\" && pwd )`"  # absolutized and normalized
ME="${0##*/}"
################################################################### INIT
## LOAD ASTROPORT ENVIRONMENT
[ ! -s $HOME/.zen/Astroport.ONE/tools/my.sh ] \
    && echo "<h1>ERROR/ Missing Astroport.ONE. Please install...<h1>" \
    && exit 1
source "$HOME/.zen/Astroport.ONE/tools/my.sh"

####################################################################
EMAIL="$1"
LANG="$2"
LAT="$3"
LON="$4"
SALT="$5"
PEPPER="$6"

if [[ "$#" -lt 6 ]]; then
    echo "Error: Missing parameters."
    cat ${MY_PATH}/templates/message.html \
    | sed -e "s~_TITLE_~$(date -u) <br> ${EMAIL}~g" \
         -e "s~_MESSAGE_~Missing parameters~g" \
        > ${MY_PATH}/tmp/${MOATS}.out.html
    echo "${MY_PATH}/tmp/${MOATS}.out.html"
    exit 0
fi



if [[ $EMAIL =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; then
    EMAIL="${EMAIL,,}"
    echo "Email detected: $EMAIL"

    ## SEARCH FOR EXISTING ACCOUNT
    if [[ -n $($HOME/.zen/Astroport.ONE/tools/search_for_this_email_in_nostr.sh ${EMAIL}) ]]; then
        cat ${MY_PATH}/templates/message.html \
        | sed -e "s~_TITLE_~$(date -u) <br> ${EMAIL}~g" \
             -e "s~_MESSAGE_~♥️BOX ACCOUNT~g" \
            > ${MY_PATH}/tmp/${MOATS}.out.html
        echo "${MY_PATH}/tmp/${MOATS}.out.html"
        exit 0
    fi

    ### SEARCH FOR EXISTING NOSTR CARD
    if [[ ! -s ${HOME}/.zen/game/nostr/${EMAIL}/.nostr.zine.html ]]; then
        ### CREATING NOSTR CARD with SALT PEPPER
        ${HOME}/.zen/Astroport.ONE/tools/make_NOSTRCARD.sh "${EMAIL}" "$LANG" "${LAT}" "${LON}" "${SALT}" "${PEPPER}"
        ## MAILJET SEND NOSTR CARD
        ${HOME}/.zen/Astroport.ONE/tools/mailjet.sh "${EMAIL}" "${HOME}/.zen/game/nostr/${EMAIL}/.nostr.zine.html" "UPlanet NOSTR Card"
        echo "${HOME}/.zen/game/nostr/${EMAIL}/.nostr.zine.html"
        exit 0
    else
        if [[ "$(cat ${HOME}/.zen/game/nostr/${EMAIL}/TODATE)" == "$TODATE" ]]; then
            echo "${HOME}/.zen/game/nostr/${EMAIL}/.nostr.zine.html"
            exit 0
        fi
    fi

    ## ALREADY EXISTING
    cat ${MY_PATH}/templates/message.html \
    | sed -e "s~_TITLE_~$(date -u) <br> ${EMAIL}~g" \
         -e "s~_MESSAGE_~NOSTR CARD ALREADY EXISTING~g" \
        > ${MY_PATH}/tmp/${MOATS}.out.html
    echo "${MY_PATH}/tmp/${MOATS}.out.html"
    exit 0

else
    cat ${MY_PATH}/templates/message.html \
    | sed -e "s~_TITLE_~$(date -u) <br> ${EMAIL}~g" \
         -e "s~_MESSAGE_~ERROR~g" \
        > ${MY_PATH}/tmp/${MOATS}.out.html
    echo "${MY_PATH}/tmp/${MOATS}.out.html"
    exit 0
fi
