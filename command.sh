#!/bin/bash

# Vérifier le nombre d'arguments
if [ $# -ne 4 ]; then
    echo "Usage: $0 <pubkey> <comment> <amount> <date>"
    exit 1
fi
source ./.env
[[ -z $ipfsNODE ]] && ipfsNODE="https://ipfs.astroport.com" # IPFS

# Récupérer les arguments
PUBKEY="$1"
COMMENT="$2"
AMOUNT="$3"
DATE="$4"

# Afficher les données reçues
echo "Données reçues:"
echo "Clé publique: $PUBKEY"
echo "Commentaire: $COMMENT"
echo "Montant: $AMOUNT"
echo "Date: $DATE"

### CHECK LAST COMMAND TIME
LASTCOMMANDTIME=$(cat ~/.zen/game/passport/${PUBKEY}/COMMANDTIME 2>/dev/null)
[ -z $LASTCOMMANDTIME ] \
    && echo $DATE > ~/.zen/game/passport/${PUBKEY}/COMMANDTIME && LASTCOMMANDTIME=$DATE ## INITIALISE

# Vérifier si DATE est supérieur à LASTCOMMANDTIME
if [ "$DATE" -lt "$LASTCOMMANDTIME" ]; then
    echo "Erreur: La date de la commande n'est pas plus récente que la dernière commande"
    exit 0
fi

# Vérifier le format de la clé publique
if [[ ! $PUBKEY =~ ^[A-Za-z0-9]{43,44}$ ]]; then
    echo "Erreur: Format de clé publique invalide"
    exit 1
fi

# Vérifier que le montant est un nombre négatif
if [[ ! $AMOUNT =~ ^-[0-9]+(\.[0-9]+)?$ ]]; then
    echo "Erreur: Le montant doit être un nombre négatif"
    exit 1
fi

# Vérifier le format de la date (timestamp Unix)
if [[ ! $DATE =~ ^[0-9]+$ ]]; then
    echo "Erreur: Format de date invalide (doit être un timestamp Unix)"
    exit 1
fi

# Convertir le timestamp en date lisible
READABLE_DATE=$(date -d @$DATE)
echo "Date lisible: $READABLE_DATE"

# Traitement spécifique selon le commentaire
case "$COMMENT" in
    "INIT")
        echo "Initialisation détectée"
        # Ajouter ici le code pour l'initialisation
        ;;
    "UPDATE")
        echo "Mise à jour wallet détectée"
        SOLDE=$(./tools/timeout.sh -t 6 ./tools/jaklis/jaklis.py balance -p ${PUBKEY})
        AMOUNT="$SOLDE Ğ1"
        cat ./templates/wallet.html \
        | sed -e "s~_WALLET_~$(date -u) <br> ${PUBKEY}~g" \
             -e "s~_AMOUNT_~<a target=_new href=${ipfsNODE}/ipfs/$(cat ./pdf/${PUBKEY}/IPFSPORTAL)/${PUBKEY}/_index.html>${AMOUNT}</a>~g" \
             -e "s~300px~303px~g" \
            > ./tmp/${ZEROCARD}.out.html

        ASTATE=$(ipfs add -q ./tmp/${ZEROCARD}.out.html)
        echo "/ipfs/${ASTATE}" > ./pdf/${PUBKEY}/ASTATE
        ipfs name publish --key ${ZEROCARD} /ipfs/${ASTATE}
        echo "./tmp/${ZEROCARD}.out.html"
        exit 0
        ;;
    *)
        echo "Commentaire non reconnu: $COMMENT"
        ;;
esac

echo "Vérification terminée avec succès"
