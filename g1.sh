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
    EMAIL="${EMAIL// }" 
    echo "Email detected: $EMAIL"

    ## SEARCH FOR EXISTING MULTIPASS
    if [[ -n $($HOME/.zen/Astroport.ONE/tools/search_for_this_email_in_nostr.sh ${EMAIL}) ]]; then
        # Check if not TODATE made account
        BIRTHDAY=$(cat ${HOME}/.zen/game/nostr/${EMAIL}/TODATE)
        if [[ $BIRTHDAY != $TODATE ]]; then
            ## Existing account from a previous day - no salt/pepper available
            cat ${MY_PATH}/templates/message.html \
            | sed -e "s~_TITLE_~$(date -u) <br> ${EMAIL}~g" \
                -e "s~_MESSAGE_~EXISTING MULTIPASS~g" \
                > ${MY_PATH}/tmp/${MOATS}.out.html
            echo "${MY_PATH}/tmp/${MOATS}.out.html"
            exit 0
        else
            ## Same-day re-reques
            echo ${HOME}/.zen/game/nostr/${EMAIL}/.nostr.zine.html
            exit 0
        fi
    fi

    ## Générer diceware ZEN Card si non fournis — via diceware.sh (wordlist officielle)
    DICEWARE_SH="${HOME}/.zen/Astroport.ONE/tools/diceware.sh"
    if [[ -z "$SALT" ]]; then
        if [[ -x "$DICEWARE_SH" ]]; then
            SALT=$("$DICEWARE_SH" 4 | tr -d '\n' | sed 's/ *$//' | tr ' ' '-')
        else
            SALT=$(tr -dc 'a-z0-9' < /dev/urandom | fold -w20 | head -n1)
        fi
        echo "🎲 ZEN Card SALT diceware auto : ${SALT}" >&2
    fi
    if [[ -z "$PEPPER" ]]; then
        if [[ -x "$DICEWARE_SH" ]]; then
            PEPPER=$("$DICEWARE_SH" 4 | tr -d '\n' | sed 's/ *$//' | tr ' ' '-')
        else
            PEPPER=$(tr -dc 'a-z0-9' < /dev/urandom | fold -w20 | head -n1)
        fi
        echo "🎲 ZEN Card PEPPER diceware auto : ${PEPPER}" >&2
    fi

    ### SEARCH FOR EXISTING NOSTR CARD
    if [[ ! -s ${HOME}/.zen/game/nostr/${EMAIL}/.nostr.zine.html ]]; then
        ### CREATING NOSTR CARD with SALT PEPPER
        ${HOME}/.zen/Astroport.ONE/tools/make_NOSTRCARD.sh "${EMAIL}" "$LANG" "${LAT}" "${LON}" "${SALT}" "${PEPPER}"
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
