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

################################################################### INIT
#######################################################################
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

# CHECK PUBKEY FORMAT
if [[ -z $(./tools/g1_to_ipfs.py ${PUBKEY} 2>/dev/null) ]]; then
    cat ./templates/wallet.html \
    | sed -e "s~_WALLET_~$(date -u) <br> ${PUBKEY}~g" \
         -e "s~_AMOUNT_~Pubkey Error<br><a target=_new href=https://cesium.app>Install CESIUM</a>~g" \
        > ./tmp/${PUBKEY}.out.html
    echo "./tmp/${PUBKEY}.out.html"
    exit 0
fi

## LOAD ASTROPORT ENVIRONMENT
[ ! -s $HOME/.zen/Astroport.ONE/tools/my.sh ] \
    && echo "ERROR/ Missing Astroport.ONE. Please install..." \
    && exit 1
. "$HOME/.zen/Astroport.ONE/tools/my.sh"

TOT=0
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
########################################################################
# Function to get Cesium+ profile, generate QR code, and annotate with UID
generate_qr_with_uid() {
    local pubkey=$1
    local member_uid=$2
    ## Extract wallet balance
    if [[ ! -s ./tmp/${pubkey}.solde ]]; then
        solde=$(./tools/timeout.sh -t 6 ./tools/jaklis/jaklis.py balance -p ${pubkey})
        [ ! $? -eq 0 ] \
            && sort -u -o ./tools/jaklis/.env ./tools/jaklis/.env \
            && GVA=$(~/.zen/Astroport.ONE/tools/duniter_getnode.sh | tail -n 1) \
            && [[ ! -z $GVA ]] && echo "NODE=$GVA" >> ./tools/jaklis/.env && echo "GVA RELAY: $GVA"
        echo "$solde" > ./tmp/${pubkey}.solde
    else
        solde=$(cat ./tmp/${pubkey}.solde)
    fi
    zen=$(echo "($solde - 1) * 10" | bc | cut -d '.' -f 1)
    TOT=$((TOT + zen))

    if [ ! -s ./tmp/${pubkey}.UID.png ]; then
        echo "GET ${member_uid} CESIUM+ [${zen}ẑ] : ${pubkey} "
        [[ ! -s ./tmp/$pubkey.cesium.json ]] \
        && ./tools/timeout.sh -t 6 \
        curl -s ${myCESIUM}/user/profile/${pubkey} > ./tmp/${pubkey}.cesium.json 2>/dev/null

        if [ ! -s "./tmp/$pubkey.cesium.json" ]; then
            echo "xxxxx ERROR PROBLEM WITH CESIUM+ NODE ${myCESIUM} xxxxx"
        else
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
        fi

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
          -annotate +2+2 "${zen} Z ${member_uid} ($ulat/$ulon)" \
          -annotate +3+1 "${zen} Z ${member_uid} ($ulat/$ulon)" \
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
./tools/timeout.sh -t 6 ./tools/jaklis/jaklis.py history -n 25 -p ${PUBKEY} -j > ./tmp/$PUBKEY.TX.json
[ ! $? -eq 0 ] \
    && sort -u -o ./tools/jaklis/.env ./tools/jaklis/.env \
    && GVA=$(~/.zen/Astroport.ONE/tools/duniter_getnode.sh | tail -n 1) \
    && [[ ! -z $GVA ]] && echo "NODE=$GVA" >> ./tools/jaklis/.env ### CHANGE DUNITER GVA RELAY

## EXTRACT SOLDE & ZEN & ROUND
if [[ -s ./tmp/$PUBKEY.TX.json ]]; then
    SOLDE=$(./tools/timeout.sh -t 20 ./tools/jaklis/jaklis.py balance -p ${PUBKEY})
    ROUND=$(echo "$SOLDE" | cut -d '.' -f 1)
    ZEN=$(echo "($SOLDE - 1) * 10" | bc | cut -d '.' -f 1)

    [[ "$(echo "$ROUND < 100" | bc)" == 1 ]] && ROUND=100

    AMOUNT="$SOLDE Ğ1"
    [[ $SOLDE == "null" ]] && AMOUNT = "EMPTY" && ROUND=200
    [[ $SOLDE == "" ]] && AMOUNT = "TIMEOUT" && ROUND=200
    [[ $ZCHK == "ZEN" ]] && AMOUNT="$ZEN ẑ€N"
else
    cat ./templates/wallet.html \
    | sed -e "s~_WALLET_~$(date -u) <br> ${PUBKEY}~g" \
         -e "s~#000~#F00~g" \
         -e "s~_AMOUNT_~CRITICAL WALLET ERROR~g" \
        > ./tmp/${PUBKEY}.out.html
    echo "./tmp/${PUBKEY}.out.html"
    exit 1
fi
echo "$AMOUNT G1 ($ZCHK) $ZEN ẑ€N"
echo "------------------------------------- $ROUND -"
echo
##################################### SCAN N°
## CHECK LAST TX IF ZEROCARD EXISTING
if [[ -s ./pdf/${PUBKEY}/ZEROCARD ]]; then
    ZEROCARD=$(cat ./pdf/${PUBKEY}/ZEROCARD)
    echo "G1 ZEROCARD FOUND: ${ZEROCARD}"
    ## CHECK IF MEMBER SENT TX TO ZEROCARD
    jq '.[-1]' ./tmp/$PUBKEY.TX.json
    LASTX=$(jq '.[-1] | .amount' ./tmp/$PUBKEY.TX.json)
    DEST=$(jq -r '.[-1] | .pubkey' ./tmp/$PUBKEY.TX.json)
    COMM=$(jq -r '.[-1] | .comment' ./tmp/$PUBKEY.TX.json)
    TXDATE=$(jq -r '.[-1] | .date' ./tmp/$PUBKEY.TX.json)

    ##################################### 3RD SCAN
    if [ -L "./pdf/${PUBKEY}" ]; then
        ############################ TRANSMIT COMMAND
        if [[ $COMM != "" && "$DEST" == "$ZEROCARD" ]]; then
            ./command.sh "$PUBKEY" "$COMM" "$LASTX" "$TXDATE"
            [ ! $? -eq 0 ] && echo ">>>>>>>>>>>> ERROR"
        fi
        ##################################### 4TH SCAN
        if [[ -s ./pdf/${PUBKEY}/ASTATE ]]; then
            ## REDIRECT TO CURRENT ASTATE
            echo '<!DOCTYPE html><html><head>
            <meta http-equiv="refresh" content="0; url='${ipfsNODE}$(cat ./pdf/${PUBKEY}/ASTATE)'">
            </head><body></body></html>' > ./tmp/${ZEROCARD}.out.html
            echo "./tmp/${ZEROCARD}.out.html"
            exit 0
        else
            ## ACTIVATE ZENCARD 1ST APP ##### 2ND SCAN
            cat ./templates/wallet.html \
            | sed -e "s~_WALLET_~$(date -u) <br> ${PUBKEY}~g" \
                 -e "s~_AMOUNT_~<a target=_new href=${ipfsNODE}/ipfs/$(cat ./pdf/${PUBKEY}/IPFSPORTAL)/${PUBKEY}/_index.html>${AMOUNT}</a>~g" \
                 -e "s~300px~301px~g" \
                > ./tmp/${ZEROCARD}.out.html

            ASTATE=$(ipfs add -q ./tmp/${ZEROCARD}.out.html)
            echo "/ipfs/${ASTATE}" > ./pdf/${PUBKEY}/ASTATE
            ipfs name publish --key ${ZEROCARD} /ipfs/${ASTATE}
            echo "./tmp/${ZEROCARD}.out.html"
            exit 0
        fi
    else
        ##### NO GOOD BACK TO 1ST SCAN
        echo "........... ZEROCARD NOT ACTIVATED YET"
    fi

    if [ "$(echo "$LASTX < 0" | bc)" -eq 1 ]; then
    ######################################################################
      echo "TX: $DEST ($COMM)"
      if [[ "$ZEROCARD" == "$DEST" ]]; then
        ################# ACTIVATION ###############
        echo "MATCHING !! ZEROCARD INITIALISATION..."
        echo "$TXDATE" > ./pdf/${PUBKEY}/COMMANDTIME
        ################# ACTIVATION ###############
        ## Replace FAC SIMILE with page2
        sed -i "s~QmRJuGqHsruaV14ZHEjk9Gxog2B9GafC35QYrJtaAU2Pry~QmVJftuuuLgTJ8tb2kLhaKdaWFWH3jd4YXYJwM4h96NF8Q/page2.png~g" ./pdf/${PUBKEY}/_index.html
        OIPNSQR=$(ipfs add -q ./pdf/${PUBKEY}/IPNS.QR.png)
        ## Collect previous data
        ZWALL=$(cat ./pdf/${PUBKEY}/ZWALL)
        ZEROCARD=$(cat ./pdf/${PUBKEY}/ZEROCARD)
        ## Change ẐeroCard G1/Cesium link to ZEROCARD /IPNS link
        sed -i "s~${ZWALL}~${OIPNSQR}~g" ./pdf/${PUBKEY}/_index.html
        sed -i "s~${ipfsNODE}/ipfs/QmXex8PTnQehx4dELrDYuZ2t5ag85crYCBxm3fcTjVWo2k/#/app/wot/${ZEROCARD}/~$(cat ./pdf/${PUBKEY}/IPNS)~g" ./pdf/${PUBKEY}/_index.html
        ## NEW IPFSPORTAL (DATA : ./pdf/${PUBKEY}/*)
        IPFSPORTAL=$(ipfs add -qrw ./pdf/${PUBKEY}/ | tail -n 1)
        ipfs pin rm ${IPFSPORTAL}

        ### EXTEND IPNS QR with CAPTAIN middle key part
            ./tools/natools.py decrypt -i ./pdf/${PUBKEY}/IPNS.captain.enc -k ~/.zen/game/players/.current/secret.dunikey -o ./tmp/${PUBKEY}.middle
            amzqr "$(cat ./tmp/${PUBKEY}.middle)" -l H -n middle_qr.png -d ./tmp/ 2>/dev/null
            convert ./pdf/${PUBKEY}/IPNS.QR.png ./tmp/middle_qr.png -append ./pdf/${PUBKEY}/combined_qr.png
            APPIPNSQRSEC=$(ipfs add -q ./pdf/${PUBKEY}/combined_qr.png)
            ipfs pin remove $APPIPNSQRSEC
            # Clean up temporary files
            rm ./tmp/middle_qr.png
            rm ./pdf/${PUBKEY}/combined_qr.png
            rm ./tmp/${PUBKEY}.middle
            ## INSERT APPIPNSQRSEC
            [ ! -z $APPIPNSQRSEC ] \
            && sed -i "s~${OIPNSQR}~${APPIPNSQRSEC}~g" ./pdf/${PUBKEY}/_index.html

        ## IPFSPORTAL = DATA ipfs link
        amzqr "${ipfsNODE}/ipfs/${IPFSPORTAL}" -l H -p ./static/img/server.png -c -n ${PUBKEY}.ipfs.png -d ./tmp/
        convert ./tmp/${PUBKEY}.ipfs.png \
        -gravity SouthWest \
        -pointsize 18 \
        -fill black \
        -annotate +2+2 "[DATA] ${ipfsNODE}/ipfs/${IPFSPORTAL}" \
        -annotate +1+3 "[DATA] ${ipfsNODE}/ipfs/${IPFSPORTAL}" \
        ./pdf/${PUBKEY}/IPFSPORTAL.QR.png

        IPFSPORTALQR=$(ipfs add -q ./pdf/${PUBKEY}/IPFSPORTAL.QR.png)
        sed -i "s~$(cat ./pdf/${PUBKEY}/IPFSPORTALQR)~${IPFSPORTALQR}~g" ./pdf/${PUBKEY}/_index.html
        sed -i "s~$(cat ./pdf/${PUBKEY}/IPFSPORTAL)~${IPFSPORTAL}~g" ./pdf/${PUBKEY}/_index.html
        echo $IPFSPORTALQR > ./pdf/${PUBKEY}/IPFSPORTALQR
        echo $IPFSPORTAL > ./pdf/${PUBKEY}/IPFSPORTAL
        echo "NEW IPFSPORTAL : https://ipfs.copylaradio.com/ipfs/${IPFSPORTAL}"

        ## IMPORT ZEROCARD into LOCAL IPFS KEYS
        ## Décodage clef IPNS par secret UPlanet (PROD = swarm.key)
        cat ./pdf/${PUBKEY}/IPNS.uplanet.asc | gpg -d --passphrase "${UPLANETNAME}" --batch > ./tmp/${MOATS}.ipns
        ipfs key rm ${ZEROCARD} > /dev/null 2>&1
        WALLETNS=$(ipfs key import ${ZEROCARD} -f pem-pkcs8-cleartext ./tmp/${MOATS}.ipns)
        ## ASTATE FIRST DApp = Wallet ZEROCARD QR :
        cat ./templates/wallet.html \
        | sed -e "s~_WALLET_~$(date -u) <br> ${PUBKEY}~g" \
             -e "s~_AMOUNT_~<img src=${ipfsNODE}/ipfs/${ZWALL} />~g" \
            > ./tmp/${ZEROCARD}.out.html
        ASTATE=$(ipfs add -q ./tmp/${ZEROCARD}.out.html)
        echo "/ipfs/${ASTATE}" > ./pdf/${PUBKEY}/ASTATE
        ipfs name publish --key ${ZEROCARD} /ipfs/${ASTATE}

        ######### move PDF to PASSPORT ################### in ASTROPORT game
        mkdir -p ~/.zen/game/passport
        mv ./pdf/${PUBKEY} ~/.zen/game/passport/
        ln -s ~/.zen/game/passport/${PUBKEY} ./pdf/${PUBKEY}

        #### UPASSPORT READY #####
        echo "./pdf/${PUBKEY}/_index.html"
        exit 0
      else
        ## RESEND FAC SIMILE
        echo "TX NOT FOR ZEROCARD"
        echo "./pdf/${PUBKEY}/_index.html"
        exit 0
      fi
    else
        ## RESEND FAC SIMILE
        echo "RX..."
        echo "./pdf/${PUBKEY}/_index.html"
        exit 0
    fi
fi

#######################################################################
#######################################################################
#######################################################################
### FIRST TRY. NO ZEROCARD MADE YET.
### FRESH PUBKEY... IS IT A MEMBER 0R A WALLET ?
echo "## GETTING CESIUM+ PROFILE"
[[ ! -s ./tmp/$PUBKEY.me.json ]] \
&& ./tools/timeout.sh -t 8 \
wget -q -O ./tmp/$PUBKEY.me.json ${myDUNITER}/wot/lookup/$PUBKEY

echo "# GET MEMBER UID"
MEMBERUID=$(cat ./tmp/$PUBKEY.me.json | jq -r '.results[].uids[].uid')

if [[ -z $MEMBERUID ]]; then
    ## NOT MEMBERUID : THIS IS A WALLET
    cat ./templates/wallet.html \
        | sed -e "s~_WALLET_~$(date -u) <br> ${PUBKEY}~g" \
             -e "s~_AMOUNT_~${AMOUNT}~g" \
            > ./tmp/${PUBKEY}.out.html
    #~ xdg-open "./tmp/${PUBKEY}.out.html"
    echo "./tmp/${PUBKEY}.out.html"
    exit 0
fi
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
### MEMBER N1 SCAN & UPASSPORT CREATION
## N1 DESTINATION PATH
mkdir -p ./pdf/${PUBKEY}/N1/
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## CESIUM & DUNITER extract
cp ./tmp/$PUBKEY.me.json ./pdf/${PUBKEY}/CESIUM.json
cp ./tmp/$PUBKEY.TX.json ./pdf/${PUBKEY}/TX.json

################################################## N1 analysing
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

echo "___________Pubkey Certifiés P21 :"
# Access the values (example: print all uids and their corresponding pubkeys)
for uid in "${!certout[@]}"; do
  echo "UID: $uid, PubKey: ${certout[$uid]}"
done

## GET certifiers-of
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
    [[ ! -s ./pdf/${PUBKEY}/N1/${certout[$uid]}.${uid}.p2p.png ]] \
        && generate_qr_with_uid "${certout[$uid]}" "$uid" \
        && cp ./tmp/${certout[$uid]}.UID.png ./pdf/${PUBKEY}/N1/${certout[$uid]}.${uid}.p2p.png \
        && sleep 2
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
    [[ ! -s ./pdf/${PUBKEY}/N1/${certin[$uid]}.${uid}.certin.png ]] \
        && generate_qr_with_uid "${certin[$uid]}" "$uid" \
        && cp ./tmp/${certin[$uid]}.UID.png ./pdf/${PUBKEY}/N1/${certin[$uid]}.${uid}.certin.png \
        && sleep 2
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
    [[ ! -s ./pdf/${PUBKEY}/N1/${certout[$uid]}.${uid}.certout.png ]] \
        && generate_qr_with_uid "${certout[$uid]}" "$uid" \
        && cp ./tmp/${certout[$uid]}.UID.png ./pdf/${PUBKEY}/N1/${certout[$uid]}.${uid}.certout.png \
        && sleep 2
  fi
done
TOTP21=$TOT
TOT=0
echo "TOTP_21=$TOTP21"

TOTAL=$((TOTP2P + TOT12P + TOTP21))

# Generate PUBKEY and MEMBERUID "QRCODE" add TOTAL
generate_qr_with_uid "$PUBKEY" "$MEMBERUID"
cp ./tmp/${PUBKEY}.UID.png ./pdf/${PUBKEY}/${PUBKEY}.UID.png
    convert ./tmp/${PUBKEY}.UID.png \
          -gravity NorthEast \
          -pointsize 25 \
          -fill black \
          -annotate +2+2 "${TOTAL} Z" \
          -annotate +3+1 "${TOTAL} Z" \
          ./pdf/${PUBKEY}/${PUBKEY}.UID.png
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# CREATE FRIENDS PAGES INTO PDF & png
## Moving Related UID into ./pdf/${PUBKEY}/N1/
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
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
echo "############################################################"
echo "# CREATE ZEROCARD ......... TOTAL = $TOTAL Z"
echo "############################################################"
prime=$(./tools/diceware.sh 1 | xargs)
SALT=$(tr -dc 'a-zA-Z0-9' < /dev/urandom | fold -w42 | head -n1)
second=$(./tools/diceware.sh 1 | xargs)
PEPPER=$(tr -dc 'a-zA-Z0-9' < /dev/urandom | fold -w42 | head -n1)
################################################################# DUNITER
./tools/keygen -t duniter -o ./tmp/${PUBKEY}.zwallet.dunikey "${SALT}" "${PEPPER}"
G1PUBZERO=$(cat ./tmp/${PUBKEY}.zwallet.dunikey  | grep 'pub:' | cut -d ' ' -f 2)

rm -f ./pdf/${PUBKEY}/zwallet.member.enc
## zwallet.dunikey PUBKEY encryption
./tools/natools.py encrypt -p $PUBKEY -i ./tmp/${PUBKEY}.zwallet.dunikey -o ./pdf/${PUBKEY}/zwallet.member.enc
echo "ZEN _WALLET: $G1PUBZERO"
rm -f ./pdf/${PUBKEY}/ZEROCARD_*.QR.jpg # cleaning & provisionning
echo "${G1PUBZERO}" > ./pdf/${PUBKEY}/ZEROCARD
echo "$(date -u)" > ./pdf/${PUBKEY}/DATE
## create ZEROCARD QR
amzqr "${G1PUBZERO}" -l H -p ./static/img/zenticket.png -c -n ZEROCARD_${G1PUBZERO}.QR.png -d ./tmp/ 2>/dev/null
        # Write G1PUBZERO at the bottom
        convert ./tmp/ZEROCARD_${G1PUBZERO}.QR.png \
          -gravity SouthWest \
          -pointsize 18 \
          -fill black \
          -annotate +2+2 "[ZEROCARD] ${G1PUBZERO}" \
          -annotate +1+3 "[ZEROCARD] ${G1PUBZERO}" \
          ./pdf/${PUBKEY}/ZEROCARD_${G1PUBZERO}.QR.jpg

################################################################# IPNS
#~ ## CREATE IPNS KEY
./tools/keygen -t ipfs -o ./tmp/${G1PUBZERO}.IPNS.key "${SALT}" "${PEPPER}"
IPNS12D=$(./tools/keygen -t ipfs "${SALT}" "${PEPPER}")
ipfs key rm ${G1PUBZERO} > /dev/null 2>&1
WALLETNS=$(ipfs key import ${G1PUBZERO} -f pem-pkcs8-cleartext ./tmp/${G1PUBZERO}.IPNS.key)

## ENCODE IPNS KEY WITH CAPTAING1PUB
echo "./tools/natools.py encrypt -p $CAPTAING1PUB -i ./tmp/${G1PUBZERO}.IPNS.key -o ./pdf/${PUBKEY}/IPNS.captain.enc"
./tools/natools.py encrypt -p $CAPTAING1PUB -i ./tmp/${G1PUBZERO}.IPNS.key -o ./pdf/${PUBKEY}/IPNS.captain.enc

## ENCRYPT WITH UPLANETNAME PASSWORD
if [[ ! -z ${UPLANETNAME} ]]; then
    rm -f ./pdf/${PUBKEY}/IPNS.uplanet.asc
    cat ./tmp/${G1PUBZERO}.IPNS.key | gpg --symmetric --armor --batch --passphrase "${UPLANETNAME}" -o ./pdf/${PUBKEY}/IPNS.uplanet.asc
fi

rm ./tmp/${G1PUBZERO}.IPNS.key
ipfs key rm ${G1PUBZERO} > /dev/null 2>&1
echo "IPNS APP KEY : $IPNS12D /ipns/ $WALLETNS"
amzqr "$IPNS12D" -l H -p ./static/img/moa_net.png -c -n ${PUBKEY}.IPNS.QR.png -d ./tmp/ 2>/dev/null
convert ./tmp/${PUBKEY}.IPNS.QR.png \
        -gravity SouthWest \
        -pointsize 18 \
        -fill black \
        -annotate +2+2 "[APP] $IPNS12D" \
        -annotate +1+3 "[APP] $IPNS12D" \
        ./pdf/${PUBKEY}/IPNS.QR.png

## Record for url linking during validation
echo "${ipfsNODE}/ipns/$WALLETNS" > ./pdf/${PUBKEY}/IPNS
echo "$IPNS12D" > ./pdf/${PUBKEY}/IPNS12D

#######################################################################
## PREPARE DISCO SHAMIR SECRET DISTRIBUTION - extend UX -
# HUMAIN = HEAD
# UPLANET = MIDDLE
# CAPTAIN = TAIL
DISCO="/?${prime}=${SALT}&${second}=${PEPPER}"
echo "SOURCE : "$DISCO

## ssss-split : Keep 2 needed over 3
echo "$DISCO" | ssss-split -t 2 -n 3 -q > ./tmp/${G1PUBZERO}.ssss
HEAD=$(cat ./tmp/${G1PUBZERO}.ssss | head -n 1) && echo "$HEAD" > ./tmp/${G1PUBZERO}.ssss.head
MIDDLE=$(cat ./tmp/${G1PUBZERO}.ssss | head -n 2 | tail -n 1) && echo "$MIDDLE" > ./tmp/${G1PUBZERO}.ssss.mid
TAIL=$(cat ./tmp/${G1PUBZERO}.ssss | tail -n 1) && echo "$TAIL" > ./tmp/${G1PUBZERO}.ssss.tail
echo "TEST DECODING..."
echo "$HEAD
$TAIL" | ssss-combine -t 2 -q
[ ! $? -eq 0 ] && echo "ERROR! SSSSKEY DECODING FAILED" && echo "./templates/wallet.html" && exit 1

##########################################################################
### CRYPTO ZONE
## ENCODE HEAD SSSS SECRET WITH MEMBER PUBKEY
echo "./tools/natools.py encrypt -p $PUBKEY -i ./tmp/${G1PUBZERO}.ssss.head -o ./pdf/${PUBKEY}/ssss.member.enc"
./tools/natools.py encrypt -p $PUBKEY -i ./tmp/${G1PUBZERO}.ssss.head -o ./pdf/${PUBKEY}/ssss.head.member.enc

## MIDDLE ENCRYPT WITH UPLANETNAME
if [[ ! -z ${UPLANETNAME} ]]; then
    rm -f ./pdf/${PUBKEY}/ssss.mid.uplanet.asc
    cat ./tmp/${G1PUBZERO}.ssss.mid | gpg --symmetric --armor --batch --passphrase "${UPLANETNAME}" -o ./pdf/${PUBKEY}/ssss.mid.uplanet.asc
    cat ./pdf/${PUBKEY}/ssss.mid.uplanet.asc | gpg -d --passphrase "${UPLANETNAME}" --batch > ./tmp/${G1PUBZERO}.ssss.test
    [[ $(diff -q ./tmp/${G1PUBZERO}.ssss.test ./tmp/${G1PUBZERO}.ssss.mid) != "" ]] && echo "ERROR: GPG ENCRYPTION FAILED "
    rm ./tmp/${G1PUBZERO}.ssss.test
fi

## ENCODE TAIL SSSS SECRET WITH CAPTAING1PUB
./tools/natools.py encrypt -p $CAPTAING1PUB -i ./tmp/${G1PUBZERO}.ssss.tail -o ./pdf/${PUBKEY}/ssss.tail.captain.enc

## REMOVE SENSIBLE DATA FROM CACHE
# DEEPER SECURITY CONCERN ? mount ./tmp as encrypted RAM disk
rm ./tmp/${G1PUBZERO}.ssss*

#### INITIALISE IPFS STORAGE ZEROCARD "BLOCK 0"
### add html page for next step...
rm -f ./pdf/${PUBKEY}/_index.html
echo "CREATION IPFS PORTAIL"

## Add Images to ipfs
MEMBERPUBQR=$(ipfs add -q ./pdf/${PUBKEY}/${PUBKEY}.UID.png)
ZWALLET=$(ipfs add -q ./pdf/${PUBKEY}/ZEROCARD_${G1PUBZERO}.QR.jpg)
echo "$ZWALLET" > ./pdf/${PUBKEY}/ZWALL

## IPFSPORTAL : ./pdf/${PUBKEY}
IPFSPORTAL=$(ipfs add -qrw ./pdf/${PUBKEY}/ | tail -n 1)
echo $IPFSPORTAL > ./pdf/${PUBKEY}/IPFSPORTAL

ipfs pin rm ${IPFSPORTAL}
echo "${ipfsNODE}/ipfs/${IPFSPORTAL}"

amzqr "${ipfsNODE}/ipfs/${IPFSPORTAL}" -l H -p ./static/img/server.png -c -n ${PUBKEY}.ipfs.png -d ./tmp/
convert ./tmp/${PUBKEY}.ipfs.png \
        -gravity SouthWest \
        -pointsize 18 \
        -fill black \
        -annotate +2+2 "[DATA] ${ipfsNODE}/ipfs/${IPFSPORTAL}" \
        -annotate +1+3 "[DATA] ${ipfsNODE}/ipfs/${IPFSPORTAL}" \
        ./pdf/${PUBKEY}/IPFSPORTAL.QR.png

IPFSPORTALQR=$(ipfs add -q ./pdf/${PUBKEY}/IPFSPORTAL.QR.png)
echo $IPFSPORTALQR > ./pdf/${PUBKEY}/IPFSPORTALQR

################################################################### ZINE
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
            -e "s~_G1PUB_~${G1PUBZERO}~g" \
            -e "s~_G1PUBZERO_~${G1PUBZERO}~g" \
            -e "s~_TOTAL_~${TOTAL}~g" \
            -e "s~_LAT_~${LAT}~g" \
            -e "s~_LON_~${LON}~g" \
            -e "s~https://ipfs.copylaradio.com~${ipfsNODE}~g" \
        > ./pdf/${PUBKEY}/_index.html

echo "./pdf/${PUBKEY}/_index.html"
#~ xdg-open ./pdf/${PUBKEY}/_index.html ## OPEN PASSPORT ON DESKTOP
[[ ! -s ./pdf/${PUBKEY}/_index.html ]] && echo "./tmp/54321.log" ## SEND LOG TO USER
exit 0
