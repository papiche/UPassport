#!/bin/bash
################################################################### g1.sh
# Author: Fred (DsEx1pS33vzYZg4MroyBV9hCw98j1gtHEhwiZ5tK7ech)
# Version: 1.0
# License: AGPL-3.0 (https://choosealicense.com/licenses/agpl-3.0/)
################################################################################
# Output is an HTML file - sent back to user through 54321 API
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
    echo "Usage: $0 <email> <lang> <lat> <lon> [salt] [pepper]"
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
            ## Same-day re-request
            # Check if .multipass.json exists and contains all fields (like email/lat/lon) 
            # and ensure G1PUB is V2 (starts with g1). If not, reconstruct it.
            JSON_FILE="${HOME}/.zen/game/nostr/${EMAIL}/.multipass.json"
            RECONSTRUCT="no"
            if [[ ! -f "$JSON_FILE" ]]; then
                RECONSTRUCT="yes"
            else
                # Check for missing fields (email) or V1 address
                if [[ "$(grep -c "email" "$JSON_FILE")" -eq 0 ]]; then RECONSTRUCT="yes"; fi
                G1PUB_JSON=$(grep -o '"g1pub": *"[^"]*"' "$JSON_FILE" 2>/dev/null | cut -d'"' -f4)
                if [[ "${G1PUB_JSON:0:2}" != "g1" ]]; then RECONSTRUCT="yes"; fi
            fi

            if [[ "$RECONSTRUCT" == "yes" ]]; then
                # Reconstruct .multipass.json from existing files
                G1PUB=$(cat "${HOME}/.zen/game/nostr/${EMAIL}/G1PUBNOSTR" 2>/dev/null)
                # Ensure G1PUB is V2 (SS58) if possible
                if [[ -x "${MY_PATH}/../Astroport.ONE/tools/g1pub_to_ss58.py" ]]; then
                    _g1v2=$(python3 "${MY_PATH}/../Astroport.ONE/tools/g1pub_to_ss58.py" "$G1PUB" 2>/dev/null)
                    [[ -n "$_g1v2" ]] && G1PUB="$_g1v2"
                fi


                NOSTRNS=$(cat "${HOME}/.zen/game/nostr/${EMAIL}/NOSTRNS" 2>/dev/null)
                
                # Extract keys from .secret.nostr
                if [[ -f "${HOME}/.zen/game/nostr/${EMAIL}/.secret.nostr" ]]; then
                    source "${HOME}/.zen/game/nostr/${EMAIL}/.secret.nostr"
                    # This loads NSEC, NPUB, HEX
                fi
                
                # Extract SSSS from .ssss.player.key (format M-...)
                SSSS_KEY=$(cat "${HOME}/.zen/game/nostr/${EMAIL}/.ssss.player.key" 2>/dev/null)
                
                # Extract SALT/PEPPER from .secret.disco
                if [[ -f "${HOME}/.zen/game/nostr/${EMAIL}/.secret.disco" ]]; then
                    DISCO_CONTENT=$(cat "${HOME}/.zen/game/nostr/${EMAIL}/.secret.disco")
                    # Format: /?salt=...&nostr=...
                    SALT=$(echo "$DISCO_CONTENT" | grep -o 'salt=[^&]*' | cut -d= -f2)
                    PEPPER=$(echo "$DISCO_CONTENT" | grep -o 'nostr=[^&]*' | cut -d= -f2)
                fi
                
                # Read lat/lon from GPS file if available
                ZLAT="$LAT"
                ZLON="$LON"
                if [[ -f "${HOME}/.zen/game/nostr/${EMAIL}/GPS" ]]; then
                    # GPS file format: LAT=...; LON=...;
                    ZLAT=$(grep -o 'LAT=[^;]*' "${HOME}/.zen/game/nostr/${EMAIL}/GPS" | cut -d= -f2)
                    ZLON=$(grep -o 'LON=[^;]*' "${HOME}/.zen/game/nostr/${EMAIL}/GPS" | cut -d= -f2)
                fi

                cat > "$JSON_FILE" <<EOFJSON
{
  "g1pub": "${G1PUB}",
  "nsec": "${NSEC}",
  "npub": "${NPUB}",
  "hex": "${HEX}",
  "ssss": "${SSSS_KEY}",
  "nostrns": "${NOSTRNS}",
  "salt": "${SALT}",
  "pepper": "${PEPPER}",
  "email": "${EMAIL}",
  "lat": "${ZLAT}",
  "lon": "${ZLON}",
  "uplanetname_g1": "${UPLANETNAME_G1}"
}
EOFJSON
            fi

            echo ${HOME}/.zen/game/nostr/${EMAIL}/.nostr.zine.html
            exit 0
        fi
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
