#!/bin/bash
##################################################################### zen_send.sh
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
export PATH=$HOME/.astro/bin:$HOME/.local/bin:$PATH

source ${MY_PATH}/.env

# Vérifier le nombre d'arguments
if [ "$#" -ne 3 ]; then
    echo "Usage: $0 <zen> <g1source> <g1dest>"
    exit 1
fi

# Récupération des arguments
ZEN=$1
G1SOURCE=$2
G1DEST=$3


## CHECK FOR SOURCE DUNIKEY
if [[ -s ${MY_PATH}/tmp/${G1SOURCE}.zencard.dunikey ]]; then

    echo "# getting ${G1SOURCE} balance : "
    SRCCOINS=$(~/.zen/Astroport.ONE/tools/COINScheck.sh ${G1SOURCE} | tail -n 1)
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
            ~/.zen/Astroport.ONE/tools/PAY4SURE.sh "${MY_PATH}/tmp/${G1SOURCE}.zencard.dunikey" "$ZEN2G1" "${G1DEST}" "UPLANET$srcprime8:ZENCARD TX"
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
            ~/.zen/Astroport.ONE/tools/PAY4SURE.sh "${MY_PATH}/tmp/${G1SOURCE}.zencard.dunikey" "$ZEN2G1" "${ASTROG1}" "UPLANET$srcprime8:ZENCARD TX"

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
    echo "SOURCE ZENCARD NOT FOUND"
    cat ${MY_PATH}/templates/message.html \
    | sed -e "s~_TITLE_~$(date -u) <br> ${G1SOURCE}~g" \
         -e "s~_MESSAGE_~d[ (☓‿‿☓) ]b<br>ZENCARD NOT FOUND~g" \
        > ${MY_PATH}/tmp/${MOATS}.out.html
    echo "${MY_PATH}/tmp/${MOATS}.out.html"
    exit 0
fi
