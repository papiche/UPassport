# NIP-42 Security Architecture — UPassport / UPlanet

> **Last updated:** 2026-03  
> **Status:** Implemented & tested (25/25 unit tests)

---

## Contexte : Pourquoi NIP-42 est-il difficile à sécuriser ?

Le kind **22242** (NIP-42 Client Authentication) est dans la plage éphémère **20 000-29 999** définie par NIP-01.
`strfry` et la plupart des relais transmettent ces événements aux abonnés actifs **mais ne les stockent pas** en base.
Un `REQ` ou `strfry scan` ultérieur ne les retrouvera donc jamais.

UPassport utilise un **fichier marqueur local** créé par le plugin relay (`filter/22242.sh`) ou par les scripts shell (`ajouter_media.sh`) comme preuve d'une authentification récente, sans requête relay.

---

## Vecteurs d'attaque identifiés (et leur correction)

### A. Challenge statique → rejoue d'événement

| Avant | Après |
|---|---|
| Challenge `"auth-" + timestamp` généré localement | Nonce 256 bits (`secrets.token_hex(32)`) généré **par l'API** |
| Même challenge pour toutes les sessions | Nonce unique par pubkey, usage unique (consommé après vérification) |

**Attaque bloquée :** Un attaquant qui intercepte un événement 22242 signé ne peut pas le rejouer car le nonce a déjà été consommé par l'API.

### B. TTL trop long (1 heure → 5 minutes)

| Avant | Après |
|---|---|
| `NIP42_MARKER_MAX_AGE = 3600` (1 heure) | `NIP42_MARKER_MAX_AGE = 300` (5 minutes) |

**Attaque bloquée :** Un marker volé ou copié expire en 5 min, réduisant la fenêtre d'exploitation.

### C. Confusion de pubkey (marker générique)

| Avant | Après |
|---|---|
| `.nip42_auth` (nom générique, partageable) | `.nip42_auth_<hex_pubkey>` (lié à l'identité) |
| Fichier vide (aucune preuve d'identité) | JSON `{"pubkey","event_hash","created_at"}` |

**Attaque bloquée :** Le marker d'Alice ne peut pas authentifier Bob (même répertoire partagé).  
La pubkey en JSON est cross-vérifiée avec le suffixe du nom de fichier.

---

## Architecture du flux sécurisé

```
┌─────────────┐          ┌──────────────────┐          ┌─────────────────────┐
│  JS App     │          │  API (54321.py)  │          │  Relay (strfry)     │
│  Shell Sc.  │          │                  │          │  + filter/22242.sh  │
└──────┬──────┘          └────────┬─────────┘          └──────────┬──────────┘
       │                          │                               │
       │  1. GET /api/nip42/challenge?npub=<hex>                  │
       │─────────────────────────►│                               │
       │  ← {"challenge":"abc…"} │ ← nonce 64-hex, TTL 120s      │
       │                          │   stocké en mémoire           │
       │                          │                               │
       │  2. kind 22242 signé     │                               │
       │     tags: [              │                               │
       │       ["relay","ws://…"],│                               │
       │       ["challenge","abc…"]│                              │
       │     ]                    │                               │
       │──────────────────────────────────────────────────────────►
       │                          │   filter/22242.sh intercepte  │
       │                          │   ✅ EMAIL reconnu ?           │
       │                          │   écrit:                      │
       │                          │  .nip42_auth_<hex>            │
       │                          │  {"pubkey","event_hash",      │
       │                          │   "created_at"}               │
       │                          │                               │
       │  3. Appel API protégé (ex: GET /api/myGPS)              │
       │─────────────────────────►│                               │
       │                          │  check_nip42_auth_local_marker():
       │                          │  A. nom fichier = .nip42_auth_<hex> ✓
       │                          │  B. Age < 300 s ✓             │
       │                          │  C. JSON pubkey == hex ✓      │
       │                          │  D. event_hash valide (64 hex) ✓
       │  ← données GPS           │                               │
```

---

## Fichiers impliqués

### Côté API (Python)

| Fichier | Rôle |
|---|---|
| [`services/nostr.py`](../services/nostr.py) | `check_nip42_auth_local_marker()`, `generate/get/consume_nip42_challenge()` |
| [`routers/geo.py`](../routers/geo.py) | `GET /api/nip42/challenge`, `/api/myGPS` avec challenge dans 403 |

### Côté relay (Bash)

| Fichier | Rôle |
|---|---|
| [`NIP-101/relay.writePolicy.plugin/filter/22242.sh`](../../NIP-101/relay.writePolicy.plugin/filter/22242.sh) | Crée `.nip42_auth_<hex>` JSON lors de la réception d'un kind 22242 valide |

### Côté scripts locaux (Bash)

| Fichier | Rôle |
|---|---|
| [`Astroport.ONE/ajouter_media.sh`](../../Astroport.ONE/ajouter_media.sh) | Obtient challenge API, envoie kind 22242, écrit marker JSON |
| [`Astroport.ONE/tools/oracle.WoT_PERMIT.init.sh`](../../Astroport.ONE/tools/oracle.WoT_PERMIT.init.sh) | `send_nip42_auth()` : extrait HEX, challenge API, marker JSON |

### Côté front-end (JavaScript)

| Fichier | Rôle |
|---|---|
| [`UPlanet/earth/common.js`](../../UPlanet/earth/common.js) | `sendNIP42Auth()` : fetch `/api/nip42/challenge`, signe kind 22242 |
| [`UPassport/templates/astro_base.html`](../templates/astro_base.html) | `sendNIP42AuthForNsec()` : idem pour mode nsec |

---

## Format du marker

```json
{
  "pubkey": "3bf0c63fcb93463407af97a5e5ee64fa883d107ef9e558472c4eb9aaaefa459d",
  "event_hash": "b1a2c3d4...e5f6a7b8...c9d0e1f2...a3b4c5d6...e7f8a9b0...c1d2e3f4",
  "created_at": 1742341200
}
```

Fichier : `~/.zen/game/nostr/<email>/.nip42_auth_3bf0c63f…`

### Règles de validation (`check_nip42_auth_local_marker`)

1. **Nom de fichier** : doit se terminer par `_<hex_pubkey>` (64 chars hex)
2. **Age** : `time.time() - mtime < 300` (5 min)
3. **Pubkey JSON** : `data["pubkey"] == hex_pubkey` (insensible à la casse)
4. **event_hash** : si présent, doit être 64 chars `[0-9a-f]`

---

## Endpoint API challenge

```
GET /api/nip42/challenge?npub=<npub_ou_hex>

→ 200 OK
{
  "challenge": "a1b2c3d4...e5f6a7b8...c9d0e1f2...a3b4c5d6...e7f8a9b0...c1d2e3f4",
  "expires_in": 120,
  "pubkey_hex": "3bf0c63f...",
  "instruction": "Include this challenge in a kind-22242 Nostr event tag..."
}
```

- Nonce : `secrets.token_hex(32)` → 256 bits d'entropie
- TTL : **120 secondes** (stocké en mémoire, usage unique)
- Après consommation : `consume_nip42_challenge()` supprime le nonce

---

## Tests unitaires

```bash
~/.astro/bin/python -m pytest tests/test_nip42_auth.py -v -m "not live_relay"
# 25 passed
```

Cas couverts :
- `test_old_generic_marker_is_rejected` — ancien format `.nip42_auth` refusé
- `test_marker_for_different_pubkey_is_rejected` — confusion Alice/Bob bloquée
- `test_pubkey_mismatch_in_json_rejected` — falsification JSON détectée
- `test_invalid_event_hash_rejected` — hash malformé refusé
- `test_expired_marker_returns_false` — TTL 300 s (307 s → refusé)
- `test_marker_within_300s_is_valid` — 299 s → accepté
- `test_fresh_marker_returns_true` — JSON complet → accepté
- `test_empty_marker_accepted_with_legacy_warning` — compatibilité ascendante
- `test_nonexistent_directory_returns_false` — exception sans crash

---

## Considérations de sécurité résiduelles

| Risque | Niveau | Mitigation |
|---|---|---|
| API hors ligne au moment de la signature | Faible | Fallback timestamp-nonce (moins sûr mais fonctionnel) |
| Accès root au répertoire `~/.zen/game/nostr/` | Hors-scope | Isolation OS, permissions 0600 sur `.secret.nostr` |
| Signature Schnorr compromise (clé privée volée) | Cryptographique | Hors-scope NIP-42, problème NOSTR global |
| Relai malveillant re-publishant le kind 22242 | Faible | TTL 120 s sur le challenge API + TTL 300 s du marker |
