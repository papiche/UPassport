################################################################################
# Author: Fred (DsEx1pS33vzYZg4MroyBV9hCw98j1gtHEhwiZ5tK7ech)
# Version: 1.0
# License: AGPL-3.0 (https://choosealicense.com/licenses/agpl-3.0/)
################################################################################
MY_PATH="`dirname \"$0\"`"              # relative
MY_PATH="`( cd \"$MY_PATH\" && pwd )`"  # absolutized and normalized
ME="${0##*/}"
## LOAD ASTROPORT ENVIRONMENT
[ ! -s $HOME/.zen/Astroport.ONE/tools/my.sh ] \
    && echo "<h1>ERROR/ Missing Astroport.ONE. Please install...<h1>" \
    && exit 1
. "$HOME/.zen/Astroport.ONE/tools/my.sh"

export PATH=$HOME/.astro/bin:$HOME/.local/bin:$PATH
# EMpty & Remove Fac Simile ZEROCARD
PUBKEY=$1

[[ -l ${MY_PATH}/pdf/${PUBKEY} ]] && echo "UPLANET ACTIVATED ACCOUNT" && exit 1
[[ ! -d ${MY_PATH}/pdf/${PUBKEY} ]] && echo "UNKNOWN ACCOUNT" && exit 1

echo "BYE BYE ZEROCARD !"
# UPLANETNAME Extract ZEROCARD secret
cat ${MY_PATH}/pdf/${PUBKEY}/zerocard.planet.asc | gpg -d --passphrase "${UPLANETNAME}" --batch > ${MY_PATH}/tmp/${MOATS}.secret
# ZEROCARD amount
solde=$(${MY_PATH}/tools/timeout.sh -t 5 ${MY_PATH}/tools/jaklis/jaklis.py balance -p ${ZEROCARD})
echo "EMPTYING $solde G1 to ${PUBKEY}"
# Pay Back
${MY_PATH}/tools/timeout.sh -t 5 ${MY_PATH}/tools/jaklis/jaklis.py -k ${MY_PATH}/tmp/${MOATS}.secret pay -a ${solde} -p ${PUBKEY} -c "BYE" -m
[ $? -eq 0 ] \
    && rm -Rf ${MY_PATH}/pdf/${PUBKEY}/ && rm ${MY_PATH}/pdf/${PUBKEY} && rmdir ~/.zen/game/passport/${PUBKEY} \
        && echo "${MY_PATH}/static/img/nature_cloud_face.png" \
        ||  { echo "PAYMENT FAILED... retry needed"; echo "${MY_PATH}/static/img/money_coins.png"; }
rm ${MY_PATH}/tmp/${MOATS}.secret
