#!/bin/bash
################################################################################
# VERIFY SHAMIR KEY CONCORDANCE
################################################################################
# Author: Fred (DsEx1pS33vzYZg4MroyBV9hCw98j1gtHEhwiZ5tK7ech)
# Version: 1.0
# License: AGPL-3.0 (https://choosealicense.com/licenses/agpl-3.0/)
###############################################################################
MY_PATH="`dirname \"$0\"`"              # relative
MY_PATH="`( cd \"$MY_PATH\" && pwd )`"  # absolutized and normalized
ME="${0##*/}"

source ${MY_PATH}/.env
[[ -z $myDUNITER ]] && myDUNITER="https://g1.cgeek.fr" # DUNITER
[[ -z $myCESIUM ]] && myCESIUM="https://g1.data.e-is.pro" # CESIUM+
[[ -z $ipfsNODE ]] && ipfsNODE="http://127.0.0.1:8080" # IPFS

# Vérifier le nombre d'arguments
if [ "$#" -ne 3 ]; then
    echo "Usage: $0 <cardns> <ssss> <zerocard>"
    exit 1
fi

# Récupération des arguments
CARDNS=$1
SSSS=$2
ZEROCARD=$3

# Définir le chemin du fichier HTML de sortie
HTML_OUTPUT="${MY_PATH}/tmp/result_${CARDNS}.html"

# QUICK Validation du SSSS 3-c9ac213472a72bfd1ea1a7780f18914.....
if [[ "$SSSS" =~ ^3-[a-f0-9]{101,} ]]; then
    VALID="VALID"
else
    VALID="INVALID"
fi

[ -z $(${MY_PATH}/tools/g1_to_ipfs.py ${ZEROCARD} 2>/dev/null) ] && VALID="INVALID"

## GET CARDNS
echo "GETTING CARDNS CARDPORTAL LOCATION.........."
CARDPORTAL=$(ipfs name resolve /ipns/${CARDNS})
if [[ ! -z $CARDPORTAL ]]; then
    echo "CARDPORTAL=$CARDPORTAL"
    LS=$(ipfs ls ${CARDPORTAL})
    [ -z $LS ] \
        && VALID="$VALID : CARDNS IS A FILE" \
        || echo VALID="$VALID : CARDNS IS A DIRECTORY ${LS}"
    ## ipfs ls... todo get html from station curl

fi

## TRY to join SSSS with CAPTAIN part
# Find CARDNS in local accounts IPNS12D
MEMBERPUB=$(grep -h -r -l --dereference "$CARDNS" ${MY_PATH}/pdf/ | grep IPNS12D | cut -d '/' -f 3)
## CAPTAIN DECRYPT MIDDLE PART ${MY_PATH}/pdf/${PUBKEY}/ssss.mid.captain.enc
${MY_PATH}/tools/natools.py decrypt -f pubsec -i ${MY_PATH}/pdf/${MEMBERPUB}/ssss.mid.captain.enc -k ~/.zen/game/players/.current/secret.dunikey -o ${MY_PATH}/tmp/${ZEROCARD}.ssss.mid
PART2=$(cat ${MY_PATH}/tmp/${ZEROCARD}.ssss.mid)
echo "SSSS + CAPTAIN PART2 COMBINE..."
disco=$(echo "$PART2
$SSSS" | ssss-combine -t 2 -q)
[ $? -eq 0 ] \
    && VALID="$VALID *** ZEROCARD CONNECTED ***" \
    || VALID="$VALID ERROR SSSS DECODING SSSS ERROR"

VALID="$VALID $disco"

## CHECK ZEROCARD SALT PEPPER

# Générer un fichier HTML de résultat
cat <<EOF > "$HTML_OUTPUT"
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SSSS Check Result</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            background-color: black;
            text-align: center;
            padding: 50px;
            color: white;
            margin: 0;
        }
        .container {
            background-color: rgba(255, 255, 255, 0.1);
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0px 0px 10px 0px #00000033;
            display: inline-block;
            max-width: 90%; /* Limite la largeur */
            margin: auto; /* Centre le conteneur */
        }
        h1 {
            color: #FFD700; /* Couleur dorée */
        }
        .result {
            font-size: 1.5em; /* Augmenter la taille de la police pour les résultats */
            margin-top: 20px;
        }
        .valid {
            color: #00FF00; /* Vert */
        }
        .invalid {
            color: #FF0000; /* Rouge */
        }
        .highlight {
            background: linear-gradient(90deg, #FF4500, #FFD700, #32CD32);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1 class="highlight">SSSS Key Validation Result</h1>
        <h2>ZEROCARD : $ZEROCARD</h2>
        <p><strong>Card NS:</strong> $CARDNS</p>
        <div class="result">
            <span class="${VALID,,}">The SSSS Key is $VALID</span>
        </div>
    </div>
</body>
</html>
EOF

# Afficher le chemin du fichier HTML en sortie pour que le script Python puisse le capturer
echo "$HTML_OUTPUT"
