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
        echo "{\"success\": false, \"error\": \"Nostr authentication missing. Please login again.\", \"type\": \"auth_missing\"}"
        exit 0
    fi

    NOW_TS=$(date +%s)
    AUTH_TS=$(cat "$AUTH_MARKER" 2>/dev/null)
    # Accept auth within last 24 hours
    if [[ -n "$AUTH_TS" ]]; then
        DIFF=$(( NOW_TS - AUTH_TS ))
        if [[ $DIFF -gt 86400 ]]; then
            echo "{\"success\": false, \"error\": \"Nostr authentication expired. Please re-authenticate.\", \"type\": \"auth_expired\"}"
            exit 0
        fi
    fi

    # 2) Resolve expected G1 pubkey from Nostr hex (sender wallet mapping)
    EXPECTED_G1=$(~/.zen/Astroport.ONE/tools/search_for_this_hex_in_uplanet.sh "$SENDER_HEX" 2>/dev/null)
    if [[ -z "$EXPECTED_G1" ]]; then
        echo "{\"success\": false, \"error\": \"No G1 wallet found for this Nostr key on UPlanet.\", \"type\": \"auth_error\"}"
        exit 0
    fi

    # 3) If no source provided, adopt resolved wallet; otherwise ensure match
    if [[ -z "$G1SOURCE" ]]; then
        echo "# No G1SOURCE provided. Adopting resolved wallet from Nostr mapping"
        G1SOURCE="$EXPECTED_G1"
    elif [[ "$EXPECTED_G1" != "$G1SOURCE" ]]; then
        echo "{\"success\": false, \"error\": \"Source wallet mismatch. Expected: ${EXPECTED_G1:0:12}..., Provided: ${G1SOURCE:0:12}...\", \"type\": \"wallet_mismatch\"}"
        exit 0
    fi
fi

# Resolve destination shortcuts
if [[ "$G1DEST" == "CAPTAIN" ]]; then
    # Convert CAPTAIN to actual G1 pubkey from CAPTAINEMAIL
    if [[ -z "$CAPTAINEMAIL" ]]; then
        echo "{\"success\": false, \"error\": \"CAPTAINEMAIL is not configured on server.\", \"type\": \"config_error\"}"
        exit 0
    fi
    if [[ ! -s "$HOME/.zen/game/players/${CAPTAINEMAIL}/.g1pub" ]]; then
        echo "{\"success\": false, \"error\": \"Captain ZEN Card g1pub not found for ${CAPTAINEMAIL}.\", \"type\": \"captain_error\"}"
        exit 0
    fi
    G1DEST=$(cat "$HOME/.zen/game/players/${CAPTAINEMAIL}/.g1pub" | tr -d '\n' )
    echo "# Resolved CAPTAIN destination: ${G1DEST:0:12}..."
elif [[ "$G1DEST" == "PLAYER" ]]; then
    # Convert PLAYER to actual G1 pubkey from player identifier
            if [[ -z "$PLAYER_ID" ]]; then
            echo "{\"success\": false, \"error\": \"PLAYER destination requires player_id (email or hex).\", \"type\": \"missing_player_id\"}"
            exit 0
        fi
    # If email, use search_for_this_email_in_players.sh
    if [[ "$PLAYER_ID" == *"@"* ]]; then
        echo "# Resolving PLAYER by email: $PLAYER_ID"
        $(~/.zen/Astroport.ONE/tools/search_for_this_email_in_players.sh "$PLAYER_ID" | tail -n 1)
        if [[ -z ${ASTROG1} ]]; then
            echo "{\"success\": false, \"error\": \"PLAYER not found by email: ${PLAYER_ID}.\", \"type\": \"player_not_found\"}"
            exit 0
        fi
        G1DEST="$ASTROG1"
        echo "# Resolved PLAYER G1 by email: ${G1DEST:0:12}..."
    else
        # Assume hex; resolve to G1 using search_for_this_hex_in_uplanet.sh
        echo "# Resolving PLAYER by hex: ${PLAYER_ID:0:12}..."
        PLAYER_G1=$(~/.zen/Astroport.ONE/tools/search_for_this_hex_in_uplanet.sh "$PLAYER_ID" 2>/dev/null)
        if [[ -z "$PLAYER_G1" ]]; then
            echo "{\"success\": false, \"error\": \"PLAYER not found by hex: ${PLAYER_ID:0:12}...\", \"type\": \"player_not_found\"}"
            exit 0
        fi
        G1DEST="$PLAYER_G1"
        echo "# Resolved PLAYER G1 by hex: ${G1DEST:0:12}..."
    fi
fi


# If sender is CAPTAIN, try paying with CAPTAIN wallet using nostr_PAY.sh
CAPTAIN_HEX_FILE="$HOME/.zen/game/nostr/${CAPTAINEMAIL}/HEX"
if [[ -n "$CAPTAINEMAIL" && -s "$CAPTAIN_HEX_FILE" ]]; then
    CAPTAIN_HEX=$(cat "$CAPTAIN_HEX_FILE" | tr -d '\n' )
    if [[ "$CAPTAIN_HEX" == "$SENDER_HEX" ]]; then
        KEYFILE="$HOME/.zen/game/nostr/${CAPTAINEMAIL}/.secret.dunikey"
        if [[ ! -s "$KEYFILE" ]]; then
            echo "{\"success\": false, \"error\": \"Captain key not found to sign payment.\", \"type\": \"captain_key_missing\"}"
            exit 0
        fi
        ZEN2G1=$(echo "scale=1; $ZEN / 10" | bc)
        ~/.zen/Astroport.ONE/tools/nostr_PAY.sh "$KEYFILE" "$ZEN2G1" "$G1DEST" "UPLANET:COINFLIP $ZEN WIN"
        if [[ $? -eq 0 ]]; then
            echo "{\"success\": true, \"message\": \"COINFLIP PAYOUT: $ZEN ẐEN\", \"type\": \"coinflip_payout\", \"zen_amount\": $ZEN, \"captain\": \"${CAPTAINEMAIL}\", \"destination\": \"${G1DEST}\"}"
            exit 0
        else
            echo "{\"success\": false, \"error\": \"Payment failed.\", \"type\": \"payment_failed\"}"
            exit 0
        fi
    fi
fi

echo "SOURCE ZENCARD NOT FOUND"
echo "{\"success\": false, \"error\": \"ZENCARD NOT FOUND\", \"type\": \"zencard_missing\"}"
exit 0
