#!/bin/bash
##################################################################### billet_gen.sh
################################################################################
# Author: Fred (support@qo-op.com)
# License: AGPL-3.0 (https://choosealicense.com/licenses/agpl-3.0/)
#
# Génère une paire de clés G1/duniter jetable pour un "Ğ1Billet papier"
# imprimable (cf. UPassport/routers/qr.py::/qr/billet). Le secret est une
# phrase mnémonique BIP39 standard (12 mots, anglais) — dérivation IDENTIQUE
# à UPlanet/earth/keygen.html (onglet "Mnemonic (v2)") : un billet peut donc
# être restauré en tapant sa phrase dans keygen.html, sans dépendre de ce
# script. Vérifié par test croisé (même mnemonic → même G1PUB des deux côtés).
#
# Tout se passe en RAM (/dev/shm) : ni le mnemonic, ni le fichier .dunikey ne
# sont jamais écrits sur disque persistant. Le secret n'existe qu'une fois,
# dans la sortie JSON de ce script — à imprimer/afficher une seule fois puis
# oublier.
#
# Usage: ./billet_gen.sh
# Sortie (une seule ligne JSON) : {"g1pub": "...", "mnemonic": "..."}
###############################################################################
MY_PATH="`dirname \"$0\"`"
MY_PATH="`( cd \"$MY_PATH\" && pwd )`"
export PATH=$HOME/.astro/bin:$HOME/.local/bin:$PATH

ASTRTOOLS="${HOME}/.zen/Astroport.ONE/tools"

# Génère la phrase BIP39 (12 mots, 128 bits) + dérive le seed Ed25519 32 octets
# selon le standard BIP39 (PBKDF2-HMAC-SHA512, "mnemonic"+passphrase, 2048
# tours) — même algorithme que bip39-libs.js::mnemonicToSeedHex côté navigateur.
_PY_OUT=$(python3 -c "
from mnemonic import Mnemonic
m = Mnemonic('english')
phrase = m.generate(strength=128)
seed = Mnemonic.to_seed(phrase, passphrase='')
print(phrase)
print(seed[:32].hex())
" 2>/dev/null)

MNEMONIC=$(echo "$_PY_OUT" | sed -n '1p')
SEEDHEX=$(echo "$_PY_OUT" | sed -n '2p')

if [[ -z "$MNEMONIC" || -z "$SEEDHEX" ]]; then
    echo "{\"error\": \"mnemonic generation failed\"}"
    exit 1
fi

_CRED=$(mktemp -p /dev/shm 2>/dev/null || mktemp)
_DUNIKEY=$(mktemp -p /dev/shm 2>/dev/null || mktemp)
chmod 600 "$_CRED" "$_DUNIKEY"
trap "rm -f '$_CRED' '$_DUNIKEY'" EXIT INT TERM

# "keygen -i" autodétecte un seed hex 64 caractères (format "seed") — dérivation
# Ed25519 directe depuis le seed, sans KDF supplémentaire, donc bit-à-bit
# identique à nacl.sign.keyPair.fromSeed() côté keygen.html.
printf '%s\n' "$SEEDHEX" > "$_CRED"

"${ASTRTOOLS}/keygen" -t duniter -o "$_DUNIKEY" -i "$_CRED" >/dev/null 2>&1

G1PUB=$(grep 'pub:' "$_DUNIKEY" 2>/dev/null | cut -d ' ' -f 2)
if [[ -z "$G1PUB" ]]; then
    echo "{\"error\": \"key generation failed\"}"
    exit 1
fi

# Conversion SS58 pour Duniter v2s (même convention que VISA.new.sh)
if [[ -x "${ASTRTOOLS}/g1pub_to_ss58.py" ]]; then
    _ss58=$(python3 "${ASTRTOOLS}/g1pub_to_ss58.py" "$G1PUB" 2>/dev/null)
    [[ -n "$_ss58" ]] && G1PUB="$_ss58"
fi

printf '{"g1pub": "%s", "mnemonic": "%s"}\n' "$G1PUB" "$MNEMONIC"
