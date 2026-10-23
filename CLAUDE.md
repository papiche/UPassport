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
  Réponse : `pepper_g1` (wallet) et `pepper_nostr` (clé NOSTR de CE compte, selon `.keyformula` : isolé ou legacy ; absent si la formule est inconnue — jamais devinée). Les clients ne connaissent jamais `UPLANETNAME`. Détection de compte existant par les DEUX npub (isolé + ancien format). Norme : `Astroport.ONE/CLAUDE.md`.
- `POST /upassport` — Vérification UPassport (pubkey → profil NOSTR + solde ẐEN)
- `POST /ssss` — Reconstruction clé SSSS (Shamir Secret Sharing)
- `GET  /.well-known/nostr/nip96.json` — NIP-96 media upload descriptor

### nostr.py
- `GET  /nostr` — Page NOSTR (templates HTML par type)
- `GET  /api/getN2` — Réseau N² (amis d'amis depuis relay)
- `POST /sendmsg` — Envoi message NOSTR signé
- `POST /api/test-nostr` — Test publication événement NOSTR
- `GET  /api/nostr/admin/events` — Requête générique d'events (kind/author/tag_d/tag_p/tag_t/since/until), admin NIP-98 Capitaine (le repli UPLANETNAME a été retiré) — utilisée par nostr_admin.html seulement pour les actions qui ne sont pas de simples lectures (le reste passe désormais en direct par le relay, cf. `UPlanet/earth/relay.js`)
- `POST /api/nostr/admin/delete`, `POST /api/nostr/admin/constellation_delete` — Suppression d'events (locale / constellation via DM-BRO), admin NIP-98 Capitaine (le repli UPLANETNAME a été retiré)

### node_admin.py — config NODE, ARBOR, purge NOSTR (préfixe `/api/nostr/admin/*` partagé avec nostr.py par convention)
- `GET/POST /api/nostr/admin/node_config`, `POST .../node_config/delete` — Config coopérative (whitelist stricte, cf. `services/coop_config.py`)
- `GET  /api/nostr/admin/arbor_status`, `POST .../arbor_trigger`, `GET .../arbor_mined_preview` — Auto-amélioration ARBOR (déclenchement NIP-98 Capitaine EXCLUSIF)
- `GET  /api/nostr/admin/arbor_baton_status`, `POST .../arbor_baton_handoff`, `POST .../arbor_baton_eligibility_check` — Bâton de parole ARBOR (délègue à `Astroport.ONE/IA/arbor_baton.sh`/`arbor_baton_eligibility.sh`) : gouverne UNIQUEMENT le cycle automatique quotidien `mine-requests` (une seule station de la constellation à la fois) — jamais le déclenchement manuel ci-dessus. Éligibilité = `git push --dry-run` réel sur `ARBOR_GIT_REMOTE_NAME` (config coop, défaut `origin`), pas une déclaration. Passation NIP-98 Capitaine EXCLUSIF vers une station swarm connue et éligible ; vacance auto-résolue après `ARBOR_BATON_TIMEOUT_HOURS` (défaut 72h)
- `POST /api/nostr/admin/arbor_chat` — Dialogue conversationnel avec Arbor (NIP-98 Capitaine EXCLUSIF), backend Claude CLI (délègue à `Astroport.ONE/IA/arbor_chat.py`, jamais Ollama — même doctrine que `arbor_tool_forge.py`). Contexte (bâton, branches, audit) reconstruit serveur, jamais fourni par le client. Peut répondre avec une `proposed_action` structurée que le capitaine valide explicitement via `arbor_trigger` — ne déclenche jamais rien seul
- `GET  /api/nostr/admin/purge_strangers` — Analyse des comptes NOSTR "étrangers" (délègue à `Astroport.ONE/admin/system/purge_nostr_strangers.sh --list-json`), admin NIP-98 Capitaine (le repli UPLANETNAME a été retiré)
- `POST /api/nostr/admin/purge_strangers/clean` — Lance la purge en arrière-plan (`--clean`, suppression irréversible), NIP-98 Capitaine EXCLUSIF — verrou PID (`~/.zen/tmp/purge_nostr_strangers.pid`) contre les lancements concurrents
- `GET  /api/nostr/admin/destroy_multipass/status` — Statut du dernier lancement de `nostr_DESTROY_TW.sh` (running + tail log), admin NIP-98 Capitaine (le repli UPLANETNAME a été retiré)
- `POST /api/nostr/admin/destroy_multipass` — Lance `Astroport.ONE/tools/nostr_DESTROY_TW.sh <email> <reason>` en arrière-plan (désactivation MULTIPASS + backup chiffré + cashback Ğ1, irréversible sur cette station), NIP-98 Capitaine EXCLUSIF — `email` validé contre `list_multipass_emails()`, `reason` en whitelist (`INSOLVENCY|INTRUSION|INCOMPATIBLE_KEY`), verrou PID (`~/.zen/tmp/nostr_destroy_tw.pid`)

### finance.py
- ~~`POST /zen_send`~~ — RETIRÉE (2026-10-17, répond 410) : autorisait un envoi avec `npub` + présence NIP-42 (npub public). Payer un membre = réaction Kind 7 `+N` signée (`NIP-101/…/filter/7.sh`) ; sinon virement Duniter via Ẑelkova. Le prototype Coinflip (qui en dépendait) a été supprimé.
- `GET  /check_balance` — Solde G1 d'une clé publique
- `GET  /check_balances` — Solde G1 batch (g1pubs=pub1,pub2,… max 20) — une seule requête Squid GraphQL
- `POST /oc_webhook` — Webhook OpenCollective (recharge MULTIPASS immédiate)
- `GET  /check_society` — Vérification sociétaire (Satellite/Constellation)
- `GET  /check_revenue` — Revenus coopératifs
- `GET  /check_zencard` — État ZenCard
- `GET  /check_impots` — Calcul fiscal coopératif
- `GET  /api/parrains_ranking` — Classement PUBLIC des parrains sociétaires (tiers Satellite/Constellation), pseudonymisé (sans email), délègue à `oc2uplanet.sh --json --parrain-ranking`, cache 1h. Affiché sur `UPlanet/earth/parrains.html`
- `GET  /api/oc_admin/contributions` — Contributions €→Ẑen (délègue à `oc2uplanet.sh --json --sync`), admin NIP-98 Capitaine (le repli UPLANETNAME a été retiré)
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
- `GET  /api/cloud/status` — `{enrolled, dav_url, files, bytes, max_file_size, faceid, received, reprocess}` — ne révèle jamais le token. `faceid` = compte des images PROPRES par statut d'analyse (`faces`/`no_face`/`pending`/`timeout`/`none` = jamais analysée), `received` = copies reçues d'amis (`origin: facecloud-share`, jamais analysées), `reprocess` = progression de `faceid_reprocess.py` (`running` recoupé avec le PID)
- `POST /api/cloud/faceid-reprocess` — lance `faceid_reprocess.py <email>` en arrière-plan (rattrapage silencieux, voir plus bas) ; 409 si déjà en cours
- `POST /api/cloud/thumbnails` — miniatures de PLUSIEURS images (`paths` = tableau JSON, 40 max, `size` ≤ 400) → `{thumbs:{path: data URL JPEG}}` : une seule signature NIP-98 pour une page de galerie ; décodage JPEG réduit (`Image.draft`), cache mémoire LRU 3000 entrées, jamais de clair sur disque
- `POST /api/cloud/reveal` — Retourne le token **EXISTANT** (même réponse qu'`enroll`) sans le renouveler. Même garde NIP-98/NIP-42 que les autres routes : cette preuve de possession de la clé MULTIPASS donne de toute façon un accès complet à `/dav/` en direct (NIP-98 est un des deux mécanismes d'auth acceptés par le montage DAV lui-même), donc révéler le token Basic Auth à ce même appelant n'élargit aucun accès — ça évite juste à l'utilisateur de devoir régénérer (et donc déconnecter ses clients existants) s'il veut juste remonter le disque sur un nouvel appareil
- `POST /api/cloud/revoke` — Supprime le token (déconnecte les clients montés) ; fichiers et clés intacts
- `GET  /api/cloud/files` — Liste TOUS les fichiers de `.ucloud/index.json` (pas de filtrage FaceID) : `{files:[{path, mime, size, mtime, tags, readonly, faceid_status, faces, from_name}]}`, triés par date décroissante. `faceid_status` : `faces` (+ `faces` = nombre détecté), `no_face`, `pending`/`timeout` (rattrapage), absent = pas encore analysé ; `from_name` = expéditeur d'une copie reçue. Alimente l'onglet Photos de FaceCloud
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

- `GET  /api/cloud/backup` · `POST /api/cloud/backup` · `POST /api/cloud/backup/restore` — sauvegarde hors station du cloud (état / maintenant / restauration en FUSION depuis le relais, puis épinglage des fichiers en arrière-plan), via `ucloud_backup.py` en sous-processus (voir plus bas)

### contacts.py + carddav.py — Carnet d'adresses FaceCloud (depuis 2026-10-12)
Stockage unique `services/contacts_store.py` : chaque fiche est une vCard
**chiffrée dans le cloud** sous `/Contacts/<nom>.vcf` (même AES-256-GCM que les
photos, visible sur le disque monté ; `<nom>` = nom de ressource CardDAV,
distinct de l'UID). Résumé chiffré `.ucloud/contacts.enc` (clé
`.ucloud/contacts.key`, 0600 ; reconstruit seul s'il manque) pour la liste, et
journal de synchro RFC 6578 (compteur `sync`, jeton par fiche, suppressions
récentes). Une fiche modifiée par le disque monté est réintégrée au prochain
chargement (comparaison `sha256_plain`). Verrou dédié `.contacts.lock` (pas
`index_lock`, qu'`ingest_plaintext` prend lui-même). `build()` réécrit une vCard
3.0 en CONSERVANT les propriétés que l'interface n'édite pas (adresse,
anniversaire, champs Apple/Google…).

- **CardDAV** (`routers/carddav.py`, RFC 6352) : `/.well-known/carddav` → 301
  `/carddav/` ; principal `/carddav/principal/`, home `/carddav/books/`, carnet
  `/carddav/books/contacts/`. Auth Basic `email:dav_token` (mêmes identifiants
  que `/dav`). REPORT `addressbook-multiget`, `addressbook-query` (tout le
  carnet), `sync-collection` (jeton inconnu → 403 `valid-sync-token`) ; PUT avec
  `If-Match` / `If-None-Match` (412), UID dupliqué → 409 `no-uid-conflict`.
  Exclu du limiteur de débit (`core/middleware.py`). Testé : DAVx5 (parcours
  rejoué dans `tests/test_contacts.py`)
- **API** (`routers/contacts.py`, NIP-98) : `GET /api/contacts` (liste + adresse
  de synchro + sources), `GET|POST|DELETE /api/contacts/item` (fiche ; photo
  depuis un fichier ou depuis un visage `photo_point_id`, recadrage 512),
  `POST /api/contacts/import-vcf`, `GET /api/contacts/export.vcf`,
  `POST /api/contacts/discover` (découverte des carnets d'un serveur :
  well-known → principal → home, `services/carddav_client.py` ; Google = OAuth,
  non pris en charge : export .vcf), `GET|POST /api/contacts/sources`,
  `DELETE …/{id}`, `POST …/{id}/sync` (import UNIDIRECTIONNEL ; une fiche
  modifiée ici depuis l'import n'est jamais écrasée ni supprimée par le serveur
  distant — `kept_local`), `POST /api/contacts/reconcile`
- **Suivi des imports** (2026-10-19) : `POST /api/contacts/import-vcf` traite en direct jusqu'à `IMPORT_SYNC_MAX` (60) fiches et rend `{state:"done", created, updated, errors}` ; au-delà il rend `{state:"running", total…}` et tourne en tâche de fond — `GET /api/contacts/import/status` donne `{state, name, total, done, created, updated, errors, error}` (en mémoire du service : `idle` après un redémarrage ; un second import simultané → 409 ; fichier sans vCard → 400). Un carnet CardDAV publie son avancement dans la source (`progress: {state: running|done|error, phase: download|apply, total, done, created, updated}`, lu par `GET /api/contacts/sources`) ; l'interface (`facecloud-contacts.js::watchImport`) interroge toutes les 1,5 s, affiche une barre et garde un message de fin. Tests : `tests/test_contacts_import.py`.
- **Photos de contacts = visages de RÉFÉRENCE** : analysées en mode rapide
  (`faceid.sh --batch`) quelques secondes après la dernière modification
  (`schedule_face_refs`), point Qdrant `ref: "contact"` dans `faces_<hex>`
  (le plus grand visage de la photo). Jamais affichés sur la table, jamais
  rangés automatiquement, exclus de la recherche du matcher : ils ne servent
  qu'au champ `maybe` de `GET /mailjet/faces` (`maybe.contact`, `maybe.email`)
- Les visages portent le lien vers leur fiche (`contact` dans le payload Qdrant,
  `faces-edit[-bulk]` paramètre `contact`, `-` = effacé)


### caldav.py + calendar_store.py + calendar_nostr_bridge.py — Agenda CalDAV ↔ NOSTR (depuis 2026-10-17)
Même schéma que le carnet : chaque événement est un `.ics` rangé chiffré sous
`/Calendar/<agenda>/<n>.ics` (`personal` existe toujours ; `<agenda>` = d-tag
kind 31924). Résumé chiffré `.ucloud/calendar.enc` (+ `calendar.key`) : titre, dates,
ETag, CID, jetons de synchro RFC 6578 et état du pont. Reconstruit depuis l'index si
perdu (non sauvegardé, comme le carnet).
- **CalDAV** (`routers/caldav.py`, RFC 4791) : `/.well-known/caldav` → 301 `/caldav/` ;
  principal, home `/caldav/calendars/`, agendas `/caldav/calendars/<agenda>/`. Mêmes
  identifiants Basic que `/dav` et `/carddav` ; exclu du limiteur de débit. Rapports :
  calendar-multiget, calendar-query (composant + période ; récurrences toujours
  renvoyées), sync-collection. MKCALENDAR refusé (les agendas naissent côté NOSTR),
  PROPPATCH répond 403 poliment. `enroll`/`reveal`/`status` renvoient `caldav_url` et
  `carddav_url` ; `ucloud.html` a un onglet « Téléphone » (DAVx⁵ / iOS).
- **Pont NOSTR** (`calendar_nostr_bridge.py`, process COURT séparé — le NSEC ne vit
  jamais dans UPassport) : `sync` = pull puis push, un seul à la fois (flock). Lancé
  détaché par `caldav.py` après chaque écriture (et à chaque lecture, ≤ 1 fois / 2 min),
  et chaque jour par `ucloud_daily.py` (étape `calendar`). Format = `calendars.html`
  (31922, `a`=31924:pub:agenda). Tout ce qui part du téléphone est chiffré NIP-44 pour
  soi (`private=nip44`, titre « 🔒 », contenu `{title, description, ics, cid}`) ; un
  événement public créé dans calendars.html le reste quand on le modifie, un nouvel
  événement du téléphone n'est JAMAIS publié en clair. Suppressions = kind 5 (`a`).
  Conflit : la modification locale non poussée gagne ; sinon created_at le plus récent.
  Lit aussi les kind 31923 (NIP-52, date Unix). VTODO/VJOURNAL restent sur CalDAV seul.
  `calendar_nostr_bridge.py status <email>` : dernier passage, éléments en attente.
  Tests : `tests/test_caldav.py` (stockage, parcours DAVx⁵, relais NOSTR en mémoire).


### visio.py + NIP-98 sur /api/myGPS (depuis 2026-10-17)
- `POST /api/visio/room` (NIP-98) → `{room, password, base, join_url, expires_at}` : salle vdo.ninja
  ET mot de passe tirés au sort (20 + 16 hex), rien n'est conservé côté station (`expires_at` est
  indicatif ; l'invitation porte son échéance). Plafond 30 salles/h par clé, 600/h pour la station.
  `GET /api/visio/config` → `{base}` (variable `VDONINJA` de `tools/my.sh`, https obligatoire).
  Remplace `uplanet_<npub[:10]>` de minelife.html (npub public → salle devinable, sans mot de passe).
- `/api/myGPS` accepte NIP-98 (npub facultatif, déduit de la signature ; refusé 403 s'il ne
  correspond pas) ET garde le repli NIP-42 journalisé `NIP42-FALLBACK`. Une requête, valable sur
  toute station, sans relais ni marqueur d'1 h. `roaming.js` tente NIP-98 puis retombe sur NIP-42
  (stations anciennes). `uplanet-header.js` reste sans signature (statut sans invite). À faire :
  migrer dev.js/youtube.html/templates/*.html, puis ajouter `/api/myGPS` à `NIP98_ONLY_PREFIXES`.
  Tests : `tests/test_visio.py`.

### facecloud.py — entrée dans FaceCloud (depuis 2026-10-12)
- `GET  /api/facecloud/invitation/{token}` — PUBLIC : expéditeur, nombre de
  photos, 4 aperçus recadrés, email masqué (le jeton de l'email prouve la
  possession de l'adresse)
- `POST /api/facecloud/start` (`email`, `claim`, `lang`, `lat`, `lon`) — avec
  `claim` : MULTIPASS créé pour l'email DE L'INVITATION (l'email saisi est
  ignoré), photos copiées AVANT la réponse (`process_pending_face_claims`),
  clé renvoyée pour une connexion immédiate en mémoire ; compte déjà existant →
  photos copiées, connexion par code PASS (aucune clé renvoyée). Sans `claim` :
  comme `/g1/onboard`. Position : navigateur, sinon la station (`~/.zen/GPS`)
- **Invitations portables + jamais de doublon** (`services/face_invites.py`, 2026-10-14) : l'invitation (lien ou email) est AUSSI publiée sur le relais commun (kind 30078 `d=facecloud:invite:<h>:<n>`, `h`=sha256(jeton), contenu AES-256-GCM dérivé du JETON : items `{cid,key,sha,name,mime,point_id,bbox}`, lots de 60) — n'importe quelle station l'honore (`_load_invite` : fichier local puis relais). Usage unique par événement `d=facecloud:claimed:<h>` signé par le destinataire. `start` demande « cet email a-t-il un MULTIPASS ? » au RELAIS (kind 0 `#i email:<email>`, `account_for_email`), jamais aux fichiers locaux : compte ailleurs → `status:"elsewhere"`, photos envoyées de station à station (`node_share.send_items`, `delivered`=n), aucune création ; relais muet → 503 (créer à l'aveugle ferait un doublon). Sans relais, l'invitation reste locale (`portable:false` dans la réponse de `person-invite-link`). `_search_email_in_nostr_hex` retombe aussi sur le relais. Tests : `TestPortableInvite`.
- **Visage de référence confié aux amis** (`services/face_ref.py`, 2026-10-15) : qui reçoit mes photos reçoit aussi mon empreinte (≤ 3 vecteurs 512D = les points de MA collection portant MA clé, « Votre visage »). Sa station les range dans `faces_<hex>` avec `ref:"shared"` (+ `pubkey`, `name`) : `satellite_face_matcher.py` me reconnaît alors seul (seul `ref:"contact"` est exclu de la reconnaissance) et me renvoie les photos par le chemin habituel, qui exige toujours le suivi RÉCIPROQUE. Canaux : copie locale (`share_copy` → `give_local`), inter-NODE (`face_ref` dans le 1er message `ucloud_share`, `face_ref_revoke` pour le retrait), invitation (dans le contenu chiffré par le jeton). Une fois par destinataire (`.ucloud/face_ref_given.json`, renvoyée seulement si l'empreinte change) ; refusée si l'expéditeur est bloqué ; vecteurs validés (512 flottants finis). Réglage `.ucloud/face_ref_sharing.json` (activé par défaut, sans effet sans « Votre visage »). API (NIP-98) : `GET|POST /api/cloud/face-ref` (`enabled`, `has_reference`, `given`), `POST /api/cloud/face-ref/revoke` (`hex` optionnel : tous sinon). UI : un interrupteur dans l'onglet Cercles de `ucloud.html`. Les refs `shared` ne sont jamais affichées en visages dans `/mailjet/faces` (comme `contact`, elles alimentent `maybe`). Tests : `TestFaceRef`.
- Expéditeurs bloqués : `.ucloud/blocked_senders.json`, vérifié dans
  `cloud_storage.ingest_received` (statut `blocked`) donc pour TOUS les canaux ;
  `POST /api/cloud/senders` (`hex`, `blocked`), liste dans `GET /api/cloud/status`

### Boucle virale FaceCloud (2026-10-20) — aperçu du lien, entonnoir, série « se dégafamiser »
- **Aperçu de lien** : `person-invite-link` et l'email d'invitation rendent `{uSPOT}/i/<jeton>` (plus `GAFAM.html?claim=`, qui reste valable pour les anciens emails). `GET /i/{jeton}` (public, `facecloud.py`) répond avec les balises Open Graph de CETTE invitation (« Fred vous a reconnu·e sur 23 photos ») puis redirige en JS vers `ucloud.html#demarrer&claim=…` ; `GET /i/{jeton}/og.jpg` = **mosaïque FLOUTÉE** 1200×630 (recadrages réduits à 8 px puis flou : aucun visage lisible, les serveurs de messagerie la gardent), cache mémoire 1 h/64 entrées. Lire la page ne réclame pas l'invitation. Invitation absente/expirée : aperçu générique, sans fuite.
- **Entonnoir** (`services/facecloud_funnel.py`, `~/.zen/log/facecloud_funnel.jsonl`, jamais de jeton ni d'email en clair) : `created` → `preview` (robot) / `visit` (humain) → `opened` (API) → `started` → `claimed` (nouveau compte) ou `delivered` (compte existant). Génération : `.facecloud_origin.json` dans le dossier du MULTIPASS (0 = hors invitation, +1 par invitation) ; `gen` voyage dans l'invitation portable. `GET /api/facecloud/funnel?days=` (NIP-98 Capitaine) : comptes par étape, taux, `viral.k` = part des invités devenus expéditeurs × invitations par expéditeur × taux de réclamation (None tant qu'il n'y a pas de données).
- **Série d'emails** (`services/degafam.py`, `ucloud_onboarding.py`, `routers/degafam.py`, page `UPlanet/earth/degafam.html`) : 4 étapes cochées d'après l'état réel (`state()` : photos ≥ 20 propres, contacts ≥ 1, agenda ≥ 1 événement, amis = une invitation/un visage confié) — la MÊME fonction alimente `GET /api/degafam/status` (NIP-98) et le message du jour. Règles : jamais ce qui est fait, un message par étape, ≥ 3 jours d'écart, 4 au plus, pas avant 12 h, pas après 60 jours de compte, flux `onboarding` (Mon compte) respecté ; aucun identifiant dans un email (le guide web les affiche via `reveal`/`enroll`). **Toujours active** (aucun drapeau) : `NOSTRCARD.refresh.sh` envoie `welcome_degafam.html` (bienvenue court + devoir OpenCollective 7 jours conservé) à la place de `welcome.html`, et lance `ucloud_onboarding.py` (setsid nohup) à la place des zines J1→J6. Après les 4 étapes viennent, une fois chacune et dans cet ordre, **Zelkova** puis **Coracle** (6 messages au plus) ; WoTx2, TrocZen, Nostr Tube, NextCloud et les autres applications non prêtes ne sont plus présentées (les fichiers `templates/NOSTR/zine/*` restent sur disque, inutilisés). `ucloud_onboarding.py <email> --preview DIR` écrit les 6 messages en HTML pour relecture ; `--dry-run`, `--force <étape>`. Le message « clés MULTIPASS » (zine) n'est jamais touché. Emails d'invitation et « photos reçues » réécrits avec `degafam.layout()` (un titre, ≤ 3 gestes, UN bouton).
- Tests : `test_facecloud_invite_page.py`, `test_facecloud_invite_ui.py` (Chromium), `test_degafam.py`, `test_degafam_ui.py`.

### Liens NOSTR (kind 3) — `routers/facecloud.py` (les « cercles » ont été retirés le 2026-10-19)
Les listes de suivis (kind 3) FONT FOI pour les relations ; plus de placement à la main. Borné par MES liens (jamais le réseau entier).
- `GET /api/facecloud/links[?refresh=1]` (NIP-98) — mes follows ET mes followers (`nostr_get_N1.sh`, `nostr_followers.sh`), avec `rel` : `mutual` (p2p, ils se suivent), `out` (je le suis seulement, 1→p), `in` (il me suit seulement, p→1) ; profil réduit (`name`, `display_name`, `email`, `npub` — jamais l'URL d'avatar ni la bio) ; plafond 600, mutuels d'abord ; `counts`. Cache 10 min, un seul calcul à la fois par clé. Lu par `ucloud.html::loadNostrLinks()` (`App.nostrLinks`) pour la fiche d'une personne et le choix d'un ami NOSTR.
- **Supprimés** : `GET /api/facecloud/circles` (renommé `links`), `GET /api/facecloud/circles/via/{hex}` (cercle 2), le paramètre `via` de `POST /mailjet/faces-edit[-bulk]` et le champ `via` de `GET /mailjet/faces` et des points Qdrant (aucun visage n'en portait). Une ancienne page qui envoie encore `via` est ignorée sans erreur.

### Galerie, doublons, aperçus (depuis 2026-10-13)
- `GET /api/cloud/files/page?filter=all|faces|no_face|waiting|received&q=&offset=&limit=(≤500)&counts=1` — images, la plus récente d'abord (`COALESCE(taken_at, mtime)`, index `entries_taken`), total + `counts` par filtre ; `GET /api/cloud/files` (tout) reste pour compatibilité.
- `GET /api/cloud/preview?path=&size=(400–2400)` — JPEG redimensionné pour la visionneuse, cache disque chiffré (`cloud_storage.thumb_cache_get/put`, clé `.ucloud/thumbcache.key`, `.ucloud/thumbcache/*.uenc`). `POST /api/cloud/thumbnails` utilise le même cache (instantané après un redémarrage).
- `POST /api/cloud/have` (`hashes` = SHA-256 hex, 500 max) → `{present}` : l'interface n'envoie pas ce qui existe (compare `sha_orig` OU `sha_plain`).
- `GET /api/cloud/duplicates[?summary=1]` — `exact` (même `COALESCE(sha_orig, sha_plain)`, SQL) et `near` (dHash `phash`, `ucloud_db.near_duplicates` : bandes 4×16 bits, distance ≤ 6, empreintes dégénérées ignorées, plafond de seau) ; chaque groupe a `keep` (la sienne d'abord, puis la plus lourde, la plus ancienne, le chemin le plus court). `POST /api/cloud/duplicates/scan` lance `ucloud_dedupe.py scan <email>` (empreinte des photos antérieures au schéma v2 ; progression `.ucloud/dedupe_scan.json`). Le nettoyage passe par `POST /api/cloud/files/delete`, qui oublie aussi les visages (Qdrant) de la copie supprimée.
- `GET /api/cloud/senders`, `POST /api/cloud/senders/purge`.
- `GET /api/cloud/geo?since=&until=` — carte : `points` (un par case de 0,01° : `lat`, `lon`, `n`, `first`, `last`, `cover`), `months` (AAAA-MM → nombre de photos géolocalisées), `scan` ; `files/page` accepte `since`, `until`, `lat`, `lon`. `ucloud_dedupe.py` complète aussi `taken_at` et `geo` des photos importées avant l'extraction EXIF (jamais d'écrasement). Schéma SQLite **v3** : colonnes `lat`/`lon` (rattrapées depuis `geo` du JSON) + index `entries_geo`.
- **Descriptions et recherche** : modèle vision `analysis.DESCRIBE_MODEL` = `qwen3-vl:4b-instruct` (env `FACECLOUD_DESCRIBE_MODEL` ; repli `qwen2.5vl:7b`, `gemma3:12b` si absent de la machine d'IA ; choisi le 2026-10-16 sur 18 photos réelles : ~1,7 s/photo et 3,3 Go contre ~5,7 s et 6 Go pour qwen2.5vl:7b, plus juste sur la composition et les textes d'objets, moins bon sur le manuscrit ; **ne jamais utiliser `qwen3-vl:4b` sans `-instruct` : modèle à réflexion, réponses vides**). `Astroport.ONE/IA/vision_describe.py` demande 2-3 phrases factuelles + une ligne « Mots-clés : … » (pensée pour la recherche), `repeat_penalty 1.05` / `temperature 0.2` (le 1.4 historique de `describe_image.py` fabriquait des mots inventés en français). `minicpm-v4.6` (1,6 Go) produisait un français mêlé d'anglais et d'italien : abandonné le 2026-10-16. `ucloud_describe.py scan <email> --redo` (ou `redo=1` sur `POST /api/cloud/analysis/run`) refait les descriptions IA d'un autre modèle, jamais celles saisies (`GET /api/cloud/analysis` : `describe.stale`, `describe.model`). **Recherche** (`ucloud_db.page(q=…)`) : `desc_text` est stocké normalisé (sans accents, minuscules ; schéma v5, rattrapé une fois) ; chaque mot de `q` (pluriel simple retiré) doit se trouver dans le chemin OU la description.
- **Traitement quotidien du `.ucloud`** (`ucloud_daily.py <email> [--only …] [--describe-limit N] [--dry-run] [-v]`, lancé en arrière-plan par `NOSTRCARD.refresh.sh` une fois par jour et par compte, journal `~/.zen/tmp/ucloud_daily_<compte>.log`) : **import** (`webdav_import.py`, nouvelles photos rangées à leur date de prise de vue EXIF — lue sur l'original téléchargé, plausible entre 1990 et demain —, repli date du serveur) → **enrich** (`ucloud_dedupe.py`, seulement s'il reste des photos sans empreinte/date/lieu) → **describe** (`ucloud_describe.py --limit N`, photos sans description, récentes d'abord ; budget `FACECLOUD_DESCRIBE_BUDGET` = 2400 photos/jour pour TOUTE la station, réparti entre les MULTIPASS, borné à 30–600 par compte ; sauté sans Ollama direct ni relais) → **embed** (`ucloud_embed.py`) → **backup** (`ucloud_backup.py snapshot`, en dernier pour emporter les descriptions du jour). **Processus autonome** : `NOSTRCARD.refresh.sh` le lance avec `setsid nohup python3 …/ucloud_daily.py … 200>&- &` (jamais en sous-shell `( … ) &`, dont la ligne de commande contiendrait « NOSTRCARD.refresh.sh » pendant des heures et ferait dire à `pgrep -f` de `_12345.sh` « déjà en cours » : plus aucun autre MULTIPASS rafraîchi) ; si `ucloud_daily.py` manque, c'est un défaut d'installation, signalé en ERROR (pas de repli). Même règle pour les scrapers de domaine (`tools/domain_scraper_run.sh`) ; un test de conformité (`TestNostrcardRefreshConformity`) échoue si une tâche d'arrière-plan de `NOSTRCARD.refresh.sh` n'est pas lancée par `setsid nohup`. Les étapes lourdes (enrich, describe, embed) sont sérialisées entre comptes par `.ucloud_daily_heavy.lock` (attente max `FACECLOUD_DAILY_WAIT` = 3 h, sinon reportées à demain) ; import et sauvegarde n'attendent pas. Étapes isolées (sous-processus, délai propre, un échec n'arrête pas les suivantes), `flock .daily.lock`, code 75 d'un script = « déjà en cours » (pas un échec). `--dry-run` affiche le plan et pourquoi chaque étape est faite ou sautée.
- **Suivi des traitements de fond** (`services/trace.py`, `FACECLOUD_VERBOSE` 0/1/2) : `ucloud_describe.py scan … -v|-vv` et `ucloud_embed.py scan … -v|-vv`. Une seule chronologie horodatée `[HH:MM:SS.mmm] composant: …` sur **stderr** (le stdout garde descriptions et JSON final) ; le niveau est hérité par `vision_describe.py` et `describe_image.py`, dont le suivi (image préparée, modèles essayés et repli, chargement du modèle, jetons lus/générés, réponse brute, nettoyage) s'insère dans le même flux. `-v` : chemin/modèle choisis, sélection et pourquoi des photos sont écartées, une ligne par photo, cadence et fin estimée, vectorisation ; `-vv` : en plus déchiffrement, commandes lancées, requêtes Ollama, textes embarqués, écritures de progression. `ucloud_describe.py` ne vectorise à sa fin QUE les photos qu'il vient de décrire (`ucloud_embed.scan(only_cids=…)`) ; le reste se vectorise par `ucloud_embed.py scan` ou en arrière-plan à la première recherche. Ctrl-C est propre dans les deux scripts (état « interrompu », verrou libéré, ce qui est fait est conservé, code retour 130).
- **Recherche par le sens** (`services/ucloud_embed.py`, script `ucloud_embed.py scan <email>`) : chaque description est vectorisée par Ollama (`/api/embed`, modèle `FACECLOUD_EMBED_MODEL`, défaut `bge-m3:latest`, 1024 d — le MÊME modèle que toute la plateforme IA (`Astroport.ONE/IA/embed_config.py`), mesuré le 2026-10-19 sur 40 requêtes naturelles / 440 vraies descriptions : 82 % de bonne photo en tête (95 % dans les 5 premières) contre 70 % (82 %) pour `paraphrase-multilingual`, utilisé du 2026-10-16 au 2026-10-19, qui avait lui-même battu `nomic-embed-text` : 11/12 contre 8/12 en tête sur des requêtes françaises, cosinus bons ≈ 0,68 / autres ≈ 0,20) et stockée en float16 dans la table `embeddings(cid, model, dsrc, vec)` de `ucloud.db` (clé = CID : une photo déplacée garde son vecteur ; `dsrc` = description vectorisée, une description corrigée est re-vectorisée ; changer de modèle re-vectorise tout). Texte embarqué = mots-clés D'ABORD (le modèle ne lit que ~128 mots). `GET /api/cloud/search?q=&filter=&since=&until=&lat=&lon=&offset=&limit=` : classement hybride = cosinus (≥ seuil du modèle — 0,30 pour paraphrase-multilingual, 0,45 bge-m3, 0,36 qwen3-embedding —, dans 0,28 du meilleur) + 0,5 si tous les mots sont dans le chemin/la description ; même forme que `files/page` + `mode` (`semantic` | `keyword` si Ollama ne répond pas), `note`, `index` {described, indexed, pending}. Le premier appel lance en tâche de fond la vectorisation des manquantes (`_kick_embed`) ; `ucloud_describe.py` la lance aussi à sa fin ; `POST /api/cloud/analysis/run task=embed` ; `GET /api/cloud/analysis` : `embed`. Mesuré : ~160 descriptions/s, recherche ≈ 0,25 s sur 21 000 photos. Matrice mémoïsée par (révision de l'index, `meta.emb_gen`).
- `GET /api/cloud/memories?month=&day=&year=` — « souvenirs du jour » : une ligne par année passée (`year`, `ago`, `n`, `cover`, `since`, `until`, jour ± 1) ; `GET /api/cloud/events?offset=&limit=&min_photos=` — événements = suites de photos espacées de moins de 6 h (≥ 6 photos par défaut), récents d'abord (`since`, `until`, `n`, `cover`, `lat`, `lon`). `ucloud_db.memories()` / `events()` (dates seules, regroupement linéaire). L'envoi d'un événement ou d'une photo à une personne réutilise `POST /mailjet/person-send` avec les seuls `point_ids` (visages) présents sur ces photos : aucun paramètre nouveau.
- `POST /api/cloud/photos/info` (`paths` = tableau JSON, 1000 max) — fiches (`files/page`) de chemins précis, plus récentes d'abord : alimente la frise d'une personne (tiroir de ucloud.html).
- **Quotas d'espace** (`services/storage_quota.py`) : offre **MULTIPASS 10 Go** (NCARD = 1 Ẑ/sem) ou **ZenCard 128 Go** (NCARD + ZCARD = 5 Ẑ/sem ; ZenCard = `~/.zen/game/players/<email>/secret.dunikey`) ; surcharge capitaine : octets dans `<multipass>/.storage_quota`. Usage = `.ucloud` (SUM(size) de l'index SQLite, mémoïsé 60 s) + `APP/uDRIVE` (parcours mémoïsé 10 min). **Période de grâce** : au-delà du forfait (`over`), l'utilisateur reçoit UNE lettre (`templates/NOSTR/multipass_storage_grace.html`, envoyée par `NOSTRCARD.refresh.sh`, rappel tous les 7 jours, marqueur `.storage_grace_letter`) l'invitant à devenir parrain de l'infrastructure ẐEN (`sponsor_url` = https://opencollective.com/monnaie-libre/contribute, surcharge `OC_URL_PARRAIN`) ; la lettre, l'API et l'interface **ne disent jamais** jusqu'où va la grâce. Les NOUVEAUX fichiers ne sont refusés (`full`) qu'à partir de forfait × `STORAGE_GRACE_FACTOR` (2 par défaut) : `ingest_many` refuse AVANT de chiffrer/épingler (`QuotaExceeded`) ; DAV → **507**, `ingest_received`/`adopt_blob` → statut `quota` (la photo reste chez l'expéditeur), `webdav_import` s'arrête avec un message ; jamais de suppression ; fichiers < 256 Kio (carnet, descriptions) toujours acceptés. `GET /api/cloud/quota` (jauge, `over`, `full`, `upgrade`, `sponsor_url`) ; `ucloud_usage.py <email>` écrit `.storage_usage.json`, appelé chaque jour par `NOSTRCARD.refresh.sh` et additionné par `ZEN.ECONOMY.sh` (log « STOCKAGE » : grâce / bloqués, + `~/.zen/tmp/coucou/storage_usage.json`). Le prélèvement hebdomadaire NCARD/ZCARD reste celui de `NOSTRCARD.refresh.sh` / `PLAYER.refresh.sh` : le quota ne le modifie pas.
- `POST /mailjet/person-invite-link` (`point_ids`, `name`) — invitation **par lien** (WhatsApp, Telegram, SMS, mail local, « Partager » du téléphone) : jeton ouvert (`open: true`, `to_email: ""`) dans `.face_invites/`, 7 jours, **usage unique**, portant toutes les photos des visages ; rend `{url, photos, expires_at}` (`GAFAM.html?claim=…` → `ucloud.html#demarrer&claim=…`). `GET /api/facecloud/invitation/{token}` ajoute `open` ; `POST /api/facecloud/start` prend alors l'email SAISI par l'invité ; `process_pending_face_claims(email, claim_token)` ne réclame une invitation ouverte que pour son jeton exact (jamais par simple correspondance d'email).
- `GET /api/cloud/webdav-sources` renvoie aussi `refresh` (`analysis.refresh_info` : heure `.refresh_time` du MULTIPASS, `done_today`, `due`) ; `POST /api/facecloud/profiles` (kind 0 sur les relais préférés : station, NIP-65 kind 10002, swarm ; cache `~/.zen/tmp/coucou/profiles`) et `POST /api/facecloud/avatars` (vignettes JPEG servies par le serveur, téléchargement protégé contre le SSRF) — `services/nostr_profiles.py`.
- Schéma SQLite **v2** : colonne `phash` + index `entries_taken` / `entries_phash`, ajoutés à chaud aux bases v1.

### Réconciliation des identités — `services/identity_links.py`
SEUL endroit qui décide que personne FaceCloud, fiche de contact et MULTIPASS
sont la même personne. Annuaire des MULTIPASS sans requête réseau : comptes
locaux, cache, et constellation (`~/.zen/tmp/swarm/*/TW/<email>/HEX`), 5 min.
Règles : même clé NOSTR ou même email = lien **certain** (`exact`) ; même email
**canonique** (`prenom+etiquette@d` ≡ `prenom@d`, casse ignorée — les points ne
sont jamais retirés) = lien **proposé** (`alias`), confirmé par l'utilisateur.
Une personne peut avoir plusieurs MULTIPASS en sous-adresses : l'interface
fait choisir le compte destinataire.

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
~/.zen/game/nostr/{EMAIL}/.ucloud/ucloud.db     SQLite WAL : tables entries (chemin DAV ↔ CID ↔ métadonnées) et keyring (clé AES-256 par CID, JAMAIS dans entries)
~/.zen/game/nostr/{EMAIL}/.ucloud/dav_token     token Basic Auth opaque (256 bits)
~/.zen/game/nostr/{EMAIL}/.ucloud/webdav_sources.json  sources WebDAV externes (url/login/mdp/dossier)
~/.zen/tmp/ucloud_cache/                        clair éphémère (0700)
```

⚠️ **Système PARALLÈLE au uDRIVE public** : `generate_ipfs_structure.sh` /
`manifest.json` / `APP/uDRIVE/` produisent un uDRIVE EN CLAIR publié sur IPNS et
restent INCHANGÉS. Ici rien n'est publié sur IPNS, et ce qui part vers IPFS est
déjà chiffré.

Interface : `UPlanet/earth/ucloud.html` — **FaceCloud** (refonte grand public
2026-10-12, cf. `UPlanet/earth/CLAUDE.md`) : table de tri des visages en
pochettes de personnes, envoi des photos à chacun, sources WebDAV, accès
disque. Un seul mécanisme d'auth : NIP-98, un event frais par appel.
L'enrôlement supervisé (`X-FaceID-Target-*`) sert au selfie « Votre visage »
et à « Ajouter des photos de cette personne ».

**Partage — `share_copy(owner_email, source_path, recipient_email, from_hex,
from_name)` → `adopt_blob()`** (secret unifié, depuis 2026-10-13) : canal UNIQUE de partage local d'une photo
vers le cloud d'un autre MULTIPASS de la station. Le destinataire référence le **MÊME bloc chiffré (même CID) avec
la même clé** — plus de rechiffrement ni de nouvel `ipfs add` : une photo partagée à N comptes = UN CID, détenu
(épinglé) par chaque station qui l'a reçue → vraie redondance au lieu de N blocs distincts. Entrée rangée sous
`/Reçues/<expéditeur>/YYYY/MM/<nom>` (suffixe `-2`… sous verrou), idempotente par `sha256` (déjà présent
n'importe où chez le destinataire → `duplicate`), métadonnées copiées (`cid`, `iv_hex`, `sha256_*`, `phash`,
`taken_at`, jamais `geo`/`faceid`/`tags`). **Jamais d'analyse FaceID sur une copie reçue** (ping-pong).
Compromis assumés : la clé d'UNE photo est connue de l'expéditeur et de ses destinataires (qui ont le clair) ;
deux comptes qui partagent une photo deviennent corrélables (même CID). **Dépinglage** : `unpin_cids(cids, email)`
ne dépingle un CID que si AUCUN autre compte de la station ne le référence (`cids_referenced_elsewhere`,
lecture seule des bases voisines) — le démon IPFS n'a qu'un épinglage par CID. Utilisé par le matcher (via
`ucloud_share.py`), `POST /mailjet/person-send` et la récupération après invitation.
Tests : `tests/test_cloud_storage.py` (40 tests — index/keyring, contrat UENC, share_copy,
pile DAV avec IPFS mocké et chiffrement réel).

⚠️ L'ancienne route `GET /cloud` (template `templates/cloud.html`, drive NOSTR
kind 1063/21/22) a été **supprimée** — elle n'avait rien à voir avec ce cloud
chiffré. `SIMPLE_UI_ROUTES` dans `routers/system.py` ne la déclare plus.
`UPlanet/earth/cloud.html` (FaceCloud) a lui-même été renommé `ucloud.html`
le 2026-10-02 — mettre à jour tout lien/bookmark existant.

### Durcissement du cloud DAV (audit du 2026-10-13)
- **NIP-98 lié à la requête** (`services/nostr.py::Nip98BindingMiddleware`, enregistré dans `54321.py`) : TOUT `Authorization: Nostr` doit porter un `method` et un chemin de `u` égaux à ceux de la requête (chemin seul, hôte ignoré — les proxies le réécrivent) ; sans tags `u`/`method` l'event reste accepté. Vérifié sur tous les clients de `UPlanet/earth` et de `generate_ipfs_structure.sh` (uDRIVE) : tous signent `u`/`method` de la requête émise, un event par appel. **Anti-rejeu** (un event = une requête pour une méthode qui modifie) seulement sous `NIP98_REPLAY_PREFIXES` (`/api/cloud`, `/api/contacts`, `/mailjet`, `/api/facecloud`, `/dav`) : `nostrFetch` ajoute un tag `nonce` aléatoire, sinon deux appels identiques dans la même seconde auraient le même id. `/dav` est vérifié par `cloud_storage.Nip98AuthMiddleware` (le middleware global l'ignore : double consommation sinon).
- **Clé AES du FaceID par l'environnement** (`UCLOUD_DECRYPTION_KEY`), plus jamais en argv (visible dans `ps`) ; lanceurs bornés (`UCLOUD_FACEID_TRIGGERS`, 4) et erreurs journalisées.
- **Ingestion** (`ingest_plaintext`) : EXIF (GPS, date) lu AVANT la normalisation d'orientation, qui conserve désormais EXIF et profil ICC ; `sha256_orig` (octets reçus) en plus de `sha256_plain` — l'import dédoublonne sur les deux ; `taken_at` (EXIF) exposé par `/api/cloud/files` ; mime générique (`application/octet-stream`) deviné par extension puis par les octets ; `._*`, `.DS_Store`, `Thumbs.db` jamais traités en photos ; image illisible → `faceid.status = "unreadable"` (jamais « en attente ») ; `unique=True` choisit le suffixe `-2`… SOUS `index_lock` ; blob dépinglé si l'écriture de l'index échoue.
- **Lecture** : borne IPFS = taille réelle de l'entrée (`cipher_limit`), +`UENC_OVERHEAD` par défaut (un fichier de 20 Mo exactement n'était pas relisible) ; le clair du GET ne passe plus par le disque (`io.BytesIO`), plus de thread de purge.
- **DAV** : `If-None-Match: *` → 412 (l'UI renomme au lieu d'écraser), MKCOL existant → 405, `Content-Length` > 20 Mo refusé avant lecture, en-têtes `nosniff` + `Content-Disposition: attachment` + CSP `sandbox` sur les GET, placeholders `cid: null` de plus d'1 h purgés, carte des dossiers construite une fois par requête (`build_children_map`).
- **Verrous** : `sources_lock` (sources WebDAV, expéditeurs bloqués), un import par source (`.ucloud/.webdav_import_<id>.lock`, 409 côté API), noms de fichiers temporaires uniques (threads), JSON de l'index compact.
- **Routes** : `files/delete` refuse `/` et `..` (400), tout le bloquant en `asyncio.to_thread`, miniatures par lot en parallèle (4) avec keyring chargé une fois, `/api/cloud/status` ne compte plus les placeholders.
- **Sources WebDAV externes** : URL validée (http(s), boucle locale / link-local / réservé refusés ; LAN permis), redirections suivies à la main, URL des fichiers pourcent-encodée (`Photo: x.jpg`, `#`, `?`), téléchargement en flux borné (`WEBDAV_MAX_DOWNLOAD`, 200 Mo), parcours borné (cycles, profondeur, dossiers) et **du plus récent au plus ancien** quand `WALK_MAX_FILES` coupe, `dest_prefix` forcé sous `/Photos/` et unique par source, progression de l'import sauvée tous les 50 fichiers.
- **Visages Qdrant** : `faces_follow_moves` (MOVE/renommage DAV → `source_path` mis à jour) et `faces_forget_paths` (DELETE DAV, `files/delete`, remplacement d'une photo par un autre contenu → points supprimés, par lots de 200) — best-effort, jamais d'exception.
- **Import par lots** : `ingest_many` (UNE lecture + écriture d'index et de keyring pour N fichiers, tout-ou-rien, blobs dépinglés en cas d'échec) ; `webdav_import` commite par lots de 25 (`WEBDAV_IMPORT_BATCH`, 5 en mode relais, 96 Mo max par lot) — l'index est réécrit en entier à chaque commit (≈ 0,8 s à 50 000 photos), donc 25× moins de réécritures. Mesuré : 7 photos → 3 écritures avec des lots de 3.
- **Lectures mémoïsées** : `index_ro` / `keyring_ro` / `children_ro` rendent un objet PARTAGÉ tant que le fichier garde sa signature (mtime_ns, taille, inode) — **lecture seule, ne jamais modifier** ; pour modifier : `load_index` sous `index_lock`. Utilisés par les requêtes DAV, `status`, `files`, miniatures, `read_plaintext`, `get_key_hex`.
- **Index et keyring = SQLite** (`services/ucloud_db.py`, `.ucloud/ucloud.db` en WAL, 0600 ; stdlib seule, importable par le matcher). Tables `entries` (une ligne par chemin ; colonnes dérivées `parent`, `cid`, `mime`, `size`, `sha_orig`, `sha_plain`, `faceid_status`, `origin` ; `data` = l'entrée JSON complète), `keyring(cid → {key_hex})`, `meta(rev, updated_at, owner_hex…)`. **Invariant : tout ancêtre est un dossier explicite.** Mesuré à 50 000 photos : ingestion d'un fichier 15 ms (≈ 800 ms en JSON), `db_entry` 0,02 ms, dossier de 4 000 fichiers 1,3 ms, migration 2,4 s.
  - **Chemins chauds** (une ligne, pas tout l'index) : `ingest_many`, `delete_paths`, `move_subtree`, `db_entry`, `db_children`, `get_key_hex`, `ucloud_db.patch_entry`, `ucloud_db.stats`, `count_under`, `find_by_hashes`. Le provider DAV ne charge plus l'index.
  - **API historique conservée** : `load_index` / `save_index` / `load_keyring` / `save_keyring` (diff O(n)), `index_lock` (flock `.lock`, toujours pris EN PLUS de la transaction SQLite pour qu'un `load → modifier → save` ancien n'écrase pas une ligne insérée entre-temps). `index_ro`/`keyring_ro` : copies mémoïsées par `meta.rev`, lecture seule.
  - **Migration automatique** au premier accès : `index.json` + `keyring.json` importés puis renommés `*.migrated-<date>` ; un index JSON illisible lève `RuntimeError` (jamais de base vide à sa place). `ucloud_db.export_json()` / `UPassport/ucloud_export.py <email> <dossier>` régénèrent le JSON historique (sauvegardes, `nostr_DESTROY_TW.sh`). `nostr_RESTORE_TW.sh` supprime `ucloud.db*` avant de recopier les JSON restaurés (réimportés à l'accès suivant).
  - **Consommateurs migrés** : `satellite_face_matcher.py` (clé, tags, `faceid`, via `ucloud_db` trouvé dans `~/.zen/UPassport/services`, repli JSON si absent), `faceid_reprocess.py` (une ligne par marquage), `contacts_store`, `ucloud_backup`, `webdav_import`, `cloud_import`, `mailjet` (photo source), `facecloud`, routes `cloud.py`.
- **Reste à faire** : pas de `Range` (format UENC mono-bloc) ; DNS rebinding non couvert pour les sources WebDAV.

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
par jour et par compte (tâche de fond « FaceCloud quotidien » : import PUIS
`ucloud_backup.py snapshot`, marqueur `facecloud_daily_<email>_<date>.done`), ou directement par
`POST /api/cloud/webdav-sources/{id}/sync` pour un import immédiat.

```bash
python3 webdav_import.py <email> [--source-id ID] [--max N] [--dry-run] [--quiet]
```
Sans `--source-id` : traite TOUTES les sources de cet email (cas cron) ;
avec : une seule (cas du bouton « Importer maintenant »).

**Analyse RAPIDE par défaut (depuis 2026-10-12)** : si ComfyUI est
joignable en direct depuis la station (`faceid_reprocess.comfyui_reachable()`
→ `comfyui.me.sh` : local, tunnel SSH ou P2P IPFS), les photos sont ingérées
SANS job DM (`ingest_plaintext(…, trigger_faceid=False)`), puis analysées en
un seul lot après chaque source par `faceid_reprocess.analyze_quick()`
(`faceid.sh --batch` + matcher direct, ~1 photo/s, silencieux : pas de
notification « visage inconnu »). Ce que le lot n'a pas pu traiter repart par
le relais NOSTR. Le résultat est ajouté à `last_sync.analysis`
(`{faces, no_face, timeout, relayed}`). Budget en mode rapide :
`DAILY_STATION_QUICK_BUDGET = 6000` (env `WEBDAV_IMPORT_QUICK_BUDGET`) réparti
entre les MULTIPASS, minimum 200 par passage — la file DM à TTL 30 min
n'est plus en jeu. `--no-quick` force l'ancien chemin.

**Parcours distant** : `webdav_client.walk_files()` est plafonné à
`WALK_MAX_FILES = 50000` (env `WEBDAV_WALK_MAX`). Ce plafond compte TOUS les
fichiers vus, déjà importés compris : à 2000 (valeur d'origine), une archive
plus grosse restait figée pour toujours une fois ses 2000 premiers fichiers
importés (constaté le 2026-10-12, `remote_capped: true`).

**Budget relais NOSTR (sans ComfyUI direct) — adaptatif, pas une constante
par compte (le point sensible de cette fonctionnalité)** : la sérialisation des jobs FaceID (un seul à la
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

### Partage entre stations — `services/node_share.py` (depuis 2026-10-12)
Destinataire hébergé sur une AUTRE station de la constellation (station connue
par `identity_links`, adresse `NODEHEX` lue dans `~/.zen/tmp/swarm/<IPFSNODEID>/12345.json`) :
les photos ne partent plus en message NIP-17 adressé à la personne mais de
station à station, sur le canal inter-NODE `ucloud_share`
(`nostr_node_intercom.py`, NIP-44 entre stations, TTL 7 jours) : le MÊME bloc (CID + clé de la photo, rien n'est rechiffré ni réuploadé), UN message pour au plus 100
photos `{to_email, to_hex, from_hex, from_name, items:[{cid,key,sha,name,mime}]}`.
La station destinataire (`bro_dm_daemon.sh` → `ucloud_share.py --receive`)
vérifie que l'email ET la clé sont hébergés chez elle, récupère le bloc auprès du swarm, le déchiffre avec la clé annoncée, contrôle l'empreinte, **recalcule** les métadonnées, **épingle le CID chez elle** et range sous `/Reçues/<expéditeur>/` (`cloud_storage.adopt_blob`, commun avec `share_copy`). Plus de copies temporaires côté expéditeur ; l'accusé `ucloud_share_ack` ne sert plus qu'aux anciennes stations. Un email récapitulatif au plus par
(destinataire, expéditeur, jour). Repli NIP-17 seulement si la station est
inconnue. Utilisé par le matcher (partage automatique) et `person-send`
(`sent.mode = "node"`).

### ucloud_share.py — partage FaceCloud (script CLI)
Modes : copie locale (`<owner> <recipient> <from_hex> <from_name> <path>…`),
`--remote <owner> <friend_hex> <from_name> <path>…` (canal inter-NODE, exit 3 si
station inconnue), `--receive` / `--ack` (JSON sur stdin, appelés par
`bro_dm_daemon.sh`).

### ucloud_backup.py — sauvegarde et restauration du cloud (script CLI)
```bash
python3 ucloud_backup.py snapshot|export|restore|pin|status <email> [dossier] [--from-relay|--bundle D] [--no-qdrant] [--gateway URL]
python3 ucloud_backup.py zencard-export <email> <dossier>   # contenu de la ZenCard → zencard_content.tgz (archive de départ, SANS clé)
python3 ucloud_backup.py zencard-import <email> <tgz>       # le remet dans la ZenCard recréée (même .g1pub exigé)
```
Une seule implémentation pour : la sauvegarde **régulière**, qui vit dans
`Astroport.ONE/RUNTIME/NOSTRCARD.refresh.sh` (une fois par jour et par compte,
dans la MÊME tâche de fond que `webdav_import.py`, juste après lui, pour que la
sauvegarde du jour contienne les photos importées du jour ; plus une
sauvegarde finale SYNCHRONE `ucloud_final_backup` juste avant toute
destruction — insolvabilité ou clé incompatible, y compris la suppression
brutale de repli) ; l'archive de `nostr_DESTROY_TW.sh` (`export`, mêmes noms
de fichiers qu'avant — ce script ne tourne qu'à la désactivation d'un compte,
ce n'est pas un mécanisme de sauvegarde) ; la restauration
(`nostr_RESTORE_TW.sh`, FaceCloud).
Paquet : index, trousseau, sources WebDAV/CardDAV, état d'import, invitations
en attente, catalogue Qdrant `faces_<hex>`, **mémoire de BRO** `memory_<hex16>`
(`qdrant_lifeos.json`, souvenirs oubliés compris), `settings/` (`face_ref_*`,
`blocked_senders`, `.mailjet` — transitoire, destiné à NOSTR kind 31903 ; jamais `.storage_quota`, décision du Capitaine), `uDRIVE_manifest.json` — tout en
0600, jamais `dav_token`. `restore` = fusion ; `--overwrite-settings` (déménagement)
fait gagner les réglages du paquet sur ceux d'un compte neuf ; une collection
Qdrant d'une AUTRE dimension (modèle d'embedding différent) n'est jamais mélangée :
erreur listée dans `errors`. `pin` épingle aussi le `final_cid` du uDRIVE.
Tests : `tests/test_ucloud_backup_bundle.py` (Qdrant simulé). Ce paquet est la base
du déménagement entre stations à clé identique (voir plan de migration). `snapshot` : archive
tar.gz chiffrée (clé neuve) sur IPFS + **UN** event kind 30078
`d=facecloud:backup`, contenu NIP-44 pour soi-même `{cid, key, sha, at, counts}`
(relais local + relay.copylaradio.com), remplacé à chaque version, ancienne
archive désépinglée, rien de republié si le contenu n'a pas changé. Choix
délibéré : pas un event par contact ou par fichier (moins de métadonnées
visibles, un seul event à remplacer ou supprimer, pas de données de tiers
répliquées dans toute la constellation). `restore` = FUSION (ce qui manque est
ajouté, rien n'est écrasé) ; `pin` épingle tous les CID de l'index (prise de
garde par la nouvelle station). Mesuré : 2037 fichiers + 1275 visages →
3,7 Mo chiffrés, 3,3 s.

### multipass_migrate.py + services/migration.py — déménagement entre stations (clé identique)
Même salt/pepper = même NSEC/HEX/wallet Ğ1/clé IPNS : rien à re-signer, seulement des données à porter, par le canal
inter-NODE (comme `node_share.py`). **Parcours utilisateur « QR + PASS »** : la personne scanne son QR (+ PASS) sur la
NOUVELLE station (roaming `check_ssss.sh` : clés re-dérivées par `derive_account_keys`, marqueur `.roaming`), puis
`POST /api/account/migration` (NIP-98 ; `GET` état, `DELETE` annule, `POST …/pin` relance l'épinglage —
`services/migration_api.py`, le serveur ne signe JAMAIS : process court `multipass_migrate.py`). La PERSONNE reste
l'autorité : `migrate_request` porte une preuve (kind 27235 signé par SA clé, `to` = la station demandeuse, ≤ 10 min, non
rejouée — `.ucloud/migrate_seen.json`). Cinq messages :
`migrate_request` (nouvelle → ancienne) → l'ancienne **gèle** le compte (`.migrating`) et exporte un paquet chiffré
(`ucloud_backup.export_encrypted(for_migration=True)` : index/trousseau ucloud, mémoire, réglages, **état du compte**
`account_state.tgz` = tous les fichiers cachés + `identity/` SAUF les secrets re-dérivables du QR, + `.pass`, +
événements NOSTR écrits par le compte et messages privés reçus ; export incomplet = refus + dégel) → `migrate_bundle`
{cid, clé, sha, counts} → la nouvelle importe (empreinte contrôlée, comptes annoncés = appliqués, événements
`strfry import` AVEC vérification des signatures) → `migrate_ack` → l'ancienne libère la clé IPNS (`ipns_handover.sh
release`) → `migrate_commit` {seq, value} → la nouvelle (`--finish`, détaché) épingle la racine, publie l'IPNS avec une
séquence supérieure, lance `tools/adopt_fixups.sh` (home.station, DID, champ home_station du kind 0, suivis Capitaine —
rien d'écrasé) puis **rapatrie TOUS les documents** (`services/account_pins.py` : racine NOSTRNS, ucloud, uDRIVE,
cookies, CID cités par les événements ; espace disque contrôlé avant, reprenable, échec jamais « ok ») → `migrate_pinned`
{total, pinned, failed, ok}. **Rien n'est nettoyé avant ce message** ; s'il confirme `failed == 0`, l'ancienne station
DÉPLACE (jamais ne supprime) le dossier vers `~/.zen/game/migrated/` et le Capitaine ne suit plus le compte ; une ZenCard
présente (non migrée) suspend le nettoyage (`--cleanup` après traitement). Un import en erreur n'envoie AUCUN accusé.
Gel : `my.sh::account_frozen` (`.migrating` côté départ ; target:requested|imported|claiming|error côté arrivée) —
ignoré par `NOSTRCARD.refresh.sh`, `PLAYER.refresh.sh`, `ZEN.ECONOMY.sh`. `.roaming` (purgé après 24 h par
`NODE.refresh.sh`) est retiré dès la demande. État : `<compte>/.migration.json` (0600 ; la clé du paquet n'est jamais
affichée). Handlers : `bro_dm_daemon.sh::_handle_migrate`. Tests : `test_migration.py` (deux stations simulées, vraie
signature), `test_account_pins.py`, `test_account_migration_api.py`, `test_ucloud_backup_bundle.py`,
`Astroport.ONE/tests/test_adopt_fixups.sh`. **Reste à faire** : migrer aussi la ZenCard (`players/<email>`), essai réel
entre deux stations, boucles de rafraîchissement hors NOSTRCARD/PLAYER/ECONOMY (G1PalPay, NOSTR.UMAP…) qui devraient
elles aussi ignorer `account_frozen`, redémarrage du daemon BRO et d'UPassport pour charger canaux et routes.

### Onglet « Déménager » de `/scan` + changement de constellation
**Interface** (`templates/scan.html`, onglet « 🚚 Déménager », lien direct `/scan#demenager`) : la personne scanne le **QR P1** de son
billet MULTIPASS (`M-<base58>:<NOSTRNS>`, part SSSS joueur) et saisit son **PASS** sur la NOUVELLE station. `POST /api/migration/start`
(`routers/migrate.py`, sans compte ni NIP-98 : le QR P1 + le PASS sont la preuve) → `services/migration_api.py::start_from_qr` :
validation stricte du QR, refus si le compte est DÉJÀ hébergé ici, roaming `check_ssss.sh … json` (part P1 par l'entrée standard,
jamais en argument), puis `multipass_migrate.py --request --pass-stdin`. L'ANCIENNE station vérifie le PASS (`services/pass_guard.py`,
compteur partagé avec `/g1nostr` : 3 échecs = PASS invalidé + Capitaine prévenu) avant tout gel. Un **ticket** aléatoire (seul son
hachage est gardé, `.migration.ticket`) donne accès au suivi : `GET /api/migration/status`, `POST …/abort`, `POST …/pin` ; un compte
inconnu et un mauvais ticket donnent la même réponse (pas d'oracle). Limites : 5 démarrages/h/IP, 1/min/compte. **Suivi à l'écran** :
étapes ordonnées (`STEP_DEFS`), barre de pourcentage, documents rapatriés « N / total », message en français, boutons relancer/annuler,
reprise après rechargement (sessionStorage : email + ticket SEULEMENT, jamais le QR ni le PASS). Tests : `test_scan_move_ui.py`
(Chromium réel, API simulée, temps accéléré), `test_migration_scan.py`, `test_account_migration_api.py`.
**E-mails** : à la fin (tout épinglé), la personne ET le Capitaine d'arrivée ; au nettoyage, le Capitaine de départ (`templates/NOSTR/
migration_*.html`, HTML statique FR+EN lisible partout). Dossiers de l'ancien compte DÉPLACÉS vers `~/.zen/tmp/migrated/<email>.<date>/
{nostr,players}` (purgés à la prochaine 20h12).
**Autre UPLANETNAME = autre constellation = AUTRE swarm IPFS privé** : le déménagement « même clé » ne s'applique PAS (wallet, IPNS et
clé NOSTR isolés diffèrent ; la part « tail » du QR est chiffrée pour l'UPlanet d'origine ; les canaux inter-NODE exigent un expéditeur du
swarm). Le chemin est la restauration par archive (`nostr_DESTROY_TW.sh` → `nostr_RESTORE_TW.sh`, identité NEUVE) avec **rapatriement par la
PASSERELLE HTTP de l'ancienne station** : `nostr_RESTORE_TW.sh <lien https://hote/ipfs/<CID> | CID> --gateway https://hote` (l'archive
porte `.gateway` ; l'e-mail de désactivation donne le lien). `services/gateway_fetch.py` : DAG demandé en **CAR** (`?format=car`) puis
`ipfs dag import --pin-roots` — blocs vérifiés par IPFS, **CID préservés** (l'index ucloud et son trousseau restent valables) ; passerelle
http(s) publique seulement (anti-SSRF : boucle locale, réseaux privés, redirections et identifiants refusés ; `GATEWAY_ALLOW_PRIVATE=1`
côté opérateur), tailles et délais bornés. `ucloud_backup.py pin <email> --gateway URL`, `gateway_fetch_cli.py`. L'archive ne contient plus
`UPLANETNAME` (secret de la constellation) mais son EMPREINTE (`.uplanetname_sha256`) ; RESTORE ne l'affiche jamais. Tests :
`test_gateway_fetch.py` (vrai IPFS : dépôt source et dépôt neuf isolés), `Astroport.ONE/tests/test_destroy_restore.sh` section 11.
**TiddlyWiki de la ZenCard dans l'archive de départ** (`ucloud_backup.py zencard-export/-import`, `zencard_content.tgz`) : le CONTENU seulement —
`ipfs/` (TiddlyWiki, chaîne de versions `.chain.*`, flux RSS) et `G1PalPay/` (historiques de paiements) — jamais de clé, de PASS, de QR ni de
`U.SOCIETY` (le statut de sociétaire appartient à la coopérative d'origine). La clé de la ZenCard étant la même sur toutes les UPlanets (crédentiel
brut), il se restaure tel quel dans une autre constellation. `nostr_DESTROY_TW.sh` l'exporte AVANT `PLAYER.unplug.sh` ; un export en échec
ANNULE la destruction (`DESTROY_SKIP_ZENCARD=1` pour y renoncer explicitement). `nostr_RESTORE_TW.sh` l'importe APRÈS la recréation de la ZenCard
(`VISA.new.sh`) : refus si ce n'est pas la même carte (`ipfs/G1SSB/_g1.pubkey` ≠ `.g1pub`) ou sans marqueur ; remplacement atomique, le
TiddlyWiki neuf est mis de côté dans `~/.zen/tmp/zencard_fresh_*` (purgé à 20h12). **Liens réécrits** : `copylaradio.com` est le domaine de UPlanet ORIGIN ; sur une UPlanet Ẑen (swarm.key privée ; JAMAIS sur ORIGIN, UPLANETNAME à zéros) , TOUT lien qui le porte (nu ou `<sous-domaine>.copylaradio.com` : ipfs., vdo., relay., u.…) passe à `OVH_ZONE` de CETTE station (= `UPLANET_DOMAIN` de `my.sh`, vide sur ORIGIN)
(`nostr_RESTORE_TW.sh` passe `--zone`, ou `ucloud_backup.py zencard-links <email> <zone>`), dans tous les fichiers texte de `ipfs/` et `G1PalPay/` (jamais `.chain.*`, binaires, liens symboliques). Adresses e-mail et `copylaradio.com.autre.org` exclues ; octets préservés, idempotent, zone invalide = erreur (le contenu restauré reste en place, `links_error`).
Appliqué aussi à la CRÉATION d'une ZenCard (`RUNTIME/VISA.new.sh`, seulement si `UPLANET_DOMAIN` non vide). `tools/my.sh` : sur UPlanet Ẑen, `OVH_ZONE` l'EMPORTE sur `VDONINJA`, `myLIBRA`, `myCORACLE`, `myUPLANET` ; la porte d'entrée `/ipns/…` de `VISA.new.sh`/`make_NOSTRCARD.sh` suit `${UPLANET_DOMAIN:-copylaradio.com}`. Déménagement « même clé » : le dossier entier de la ZenCard voyage (`zencard_state.tgz`, TiddlyWiki compris), sans réécriture de liens (même constellation = même `OVH_ZONE`). `templates/twdefault.html` reste le modèle d'ORIGINE.

### faceid_reprocess.py — rattrapage FaceID silencieux (script CLI)
```bash
python3 faceid_reprocess.py <email> [--quick] [--before ISO] [--status no_face,none,pending,timeout] [--parallel 6] [--wait 240] [--limit N] [--dry-run]
```
**`--quick`** (utilisé par `POST /api/cloud/faceid-reprocess`) : un seul
`Astroport.ONE/IA/generators/faceid.sh --batch` parle DIRECTEMENT à ComfyUI
(local, tunnel SSH ou P2P IPFS via `comfyui.me.sh`, vérifié UNE fois), le
résultat est passé à `satellite_face_matcher.py` comme le ferait
`bro_dm_daemon.sh` — sans aller-retour DM NOSTR. Mesuré le 2026-10-12 :
~0,8 s par photo contre ~10 s (un appel unitaire de `faceid.sh` coûtait ~2,6 s
de `my.sh` + ~4,3 s de `comfyui.me.sh` pour ~0,6 s d'inférence). Clairs
déchiffrés dans un dossier 0700 sous `/tmp` (pas `~/.zen/tmp/`, purgé la nuit),
6 d'avance au plus, supprimés aussitôt analysés. ComfyUI injoignable → repli
automatique sur le relais NOSTR décrit ci-dessous. `--before ISO` ne reprend que
les photos analysées avant cette date (évite de refaire un rattrapage partiel).

Réanalyse les images PROPRES (pas les copies reçues) dont le statut est dans
`--status`. Créé le 2026-10-12 après le bug `faceid.sh` qui marquait `no_face`
toute photo réduite avant détection (> 1600 px). Fonctionnement :
- pose `.ucloud/faceid_quiet_until` (epoch, prolongé à chaque tour, expire
  seul) : `satellite_face_matcher.py::_quiet_mode()` suspend alors les
  notifications « visage inconnu » et les envois NIP-17 inter-stations ; les
  copies locales (`ucloud_share.py`) restent actives ;
- au plus `--parallel` photos en vol : marquées `pending`, analyse déclenchée
  par `cloud_storage._trigger_faceid_analysis()`, la suivante part quand le
  matcher a écrit `faces`/`no_face` (ou `timeout` après `--wait`). Mesuré :
  ~60 s par aller-retour (relève des DM par `bro_dm_daemon.sh`, pas le GPU),
  d'où le parallélisme, loin du TTL 30 min de la file ;
- progression dans `.ucloud/faceid_reprocess.json` (lue par
  `GET /api/cloud/status`), une instance par compte (`.ucloud/.reprocess.lock`).

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


- **« Qui d'autre maîtrise ça dans le coin ? »** (`services/skill_map.py`, `services/relay_io.py`, NIP-98, MULTIPASS de cette station) : `GET /api/skills/mine` (mes certificats + visibilité + position), `POST /api/skills` (`skill, level, description, icon, visibility none|region|sector|umap, lat?, lon?`), `DELETE /api/skills?skill=`, `GET /api/skills/nearby?skill=&scope=umap|sector|region&min_level=&lat?&lon?`, `GET /api/skills/of?hex=`. Deux événements par savoir-faire : le **certificat** kind 30503 signé par le MULTIPASS (`d=PERMIT_<SKILL>_X<n>`, `t=<skill>`, x1 auto-proclamé, expire à 260 j comme `emit_skill.sh`) = seule source de vérité ; l'**annuaire du coin** kind 30078 `d=skillmap:<skill>:<hex membre>` (`l=skillmap`, `t`, `p`, `level`, `a=30503:…`) signé par la clé de la CASE — UMAP (≈ 1 km), SECTOR (≈ 11 km), REGION (≈ 111 km), dérivées de `~/.ipfs/swarm.key` + coordonnées EXACTEMENT comme `NOSTR.UMAP.refresh.sh` (`keygen -t nostr`, UMAP = deux sels `${U}${LAT}` / `${U}${LON}`, SECTOR/REGION = `${U}_43.9_2.1` / `${U}_43_2` ; vérifié sur la clé UMAP de la station). Visibilité = le niveau choisi ET les plus larges (`umap` → 3 cases, `sector` → 2, `region` → 1, `none` retire tout, le certificat reste ; défaut `sector`). La recherche **revérifie chaque annonce** contre le certificat du membre (existe, non expiré) et retient le niveau du CERTIFICAT : un index falsifié ne crée pas de fausse compétence. Position = fichier `GPS` du MULTIPASS ou `lat/lon`. Relais muet → 503 (jamais « personne » à tort). Relais : `NIP-101/.../filter/30078.sh` accepte `d=skillmap:*` si le membre `p` est un MULTIPASS connu (la clé de case n'est pas un MULTIPASS). Tests : `tests/test_skill_map.py`.
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
  `[{id, name, pubkey, email, timestamp, has_photo, photo, sent, maybe, group_id, group_size, x, y}]`
  (scroll Qdrant PAGINÉ, jusqu'à `_FACES_MAX=20000` points, `truncated` sinon ;
  `photo` = `source_path`, `sent` = dernier envoi via `person-send`, `email` =
  email de contact). Un visage est « nommé » s'il a une `pubkey` OU un nom ne
  commençant pas par `Inconnu` (`_is_named()`).
  `maybe` (depuis 2026-09-25, pour les archives longue durée) : sur une entrée
  SANS pubkey, `{name, pubkey, score}` du visage déjà nommé le plus proche par
  cosinus quand le score tombe dans `[0.55, 0.82[` (sous le seuil de match
  automatique `satellite_face_matcher.MATCH_THRESHOLD`, mais assez proche pour
  être la même personne à un autre âge). `group_id`/`group_size` (depuis
  2026-09-30) : regroupement ENTRE ELLES des entrées non identifiées — depuis
  le 2026-10-12 par k plus proches voisins MUTUELS (`_mutual_knn_groups()`,
  k=8, cosinus ≥ `_MAYBE_SAME_MIN=0.55`, groupe > 120 redécoupé avec un seuil
  relevé de 0,05) : l'ancien union-find transitif enchaînait A–B–C… en
  groupes géants mélangeant plusieurs personnes. But : la même personne
  détectée sur plusieurs photos, pas encore identifiée ; présent seulement
  pour un groupe d'au moins 2 visages. `x`/`y` (depuis 2026-10-02) :
  projection PCA 2D de l'embedding (`_pca_2d()`, SVD numpy), normalisée dans
  [-1, 1] — ne sert plus qu'à ordonner les visages isolés sur la table de tri
  (`face-board.js`, qui empile les grappes en disques) de `ucloud.html`. Les trois
  sont calculés serveur par `_annotate_faces()` (numpy, produits matriciels
  par blocs de 512, dans un thread — ~1,5 s pour 6000 visages ; plus de
  plafond à 400 visages comme du temps du Python pur O(n²)) — les vecteurs
  512D ne sont jamais renvoyés au client, et rien n'est persisté dans Qdrant
  (recalculé à chaque appel). `maybe` compare aux visages NOMMÉS (clé OU nom).
  UI : bandeau « est-ce X ? » + halos de groupe sur la table de tri de `ucloud.html`
- `POST /mailjet/faces-edit` — Nomme un visage / l'associe à un pubkey (64 hex) et/ou un `contact_email` (vide = inchangé, `-` = effacé)
- `POST /mailjet/faces-edit-bulk` — Même chose pour plusieurs `point_id` à la
  fois (`point_ids` séparés par des virgules, 2000 max, `contact_email` idem) : un seul `set_payload`
  Qdrant sur toute la liste — permet d'attribuer une identité commune à un
  groupe suggéré (ou une sélection manuelle) en un seul appel NIP-98 plutôt
  qu'un aller-retour par photo
- `POST /mailjet/faces-delete` — Oublie un ou plusieurs visages (`point_id`, ou `point_ids` séparés par des virgules, 2000 max ; vecteurs supprimés, photos conservées)
- `POST /mailjet/faces/thumbs` — miniatures recadrées de PLUSIEURS visages (`point_ids`, 80 max, `size` 48–320 ; la table demande 96, puis 256 pour les visages affichés en grand au zoom) → `{thumbs:{id: data URL}}`. Mesuré sur 12 Mpx : ipfs cat ~90 ms, AES ~5 ms, décodage JPEG ~135 ms — le DÉCODAGE domine, d'où : chaque photo source décodée une seule fois pour tous ses visages du lot et à la résolution juste nécessaire (`_crop_faces()`, `Image.draft`), 4 photos en parallèle, et **cache disque CHIFFRÉ** (`.ucloud/thumbcache/*.uenc`, AES-256-GCM avec la clé du compte `.ucloud/thumbcache.key`, 0600 — jamais de clair sur disque). Lot de 80 : ~3,7 s à froid, instantané ensuite, y compris après redémarrage du service
- `POST /mailjet/person-send` — « Envoyer ses photos » (`point_ids` d'une personne, `name`, `pubkey` OU `to_email`) : MULTIPASS sur cette station → `share_copy` de toutes les photos en tâche de fond + UN email récapitulatif ; MULTIPASS ailleurs (`search_for_this_email_in_nostr.sh`) → `satellite_face_matcher.py --send` (NIP-17, une photo par message) ; aucun MULTIPASS → invitation email (jeton `.face_invites/` 7 jours portant TOUTES les photos, aperçu des 4 premières). Mémorise `name`/`pubkey`/`email` et `sent:{at, mode, photos}` sur les points. Geste explicite : pas de condition de suivi mutuel (exigée seulement pour le partage AUTOMATIQUE du matcher). Remplace `POST /mailjet/faces-invite` (supprimée : 4 photos max, inconnus seulement). `process_pending_face_claims()` (après une création MULTIPASS locale) passe elle aussi par `share_copy` — plus d'enrôlement supervisé du destinataire sur des photos de groupe
- `GET  /mailjet/faces/thumbnail?point_id=…` — Miniature JPEG recadrée sur
  `bbox` (marge 50%) à partir de `source_path` : résout l'entrée
  `.ucloud/index.json` du propriétaire → CID chiffré → clé `.ucloud/keyring.json`
  → `ipfs cat` → `uenc_codec.decrypt_aes256gcm()` → recadrage Pillow →
  réponse JPEG directe. Jamais persistée en clair (même discipline que le
  GET `/dav/`). 404 si `source_path` absent (points pré-2026-09-20) ou si la
  photo/clé a depuis été supprimée du cloud chiffré.
- `GET  /mailjet/faces/photo?point_id=…[&max_side=1024][&mark=1]` — Même
  résolution que la miniature ci-dessus, mais photo ENTIÈRE, ≤`max_side` px
  (256–2048 ; décodage réduit `_open_reduced()`), `mark=1` entoure le visage
  d'une ellipse (le retrouver sur une photo de groupe). Cache MÉMOIRE de la
  photo redimensionnée (80 entrées) : survoler plusieurs visages d'une même
  photo ne la redéchiffre qu'une fois — aperçu au survol d'un point dans la vue
  table de tri de `ucloud.html` (la vignette identifie le visage, cette route
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


### account.py — « Mon compte » du MULTIPASS (NIP-98, remplace /mailjet à terme)
- `GET  /api/account/overview` — état complet (offre, quota, g1pub, sécurité, flux de notification, mémoire) ; interface : `UPlanet/earth/moncompte.html`
- `POST /api/account/prefs` — canaux e-mail/NOSTR par flux + préférences KIN (champs présents seulement, clés inconnues refusées)
- `POST /api/account/pass`, `POST /api/account/pass/reveal` — bloquer/réactiver la récupération par PASS ; révéler le PASS (POST, no-store)
- Même stockage que `/mailjet` (`.mailjet`) : les deux écrans restent cohérents pendant la transition. `/mailjet` exige désormais TOUJOURS un jeton valide (absence de jeton → page de connexion).

### savoirs.py — révisions chaînées (logique git sur 30500-30505)
- `POST /api/savoirs/pin` (NIP-98) — épingle un événement de savoir signé, renvoie son CID (JSON canonique)
- `GET  /api/savoirs/history?cid=` — remonte la chaîne `prev` (public, lecture seule). Convention : `UPlanet/earth/CLAUDE.md`

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

## Carte station — `services/ustats.py` + surveillance `/health`

- `GET /` (alias `/api/ustats`) sert la carte station + swarm depuis le cache RAM de `core/state.py` (relu toutes les 60 s dans `~/.zen/tmp/Ustats.json`, **sans fork**) ; `lat`/`lon`/`deg` filtrent ce cache en Python (`filter_area`, plus de fichier par requête). Cache absent → 503.
- Génération : `python3 -m services.ustats [lat lon deg] [--refresh] [--stdout]` dans un process COURT détaché (flock, écriture atomique), lancé par `core/state.py` seulement si le cache manque ou a plus d'1 h. `Astroport.ONE/Ustats.sh` n'est plus qu'un wrapper vers ce module (même contrat : dernière ligne = chemin du JSON). Remplace l'ancien script bash ; la liste NOSTR est désormais remplie (l'ancien appelait `process_nostr`, défini nulle part) et `DISTANCE_KM` est un vrai haversine. Tests : `tests/test_ustats.py`.
- Pourquoi : un fork périodique depuis ce process (1,7 Go, ~40 threads) s'est interbloqué le 2026-10-23 et a figé l'API ~5 h sans aucune trace. **Ne jamais forker depuis le process FastAPI** pour une tâche périodique : process détaché (`start_new_session`).
- **Surveillance** (`watchdog/upassport-watchdog.sh`, appelé par la boucle de `Astroport.ONE/_12345.sh` avec les autres watchdogs, ~300 s) : 3 essais de `/health` (10 s, espacés de 15 s) tous en échec → trace des threads par py-spy dans `~/.zen/log/upassport_watchdog/hang-*.txt` PUIS `sudo systemctl restart upassport`. Journal : `~/.zen/log/upassport_watchdog/watchdog.log`. `_12345.sh` doit être relancé pour prendre en compte ce bloc.

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
- **Vidéos FUN pour des amis** (`services/story_assistant.py`, `story_person.py`, `story_share.py`) :
  - `GET /api/story/people` — personnes NOMMÉES de l'ucloud (Qdrant `faces_<hex>`, ≥ 2 visages), `sample_ids` = leurs 8 plus grands visages ; vignettes par `POST /mailjet/faces/thumbs`.
  - `POST /api/story/portrait-candidates {point_ids}` → `{candidates:[{id, preview, score}], suggested:[id…]}` : les photos de la personne (une par cliché, la plus nette d'abord) pour CHOISIR le visage ; `suggested` = sélection automatique (la meilleure + jusqu'à 2 autres assez nettes, `story_person.auto_pick`).
  - `POST /api/story/character-from-person {name, point_ids, photo_ids?, portrait_only?, consent:true, render?}` — personnage **PRIVÉ** : sans `photo_ids`, sélection automatique ; avec (ordre = priorité, 4 au plus, dédoublonné) exactement ces photos : la première = `portrait.png`, les autres `portrait_2.png`… (références de visage supplémentaires : MiniMax H3 ref2va accepte 9 images). `portrait_only` (personnage existant) ne remplace QUE les photos (anciennes vues retirées ; fiche, apparence, voix intactes). Portrait = carré « tête et épaules » 1024 px (`story_person.crop_portrait/rank_portraits`), `look` + `voice_design` décrits par le modèle de vision (`analysis.DESCRIBE_MODEL`, repli générique), fiche `cast/<nom>/character.json` avec `source:"facecloud"`, `private_only:true` et `consent` ; refuse sans `consent: true` (booléen exact) ; rappel du même nom = nouvelle VERSION du personnage (pas de doublon) ; `render` lance la voix (job). Le consentement de la personne est une exigence du Studio (« jamais le visage ou la voix d'une personne réelle sans son accord »).
  - Assistant (même conversation que `generate_storyboard.sh`) : `GET /api/story/assistant/models`, `POST …/assistant/chat {messages, cast[], model?}`, `POST …/assistant/storyboard {messages, cast[], model?}` (JSON strict, validé par `generate_scene.sh --check`, 3 tentatives de correction), `POST /api/story/scene-from-storyboard {name, storyboard}`. Modèle de texte : `FACECLOUD_STORY_MODEL` (défaut `gemma3:12b`, repli `gemma3:latest`/`qwen2.5`/`llama3.2` ; les modèles de vision, d'embedding, de code sont exclus). La consigne système est TOUJOURS reconstruite côté serveur (le client n'envoie que `user`/`assistant`). **Consignes = `Astroport.ONE/IA/generators/lib/storyboard_prompts.json`** (`system` avec `{{CAST}}`, `final`, `web_extra`), lues par le bash ET par le web : une seule source (un test échoue si le script en réembarque une copie). 503 si Ollama est injoignable, 422 pour les erreurs de contenu.
  - Envoi : `POST /api/story/share {version_cid, render_id, title?, to?, days?}` → lien secret (jeton 32+ car., 14 jours, max 60, révocable : `GET /api/story/shares`, `DELETE /api/story/share/{token}`), rangé dans `<bibliothèque>/shares/`. **Public** : `GET /api/story/watch/{token}` (page de lecture sans script, `noindex`) et `…/video` (Range 206/416 géré à la main : Starlette 0.36 ne le fait pas, iOS en a besoin). Sans le jeton rien n'est accessible ; le jeton ne donne que ce rendu.
  - **Voix enregistrée, décor, durée** : `POST /api/story/asset/{cid}/voice` (audio uploadé → `voice.wav` du personnage, remplace la voix générée ; `voice_source:"upload"`) ; `scene-from-storyboard` accepte `decor` (image du décor, passée à MiniMax en `-R` : « Use <Picture N> as the setting ») et `shoot {max_seconds 5–12, brief}` (5 s = minimum du moteur MiniMax, jamais de raccourcissement : l'assistant adapte les plans pour que leur somme ≤ durée choisie).
  - **/VideoFUN/ (dossier DAV de l'ucloud)** — `services/videofun.py` : chaque rendu est COPIÉ, chiffré comme tout fichier du cloud, sous `/VideoFUN/<ami>/<AAAA-MM-JJ> <titre>.mp4` (visible par tout client DAV, compté dans le quota, sauvegardé). Métadonnées dans l'entrée d'index (`entry["videofun"]` : to, contact = UID de la fiche du carnet, title, cast, scene, duration…) : elles suivent le fichier. Hook `story_render.ON_SCENE_DONE` (échec → `job.render.videofun_error`, jamais silencieux) ; `POST /api/story/render/{version_cid}/{render_id}/to-ucloud` pour ranger à la main. API ucloud (NIP-98) : `GET /api/cloud/videofun` (liste), `GET …/file?path=` (déchiffrée en mémoire), `DELETE /api/cloud/videofun?path=` — toutes bornées à `/VideoFUN/` (`videofun.in_root`). `POST /api/story/share` accepte `{path, title, to}` pour une vidéo de `/VideoFUN/` (jeton `source.kind:"ucloud"` ; supprimer la vidéo = le lien répond 404). Tests : `TestVideoFun` (test_cloud_storage.py), test_story_router.py.
  - **Fiche d'un ami** : `GET /api/cloud/friend?name=&hex=&contact=` (`services/friend_card.py`, lecture seule) = photos reçues de lui (`/Reçues/<nom>/`), vidéos FUN pour lui (`/VideoFUN/<nom>/` ou `entry.videofun.contact`), liens d'envoi valables vers lui avec leurs vues (Capitaine seulement). Les photos où il apparaît et celles qu'on lui a envoyées viennent déjà des visages (`/mailjet/faces`, `sent`). Test : `test_friend_card_gathers_…`.
  - **Aperçu du lien** : la page publique porte `og:image` / `twitter:image` (absolus) = `GET /api/story/watch/{token}/poster.jpg` (PUBLIC, une image de la vidéo par ffmpeg `videofun.poster_frame`, calculée une fois et cachée dans `shares/<jeton>.jpg`, effacée avec le lien) et `poster=` du lecteur : l'aperçu WhatsApp/Telegram montre la vidéo.
  - **Envoi cloud à cloud (sans lien)** : `POST /api/cloud/videofun/send` (`path`, `pubkey` ou `to_email`) → `videofun.send_to_friend` : même station = `share_copy` (même CID, même clé), autre station de la constellation = canal inter-NODE `ucloud_share` (`node_share.send`) ; rangée chez l'ami sous `/VideoFUN/Reçues/<expéditeur>/` (`adopt_blob` : toute vidéo reçue va là, visible dans son onglet Vidéos ; `list_videos` marque `received`, `from_name`). 404 `no_account` si l'ami n'a pas de MULTIPASS connu → l'interface propose le lien. La fiche d'ami (`/api/cloud/friend`) liste `videos_received`.
  - **Les plans se suivent** : `story_assistant.chain_shots` (appelé par `apply_shoot`, donc à chaque scène créée par l'assistant) pose `continue: true` sur tout plan IA qui en suit un autre (sauf image de départ propre, carton, écran) ; `generate_scene.sh` fait repartir ce plan de la dernière image du précédent (image de départ ; avec acteur/décor : référence de plus « même lieu, lumière, cadre, tenue, pose » — proche, pas au pixel près, car ref2va n'accepte pas d'image de départ).
  - **« C'est moi »** : `/api/story/people` marque `me` (clé NOSTR du Capitaine) ; un seul visage suffit. L'assistant propose un micro direct (phrase à lire) envoyé à `/api/story/asset/{cid}/voice` après `character-from-person {render:false}` : voix calibrée sans voix générée.
  - **Pas encore construit** : le calcul de la vidéo par un autre NODE via DM internode (protocole `story_render_job` / `story_render_result` calqué sur `vision_analysis_job`) — décision de confidentialité en attente (portrait et voix de personnes réelles quitteraient la station).
- Jobs : process détachés (`start_new_session`), un seul à la fois (file `queued`), état dans `library/jobs/*.json`,
  `progress.json` écrit par `generate_scene.sh` / `generate_character.sh`. Rendus archivés par version (`renders/<auteur>-<d>/v/<cid>/<job>/`),
  coop : mp4 ajouté à IPFS et annoncé dans l'événement (`renders`). Tests : `tests/test_story_router.py`.
