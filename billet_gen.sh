#!/bin/bash
##################################################################### billet_gen.sh
################################################################################
# Author: Fred (support@qo-op.com)
# License: AGPL-3.0 (https://choosealicense.com/licenses/agpl-3.0/)
#
# Génère une paire de clés G1/duniter jetable pour un "Ğ1Billet papier"
# imprimable (cf. UPassport/routers/qr.py::/qr/billet), puis découpe son seed
# (32 octets) en 3 parts Shamir (2-sur-3, GF(256)) — MÊME procédé que
# TrocZen (cf. TrocZen/troczen/lib/services/crypto_service.dart::
# shamirSplitBytes : un seul coefficient aléatoire par octet, évaluation du
# polynôme en x=1,2,3, réduction GF(256) par le polynôme irréductible 0x11B
# — vérifié par round-trip croisé Python/JS). Aucune des 3 parts seule ne
# permet de reconstruire le secret : il faut en réunir au moins 2.
#   P1 = imprimé en clair (QR visible sur le billet)
#   P2 = imprimé sous la bande repliable/scellée du billet
#   P3 = jamais imprimé — publié chiffré sur NOSTR (cf. qr.py::
#        _publish_billet_witness), réserve de recours/support uniquement
#
# Tout se passe en RAM (/dev/shm) : ni le seed, ni le fichier .dunikey ne
# sont jamais écrits sur disque persistant. Le secret n'existe qu'une fois,
# dans la sortie JSON de ce script — à imprimer/publier une seule fois puis
# oublier.
#
# Usage: ./billet_gen.sh
# Sortie (une seule ligne JSON) :
#   {"g1pub": "...", "p1": "...", "p2": "...", "p3": "..."}  (p1/p2/p3 en hex, 32 octets chacune)
###############################################################################
MY_PATH="`dirname \"$0\"`"
MY_PATH="`( cd \"$MY_PATH\" && pwd )`"
export PATH=$HOME/.astro/bin:$HOME/.local/bin:$PATH

ASTRTOOLS="${HOME}/.zen/Astroport.ONE/tools"

# Génère la phrase BIP39 (12 mots, 128 bits) + dérive le seed Ed25519 32 octets
# selon le standard BIP39 (PBKDF2-HMAC-SHA512, "mnemonic"+passphrase, 2048
# tours) — même algorithme que bip39-libs.js::mnemonicToSeedHex côté navigateur.
# La phrase elle-même ne quitte jamais ce script (seul le seed sert ensuite) :
# découpage Shamir GF(256) du seed en 3 parts (2-sur-3), même algèbre que
# TrocZen (crypto_service.dart::shamirSplitBytes) — un seul coefficient
# aléatoire par octet, évaluation en x=1 (P1), x=2 (P2), x=3 (P3), réduction
# par le polynôme irréductible 0x11B.
_PY_OUT=$(python3 -c "
import secrets
from mnemonic import Mnemonic

def gf256_xtime(a):
    a <<= 1
    if a & 0x100:
        a ^= 0x11B
    return a & 0xFF

def shamir_split_32(secret):
    p1, p2, p3 = bytearray(32), bytearray(32), bytearray(32)
    for i, a0 in enumerate(secret):
        a1 = secrets.randbelow(256)
        a1x2 = gf256_xtime(a1)
        p1[i] = a0 ^ a1
        p2[i] = a0 ^ a1x2
        p3[i] = a0 ^ (a1x2 ^ a1)
    return bytes(p1), bytes(p2), bytes(p3)

m = Mnemonic('english')
phrase = m.generate(strength=128)
seed = Mnemonic.to_seed(phrase, passphrase='')[:32]
p1, p2, p3 = shamir_split_32(seed)
print(seed.hex())
print(p1.hex())
print(p2.hex())
print(p3.hex())
" 2>/dev/null)

SEEDHEX=$(echo "$_PY_OUT" | sed -n '1p')
P1HEX=$(echo "$_PY_OUT" | sed -n '2p')
P2HEX=$(echo "$_PY_OUT" | sed -n '3p')
P3HEX=$(echo "$_PY_OUT" | sed -n '4p')

if [[ -z "$SEEDHEX" || -z "$P1HEX" || -z "$P2HEX" || -z "$P3HEX" ]]; then
    echo "{\"error\": \"key generation failed\"}"
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

printf '{"g1pub": "%s", "p1": "%s", "p2": "%s", "p3": "%s"}\n' \
    "$G1PUB" "$P1HEX" "$P2HEX" "$P3HEX"
