#!/bin/bash
MY_PATH="`dirname \"$0\"`"              # relative
MY_PATH="`( cd \"$MY_PATH\" && pwd )`"  # absolutized and normalized
if [ $# -ne 1 ]; then
    echo "Usage: $0 <pubkey>"
    exit 1
fi

PUBKEY=$1

mkdir -p $MY_PATH/tmp

RESPONSE=$(wget -q -O ./tmp/me.json https://g1.cgeek.fr/wot/lookup/$PUBKEY)
CERTIFIED=$(cat ./tmp/me.json | jq '.results[0].signed[] | .uid')
## jq '.results[].signed[] | {uid, pubkey}'
echo "Certifiés :"
echo "$CERTIFIED"

CERTIFIERS=$(wget -q -O ./tmp/them.json "https://g1.cgeek.fr/wot/certifiers-of/$PUBKEY?pubkey=true")

if [ $? -ne 0 ]; then
    echo "Erreur lors de la recherche des certificateurs : $?"
    exit 1
fi

# Extraire les pubkey certifiantes
PUBKEY_CERTIFIANTES=$(cat ./tmp/them.json | jq -r '.certifications[].pubkey')

if [ -z "$PUBKEY_CERTIFIANTES" ]; then
    echo "Aucun certifiant trouvé pour le pubkey $PUBKEY"
    exit 1
fi

echo "Pubkey certifiantes :"
echo "$PUBKEY_CERTIFIANTES"
