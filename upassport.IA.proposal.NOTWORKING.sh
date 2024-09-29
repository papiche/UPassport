#!/bin/bash

################
# Configuration
################

source ./.env
[[ -z $myDUNITER ]] && myDUNITER="https://g1.cgeek.fr"
[[ -z $myCESIUM ]] && myCESIUM="https://g1.data.e-is.pro"
[[ -z $ipfsNODE ]] && ipfsNODE="http://127.0.0.1:8080"

################
# Functions
################

function error_exit() {
    echo "$1" >&2
    exit 1
}

function check_dependencies() {
    [[ ! -s $HOME/.zen/Astroport.ONE/tools/my.sh ]] && error_exit "ERROR: Missing Astroport.ONE. Please install..."
    . "$HOME/.zen/Astroport.ONE/tools/my.sh"
}

function parse_input() {
    if [ $# -ne 1 ]; then
        error_exit "Usage: $0 <PUBKEY or LINK>"
    fi

    LINK="$1"
    PUBKEY=$(echo "$LINK" | tr -d '')
    ZCHK="$(echo $PUBKEY | cut -d ':' -f 2-)"
    [[ $ZCHK == $PUBKEY ]] && ZCHK=""
    PUBKEY="$(echo $PUBKEY | cut -d ':' -f 1)"
    echo "PUBKEY? $PUBKEY"
}

function handle_link() {
    if [[ $PUBKEY == "https" ]]; then
        echo "This is a link : $LINK"
        ipns12D=$(echo "$LINK" | grep -oP "(?<=12D3Koo)[^/]*")
        if [ -z $ipns12D ]; then
            echo '' > ./tmp/${ZEROCARD}.out.html
            echo "./tmp/${ZEROCARD}.out.html"
            exit 0
        else
            process_ipns12D "$ipns12D"
        fi
    fi
}

function process_ipns12D() {
    local ipns12D="$1"
    CARDNS="12D3Koo$ipns12D"
    CARDG1=$(./tools/ipfs_to_g1.py $CARDNS)
    echo "ZEROCARD IPNS12D QRCODE : /ipns/$CARDNS ($CARDG1)"

    MEMBERPUB=$(grep -h -r -l --dereference "$CARDNS" ./pdf/ | grep IPNS12D | cut -d '/' -f 3)
    [ -z $MEMBERPUB ] && generate_error_html "ERROR --- ZEROCARD NOT FOUND --- UPassport is not registered on this Astroport. support@qo-op.com"

    ZEROCARD=$(cat ./pdf/${MEMBERPUB}/ZEROCARD)
    generate_ssss_scanner "$CARDNS" "$ZEROCARD"
}

function generate_error_html() {
    local error_message="$1"
    echo "$error_message" > ./tmp/${PUBKEY}.out.html
    echo "./tmp/${ZEROCARD}.out.html"
    exit 1
}

function generate_ssss_scanner() {
    local cardns="$1"
    local zerocard="$2"
    cat ./templates/scan_ssss.html \
    | sed -e "s~_CARDNS_~${cardns}~g" \
          -e "s~_ZEROCARD_~${zerocard}~g" \
          -e "s~https://ipfs.copylaradio.com~${ipfsNODE}~g" \
    > ./tmp/${cardns}.out.html
    echo "./tmp/${cardns}.out.html"
    exit 0
}

function check_pubkey_format() {
    if [[ -z $(./tools/g1_to_ipfs.py ${PUBKEY} 2>/dev/null) ]]; then
        generate_error_wallet "QR CODE Error Try CESIUM..."
    fi
}

function generate_error_wallet() {
    local error_message="$1"
    cat ./templates/wallet.html \
    | sed -e "s~_WALLET_~$(date -u) ${PUBKEY}~g" \
          -e "s~_AMOUNT_~${error_message}~g" \
    > ./tmp/${PUBKEY}.out.html
    echo "./tmp/${PUBKEY}.out.html"
    exit 0
}

function makecoord() {
    local input="$1"
    input=$(echo "${input}" | sed 's/\([0-9]*\.[0-9]\{2\}\).*/\1/')
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

function get_wallet_history() {
    echo "LOADING WALLET HISTORY"
    ./tools/timeout.sh -t 6 ./tools/jaklis/jaklis.py history -n 25 -p ${PUBKEY} -j > ./tmp/$PUBKEY.TX.json
    if [ ! $? -eq 0 ]; then
        update_jaklis_env
        ./tools/timeout.sh -t 6 ./tools/jaklis/jaklis.py history -n 25 -p ${PUBKEY} -j > ./tmp/$PUBKEY.TX.json
    fi
}

function update_jaklis_env() {
    sort -u -o ./tools/jaklis/.env ./tools/jaklis/.env
    GVA=$(~/.zen/Astroport.ONE/tools/duniter_getnode.sh | tail -n 1)
    [[ ! -z $GVA ]] && echo "NODE=$GVA" >> ./tools/jaklis/.env
}

function extract_wallet_info() {
    if [[ -s ./tmp/$PUBKEY.TX.json ]]; then
        SOLDE=$(./tools/timeout.sh -t 20 ./tools/jaklis/jaklis.py balance -p ${PUBKEY})
        ROUND=$(echo "$SOLDE" | cut -d '.' -f 1)
        ZEN=$(echo "($SOLDE - 1) * 10" | bc | cut -d '.' -f 1)
        [[ "$(echo "$ROUND < 100" | bc)" == 1 ]] && ROUND=100
        AMOUNT="$SOLDE Ğ1"
        [[ $SOLDE == "null" ]] && AMOUNT="EMPTY" && ROUND=200
        [[ $SOLDE == "" ]] && AMOUNT="TIMEOUT" && ROUND=200
        [[ $ZCHK == "ZEN" ]] && AMOUNT="$ZEN ẑ€N"
    else
        generate_error_wallet "CRITICAL WALLET ERROR"
    fi
    echo "$AMOUNT G1 ($ZCHK) $ZEN ẑ€N"
    echo "------------------------------------- $ROUND -"
}

function process_zerocard() {
    if [[ -s ./pdf/${PUBKEY}/ZEROCARD ]]; then
        ZEROCARD=$(cat ./pdf/${PUBKEY}/ZEROCARD)
        echo "G1 ZEROCARD FOUND: ${ZEROCARD}"
        process_last_transaction
    else
        echo "........... ZEROCARD NOT ACTIVATED YET"
        check_activation_tx
    fi
}

function process_last_transaction() {
    LASTX=$(jq '.[-1] | .amount' ./tmp/$PUBKEY.TX.json)
    DEST=$(jq -r '.[-1] | .pubkey' ./tmp/$PUBKEY.TX.json)
    COMM=$(jq -r '.[-1] | .comment' ./tmp/$PUBKEY.TX.json)
    TXDATE=$(jq -r '.[-1] | .date' ./tmp/$PUBKEY.TX.json)

    check_astroport
    handle_upassport_state
}

function check_astroport() {
    if [[ -s ./pdf/${PUBKEY}/ASTROPORT ]]; then
        echo "ASTROPORT Web3 Ambassy : $(cat ./pdf/${PUBKEY}/ASTROPORT)"
    else
        echo $IPFSNODEID > ./pdf/${PUBKEY}/ASTROPORT
    fi
}

function handle_upassport_state() {
    if [ -L "./pdf/${PUBKEY}" ]; then
        if [[ $COMM != "" && "$DEST" == "$ZEROCARD" ]]; then
            ./command.sh "$PUBKEY" "$COMM" "$LASTX" "$TXDATE" "$ZEROCARD"
            [[ ! $? -eq 0 ]] && echo ">>>>>>>>>>>> ERROR"
        fi
        check_drivestate
    else
        initialize_zerocard
    fi
}

function check_drivestate() {
    if [[ -s ./pdf/${PUBKEY}/DRIVESTATE ]]; then
        echo '' > ./tmp/${ZEROCARD}.out.html
        echo "./tmp/${ZEROCARD}.out.html"
        exit 0
    else
        initialize_zerocard
    fi
}

function initialize_zerocard() {
    CODEINJECT="${AMOUNT}"
    cat ./templates/wallet.html \
    | sed -e "s~_WALLET_~$(date -u) ${PUBKEY}~g" \
          -e "s~_AMOUNT_~${CODEINJECT}~g" \
          -e "s~300px~501px~g" \
    > ./pdf/${PUBKEY}/_index.html

    echo ${PUBKEY} > ./pdf/${PUBKEY}/PUBKEY
    DRIVESTATE=$(ipfs add -qwr ./pdf/${PUBKEY}/* | tail -n 1)
    echo "/ipfs/${DRIVESTATE}" > ./pdf/${PUBKEY}/DRIVESTATE
    ipfs name publish --key ${ZEROCARD} /ipfs/${DRIVESTATE}
    echo "./tmp/${ZEROCARD}.out.html"
    exit 0
}

function check_activation_tx() {
    if [ "$(echo "$LASTX < 0" | bc)" -eq 1 ]; then
        echo "TX: $DEST ($COMM)"
        if [[ "$ZEROCARD" == "$DEST" ]]; then
            activate_zerocard
        fi
    fi
}

function activate_zerocard() {
    echo "MATCHING!! ZEROCARD INITIALISATION..."
    echo "$TXDATE" > ./pdf/${PUBKEY}/COMMANDTIME
    process_avatar
    # Add more activation steps here
}

function process_avatar() {
    if [[ -s ./pdf/${PUBKEY}/AVATAR.png ]]; then
        convert ./pdf/${PUBKEY}/AVATAR.png -rotate +90 -resize 120x120 ./tmp/${PUBKEY}.AVATAR_rotated.png
        composite -gravity NorthEast -geometry +16+16 ./tmp/${PUBKEY}.AVATAR_rotated.png ./pdf/${PUBKEY}/page2.png ./pdf/${PUBKEY}/page2.png
    fi
    # Add more avatar processing steps here
}

################
# Main Execution
################

parse_input "$@"
handle_link
check_pubkey_format
check_dependencies

mkdir -p ./tmp
find ./tmp -mtime +1 -type f -exec rm '{}' \;

get_wallet_history
extract_wallet_info

process_zerocard

# End of script
