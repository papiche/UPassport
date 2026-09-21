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
- `GET  /api/nostr/admin/events` — Requête générique d'events (kind/author/tag_d/tag_p/tag_t/since/until), admin NIP-98/UPLANETNAME — utilisée par nostr_admin.html seulement pour les actions qui ne sont pas de simples lectures (le reste passe désormais en direct par le relay, cf. `UPlanet/earth/relay.js`)
- `POST /api/nostr/admin/delete`, `POST /api/nostr/admin/constellation_delete` — Suppression d'events (locale / constellation via DM-BRO), admin NIP-98/UPLANETNAME

### node_admin.py — config NODE, ARBOR, purge NOSTR (préfixe `/api/nostr/admin/*` partagé avec nostr.py par convention)
- `GET/POST /api/nostr/admin/node_config`, `POST .../node_config/delete` — Config coopérative (whitelist stricte, cf. `services/coop_config.py`)
- `GET  /api/nostr/admin/arbor_status`, `POST .../arbor_trigger`, `GET .../arbor_mined_preview` — Auto-amélioration ARBOR (déclenchement NIP-98 Capitaine EXCLUSIF)
- `GET  /api/nostr/admin/purge_strangers` — Analyse des comptes NOSTR "étrangers" (délègue à `Astroport.ONE/admin/system/purge_nostr_strangers.sh --list-json`), admin NIP-98/UPLANETNAME
- `POST /api/nostr/admin/purge_strangers/clean` — Lance la purge en arrière-plan (`--clean`, suppression irréversible), NIP-98 Capitaine EXCLUSIF — verrou PID (`~/.zen/tmp/purge_nostr_strangers.pid`) contre les lancements concurrents
- `GET  /api/nostr/admin/destroy_multipass/status` — Statut du dernier lancement de `nostr_DESTROY_TW.sh` (running + tail log), admin NIP-98/UPLANETNAME
- `POST /api/nostr/admin/destroy_multipass` — Lance `Astroport.ONE/tools/nostr_DESTROY_TW.sh <email> <reason>` en arrière-plan (désactivation MULTIPASS + backup chiffré + cashback Ğ1, irréversible sur cette station), NIP-98 Capitaine EXCLUSIF — `email` validé contre `list_multipass_emails()`, `reason` en whitelist (`INSOLVENCY|INTRUSION|INCOMPATIBLE_KEY`), verrou PID (`~/.zen/tmp/nostr_destroy_tw.pid`)

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
- `GET  /api/parrains_ranking` — Classement PUBLIC des parrains sociétaires (tiers Satellite/Constellation), pseudonymisé (sans email), délègue à `oc2uplanet.sh --json --parrain-ranking`, cache 1h. Affiché sur `UPlanet/earth/parrains.html`
- `GET  /api/oc_admin/contributions` — Contributions €→Ẑen (délègue à `oc2uplanet.sh --json --sync`), admin NIP-98/UPLANETNAME
- `GET  /api/oc_admin/expenses` — Dépenses OpenCollective (lecture `OC2UPlanet/data/expenses.json`)
- `GET  /api/oc_admin/dues` — Découverte stations sœurs + PAF/capacités (pas de calcul de montant dû)
- `POST /api/oc_admin/invoice/burn` — Déclenche `ZEN.INVOICE.sh` (burn Ẑen + soumission OC), jamais automatique
- `GET  /api/oc_admin/g1n2_wallets` — Wallets Ğ1-Nostr (N²) de CETTE station (lecture directe du cache `filter/30852.sh`, jamais recalculé) — kind 30852 n'étant jamais synchronisé entre stations, ne reflète que le ledger local
- `GET  /api/g1n2/balance?hex=<hex64>` — Solde Ğ1-N² PUBLIC (pas d'auth) d'un pubkey donné, typiquement une identité LOVE (`HEX_LOVE`) — utilisé par atomic_chat.html/Zelkova pour l'affichage ♥ et la construction du tag `prev`

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

### cloud.py — Cloud personnel chiffré (WebDAV)
Enrôlement seul : le transfert de fichiers passe par le montage WebDAV `/dav/`
(cf. `services/cloud_storage.py`), pas par un endpoint JSON.
- `POST /api/cloud/enroll` — Délivre/renouvelle le token Basic Auth WebDAV (auth NIP-98, NIP-42 en repli). Retourne `{dav_url, email, token, instructions}` ; `dav_url` est dérivé dynamiquement de `uSPOT` (`my.sh`), jamais codé en dur
- `GET  /api/cloud/status` — `{enrolled, dav_url, files, bytes, max_file_size}` — ne révèle jamais le token
- `POST /api/cloud/revoke` — Supprime le token (déconnecte les clients montés) ; fichiers et clés intacts

### Montage `/dav` — WebDAV chiffré (services/cloud_storage.py)
`54321.py` monte une app WSGI wsgidav via **a2wsgi** (`WSGIMiddleware`, PAS
`starlette.middleware.wsgi` qui est déprécié) : les I/O IPFS bloquantes et le CPU
AES-GCM tournent dans un pool de threads, sans figer la boucle uvicorn.

- **PUT** : flux DAV → buffer borné 20 MB → clé AES-256 **aléatoire par fichier** → `uenc_codec.encrypt_aes256gcm()` → `ipfs add` → index + keyring
  - Si image : GPS EXIF extrait ICI (clair encore en main, avant chiffrement — jamais via le Brain) → `entry.geo = {lat, lon, umap_key}` si présent (Pillow, best effort, silencieux sinon)
  - Si image : déclenche `_trigger_faceid_analysis()` (voir plus bas) — asynchrone, ne bloque jamais la réponse DAV
  - **Enrôlement supervisé** (depuis 2026-09-20) : en-têtes optionnels `X-FaceID-Target-Pubkey` (64 hex, sinon ignoré) / `X-FaceID-Target-Name` lus sur la requête PUT et transmis à `_trigger_faceid_analysis()` → `trigger_bro_vision_analysis.sh` → DM `vision_analysis_job` → `bro_dm_daemon.sh` → `satellite_face_matcher.py`, qui cataloguera alors CHAQUE visage détecté DIRECTEMENT sous cette identité (pas de recherche par similarité, pas de bootstrap `Inconnu_xxx`). Émis par `UPlanet/earth/cloud.html` pour les flux « Définir mon FaceID » et « Photos d'un ami » (voir plus bas)
- **GET** : index → CID → `ipfs cat` → déchiffrement one-shot → fichier éphémère 0600 → flux HTTP → purge immédiate (+ purge TTL 60 s de secours)
- **Auth** : `Authorization: Nostr …` (NIP-98 vérifiée par `services/nostr.py`, aucune duplication crypto) OU Basic `email:dav_token`
- **Isolation** : la racine DAV est résolue depuis l'email authentifié, jamais depuis le chemin
- `mount_path: "/dav"` est OBLIGATOIRE dans la config wsgidav — sans lui les href générés ignorent le préfixe de montage (PROPFIND cassé, COPY/MOVE en 409)
- `/dav` est exclu du rate limiting (`core/middleware.py`) : un disque monté émet des rafales de PROPFIND bien au-delà de 60/min

Stockage par utilisateur (tout en 0600, écritures atomiques sous `flock`) :
```
~/.zen/game/nostr/{EMAIL}/.ucloud/index.json    chemin DAV ↔ CID ↔ métadonnées
~/.zen/game/nostr/{EMAIL}/.ucloud/keyring.json  clé AES-256 par CID (JAMAIS dans l'index)
~/.zen/game/nostr/{EMAIL}/.ucloud/dav_token     token Basic Auth opaque (256 bits)
~/.zen/tmp/ucloud_cache/                        clair éphémère (0700)
```

⚠️ **Système PARALLÈLE au uDRIVE public** : `generate_ipfs_structure.sh` /
`manifest.json` / `APP/uDRIVE/` produisent un uDRIVE EN CLAIR publié sur IPNS et
restent INCHANGÉS. Ici rien n'est publié sur IPNS, et ce qui part vers IPFS est
déjà chiffré.

Interface : `UPlanet/earth/cloud.html` — **FaceCloud**, page unique combinant
activation + instructions de montage, envoi de photos (`PUT /dav/Photos/…`)
et catalogue de visages (`/mailjet/faces*`). Tout y passe par un seul
mécanisme d'auth : NIP-98, un event frais par appel. Trois flux d'envoi :
« Ajouter des photos » (générique, détection auto), « Définir mon FaceID »
et « Photos d'un ami » (enrôlement supervisé, cible choisie AVANT l'envoi —
voir en-têtes `X-FaceID-Target-*` ci-dessus) — réduit les faux positifs par
rapport à la détection auto seule.
Tests : `tests/test_cloud_storage.py` (37 tests — index/keyring, contrat UENC,
pile DAV avec IPFS mocké et chiffrement réel).

⚠️ L'ancienne route `GET /cloud` (template `templates/cloud.html`, drive NOSTR
kind 1063/21/22) a été **supprimée** — elle n'avait rien à voir avec ce cloud
chiffré. `SIMPLE_UI_ROUTES` dans `routers/system.py` ne la déclare plus.

### system.py
- `GET  /` — Statut station UPlanet (avec lat/lon/deg pour grille UMAP)
- `GET  /health` — Health check
- `GET  /credentials/v1` — Contexte JSON-LD Verifiable Credentials

### feedback.py
- `POST /api/feedback` — Crée une issue Git (GitHub ou GitLab) depuis n'importe quelle app.
  Routing : `source="coracle"` → `{GIT_OWNER}/coracle`. Config via kind 30800 : `GIT_HOST`, `GIT_TOKEN`, `GIT_OWNER`.
  Dégradation gracieuse si token absent. Voir `docs/FEEDBACK_INTEGRATION.md`.

### zine.py
- `POST /api/zine-submit` — Alternative numérique aux ZINE imprimables (`UPlanet/earth/ZINE.html`,
  `ZINE.MIZ.html`, `ZINE.MOUVEMENT.html`). Reçoit `zine` (`"armateur"|"adhesion"|"talent"`) + `fields`
  (JSON stringifié `{label: valeur}`), envoie un email formaté au capitaine (`settings.CAPTAINEMAIL`)
  via `mailjet.sh`. Pas d'authentification (visiteurs sans MULTIPASS) ; honeypot `website` + rate
  limiting global. Dégradation : si `CAPTAINEMAIL`/`mailjet.sh` indisponible ou l'envoi échoue,
  répond `ok=false` — le front invite alors à imprimer le zine.

### skills.py
- `GET  /api/skill/session` — Lit `~/.zen/tmp/$IPFSNODEID/install_session.json` (écrit par `install.sh`). Retourne le JSON de session : mode, profile, score, tier, cpu_model, ram_gb, vram_gb, log_cid. Fallback : dernier fichier `~/.zen/log/install_session_*.log`
- `GET  /api/skill/media/{skill}` — Interroge strfry (Kind 30504, `#t=[skill_norm]`, limit=20). Retourne les CIDs IPFS des preuves partagées dans la constellation. Dédoublonnage par CID. Utilisé par `install_craft.html` pour afficher les médias existants

### mailjet.py — Préférences notifications + Opt-out
- `GET  /mailjet` — Page préférences (auth NIP-42 ou email+token)
- `POST /mailjet` — Sauvegarde préférences → `~/.zen/game/nostr/EMAIL/.mailjet`
- `GET  /mailjet/challenge` — Challenge NIP-42 (TTL 5 min, usage unique)
- `POST /mailjet/auth` — Vérification Schnorr BIP-340, détection roaming, redirection
- `POST /mailjet/pass-toggle` — Bloque/débloque la récupération de compte par email+PASS (`/g1nostr`, `/atom4love/activate`) via le flag réversible `~/.zen/game/nostr/EMAIL/.pass.disabled` (cf. `routers/identity.py::_pass_disabled_flag`) ; le code PASS lui-même n'est jamais détruit (contrairement au verrouillage anti-bruteforce de `POST /g1nostr/alert`)

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

**FaceID — catalogue de visages** (Qdrant `faces_{hex}`, alimenté par
`Astroport.ONE/IA/bro/satellite_face_matcher.py`, payload
`{name, pubkey, timestamp, source_path, bbox}` — les deux derniers champs
depuis 2026-09-20, absents sur les points catalogués avant) :
- `GET  /mailjet/faces` — Liste `[{id, name, pubkey, timestamp, has_photo}]`
- `POST /mailjet/faces-edit` — Nomme un visage / l'associe à un pubkey (64 hex)
- `POST /mailjet/faces-delete` — Oublie un visage (vecteur supprimé)
- `GET  /mailjet/faces/thumbnail?point_id=…` — Miniature JPEG recadrée sur
  `bbox` (marge 50%) à partir de `source_path` : résout l'entrée
  `.ucloud/index.json` du propriétaire → CID chiffré → clé `.ucloud/keyring.json`
  → `ipfs cat` → `uenc_codec.decrypt_aes256gcm()` → recadrage Pillow →
  réponse JPEG directe. Jamais persistée en clair (même discipline que le
  GET `/dav/`). 404 si `source_path` absent (points pré-2026-09-20) ou si la
  photo/clé a depuis été supprimée du cloud chiffré.

Auth de ces 3 routes (`_faces_auth`) : **`Authorization: Nostr <event>` (NIP-98)**
— même mécanisme que `/api/cloud/enroll` et `/api/fileupload`, EMAIL résolu via
`services.cloud_storage.email_for_hex()` — **OU** le couple `email`+`token`
historique (repli conservé pour `mailjet_prefs.html`). Un NIP-98 valide prime et
rend `email`/`token` inutiles. Interface : `UPlanet/earth/cloud.html` (FaceCloud) —
`mailjet_prefs.html` n'affiche plus les visages.

**Inventaire — objets/lieux/scènes** (contrepartie du catalogue de visages
pour les photos où AUCUN visage n'a été détecté ; pas de base vectorielle ici,
juste les entrées de `.ucloud/index.json` portant un champ `scene`, écrit par
`satellite_face_matcher.py::_tag_ucloud_scene()` — jamais mélangé avec `tags`,
réservé aux noms d'amis à qui une photo de visage a été partagée) :
- `GET /mailjet/inventory` — `{items:[{path, type, category, name, description, confidence, tags, timestamp}]}`
- `GET /mailjet/inventory/thumbnail?path=…` — miniature JPEG de la photo entière
  (pas de `bbox` à recadrer, contrairement aux visages), déchiffrée à la volée,
  jamais persistée en clair
Même auth (`_faces_auth`) que les routes `/mailjet/faces*`.

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
wsgidav             a2wsgi              cryptography
```

`wsgidav` + `a2wsgi` servent exclusivement le montage `/dav` ; `cryptography`
fournit AES-256-GCM à `Astroport.ONE/tools/uenc_codec.py`. `cheroot` n'est PAS
requis : le serveur autonome de wsgidav n'est jamais utilisé ici.

## Rate Limiting

- Middleware personnalisé `RateLimitMiddleware` (core/middleware.py)
- Protections DOS documentées dans `DOS_PROTECTION_README.md`
- Tests agressifs : `test_rate_limit_aggressive.py`, `verify_rate_limiting_coverage.py`
- Monitoring : `monitor_rate_limits.sh`

## Fichiers statiques servis

- `/static/` — Assets internes
- `/earth/` — Monté depuis `~/.zen/workspace/UPlanet/earth/` (si disponible)
- `/dav/` — WebDAV chiffré (app WSGI wsgidav via a2wsgi, cf. `services/cloud_storage.py`)

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
