#!/bin/bash
################################################################################
# CREATE SHAMIR KEY
################################################################################
# Author: Fred (DsEx1pS33vzYZg4MroyBV9hCw98j1gtHEhwiZ5tK7ech)
# Version: 1.0
# License: AGPL-3.0 (https://choosealicense.com/licenses/agpl-3.0/)
################################################################################
if [ $# -ne 1 ]; then
    echo "Usage: $0 <email>"
    exit 1
fi

EMAIL="$1"
[[ ! "${EMAIL}" =~ ^[a-zA-Z0-9.%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,4}$ ]] \
&& echo "BAD EMAIL ${EMAIL}" && exit 1

################################################################################
## CREATE A SHAMIR 2/3 KEY
## + IPNS incremental secret vault
## insert UPLANETNAME part in key generation
[ ! -s ~/.zen/Astroport.ONE/tools/my.sh ] && echo "ERROR/ Missing Astroport.ONE. Please install..." && exit 1
. "$HOME/.zen/Astroport.ONE/tools/my.sh"
################################################################################
## GENERATE STRONG RANDOM KEYS
MOATS=$(date -u +"%Y%m%d%H%M%S%4N")

prime=$(./tools/diceware.sh 1 | xargs)
SALT=$(tr -dc 'a-zA-Z0-9' < /dev/urandom | fold -w42 | head -n1)
echo ${prime} > ./cards/CHK

second=$(./tools/diceware.sh 1 | xargs)
PEPPER=$(tr -dc 'a-zA-Z0-9' < /dev/urandom | fold -w42 | head -n1)
echo ${second} > ./cards/CHK

./tools/keygen -t duniter -o ./tmp/${MOATS}.zwallet.dunikey "${SALT}" "${PEPPER}"
G1PUB=$(cat ./tmp/${MOATS}.zwallet.dunikey  | grep 'pub:' | cut -d ' ' -f 2)

echo "./tools/natools.py encrypt -p $CAPTAING1PUB -i ./tmp/${MOATS}.zwallet.dunikey -o ./cards/zwallet.dunikey.enc"
rm ./cards/zwallet.dunikey.enc
./tools/natools.py encrypt -p $CAPTAING1PUB -i ./tmp/${MOATS}.zwallet.dunikey -o ./cards/zwallet.dunikey.enc

echo "SECURED G1 _WALLET: $G1PUB"
echo "./cards/zwallet.dunikey.enc (CAPTAING1PUB)*"

amzqr "${G1PUB}" -l H -p ./static/img/zen1.png -c -n _${G1PUB}.QR.png -d ./tmp/ 2>/dev/null
# Write G1PUB at the bottom
convert ./tmp/_${G1PUB}.QR.png \
        -gravity SouthWest \
        -pointsize 18 \
        -fill black \
        -annotate +5+5 "${G1PUB}" \
        ./cards/_${G1PUB}.QR.png

## CREATE IPNS KEY
./tools/keygen -t ipfs -o ./tmp/${MOATS}.zwallet.ipns "${SALT}" "${PEPPER}"
ipfs key rm "SSSS_${EMAIL}" > /dev/null 2>&1
WALLETNS=$(ipfs key import "SSSS_${EMAIL}" -f pem-pkcs8-cleartext ./tmp/${MOATS}.zwallet.ipns)
echo "SSSS_${EMAIL} STORAGE: /ipns/$WALLETNS"

## Create /ipns/$WALLETNS QR Code
amzqr "https://ipfs.astroport.com/ipns/$WALLETNS" -l H -p ./static/img/astroport.png -c -n _${WALLETNS}.QR.png -d ./cards/ 2>/dev/null

#######################################################################
## PREPARE DISCO SECRET
DISCO="/?${prime}=${SALT}&${second}=${PEPPER}"
echo "SOURCE : "$DISCO

## ssss-split : Keep 2 needed over 3
echo "$DISCO" | ssss-split -t 2 -n 3 -q > ./tmp/${G1PUB}.ssss
HEAD=$(cat ./tmp/${G1PUB}.ssss | head -n 1) && echo "$HEAD"
MIDDLE=$(cat ./tmp/${G1PUB}.ssss | head -n 2 | tail -n 1) && echo "$MIDDLE"
TAIL=$(cat ./tmp/${G1PUB}.ssss | tail -n 1) && echo "$TAIL"
echo "TEST DECODING..."
echo "$HEAD
$TAIL" | ssss-combine -t 2 -q

echo "./tools/natools.py encrypt -p ${CAPTAING1PUB} -i ./tmp/${G1PUB}.ssss -o ./cards/ssss.enc"
./tools/natools.py encrypt -p ${CAPTAING1PUB} -i ./tmp/${G1PUB}.ssss -o ./cards/ssss.enc

## ENCRYPT WITH UPLANETNAME
if [[ ! -z ${UPLANETNAME} ]]; then
    cat ./cards/ssss.enc | gpg --symmetric --armor --batch --passphrase "${UPLANETNAME}" -o ./cards/ssss.enc.asc
    cat ./cards/ssss.enc.asc | gpg -d --passphrase "${UPLANETNAME}" --batch > ./cards/ssss.enc.test
    [[ $(diff -q ./cards/ssss.enc.test ./cards/ssss.enc.ssss) != "" ]] && echo "ERROR: GPG ENCRYPTION FAILED "
    rm ./cards/ssss.enc.test
fi

## Create HEAD QR Code
amzqr "$HEAD" -l H -p ./static/img/serrure.png -c -n _KEY1.QR.png -d ./cards/ 2>/dev/null


## REMOVE SSSS
rm ./tmp/${G1PUB}.ssss

echo

#######################################################################
echo "Create Zine Passport"

## Add Images to ipfs
ZWALLET=$(ipfs add -q ./cards/_${G1PUB}.QR.png)
ZIPNS=$(ipfs add -q ./cards/_${WALLETNS}.QR.png)
ZKEY1=$(ipfs add -q ./cards/_KEY1.QR.png)

LAT=""
LON=""

cat ./zine/index.html \
    | sed -e "s~QmTL7VDgkYjpYC2qiiFCfah2pSqDMkTANMeMtjMndwXq9y~${ZIPNS}~g" \
            -e "s~QmU43PSABthVtM8nWEWVDN1ojBBx36KLV5ZSYzkW97NKC3/page1.png~QmV45AUVq8SexwiEt66iGZyZSTbz6gE5xaWjj9wF7qnEpa/sagittarius_page1.jpg~g" \
            -e "s~QmexZHwUuZdFLZuHt1PZunjC7c7rTFKRWJDASGPTyrqysP/page3.png~${ZKEY1}~g" \
            -e "s~QmdmeZhD8ncBFptmD5VSJoszmu41edtT265Xq3HVh8PhZP~${ZWALLET}~g" \
            -e "s~_WALLETNS_~${WALLETNS}~g" \
            -e "s~_PLAYER_~${EMAIL}~g" \
            -e "s~_G1PUB_~${G1PUB}~g" \
            -e "s~_PUBKEY_~${G1PUB}~g" \
            -e "s~_LAT_~${LAT}~g" \
            -e "s~_LON_~${LON}~g" \
            -e "s~https://ipfs.copylaradio.com~http://127.0.0.1:8080~g" \
        > ./cards/ZINE.html

xdg-open ./cards/ZINE.html
