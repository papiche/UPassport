#!/bin/bash
################################################################### delete0card.sh
# Author: Fred (DsEx1pS33vzYZg4MroyBV9hCw98j1gtHEhwiZ5tK7ech)
# Version: 1.0
# License: AGPL-3.0 (https://choosealicense.com/licenses/agpl-3.0/)
################################################################################
# EMpty & Remove ZEROCARD

MY_PATH="`dirname \"$0\"`"              # relative
MY_PATH="`( cd \"$MY_PATH\" && pwd )`"  # absolutized and normalized
ME="${0##*/}"
## LOAD ASTROPORT ENVIRONMENT
[ ! -s $HOME/.zen/Astroport.ONE/tools/my.sh ] \
    && echo "<h1>ERROR/ Missing Astroport.ONE. Please install...<h1>" \
    && exit 1
. "$HOME/.zen/Astroport.ONE/tools/my.sh"
# Vérifier le nombre d'arguments
if [ $# -ne 1 ]; then
    echo "Usage: $0 <pubkey>"
    exit 1
fi
export PATH=$HOME/.astro/bin:$HOME/.local/bin:$PATH
PUBKEY=$1

function urldecode() { : "${*//+/ }"; echo -e "${_//%/\\x}"; }

## UPASSPORT RELINKING
[[ -d $HOME/.zen/game/passport/${PUBKEY} ]] \
    && [[ ! -L ${MY_PATH}/pdf/${PUBKEY} ]] \
        && ln -s ~/.zen/game/passport/${PUBKEY} ${MY_PATH}/pdf/${PUBKEY}

if [[ -L ${MY_PATH}/pdf/${PUBKEY} ]]; then
    echo "BYE BYE UPASSPORT !"
    IPFSPORTAL=$(cat ${MY_PATH}/pdf/${PUBKEY}/IPFSPORTAL)
    ZEROCARD=$(cat ${MY_PATH}/pdf/${PUBKEY}/ZEROCARD)

    # UPLANETNAME Extract ZEROCARD secret
    cat ${MY_PATH}/pdf/${PUBKEY}/ssss.uplanet.asc \
        | gpg -d --passphrase "${UPLANETNAME}" --batch \
            > ${MY_PATH}/tmp/${MOATS}.ssss

    DISCO="$(cat ${MY_PATH}/tmp/${MOATS}.ssss | ssss-combine -t 2 -q 2>&1)"
    arr=(${DISCO//[=&]/ })
    salt=$(urldecode ${arr[1]} | xargs)
    pepper=$(urldecode ${arr[3]} | xargs)
    ${MY_PATH}/tools/keygen -t duniter -o ${MY_PATH}/tmp/${MOATS}.secret "$salt" "$pepper"

    # ZEROCARD amount
    solde=$(${MY_PATH}/tools/timeout.sh -t 5 ${MY_PATH}/tools/jaklis/jaklis.py balance -p ${ZEROCARD})
    echo "EMPTYING $solde G1 to ${PUBKEY}"
    # Pay Back
    ${MY_PATH}/tools/timeout.sh -t 5 ${MY_PATH}/tools/jaklis/jaklis.py -k ${MY_PATH}/tmp/${MOATS}.secret pay -a ${solde} -p ${PUBKEY} -c "UPASSPORT:UNPLUG:/ipfs/$IPFSPORTAL" -m
    [ $? -eq 0 ] \
        && rm -Rf ${MY_PATH}/pdf/${PUBKEY}/ && rm ${MY_PATH}/pdf/${PUBKEY} && rmdir ~/.zen/game/passport/${PUBKEY} \
            && echo "${MY_PATH}/static/img/nature_cloud_face.png" \
            || { echo "PAYMENT FAILED... retry needed"; echo "${MY_PATH}/static/img/money_coins.png"; }

    rm ${MY_PATH}/tmp/${MOATS}.secret

else


    echo "UNKNOWN ACCOUNT"

fi

exit 0
