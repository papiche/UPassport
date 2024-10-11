#!/bin/bash
################################################################################
# CREATE SHAMIR KEY - EXAMPLE
################################################################################
# Author: Fred (DsEx1pS33vzYZg4MroyBV9hCw98j1gtHEhwiZ5tK7ech)
# Version: 1.0
# License: AGPL-3.0 (https://choosealicense.com/licenses/agpl-3.0/)
################################################################################
MY_PATH="`dirname \"$0\"`"              # relative
MY_PATH="`( cd \"$MY_PATH\" && pwd )`"  # absolutized and normalized
ME="${0##*/}"

if [ $# -ne 1 ]; then
    echo "Usage: $0 <email>"
    exit 1
fi

EMAIL="$1"
[[ ! "${EMAIL}" =~ ^[a-zA-Z0-9.%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,4}$ ]] \
&& echo "BAD EMAIL ${EMAIL}" && exit 1

source ${MY_PATH}/.env
[[ -z $myDUNITER ]] && myDUNITER="https://g1.cgeek.fr" # DUNITER
[[ -z $myCESIUM ]] && myCESIUM="https://g1.data.e-is.pro" # CESIUM+
[[ -z $ipfsNODE ]] && ipfsNODE="http://127.0.0.1:8080" # IPFS

## CLEANING cards
rm -f ${MY_PATH}/cards/*
################################################################################
## CREATE A SHAMIR 2/3 KEY
## + IPNS incremental secret vault
## insert UPLANETNAME part in key generation
[ ! -s $HOME/.zen/Astroport.ONE/tools/my.sh ] && echo "ERROR/ Missing Astroport.ONE. Please install..." && exit 1
. "$HOME/.zen/Astroport.ONE/tools/my.sh"
################################################################################
## GENERATE STRONG RANDOM KEYS
MOATS=$(date -u +"%Y%m%d%H%M%S%4N")

prime=$(${MY_PATH}/tools/diceware.sh 1 | xargs)
SALT=$(tr -dc 'a-zA-Z0-9' < /dev/urandom | fold -w42 | head -n1)
echo ${prime} > ${MY_PATH}/cards/WORDS

second=$(${MY_PATH}/tools/diceware.sh 1 | xargs)
PEPPER=$(tr -dc 'a-zA-Z0-9' < /dev/urandom | fold -w42 | head -n1)
echo ${second} >> ${MY_PATH}/cards/WORDS

${MY_PATH}/tools/keygen -t duniter -o ${MY_PATH}/tmp/${MOATS}.zerocard.dunikey "${SALT}" "${PEPPER}"
G1PUB=$(cat ${MY_PATH}/tmp/${MOATS}.zerocard.dunikey  | grep 'pub:' | cut -d ' ' -f 2)

echo "${MY_PATH}/tools/natools.py encrypt -p $CAPTAING1PUB -i ${MY_PATH}/tmp/${MOATS}.zerocard.dunikey -o ${MY_PATH}/cards/zerocard.dunikey.enc"
rm ${MY_PATH}/cards/G1.captain.enc
${MY_PATH}/tools/natools.py encrypt -p $CAPTAING1PUB -i ${MY_PATH}/tmp/${MOATS}.zerocard.dunikey -o ${MY_PATH}/cards/G1.captain.enc

echo "SECURED G1 _WALLET: $G1PUB"
echo "${MY_PATH}/cards/G1.captain.enc (CAPTAING1PUB)*"

amzqr "${G1PUB}:ZEN" -l H -p ${MY_PATH}/static/img/zen1.png -c -n ZEN_${G1PUB}.QR.png -d ${MY_PATH}/tmp/ 2>/dev/null
# Write G1PUB at the bottom
convert ${MY_PATH}/tmp/ZEN_${G1PUB}.QR.png \
        -gravity SouthWest \
        -pointsize 18 \
        -fill black \
        -annotate +2+2 "ZEN:${G1PUB}:ZEN" \
        -annotate +1+3 "ZEN:${G1PUB}:ZEN" \
        ${MY_PATH}/cards/ZEN_${G1PUB}.QR.png

## CREATE IPNS KEY
${MY_PATH}/tools/keygen -t ipfs -o ${MY_PATH}/tmp/${MOATS}.zerocard.ipns "${SALT}" "${PEPPER}"
ipfs key rm "SSSS_${EMAIL}" > /dev/null 2>&1
WALLETNS=$(ipfs key import "SSSS_${EMAIL}" -f pem-pkcs8-cleartext ${MY_PATH}/tmp/${MOATS}.zerocard.ipns)
echo "SSSS_${EMAIL} STORAGE: /ipns/$WALLETNS"

## ENCRYPT WITH UPLANETNAME PASSWORD
if [[ ! -z ${UPLANETNAME} ]]; then
    cat ${MY_PATH}/tmp/${MOATS}.zerocard.ipns \
        | gpg --symmetric --armor --batch --passphrase "${UPLANETNAME}" \
            -o ${MY_PATH}/cards/IPNS.uplanet.asc
fi

## Create /ipns/$WALLETNS QR Code
amzqr "${ipfsNODE}/ipns/$WALLETNS" -l H -p ${MY_PATH}/static/img/moa_net.png -c -n IPNS.QR.png -d ${MY_PATH}/tmp/ 2>/dev/null
echo "${ipfsNODE}/ipns/$WALLETNS" > ${MY_PATH}/cards/IPNS
convert ${MY_PATH}/tmp/IPNS.QR.png \
        -gravity SouthWest \
        -pointsize 18 \
        -fill black \
        -annotate +2+2 "${ipfsNODE}/ipns/$WALLETNS" \
        -annotate +1+3 "${ipfsNODE}/ipns/$WALLETNS" \
        ${MY_PATH}/cards/IPNS.QR.png
#######################################################################
## PREPARE DISCO SECRET
DISCO="/?${prime}=${SALT}&${second}=${PEPPER}"
echo "SOURCE : "$DISCO

## ssss-split : Keep 2 needed over 3
echo "$DISCO" | ssss-split -t 2 -n 3 -q > ${MY_PATH}/tmp/${G1PUB}.ssss
HEAD=$(cat ${MY_PATH}/tmp/${G1PUB}.ssss | head -n 1) && echo "$HEAD" > ${MY_PATH}/tmp/${G1PUB}.ssss.head
MIDDLE=$(cat ${MY_PATH}/tmp/${G1PUB}.ssss | head -n 2 | tail -n 1) && echo "$MIDDLE" > ${MY_PATH}/tmp/${G1PUB}.ssss.mid
TAIL=$(cat ${MY_PATH}/tmp/${G1PUB}.ssss | tail -n 1) && echo "$TAIL" > ${MY_PATH}/tmp/${G1PUB}.ssss.tail
#~ echo "TEST DECODING..."
#~ echo "$HEAD
#~ $TAIL" | ssss-combine -t 2 -q

## encrypt tail with captain key
echo "${MY_PATH}/tools/natools.py encrypt -p ${CAPTAING1PUB} -i ${MY_PATH}/tmp/${G1PUB}.ssss.tail -o ${MY_PATH}/cards/ssss.tail.captain.enc"
${MY_PATH}/tools/natools.py encrypt -p ${CAPTAING1PUB} -i ${MY_PATH}/tmp/${G1PUB}.ssss.tail -o ${MY_PATH}/cards/ssss.tail.captain.enc

## Hash HEAD (sha256)
ZKEY1H=$(echo "$HEAD" | sha256sum  | cut -f 1 -d ' ')
echo "$ZKEY1H" > ${MY_PATH}/cards/sss.head.hash.txt

## make HEAD QR Code
amzqr "$HEAD" -l H -p ${MY_PATH}/static/img/key.png -c -n _KEY1.QR.png -d ${MY_PATH}/tmp/ 2>/dev/null
convert ${MY_PATH}/tmp/_KEY1.QR.png \
        -gravity SouthWest \
        -pointsize 18 \
        -fill black \
        -annotate +2+2 "$HEAD" \
        -annotate +1+3 "$HEAD" \
        ${MY_PATH}/cards/_KEY1.QR.png

## REMOVE SSSS
rm ${MY_PATH}/tmp/${G1PUB}.ssss*

echo

ZCARDIPFS=$(ipfs add -qw ${MY_PATH}/cards/* | tail -n 1)
ipfs pin rm $ZCARDIPFS

amzqr "https://opencollective.com/uplanet-zero" -l H -p ${MY_PATH}/static/img/zenplanet.png -c -n OC.png -d ${MY_PATH}/tmp/ 2>/dev/null
convert ${MY_PATH}/tmp/OC.png \
        -gravity SouthWest \
        -pointsize 18 \
        -fill black \
        -annotate +2+2 "https://opencollective.com/uplanet-zero" \
        -annotate +1+3 "https://opencollective.com/uplanet-zero" \
        ${MY_PATH}/cards/OC.png
#######################################################################
echo "Create Sagittarius Passport"

## Add Images to ipfs
ZWALLET=$(ipfs add -q ${MY_PATH}/cards/ZEN_${G1PUB}.QR.png)
ZIPNS=$(ipfs add -q ${MY_PATH}/cards/IPNS.QR.png)
ZKEY1QR=$(ipfs add -q ${MY_PATH}/cards/_KEY1.QR.png)
ZOC=$(ipfs add -q ${MY_PATH}/cards/OC.png)

LAT=""
LON=""

cat ${MY_PATH}/static/zine/zencard.html \
    | sed -e "s~QmTL7VDgkYjpYC2qiiFCfah2pSqDMkTANMeMtjMndwXq9y~${ZIPNS}~g" \
            -e "s~QmexZHwUuZdFLZuHt1PZunjC7c7rTFKRWJDASGPTyrqysP/page3.png~${ZKEY1QR}~g" \
            -e "s~QmZHV5QppQX9N7MS1GFMqzmnRU5vLbpmQ1UkSRY5K5LfA9/page_.png~${ZOC}~g" \
            -e "s~QmdmeZhD8ncBFptmD5VSJoszmu41edtT265Xq3HVh8PhZP~${ZWALLET}~g" \
            -e "s~_WALLETNS_~${WALLETNS}~g" \
            -e "s~_PLAYER_~${EMAIL}~g" \
            -e "s~_G1PUB_~${G1PUB}~g" \
            -e "s~_PUBKEY_~${G1PUB}~g" \
            -e "s~_DATE_~$(date -u)~g" \
            -e "s~_IPFS_~/ipfs/${ZCARDIPFS}~g" \
            -e "s~_LAT_~${LAT}~g" \
            -e "s~_LON_~${LON}~g" \
            -e "s~https://ipfs.copylaradio.com~http://127.0.0.1:8080~g" \
        > ${MY_PATH}/cards/ZENCARD.html

xdg-open ${MY_PATH}/cards/ZENCARD.html
