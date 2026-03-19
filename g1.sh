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
## Write .multipass.json sidecar with credentials + MULTIPASS data
write_multipass_json() {
    # ARCHITECTURE v1→v2 :
    # _SALT/_PEPPER = clés ZEN Card (user-provided, mémorisables)
    # MULTIPASS NSEC = aléatoire → lu depuis .secret.nostr (DISCO protégé par SSSS QR)
    local _EMAIL="$1" _ZENCARD_SALT="$2" _ZENCARD_PEPPER="$3"
    local NOSTR_DIR="${HOME}/.zen/game/nostr/${_EMAIL}"
    local JSON_FILE="${NOSTR_DIR}/.multipass.json"
    [[ ! -d "$NOSTR_DIR" ]] && return 1
    local _G1PUB=$(cat "${NOSTR_DIR}/G1PUBNOSTR" 2>/dev/null)
    local _NPUB=$(cat "${NOSTR_DIR}/NPUB" 2>/dev/null)
    local _HEX=$(cat "${NOSTR_DIR}/HEX" 2>/dev/null)
    local _NOSTRNS=$(cat "${NOSTR_DIR}/NOSTRNS" 2>/dev/null)
    local _GPS=$(cat "${NOSTR_DIR}/GPS" 2>/dev/null)
    local _LAT=$(echo "$_GPS" | grep -oP 'LAT=\K[^;]+')
    local _LON=$(echo "$_GPS" | grep -oP 'LON=\K[^;]+')
    local _SSSS_PLAYER=$(cat "${NOSTR_DIR}/.ssss.player.key" 2>/dev/null)
    ## MULTIPASS NSEC : toujours aléatoire → lu depuis .secret.nostr (pas dérivé du ZEN Card SALT/PEPPER)
    local _NSEC=$(grep -oP 'NSEC=\K[^;]+' "${NOSTR_DIR}/.secret.nostr" 2>/dev/null)
    ## ZEN Card G1 wallet (dérivé des clés mémorisables de l'utilisateur)
    local _ZENCARD_G1PUB=""
    [[ -z "$_ZENCARD_G1PUB" ]] \
        && _ZENCARD_G1PUB=$(cat "${HOME}/.zen/game/players/${_EMAIL}/.g1pub" 2>/dev/null)
    cat > "$JSON_FILE" <<EOJSON
{
  "email": "${_EMAIL}",
  "zencard_salt": "${_ZENCARD_SALT}",
  "zencard_pepper": "${_ZENCARD_PEPPER}",
  "zencard_g1pub": "${_ZENCARD_G1PUB}",
  "nsec": "${_NSEC}",
  "g1pub": "${_G1PUB}",
  "npub": "${_NPUB}",
  "hex": "${_HEX}",
  "nostrns": "${_NOSTRNS}",
  "lat": "${_LAT}",
  "lon": "${_LON}",
  "ssss_player": "${_SSSS_PLAYER}"
}
EOJSON
    echo "Wrote ${JSON_FILE}" >&2
}

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
            ## Same-day re-request - salt/pepper still in params
            write_multipass_json "${EMAIL}" "${SALT}" "${PEPPER}"
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
        ## NOMAIL=1 : mailjet called below with custom subject (avoid double send)
        NOMAIL=1 ${HOME}/.zen/Astroport.ONE/tools/make_NOSTRCARD.sh "${EMAIL}" "$LANG" "${LAT}" "${LON}" "${SALT}" "${PEPPER}"
        ## MAILJET SEND NOSTR CARD
        YOUSER=$(${HOME}/.zen/Astroport.ONE/tools/clyuseryomail.sh ${EMAIL})
        ${HOME}/.zen/Astroport.ONE/tools/mailjet.sh --expire 0s "${EMAIL}" "${HOME}/.zen/game/nostr/${EMAIL}/.nostr.zine.html" "MULTIPASS[Ğ1] [UPlanet:${UPLANETG1PUB:0:8}:${LAT}:${LON}]"
        write_multipass_json "${EMAIL}" "${SALT}" "${PEPPER}"
        echo "${HOME}/.zen/game/nostr/${EMAIL}/.nostr.zine.html"
        exit 0
    else
        if [[ "$(cat ${HOME}/.zen/game/nostr/${EMAIL}/TODATE)" == "$TODATE" ]]; then
            write_multipass_json "${EMAIL}" "${SALT}" "${PEPPER}"
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
