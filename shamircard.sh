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

source ./.env
[[ -z $myDUNITER ]] && myDUNITER="https://g1.cgeek.fr" # DUNITER
[[ -z $myCESIUM ]] && myCESIUM="https://g1.data.e-is.pro" # CESIUM+
[[ -z $ipfsNODE ]] && ipfsNODE="${ipfsNODE}" # IPFS

## CLEANING cards
rm -f ./cards/*
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
echo ${prime} > ./cards/WORDS

second=$(./tools/diceware.sh 1 | xargs)
PEPPER=$(tr -dc 'a-zA-Z0-9' < /dev/urandom | fold -w42 | head -n1)
echo ${second} >> ./cards/WORDS

./tools/keygen -t duniter -o ./tmp/${MOATS}.zwallet.dunikey "${SALT}" "${PEPPER}"
G1PUB=$(cat ./tmp/${MOATS}.zwallet.dunikey  | grep 'pub:' | cut -d ' ' -f 2)

echo "./tools/natools.py encrypt -p $CAPTAING1PUB -i ./tmp/${MOATS}.zwallet.dunikey -o ./cards/zwallet.dunikey.enc"
rm ./cards/G1.captain.enc
./tools/natools.py encrypt -p $CAPTAING1PUB -i ./tmp/${MOATS}.zwallet.dunikey -o ./cards/G1.captain.enc

echo "SECURED G1 _WALLET: $G1PUB"
echo "./cards/G1.captain.enc (CAPTAING1PUB)*"

amzqr "${G1PUB}:ZEN" -l H -p ./static/img/zen1.png -c -n ZEN_${G1PUB}.QR.png -d ./tmp/ 2>/dev/null
# Write G1PUB at the bottom
convert ./tmp/ZEN_${G1PUB}.QR.png \
        -gravity SouthWest \
        -pointsize 18 \
        -fill black \
        -annotate +2+2 "ZEN:${G1PUB}:ZEN" \
        -annotate +1+3 "ZEN:${G1PUB}:ZEN" \
        ./cards/ZEN_${G1PUB}.QR.png

## CREATE IPNS KEY
./tools/keygen -t ipfs -o ./tmp/${MOATS}.zwallet.ipns "${SALT}" "${PEPPER}"
ipfs key rm "SSSS_${EMAIL}" > /dev/null 2>&1
WALLETNS=$(ipfs key import "SSSS_${EMAIL}" -f pem-pkcs8-cleartext ./tmp/${MOATS}.zwallet.ipns)
echo "SSSS_${EMAIL} STORAGE: /ipns/$WALLETNS"

## ENCRYPT WITH UPLANETNAME PASSWORD
if [[ ! -z ${UPLANETNAME} ]]; then
    cat ./tmp/${MOATS}.zwallet.ipns \
        | gpg --symmetric --armor --batch --passphrase "${UPLANETNAME}" \
            -o ./cards/IPNS.uplanet.asc
fi

## Create /ipns/$WALLETNS QR Code
amzqr "${ipfsNODE}/ipns/$WALLETNS" -l H -p ./static/img/moa_net.png -c -n IPNS.QR.png -d ./tmp/ 2>/dev/null
echo "${ipfsNODE}/ipns/$WALLETNS" > ./cards/IPNS
convert ./tmp/IPNS.QR.png \
        -gravity SouthWest \
        -pointsize 18 \
        -fill black \
        -annotate +2+2 "${ipfsNODE}/ipns/$WALLETNS" \
        -annotate +1+3 "${ipfsNODE}/ipns/$WALLETNS" \
        ./cards/IPNS.QR.png
#######################################################################
## PREPARE DISCO SECRET
DISCO="/?${prime}=${SALT}&${second}=${PEPPER}"
echo "SOURCE : "$DISCO

## ssss-split : Keep 2 needed over 3
echo "$DISCO" | ssss-split -t 2 -n 3 -q > ./tmp/${G1PUB}.ssss
HEAD=$(cat ./tmp/${G1PUB}.ssss | head -n 1) && echo "$HEAD" > ./tmp/${G1PUB}.ssss.head
MIDDLE=$(cat ./tmp/${G1PUB}.ssss | head -n 2 | tail -n 1) && echo "$MIDDLE" > ./tmp/${G1PUB}.ssss.mid
TAIL=$(cat ./tmp/${G1PUB}.ssss | tail -n 1) && echo "$TAIL" > ./tmp/${G1PUB}.ssss.tail
#~ echo "TEST DECODING..."
#~ echo "$HEAD
#~ $TAIL" | ssss-combine -t 2 -q

## encrypt tail with captain key
echo "./tools/natools.py encrypt -p ${CAPTAING1PUB} -i ./tmp/${G1PUB}.ssss.tail -o ./cards/ssss.tail.captain.enc"
./tools/natools.py encrypt -p ${CAPTAING1PUB} -i ./tmp/${G1PUB}.ssss.tail -o ./cards/ssss.tail.captain.enc

## Hash HEAD (sha256)
ZKEY1H=$(echo "$HEAD" | sha256sum  | cut -f 1 -d ' ')
echo "$ZKEY1H" > ./cards/sss.head.hash.txt

## make HEAD QR Code
amzqr "$HEAD" -l H -p ./static/img/key.png -c -n _KEY1.QR.png -d ./tmp/ 2>/dev/null
convert ./tmp/_KEY1.QR.png \
        -gravity SouthWest \
        -pointsize 18 \
        -fill black \
        -annotate +2+2 "$HEAD" \
        -annotate +1+3 "$HEAD" \
        ./cards/_KEY1.QR.png

## REMOVE SSSS
rm ./tmp/${G1PUB}.ssss*

echo

ZCARDIPFS=$(ipfs add -qw ./cards/* | tail -n 1)
ipfs pin rm $ZCARDIPFS

amzqr "https://opencollective.com/uplanet-zero" -l H -p ./static/img/zenplanet.png -c -n OC.png -d ./tmp/ 2>/dev/null
convert ./tmp/OC.png \
        -gravity SouthWest \
        -pointsize 18 \
        -fill black \
        -annotate +2+2 "https://opencollective.com/uplanet-zero" \
        -annotate +1+3 "https://opencollective.com/uplanet-zero" \
        ./cards/OC.png
#######################################################################
echo "Create Sagittarius Passport"

## Add Images to ipfs
ZWALLET=$(ipfs add -q ./cards/ZEN_${G1PUB}.QR.png)
ZIPNS=$(ipfs add -q ./cards/IPNS.QR.png)
ZKEY1QR=$(ipfs add -q ./cards/_KEY1.QR.png)
ZOC=$(ipfs add -q ./cards/OC.png)

LAT=""
LON=""

cat ./static/zine/zencard.html \
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
        > ./cards/ZENCARD.html

xdg-open ./cards/ZENCARD.html
