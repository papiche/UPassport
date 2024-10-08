#!/bin/bash

# Vérifier le nombre d'arguments
if [ $# -ne 5 ]; then
    echo "Usage: $0 <pubkey> <comment> <amount> <date> <zerocard>"
    exit 1
fi
source ./.env
[[ -z $ipfsNODE ]] && ipfsNODE="https://ipfs.astroport.com" # IPFS
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
ZEROCARD="$5" ## PUBKEY or EMAIL

[[ ! -L ./pdf/${PUBKEY} ]] \
    && echo "NOT A VALID UPASSPORT"\
    && echo "./static/img/money_coins.png" \
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
# If an email is received it means Member want to

### ZENCARD
### PRIVILEGE ESCALADE
isEMAIL=$(echo "$ZEROCARD" | grep -E -o "\b[a-zA-Z0-9.%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,6}\b")
[ ! -s $isEMAIL ] \
    && echo "ZenCard chalenge... $isEMAIL" \
    && [ -d ~/.zen/game/players/$isEMAIL ] \
        && echo "////////////// REGISTERED ZENCARD \\\\\\\\\\\\\\" \
        && ~/.zen/Astroport.ONE/tools/search_for_this_email_in_players.sh "$isEMAIL"

### CHECK LAST COMMAND TIME
LASTCOMMANDTIME=$(cat ./pdf/${PUBKEY}/COMMANDTIME 2>/dev/null)
[ -z $LASTCOMMANDTIME ] \
    && LASTCOMMANDTIME=$DATE \
    && echo $DATE > ./pdf/${PUBKEY}/COMMANDTIME

# Vérifier si DATE est supérieur à LASTCOMMANDTIME
if [ "$DATE" -lt "$LASTCOMMANDTIME" ]; then
    echo "Erreur: La date de la commande n'est pas plus récente que la dernière commande"
    echo "./static/img/money_coins.png";
    exit 0
fi

# Vérifier le format de la clé publique
if [[ ! $PUBKEY =~ ^[A-Za-z0-9]{43,44}$ ]]; then
    echo "Erreur: Format de clé publique invalide"
    echo "./static/img/money_coins.png";
    exit 1
fi

# Vérifier que le montant est un nombre négatif
if [[ ! $AMOUNT =~ ^-[0-9]+(\.[0-9]+)?$ ]]; then
    echo "Erreur: Le montant doit être un nombre négatif"
    echo "./static/img/money_coins.png";
    exit 1
fi

# Vérifier le format de la date (timestamp Unix)
if [[ ! $DATE =~ ^[0-9]+$ ]]; then
    echo "Erreur: Format de date invalide (doit être un timestamp Unix)"
    echo "./static/img/money_coins.png";
    exit 1
fi


# Traitement spécifique selon le commentaire
case "$COMMENT" in
    "BYE")
        ZEROCARD=$(cat ./pdf/${PUBKEY}/ZEROCARD)
        echo "BYE BYE ${ZEROCARD} !"
        # UPLANETNAME Extract ZEROCARD secret
        cat ./pdf/${PUBKEY}/zerocard.planet.asc | gpg -d --passphrase "${UPLANETNAME}" --batch > ./tmp/${MOATS}.secret
        # ZEROCARD amount
        solde=$(./tools/timeout.sh -t 5 ./tools/jaklis/jaklis.py balance -p ${ZEROCARD})
        echo "EMPTYING $solde G1 to ${PUBKEY}"
        # Pay Back
        ./tools/timeout.sh -t 5 ./tools/jaklis/jaklis.py -k ./tmp/${MOATS}.secret pay -a ${solde} -p ${PUBKEY} -c "BYE" -m
        [ $? -eq 0 ] \
            && rm -Rf ./pdf/${PUBKEY}/ && rm ./pdf/${PUBKEY} && rmdir ~/.zen/game/passport/${PUBKEY} \
                && echo "./static/img/nature_cloud_face.png" \
                ||  { echo "PAYMENT FAILED... retry needed"; echo "./static/img/money_coins.png"; }
        rm ./tmp/${MOATS}.secret
        exit 0
        ;;
    "MAJ")
        echo "Mise à jour MEMBER wallet AMOUNT ${PUBKEY}"
        ZEROCARD=$(cat ./pdf/${PUBKEY}/ZEROCARD)
        SOLDE=$(./tools/timeout.sh -t 6 ./tools/jaklis/jaklis.py balance -p ${PUBKEY})
        AMOUNT="$SOLDE Ğ1"
        cat ./templates/wallet.html \
        | sed -e "s~_WALLET_~$(date -u) <br> ${PUBKEY}~g" \
             -e "s~_AMOUNT_~<a target=_new href=${ipfsNODE}/ipfs/$(cat ./pdf/${PUBKEY}/IPFSPORTAL)/${PUBKEY}/_index.html>${AMOUNT}</a>~g" \
             -e "s~300px~303px~g" \
            > ./tmp/${ZEROCARD}.out.html

        DRIVESTATE=$(ipfs add -q ./tmp/${ZEROCARD}.out.html)
        echo "/ipfs/${DRIVESTATE}" > ./pdf/${PUBKEY}/DRIVESTATE
        ipfs name publish --key ${ZEROCARD} /ipfs/${DRIVESTATE}
        echo "./tmp/${ZEROCARD}.out.html"
        exit 0
        ;;
    *)
        echo "Envoi de $COMMENT à IA"

        ;;
esac
echo "comande éxécutée avec succès"
echo $DATE > ./pdf/${PUBKEY}/COMMANDTIME
echo "./static/img/astroport.png"
exit 0
