#!/bin/bash
##################################################################### zen_send.sh
################################################################################
# Author: Fred (DsEx1pS33vzYZg4MroyBV9hCw98j1gtHEhwiZ5tK7ech)
# Version: 1.0
# License: AGPL-3.0 (https://choosealicense.com/licenses/agpl-3.0/)
###############################################################################
MY_PATH="`dirname \"$0\"`"              # relative
MY_PATH="`( cd \"$MY_PATH\" && pwd )`"  # absolutized and normalized
ME="${0##*/}"
MOATS=$(date -u +"%Y%m%d%H%M%S%4N")
export PATH=$HOME/.astro/bin:$HOME/.local/bin:$PATH

source ${HOME}/.zen/Astroport.ONE/tools/my.sh
source ${MY_PATH}/.env


# Arguments: require 4 or 5 (Nostr sender hex mandatory; optional player id when g1dest=PLAYER)
if [ "$#" -lt 4 ] || [ "$#" -gt 5 ]; then
    echo "Usage: $0 <zen> <g1source> <g1dest> <sender_hex> [player_id]"
    exit 1
fi

# Collect arguments
ZEN=$1
G1SOURCE=$2
G1DEST=$3
SENDER_HEX=${4}
PLAYER_ID=${5:-}

# If Nostr sender is provided, verify recent NIP-42 auth and mapping to G1SOURCE
if [[ -n "$SENDER_HEX" ]]; then
    echo "# Nostr sender provided: ${SENDER_HEX:0:8}..."

    # 1) Check auth marker created by backend
    AUTH_MARKER="$HOME/.zen/tmp/nostr_auth_ok_${SENDER_HEX}"
    if [[ ! -s "$AUTH_MARKER" ]]; then
        cat ${MY_PATH}/templates/message.html \
        | sed -e "s~_TITLE_~$(date -u) <br> AUTH~g" \
             -e "s~_MESSAGE_~Nostr authentication missing. Please login again.~g" \
            > ${MY_PATH}/tmp/${MOATS}.out.html
        echo "${MY_PATH}/tmp/${MOATS}.out.html"
        exit 0
    fi

    NOW_TS=$(date +%s)
    AUTH_TS=$(cat "$AUTH_MARKER" 2>/dev/null)
    # Accept auth within last 24 hours
    if [[ -n "$AUTH_TS" ]]; then
        DIFF=$(( NOW_TS - AUTH_TS ))
        if [[ $DIFF -gt 86400 ]]; then
            cat ${MY_PATH}/templates/message.html \
            | sed -e "s~_TITLE_~$(date -u) <br> AUTH~g" \
                 -e "s~_MESSAGE_~Nostr authentication expired. Please re-authenticate.~g" \
                > ${MY_PATH}/tmp/${MOATS}.out.html
            echo "${MY_PATH}/tmp/${MOATS}.out.html"
            exit 0
        fi
    fi

    # 2) Resolve expected G1 pubkey from Nostr hex (sender wallet mapping)
    EXPECTED_G1=$(~/.zen/Astroport.ONE/tools/search_for_this_hex_in_uplanet.sh "$SENDER_HEX" 2>/dev/null)
    if [[ -z "$EXPECTED_G1" ]]; then
        cat ${MY_PATH}/templates/message.html \
        | sed -e "s~_TITLE_~$(date -u) <br> AUTH~g" \
             -e "s~_MESSAGE_~No G1 wallet found for this Nostr key on UPlanet.~g" \
            > ${MY_PATH}/tmp/${MOATS}.out.html
        echo "${MY_PATH}/tmp/${MOATS}.out.html"
        exit 0
    fi

    # 3) If no source provided, adopt resolved wallet; otherwise ensure match
    if [[ -z "$G1SOURCE" ]]; then
        echo "# No G1SOURCE provided. Adopting resolved wallet from Nostr mapping"
        G1SOURCE="$EXPECTED_G1"
    elif [[ "$EXPECTED_G1" != "$G1SOURCE" ]]; then
        cat ${MY_PATH}/templates/message.html \
        | sed -e "s~_TITLE_~$(date -u) <br> AUTH~g" \
             -e "s~_MESSAGE_~Source wallet mismatch. Refusing to send.\nExpected: ${EXPECTED_G1:0:12}...\nProvided: ${G1SOURCE:0:12}...~g" \
            > ${MY_PATH}/tmp/${MOATS}.out.html
        echo "${MY_PATH}/tmp/${MOATS}.out.html"
        exit 0
    fi
fi

# Resolve destination shortcuts
if [[ "$G1DEST" == "CAPTAIN" ]]; then
    # Convert CAPTAIN to actual G1 pubkey from CAPTAINEMAIL
    if [[ -z "$CAPTAINEMAIL" ]]; then
        cat ${MY_PATH}/templates/message.html \
        | sed -e "s~_TITLE_~$(date -u) <br> DEST~g" \
             -e "s~_MESSAGE_~CAPTAINEMAIL is not configured on server.~g" \
            > ${MY_PATH}/tmp/${MOATS}.out.html
        echo "${MY_PATH}/tmp/${MOATS}.out.html"
        exit 0
    fi
    if [[ ! -s "$HOME/.zen/game/nostr/${CAPTAINEMAIL}/G1PUBNOSTR" ]]; then
        cat ${MY_PATH}/templates/message.html \
        | sed -e "s~_TITLE_~$(date -u) <br> DEST~g" \
             -e "s~_MESSAGE_~Captain G1PUBNOSTR not found for ${CAPTAINEMAIL}.~g" \
            > ${MY_PATH}/tmp/${MOATS}.out.html
        echo "${MY_PATH}/tmp/${MOATS}.out.html"
        exit 0
    fi
    G1DEST=$(cat "$HOME/.zen/game/nostr/${CAPTAINEMAIL}/G1PUBNOSTR" | tr -d '\n' )
    echo "# Resolved CAPTAIN destination: ${G1DEST:0:12}..."
elif [[ "$G1DEST" == "PLAYER" ]]; then
    # Convert PLAYER to actual G1 pubkey from player identifier
    if [[ -z "$PLAYER_ID" ]]; then
        cat ${MY_PATH}/templates/message.html \
        | sed -e "s~_TITLE_~$(date -u) <br> DEST~g" \
             -e "s~_MESSAGE_~PLAYER destination requires player_id (email or hex).~g" \
            > ${MY_PATH}/tmp/${MOATS}.out.html
        echo "${MY_PATH}/tmp/${MOATS}.out.html"
        exit 0
    fi
    # If email, use search_for_this_email_in_players.sh
    if [[ "$PLAYER_ID" == *"@"* ]]; then
        echo "# Resolving PLAYER by email: $PLAYER_ID"
        $(~/.zen/Astroport.ONE/tools/search_for_this_email_in_players.sh "$PLAYER_ID" | tail -n 1)
        if [[ -z ${ASTROG1} ]]; then
            cat ${MY_PATH}/templates/message.html \
            | sed -e "s~_TITLE_~$(date -u) <br> DEST~g" \
                 -e "s~_MESSAGE_~PLAYER not found by email: ${PLAYER_ID}.~g" \
                > ${MY_PATH}/tmp/${MOATS}.out.html
            echo "${MY_PATH}/tmp/${MOATS}.out.html"
            exit 0
        fi
        G1DEST="$ASTROG1"
        echo "# Resolved PLAYER G1 by email: ${G1DEST:0:12}..."
    else
        # Assume hex; resolve to G1 using search_for_this_hex_in_uplanet.sh
        echo "# Resolving PLAYER by hex: ${PLAYER_ID:0:12}..."
        PLAYER_G1=$(~/.zen/Astroport.ONE/tools/search_for_this_hex_in_uplanet.sh "$PLAYER_ID" 2>/dev/null)
        if [[ -z "$PLAYER_G1" ]]; then
            cat ${MY_PATH}/templates/message.html \
            | sed -e "s~_TITLE_~$(date -u) <br> DEST~g" \
                 -e "s~_MESSAGE_~PLAYER not found by hex: ${PLAYER_ID:0:12}....~g" \
                > ${MY_PATH}/tmp/${MOATS}.out.html
            echo "${MY_PATH}/tmp/${MOATS}.out.html"
            exit 0
        fi
        G1DEST="$PLAYER_G1"
        echo "# Resolved PLAYER G1 by hex: ${G1DEST:0:12}..."
    fi
fi


## CHECK FOR SOURCE DUNIKEY - DEPRECATED (was used in UPassport/N1 App) - draft code
if [[ -s ${MY_PATH}/tmp/${G1SOURCE}.zencard.dunikey ]]; then

    echo "# getting ${G1SOURCE} balance : "
    SRCCOINS=$(~/.zen/Astroport.ONE/tools/G1check.sh ${G1SOURCE} | tail -n 1)
    SRCZEN=$(echo "($SRCCOINS - 1) * 10" | bc | cut -d '.' -f 1)
    echo "# verify $SRCZEN > $ZEN ? "
    if [[ $(echo "$SRCZEN < $ZEN" | bc) == 1 || $(echo "$SRCZEN < 10" | bc) == 1 ]]; then
        cat ${MY_PATH}/templates/message.html \
        | sed -e "s~_TITLE_~$(date -u) <br> ${G1SOURCE}~g" \
             -e "s~_MESSAGE_~ ˁ(OᴥO)ˀ <br> MISSING ẐEN <br>$SRCZEN < $ZEN~g" \
            > ${MY_PATH}/tmp/${MOATS}.out.html
        echo "${MY_PATH}/tmp/${MOATS}.out.html"
        exit 0
    fi

    echo "# checking SOURCE primal transaction : ${G1SOURCE} "
    if [[ ! -s  ~/.zen/tmp/coucou/${G1SOURCE}.prime8 ]]; then
        srcprime=$(silkaj money history ${G1SOURCE} | tail -n 3 | head -n 1)
        srcprime8=$(echo $srcprime | awk -F'│' '{gsub(/[[:space:]]*/, "", $3); split($3, a, ":"); print substr(a[1], 1, 8)}')
        echo "srcprime = $srcprime"
        echo "srcprime8 = $srcprime8"
        echo "$srcprime8" > ~/.zen/tmp/coucou/${G1SOURCE}.prime8
    else
        srcprime8=$(cat ~/.zen/tmp/coucou/${G1SOURCE}.prime8)
    fi
    [[ "$srcprime8" == "════════" ]] && rm ~/.zen/tmp/coucou/${G1SOURCE}.prime8

    ### G1DEST NOT AN EMAIL
    if [[ ! "${G1DEST}" =~ ^[a-zA-Z0-9.%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,4}$ ]]; then
        echo "# checking DESTINATION primal transaction : ${G1DEST} "
        if [[ ! -s  ~/.zen/tmp/coucou/${G1DEST}.prime8 ]]; then
            dstprime=$(silkaj money history ${G1DEST} | tail -n 3 | head -n 1)
            dstprime8=$(echo $dstprime | awk -F'│' '{gsub(/[[:space:]]*/, "", $3); split($3, a, ":"); print substr(a[1], 1, 8)}')
            echo "dstprime = $dstprime"
            echo "dstprime8 = $dstprime8"
            echo "$dstprime8" > ~/.zen/tmp/coucou/${G1DEST}.prime8
        else
            dstprime8=$(cat ~/.zen/tmp/coucou/${G1DEST}.prime8)
        fi
        [[ "$dstprime8" == "════════" ]] && rm ~/.zen/tmp/coucou/${G1DEST}.prime8

        if [[ "$srcprime8" == "$dstprime8" && "$srcprime8" != "" ]]; then
            ## SAME UPLANET ZENCARD : TX AUTHORIZED
            ZEN2G1=$(echo "scale=1; $ZEN / 10" | bc)
            ~/.zen/Astroport.ONE/tools/PAYforSURE.sh "${MY_PATH}/tmp/${G1SOURCE}.zencard.dunikey" "$ZEN2G1" "${G1DEST}" "UPLANET$srcprime8:ZENCARD TX"
            echo "¸¸♬·¯·♩¸¸♪·¯·♫¸¸ $ZEN ¸¸♬·¯·♩¸¸♪·¯·♫¸¸ sent on UPLANET$srcprime8"
            cat ${MY_PATH}/templates/message.html \
            | sed -e "s~_TITLE_~$(date -u) <br> ${G1SOURCE}<br>>>> ${G1DEST}~g" \
                 -e "s~_MESSAGE_~♪·¯·♫¸ $ZEN ẐEN ¸♬¸¸♪<br>UPLANET$srcprime8~g" \
                > ${MY_PATH}/tmp/${MOATS}.out.html
            echo "${MY_PATH}/tmp/${MOATS}.out.html"
            rm ${MY_PATH}/tmp/${G1SOURCE}.zencard.dunikey ## REMOVE DECODED ZENCARD
            exit 0
        else
            ## NOT FROM SAME UPLANET
            cat ${MY_PATH}/templates/message.html \
            | sed -e "s~_TITLE_~$(date -u) <br> ${G1SOURCE}~g" \
                 -e "s~_MESSAGE_~ ／人 ◕‿‿◕ 人＼ <br> NOT COMPATIBLE <br>$srcprime8 != $dstprime8~g" \
                > ${MY_PATH}/tmp/${MOATS}.out.html
            echo "${MY_PATH}/tmp/${MOATS}.out.html"
            rm ${MY_PATH}/tmp/${G1SOURCE}.zencard.dunikey ## REMOVE DECODED ZENCARD
            exit 0
        fi
    else
        ### G1PalPay mode : if G1DEST is an EMAIL, then search for it on UPlanet
        echo "${G1DEST} searching on UPlanet"
        $(~/.zen/Astroport.ONE/tools/search_for_this_email_in_players.sh "${G1DEST}" | tail -n 1)
        if [[ -z ${ASTROG1} ]]; then
            echo "${G1DEST} NOT FOUND"
            cat ${MY_PATH}/templates/message.html \
            | sed -e "s~_TITLE_~$(date -u) <br> ${G1SOURCE}~g" \
                 -e "s~_MESSAGE_~d[ (☓‿‿☓) ]b<br>${G1DEST} NOT FOUND~g" \
                > ${MY_PATH}/tmp/${MOATS}.out.html
            echo "${MY_PATH}/tmp/${MOATS}.out.html"
            exit 0
        else
            ZEN2G1=$(echo "scale=1; $ZEN / 10" | bc)
            ~/.zen/Astroport.ONE/tools/PAYforSURE.sh "${MY_PATH}/tmp/${G1SOURCE}.zencard.dunikey" "$ZEN2G1" "${ASTROG1}" "UPLANET$srcprime8:ZENCARD TX"

            echo "¸¸♬·¯·♩¸¸♪·¯·♫¸¸ $ZEN ¸¸♬·¯·♩¸¸♪·¯·♫¸¸ sent on PALPAY"
            cat ${MY_PATH}/templates/message.html \
            | sed -e "s~_TITLE_~$(date -u) <br> ${G1SOURCE}<br>>>> ${G1DEST}~g" \
                 -e "s~_MESSAGE_~♪·¯·♫¸ $ZEN ẐEN ¸♬¸¸♪<br> to ${G1DEST}~g" \
                > ${MY_PATH}/tmp/${MOATS}.out.html
            echo "${MY_PATH}/tmp/${MOATS}.out.html"
            rm ${MY_PATH}/tmp/${G1SOURCE}.zencard.dunikey ## REMOVE DECODED ZENCARD
            exit 0
        fi
    fi

else
    # If sender is CAPTAIN, try paying with CAPTAIN wallet using nostr_PAY.sh
    CAPTAIN_HEX_FILE="$HOME/.zen/game/nostr/${CAPTAINEMAIL}/HEX"
    if [[ -n "$CAPTAINEMAIL" && -s "$CAPTAIN_HEX_FILE" ]]; then
        CAPTAIN_HEX=$(cat "$CAPTAIN_HEX_FILE" | tr -d '\n' )
        if [[ "$CAPTAIN_HEX" == "$SENDER_HEX" ]]; then
            KEYFILE="$HOME/.zen/game/nostr/${CAPTAINEMAIL}/.secret.dunikey"
            if [[ ! -s "$KEYFILE" ]]; then
                cat ${MY_PATH}/templates/message.html \
                | sed -e "s~_TITLE_~$(date -u) <br> ${CAPTAINEMAIL}~g" \
                     -e "s~_MESSAGE_~Captain key not found to sign payment.~g" \
                    > ${MY_PATH}/tmp/${MOATS}.out.html
                echo "${MY_PATH}/tmp/${MOATS}.out.html"
                exit 0
            fi
            ZEN2G1=$(echo "scale=1; $ZEN / 10" | bc)
            ~/.zen/Astroport.ONE/tools/nostr_PAY.sh "$KEYFILE" "$ZEN2G1" "$G1DEST" "UPLANET:COINFLIP $ZEN WIN"
            if [[ $? -eq 0 ]]; then
                cat ${MY_PATH}/templates/message.html \
                | sed -e "s~_TITLE_~$(date -u) <br> ${CAPTAINEMAIL} >>> ${G1DEST}~g" \
                     -e "s~_MESSAGE_~COINFLIP PAYOUT: $ZEN ẐEN~g" \
                    > ${MY_PATH}/tmp/${MOATS}.out.html
                echo "${MY_PATH}/tmp/${MOATS}.out.html"
                exit 0
            else
                cat ${MY_PATH}/templates/message.html \
                | sed -e "s~_TITLE_~$(date -u) <br> ${CAPTAINEMAIL}~g" \
                     -e "s~_MESSAGE_~Payment failed.~g" \
                    > ${MY_PATH}/tmp/${MOATS}.out.html
                echo "${MY_PATH}/tmp/${MOATS}.out.html"
                exit 0
            fi
        fi
    fi

    echo "SOURCE ZENCARD NOT FOUND"
    cat ${MY_PATH}/templates/message.html \
    | sed -e "s~_TITLE_~$(date -u) <br> ${G1SOURCE}~g" \
         -e "s~_MESSAGE_~d[ (☓‿‿☓) ]b<br>ZENCARD NOT FOUND~g" \
        > ${MY_PATH}/tmp/${MOATS}.out.html
    echo "${MY_PATH}/tmp/${MOATS}.out.html"
    exit 0
fi
