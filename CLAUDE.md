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
- `POST /api/cloud/enroll` — Délivre/**renouvelle** le token Basic Auth WebDAV (auth NIP-98, NIP-42 en repli). Retourne `{dav_url, email, token, instructions}` ; `dav_url` est dérivé dynamiquement de `uSPOT` (`my.sh`), jamais codé en dur. Génère un NOUVEAU token — déconnecte tout client DAV déjà monté avec l'ancien
- `GET  /api/cloud/status` — `{enrolled, dav_url, files, bytes, max_file_size}` — ne révèle jamais le token
- `POST /api/cloud/reveal` — Retourne le token **EXISTANT** (même réponse qu'`enroll`) sans le renouveler. Même garde NIP-98/NIP-42 que les autres routes : cette preuve de possession de la clé MULTIPASS donne de toute façon un accès complet à `/dav/` en direct (NIP-98 est un des deux mécanismes d'auth acceptés par le montage DAV lui-même), donc révéler le token Basic Auth à ce même appelant n'élargit aucun accès — ça évite juste à l'utilisateur de devoir régénérer (et donc déconnecter ses clients existants) s'il veut juste remonter le disque sur un nouvel appareil
- `POST /api/cloud/revoke` — Supprime le token (déconnecte les clients montés) ; fichiers et clés intacts
- `GET  /api/cloud/files` — Liste TOUS les fichiers de `.ucloud/index.json` (pas de filtrage FaceID) : `{files:[{path, mime, size, mtime, tags, readonly, faceid_status}]}`, triés par date décroissante. `faceid_status` (`"no_face"` ou absent) distingue « analysé, aucun visage » de « pas encore analysé ». Alimente la section « Mes fichiers » de FaceCloud (galerie brute du disque, indépendante de ce qui a été catalogué)
- `POST /api/cloud/files/delete` — Suppression groupée (multipart `paths`, tableau JSON de chemins, 200 max) depuis la section « Mes fichiers » (cases à cocher + `deleteSelectedGalleryFiles()`). Entrées en lecture seule (partagées) ignorées et renvoyées dans `skipped_readonly`
- `GET  /api/cloud/thumbnail?path=…` — Miniature JPEG (300×300 max) de N'IMPORTE QUEL fichier image de l'index, déchiffrée à la volée (`ipfs_cat` + `uenc_codec.decrypt_aes256gcm`, jamais persistée en clair). Contrairement à `/mailjet/faces|inventory/thumbnail`, ne requiert aucun catalogage préalable — fonctionne sur toute image présente sur `/dav/`

**Sources WebDAV externes** (depuis 2026-10-03, `services/webdav_client.py` +
`services/cloud_storage.py::*_webdav_source*`) — importer régulièrement des
photos depuis un AUTRE serveur WebDAV (NextCloud, ownCloud, une autre
station Astroport…) vers son propre `.ucloud`, cf. `webdav_import.py` plus
bas et la section « Importer depuis un autre cloud » de `ucloud.html` :
- `GET  /api/cloud/webdav-sources` — liste les sources configurées (`id`,
  `label`, `url`, `username`, `remote_path`, `dest_prefix`, `created_at`,
  `last_sync`) — le mot de passe n'est **jamais** renvoyé (`_public_source()`)
- `POST /api/cloud/webdav-browse` — `{url, username, password, path}` →
  `PROPFIND Depth:1` sur le serveur distant (`webdav_client.list_folder()`),
  pour le sélecteur de dossier de l'UI. Rien n'est persisté par cet appel
- `POST /api/cloud/webdav-sources` — `{label, url, username, password,
  remote_path, dest_prefix}` → revalide la connexion (`check_connection()`)
  AVANT d'écrire quoi que ce soit (échec immédiat plutôt que silencieux le
  lendemain en cron), puis persiste (même discipline que `dav_token` : JSON
  en clair, 0600 — un identifiant vers un service tiers, jamais la clé
  MULTIPASS)
- `DELETE /api/cloud/webdav-sources/{id}` — supprime une source (les photos
  déjà importées restent dans `.ucloud`)
- `POST /api/cloud/webdav-sources/{id}/sync` — lance `webdav_import.py
  <email> --source-id <id>` en arrière-plan (`subprocess.Popen`, même
  discipline fire-and-forget que `_trigger_faceid_analysis()`) ; répond
  immédiatement, le résultat apparaît dans `last_sync` au prochain `GET`

### Montage `/dav` — WebDAV chiffré (services/cloud_storage.py)
`54321.py` monte une app WSGI wsgidav via **a2wsgi** (`WSGIMiddleware`, PAS
`starlette.middleware.wsgi` qui est déprécié) : les I/O IPFS bloquantes et le CPU
AES-GCM tournent dans un pool de threads, sans figer la boucle uvicorn.

- **PUT** : flux DAV → buffer borné 20 MB → clé AES-256 **aléatoire par fichier** → `uenc_codec.encrypt_aes256gcm()` → `ipfs add` → index + keyring
  - Si image : GPS EXIF extrait ICI (clair encore en main, avant chiffrement — jamais via le Brain) → `entry.geo = {lat, lon, umap_key}` si présent (Pillow, best effort, silencieux sinon)
  - Si image : orientation EXIF normalisée ICI, AVANT chiffrement (`_normalize_image_orientation()`, depuis 2026-10-05) — ré-encode seulement si un tag `Orientation` ≠ 1 est présent (rotation/miroir), sinon octets inchangés. Sans ça, le pixel stocké restait « à plat » alors que ComfyUI (`LoadImage`) normalise l'orientation avant détection FaceID : `bbox` revenait dans un repère différent de l'image chiffrée, d'où des visages mal recadrés/pivotés sur un import de masse (photos smartphone en paysage/portrait). Défensif en complément : `_crop_face_jpeg()`/`_resize_full_jpeg()` (`routers/mailjet.py`) appliquent aussi `ImageOps.exif_transpose()` pour les photos déjà stockées AVANT ce correctif
  - Si image : déclenche `_trigger_faceid_analysis()` (voir plus bas) — asynchrone, ne bloque jamais la réponse DAV
  - **Enrôlement supervisé** (depuis 2026-09-20) : en-têtes optionnels `X-FaceID-Target-Pubkey` (64 hex, sinon ignoré) / `X-FaceID-Target-Name` lus sur la requête PUT et transmis à `_trigger_faceid_analysis()` → `trigger_bro_vision_analysis.sh` → DM `vision_analysis_job` → `bro_dm_daemon.sh` → `satellite_face_matcher.py`, qui cataloguera alors CHAQUE visage détecté DIRECTEMENT sous cette identité (pas de recherche par similarité, pas de bootstrap `Inconnu_xxx`). Émis par `UPlanet/earth/ucloud.html` pour les flux « Définir mon FaceID » et « Photos d'un ami » (voir plus bas)
  - **Analyse FaceID — conservé même sans visage** : si `satellite_face_matcher.py` ne détecte AUCUN visage sur l'image, l'entrée est désormais CONSERVÉE (depuis 2026-10-04 ; entre 2026-10-02 et 2026-10-04 elle était supprimée — politique abandonnée, voir ci-dessous) et simplement marquée `entry.faceid = {"status":"no_face","checked_at":…}` (`_tag_ucloud_no_face()`, appelée depuis le bloc `if not faces:` de `main()`) — le fichier est déjà sur IPFS à coût marginal, le garder permet un post-traitement ultérieur (nouvelle version du modèle, autre type de reconnaissance). `GET /api/cloud/files` expose ce marqueur (`faceid_status`) pour que `ucloud.html` distingue « analysé, pas de visage » de « pas encore analysé » (absent). L'ancienne fonctionnalité « Objets & lieux détectés » (`_tag_ucloud_scene()`, `/mailjet/inventory*`, §8.5 de `IA/generators/faceid.sh`) reste, elle, retirée entièrement — `IA/inventory_recognition.py` est utilisé par ailleurs pour la commande NOSTR `#inventory` du BRO responder, indépendante de ce pipeline. **Historique** : `cloud_purge_no_face.py` (voir plus bas) a supprimé un petit nombre d'entrées `scene`-taguées de l'ère « inventaire » avant cette politique — ce qu'il a supprimé reste perdu, mais plus aucune nouvelle suppression n'a lieu depuis le 2026-10-04
- **GET** : index → CID → `ipfs cat` → déchiffrement one-shot → fichier éphémère 0600 → flux HTTP → purge immédiate (+ purge TTL 60 s de secours)
- **DELETE** (fichier ou dossier) : `remove_subtree()` + `prune_keyring()` (clé détruite) PUIS `unpin_orphaned_cids()` (depuis 2026-10-04) — dépingle le ou les CID devenus orphelins, hors du verrou `index_lock` (I/O réseau IPFS, best-effort : un échec d'unpin ne fait jamais échouer la suppression). Avant cette date, le blob restait épinglé indéfiniment ; `ipfs repo gc` (périodique, hors de ce code) récupère l'espace des blocs non épinglés — jamais déclenché à chaque suppression (opération globale coûteuse). Même discipline pour `POST /api/cloud/files/delete` (suppression groupée JSON, section « Mes fichiers » de `ucloud.html`) et `cloud_purge_no_face.py`
- **Auth** : `Authorization: Nostr …` (NIP-98 vérifiée par `services/nostr.py`, aucune duplication crypto) OU Basic `email:dav_token`
- **Isolation** : la racine DAV est résolue depuis l'email authentifié, jamais depuis le chemin
- `mount_path: "/dav"` est OBLIGATOIRE dans la config wsgidav — sans lui les href générés ignorent le préfixe de montage (PROPFIND cassé, COPY/MOVE en 409)
- `/dav` est exclu du rate limiting (`core/middleware.py`) : un disque monté émet des rafales de PROPFIND bien au-delà de 60/min

Stockage par utilisateur (tout en 0600, écritures atomiques sous `flock`) :
```
~/.zen/game/nostr/{EMAIL}/.ucloud/index.json    chemin DAV ↔ CID ↔ métadonnées
~/.zen/game/nostr/{EMAIL}/.ucloud/keyring.json  clé AES-256 par CID (JAMAIS dans l'index)
~/.zen/game/nostr/{EMAIL}/.ucloud/dav_token     token Basic Auth opaque (256 bits)
~/.zen/game/nostr/{EMAIL}/.ucloud/webdav_sources.json  sources WebDAV externes (url/login/mdp/dossier)
~/.zen/tmp/ucloud_cache/                        clair éphémère (0700)
```

⚠️ **Système PARALLÈLE au uDRIVE public** : `generate_ipfs_structure.sh` /
`manifest.json` / `APP/uDRIVE/` produisent un uDRIVE EN CLAIR publié sur IPNS et
restent INCHANGÉS. Ici rien n'est publié sur IPNS, et ce qui part vers IPFS est
déjà chiffré.

Interface : `UPlanet/earth/ucloud.html` — **FaceCloud**, page unique combinant
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
`UPlanet/earth/cloud.html` (FaceCloud) a lui-même été renommé `ucloud.html`
le 2026-10-02 — mettre à jour tout lien/bookmark existant.

### cloud_import.py — Import en masse (script CLI, hors API)
Recopie un répertoire local (archives, export NextCloud…) vers le cloud
chiffré d'un MULTIPASS DÉJÀ EXISTANT sur cette station — même chemin de
chiffrement/index/déclenchement FaceID qu'un `PUT /dav/…` (délègue à
`services/cloud_storage.py::ingest_plaintext()`, extraite de
`_commit_plaintext()` pour être réutilisable hors DAV). Pas d'auth NOSTR :
tourne directement sur la station, avec l'accès filesystem local comme
frontière de confiance — donc pensé pour un usage admin/cron, pas exposé en HTTP.

```bash
python3 cloud_import.py <email> <répertoire> [--dest-prefix /Photos/Import] [--dry-run] [--quiet]
```

Idempotent via un cache local (`~/.zen/tmp/cloud_import/<sha256(email)[:16]>.json`,
`{mtime, size, dest_path}` par fichier source) — un fichier inchangé est
ignoré sans relire l'index ni recalculer de hash, essentiel pour un cron
répété sur des dizaines de milliers de photos. Cache perdu/absent → repli sur
`sha256_plain` de l'entrée d'index existante au même chemin (pas de
ré-import, pas de nouvelle clé/CID/job FaceID pour un fichier déjà importé).

### cloud_purge_no_face.py — Purge rétroactive HISTORIQUE (script CLI, hors API)
⚠️ Outil ponctuel pour un arriéré qui ne grossit plus : purge les entrées
cataloguées par l'ancienne fonctionnalité « Objets & lieux détectés »
(retirée le 2026-10-02), reconnaissables à leur champ `scene` dans
`index.json`. Depuis le 2026-10-04, aucune nouvelle entrée `scene` n'est
plus produite ET les photos sans visage ne sont plus supprimées (voir
ci-dessus) — ce script ne sert donc plus qu'à nettoyer un résidu de l'ère
pré-2026-10-02, pas à une maintenance courante. Portée volontairement
conservatrice — une entrée sans `scene` ET sans `tags` est ambiguë (visage
jamais partagé, ou jamais analysée) et n'est PAS touchée, pour ne jamais
risquer de supprimer une vraie photo de visage.

```bash
python3 cloud_purge_no_face.py [--email EMAIL] [--clean] [--quiet] [--list-ambiguous]
```
Sans `--clean` : dry-run (liste les candidats, rien n'est supprimé). Avec
`--email` : un seul MULTIPASS, sinon tous ceux hébergés sur cette station
(`~/.zen/game/nostr/*/.ucloud/index.json`). Supprime l'entrée d'index + sa
clé de keyring (`remove_subtree`/`prune_keyring`, même discipline qu'un
DELETE DAV) et dépingle le CID devenu orphelin (`unpin_orphaned_cids()`,
depuis 2026-10-04 — avant cette date, seule la clé était détruite, le blob
restait épinglé indéfiniment).

`--list-ambiguous` (`find_ambiguous()`) lève partiellement l'ambiguïté des
entrées sans `scene`/`tags` SANS rien supprimer : croise chaque chemin avec
le catalogue Qdrant `faces_{hex}` du propriétaire (`source_path`, présent
depuis 2026-09-20) via un scroll direct (httpx synchrone, même
`_qdrant_headers()` que `routers/mailjet.py` mais dupliqué ici — ce script
tourne hors FastAPI). Une entrée dont le chemin correspond à un point Qdrant
est un visage confirmé (ignorée) ; sinon elle reste ambiguë et est listée
pour décision humaine (ré-analyser, ignorer, supprimer à la main).

### webdav_import.py — Import quotidien depuis des sources WebDAV externes
Consomme `.ucloud/webdav_sources.json` (géré par l'utilisateur depuis
`ucloud.html`, voir `routers/cloud.py` plus haut) — ne configure rien
lui-même. Invoqué par `Astroport.ONE/RUNTIME/NOSTRCARD.refresh.sh` une fois
par jour et par compte (même patron que les scrapers de domaine : fichier
marqueur `.done`, lancé en arrière-plan), ou directement par
`POST /api/cloud/webdav-sources/{id}/sync` pour un import immédiat.

```bash
python3 webdav_import.py <email> [--source-id ID] [--max N] [--dry-run] [--quiet]
```
Sans `--source-id` : traite TOUTES les sources de cet email (cas cron) ;
avec : une seule (cas du bouton « Importer maintenant »).

**Budget adaptatif, pas une constante par compte (le point sensible de
cette fonctionnalité)** : la sérialisation des jobs FaceID (un seul à la
fois, flock GPU) est déjà assurée plus loin par `IA/generators/faceid.sh`
côté Brain — une fois le modèle chargé (ComfyUI « chaud »), traiter un
visage est rapide. Le vrai risque est le **TTL 30 min** du DM NOSTR
`vision_analysis_job` (traité strictement en série, sans retry) : si le
volume CUMULÉ de jobs envoyés par TOUS les comptes de la station dans cette
fenêtre dépasse ce que le GPU avale en 30 min, les derniers jobs expirent
silencieusement. Combien CE script peut raisonnablement envoyer dépend donc
du nombre de MULTIPASS hébergés sur la station — pas d'un chiffre arbitraire
par compte :
- `DAILY_STATION_BUDGET = 300` (env `WEBDAV_IMPORT_DAILY_BUDGET`) — photos/jour,
  **station entière**, toutes sources confondues
- `_station_multipass_count()` — nombre de comptes `~/.zen/game/nostr/*@*`
  (même source que `NOSTRCARD.refresh.sh`)
- `_default_max_import()` = `max(5, DAILY_STATION_BUDGET // nb_comptes)` —
  le budget du compte courant, lui-même divisé par son propre nombre de
  sources configurées (un compte avec 3 sources ne triple pas sa part)
- `IMPORT_DELAY_SECONDS = 2` (env `WEBDAV_IMPORT_DELAY_SECONDS`) — courtoisie
  envers le relais NOSTR (éviter une rafale d'events dans la même seconde),
  **pas** un espacement pensé pour le GPU (déjà sérialisé en aval)
- `--max N` explicite outrepasse ce calcul (ex. import manuel ponctuel)

**Robustesse mémoire/GPU par job (2026-10-05)** : `IA/generators/faceid.sh`
redimensionne désormais l'image AVANT l'upload ComfyUI si son plus grand côté
dépasse `_MAX_DETECT_DIM=1600` px (étape 1.5, Pillow `LANCZOS`, JPEG qualité
90) — aucune image, même 12 Mpx smartphone, n'était réduite avant ce
correctif : chaque job FaceID chargeait le pixel natif en mémoire ComfyUI,
coût qui grimpe avec la taille des photos sources — facteur aggravant
plausible d'un crash machine constaté sur un import WebDAV de masse. `bbox`
est ensuite reprojeté sur l'image ORIGINALE (facteur `_scale`, étape 8.5,
`jq`) avant la sortie finale — jamais celui de la copie réduite envoyée à
ComfyUI, sous peine de recadrages décalés en aval (`_crop_face_jpeg()`).
Best-effort : un échec de lecture/redimensionnement retombe sur l'image en
taille native plutôt que d'abandonner le job.

Parcours récursif du dossier distant via `services/webdav_client.py`
(`walk_files()`, `Depth: 1` niveau par niveau — **jamais** `Depth: infinity`,
que NextCloud refuse par défaut ; le filtrage des enfants se fait contre le
chemin du DOSSIER INTERROGÉ, pas contre la racine — bug corrigé le
2026-10-03 qui renvoyait silencieusement une liste vide pour tout
sous-dossier).

**Rangement par date** — `{dest_prefix}/YYYY/MM/<nom>` (`_dated_dest()`),
pas l'arborescence distante préservée (abandonné le 2026-10-04). Année/mois
tirés du `getlastmodified` PROPFIND distant (RFC 1123) — pas une vraie date
de prise de vue EXIF, mais disponible sans coût de téléchargement/décodage
supplémentaire.

Idempotence à TROIS niveaux :
1. Cache `(mtime, size)` distants dans
   `~/.zen/game/nostr/{EMAIL}/.ucloud/webdav_import_state_<source_id>.json`
   — **pas** sous `~/.zen/tmp/`, purgé CHAQUE NUIT par `20h12.process.sh`
   (ce qui viderait ce cache à chaque passage).
2. Filet de sécurité SHA256 **global** (`_hash_index()`) : si le cache est
   absent, le contenu téléchargé est cherché n'importe où dans `index.json`
   (pas seulement au chemin de destination calculé) — un PUT manuel
   antérieur ou une autre source WebDAV pointant sur le même contenu est
   ainsi reconnu comme déjà catalogué, sans quoi il serait réimporté sous un
   nouveau chemin ET redéclencherait une analyse FaceID GPU inutile.
3. Désambiguïsation de nom (`_unique_dest()`) : si le chemin daté calculé
   est déjà occupé par un AUTRE contenu (homonymie — plus probable
   maintenant qu'un seul mois de photos partage le même dossier), un
   suffixe `-2`, `-3`… est ajouté plutôt que d'écraser silencieusement
   l'entrée existante (CID/clé orphelins, invisibles).

Résultat de chaque passage écrit dans `webdav_sources.json` via
`cloud_storage.record_webdav_sync()` (`{at, imported, skipped, errors}`),
lu par `ucloud.html` sans ré-exécuter l'import.

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
depuis 2026-09-20, absents sur les points catalogués avant). Chaque
détection candidate du node ComfyUI porte un `det_score` (confiance
InsightFace) ; depuis 2026-10-05, `satellite_face_matcher.DET_SCORE_THRESHOLD
= 0.60` écarte avant tout traitement (catalogage OU enrôlement supervisé)
celles en-dessous — auparavant ignoré, ce qui laissait passer des faux
positifs (ex. `Inconnu_xxx` sur un détail d'image sans visage réel) lors
d'un import de masse. Une image dont TOUTES les détections tombent
sous le seuil suit le même chemin que « zéro visage détecté » (`_tag_ucloud_no_face()`) :
- `GET  /mailjet/faces` — Liste
  `[{id, name, pubkey, timestamp, has_photo, maybe, group_id, group_size, x, y}]`.
  `maybe` (depuis 2026-09-25, pour les archives longue durée) : sur une entrée
  SANS pubkey, `{name, pubkey, score}` du visage déjà nommé le plus proche par
  cosinus quand le score tombe dans `[0.55, 0.82[` (sous le seuil de match
  automatique `satellite_face_matcher.MATCH_THRESHOLD`, mais assez proche pour
  être la même personne à un autre âge). `group_id`/`group_size` (depuis
  2026-09-30) : regroupement ENTRE ELLES (union-find transitif, même seuil
  `_MAYBE_SAME_MIN=0.55`) des entrées SANS pubkey — même personne détectée sur
  plusieurs photos, mais pas encore identifiée du tout — présent seulement
  quand la composante connexe compte ≥2 membres. `x`/`y` (depuis 2026-10-02) :
  projection PCA 2D de l'embedding (`_pca_2d()`, SVD numpy), normalisée dans
  [-1, 1] — alimente la vue « nébuleuse » p5.js de `ucloud.html`. Les trois
  sont calculés serveur (scroll Qdrant `with_vector: true`,
  `_cosine()`/`_cluster_unnamed_faces()`/`_pca_2d()`, cf. `routers/mailjet.py`)
  — les vecteurs 512D ne sont jamais renvoyés au client, et rien n'est
  persisté dans Qdrant (recalculé à chaque appel). Garde-fous de performance
  (Python pur, O(n²)) : `_CLUSTER_MAX_UNNAMED=400` visages sans pubkey
  au-delà duquel le regroupement est ignoré (la liste reste utilisable, juste
  sans suggestion) ; `_PCA_MAX_FACES=5000` visages au total au-delà duquel la
  projection est ignorée (`x`/`y` à 0). UI : bandeau de suggestion +
  regroupement visuel + nébuleuse dans `ucloud.html`
- `POST /mailjet/faces-edit` — Nomme un visage / l'associe à un pubkey (64 hex)
- `POST /mailjet/faces-edit-bulk` — Même chose pour plusieurs `point_id` à la
  fois (`point_ids` séparés par des virgules, 200 max) : un seul `set_payload`
  Qdrant sur toute la liste — permet d'attribuer une identité commune à un
  groupe suggéré (ou une sélection manuelle) en un seul appel NIP-98 plutôt
  qu'un aller-retour par photo
- `POST /mailjet/faces-delete` — Oublie un visage (vecteur supprimé)
- `GET  /mailjet/faces/thumbnail?point_id=…` — Miniature JPEG recadrée sur
  `bbox` (marge 50%) à partir de `source_path` : résout l'entrée
  `.ucloud/index.json` du propriétaire → CID chiffré → clé `.ucloud/keyring.json`
  → `ipfs cat` → `uenc_codec.decrypt_aes256gcm()` → recadrage Pillow →
  réponse JPEG directe. Jamais persistée en clair (même discipline que le
  GET `/dav/`). 404 si `source_path` absent (points pré-2026-09-20) ou si la
  photo/clé a depuis été supprimée du cloud chiffré.
- `GET  /mailjet/faces/photo?point_id=…` — Même résolution que la miniature
  ci-dessus, mais photo ENTIÈRE (pas de recadrage `bbox`), redimensionnée
  ≤1024px (`_resize_full_jpeg()`) — aperçu au survol d'un point dans la vue
  nébuleuse de `ucloud.html` (la vignette identifie le visage, cette route
  donne le contexte complet de la photo)

Auth de ces 3 routes (`_faces_auth`) : **`Authorization: Nostr <event>` (NIP-98)**
— même mécanisme que `/api/cloud/enroll` et `/api/fileupload`, EMAIL résolu via
`services.cloud_storage.email_for_hex()` — **OU** le couple `email`+`token`
historique (repli conservé pour `mailjet_prefs.html`). Un NIP-98 valide prime et
rend `email`/`token` inutiles. Interface : `UPlanet/earth/ucloud.html` (FaceCloud) —
`mailjet_prefs.html` n'affiche plus les visages.

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


## Studio vidéo IA — `routers/story.py` + `services/story_render.py` (Kind 30510)

Personnages et scènes de vidéos IA (`Astroport.ONE/tools/story_asset.py`), versions, rendus. Auth NIP-98 **et** clé = MULTIPASS
du Capitaine (`settings.CAPTAINEMAIL`) ; interface `UPlanet/earth/story.html`. Portées : `private` (clé dans le keyring, jamais
exposée) ou `coop` (clé dérivée de `$UPLANETNAME`, lisible par tous les Capitaines via les événements du relais).
- `GET/POST /api/story/assets` — liste (mes paquets + coop des autres) / création `{type,name,scope}`
- `GET /api/story/asset/{cid}` · `GET …/file?path=` — contenu déchiffré d'une version quelconque (texte ≤ 512 Ko inclus)
- `PUT /api/story/asset/{cid}` — `{changes:{chemin:{text|b64|delete}}, description?, attach:[{cid}], reshare?}` → nouvelle version
  (anciens CID conservés ; un paquet d'un autre Capitaine → 400, utiliser `fork`). Corps ≤ 25 Mo, chemins sans `..`,
  storyboard validé comme `generate_scene.sh`
- `GET …/versions` · `POST …/restore` · `POST …/fork {name,scope}`
- `DELETE /api/story/asset/{cid}` — retire toute la lignée du trousseau local + demande de suppression NIP-09
  (kind 5, best-effort) pour l'événement courant. Équivalent CLI : `story_asset.py delete REF`
- `GET …/export` — tar.gz EN CLAIR (manifest + fichiers, pas de chiffrement/IPFS/NOSTR) : sauvegarde locale ou transfert
  manuel vers une autre station. `POST /api/story/import` (multipart `file`,`scope`,`name?`) — installe un paquet exporté
  comme NOUVEL asset de cette bibliothèque (nouvelle clé/CID/lignée, comme `fork` mais depuis un fichier local ; nom
  dédupliqué automatiquement s'il collide avec un asset déjà présent, pour ne pas écraser son événement Kind 30510
  remplaçable). Équivalent CLI : `story_asset.py export REF -o F.tar.gz` / `importpkg F.tar.gz [--scope] [--name]`
- `POST …/render {regen?}` → job ; `GET /api/story/jobs[/{id}]` (étape, plan en cours, plans prêts, ETA, journal),
  `POST …/cancel`, `GET …/jobs/{id}/file?name=` (shot_NN.mp4, vo_NN.wav, scene.mp4), `GET /api/story/render/{version_cid}/{id}/file`
- `POST …/render-shot {index}` — job `kind:"shot"` : ne (re)calcule qu'un plan (même cache de travail que la scène,
  ni finition ni assemblage). La prise précédente de ce plan est archivée avant d'être remplacée (jamais perdue) :
  `GET …/shot/{index}/takes` (liste) · `GET …/shot/{index}/takes/{take}/file`. Garde anti-collision : refuse si un
  rendu (scène entière OU plan seul) tourne déjà sur le même répertoire de travail.
- CLI uniquement (pont CLI/Web, pas exposé en API) : `story_asset.py resolve-character NOM --dest DIR` installe un
  personnage de la bibliothèque dans un dossier quelconque (utilisé automatiquement par `generate_scene.sh` quand un
  acteur `"nom": {}` est absent de `$CAST_BANK`) ; `story_asset.py rebuild [--force]` reconstruit `library/keyring.json`
  depuis le relais NOSTR si `$SCENES_DIR` a été supprimé — `coop` toujours récupérable (clé dérivée de
  `$UPLANETNAME`), `private` seulement si sa clé a été sauvegardée par DM NOSTR à soi-même (kind 4, envoyé
  automatiquement par `seal()` à chaque version privée).
- Jobs : process détachés (`start_new_session`), un seul à la fois (file `queued`), état dans `library/jobs/*.json`,
  `progress.json` écrit par `generate_scene.sh` / `generate_character.sh`. Rendus archivés par version (`renders/<auteur>-<d>/v/<cid>/<job>/`),
  coop : mp4 ajouté à IPFS et annoncé dans l'événement (`renders`). Tests : `tests/test_story_router.py`.
