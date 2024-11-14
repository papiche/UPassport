#!/bin/bash
################################################################################
# VERIFY SHAMIR KEY CONCORDANCE and connect player
################################################################################
# Author: Fred (DsEx1pS33vzYZg4MroyBV9hCw98j1gtHEhwiZ5tK7ech)
# Version: 1.0
# License: AGPL-3.0 (https://choosealicense.com/licenses/agpl-3.0/)
###############################################################################
MY_PATH="`dirname \"$0\"`"              # relative
MY_PATH="`( cd \"$MY_PATH\" && pwd )`"  # absolutized and normalized
ME="${0##*/}"
MOATS=$(date -u +"%Y%m%d%H%M%S%4N")
export PATH=$HOME/.astro/bin:$HOME/.local/bin:$PATH

source ${MY_PATH}/.env
[[ -z $myDUNITER ]] && myDUNITER="https://g1.cgeek.fr" # DUNITER
[[ -z $myCESIUM ]] && myCESIUM="https://g1.data.e-is.pro" # CESIUM+
[[ -z $ipfsNODE ]] && ipfsNODE="http://127.0.0.1:8080" # IPFS

# Vérifier le nombre d'arguments
if [ "$#" -ne 3 ]; then
    echo "Usage: $0 <zen> <g1source> <g1dest>"
    exit 1
fi

# Récupération des arguments
ZEN=$1
G1SOURCE=$2
G1DEST=$3

# Définir le chemin du fichier HTML de sortie
HTML_OUTPUT="${MY_PATH}/tmp/result_${MOATS}.html"

# Générer un fichier HTML de résultat
cat <<EOF > "$HTML_OUTPUT"
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ZEN SEND Result</title>
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
        <p>$G1SOURCE<strong></strong></p> send
        <h1 class="highlight">$ZEN ẐEN</h1>
        <div class="result">
            <span class="${VALID,,}"> to </span>
        </div>
        <h2>$G1DEST</h2>
    </div>
</body>
</html>
EOF

# Afficher le chemin du fichier HTML en sortie pour que le script Python puisse le capturer
echo "$HTML_OUTPUT"
exit 0
