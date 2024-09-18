#!/bin/bash
################################################################################
# CREATE SHAMIR KEY
################################################################################
# Author: Fred (DsEx1pS33vzYZg4MroyBV9hCw98j1gtHEhwiZ5tK7ech)
# Version: 1.0
# License: AGPL-3.0 (https://choosealicense.com/licenses/agpl-3.0/)
################################################################################
MY_PATH="`dirname \"$0\"`"              # relative
MY_PATH="`( cd \"$MY_PATH\" && pwd )`"  # absolutized and normalized

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
[ ! -s ~/.zen/Astroport.ONE/tools/my.sh ] && echo "Missing Astroport.ONE. Please install..." && exit 1
. "$HOME/.zen/Astroport.ONE/tools/my.sh"
################################################################################
## GENERATE STRONG RANDOM KEYS
MOATS=$(date -u +"%Y%m%d%H%M%S%4N")

prime=$(${MY_PATH}/tools/diceware.sh 1 | xargs)
SALT=$(tr -dc 'a-zA-Z0-9' < /dev/urandom | fold -w42 | head -n1)

second=$(${MY_PATH}/tools/diceware.sh 1 | xargs)
PEPPER=$(tr -dc 'a-zA-Z0-9' < /dev/urandom | fold -w42 | head -n1)

${MY_PATH}/tools/keygen -t duniter -o ./tmp/${MOATS}.zwallet.dunikey "${SALT}" "${PEPPER}"
G1PUB=$(cat ./tmp/${MOATS}.zwallet.dunikey  | grep 'pub:' | cut -d ' ' -f 2)

rm -f ./scards/${NODEG1PUB}/zwallet.dunikey.enc
${MY_PATH}/tools/natools.py encrypt -p $NODEG1PUB -i ./tmp/${MOATS}.zwallet.dunikey -o ./scards/${NODEG1PUB}/zwallet.dunikey.enc

echo "SECURED G1 _WALLET: $G1PUB"

amzqr "${G1PUB}" -l H -p ./static/img/zenticket.png -c -n _${G1PUB}.QR.png -d ./tmp/ 2>/dev/null
# Write G1PUB at the bottom
convert ./tmp/_${G1PUB}.QR.png \
        -gravity SouthWest \
        -pointsize 18 \
        -fill black \
        -annotate +5+5 "${G1PUB}" \
        ./scards/${NODEG1PUB}/_${G1PUB}.QR.png

## CREATE IPNS KEY
${MY_PATH}/tools/keygen -t ipfs -o ./tmp/${NODEG1PUB}.zwallet.ipns "${SALT}" "${PEPPER}"
ipfs key rm ${NODEG1PUB} > /dev/null 2>&1
WALLETNS=$(ipfs key import ${NODEG1PUB} -f pem-pkcs8-cleartext ./tmp/${NODEG1PUB}.zwallet.ipns)
echo "_WALLET STORAGE: /ipns/$WALLETNS"

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

${MY_PATH}/tools/natools.py encrypt -p $NODEG1PUB -i ./tmp/${G1PUB}.ssss -o ./scards/${NODEG1PUB}/ssss.enc

rm ./tmp/${G1PUB}.ssss

echo

#######################################################################
echo "Create Zine Passport"

## Add Images to ipfs
MEMBERQRIPFS=$(ipfs add -q ./scards/${NODEG1PUB}/${NODEG1PUB}.UID.png)
FULLCERT=$(ipfs add -q ./scards/${NODEG1PUB}/P2P.png)
[ -s ./scards/${NODEG1PUB}/P21.png ] \
    && CERTIN=$(ipfs add -q ./scards/${NODEG1PUB}/P21.png) \
    || CERTIN="QmReCfHszucv2Ra9zbKjKwmgoJ4krWpqB12TDK5AR9PKCQ/page3.png"
[ -s ./scards/${NODEG1PUB}/P21.png ] \
    && CERTOUT=$(ipfs add -q ./scards/${NODEG1PUB}/12P.png) \
    || CERTOUT="QmReCfHszucv2Ra9zbKjKwmgoJ4krWpqB12TDK5AR9PKCQ/page4.png"

ZWALLET=$(ipfs add -q ./scards/${NODEG1PUB}/_${G1PUB}.QR.png)

LAT=$(cat ./tmp/${NODEG1PUB}.cesium.json | jq -r '._source.geoPoint.lat')
LAT=$(makecoord $LAT)
LON=$(cat ./tmp/${NODEG1PUB}.cesium.json | jq -r '._source.geoPoint.lon')
LON=$(makecoord $LON)

cat ./zine/index.html \
    | sed -e "s~QmTL7VDgkYjpYC2qiiFCfah2pSqDMkTANMeMtjMndwXq9y~${MEMBERQRIPFS}~g" \
            -e "s~QmReCfHszucv2Ra9zbKjKwmgoJ4krWpqB12TDK5AR9PKCQ/page2.png~${FULLCERT}~g" \
            -e "s~QmReCfHszucv2Ra9zbKjKwmgoJ4krWpqB12TDK5AR9PKCQ/page3.png~${CERTIN}~g" \
            -e "s~QmReCfHszucv2Ra9zbKjKwmgoJ4krWpqB12TDK5AR9PKCQ/page4.png~${CERTOUT}~g" \
            -e "s~QmdmeZhD8ncBFptmD5VSJoszmu41edtT265Xq3HVh8PhZP~${ZWALLET}~g" \
            -e "s~_WALLETNS_~${WALLETNS}~g" \
            -e "s~_PLAYER_~${MEMBERUID}~g" \
            -e "s~_NODEG1PUB_~${NODEG1PUB}~g" \
            -e "s~_G1PUB_~${G1PUB}~g" \
            -e "s~_LAT_~${LAT}~g" \
            -e "s~_LON_~${LON}~g" \
            -e "s~https://ipfs.copylaradio.com~http://127.0.0.1:8080~g" \
        > ./scards/${NODEG1PUB}/PASSPORT.${MEMBERUID}.html

xdg-open ./scards/${NODEG1PUB}/PASSPORT.${MEMBERUID}.html
