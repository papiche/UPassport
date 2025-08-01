#!/bin/bash
################################################################### upassport.sh
# Author: Fred (DsEx1pS33vzYZg4MroyBV9hCw98j1gtHEhwiZ5tK7ech)
# Version: 1.0
# License: AGPL-3.0 (https://choosealicense.com/licenses/agpl-3.0/)
################################################################################
echo "Usage: $0 <qrcode> (<image_path or pass>)"
MOATS=$(date -u +"%Y%m%d%H%M%S%4N")
MY_PATH="`dirname \"$0\"`"              # relative
MY_PATH="`( cd \"$MY_PATH\" && pwd )`"  # absolutized and normalized
ME="${0##*/}"
################################################################### INIT
export PATH=$HOME/.astro/bin:$HOME/.local/bin:$PATH
## LOAD ASTROPORT ENVIRONMENT
[ ! -s $HOME/.zen/Astroport.ONE/tools/my.sh ] \
    && echo "<h1>ERROR/ Missing Astroport.ONE. Please install...<h1>" \
    && exit 1

. "$HOME/.zen/Astroport.ONE/tools/my.sh"

###########\,,/(^_^)\,,/################# https://1lineart.kulaone.com/
source ${MY_PATH}/.env
[[ -z $myDUNITER ]] && myDUNITER="https://g1.cgeek.fr" # DUNITER
[[ -z $myCESIUM ]] && myCESIUM="https://g1.data.e-is.pro" # CESIUM+
[[ -z $myIPFS ]] && myIPFS="http://127.0.0.1:8080" # IPFS

function urldecode() { : "${*//+/ }"; echo -e "${_//%/\\x}"; }

## PUBKEY SHOULD BE A MEMBER PUBLIC KEY
QRCODE="$1"
IMAGE="$2"
LAT="$3"
LON="$4"

ZLAT=$(makecoord $LAT)
ZLON=$(makecoord $LON)

## IMAGE CAN BE A FILE or 4 digit PASS
[ ! -z "$IMAGE" ] && echo "IMAGE : $IMAGE"

PUBKEY=$(echo "$QRCODE" | tr -d ' ') ## REMOVE SPACE
ZCHK="$(echo $PUBKEY | cut -d ':' -f 2-)" # :G1CHK or :ZEN or :NOSTRNS
[[ $ZCHK == $PUBKEY ]] && ZCHK=""
PUBKEY="$(echo $PUBKEY | cut -d ':' -f 1)" # ":" split
echo "PUBKEY ? $PUBKEY ($ZCHK)"
if [ -n "$PUBKEY" ]; then
    PUBKEY="${PUBKEY:0:256}" ## cut
else
    echo "PUBKEY est vide. DROP."
    exit 0
fi

#### AVOID MULITPLE RUN
countMErunning=$(pgrep -au $USER -f "$ME" | wc -l)
if [[ $countMErunning -gt 2 ]]; then
    echo "$ME already running $countMErunning time"
    cat ${MY_PATH}/templates/message.html \
    | sed -e "s~_TITLE_~$(date -u) <br> ${PUBKEY}~g" \
         -e "s~_MESSAGE_~$countMErunning x d[ o_0 ]b ... please wait ~g" \
        > ${MY_PATH}/tmp/${MOATS}.out.html
    echo "${MY_PATH}/tmp/${MOATS}.out.html"
    exit 0
fi


############ ZENCARD QRCODE !!!!
if [[ ${QRCODE:0:5} == "~~~~~" ]]; then
    ## Recreate GPG aes file
    urldecode "${QRCODE}" | tr '_' '+' | tr '-' '\n' | tr '~' '-'  > ${MY_PATH}/tmp/${MOATS}.disco.aes
    sed -i '$ d' ${MY_PATH}/tmp/${MOATS}.disco.aes
    # Decoding
    echo "cat ~/.zen/tmp/${MOATS}/disco.aes | gpg -d --passphrase "${IMAGE}" --batch"
    cat ${MY_PATH}/tmp/${MOATS}.disco.aes | gpg -d --passphrase "${IMAGE}" --batch > ${MY_PATH}/tmp/${MOATS}.decoded

    [[ -s ${MY_PATH}/tmp/${MOATS}.decoded ]] \
        && DISCO=$(cat ${MY_PATH}/tmp/${MOATS}.decoded | cut -d '?' -f2)

    if [[ ${DISCO} == "" ]]; then ## BAD PASS ...
        cat ${MY_PATH}/templates/message.html \
        | sed -e "s~_TITLE_~$(date -u) <br> BAD ${IMAGE}~g" \
             -e "s~_MESSAGE_~@( * O * )@~g" \
            > ${MY_PATH}/tmp/${MOATS}.out.html
        echo "${MY_PATH}/tmp/${MOATS}.out.html"
        exit 0
    fi
    ## GOOD PASS DISCO : "/?salt=${USALT}&pepper=${UPEPPER}"
    arr=(${DISCO//[=&]/ })
    s=$(urldecode ${arr[0]} | xargs)
    salt=$(urldecode ${arr[1]} | xargs)
    p=$(urldecode ${arr[2]} | xargs)
    pepper=$(urldecode ${arr[3]} | xargs)
    ## CREATE WALLET KEY
    TWNS=$($HOME/.zen/Astroport.ONE/tools/keygen -t ipfs "${salt}" "${pepper}")
    $HOME/.zen/Astroport.ONE/tools/keygen -t duniter -o ${MY_PATH}/tmp/${IMAGE}.zencard.dunikey "${salt}" "${pepper}"
    g1source=$(cat ${MY_PATH}/tmp/${IMAGE}.zencard.dunikey  | grep 'pub:' | cut -d ' ' -f 2)
    SRCCOINS=$(~/.zen/Astroport.ONE/tools/COINScheck.sh ${g1source} | tail -n 1)
    SRCZEN=$(echo "($SRCCOINS - 1) * 10" | bc | cut -d '.' -f 1)
    ### REVEAL DUNIKEY KEY
    mv ${MY_PATH}/tmp/${IMAGE}.zencard.dunikey ${MY_PATH}/tmp/${g1source}.zencard.dunikey
    ### TODO !!! ACTIVATE IPNS KEY ??
    ############################################
    ## REDIRECT TO ZENCARD DESTINATION SCANNER
    cat ${MY_PATH}/templates/scan_zen.html \
        | sed -e "s~_G1SOURCE_~${g1source}~g" \
        -e "s~_ZEN_~$SRCZEN~g" \
        -e "s~_TW_~<a href=${myIPFS}/ipns/${TWNS} target=_new>TW</a>~g" \
        -e "s~https://ipfs.copylaradio.com~${myIPFS}~g" \
    > ${MY_PATH}/tmp/${MOATS}.out.html
    echo "${MY_PATH}/tmp/${MOATS}.out.html"
    exit 0

fi

## IS IT k51qzi5uqu5d STYLE IPNS KEY (like a TW on MULTIPASS)
ipnsk51=$(echo "$PUBKEY" | grep -oP "(?<=k51qzi5uqu5d)[^/]*")
if [[ ${ipnsk51} != "" ]]; then
    TWNS="k51qzi5uqu5d"$ipnsk51

    echo '<!DOCTYPE html><html><head>
    <meta http-equiv="refresh" content="0; url='${myIPFS}/ipns/${TWNS}'">
    </head><body></body></html>' > ${MY_PATH}/tmp/${TWNS}.out.html
    echo "${MY_PATH}/tmp/${TWNS}.out.html"
    exit 0
fi


## IS IT http(s) link
if [[ ${PUBKEY:0:4} == "http" ]]; then
    echo "This is HTTP link : $QRCODE"
    ## SEARCH FOR IPNS KEY (12D3Koo ZEROCARD DISKDRIVE style)
    ipns12D=$(echo "$QRCODE" | grep -oP "(?<=12D3Koo)[^/]*")
    if [ -z $ipns12D ]; then
        ## ANY HTTP LINK
        echo '<!DOCTYPE html><html><head>
            <meta http-equiv="refresh" content="0; url='${QRCODE}'">
            </head><body></body></html>' > ${MY_PATH}/tmp/${MOATS}.out.html
        echo "${MY_PATH}/tmp/${MOATS}.out.html"
        exit 0
    else
        ## ZEROCARD IPNS LINK DETECTED
        CARDNS="12D3Koo"$ipns12D
        CARDG1=$($HOME/.zen/Astroport.ONE/tools/ipfs_to_g1.py $CARDNS)
        echo "ZEROCARD IPNS12D QRCODE : /ipns/$CARDNS ($CARDG1)"
        # FIND MEMBER & ZEROCARD
        MEMBERPUB=$(grep -h -r -l --dereference "$CARDNS" ${MY_PATH}/pdf/ | grep IPNS12D | cut -d '/' -f 3)
        [ -z $MEMBERPUB ] && echo '<!DOCTYPE html><html><head>
                        </head><body><h1>ERROR --- ZEROCARD NOT FOUND ---<h1>
                        UPassport is not registered on this Astroport. Contact support@qo-op.com
                        </body></html>' > ${MY_PATH}/tmp/${MOATS}.out.html \
                                && echo "${MY_PATH}/tmp/${MOATS}.out.html" \
                                    &&  exit 0
        ZEROCARD=$(cat ${MY_PATH}/pdf/${MEMBERPUB}/ZEROCARD)
        ############################################
        ## REDIRECT TO SSSS SECURITY QR SCANNER
        cat ${MY_PATH}/templates/scan_ssss.html \
            | sed -e "s~_CARDNS_~${CARDNS}~g" \
            -e "s~_ZEROCARD_~${ZEROCARD}~g" \
            -e "s~https://ipfs.copylaradio.com~${myIPFS}~g" \
        > ${MY_PATH}/tmp/${CARDNS}.out.html
        echo "${MY_PATH}/tmp/${CARDNS}.out.html"
        exit 0
    fi
fi

####################################################################
# CHECK IF IT IS AN EMAIL = NOSTR CARD
########################################################################
if [[ $PUBKEY =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; then
    EMAIL="${QRCODE,,}"
    echo "Email detected: $EMAIL"

    ## SEARCH FOR EXISTING MULTIPASS
    if [[ -n $($HOME/.zen/Astroport.ONE/tools/search_for_this_email_in_nostr.sh ${EMAIL}) ]]; then
        # Check if not TODATE made account - but only if not trying to delete
        if [[ "$IMAGE" != "0000" ]]; then
            if [[ -f "${HOME}/.zen/game/nostr/${EMAIL}/TODATE" ]]; then
                BIRTHDAY=$(cat ${HOME}/.zen/game/nostr/${EMAIL}/TODATE)
                if [[ $BIRTHDAY != $TODATE ]]; then
                    cat ${MY_PATH}/templates/message.html \
                    | sed -e "s~_TITLE_~$(date -u) <br> ${EMAIL}~g" \
                        -e "s~_MESSAGE_~EXISTING MULTIPASS~g" \
                        > ${MY_PATH}/tmp/${MOATS}.out.html
                    echo "${MY_PATH}/tmp/${MOATS}.out.html"
                    exit 0
                fi
            fi
        fi
        ## export NOSTR param
        $($HOME/.zen/Astroport.ONE/tools/search_for_this_email_in_nostr.sh ${EMAIL} | tail -n 1) # export NOSTR param
    fi
    ### SEARCH FOR EXISTING NOSTR CARD
    if [[ ! -s ${HOME}/.zen/game/nostr/${EMAIL}/.nostr.zine.html ]]; then
        #########################################################""
        ### NEW USER : CREATING NOSTR CARD MULTIPASS ZINE
        #########################################################""
        ${HOME}/.zen/Astroport.ONE/tools/make_NOSTRCARD.sh "${EMAIL}" "$IMAGE" "${ZLAT}" "${ZLON}"
        ## MAILJET SEND NOSTR CARD
        YOUSER=$(${HOME}/.zen/Astroport.ONE/tools/clyuseryomail.sh ${EMAIL})
        ${HOME}/.zen/Astroport.ONE/tools/mailjet.sh "${EMAIL}" "${HOME}/.zen/game/nostr/${EMAIL}/.nostr.zine.html" "UPlanet MULTIPASS"
        echo "${HOME}/.zen/game/nostr/${EMAIL}/.nostr.zine.html"
        exit 0
        #########################################################""
        #########################################################""
    else
        ## DID SEND "0000" FOR DELETION ?
        if [[ "$IMAGE" == "0000" ]]; then
            echo "## 0000 => CHECKING FOR DELETION ${EMAIL}"
            ## Check if account was created today (TODATE matches current date)
            if [[ -f "${HOME}/.zen/game/nostr/${EMAIL}/TODATE" ]] && [[ "$(cat ${HOME}/.zen/game/nostr/${EMAIL}/TODATE)" == "$TODATE" ]]; then
                echo "## 0000 => DELETING ACCOUNT ${EMAIL} (created today)"
                sed -i "s~Print~DELETED~g" ${HOME}/.zen/game/nostr/${EMAIL}/.nostr.zine.html
                sed -i "s~${PUBKEY}~DELETED~g" ${HOME}/.zen/game/nostr/${EMAIL}/.nostr.zine.html
                cp ${HOME}/.zen/game/nostr/${EMAIL}/.nostr.zine.html ~/.zen/tmp/${EMAIL}.nostr.zine.html
                ${HOME}/.zen/Astroport.ONE/tools/nostr_DESTROY_TW.sh "${EMAIL}"
                echo "${HOME}/.zen/tmp/${EMAIL}.nostr.zine.html"
                exit 0
            else
                echo "## 0000 => ACCOUNT NOT CREATED TODAY, CANNOT DELETE"
                cat ${MY_PATH}/templates/message.html \
                | sed -e "s~_TITLE_~$(date -u) <br> ${EMAIL}~g" \
                     -e "s~_MESSAGE_~ACCOUNT NOT CREATED TODAY<br>CANNOT DELETE~g" \
                    > ${MY_PATH}/tmp/${MOATS}.out.html
                echo "${MY_PATH}/tmp/${MOATS}.out.html"
                exit 0
            fi
        fi
        ## SHOW AGAIN ON 1ST DAY
        if [[ -f "${HOME}/.zen/game/nostr/${EMAIL}/TODATE" ]] && [[ "$(cat ${HOME}/.zen/game/nostr/${EMAIL}/TODATE)" == "$TODATE" ]]; then
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

fi

get_NOSTRNS_directory() {
    local pubkey="$1"
    local key_file
    local found_dir=""

    while IFS= read -r -d $'\0' key_file; do
        if [[ "/ipns/$pubkey" == "$(cat "$key_file")" ]]; then
            # Extraire le dernier répertoire du chemin
            KNAME=$(basename "$(dirname "$key_file")")
            echo $KNAME
            return 0
        fi
    done < <(find "$HOME/.zen/game/nostr" -type f -name "NOSTRNS" -print0)
}
########################################################################
############ MULTIPASS SSSS 1-xxx:ipns KEY QRCODE RECEIVED
########################################################################
if [[ ${PUBKEY:0:2} == "1-" && ${ZCHK:0:6} == "k51qzi" ]]; then
    echo "NOSTR CARD SSSS KEY verification......"
    SSSS1=$(echo ${QRCODE} | cut -d ':' -f 1)
    IPNSVAULT=${ZCHK}
    ipnsk51=$(echo "$IPNSVAULT" | grep -oP "(?<=k51qzi5uqu5d)[^/]*")

    if [[ ${ipnsk51} != "" ]]; then
        VAULTNS="k51qzi5uqu5d"$ipnsk51
        ## SEARCHING FOR LOCAL NOSTR CARD : todo extend search to all swarm => allow MULTIPASS on all Astroport
        PLAYER=$(get_NOSTRNS_directory ${VAULTNS})
        #################################################### NOT FOUND #
        if [[ -z $PLAYER ]]; then
            cat ${MY_PATH}/templates/message.html \
            | sed -e "s~_TITLE_~$(date -u) <br> ${IPNSVAULT}~g" \
                 -e "s~_MESSAGE_~NOSTR CARD MISSING~g" \
                > ${MY_PATH}/tmp/${MOATS}.out.html
            echo "${MY_PATH}/tmp/${MOATS}.out.html"
            exit 0
        fi
        mkdir -p $HOME/.zen/tmp/$MOATS/$IPNSVAULT/$PLAYER
        ## DISCO DECODING
        ######################################################## FOUND ##
        ########################## DISCO 1-DECRYPTION
        tmp_player=$(mktemp)
        echo "$SSSS1" > "$tmp_player"

        # Decrypt the tail part using UPLANET key
        tmp_tail=$(mktemp)
        $HOME/.zen/Astroport.ONE/tools/keygen -t duniter -o $HOME/.zen/tmp/$MOATS/uplanet.dunikey "${UPLANETNAME}" "${UPLANETNAME}"
        $HOME/.zen/Astroport.ONE/tools/natools.py decrypt -f pubsec -i "$HOME/.zen/game/nostr/${PLAYER}/ssss.tail.uplanet.enc" \
                -k $HOME/.zen/tmp/$MOATS/uplanet.dunikey -o "$tmp_tail"

        rm $HOME/.zen/tmp/$MOATS/uplanet.dunikey

        # Combine "$tmp_player" "$tmp_tail"  decrypted shares ## STRANGE 1, 2 OUTPUT
        # WARNING: couldn't get memory lock (ENOMEM, try to adjust RLIMIT_MEMLOCK!).*
        DISCO=$(cat "$tmp_player" "$tmp_tail" | ssss-combine -t 2 -q 2>&1 | tail -n 1)
        #~ echo "DISCO = $DISCO"
        IFS='=&' read -r s salt p pepper <<< "$DISCO"
        if [[ -n $salt && -n $pepper ]]; then
            rm "$tmp_player" "$tmp_tail"
        else
            echo "ERROR : BAD DISCO DECODING"
            cat ${MY_PATH}/templates/message.html \
            | sed -e "s~_TITLE_~$(date -u) <br> ${IPNSVAULT}~g" \
                 -e "s~_MESSAGE_~DISCO DECODING ERROR~g" \
                > ${MY_PATH}/tmp/${MOATS}.out.html
            echo "${MY_PATH}/tmp/${MOATS}.out.html"
            exit 0
        fi
        ##################################################### DISCO DECODED
        ### CASH BACK => EMPTY WALLET CLOSE ACCOUNT
        if [[ "$IMAGE" == "0000" ]]; then
            ## DUNIKEY PRIVATE KEY for CASH BACK
            $HOME/.zen/Astroport.ONE/tools/keygen -t duniter -o ~/.zen/tmp/$MOATS/$IPNSVAULT/nostr.dunikey "${salt}" "${pepper}"
            G1PUBNOSTR=$(cat ~/.zen/game/nostr/${PLAYER}/G1PUBNOSTR) ## NOSTR G1PUB READING
            G1PRIME=$(cat ~/.zen/tmp/coucou/$G1PUBNOSTR.primal) ## NOSTR G1PRIME READING
            AMOUNT=$(~/.zen/Astroport.ONE/tools/G1check.sh ${G1PUBNOSTR} | tail -n 1)
            echo "______ AMOUNT = ${AMOUNT} G1"
            ## EMPTY AMOUNT G1 to PRIMAL
            ~/.zen/Astroport.ONE/tools/PAYforSURE.sh "${HOME}/.zen/tmp/$MOATS/$IPNSVAULT/nostr.dunikey" "$AMOUNT" "${G1PRIME}" "NOSTR:EXIT"

            ## UPDATE TODATE - one day to reactivate...
            echo ${TODATE} > ${HOME}/.zen/game/nostr/${PLAYER}/TODATE 2>/dev/null

            cat ${MY_PATH}/templates/message.html \
            | sed -e "s~_TITLE_~$(date -u) <br> ${G1PRIME}~g" \
                  -e "s~_MESSAGE_~PRIMAL PAY BACK <br> $AMOUNT~g" \
                > ${MY_PATH}/tmp/${MOATS}.out.html
            echo "${MY_PATH}/tmp/${MOATS}.out.html"
            exit 0
        else
        ### OPEN UPLANET GEO KEY MESSAGE
            ### Filling UPassport API template with nsec
            NSEC=$($HOME/.zen/Astroport.ONE/tools/keygen -t nostr "${salt}" "${pepper}" -s)
            cat ~/.zen/UPassport/templates/nostr.html \
                | sed "s/const userNsec = '';/const userNsec = '${NSEC}';/" \
                > ${MY_PATH}/tmp/${MOATS}.out.html
            echo "${MY_PATH}/tmp/${MOATS}.out.html"
            exit 0
        fi
    else

        echo "IRREGULAR... ${QRCODE}"
        cat ${MY_PATH}/templates/message.html \
        | sed -e "s~_TITLE_~$(date -u) <br> ${QRCODE}~g" \
             -e "s~_MESSAGE_~IRREGULAR QRCODE<br>^(*(oo)*)^~g" \
            > ${MY_PATH}/tmp/${MOATS}.out.html
        echo "${MY_PATH}/tmp/${MOATS}.out.html"
        exit 0

    fi
fi

# CHECK G1 PUBKEY FORMAT
if [[ -z $($HOME/.zen/Astroport.ONE/tools/g1_to_ipfs.py ${PUBKEY} 2>/dev/null) ]]; then
    cat ${MY_PATH}/templates/message.html \
    | sed -e "s~_TITLE_~$(date -u) <br> ${PUBKEY}~g" \
         -e "s~_MESSAGE_~QR CODE Error<br><a target=_new href=https://pad.p2p.legal/HELP>HELP</a>?!~g" \
        > ${MY_PATH}/tmp/${MOATS}.out.html
    echo "${MY_PATH}/tmp/${MOATS}.out.html"
    exit 0
fi

TOT=0
########################################################################
### FUNCTIONS
########################################################################
########################################################################
# Function to get Cesium+ profile, generate QR code, and annotate with UID
generate_qr_with_uid() {
    local pubkey=$1
    local member_uid=$2
    ## Extract wallet balance
    if [[ ! -s ${MY_PATH}/tmp/${pubkey}.solde ]]; then
        solde=$($HOME/.zen/Astroport.ONE/tools/timeout.sh -t 5 $HOME/.zen/Astroport.ONE/tools/jaklis/jaklis.py balance -p ${pubkey})
        [ ! $? -eq 0 ] \
            && GVA=$(~/.zen/Astroport.ONE/tools/duniter_getnode.sh | tail -n 1) \
            && [[ ! -z $GVA ]] && sed -i '/^NODE=/d' $HOME/.zen/Astroport.ONE/tools/jaklis/.env \
            && echo "NODE=$GVA" >> $HOME/.zen/Astroport.ONE/tools/jaklis/.env \
            && echo "GVA RELAY: $GVA" ## GVA RELAY SWITCHING
        echo "$solde" > ${MY_PATH}/tmp/${pubkey}.solde
        sleep 2
    else
        ## EXTRACT CESIUM+ GEOLOCATION
        if [[ -s ${MY_PATH}/tmp/$pubkey.cesium.json ]];then
            zlat=$(cat ${MY_PATH}/tmp/${pubkey}.cesium.json | jq -r '._source.geoPoint.lat')
            ulat=$(makecoord $zlat)
            zlon=$(cat ${MY_PATH}/tmp/${pubkey}.cesium.json | jq -r '._source.geoPoint.lon')
            ulon=$(makecoord $zlon)
            echo "ulat=$ulat; ulon=$ulon" > ${MY_PATH}/tmp/${pubkey}.GPS
        fi
        solde=$(cat ${MY_PATH}/tmp/${pubkey}.solde)
    fi
    zen=$(echo "($solde - 1) * 10" | bc | cut -d '.' -f 1)
    TOT=$((TOT + zen))

    if [ ! -s ${MY_PATH}/tmp/${pubkey}.UID.png ]; then
        echo "___________ CESIUM+ ${member_uid} [${zen} ẑ] : ${pubkey} "
        [[ ! -s ${MY_PATH}/tmp/$pubkey.cesium.json ]] \
        && $HOME/.zen/Astroport.ONE/tools/timeout.sh -t 6 \
        curl -s ${myCESIUM}/user/profile/${pubkey} > ${MY_PATH}/tmp/${pubkey}.cesium.json 2>/dev/null

        if [ ! -s "${MY_PATH}/tmp/$pubkey.cesium.json" ]; then
            echo "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
            echo "xxxxx No profil found CESIUM+ ${myCESIUM} xxxxx"
            echo "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        else
            zlat=$(cat ${MY_PATH}/tmp/${pubkey}.cesium.json | jq -r '._source.geoPoint.lat')
            ulat=$(makecoord $zlat)
            zlon=$(cat ${MY_PATH}/tmp/${pubkey}.cesium.json | jq -r '._source.geoPoint.lon')
            ulon=$(makecoord $zlon)
            echo "ulat=$ulat; ulon=$ulon" > ${MY_PATH}/tmp/${pubkey}.GPS
            # Extract avatar.png from json
            cat ${MY_PATH}/tmp/${pubkey}.cesium.json | jq -r '._source.avatar._content' | base64 -d > ${MY_PATH}/tmp/${pubkey}.png

            # Resize avatar picure & add transparent canvas
            convert ${MY_PATH}/tmp/${pubkey}.png \
              -resize 120x120 \
              -bordercolor white \
              -border 120x120 \
              -background none \
              -transparent white \
              ${MY_PATH}/tmp/${pubkey}.small.png
        fi

        # Create QR Code with Cesium+ picture in
        [ -s ${MY_PATH}/tmp/${pubkey}.small.png ] \
            && amzqr "${pubkey}" -l H -p ${MY_PATH}/tmp/${pubkey}.small.png -c -n ${pubkey}.QR.png -d ${MY_PATH}/tmp/ 2>/dev/null
        [ ! -s ${MY_PATH}/tmp/${pubkey}.QR.png ] \
            && amzqr "${pubkey}" -l H -p ${MY_PATH}/static/img/g1ticket.png -n ${pubkey}.QR.png -d ${MY_PATH}/tmp/ \
            && cp -f ${MY_PATH}/static/img/g1ticket.png ${MY_PATH}/tmp/${pubkey}.png

        # Write UID at the bottom
        convert ${MY_PATH}/tmp/${pubkey}.QR.png \
          -gravity SouthWest \
          -pointsize 25 \
          -fill black \
          -annotate +2+2 "${zen} Z ${member_uid} ($ulat/$ulon)" \
          -annotate +3+1 "${zen} Z ${member_uid} ($ulat/$ulon)" \
          ${MY_PATH}/tmp/${pubkey}.UID.png

        [[ -s ${MY_PATH}/tmp/${pubkey}.UID.png ]] && rm ${MY_PATH}/tmp/${pubkey}.QR.png
        sleep 2
    fi
}
########################################################################
########################################################################

#### PUBKEY RECEIVED !!!
########################################################################
### RUN TIME ######
########################################################################
## MANAGING CACHE
mkdir -p ${MY_PATH}/tmp
mkdir -p ${MY_PATH}/pdf
# Delete older than 3 days files from ${MY_PATH}/tmp
find ${MY_PATH}/tmp -mtime +3 -type f -exec rm '{}' \;
# Detect older than 7 days "fac-simile" from ${MY_PATH}/pdf (not ls with game/passport)
find ${MY_PATH}/pdf -type d -mtime +7 -not -xtype l -exec rm -r {} \;

## GET PUBKEY TX HISTORY : working with jaklis (TODO use silkaj?)
echo "LOADING WALLET HISTORY"
$HOME/.zen/Astroport.ONE/tools/timeout.sh -t 6 $HOME/.zen/Astroport.ONE/tools/jaklis/jaklis.py history -n 25 -p ${PUBKEY} -j > ${MY_PATH}/tmp/$PUBKEY.TX.json
[ ! $? -eq 0 ] \
    && GVA=$(~/.zen/Astroport.ONE/tools/duniter_getnode.sh | tail -n 1) \
    && [[ ! -z $GVA ]] && sed -i '/^NODE=/d' $HOME/.zen/Astroport.ONE/tools/jaklis/.env \
    && echo "NODE=$GVA" >> $HOME/.zen/Astroport.ONE/tools/jaklis/.env \
    && $HOME/.zen/Astroport.ONE/tools/timeout.sh -t 6 $HOME/.zen/Astroport.ONE/tools/jaklis/jaklis.py history -n 25 -p ${PUBKEY} -j > ${MY_PATH}/tmp/$PUBKEY.TX.json
    ## TEST AND SWITCH GVA SERVER


## EXTRACT SOLDE & ZEN
if [[ -s ${MY_PATH}/tmp/$PUBKEY.TX.json ]]; then
    SOLDE=$($HOME/.zen/Astroport.ONE/tools/timeout.sh -t 20 $HOME/.zen/Astroport.ONE/tools/jaklis/jaklis.py balance -p ${PUBKEY})
    ZEN=$(echo "($SOLDE - 1) * 10" | bc | cut -d '.' -f 1)

    AMOUNT="$SOLDE Ğ1"
    [[ $SOLDE == "null" ]] && AMOUNT="EMPTY"
    [[ $SOLDE == "" ]] && AMOUNT="TIMEOUT"
    [[ $ZCHK == "ZEN" || $ZCHK == "" ]] && AMOUNT="$ZEN ẑ€N"
else
    cat ${MY_PATH}/templates/message.html \
    | sed -e "s~_TITLE_~$(date -u) <br> ${PUBKEY}~g" \
         -e "s~#000~#F00~g" \
         -e "s~_MESSAGE_~PRIMAL EXTRACT ERROR<br>╭∩╮ (òÓ,) ╭∩╮~g" \
        > ${MY_PATH}/tmp/${MOATS}.out.html
    echo "${MY_PATH}/tmp/${MOATS}.out.html"
    exit 0
fi
echo "$AMOUNT G1 ($ZCHK) $ZEN ẑ€N"
echo "--------------------------------------"
echo
##################################### SCAN N°
## CHECK LAST TX IF ZEROCARD EXISTING
if [[ -s ${MY_PATH}/pdf/${PUBKEY}/ZEROCARD ]]; then
    ZEROCARD=$(cat ${MY_PATH}/pdf/${PUBKEY}/ZEROCARD)
    echo "G1 ZEROCARD FOUND: ${ZEROCARD}"
    ## CHECK IF MEMBER SENT TX TO ZEROCARD
    jq '.[-1]' ${MY_PATH}/tmp/$PUBKEY.TX.json
    LASTX=$(jq '.[-1] | .amount' ${MY_PATH}/tmp/$PUBKEY.TX.json)
    DEST=$(jq -r '.[-1] | .pubkey' ${MY_PATH}/tmp/$PUBKEY.TX.json)
    COMM=$(jq -r '.[-1] | .comment' ${MY_PATH}/tmp/$PUBKEY.TX.json)
    TXDATE=$(jq -r '.[-1] | .date' ${MY_PATH}/tmp/$PUBKEY.TX.json)

    ## which ASTROPORT is UPASSPORT ambassy
    [[ -s ${MY_PATH}/pdf/${PUBKEY}/ASTROPORT ]] \
        && echo "ASTROPORT Web3 Ambassy : $(cat ${MY_PATH}/pdf/${PUBKEY}/ASTROPORT)" \
        || echo $IPFSNODEID > ${MY_PATH}/pdf/${PUBKEY}/ASTROPORT

    ##################################### 3RD SCAN
    if [ -L "${MY_PATH}/pdf/${PUBKEY}" ]; then
        ############################ TRANSMIT TX COMMENT AS COMMAND TO ZEROCARD
        if [[ $COMM != "" && "$DEST" == "$ZEROCARD" ]]; then
            source ${MY_PATH}/pdf/${PUBKEY}/GPS
            ${MY_PATH}/u.command.sh "$PUBKEY" "$COMM" "$LASTX" "$TXDATE" "$ZEROCARD" "$ulan" "$ulon"
            [ ! $? -eq 0 ] && echo ">>>>>>>>>>>> ERROR"
        fi
        ##################################### 4TH SCAN : DRIVESTATE REDIRECT
        if [[ -s ${MY_PATH}/pdf/${PUBKEY}/DRIVESTATE ]]; then
            ## REDIRECT TO CURRENT DRIVESTATE
            echo '<!DOCTYPE html><html><head>
            <meta http-equiv="refresh" content="0; url='${myIPFS}$(cat ${MY_PATH}/pdf/${PUBKEY}/DRIVESTATE)'">
            </head><body></body></html>' > ${MY_PATH}/tmp/${MOATS}.out.html
            echo "${MY_PATH}/tmp/${MOATS}.out.html"
            exit 0
        else
            ## ZEROCARD 1ST APP ##### 2ND SCAN : CHANGE DRIVESTATE TO IPFSPORTAL CONTENT
            CODEINJECT='<a target=_new href='${myIPFS}'/ipfs/'$(cat ${MY_PATH}/pdf/${PUBKEY}/IPFSPORTAL)'/${PUBKEY}/>'${AMOUNT}'</a>'
            ## BACKUP FAC SIMILE
            mv ${MY_PATH}/pdf/${PUBKEY}/_index.html \
                ${MY_PATH}/pdf/${PUBKEY}/_facsimile.html

            cat ${MY_PATH}/templates/message.html \
            | sed -e "s~_TITLE_~$(date -u) <br> ${PUBKEY}~g" \
                 -e "s~_MESSAGE_~${CODEINJECT}~g" \
                 -e "s~300px~401px~g" \
                > ${MY_PATH}/pdf/${PUBKEY}/_index.html # REPLACE UPASSPORT HTML

            echo ${PUBKEY} > ${MY_PATH}/pdf/${PUBKEY}/PUBKEY
            DRIVESTATE=$(ipfs add -q ${MY_PATH}/pdf/${PUBKEY}/_index.html)
            echo "/ipfs/${DRIVESTATE}" > ${MY_PATH}/pdf/${PUBKEY}/DRIVESTATE # UPDATE DRIVESTATE
            ipfs name publish --key ${ZEROCARD} /ipfs/${DRIVESTATE}
            echo "${MY_PATH}/pdf/${PUBKEY}/_index.html"
            exit 0
        fi

    else
        ##### NO GOOD BACK TO 1ST SCAN
        echo "........... ZEROCARD NOT ACTIVATED YET"
    fi

    ## CHECK IF OUTGOING TX
    if [ "$(echo "$LASTX < 0" | bc)" -eq 1 ]; then
    ######################################################################
      echo "TX: $DEST ($COMM)"
      if [[ "$ZEROCARD" == "$DEST" ]]; then
        ################# ACTIVATION ###############
        echo "MATCHING !! ZEROCARD INITIALISATION..."
        echo "$TXDATE" > ${MY_PATH}/pdf/${PUBKEY}/COMMANDTIME
        ################# ACTIVATION ###############
        ## Replace FAC SIMILE with page2
        ## Add AVATAR.png to onPAPERIPFS
        [[ -s ${MY_PATH}/pdf/${PUBKEY}/AVATAR.png ]] \
            && convert ${MY_PATH}/pdf/${PUBKEY}/AVATAR.png -rotate +90 -resize 120x120 ${MY_PATH}/tmp/${PUBKEY}.AVATAR_rotated.png \
            && composite -gravity NorthEast -geometry +160+20 \
                ${MY_PATH}/tmp/${PUBKEY}.AVATAR_rotated.png ${MY_PATH}/static/.nostr.zine/page2.png \
                ${MY_PATH}/tmp/${PUBKEY}.onPAPER.png \
            && onPAPERIPFS="$(ipfs add -wq ${MY_PATH}/tmp/${PUBKEY}.onPAPER.png | tail -n 1)/${PUBKEY}.onPAPER.png"

        [[ -z $onPAPERIPFS ]] \
            && onPAPERIPFS="QmVJftuuuLgTJ8tb2kLhaKdaWFWH3jd4YXYJwM4h96NF8Q/page2.png"
        echo "AVATAR is on PAPER"
        sed -i "s~QmRJuGqHsruaV14ZHEjk9Gxog2B9GafC35QYrJtaAU2Pry~${onPAPERIPFS}~g" ${MY_PATH}/pdf/${PUBKEY}/_index.html

        ## Collect previous data
        OIPNSQR=$(ipfs add -q ${MY_PATH}/pdf/${PUBKEY}/IPNS.QR.png)
        ZWALL=$(cat ${MY_PATH}/pdf/${PUBKEY}/ZWALL)
        ZEROCARD=$(cat ${MY_PATH}/pdf/${PUBKEY}/ZEROCARD)
        ## Change ẐeroCard G1/Cesium link to ZEROCARD /IPNS link
        sed -i "s~${ZWALL}~${OIPNSQR}~g" ${MY_PATH}/pdf/${PUBKEY}/_index.html
        sed -i "s~${myIPFS}/ipfs/QmYZWzSfPgb1y83fWTmKBEHdA9QoxsYBmqLkEJU2KQ1DYW/#/app/wot/${ZEROCARD}/~${myIPFS}/ipns/$(cat ${MY_PATH}/pdf/${PUBKEY}/IPNS)~g" ${MY_PATH}/pdf/${PUBKEY}/_index.html
        ## NEW IPFSPORTAL (DATA : ${MY_PATH}/pdf/${PUBKEY}/*)
        IPFSPORTAL=$(ipfs add -qrw ${MY_PATH}/pdf/${PUBKEY}/ | tail -n 1)
        ipfs pin rm ${IPFSPORTAL}

        ### EXTEND IPNS QR with CAPTAIN ssss key part
        $HOME/.zen/Astroport.ONE/tools/natools.py decrypt -f pubsec -i ${MY_PATH}/pdf/${PUBKEY}/ssss.tail.2U.enc -k ~/.zen/game/players/.current/secret.dunikey -o ${MY_PATH}/tmp/${PUBKEY}.2U
            amzqr "$(cat ${MY_PATH}/tmp/${PUBKEY}.2U)" -l H -n ${PUBKEY}.2U.png -d ${MY_PATH}/tmp/ 2>/dev/null

            CAPTAINTAIL=$(ipfs add -q ${MY_PATH}/tmp/${PUBKEY}.2U.png)
            ipfs pin rm $CAPTAINTAIL
            # Clean up temporary files
            rm ${MY_PATH}/tmp/${PUBKEY}.captain.png
            rm ${MY_PATH}/tmp/${PUBKEY}.captain

        ## IPFSPORTAL = DATA ipfs link
        amzqr "${myIPFS}/ipfs/${IPFSPORTAL}" -l H -p ${MY_PATH}/static/img/server.png -c -n ${PUBKEY}.ipfs.png -d ${MY_PATH}/tmp/
        convert ${MY_PATH}/tmp/${PUBKEY}.ipfs.png \
        -gravity SouthWest \
        -pointsize 18 \
        -fill black \
        -annotate +2+2 "[DRIVE] ${IPFSPORTAL}" \
        -annotate +1+3 "[DRIVE] ${IPFSPORTAL}" \
        ${MY_PATH}/pdf/${PUBKEY}/IPFSPORTAL.QR.png

        IPFSPORTALQR=$(ipfs add -q ${MY_PATH}/pdf/${PUBKEY}/IPFSPORTAL.QR.png)
        ipfs pin rm $IPFSPORTALQR
        sed -i "s~$(cat ${MY_PATH}/pdf/${PUBKEY}/IPFSPORTALQR)~${CAPTAINTAIL}~g" ${MY_PATH}/pdf/${PUBKEY}/_index.html
        sed -i "s~$(cat ${MY_PATH}/pdf/${PUBKEY}/IPFSPORTAL)~${IPFSPORTAL}~g" ${MY_PATH}/pdf/${PUBKEY}/_index.html
        echo $IPFSPORTALQR > ${MY_PATH}/pdf/${PUBKEY}/IPFSPORTALQR
        echo $IPFSPORTAL > ${MY_PATH}/pdf/${PUBKEY}/IPFSPORTAL
        echo "$(date -u)" > ${MY_PATH}/pdf/${PUBKEY}/DATE
        echo $IPFSNODEID > ${MY_PATH}/pdf/${PUBKEY}/ASTROPORT
        echo "NEW IPFSPORTAL : ${myIPFS}/ipfs/${IPFSPORTAL} $(cat ${MY_PATH}/pdf/${PUBKEY}/DATE)"

        ## IMPORT ZEROCARD into LOCAL IPFS KEYS
        ## Décodage clef IPNS par secret UPLANETNAME
        cat ${MY_PATH}/pdf/${PUBKEY}/IPNS.uplanet.asc | gpg -d --passphrase "${UPLANETNAME}" --batch > ${MY_PATH}/tmp/${MOATS}.ipns
        ipfs key rm ${ZEROCARD} > /dev/null 2>&1
        WALLETNS=$(ipfs key import ${ZEROCARD} -f pem-pkcs8-cleartext ${MY_PATH}/tmp/${MOATS}.ipns)
        ## DRIVESTATE FIRST DApp => Wallet AMOUNT + ZEROCARD QR + N1 APP link
        CODEINJECT="<a target=N1 href=${myIPFS}/ipfs/${IPFSPORTAL}/${PUBKEY}/><img width=240px src=${myIPFS}/ipfs/${ZWALL} /></a>"
        cat ${MY_PATH}/templates/message.html \
        | sed -e "s~_TITLE_~$(date -u) <br> ${PUBKEY}~g" \
             -e "s~_MESSAGE_~${CODEINJECT}~g" \
             -e "s~300px~340px~g" \
            > ${MY_PATH}/tmp/${MOATS}.out.html

        # PUBLISH 1ST DRIVESTATE
        ###  N1 NETWORK EXPLORER PREVIEW
        ### SO USER ENTER EMAIL TO JOIN UPLANET...
        DRIVESTATE=$(ipfs add -q ${MY_PATH}/tmp/${MOATS}.out.html)
        echo "/ipfs/${DRIVESTATE}" > ${MY_PATH}/pdf/${PUBKEY}/DRIVESTATE
        ipfs name publish --key ${ZEROCARD} /ipfs/${DRIVESTATE}

        ### OFFICIAL
        ######### move PDF to PASSPORT ################### in ASTROPORT game
        mkdir -p ~/.zen/game/passport
        mv ${MY_PATH}/pdf/${PUBKEY} ~/.zen/game/passport/
        ln -s ~/.zen/game/passport/${PUBKEY} ${MY_PATH}/pdf/${PUBKEY}
        cp ~/.zen/game/passport/${PUBKEY}/_index.html ~/.zen/game/passport/${PUBKEY}/.passport.html ## Hide from IPFS publish 
        ## UPLANETNAME SECURIZE UPASSPORT.asc
        rm -f ${MY_PATH}/pdf/${PUBKEY}/UPASSPORT.asc
        cat ${MY_PATH}/pdf/${PUBKEY}/_index.html | gpg --symmetric --armor --batch --passphrase "${UPLANETNAME}" -o ${MY_PATH}/pdf/${PUBKEY}/UPASSPORT.asc
        ## REMOVE FROM PORTAL DIRECTORY
        mv ${MY_PATH}/pdf/${PUBKEY}/_index.html ${MY_PATH}/tmp/${PUBKEY}_index.html
        #### UPASSPORT READY #####
        echo "${MY_PATH}/tmp/${PUBKEY}_index.html"
        exit 0
      else
        ## RESEND FAC SIMILE
        echo "TX NOT FOR ZEROCARD"
        echo "${MY_PATH}/pdf/${PUBKEY}/_index.html"
        exit 0
      fi
    else
        ## RESEND FAC SIMILE
        echo "RX..."
        echo "${MY_PATH}/pdf/${PUBKEY}/_index.html"
        exit 0
    fi
fi

#######################################################################
#######################################################################
#######################################################################
### FIRST TRY. NO ZEROCARD MADE YET.
### FRESH PUBKEY... IS IT A MEMBER 0R A WALLET ?
echo "## GETTING CESIUM+ PROFILE"
[[ ! -s ${MY_PATH}/tmp/$PUBKEY.me.json ]] \
&& $HOME/.zen/Astroport.ONE/tools/timeout.sh -t 8 \
wget -q -O ${MY_PATH}/tmp/$PUBKEY.me.json ${myDUNITER}/wot/lookup/$PUBKEY

echo "# GET MEMBER UID"
MEMBERUID=$(cat ${MY_PATH}/tmp/$PUBKEY.me.json | jq -r '.results[].uids[].uid')

if [[ -z $MEMBERUID ]]; then
    ## NOT MEMBERUID : THIS IS A SIMPLE WALLET - show amount -
    cat ${MY_PATH}/templates/message.html \
        | sed -e "s~_TITLE_~$(date -u) <br> ${PUBKEY}~g" \
             -e "s~_MESSAGE_~${AMOUNT}~g" \
            > ${MY_PATH}/tmp/${MOATS}.out.html
    #~ xdg-open "${MY_PATH}/tmp/${MOATS}.out.html"
    echo "${MY_PATH}/tmp/${MOATS}.out.html"
    exit 0
fi
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
### MEMBER N1 SCAN & UPASSPORT CREATION
## N1 DESTINATION PATH
mkdir -p ${MY_PATH}/pdf/${PUBKEY}/N1/
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## CESIUM RELATIONS & DUNITER AMOUNT extract
cp ${MY_PATH}/tmp/$PUBKEY.me.json ${MY_PATH}/pdf/${PUBKEY}/P2P.CESIUM.json
cp ${MY_PATH}/tmp/$PUBKEY.TX.json ${MY_PATH}/pdf/${PUBKEY}/TX.json

################################################## N1 analysing
### ANALYSE RELATIONS FROM ${MY_PATH}/tmp/$PUBKEY.me.json
##################################################"
# Extract the uids and pubkeys into a bash array
certbyme=$(jq -r '.results[].signed[] | [.uid, .pubkey] | @tsv' ${MY_PATH}/tmp/$PUBKEY.me.json)
# Initialize a bash array
declare -A certout

# Populate the array
while IFS=$'\t' read -r uid pubkey; do
  certout["$uid"]="$pubkey"
done <<< "$certbyme"

echo "___________Pubkey Certifiés P21 :"
# Access the values (example: print all uids and their corresponding pubkeys)
for uid in "${!certout[@]}"; do
  echo "UID: $uid, PubKey: ${certout[$uid]}"
done

## GET certifiers-of
[[ ! -s ${MY_PATH}/tmp/$PUBKEY.them.json ]] \
&& wget -q -O ${MY_PATH}/tmp/$PUBKEY.them.json "${myDUNITER}/wot/certifiers-of/$PUBKEY?pubkey=true"

# Extract the uids and pubkeys into a bash array
certbythem=$(jq -r '.certifications[] | [.uid, .pubkey] | @tsv' ${MY_PATH}/tmp/$PUBKEY.them.json)
# Initialize a bash array
declare -A certin

# Populate the array
while IFS=$'\t' read -r uid pubkey; do
  certin["$uid"]="$pubkey"
done <<< "$certbythem"

echo "___________Pubkey certifiantes 12P :"
# Access the values (example: print all uids and their corresponding pubkeys)
for uid in "${!certin[@]}"; do
  echo "UID: $uid, PubKey: ${certin[$uid]}"
done

# Compare the arrays for matches and differences
echo "Comparaison des certificats:"
echo "========================================================"
echo "___________ UIDs présents dans certin et certout (matchs) : P2P"
for uid in "${!certin[@]}"; do
  if [[ -n "${certout[$uid]}" ]]; then
    echo "UID: $uid, PubKey certifié: ${certout[$uid]}, PubKey certifiant: ${certin[$uid]}"

    # make friends QR
    [[ ! -s ${MY_PATH}/pdf/${PUBKEY}/N1/${certout[$uid]}.${uid}.p2p.png ]] \
        && generate_qr_with_uid "${certout[$uid]}" "$uid" \
        && $(source ${MY_PATH}/tmp/${certout[$uid]}.GPS) \
        && cp ${MY_PATH}/tmp/${certout[$uid]}.UID.png ${MY_PATH}/pdf/${PUBKEY}/N1/${certout[$uid]}.${uid}.p2p.$ulat.$ulon.png
  fi
done
TOTP2P=$TOT
TOT=0
echo "TOT_P2P=$TOTP2P"

echo "========================================================"
echo "___________ UIDs présents uniquement dans certin (certificateurs uniquement) : 12P"
for uid in "${!certin[@]}"; do
  if [[ -z "${certout[$uid]}" ]]; then
    echo "UID: $uid, PubKey certifiant: ${certin[$uid]}"

    # make certin only QR
    [[ ! -s ${MY_PATH}/pdf/${PUBKEY}/N1/${certin[$uid]}.${uid}.certin.png ]] \
        && generate_qr_with_uid "${certin[$uid]}" "$uid" \
        && $(source ${MY_PATH}/tmp/${certin[$uid]}.GPS) \
        && cp ${MY_PATH}/tmp/${certin[$uid]}.UID.png ${MY_PATH}/pdf/${PUBKEY}/N1/${certin[$uid]}.${uid}.certin.$ulat.$ulon.png
  fi
done
TOT12P=$TOT
TOT=0
echo "TOT_12P=$TOT12P"

echo "========================================================"
echo "___________UIDs présents uniquement dans certout (certifiés uniquement) : P21"
for uid in "${!certout[@]}"; do
  if [[ -z "${certin[$uid]}" ]]; then
    echo "UID: $uid, PubKey certifié: ${certout[$uid]}"
    # make certout only QR
    [[ ! -s ${MY_PATH}/pdf/${PUBKEY}/N1/${certout[$uid]}.${uid}.certout.png ]] \
        && generate_qr_with_uid "${certout[$uid]}" "$uid" \
        && $(source ${MY_PATH}/tmp/${certout[$uid]}.GPS) \
        && cp ${MY_PATH}/tmp/${certout[$uid]}.UID.png ${MY_PATH}/pdf/${PUBKEY}/N1/${certout[$uid]}.${uid}.certout.$ulat.$ulon.png
  fi
done
TOTP21=$TOT
TOT=0
echo "TOTP_21=$TOTP21"

TOTAL=$((TOTP2P + TOT12P + TOTP21))
echo $TOTAL > ${MY_PATH}/pdf/${PUBKEY}/TOTAL

# Create manifest.json add App for N1 level
${MY_PATH}/tools/createN1json.sh ${MY_PATH}/pdf/${PUBKEY}/N1/
cp ${MY_PATH}/static/N1/index.html ${MY_PATH}/pdf/${PUBKEY}/N1/_index.html

# Generate PUBKEY and MEMBERUID "QRCODE" add TOTAL
generate_qr_with_uid "$PUBKEY" "$MEMBERUID"
cp ${MY_PATH}/tmp/${PUBKEY}.GPS ${MY_PATH}/pdf/${PUBKEY}/GPS
cp ${MY_PATH}/tmp/${PUBKEY}.png ${MY_PATH}/pdf/${PUBKEY}/AVATAR.png
convert ${MY_PATH}/tmp/${PUBKEY}.png -resize 32x32! ${MY_PATH}/pdf/${PUBKEY}/N1/favicon.ico
cp ${MY_PATH}/tmp/${PUBKEY}.UID.png ${MY_PATH}/pdf/${PUBKEY}/${PUBKEY}.UID.png
convert ${MY_PATH}/tmp/${PUBKEY}.UID.png \
      -gravity NorthEast \
      -pointsize 25 \
      -fill black \
      -annotate +2+2 "${TOTAL} Z" \
      -annotate +3+1 "${TOTAL} Z" \
      ${MY_PATH}/pdf/${PUBKEY}/${PUBKEY}.UID.png
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# CREATE FRIENDS PAGES INTO PDF & png
## Moving Related UID into ${MY_PATH}/pdf/${PUBKEY}/N1/
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## Peer to Peer “Followers/Following”
nb_fichiers=$(ls ${MY_PATH}/pdf/${PUBKEY}/N1/*.p2p*.png | wc -l)
montage -mode concatenate -geometry +20x20 -tile $(echo "scale=0; $nb_fichiers / sqrt($nb_fichiers) - 1" | bc)x$(echo "scale=0; sqrt($nb_fichiers) + 3" | bc) -density 300 ${MY_PATH}/pdf/${PUBKEY}/N1/*.p2p*.png ${MY_PATH}/pdf/${PUBKEY}/P2P.${PUBKEY}.pdf
echo "convert -density 300 ${MY_PATH}/pdf/${PUBKEY}/P2P.${PUBKEY}.pdf -resize 375x550 ${MY_PATH}/pdf/${PUBKEY}/P2P.png"
convert -density 300 ${MY_PATH}/pdf/${PUBKEY}/P2P.${PUBKEY}.pdf -resize 375x550 ${MY_PATH}/pdf/${PUBKEY}/P2P.png
## Peer to One “Followers”
nb_fichiers=$(ls ${MY_PATH}/pdf/${PUBKEY}/N1/*.certin*.png | wc -l)
montage -mode concatenate -geometry +20x20 -tile $(echo "scale=0; $nb_fichiers / sqrt($nb_fichiers) - 1" | bc)x$(echo "scale=0; sqrt($nb_fichiers) + 3" | bc) -density 300 ${MY_PATH}/pdf/${PUBKEY}/N1/*.certin*.png ${MY_PATH}/pdf/${PUBKEY}/P21.${PUBKEY}.pdf
echo "convert -density 300 ${MY_PATH}/pdf/${PUBKEY}/P21.${PUBKEY}.pdf -resize 375x550 ${MY_PATH}/pdf/${PUBKEY}/P21.png"
convert -density 300 ${MY_PATH}/pdf/${PUBKEY}/P21.${PUBKEY}.pdf -resize 375x550 ${MY_PATH}/pdf/${PUBKEY}/P21.png
## One to Peer “Following”
nb_fichiers=$(ls ${MY_PATH}/pdf/${PUBKEY}/N1/*.certout*.png | wc -l)
montage -mode concatenate -geometry +20x20 -tile $(echo "scale=0; $nb_fichiers / sqrt($nb_fichiers) - 1" | bc)x$(echo "scale=0; sqrt($nb_fichiers) + 3" | bc) -density 300 ${MY_PATH}/pdf/${PUBKEY}/N1/*.certout*.png ${MY_PATH}/pdf/${PUBKEY}/12P.${PUBKEY}.pdf
echo "convert -density 300 ${MY_PATH}/pdf/${PUBKEY}/12P.${PUBKEY}.pdf -resize 375x550 ${MY_PATH}/pdf/${PUBKEY}/12P.png"
convert -density 300 ${MY_PATH}/pdf/${PUBKEY}/12P.${PUBKEY}.pdf -resize 375x550 ${MY_PATH}/pdf/${PUBKEY}/12P.png

################################################################################
echo "############################################################"
echo "# CREATE ZEROCARD ......... TOTAL = $TOTAL Z"
echo "############################################################"
############################################## PREPARE SALT PEPPER
prime=$($HOME/.zen/Astroport.ONE/tools/diceware.sh 1 | xargs)
SALT=$(tr -dc 'a-zA-Z0-9' < /dev/urandom | fold -w42 | head -n1)
second=$($HOME/.zen/Astroport.ONE/tools/diceware.sh 1 | xargs)
PEPPER=$(tr -dc 'a-zA-Z0-9' < /dev/urandom | fold -w42 | head -n1)
################################################################# DUNITER
$HOME/.zen/Astroport.ONE/tools/keygen -t duniter -o ${MY_PATH}/tmp/${PUBKEY}.zerocard.dunikey "${SALT}" "${PEPPER}"
G1PUBZERO=$(cat ${MY_PATH}/tmp/${PUBKEY}.zerocard.dunikey  | grep 'pub:' | cut -d ' ' -f 2)

## ENCRYPT WITH UPLANETNAME PASSWORD
if [[ ! -z ${UPLANETNAME} ]]; then
    rm -f ${MY_PATH}/pdf/${PUBKEY}/zerocard.planet.asc
    cat ${MY_PATH}/tmp/${PUBKEY}.zerocard.dunikey | gpg --symmetric --armor --batch --passphrase "${UPLANETNAME}" -o ${MY_PATH}/pdf/${PUBKEY}/zerocard.planet.asc
fi

rm -f ${MY_PATH}/pdf/${PUBKEY}/zerocard.member.enc
## zerocard.dunikey PUBKEY encryption
$HOME/.zen/Astroport.ONE/tools/natools.py encrypt -p $PUBKEY -i ${MY_PATH}/tmp/${PUBKEY}.zerocard.dunikey -o ${MY_PATH}/pdf/${PUBKEY}/zerocard.member.enc
echo "ZEN _WALLET: $G1PUBZERO"
rm -f ${MY_PATH}/pdf/${PUBKEY}/ZEROCARD_*.QR.jpg # cleaning & provisionning
echo "${G1PUBZERO}" > ${MY_PATH}/pdf/${PUBKEY}/ZEROCARD
echo "$(date -u)" > ${MY_PATH}/pdf/${PUBKEY}/DATE
## create ZEROCARD QR
amzqr "${G1PUBZERO}" -l H -p ${MY_PATH}/static/img/GZen.png -c -n ZEROCARD_${G1PUBZERO}.QR.png -d ${MY_PATH}/tmp/ 2>/dev/null
        # Write G1PUBZERO at the bottom
        convert ${MY_PATH}/tmp/ZEROCARD_${G1PUBZERO}.QR.png \
          -gravity SouthWest \
          -pointsize 18 \
          -fill black \
          -annotate +2+2 "[ZEROCARD] ${G1PUBZERO}" \
          -annotate +1+3 "[ZEROCARD] ${G1PUBZERO}" \
          ${MY_PATH}/pdf/${PUBKEY}/ZEROCARD_${G1PUBZERO}.QR.jpg

################################################################# IPNS
#~ ## CREATE IPNS KEY
$HOME/.zen/Astroport.ONE/tools/keygen -t ipfs -o ${MY_PATH}/tmp/${G1PUBZERO}.IPNS.key "${SALT}" "${PEPPER}"
IPNS12D=$($HOME/.zen/Astroport.ONE/tools/keygen -t ipfs "${SALT}" "${PEPPER}")
ipfs key rm ${G1PUBZERO} > /dev/null 2>&1
WALLETNS=$(ipfs key import ${G1PUBZERO} -f pem-pkcs8-cleartext ${MY_PATH}/tmp/${G1PUBZERO}.IPNS.key)

## ENCODE IPNS KEY WITH CAPTAING1PUB
echo "$HOME/.zen/Astroport.ONE/tools/natools.py encrypt -p $CAPTAING1PUB -i ${MY_PATH}/tmp/${G1PUBZERO}.IPNS.key -o ${MY_PATH}/pdf/${PUBKEY}/IPNS.captain.enc"
$HOME/.zen/Astroport.ONE/tools/natools.py encrypt -p $CAPTAING1PUB -i ${MY_PATH}/tmp/${G1PUBZERO}.IPNS.key -o ${MY_PATH}/pdf/${PUBKEY}/IPNS.captain.enc

## ENCRYPT WITH UPLANETNAME PASSWORD
if [[ ! -z ${UPLANETNAME} ]]; then
    rm -f ${MY_PATH}/pdf/${PUBKEY}/IPNS.uplanet.asc
    cat ${MY_PATH}/tmp/${G1PUBZERO}.IPNS.key | gpg --symmetric --armor --batch --passphrase "${UPLANETNAME}" -o ${MY_PATH}/pdf/${PUBKEY}/IPNS.uplanet.asc
fi

rm ${MY_PATH}/tmp/${G1PUBZERO}.IPNS.key
ipfs key rm ${G1PUBZERO} > /dev/null 2>&1
echo "IPNS APP KEY : $IPNS12D /ipns/ $WALLETNS"
amzqr "${myIPFS}/ipns/$IPNS12D" -l H -p ${MY_PATH}/static/img/moa_net.png -c -n ${PUBKEY}.IPNS.QR.png -d ${MY_PATH}/tmp/ 2>/dev/null
convert ${MY_PATH}/tmp/${PUBKEY}.IPNS.QR.png \
        -gravity SouthWest \
        -pointsize 18 \
        -fill black \
        -annotate +2+2 "[APP] $IPNS12D" \
        -annotate +1+3 "[APP] $IPNS12D" \
        ${MY_PATH}/pdf/${PUBKEY}/IPNS.QR.png

## Record for url linking during validation
echo "$WALLETNS" > ${MY_PATH}/pdf/${PUBKEY}/IPNS
echo "$IPNS12D" > ${MY_PATH}/pdf/${PUBKEY}/IPNS12D

#######################################################################
## PREPARE DISCO SHAMIR SECRET DISTRIBUTION - extend UX -
# HUMAIN = HEAD
# UPLANET = MIDDLE
# CAPTAIN = TAIL
DISCO="/?${prime}=${SALT}&${second}=${PEPPER}"
echo "SOURCE : "$DISCO

## ssss-split : Need 2 over 3
echo "$DISCO" | ssss-split -t 2 -n 3 -q > ${MY_PATH}/tmp/${G1PUBZERO}.ssss
HEAD=$(cat ${MY_PATH}/tmp/${G1PUBZERO}.ssss | head -n 1) && echo "$HEAD" > ${MY_PATH}/tmp/${G1PUBZERO}.ssss.head
MIDDLE=$(cat ${MY_PATH}/tmp/${G1PUBZERO}.ssss | head -n 2 | tail -n 1) && echo "$MIDDLE" > ${MY_PATH}/tmp/${G1PUBZERO}.ssss.mid
TAIL=$(cat ${MY_PATH}/tmp/${G1PUBZERO}.ssss | tail -n 1) && echo "$TAIL" > ${MY_PATH}/tmp/${G1PUBZERO}.ssss.tail
echo "TEST DECODING..."
echo "$HEAD
$TAIL" | ssss-combine -t 2 -q
[ ! $? -eq 0 ] && echo "ERROR! SSSSKEY DECODING FAILED" && echo "${MY_PATH}/templates/message.html" && exit 1

##########################################################################
### CRYPTO ZONE
## ENCODE HEAD SSSS SECRET WITH MEMBER PUBKEY
echo "$HOME/.zen/Astroport.ONE/tools/natools.py encrypt -p $PUBKEY -i ${MY_PATH}/tmp/${G1PUBZERO}.ssss.head -o ${MY_PATH}/pdf/${PUBKEY}/ssss.member.enc"
$HOME/.zen/Astroport.ONE/tools/natools.py encrypt -p $PUBKEY -i ${MY_PATH}/tmp/${G1PUBZERO}.ssss.head -o ${MY_PATH}/pdf/${PUBKEY}/ssss.head.member.enc

echo "$HOME/.zen/Astroport.ONE/tools/natools.py encrypt -p $CAPTAING1PUB -i ${MY_PATH}/tmp/${G1PUBZERO}.ssss.mid -o ${MY_PATH}/pdf/${PUBKEY}/ssss.mid.captain.enc"
$HOME/.zen/Astroport.ONE/tools/natools.py encrypt -p $CAPTAING1PUB -i ${MY_PATH}/tmp/${G1PUBZERO}.ssss.mid -o ${MY_PATH}/pdf/${PUBKEY}/ssss.mid.captain.enc

## MIDDLE ENCRYPT WITH UPLANETNAME
if [[ ! -z ${UPLANETNAME} ]]; then
    rm -f ${MY_PATH}/pdf/${PUBKEY}/ssss.uplanet.asc
    cat ${MY_PATH}/tmp/${G1PUBZERO}.ssss | gpg --symmetric --armor --batch --passphrase "${UPLANETNAME}" -o ${MY_PATH}/pdf/${PUBKEY}/ssss.uplanet.asc
    cat ${MY_PATH}/pdf/${PUBKEY}/ssss.uplanet.asc | gpg -d --passphrase "${UPLANETNAME}" --batch > ${MY_PATH}/tmp/${G1PUBZERO}.ssss.test
    [[ $(diff -q ${MY_PATH}/tmp/${G1PUBZERO}.ssss.test ${MY_PATH}/tmp/${G1PUBZERO}.ssss) != "" ]] && echo "ERROR: GPG ENCRYPTION FAILED !!!"
    rm ${MY_PATH}/tmp/${G1PUBZERO}.ssss.test
fi

## ENCODE TAIL SSSS SECRET WITH CAPTAING1PUB
$HOME/.zen/Astroport.ONE/tools/natools.py encrypt -p ${CAPTAING1PUB} -i ${MY_PATH}/tmp/${G1PUBZERO}.ssss.tail -o ${MY_PATH}/pdf/${PUBKEY}/ssss.tail.2U.enc

## REMOVE SENSIBLE DATA FROM CACHE
# DEEPER SECURITY CONCERN ? mount ${MY_PATH}/tmp as encrypted RAM disk
rm ${MY_PATH}/tmp/${G1PUBZERO}.ssss*

#### INITIALISE IPFS STORAGE ZEROCARD "BLOCK 0"
### add html page for next step...
rm -f ${MY_PATH}/pdf/${PUBKEY}/_index.html
echo "CREATION IPFS PORTAIL"

## Add Images to ipfs
MEMBERPUBQR=$(ipfs add -q ${MY_PATH}/pdf/${PUBKEY}/${PUBKEY}.UID.png)
ZWALLET=$(ipfs add -q ${MY_PATH}/pdf/${PUBKEY}/ZEROCARD_${G1PUBZERO}.QR.jpg)
echo "$ZWALLET" > ${MY_PATH}/pdf/${PUBKEY}/ZWALL

## IPFSPORTAL : ${MY_PATH}/pdf/${PUBKEY}
IPFSPORTAL=$(ipfs add -qrw ${MY_PATH}/pdf/${PUBKEY}/ | tail -n 1)
echo $IPFSPORTAL > ${MY_PATH}/pdf/${PUBKEY}/IPFSPORTAL

ipfs pin rm ${IPFSPORTAL}
echo "${myIPFS}/ipfs/${IPFSPORTAL}"

amzqr "${myIPFS}/ipfs/${IPFSPORTAL}" -l H -p ${MY_PATH}/static/img/server.png -c -n ${PUBKEY}.ipfs.png -d ${MY_PATH}/tmp/
convert ${MY_PATH}/tmp/${PUBKEY}.ipfs.png \
        -gravity SouthWest \
        -pointsize 18 \
        -fill black \
        -annotate +2+2 "[N1] ${IPFSPORTAL}" \
        -annotate +1+3 "[N1] ${IPFSPORTAL}" \
        ${MY_PATH}/pdf/${PUBKEY}/IPFSPORTAL.QR.png

IPFSPORTALQR=$(ipfs add -q ${MY_PATH}/pdf/${PUBKEY}/IPFSPORTAL.QR.png)
echo $IPFSPORTALQR > ${MY_PATH}/pdf/${PUBKEY}/IPFSPORTALQR

################################################################### ZINE
echo "Create Zine Passport"

[ -s ${MY_PATH}/pdf/${PUBKEY}/P2P.png ] \
    && FULLCERT=$(ipfs add -q ${MY_PATH}/pdf/${PUBKEY}/P2P.png) \
    || FULLCERT="QmReCfHszucv2Ra9zbKjKwmgoJ4krWpqB12TDK5AR9PKCQ/page3.png"

[ -s ${MY_PATH}/pdf/${PUBKEY}/P21.png ] \
    && CERTIN=$(ipfs add -q ${MY_PATH}/pdf/${PUBKEY}/P21.png) \
    || CERTIN="QmReCfHszucv2Ra9zbKjKwmgoJ4krWpqB12TDK5AR9PKCQ/page4.png"

[ -s ${MY_PATH}/pdf/${PUBKEY}/12P.png ] \
    && CERTOUT=$(ipfs add -q ${MY_PATH}/pdf/${PUBKEY}/12P.png) \
    || CERTOUT="QmReCfHszucv2Ra9zbKjKwmgoJ4krWpqB12TDK5AR9PKCQ/page4.png"

if [[ -z $LAT || -z $LON ]]; then
    cLAT=$(cat ${MY_PATH}/tmp/${PUBKEY}.cesium.json | jq -r '._source.geoPoint.lat')
    LAT=$(makecoord $cLAT)
    cLON=$(cat ${MY_PATH}/tmp/${PUBKEY}.cesium.json | jq -r '._source.geoPoint.lon')
    LON=$(makecoord $cLON)
fi

# QmRJuGqHsruaV14ZHEjk9Gxog2B9GafC35QYrJtaAU2Pry Fac Similé
# QmVJftuuuLgTJ8tb2kLhaKdaWFWH3jd4YXYJwM4h96NF8Q/page2.png
cat ${MY_PATH}/static/zine/UPassport.html \
    | sed -e "s~QmU43PSABthVtM8nWEWVDN1ojBBx36KLV5ZSYzkW97NKC3/page1.png~QmdEPc4Toy1vth7MZtpRSjgMtAWRFihZp3G72Di1vMhf1J~g" \
            -e "s~QmVJftuuuLgTJ8tb2kLhaKdaWFWH3jd4YXYJwM4h96NF8Q/page2.png~${FULLCERT}~g" \
            -e "s~QmTL7VDgkYjpYC2qiiFCfah2pSqDMkTANMeMtjMndwXq9y~QmRJuGqHsruaV14ZHEjk9Gxog2B9GafC35QYrJtaAU2Pry~g" \
            -e "s~QmexZHwUuZdFLZuHt1PZunjC7c7rTFKRWJDASGPTyrqysP/page3.png~${CERTIN}~g" \
            -e "s~QmNNTCYNSHS3iKZsBHXC1tiP2eyFqgLT4n3AXdcK7GywVc/page4.png~${CERTOUT}~g" \
            -e "s~QmZHV5QppQX9N7MS1GFMqzmnRU5vLbpmQ1UkSRY5K5LfA9/page_.png~${IPFSPORTALQR}~g" \
            -e "s~QmNSck9ygXYG6YHu19DfuJnH2B8yS9RRkEwP1tD35sjUgE/pageZ.png~${MEMBERPUBQR}~g" \
            -e "s~QmdmeZhD8ncBFptmD5VSJoszmu41edtT265Xq3HVh8PhZP~${ZWALLET}~g" \
            -e "s~_IPFS_~ipfs/${IPFSPORTAL}/${PUBKEY}/~g" \
            -e "s~_PLAYER_~${MEMBERUID}~g" \
            -e "s~_UPLANET8_~UPlanet:${UPLANETG1PUB:0:8}~g" \
            -e "s~_DATE_~$(date -u)~g" \
            -e "s~_PUBKEY_~${PUBKEY}~g" \
            -e "s~_ZEROCARD_~${ZEROCARD}~g" \
            -e "s~_G1PUB_~${G1PUBZERO}~g" \
            -e "s~_G1PUBZERO_~${G1PUBZERO}~g" \
            -e "s~_TOTAL_~${TOTAL}~g" \
            -e "s~_LAT_~${LAT}~g" \
            -e "s~_LON_~${LON}~g" \
            -e "s~_ASTROPORT_~${myIPFS}~g" \
            -e "s~http://127.0.0.1:8080~${myIPFS}~g" \
        > ${MY_PATH}/pdf/${PUBKEY}/_index.html

echo "${MY_PATH}/pdf/${PUBKEY}/_index.html"

exit 0
