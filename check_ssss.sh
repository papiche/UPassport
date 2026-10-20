#!/bin/bash
################################################################################
# check_ssss.sh - VERIFY SHAMIR KEY CONCORDANCE & HYDRATE ROAMING PLAYER
################################################################################
# Author: Fred (support@qo-op.com)
# Version: 2.0 (Roaming & IPNS resolution supported)
# License: AGPL-3.0 (https://choosealicense.com/licenses/agpl-3.0/)
###############################################################################
MY_PATH="`dirname \"$0\"`"              # relative
MY_PATH="`( cd \"$MY_PATH\" && pwd )`"  # absolutized and normalized
ME="${0##*/}"
MOATS=$(date -u +"%Y%m%d%H%M%S%4N")
export PATH=$HOME/.astro/bin:$HOME/.local/bin:$PATH

## LOAD ASTROPORT ENVIRONMENT
[ ! -s $HOME/.zen/Astroport.ONE/tools/my.sh ] \
    && echo "ERROR/ Missing Astroport.ONE. Please install..." \
    && exit 1
. "$HOME/.zen/Astroport.ONE/tools/my.sh"

# Vérifier le nombre d'arguments
if [ "$#" -lt 2 ]; then
    echo "Usage: $0 <cardns> <ssss> [zerocard] [json]"
    echo "  json : la dernière ligne de sortie est un JSON {valid, email, hex | reason} (onglet « Déménager » de /scan)"
    exit 1
fi

CARDNS=$1
SSSS=$2
ZEROCARD=${3:-$MOATS}
OUTPUT_MODE=${4:-html}
# La part P1 est un facteur secret : "-" = la lire sur l'entrée standard (jamais dans `ps`)
[[ "$SSSS" == "-" ]] && read -r SSSS

# Formater le CARDNS proprement
[[ "$CARDNS" != "/ipns/"* ]] && CARDNS="/ipns/$CARDNS"

mkdir -p ${HOME}/.zen/tmp
HTML_OUTPUT="${HOME}/.zen/tmp/result_${MOATS}.html"
VALID="INVALID"
DISCO=""
EMAIL=""


# 1. Validation de format SSSS
if [[ "$SSSS" =~ ^[1-3]-[a-f0-9]{100,} ]]; then
    
    # 2. Résoudre l'IPNS sur le réseau IPFS
    echo "Resolving IPNS ${CARDNS} on IPFS network... (Timeout 30s)" >&2
    RESOLVED_CID=$(ipfs name resolve --timeout=30s "$CARDNS" 2>/dev/null)

    if [[ -n "$RESOLVED_CID" ]]; then
        echo "Resolved to CID: $RESOLVED_CID" >&2
        
        # 3. Extraire l'Email à partir de l'arborescence IPFS
        # L'arborescence est /ipfs/CID/email@domaine.com/
        EMAIL=$(ipfs ls "$RESOLVED_CID" 2>/dev/null | grep "@" | awk '{print $NF}' | tr -d '/')
        
        if [[ -n "$EMAIL" ]]; then
            echo "Found MULTIPASS identity: $EMAIL" >&2

            # GARDE : un compte DÉJÀ HÉBERGÉ ici (clés présentes, pas de marqueur .roaming) n'est jamais « hydraté » :
            # on réécrirait ses fichiers de clés et on poserait .roaming — le compte serait alors ignoré par les
            # rafraîchissements puis PURGÉ au bout de 24 h par NODE.refresh.sh.
            if [[ -s "$HOME/.zen/game/nostr/$EMAIL/.secret.nostr" && ! -f "$HOME/.zen/game/nostr/$EMAIL/.roaming" ]]; then
                VALID="INVALID (account already hosted on this station)"
                EMAIL_HOSTED_HERE=1
            fi
        fi
        if [[ -n "$EMAIL" && -z "$EMAIL_HOSTED_HERE" ]]; then

            # 4. Télécharger la part SSSS UPlanet distante
            REMOTE_TAIL_PATH="$RESOLVED_CID/$EMAIL/ssss.tail.uplanet.enc"
            TEMP_TAIL_ENC="${HOME}/.zen/tmp/${MOATS}_tail.enc"
            TEMP_TAIL_DEC="${HOME}/.zen/tmp/${MOATS}_tail.dec"

            echo "Downloading UPlanet SSSS share..." >&2
            if ipfs cat "$REMOTE_TAIL_PATH" > "$TEMP_TAIL_ENC" 2>/dev/null; then
                
                # 5. Déchiffrer avec la clé UPlanet locale (partagée par l'essaim)
                $HOME/.zen/Astroport.ONE/tools/natools.py decrypt -f pubsec \
                    -i "$TEMP_TAIL_ENC" \
                    -k ~/.zen/game/uplanet.dunikey \
                    -o "$TEMP_TAIL_DEC" 2>/dev/null

                if [[ -s "$TEMP_TAIL_DEC" ]]; then
                    PART_UPLANET=$(cat "$TEMP_TAIL_DEC")
                    
                    # 6. Recombiner les 2 parts (Tête du joueur + Queue UPlanet)
                    echo "Combining SSSS parts..." >&2
                    DISCO=$(echo -e "$SSSS\n$PART_UPLANET" | ssss-combine -t 2 -q 2>&1 | tail -n 1)

                    # 7. Vérifier si le DISCO est valide
                    if [[ "$DISCO" == *"salt="* && "$DISCO" == *"nostr="* ]]; then
                        VALID="VALID"
                        
                        # ====================================================================
                        # HYDRATATION DU PROFIL LOCAL EN MODE ITINÉRANT (ROAMING)
                        # ====================================================================
                        echo "Reconstructing keys for Roaming..." >&2
                        # Rien n'est écrit dans ~/.zen/game/nostr/ tant que les clés ne sont pas
                        # validées : un échec ne doit ni laisser un DISCO ni écraser un compte existant.

                        # Extraire salt et pepper (compatible avec make_NOSTRCARD.sh)
                        # 1. On enlève le préfixe /?salt=
                        tmp="${DISCO#/?salt=}"
                        # 2. On extrait le salt (tout ce qui précède le '&nostr=')
                        salt="${tmp%%&nostr=*}"
                        # 3. On extrait le pepper (tout ce qui suit le '&nostr=')
                        pepper="${tmp#*&nostr=}"

                        # Reconstruire les clés : CHACUNE avec la formule de crédentiel qui reproduit sa
                        # référence PUBLIÉE sur le NOSTRNS (HEX, G1PUBNOSTR, id IPNS) — formule isolée par
                        # UPlanet (SALT, PEPPER_<sel UPlanet>) d'abord, ancienne formule seulement si c'est
                        # elle qui a créé ce compte. Aucune référence reproduite = on s'arrête : jamais un
                        # wallet ou une clé IPNS « proches » (cf. my.sh::derive_account_keys).
                        REF_HEX=$(ipfs cat --timeout=30s "$RESOLVED_CID/$EMAIL/HEX" 2>/dev/null | tr -d '[:space:]')
                        REF_G1=$(ipfs cat --timeout=30s "$RESOLVED_CID/$EMAIL/G1PUBNOSTR" 2>/dev/null | tr -d '[:space:]')
                        ROAMING_DIR="$HOME/.zen/game/nostr/$EMAIL"
                        KEYS_TMP=$(mktemp -d)
                        if [[ -n "$REF_HEX" && -n "$REF_G1" ]] \
                           && derive_account_keys "$KEYS_TMP" "$salt" "$pepper" "$REF_HEX" "$REF_G1" "$CARDNS" >&2; then
                            mkdir -p "$ROAMING_DIR"
                            cp -a "$KEYS_TMP"/. "$ROAMING_DIR"/
                            echo "$DISCO" > "$ROAMING_DIR/.secret.disco"
                            chmod 600 "$ROAMING_DIR/.secret.disco"
                            G1PUB=$(cat "$ROAMING_DIR/G1PUBNOSTR")
                            echo "Key formulas: $(cat "$ROAMING_DIR/.keyformula")" >&2

                            # Importer la clé IPNS dans le démon IPFS local pour pouvoir republier
                            ipfs key rm "${G1PUB}:NOSTR" >/dev/null 2>&1 || true
                            ipfs key import "${G1PUB}:NOSTR" -f pem-pkcs8-cleartext "$ROAMING_DIR/.secret.ipns" >/dev/null 2>&1
                        else
                            VALID="INVALID (keys do not match the published identity)"
                        fi
                        rm -rf "$KEYS_TMP"
                    fi

                    if [[ "$VALID" == "VALID" ]]; then

                        # 🔥 CRÉATION DU MARQUEUR DE ROAMING 🔥
                        # Ce fichier indique à ZEN.ECONOMY.sh et PLAYER.refresh.sh d'ignorer ce joueur
                        touch "$ROAMING_DIR/.roaming"
                        chmod 600 "$ROAMING_DIR/.roaming"

                        # Pré-téléchargement asynchrone du uDRIVE depuis la home station.
                        # Ne bloque pas la réponse SSSS ; NOSTRCARD.refresh.sh publie ensuite.
                        (
                            _udrive_cid=$(ipfs resolve -r --timeout=20s "${CARDNS}/${EMAIL}/APP/uDRIVE" 2>/dev/null | sed 's|/ipfs/||')
                            if [[ -n "$_udrive_cid" ]]; then
                                mkdir -p "$ROAMING_DIR/APP"
                                if timeout 60 ipfs get "/ipfs/$_udrive_cid" \
                                        -o "$ROAMING_DIR/APP/uDRIVE" 2>/dev/null; then
                                    echo "$_udrive_cid" > "$ROAMING_DIR/.udrive"
                                    touch "$ROAMING_DIR/.udrive"  # .udrive > uDRIVE/ mtime
                                fi
                            fi
                        ) >/dev/null 2>&1 &    # sortie détachée : sinon le tube de l'appelant reste ouvert jusqu'à 60 s
                                               # et des lignes arrivent APRÈS le JSON (mode json)

                        echo "Hydration complete. Welcome $EMAIL." >&2
                    elif [[ "$VALID" == "INVALID" ]]; then
                        VALID="INVALID (SSSS mismatch)"
                    fi
                else
                    VALID="INVALID (Cannot decrypt UPlanet share)"
                fi
            else
                VALID="INVALID (Cannot download remote share)"
            fi
        elif [[ -z "$EMAIL" ]]; then
            VALID="INVALID (Email directory not found in IPNS)"
        fi
    else
        VALID="INVALID (Cannot resolve IPNS. Network timeout)"
    fi
else
    VALID="INVALID (Format SSSS incorrect)"
fi

# Nettoyage
rm -f ${HOME}/.zen/tmp/${MOATS}_*

# Mode JSON (onglet « Déménager » de /scan) : dernière ligne = JSON, pas de page HTML
if [[ "$OUTPUT_MODE" == "json" ]]; then
    if [[ "$VALID" == "VALID" ]]; then
        jq -nc --arg email "$EMAIL" --arg hex "$(cat "$ROAMING_DIR/HEX" 2>/dev/null)" '{valid:true,email:$email,hex:$hex}'
        exit 0
    fi
    jq -nc --arg reason "$VALID" '{valid:false,reason:$reason}'
    exit 1
fi

# Générer le fichier HTML de résultat
cat <<EOF > "$HTML_OUTPUT"
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SSSS Roaming Connection</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            background-color: #0f172a;
            text-align: center;
            padding: 50px;
            color: #f8fafc;
            margin: 0;
        }
        .container {
            background-color: rgba(30, 41, 59, 0.8);
            padding: 30px;
            border-radius: 12px;
            box-shadow: 0px 4px 20px 0px rgba(0, 0, 0, 0.5);
            display: inline-block;
            max-width: 600px;
            margin: auto;
        }
        h1 { color: #38bdf8; }
        .result {
            font-size: 1.3em;
            margin-top: 20px;
            padding: 15px;
            border-radius: 8px;
            background: rgba(0,0,0,0.3);
        }
        .VALID { color: #4ade80; border-left: 4px solid #4ade80; }
        .INVALID { color: #f87171; border-left: 4px solid #f87171; }
        .highlight {
            background: linear-gradient(90deg, #38bdf8, #818cf8);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .roaming-badge {
            display: inline-block;
            background: #8b5cf6;
            color: white;
            padding: 4px 10px;
            border-radius: 20px;
            font-size: 0.8em;
            font-weight: bold;
            margin-top: 10px;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1 class="highlight">Astroport Roaming Access</h1>
        <p><strong>Identity:</strong> ${EMAIL:-"Unknown"}</p>
        <p style="font-size: 0.8em; color: #94a3b8; word-break: break-all;"><strong>IPNS:</strong> $CARDNS</p>
        
        <div class="result ${VALID%% *}">
            Status: <strong>$VALID</strong>
        </div>
        
        $(if [[ "$VALID" == "VALID" ]]; then echo '<div class="roaming-badge">✈️ Roaming Mode Active</div><p style="font-size:0.9em; margin-top:20px; color:#cbd5e1;">Your private keys have been temporarily reconstructed on this station. You can now securely upload large files and execute Ğ1 transactions.</p>'; fi)
    </div>
</body>
</html>
EOF

# Afficher le chemin du fichier HTML en sortie (dernière ligne capturée par FastAPI)
echo "$HTML_OUTPUT"

# Renvoyer un code d'erreur si invalide (permet au python de gérer l'erreur)
[[ "$VALID" != "VALID" ]] && exit 1
exit 0