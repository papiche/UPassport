# CLAUDE.md — UPassport

API centrale FastAPI de l'écosystème UPlanet. Sert les wallets Flutter (Ẑelkova, TrocZen),
l'interface web UPlanet/earth, et les outils CLI.
Author: Fred (support@qo-op.com). License: AGPL-3.0.

## Architecture

```
54321.py          ← Point d'entrée FastAPI (port 54321)
core/
  config.py       ← Settings (ZEN_PATH, etc.)
  state.py        ← Lifespan (startup/shutdown)
  middleware.py   ← RateLimitMiddleware
  logging.py      ← Setup logging
  exceptions.py   ← Gestionnaires d'erreurs globaux
routers/          ← Modules API par domaine
services/         ← Logique métier (nostr, ipfs, g1_squid)
models/           ← Schémas Pydantic
static/           ← Assets statiques
templates/        ← Jinja2 HTML templates
```

## Endpoints par router

### identity.py
- `POST /g1nostr` — Création / récupération MULTIPASS (email + photo → pubkey NOSTR + ZenCard)
- `POST /upassport` — Vérification UPassport (pubkey → profil NOSTR + solde ẐEN)
- `POST /ssss` — Reconstruction clé SSSS (Shamir Secret Sharing)
- `GET  /.well-known/nostr/nip96.json` — NIP-96 media upload descriptor

### nostr.py
- `GET  /nostr` — Page NOSTR (templates HTML par type)
- `GET  /api/getN2` — Réseau N² (amis d'amis depuis relay)
- `POST /sendmsg` — Envoi message NOSTR signé
- `POST /api/test-nostr` — Test publication événement NOSTR

### finance.py
- `POST /zen_send` — Envoi ẐEN entre comptes (transaction G1)
- `GET  /check_balance` — Solde G1 d'une clé publique
- `GET  /check_balances` — Solde G1 batch (g1pubs=pub1,pub2,… max 20) — une seule requête Squid GraphQL
- `POST /oc_webhook` — Webhook OpenCollective (recharge MULTIPASS immédiate)
- `GET  /check_society` — Vérification sociétaire (Satellite/Constellation)
- `GET  /check_revenue` — Revenus coopératifs
- `GET  /check_zencard` — État ZenCard
- `GET  /check_impots` — Calcul fiscal coopératif
- `POST /coinflip/start|flip|payout` — Jeu pile/face ẐEN

### media_upload.py
- `GET  /webcam` — Interface webcam HTML
- `POST /webcam` — Traitement vidéo webcam (visage → IPFS)
- `POST /vocals` — Traitement message vocal
- `POST /api/fileupload`, `POST /api/upload` — Upload fichier → IPFS
- `POST /api/upload/image` — Upload image → IPFS + publication NOSTR
- `POST /api/upload_from_drive` — Import depuis un autre uDrive que le sien
- `POST /upload2ipfs` — Upload direct IPFS

### media_library.py
- Gestion bibliothèque médias IPFS liés au MULTIPASS

### cloud.py
- `POST /api/cloud/upload` — Upload cloud avec auth NIP-98

### system.py
- `GET  /` — Statut station UPlanet (avec lat/lon/deg pour grille UMAP)
- `GET  /health` — Health check
- `GET  /credentials/v1` — Contexte JSON-LD Verifiable Credentials

### feedback.py
- `POST /api/feedback` — Crée une issue Git (GitHub ou GitLab) depuis n'importe quelle app.
  Routing : `source="coracle"` → `{GIT_OWNER}/coracle`. Config via kind 30800 : `GIT_HOST`, `GIT_TOKEN`, `GIT_OWNER`.
  Dégradation gracieuse si token absent. Voir `docs/FEEDBACK_INTEGRATION.md`.

### skills.py
- `GET  /api/skill/session` — Lit `~/.zen/tmp/$IPFSNODEID/install_session.json` (écrit par `install.sh`). Retourne le JSON de session : mode, profile, score, tier, cpu_model, ram_gb, vram_gb, log_cid. Fallback : dernier fichier `~/.zen/log/install_session_*.log`
- `GET  /api/skill/media/{skill}` — Interroge strfry (Kind 30504, `#t=[skill_norm]`, limit=20). Retourne les CIDs IPFS des preuves partagées dans la constellation. Dédoublonnage par CID. Utilisé par `install_craft.html` pour afficher les médias existants

### mailjet.py — Préférences notifications + Opt-out
- `GET  /mailjet` — Page préférences (auth NIP-42 ou email+token)
- `POST /mailjet` — Sauvegarde préférences → `~/.zen/game/nostr/EMAIL/.mailjet`
- `GET  /mailjet/challenge` — Challenge NIP-42 (TTL 5 min, usage unique)
- `POST /mailjet/auth` — Vérification Schnorr BIP-340, détection roaming, redirection

**Format `.mailjet` JSON** (géré par UPassport, lu par `tools/kin_prefs.sh`) :
```json
{
  "email_channel": false, "nostr_channel": false,
  "channels": [],
  "kin": {
    "daily": true, "weekly": true,
    "scope": "relay",
    "types": ["quartet","occult","analog","tone","guide","antipode"]
  }
}
```
`scope` : `"n1"` (follows directs) | `"n2"` (follows + amisOfAmis.txt) | `"relay"` (tous)

**Templates** (Jinja2, dans `templates/`) :
- `mailjet_base.html` — Base partagée (CSS + blocs)
- `mailjet_landing.html` — Authentification NIP-42 ou token
- `mailjet_prefs.html` — Page préférences complète avec section KIN
- `mailjet_success.html` — Confirmation sauvegarde
- `mailjet_error.html` — Page d'erreur

### Autres routers
- `analytics.py` — Statistiques d'usage
- `ipfs.py` — Opérations IPFS directes
- `crowdfunding.py` — Financement participatif ẐEN
- `geo.py` — Géolocalisation UMAP + `GET /api/nip42/challenge` (auth NIP-42)
- `permits.py` — Permissions (add_permits.py)
- `robohash.py` — Génération d'avatars robohash

## Commandes

```bash
cd UPassport
pip install -r requirements.txt   # Installer les dépendances
python3 54321.py                   # Démarrer le serveur (port 54321)
make run                           # Idem via Makefile
make test                          # Tests rate-limiting agressifs
pytest                             # Suite complète
pytest -m live_relay               # Tests nécessitant strfry ws://127.0.0.1:7777
make clean                         # Supprimer __pycache__ et .pyc
```

## Dépendances clés

```
fastapi==0.110.0    uvicorn[standard]   pydantic-settings
aiofiles            python-multipart    python-magic
websockets          httpx               cachetools
bech32              jinja2
```

## Rate Limiting

- Middleware personnalisé `RateLimitMiddleware` (core/middleware.py)
- Protections DOS documentées dans `DOS_PROTECTION_README.md`
- Tests agressifs : `test_rate_limit_aggressive.py`, `verify_rate_limiting_coverage.py`
- Monitoring : `monitor_rate_limits.sh`

## Fichiers statiques servis

- `/static/` — Assets internes
- `/earth/` — Monté depuis `~/.zen/workspace/UPlanet/earth/` (si disponible)

## Intégrations externes

- **Astroport.ONE** : Appelle les scripts bash via subprocess (`g1.sh`, `upassport.sh`)
- **IPFS** : Daemon local (port 5001) via `services/ipfs.py`
- **strfry NOSTR relay** : WebSocket ws://127.0.0.1:7777 via `services/nostr.py`
- **G1/Duniter v2s** : Requêtes Squid GraphQL via `services/g1_squid.py`
- **OpenCollective** : Webhook entrant `POST /oc_webhook`

## Access Control & Mémoire

- `MEMORY_ACCESS_CONTROL.md` — Politique de contrôle d'accès mémoire
- `oracle_system.py` — Système d'oracle (vérification de conditions on-chain)

## Déploiement systemd

```bash
./setup_systemd.sh   # Installe le service upassport
# Template : upassport.service.tpl
# Démarrage : ./start_secure_server.sh
```
