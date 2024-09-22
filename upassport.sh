#!/bin/bash
################################################################################
# Author: Fred (DsEx1pS33vzYZg4MroyBV9hCw98j1gtHEhwiZ5tK7ech)
# Version: 1.0
# License: AGPL-3.0 (https://choosealicense.com/licenses/agpl-3.0/)
################################################################################
if [ $# -ne 1 ]; then
    echo "Usage: $0 <pubkey>"
    exit 1
fi

source ./.env
[[ -z $myDUNITER ]] && myDUNITER="https://g1.cgeek.fr" # DUNITER
[[ -z $myCESIUM ]] && myCESIUM="https://g1.data.e-is.pro" # CESIUM+
[[ -z $ipfsNODE ]] && ipfsNODE="https://ipfs.astroport.com" # IPFS

## PUBKEY SHOULD BE A MEMBER PUBLIC KEY
PUBKEY="$1"
PUBKEY=$(echo "$PUBKEY" | tr -d ' ')
ZCHK="$(echo $PUBKEY | cut -d ':' -f 2-)" # "PUBKEY" ChK or ZEN
[[ $ZCHK == $PUBKEY ]] && ZCHK=""
PUBKEY="$(echo $PUBKEY | cut -d ':' -f 1)" # Cleaning
echo "PUBKEY ? $PUBKEY"

if [[ -z $(./tools/g1_to_ipfs.py ${PUBKEY} 2>/dev/null) ]]; then
    cat ./templates/wallet.html \
    | sed -e "s~_WALLET_~$(date -u) : ${PUBKEY}~g" \
         -e "s~_AMOUNT_~Pubkey Error<br><a target=_new href=https://cesium.app>Install CESIUM</a>~g" \
        > ./tmp/${PUBKEY}.out.html
    echo "./tmp/${PUBKEY}.out.html"
    exit 0
fi

[ ! -s $HOME/.zen/Astroport.ONE/tools/my.sh ] \
    && echo "ERROR/ Missing Astroport.ONE. Please install..." \
    && exit 1

. "$HOME/.zen/Astroport.ONE/tools/my.sh"
########################################################################
### FUNCTIONS
########################################################################
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
        && ./tools/timeout.sh -t 12 \
        curl -s ${myCESIUM}/user/profile/${pubkey} > ./tmp/${pubkey}.cesium.json 2>/dev/null

        [ ! -s "./tmp/$pubkey.cesium.json" ] \
            && echo "xxxxx ERROR PROBLEM WITH CESIUM+ NODE ${myCESIUM} xxxxx"

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
            && amzqr "${pubkey}" -l H -p ./static/img/g1ticket.png -n ${pubkey}.QR.png -d ./tmp/

        # Write UID at the bottom
        convert ./tmp/${pubkey}.QR.png \
          -gravity SouthWest \
          -pointsize 25 \
          -fill black \
          -annotate +2+2 "($ulat/$ulon) ${member_uid}" \
          -annotate +3+1 "($ulat/$ulon) ${member_uid}" \
          ./tmp/${pubkey}.UID.png

        [[ -s ./tmp/${pubkey}.UID.png ]] && rm ./tmp/${pubkey}.QR.png
    fi
}
########################################################################
########################################################################
########################################################################
### RUN TIME ######
########################################################################
## MANAGING CACHE
mkdir -p ./tmp
# Delete older than 1 day cache
find ./tmp -mtime +1 -type f -exec rm '{}' \;

## GET PUBKEY TX HISTORY
echo "LOADING WALLET HISTORY"
./tools/timeout.sh -t 12 ./tools/jaklis/jaklis.py history -n 25 -p ${PUBKEY} -j > ./tmp/$PUBKEY.TX.json
if [[ -s ./tmp/$PUBKEY.TX.json ]]; then
    SOLDE=$(./tools/timeout.sh -t 20 ./tools/jaklis/jaklis.py balance -p ${PUBKEY})
    ZEN=$(echo "($SOLDE - 1) * 10" | bc | cut -d '.' -f 1)
    AMOUNT="$SOLDE Ğ1"
    [[ $SOLDE == "null" ]] && AMOUNT = "VIDE"
    [[ $SOLDE == "" ]] && AMOUNT = "TIMEOUT"
    [[ $ZCHK == "ZEN" ]] && AMOUNT="$ZEN Ẑ€N"
else
    echo "WALLET ERROR"
    exit 1
fi
echo "$AMOUNT ($ZCHK)"

##################################### 2ND SCAN IN A DAY
## CHECK LAST TX IF ZEROCARD EXISTING
if [[ -s ./pdf/${PUBKEY}/ZEROCARD ]]; then
    ZEROCARD=$(cat ./pdf/${PUBKEY}/ZEROCARD)
    echo "G1 ZEROCARD FOUND: ${ZEROCARD}"

    if [[ -s ./pdf/${PUBKEY}/ASTATE ]]; then
        cat ./templates/wallet.html \
        | sed -e "s~_WALLET_~$(date -u) : ${PUBKEY}~g" \
             -e "s~_AMOUNT_~${AMOUNT}~g" \
            > ./tmp/${ZEROCARD}.out.html

        ASTATE=$(ipfs add -q ./tmp/${ZEROCARD}.out.html)
        echo "/ipfs/${ASTATE}" > ./pdf/${PUBKEY}/ASTATE
        ipfs name publish --key ${ZEROCARD} /ipfs/${ASTATE}
        echo "./pdf/${PUBKEY}/_index.html"
        exit 0
    else
        echo "NOT ACTIVATED YET"
    fi

    ## CHECK IF MEMBER SENT TX TO ZEROCARD
    jq '.[-1]' ./tmp/$PUBKEY.TX.json
    LASTX=$(jq '.[-1] | .amount' ./tmp/$PUBKEY.TX.json)

    if [ "$(echo "$LASTX < 0" | bc)" -eq 1 ]; then
      DEST=$(jq -r '.[-1] | .pubkey' ./tmp/$PUBKEY.TX.json)
      COMM=$(jq -r '.[-1] | .comment' ./tmp/$PUBKEY.TX.json)
      echo "TX: $DEST ($COMM)"
      if [[ "$ZEROCARD" == "$DEST" ]]; then
        echo "MATCHING !! ZEROCARD INITIALISATION..."

        ## FAC SIMILE to PAGE2
        sed -i "s~${QmRJuGqHsruaV14ZHEjk9Gxog2B9GafC35QYrJtaAU2Pry}~QmVJftuuuLgTJ8tb2kLhaKdaWFWH3jd4YXYJwM4h96NF8Q/page2.png~g" ./pdf/${PUBKEY}/_index.html

        UBQR=$(ipfs add -q ./pdf/${PUBKEY}/IPNS.QR.png)

        ZWALL=$(cat ./pdf/${PUBKEY}/ZWALL)
        ZEROCARD=$(cat ./pdf/${PUBKEY}/ZEROCARD)
        ## Change ẐeroCard G1/Cesium link to ZEROCARD /IPNS link
        sed -i "s~${ZWALL}~${UBQR}~g" ./pdf/${PUBKEY}/_index.html
        sed -i "s~${ipfsNODE}/ipfs/QmXex8PTnQehx4dELrDYuZ2t5ag85crYCBxm3fcTjVWo2k/#/app/wot/${ZEROCARD}/~$(cat ./pdf/${PUBKEY}/IPNS)~g" ./pdf/${PUBKEY}/_index.html
        rm ./pdf/${PUBKEY}/ZWALL

        ## NEW IPFSPORTAL (./pdf/${PUBKEY}/)
        mv ./pdf/${PUBKEY}/_index.html ./pdf/${PUBKEY}/_index.html
        IPFSPORTAL=$(ipfs add -qrw ./pdf/${PUBKEY}/ | tail -n 1)
        ipfs pin rm ${IPFSPORTAL}

        amzqr "${ipfsNODE}/ipfs/${IPFSPORTAL}" -l H -p ./static/img/server.png -c -n ${PUBKEY}.ipfs.png -d ./tmp/
        convert ./tmp/${PUBKEY}.ipfs.png \
        -gravity SouthWest \
        -pointsize 18 \
        -fill black \
        -annotate +2+2 "DATA : ${ipfsNODE}/ipfs/${IPFSPORTAL}" \
        -annotate +1+3 "DATA : ${ipfsNODE}/ipfs/${IPFSPORTAL}" \
        ./pdf/${PUBKEY}/IPFSPORTAL.QR.png

        IPFSPORTALQR=$(ipfs add -q ./pdf/${PUBKEY}/IPFSPORTAL.QR.png)
        sed -i "s~$(cat ./pdf/${PUBKEY}/IPFSPORTALQR)~${IPFSPORTALQR}~g" ./pdf/${PUBKEY}/_index.html
        sed -i "s~$(cat ./pdf/${PUBKEY}/IPFSPORTAL)~${IPFSPORTAL}~g" ./pdf/${PUBKEY}/_index.html
        echo $IPFSPORTALQR > ./pdf/${PUBKEY}/IPFSPORTALQR
        echo $IPFSPORTAL > ./pdf/${PUBKEY}/IPFSPORTAL
        echo "NEW IPFSPORTAL : https://ipfs.copylaradio.com/ipfs/${IPFSPORTAL}"

        ## Décodage clef IPNS par secret UPlanet (PROD = swarm.key)
        cat ./pdf/${PUBKEY}/IPNS.uplanet.asc | gpg -d --passphrase "${UPLANETNAME}" --batch > ./tmp/${MOATS}.ipns
        ipfs key rm ${ZEROCARD} > /dev/null 2>&1
        WALLETNS=$(ipfs key import ${ZEROCARD} -f pem-pkcs8-cleartext ./tmp/${MOATS}.ipns)
        ## ASTATE FIRST DApp = Wallet Ammout :
        cat ./templates/wallet.html \
        | sed -e "s~_WALLET_~$(date -u) : ${PUBKEY}~g" \
             -e "s~_AMOUNT_~${AMOUNT}~g" \
            > ./tmp/${ZEROCARD}.out.html
        ASTATE=$(ipfs add -q ./tmp/${ZEROCARD}.out.html)
        echo "/ipfs/${ASTATE}" > ./pdf/${PUBKEY}/ASTATE
        ipfs name publish --key ${ZEROCARD} /ipfs/${ASTATE}

        echo "./pdf/${PUBKEY}/_index.html"
        exit 0
      else
        echo "TX NOT FOR ZEROCARD"
      fi
    else
        echo "RX..."
    fi
fi

echo "## GETTING CESIUM+ PROFILE"
[[ ! -s ./tmp/$PUBKEY.me.json ]] \
&& wget -q -O ./tmp/$PUBKEY.me.json ${myDUNITER}/wot/lookup/$PUBKEY

echo "# GET MEMBER UID"
MEMBERUID=$(cat ./tmp/$PUBKEY.me.json | jq -r '.results[].uids[].uid')
## NO MEMBER
if [[ -z $MEMBERUID ]]; then
    cat ./templates/wallet.html \
        | sed -e "s~_WALLET_~${PUBKEY}~g" \
             -e "s~_AMOUNT_~${AMOUNT}~g" \
            > ./tmp/${PUBKEY}.out.html
    #~ xdg-open "./tmp/${PUBKEY}.out.html"
    echo "./tmp/${PUBKEY}.out.html"
    exit 0
fi
### ============================================

### MEMBER N1 SCAN : PASSPORT CREATION
mkdir -p ./pdf/${PUBKEY}/N1/

cp ./tmp/$PUBKEY.me.json ./pdf/${PUBKEY}/CESIUM.json
cp ./tmp/$PUBKEY.TX.json ./pdf/${PUBKEY}/TX.json

# Call the function with PUBKEY and MEMBERUID
generate_qr_with_uid "$PUBKEY" "$MEMBERUID"
cp ./tmp/${PUBKEY}.UID.png ./pdf/${PUBKEY}/${PUBKEY}.UID.png

##################################################"
### ANALYSE RELATIONS FROM ./tmp/$PUBKEY.me.json
##################################################"
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
    [[ ! -s ./pdf/${PUBKEY}/N1/${certout[$uid]}.${uid}.p2p.png ]] \
        && generate_qr_with_uid "${certout[$uid]}" "$uid" \
        && cp ./tmp/${certout[$uid]}.UID.png ./pdf/${PUBKEY}/N1/${certout[$uid]}.${uid}.p2p.png \
        && sleep 3
  fi
done

echo "========================================================"
echo "UIDs présents uniquement dans certin (certificateurs uniquement) :"
for uid in "${!certin[@]}"; do
  if [[ -z "${certout[$uid]}" ]]; then
    echo "UID: $uid, PubKey certifiant: ${certin[$uid]}"

    # make certin only QR
    [[ ! -s ./pdf/${PUBKEY}/N1/${certin[$uid]}.${uid}.certin.png ]] \
        && generate_qr_with_uid "${certin[$uid]}" "$uid" \
        && cp ./tmp/${certin[$uid]}.UID.png ./pdf/${PUBKEY}/N1/${certin[$uid]}.${uid}.certin.png \
        && sleep 3
  fi
done

echo "========================================================"
echo "UIDs présents uniquement dans certout (certifiés uniquement) :"
for uid in "${!certout[@]}"; do
  if [[ -z "${certin[$uid]}" ]]; then
    echo "UID: $uid, PubKey certifié: ${certout[$uid]}"
    # make certout only QR
    [[ ! -s ./pdf/${PUBKEY}/N1/${certout[$uid]}.${uid}.certout.png ]] \
        && generate_qr_with_uid "${certout[$uid]}" "$uid" \
        && cp ./tmp/${certout[$uid]}.UID.png ./pdf/${PUBKEY}/N1/${certout[$uid]}.${uid}.certout.png \
        && sleep 3
  fi
done


########################################"
# CREATE FRIENDS PAGES INTO PDF
## Moving Related UID into ./pdf/${PUBKEY}/N1/
## Peer to Peer
nb_fichiers=$(ls ./pdf/${PUBKEY}/N1/*.p2p.png | wc -l)
montage -mode concatenate -geometry +20x20 -tile $(echo "scale=0; $nb_fichiers / sqrt($nb_fichiers) - 1" | bc)x$(echo "scale=0; sqrt($nb_fichiers) + 3" | bc) -density 300 ./pdf/${PUBKEY}/N1/*.p2p.png ./pdf/${PUBKEY}/P2P.${PUBKEY}.pdf
convert -density 300 ./pdf/${PUBKEY}/P2P.${PUBKEY}.pdf -resize 375x550 ./pdf/${PUBKEY}/P2P.png
## Peer to One
nb_fichiers=$(ls ./pdf/${PUBKEY}/N1/*.certin.png | wc -l)
montage -mode concatenate -geometry +20x20 -tile $(echo "scale=0; $nb_fichiers / sqrt($nb_fichiers) - 1" | bc)x$(echo "scale=0; sqrt($nb_fichiers) + 3" | bc) -density 300 ./pdf/${PUBKEY}/N1/*.certin.png ./pdf/${PUBKEY}/P21.${PUBKEY}.pdf
convert -density 300 ./pdf/${PUBKEY}/P21.${PUBKEY}.pdf -resize 375x550 ./pdf/${PUBKEY}/P21.png
## One to Peer
nb_fichiers=$(ls ./pdf/${PUBKEY}/N1/*.certout.png | wc -l)
montage -mode concatenate -geometry +20x20 -tile $(echo "scale=0; $nb_fichiers / sqrt($nb_fichiers) - 1" | bc)x$(echo "scale=0; sqrt($nb_fichiers) + 3" | bc) -density 300 ./pdf/${PUBKEY}/N1/*.certout.png ./pdf/${PUBKEY}/12P.${PUBKEY}.pdf
convert -density 300 ./pdf/${PUBKEY}/12P.${PUBKEY}.pdf -resize 375x550 ./pdf/${PUBKEY}/12P.png

################################################################################
echo "# CREATE SHAMIR KEY ................"

prime=$(./tools/diceware.sh 1 | xargs)
SALT=$(tr -dc 'a-zA-Z0-9' < /dev/urandom | fold -w42 | head -n1)

second=$(./tools/diceware.sh 1 | xargs)
PEPPER=$(tr -dc 'a-zA-Z0-9' < /dev/urandom | fold -w42 | head -n1)

./tools/keygen -t duniter -o ./tmp/${PUBKEY}.zwallet.dunikey "${SALT}" "${PEPPER}"
ZENWALLET=$(cat ./tmp/${PUBKEY}.zwallet.dunikey  | grep 'pub:' | cut -d ' ' -f 2)

rm -f ./pdf/${PUBKEY}/zwallet.dunikey.enc
./tools/natools.py encrypt -p $PUBKEY -i ./tmp/${PUBKEY}.zwallet.dunikey -o ./pdf/${PUBKEY}/zwallet.dunikey.enc
echo "ZEN _WALLET: $ZENWALLET"

rm -f ./pdf/${PUBKEY}/ZEROCARD_*.QR.jpg
echo "${ZENWALLET}" > ./pdf/${PUBKEY}/ZEROCARD
echo "$(date -u)" > ./pdf/${PUBKEY}/DATE

amzqr "${ZENWALLET}" -l H -p ./static/img/zenticket.png -c -n ZEROCARD_${ZENWALLET}.QR.png -d ./tmp/ 2>/dev/null
        # Write ZENWALLET at the bottom
        convert ./tmp/ZEROCARD_${ZENWALLET}.QR.png \
          -gravity SouthWest \
          -pointsize 18 \
          -fill black \
          -annotate +2+2 "ZEROCARD : ${ZENWALLET}" \
          -annotate +1+3 "ZEROCARD : ${ZENWALLET}" \
          ./pdf/${PUBKEY}/ZEROCARD_${ZENWALLET}.QR.jpg

############################################################
################################################################# IPNS
#~ ## CREATE IPNS KEY
./tools/keygen -t ipfs -o ./tmp/${ZENWALLET}.IPNS.key "${SALT}" "${PEPPER}"
ipfs key rm ${ZENWALLET} > /dev/null 2>&1
WALLETNS=$(ipfs key import ${ZENWALLET} -f pem-pkcs8-cleartext ./tmp/${ZENWALLET}.IPNS.key)

## ENCODE IPNS KEY WITH CAPTAING1PUB
echo "./tools/natools.py encrypt -p $CAPTAING1PUB -i ./tmp/${ZENWALLET}.IPNS.key -o ./pdf/${PUBKEY}/IPNS.captain.enc"
./tools/natools.py encrypt -p $CAPTAING1PUB -i ./tmp/${ZENWALLET}.IPNS.key -o ./pdf/${PUBKEY}/IPNS.captain.enc

## ENCRYPT WITH UPLANETNAME PASSWORD
if [[ ! -z ${UPLANETNAME} ]]; then
    rm -f ./pdf/${PUBKEY}/IPNS.uplanet.asc
    cat ./tmp/${ZENWALLET}.IPNS.key | gpg --symmetric --armor --batch --passphrase "${UPLANETNAME}" -o ./pdf/${PUBKEY}/IPNS.uplanet.asc
fi

rm ./tmp/${ZENWALLET}.IPNS.key
ipfs key rm ${ZENWALLET} > /dev/null 2>&1
echo "_WALLET IPNS STORAGE: /ipns/$WALLETNS"
amzqr "${ipfsNODE}/ipns/$WALLETNS" -l H -p ./static/img/moa_net.png -c -n ${PUBKEY}.IPNS.QR.png -d ./tmp/ 2>/dev/null
convert ./tmp/${PUBKEY}.IPNS.QR.png \
        -gravity SouthWest \
        -pointsize 18 \
        -fill black \
        -annotate +2+2 "APP : ${ipfsNODE}/ipns/$WALLETNS" \
        -annotate +1+3 "APP : ${ipfsNODE}/ipns/$WALLETNS" \
        ./pdf/${PUBKEY}/IPNS.QR.png

echo "${ipfsNODE}/ipns/$WALLETNS" > ./pdf/${PUBKEY}/IPNS

#######################################################################
## PREPARE DISCO SECRET
DISCO="/?${prime}=${SALT}&${second}=${PEPPER}"
echo "SOURCE : "$DISCO

## ssss-split : Keep 2 needed over 3
echo "$DISCO" | ssss-split -t 2 -n 3 -q > ./tmp/${ZENWALLET}.ssss
HEAD=$(cat ./tmp/${ZENWALLET}.ssss | head -n 1) && echo "$HEAD" > ./tmp/${ZENWALLET}.ssss.head
MIDDLE=$(cat ./tmp/${ZENWALLET}.ssss | head -n 2 | tail -n 1) && echo "$MIDDLE" > ./tmp/${ZENWALLET}.ssss.mid
TAIL=$(cat ./tmp/${ZENWALLET}.ssss | tail -n 1) && echo "$TAIL" > ./tmp/${ZENWALLET}.ssss.tail
echo "TEST DECODING..."
echo "$HEAD
$TAIL" | ssss-combine -t 2 -q
[ ! $? -eq 0 ] && echo "ERROR! SSSSKEY DECODING FAILED" && exit 1

## ENCODE HEAD SSSS SECRET WITH MEMBER PUBKEY
echo "./tools/natools.py encrypt -p $PUBKEY -i ./tmp/${ZENWALLET}.ssss.head -o ./pdf/${PUBKEY}/ssss.member.enc"
./tools/natools.py encrypt -p $PUBKEY -i ./tmp/${ZENWALLET}.ssss.head -o ./pdf/${PUBKEY}/ssss.head.member.enc

## MIDDLE ENCRYPT WITH UPLANETNAME
if [[ ! -z ${UPLANETNAME} ]]; then
    rm -f ./pdf/${PUBKEY}/ssss.mid.uplanet.asc
    cat ./tmp/${ZENWALLET}.ssss.mid | gpg --symmetric --armor --batch --passphrase "${UPLANETNAME}" -o ./pdf/${PUBKEY}/ssss.mid.uplanet.asc
    cat ./pdf/${PUBKEY}/ssss.mid.uplanet.asc | gpg -d --passphrase "${UPLANETNAME}" --batch > ./tmp/${ZENWALLET}.ssss.test
    [[ $(diff -q ./tmp/${ZENWALLET}.ssss.test ./tmp/${ZENWALLET}.ssss.mid) != "" ]] && echo "ERROR: GPG ENCRYPTION FAILED "
    rm ./tmp/${ZENWALLET}.ssss.test
fi

## ENCODE TAIL SSSS SECRET WITH CAPTAING1PUB
./tools/natools.py encrypt -p $CAPTAING1PUB -i ./tmp/${ZENWALLET}.ssss.tail -o ./pdf/${PUBKEY}/ssss.tail.captain.enc

#~ rm ./tmp/${ZENWALLET}.ssss*

#### INITIALISE IPFS STORAGE (ZENCARD BLOCK 0)
### add html page for next step...
rm -f ./pdf/${PUBKEY}/_index.html
echo "CREATION IPFS PORTAIL"

## Add Images to ipfs
MEMBERPUBQR=$(ipfs add -q ./pdf/${PUBKEY}/${PUBKEY}.UID.png)
ZWALLET=$(ipfs add -q ./pdf/${PUBKEY}/ZEROCARD_${ZENWALLET}.QR.jpg)
echo "$ZWALLET" > ./pdf/${PUBKEY}/ZWALL ## USED & DELETED AFTER CONFIRMED PRIMAL TX

IPFSPORTAL=$(ipfs add -qrw ./pdf/${PUBKEY}/ | tail -n 1)
echo $IPFSPORTAL > ./pdf/${PUBKEY}/IPFSPORTAL

ipfs pin rm ${IPFSPORTAL}
echo "${ipfsNODE}/ipfs/${IPFSPORTAL}"

amzqr "${ipfsNODE}/ipfs/${IPFSPORTAL}" -l H -p ./static/img/server.png -c -n ${PUBKEY}.ipfs.png -d ./tmp/
convert ./tmp/${PUBKEY}.ipfs.png \
        -gravity SouthWest \
        -pointsize 18 \
        -fill black \
        -annotate +2+2 "DATA : ${ipfsNODE}/ipfs/${IPFSPORTAL}" \
        -annotate +1+3 "DATA : ${ipfsNODE}/ipfs/${IPFSPORTAL}" \
        ./pdf/${PUBKEY}/IPFSPORTAL.QR.png

IPFSPORTALQR=$(ipfs add -q ./pdf/${PUBKEY}/IPFSPORTAL.QR.png)
echo $IPFSPORTALQR > ./pdf/${PUBKEY}/IPFSPORTALQR
#######################################################################
echo "Create Zine Passport"

[ -s ./pdf/${PUBKEY}/P2P.png ] \
    && FULLCERT=$(ipfs add -q ./pdf/${PUBKEY}/P2P.png) \
    || FULLCERT="QmReCfHszucv2Ra9zbKjKwmgoJ4krWpqB12TDK5AR9PKCQ/page3.png"

[ -s ./pdf/${PUBKEY}/P21.png ] \
    && CERTIN=$(ipfs add -q ./pdf/${PUBKEY}/P21.png) \
    || CERTIN="QmReCfHszucv2Ra9zbKjKwmgoJ4krWpqB12TDK5AR9PKCQ/page4.png"

[ -s ./pdf/${PUBKEY}/12P.png ] \
    && CERTOUT=$(ipfs add -q ./pdf/${PUBKEY}/12P.png) \
    || CERTOUT="QmReCfHszucv2Ra9zbKjKwmgoJ4krWpqB12TDK5AR9PKCQ/page4.png"

LAT=$(cat ./tmp/${PUBKEY}.cesium.json | jq -r '._source.geoPoint.lat')
LAT=$(makecoord $LAT)
LON=$(cat ./tmp/${PUBKEY}.cesium.json | jq -r '._source.geoPoint.lon')
LON=$(makecoord $LON)

# QmRJuGqHsruaV14ZHEjk9Gxog2B9GafC35QYrJtaAU2Pry Fac Similé
# QmVJftuuuLgTJ8tb2kLhaKdaWFWH3jd4YXYJwM4h96NF8Q/page2.png
cat ./static/zine/index.html \
    | sed -e "s~QmU43PSABthVtM8nWEWVDN1ojBBx36KLV5ZSYzkW97NKC3/page1.png~QmdEPc4Toy1vth7MZtpRSjgMtAWRFihZp3G72Di1vMhf1J~g" \
            -e "s~QmVJftuuuLgTJ8tb2kLhaKdaWFWH3jd4YXYJwM4h96NF8Q/page2.png~${FULLCERT}~g" \
            -e "s~QmTL7VDgkYjpYC2qiiFCfah2pSqDMkTANMeMtjMndwXq9y~QmRJuGqHsruaV14ZHEjk9Gxog2B9GafC35QYrJtaAU2Pry~g" \
            -e "s~QmexZHwUuZdFLZuHt1PZunjC7c7rTFKRWJDASGPTyrqysP/page3.png~${CERTIN}~g" \
            -e "s~QmNNTCYNSHS3iKZsBHXC1tiP2eyFqgLT4n3AXdcK7GywVc/page4.png~${CERTOUT}~g" \
            -e "s~QmZHV5QppQX9N7MS1GFMqzmnRU5vLbpmQ1UkSRY5K5LfA9/page_.png~${IPFSPORTALQR}~g" \
            -e "s~QmNSck9ygXYG6YHu19DfuJnH2B8yS9RRkEwP1tD35sjUgE/pageZ.png~${MEMBERPUBQR}~g" \
            -e "s~QmdmeZhD8ncBFptmD5VSJoszmu41edtT265Xq3HVh8PhZP~${ZWALLET}~g" \
            -e "s~_IPFS_~ipfs/${IPFSPORTAL}~g" \
            -e "s~_PLAYER_~${MEMBERUID}~g" \
            -e "s~_DATE_~$(date -u)~g" \
            -e "s~_PUBKEY_~${PUBKEY}~g" \
            -e "s~_G1PUB_~${ZENWALLET}~g" \
            -e "s~_ZENWALLET_~${ZENWALLET}~g" \
            -e "s~_LAT_~${LAT}~g" \
            -e "s~_LON_~${LON}~g" \
            -e "s~https://ipfs.copylaradio.com~${ipfsNODE}~g" \
        > ./pdf/${PUBKEY}/_index.html

echo "./pdf/${PUBKEY}/_index.html"
#~ xdg-open ./pdf/${PUBKEY}/_index.html ## OPEN PASSPORT ON DESKTOP
[[ ! -s ./pdf/${PUBKEY}/_index.html ]] && echo "./tmp/54321.log" ## SEND LOG TO USER
exit 0
