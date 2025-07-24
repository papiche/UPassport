#!/bin/bash
################################################################### g1.sh
# Author: Fred (DsEx1pS33vzYZg4MroyBV9hCw98j1gtHEhwiZ5tK7ech)
# Version: 1.0
# License: AGPL-3.0 (https://choosealicense.com/licenses/agpl-3.0/)
################################################################################
# Output is an HTML file - sent back to user through 54321 API
echo "Usage: $0 <email> <lang> <lat> <lon> [salt] [pepper]"
echo "  email:   Email address (required)"
echo "  lang:    Language code (required)"
echo "  lat:     Latitude (required)"
echo "  lon:     Longitude (required)"
echo "  salt:    Salt for key generation (optional, auto-generated if not provided)"
echo "  pepper:  Pepper for key generation (optional, auto-generated if not provided)"
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

if [[ "$#" -lt 4 ]]; then
    echo "Error: Missing required parameters (email, lang, lat, lon)." >&2
    cat ${MY_PATH}/templates/message.html \
    | sed -e "s~_TITLE_~$(date -u) <br> ${EMAIL}~g" \
         -e "s~_MESSAGE_~Missing required parameters~g" \
        > ${MY_PATH}/tmp/${MOATS}.out.html
    echo "${MY_PATH}/tmp/${MOATS}.out.html"
    exit 0
fi

if [[ $EMAIL =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; then
    EMAIL="${EMAIL,,}"
    echo "Email detected: $EMAIL"

    ## SEARCH FOR EXISTING MULTIPASS
    if [[ -n $($HOME/.zen/Astroport.ONE/tools/search_for_this_email_in_nostr.sh ${EMAIL}) ]]; then
        # Check if not TODATE made account
        BIRTHDAY=$(cat ${HOME}/.zen/game/nostr/${EMAIL}/TODATE)
        if [[ $BIRTHDAY != $TODATE ]]; then
            cat ${MY_PATH}/templates/message.html \
            | sed -e "s~_TITLE_~$(date -u) <br> ${EMAIL}~g" \
                -e "s~_MESSAGE_~EXISTING MULTIPASS~g" \
                > ${MY_PATH}/tmp/${MOATS}.out.html
            echo "${MY_PATH}/tmp/${MOATS}.out.html"
            exit 0
        else
            echo ${HOME}/.zen/game/nostr/${EMAIL}/.nostr.zine.html
            exit 0
        fi
    fi
    # Generate random salt and pepper if not provided
    if [[ -z "$SALT" ]]; then
        SALT=$(tr -dc 'a-zA-Z0-9' < /dev/urandom | fold -w42 | head -n1)
        echo "Generated random salt: ${SALT:0:10}..." >&2
    fi

    if [[ -z "$PEPPER" ]]; then
        PEPPER=$(tr -dc 'a-zA-Z0-9' < /dev/urandom | fold -w42 | head -n1)
        echo "Generated random pepper: ${PEPPER:0:10}..." >&2
    fi

    ### SEARCH FOR EXISTING NOSTR CARD
    if [[ ! -s ${HOME}/.zen/game/nostr/${EMAIL}/.nostr.zine.html ]]; then
        ### CREATING NOSTR CARD with SALT PEPPER
        ${HOME}/.zen/Astroport.ONE/tools/make_NOSTRCARD.sh "${EMAIL}" "$LANG" "${LAT}" "${LON}" "${SALT}" "${PEPPER}"
        ## MAILJET SEND NOSTR CARD
        YOUSER=$(${HOME}/.zen/Astroport.ONE/tools/clyuseryomail.sh ${EMAIL})
        ${HOME}/.zen/Astroport.ONE/tools/mailjet.sh "${EMAIL}" "${HOME}/.zen/game/nostr/${EMAIL}/.nostr.zine.html" "Ğ1 UPlanet MULTIPASS ${YOUSER}"
        echo "${HOME}/.zen/game/nostr/${EMAIL}/.nostr.zine.html"
        exit 0
    else
        if [[ "$(cat ${HOME}/.zen/game/nostr/${EMAIL}/TODATE)" == "$TODATE" ]]; then
            echo "${HOME}/.zen/game/nostr/${EMAIL}/.nostr.zine.html"
            exit 0
        fi
    fi

else
    cat ${MY_PATH}/templates/message.html \
    | sed -e "s~_TITLE_~$(date -u) <br> ${EMAIL}~g" \
         -e "s~_MESSAGE_~SYNTAX ERROR~g" \
        > ${MY_PATH}/tmp/${MOATS}.out.html
    echo "${MY_PATH}/tmp/${MOATS}.out.html"
    exit 0
fi
