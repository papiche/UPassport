#!/bin/bash
################################################################################
# Author: Fred (DsEx1pS33vzYZg4MroyBV9hCw98j1gtHEhwiZ5tK7ech)
# Version: 1.0
# License: AGPL-3.0 (https://choosealicense.com/licenses/agpl-3.0/)
################################################################################
MY_PATH="`dirname \"$0\"`"              # relative
MY_PATH="`( cd \"$MY_PATH\" && pwd )`"  # absolutized and normalized
if [ $# -ne 1 ]; then
    echo "Usage: $0 <pubkey>"
    exit 1
fi

myDUNITER="https://g1.cgeek.fr"
myCESIUM="https://g1.data.e-is.pro"

PUBKEY=$1

mkdir -p $MY_PATH/tmp
# Delete older than 1 day cache
find $MY_PATH/tmp -mtime +1 -type f -exec rm '{}' \;

[[ ! -s ./tmp/$PUBKEY.me.json ]] \
&& wget -q -O ./tmp/$PUBKEY.me.json ${myDUNITER}/wot/lookup/$PUBKEY

if [ ! -s "./tmp/$PUBKEY.me.json" ]; then
    echo "Invalid PUBKEY: $PUBKEY.me.json"
    exit 1
fi

# GET MEMBER UID
G1PUB=${PUBKEY}
MEMBERUID=$(cat ./tmp/$PUBKEY.me.json | jq -r '.results[].uids[].uid')
mkdir -p $MY_PATH/pdf/${PUBKEY}/N1

function makecoord() {
    local input="$1"

    input=$(echo "${input}" | sed 's/\([0-9]*\.[0-9]\{2\}\).*/\1/')  # Ensure has exactly two decimal places

    if [[ ${input} =~ ^-?[0-9]+\.[0-9]$ ]]; then
        input="${input}0"
    elif [[ ${input} =~ ^\.[0-9]+$ ]]; then
        input="0${input}"
    elif [[ ${input} =~ ^-?[0-9]+\.$ ]]; then
        input="${input}00"
    elif [[ ${input} =~ ^-?[0-9]+$ ]]; then
        input="${input}.00"
    fi
    echo "${input}"
}

# Function to get Cesium+ profile, generate QR code, and annotate with UID
generate_qr_with_uid() {
    local pubkey=$1
    local member_uid=$2

    if [ ! -s ./tmp/${pubkey}.UID.png ]; then

        echo "GET CESIUM+ PROFILE ${pubkey} ${member_uid}"
        [[ ! -s ./tmp/$pubkey.cesium.json ]] \
        && ${MY_PATH}/tools/timeout.sh -t 20 \
        curl -s ${myCESIUM}/user/profile/${pubkey} > ./tmp/${pubkey}.cesium.json 2>/dev/null

        [ ! -s "./tmp/$pubkey.cesium.json" ] && echo "xxxxx ERROR PROBLEM WITH CESIUM+ NODE ${myCESIUM} xxxxx"

        # Extract png from json
        zlat=$(cat ./tmp/${pubkey}.cesium.json | jq -r '._source.geoPoint.lat')
        ulat=$(makecoord $zlat)
        zlon=$(cat ./tmp/${pubkey}.cesium.json | jq -r '._source.geoPoint.lon')
        ulon=$(makecoord $zlon)

        cat ./tmp/${pubkey}.cesium.json | jq -r '._source.avatar._content' | base64 -d > ./tmp/${pubkey}.png

        # Resize avatar picure & add transparent canvas
        convert ./tmp/${pubkey}.png \
          -resize 120x120 \
          -bordercolor white \
          -border 120x120 \
          -background none \
          -transparent white \
          ./tmp/${pubkey}.small.png

        # Create QR Code with Cesium+ picture in
        [ -s ./tmp/${pubkey}.small.png ] \
            && amzqr "${pubkey}" -l H -p ./tmp/${pubkey}.small.png -c -n ${pubkey}.QR.png -d ./tmp/ 2>/dev/null
        [ ! -s ./tmp/${pubkey}.QR.png ] \
            && amzqr "${pubkey}" -l H -n ${pubkey}.QR.png -d ./tmp/

        # Write UID at the bottom
        convert ./tmp/${pubkey}.QR.png \
          -gravity SouthWest \
          -pointsize 25 \
          -fill black \
          -annotate +5+5 "${member_uid} : $ulat / $ulon" \
          ./tmp/${pubkey}.UID.png

        [[ -s ./tmp/${pubkey}.UID.png ]] && rm ./tmp/${pubkey}.QR.png
    fi
}

## PERFORM PUBKEY TX HISTORY
./tools/timeout.sh -t 20 ${MY_PATH}/tools/jaklis/jaklis.py history -n 40 -p ${PUBKEY} -j > ./tmp/$PUBKEY.TX.json
## VERIFY TX FOR "MEMBERUID" IN COMMENT
## FIND PAYMENT DESTINATION...
## FIND RX FROM PD EXTRACT COMMENT
## GET IPNS+


## CHECK IF IPNS KEY WITH PUBKEY EXISTS
WIPNS=$(ipfs key list -l | grep ${PUBKEY} | cut -f 1 -d ' ')
if [ ! -z $WIPNS ]; then
    echo "EXISTING PASSPORT: /ipns/$WIPNS"
    echo "CANCEL : Ctrl+C / OK : Enter"
    read
fi

# Call the function with PUBKEY and MEMBERUID
generate_qr_with_uid "$PUBKEY" "$MEMBERUID"
mv ./tmp/${PUBKEY}.UID.png ./pdf/${PUBKEY}/${PUBKEY}.UID.png

########################################"
# Extract the uids and pubkeys into a bash array
certbyme=$(jq -r '.results[].signed[] | [.uid, .pubkey] | @tsv' ./tmp/$PUBKEY.me.json)
# Initialize a bash array
declare -A certout

# Populate the array
while IFS=$'\t' read -r uid pubkey; do
  certout["$uid"]="$pubkey"
done <<< "$certbyme"

echo "Pubkey Certifiés :"
# Access the values (example: print all uids and their corresponding pubkeys)
for uid in "${!certout[@]}"; do
  echo "UID: $uid, PubKey: ${certout[$uid]}"
done

[[ ! -s ./tmp/$PUBKEY.them.json ]] \
&& wget -q -O ./tmp/$PUBKEY.them.json "${myDUNITER}/wot/certifiers-of/$PUBKEY?pubkey=true"

# Extract the uids and pubkeys into a bash array
certbythem=$(jq -r '.certifications[] | [.uid, .pubkey] | @tsv' ./tmp/$PUBKEY.them.json)
# Initialize a bash array
declare -A certin

# Populate the array
while IFS=$'\t' read -r uid pubkey; do
  certin["$uid"]="$pubkey"
done <<< "$certbythem"

echo "Pubkey certifiantes :"
# Access the values (example: print all uids and their corresponding pubkeys)
for uid in "${!certin[@]}"; do
  echo "UID: $uid, PubKey: ${certin[$uid]}"
done

# Compare the arrays for matches and differences
echo "Comparaison des certificats:"
echo "========================================================"
echo "UIDs présents dans certin et certout (matchs) :"
for uid in "${!certin[@]}"; do
  if [[ -n "${certout[$uid]}" ]]; then
    echo "UID: $uid, PubKey certifié: ${certout[$uid]}, PubKey certifiant: ${certin[$uid]}"

    # make friends QR
    [[ ! -s ./pdf/${PUBKEY}/N1/${certout[$uid]}.UID.png ]] \
        && generate_qr_with_uid "${certout[$uid]}" "$uid" \
        && mv ./tmp/${certout[$uid]}.UID.png ./pdf/${PUBKEY}/N1/ \
        && sleep 3
  fi
done

echo "========================================================"
echo "UIDs présents uniquement dans certin (certificateurs uniquement) :"
for uid in "${!certin[@]}"; do
  if [[ -z "${certout[$uid]}" ]]; then
    echo "UID: $uid, PubKey certifiant: ${certin[$uid]}"

    # make certin only QR
    [[ ! -s ./pdf/${PUBKEY}/N1/${certin[$uid]}.certin.png ]] \
        && generate_qr_with_uid "${certin[$uid]}" "$uid" \
        && mv ./tmp/${certin[$uid]}.UID.png ./pdf/${PUBKEY}/N1/${certin[$uid]}.certin.png \
        && sleep 3
  fi
done

echo "========================================================"
echo "UIDs présents uniquement dans certout (certifiés uniquement) :"
for uid in "${!certout[@]}"; do
  if [[ -z "${certin[$uid]}" ]]; then
    echo "UID: $uid, PubKey certifié: ${certout[$uid]}"
    # make certout only QR
    [[ ! -s ./pdf/${PUBKEY}/N1/${certout[$uid]}.certout.png ]] \
        && generate_qr_with_uid "${certout[$uid]}" "$uid" \
        && mv ./tmp/${certout[$uid]}.UID.png ./pdf/${PUBKEY}/N1/${certout[$uid]}.certout.png \
        && sleep 3
  fi
done



## Moving Related UID into ./pdf/${PUBKEY}/N1/
nb_fichiers=$(ls ./pdf/${PUBKEY}/N1/*.UID.png | wc -l)
montage -mode concatenate -geometry +20x20 -tile $(echo "scale=0; $nb_fichiers / sqrt($nb_fichiers) - 1" | bc)x$(echo "scale=0; sqrt($nb_fichiers) + 3" | bc) -density 300 ./pdf/${PUBKEY}/N1/*.UID.png ./pdf/${PUBKEY}/P2P.${PUBKEY}.pdf
convert -density 300 ./pdf/${PUBKEY}/P2P.${PUBKEY}.pdf -resize 375x550 ./pdf/${PUBKEY}/P2P.png

nb_fichiers=$(ls ./pdf/${PUBKEY}/N1/*.certin.png | wc -l)
montage -mode concatenate -geometry +20x20 -tile $(echo "scale=0; $nb_fichiers / sqrt($nb_fichiers) - 1" | bc)x$(echo "scale=0; sqrt($nb_fichiers) + 3" | bc) -density 300 ./pdf/${PUBKEY}/N1/*.certin.png ./pdf/${PUBKEY}/P21.${PUBKEY}.pdf
convert -density 300 ./pdf/${PUBKEY}/P21.${PUBKEY}.pdf -resize 375x550 ./pdf/${PUBKEY}/P21.png

nb_fichiers=$(ls ./pdf/${PUBKEY}/N1/*.certout.png | wc -l)
montage -mode concatenate -geometry +20x20 -tile $(echo "scale=0; $nb_fichiers / sqrt($nb_fichiers) - 1" | bc)x$(echo "scale=0; sqrt($nb_fichiers) + 3" | bc) -density 300 ./pdf/${PUBKEY}/N1/*.certout.png ./pdf/${PUBKEY}/12P.${PUBKEY}.pdf
convert -density 300 ./pdf/${PUBKEY}/12P.${PUBKEY}.pdf -resize 375x550 ./pdf/${PUBKEY}/12P.png

################################################################################
# CREATE SHAMIR KEY

prime=$(${MY_PATH}/tools/diceware.sh 1 | xargs)
SALT=$(tr -dc 'a-zA-Z0-9' < /dev/urandom | fold -w42 | head -n1)

second=$(${MY_PATH}/tools/diceware.sh 1 | xargs)
PEPPER=$(tr -dc 'a-zA-Z0-9' < /dev/urandom | fold -w42 | head -n1)

${MY_PATH}/tools/keygen -t duniter -o ./tmp/${PUBKEY}.zwallet.dunikey "${SALT}" "${PEPPER}"
G1PUB=$(cat ./tmp/${PUBKEY}.zwallet.dunikey  | grep 'pub:' | cut -d ' ' -f 2)
rm -f ./pdf/${PUBKEY}/zwallet.dunikey.enc
${MY_PATH}/tools/natools.py encrypt -p $PUBKEY -i ./tmp/${PUBKEY}.zwallet.dunikey -o ./pdf/${PUBKEY}/zwallet.dunikey.enc
echo "G1 _WALLET: $G1PUB"

amzqr "${G1PUB}" -l H -p ./static/img/zenticket.png -c -n _${G1PUB}.QR.png -d ./tmp/ 2>/dev/null
        # Write G1PUB at the bottom
        convert ./tmp/_${G1PUB}.QR.png \
          -gravity SouthWest \
          -pointsize 18 \
          -fill black \
          -annotate +5+5 "${G1PUB}" \
          ./pdf/${PUBKEY}/_${G1PUB}.QR.png

## CREATE IPNS KEY
${MY_PATH}/tools/keygen -t ipfs -o ./tmp/${PUBKEY}.zwallet.ipns "${SALT}" "${PEPPER}"
ipfs key rm ${PUBKEY} > /dev/null 2>&1
WALLETNS=$(ipfs key import ${PUBKEY} -f pem-pkcs8-cleartext ./tmp/${PUBKEY}.zwallet.ipns)
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

${MY_PATH}/tools/natools.py encrypt -p $PUBKEY -i ./tmp/${G1PUB}.ssss -o ./pdf/${PUBKEY}/ssss.enc

rm ./tmp/${G1PUB}.ssss

echo

#######################################################################
echo "Create Zine Passport"

## Add Images to ipfs
MEMBERQRIPFS=$(ipfs add -q ./pdf/${PUBKEY}/${PUBKEY}.UID.png)
FULLCERT=$(ipfs add -q ./pdf/${PUBKEY}/P2P.png)
[ -s ./pdf/${PUBKEY}/P21.png ] \
    && CERTIN=$(ipfs add -q ./pdf/${PUBKEY}/P21.png) \
    || CERTIN="QmReCfHszucv2Ra9zbKjKwmgoJ4krWpqB12TDK5AR9PKCQ/page3.png"
[ -s ./pdf/${PUBKEY}/P21.png ] \
    && CERTOUT=$(ipfs add -q ./pdf/${PUBKEY}/12P.png) \
    || CERTOUT="QmReCfHszucv2Ra9zbKjKwmgoJ4krWpqB12TDK5AR9PKCQ/page4.png"

ZWALLET=$(ipfs add -q ./pdf/${PUBKEY}/_${G1PUB}.QR.png)

LAT=$(cat ./tmp/${PUBKEY}.cesium.json | jq -r '._source.geoPoint.lat')
LAT=$(makecoord $LAT)
LON=$(cat ./tmp/${PUBKEY}.cesium.json | jq -r '._source.geoPoint.lon')
LON=$(makecoord $LON)

cat ./zine/index.html \
    | sed -e "s~QmTL7VDgkYjpYC2qiiFCfah2pSqDMkTANMeMtjMndwXq9y~${MEMBERQRIPFS}~g" \
            -e "s~QmReCfHszucv2Ra9zbKjKwmgoJ4krWpqB12TDK5AR9PKCQ/page2.png~${FULLCERT}~g" \
            -e "s~QmReCfHszucv2Ra9zbKjKwmgoJ4krWpqB12TDK5AR9PKCQ/page3.png~${CERTIN}~g" \
            -e "s~QmReCfHszucv2Ra9zbKjKwmgoJ4krWpqB12TDK5AR9PKCQ/page4.png~${CERTOUT}~g" \
            -e "s~QmdmeZhD8ncBFptmD5VSJoszmu41edtT265Xq3HVh8PhZP~${ZWALLET}~g" \
            -e "s~_WALLETNS_~${WALLETNS}~g" \
            -e "s~_PLAYER_~${MEMBERUID}~g" \
            -e "s~_PUBKEY_~${PUBKEY}~g" \
            -e "s~_G1PUB_~${G1PUB}~g" \
            -e "s~_LAT_~${LAT}~g" \
            -e "s~_LON_~${LON}~g" \
            -e "s~https://ipfs.copylaradio.com~http://127.0.0.1:8080~g" \
        > ./pdf/${PUBKEY}/PASSPORT.${MEMBERUID}.html

xdg-open ./pdf/${PUBKEY}/PASSPORT.${MEMBERUID}.html
