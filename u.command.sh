#!/bin/bash
################################################################### u.command.sh
# Author: Fred (DsEx1pS33vzYZg4MroyBV9hCw98j1gtHEhwiZ5tK7ech)
# Version: 1.0
# License: AGPL-3.0 (https://choosealicense.com/licenses/agpl-3.0/)
################################################################################
MY_PATH="`dirname \"$0\"`"              # relative
MY_PATH="`( cd \"$MY_PATH\" && pwd )`"  # absolutized and normalized
ME="${0##*/}"
export PATH=$HOME/.astro/bin:$HOME/.local/bin:$PATH

###################################################################
## EXECUTE ZENCARD COMMANDS
## FOUND IN G1 RX "upassport.sh"
## USED BY N1App FORM API : @app.post("/sendmsg")
###################################################################

# Vérifier le nombre d'arguments
if [ $# -ne 7 ]; then
    echo "Usage: $0 <pubkey> <comment> <amount> <date> <zerocard> <zlat> <zlon>"
    exit 1
fi

source ${MY_PATH}/.env
[[ -z $myIPFS ]] && myIPFS="https://ipfs.copylaradio.com" # IPFS
MOATS=$(date -u +"%Y%m%d%H%M%S%4N")

## LOAD ASTROPORT ENVIRONMENT
[ ! -s $HOME/.zen/Astroport.ONE/tools/my.sh ] \
    && echo "ERROR/ Missing Astroport.ONE. Please install..." \
    && exit 1
. "$HOME/.zen/Astroport.ONE/tools/my.sh"

# Récupérer les arguments
PUBKEY="$1"
COMMENT="$2"
AMOUNT="$3"
DATE="$4"
ZEROCARD="$5" ## can be PUBKEY or EMAIL (N1Form)
ZLAT="$6"
ZLON="$7"

## VALID UPASSPORT ARE BECOMING LINKS TO ~/.zen/game/passport
[[ ! -L ${MY_PATH}/pdf/${PUBKEY} ]] \
    && echo "NOT A VALID UPASSPORT"\
    && echo "${MY_PATH}/static/img/money_coins.png" \
    && exit 1

# Convertir le timestamp en date lisible
READABLE_DATE=$(date -d @$DATE)
# Afficher les données reçues
echo "Date: $READABLE_DATE"
echo "Clé publique (controling member): $PUBKEY"
echo "Commentaire: $COMMENT"
echo "Montant: $AMOUNT"
echo "Date: $DATE"
echo "ZEROCARD: $ZEROCARD" # EMAIL OR MEMBERG1PUB
# If an email is received it means Member want to register or be recognized.

#########################################################################
### ZEROCARD EMAIL <=> PLAYER ACCOUNT
### PRIVILEGE ESCALADE -- PRIMAL TX with ZENCARD EMAIL IN COMMENT
isEMAIL=$(echo "$ZEROCARD" | grep -E -o "\b[a-zA-Z0-9.%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,6}\b")
if [ ! -z $isEMAIL ]; then
    echo "ZenCard challenge... $isEMAIL"
    if [ -d ~/.zen/game/players/$isEMAIL ]; then
        echo "////////////// REGISTERED ZENCARD \\\\\\\\\\\\\\"
        ~/.zen/Astroport.ONE/tools/search_for_this_email_in_players.sh "$isEMAIL"
        $(~/.zen/Astroport.ONE/tools/search_for_this_email_in_players.sh "$isEMAIL" | tail -n 1)
        ## https://opencollective.com/monnaie-libre <-> Corresponding Ẑen from "$UPLANETNAME.SOCIETY" wallet
        echo "$TODATE" > ~/.zen/game/players/$isEMAIL/U.SOCIETY
        ln -s ~/.zen/game/players/$isEMAIL/U.SOCIETY ~/.zen/game/nostr/$isEMAIL/U.SOCIETY
        ## ## ## ## UPASSPORT will receive Ẑen from "UPLANETNAME.SOCIETY" wallet 
            # Satellite = 50 Ẑ / an : https://opencollective.com/monnaie-libre/contribute/parrainage-infrastructure-extension-128-go-98386
            # PC GAMER = 540 Ẑ / 3 ans : https://opencollective.com/monnaie-libre/contribute/parrainage-infrastructure-module-gpu-1-24-98385
        ## SEND ACTIVATED UPASSPORT to PLAYER -- OC2UPLANET STUFF --
        ${HOME}/.zen/Astroport.ONE/tools/mailjet.sh "${isEMAIL}" "$HOME/.zen/game/passport/${PUBKEY}/.passport.html" "UPlanet SOCIETY Welcome"
        ###################################################################################################""
    else
        echo "$isEMAIL ZENCARD NOT FOUND ON THIS ASTROPORT..........."
        # Check actual member PLAYER file
        if [ -s ${MY_PATH}/pdf/${PUBKEY}/PLAYER ]; then
            echo "DECLARED PLAYER = $(cat ${MY_PATH}/pdf/${PUBKEY}/PLAYER) .... SEARCH IN SWARM"
            ~/.zen/Astroport.ONE/tools/search_for_this_email_in_players.sh "$isEMAIL"
            $(~/.zen/Astroport.ONE/tools/search_for_this_email_in_players.sh "$isEMAIL" | tail -n 1)
            ## WHAT TO DO NEXT ?? Same email on !=Astroport should not be possible... 

        else    
            ## In case ZENCARD is not existing : Creating it with MULTIPASS Linking - deprecated
            source ~/.zen/game/nostr/$isEMAIL/.secret.nostr 2>/dev/null
            if [[ -n "$COMMENT" && -n "$HEX" && -n "$NPUB" ]]; then
                comment="${COMMENT,,}"
                ulang="${comment:0:2}"
                echo "CREATING UPLANET PLAYER ACCOUNT : $ulang"
                ## CREATE ZENCARD VISA via ASTROPORT CLI API CMD="" THAT="" AND="" THIS="" APPNAME="" WHAT="" OBJ="" VAL=""
                # http://127.0.0.1:1234/?uplanet=dev%40g1sms.fr&zlat=0.00&zlon=0.00&g1pub=fr
                # ~/.zen/Astroport.ONE/API/UPLANET.sh uplanet dev@g1sms.fr zlat 0.00 zlon 0.00 g1pub fr 202410121305468792 123460:
                exec ~/.zen/Astroport.ONE/API/UPLANET.sh "uplanet" "$isEMAIL" "zlat" "$ZLAT" "zlon" "$ZLON" "g1pub" "${ulang}" "${HEX}" "${NPUB}" &
                ## WRITE PLAYER into ZEROCARD APP
                echo "$isEMAIL" > ${MY_PATH}/pdf/${PUBKEY}/PLAYER
            else
                echo "MISSING MULTIPASS received COMMENT=$COMMENT"
            fi
        fi

    fi
fi

#########################################################################
############# REGULAR COMMAND /sendmsg
### CHECK & UPDATE LAST COMMAND TIME
LASTCOMMANDTIME=$(cat ${MY_PATH}/pdf/${PUBKEY}/COMMANDTIME 2>/dev/null)
[ -z $LASTCOMMANDTIME ] \
    && LASTCOMMANDTIME=$DATE \
    && echo $DATE > ${MY_PATH}/pdf/${PUBKEY}/COMMANDTIME

# Vérifier si DATE est supérieur à LASTCOMMANDTIME
if [ "$DATE" -lt "$LASTCOMMANDTIME" ]; then
    echo "Erreur: La date de la commande n'est pas plus récente que la dernière commande"
    echo "${MY_PATH}/static/img/money_coins.png";
    exit 0
fi

# Vérifier le format de la clé publique
if [[ ! $PUBKEY =~ ^[A-Za-z0-9]{43,44}$ ]]; then
    echo "Erreur: Format de clé publique invalide"
    echo "${MY_PATH}/static/img/money_coins.png";
    exit 1
fi

# Vérifier que le montant est un nombre négatif
if [[ ! $AMOUNT =~ ^-[0-9]+(\.[0-9]+)?$ ]]; then
    echo "Erreur: Le montant doit être un nombre négatif"
    echo "${MY_PATH}/static/img/money_coins.png";
    exit 1
fi

# Vérifier le format de la date (timestamp Unix)
if [[ ! $DATE =~ ^[0-9]+$ ]]; then
    echo "Erreur: Format de date invalide (doit être un timestamp Unix)"
    echo "${MY_PATH}/static/img/money_coins.png";
    exit 1
fi


# Traitement spécifique selon le commentaire
case "$COMMENT" in
    "BYE")
        ZEROCARD=$(cat ${MY_PATH}/pdf/${PUBKEY}/ZEROCARD)
        echo "BYE BYE ${ZEROCARD} !"
        # UPLANETNAME Extract ZEROCARD secret
        cat ${MY_PATH}/pdf/${PUBKEY}/zerocard.planet.asc | gpg -d --passphrase "${UPLANETNAME}" --batch > ${MY_PATH}/tmp/${MOATS}.secret
        # ZEROCARD amount
        solde=$($HOME/.zen/Astroport.ONE/tools/G1check.sh ${ZEROCARD} | tail -n 1)
        echo "EMPTYING $solde G1 to ${PUBKEY}"
        # Pay Back
        $HOME/.zen/Astroport.ONE/tools/PAYforSURE.sh ${MY_PATH}/tmp/${MOATS}.secret pay ${solde} ${PUBKEY} "BYE BYE" 2>/dev/null
        [ $? -eq 0 ] \
            && rm -Rf ${MY_PATH}/pdf/${PUBKEY}/ && rm ${MY_PATH}/pdf/${PUBKEY} && rmdir ~/.zen/game/passport/${PUBKEY} \
                && echo "${MY_PATH}/static/img/nature_cloud_face.png" \
                ||  { echo "PAYMENT FAILED... retry needed"; echo "${MY_PATH}/static/img/money_coins.png"; }
        rm ${MY_PATH}/tmp/${MOATS}.secret
        exit 0
        ;;
    "MAJ")
        echo "Mise à jour MEMBER wallet AMOUNT ${PUBKEY}"
        ZEROCARD=$(cat ${MY_PATH}/pdf/${PUBKEY}/ZEROCARD)
        SOLDE=$($HOME/.zen/Astroport.ONE/tools/G1check.sh ${PUBKEY} | tail -n 1)
        AMOUNT="$SOLDE Ğ1"
        cat ${MY_PATH}/templates/message.html \
        | sed -e "s~_TITLE_~$(date -u) <br> ${PUBKEY}~g" \
             -e "s~_MESSAGE_~<a target=_new href=${myIPFS}/ipfs/$(cat ${MY_PATH}/pdf/${PUBKEY}/IPFSPORTAL)/${PUBKEY}/N1/_index.html>${AMOUNT}</a>~g" \
             -e "s~300px~303px~g" \
            > ${MY_PATH}/tmp/${ZEROCARD}.out.html

        DRIVESTATE=$(ipfs add -q ${MY_PATH}/tmp/${ZEROCARD}.out.html)
        echo "/ipfs/${DRIVESTATE}" > ${MY_PATH}/pdf/${PUBKEY}/DRIVESTATE
        ipfs name publish --key ${ZEROCARD} /ipfs/${DRIVESTATE}
        echo "${MY_PATH}/tmp/${ZEROCARD}.out.html"
        exit 0
        ;;
    *)
        echo "Envoi de $COMMENT à UPLANET"
        ##

        ;;
esac
echo "comande éxécutée avec succès"
echo $DATE > ${MY_PATH}/pdf/${PUBKEY}/COMMANDTIME
echo "${MY_PATH}/static/img/astroport.png"
exit 0
