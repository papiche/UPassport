# Feedback Integration — POST /api/feedback

Ce document explique comment connecter n'importe quelle application de l'écosystème UPlanet
au endpoint `/api/feedback` de UPassport pour créer automatiquement des issues dans un dépôt
Git (GitHub ou GitLab auto-hébergé), avec fallback email via Mailjet.

## Vue d'ensemble

```
App (Flutter / Web / CLI / Bash)
  │
  └── POST {upassport}/api/feedback
          │
          ├── 1. Git issue ─────────────────────────────────────────────────────
          │     lit GIT_HOST / GIT_TOKEN / GIT_OWNER  ← kind 30800 (NOSTR DID)
          │     ├── GitHub  → POST https://api.github.com/repos/{owner}/{repo}/issues
          │     └── GitLab  → POST {host}/api/v4/projects/{owner}%2F{repo}/issues
          │
          ├── 2. Email (si Git absent ou inaccessible) ─────────────────────────
          │     mailjet.sh MJ_SENDER_EMAIL feedback.html "[feedback] {repo}: {title}"
          │     lit MJ_APIKEY_PUBLIC / MJ_APIKEY_PRIVATE / MJ_SENDER_EMAIL ← kind 30800
          │
          └── 3. Stockage local silencieux (si Mailjet également absent)
                retourne { "ok": true, "stored": "local" }
```

Toutes les stations du swarm partagent la même configuration via le DID NOSTR kind 30800,
chiffré AES-256-CBC avec `UPLANETNAME` comme clé. Aucun fichier `.env` nécessaire.

---

## Configuration (une seule fois, par le capitaine)

### 1. Générer un Personal Access Token (PAT)

**GitHub** : `Settings → Developer settings → Personal access tokens → Fine-grained tokens`
- Portée minimale : `Issues: Read and write` sur les dépôts cibles

**GitLab** : `User Settings → Access Tokens`
- Portée minimale : `api`

### 2. Enregistrer dans le DID coopératif (kind 30800)

```bash
source ~/.zen/Astroport.ONE/tools/cooperative_config.sh

# GitHub (par défaut)
coop_config_set GIT_HOST  "https://github.com"
coop_config_set GIT_TOKEN "ghp_votre_token"
coop_config_set GIT_OWNER "papiche"

# OU GitLab auto-hébergé
coop_config_set GIT_HOST  "https://git.p2p.legal"
coop_config_set GIT_TOKEN "glpat-votre_token"
coop_config_set GIT_OWNER "papiche"
```

Le token est chiffré automatiquement (contient "TOKEN") avec la clé UPLANETNAME.

### 3. Vérifier via economy.html

Dans l'onglet **API Keys** du widget Coopérative sur `economy.html`,
les champs `GIT_HOST`, `GIT_TOKEN`, `GIT_OWNER` sont éditables.

---

## Format de la requête

```
POST /api/feedback
Content-Type: application/x-www-form-urlencoded

title        = "Titre de l'issue"            (obligatoire)
description  = "Corps détaillé"              (obligatoire)
source       = "zelkova"                     (nom du dépôt cible, recommandé)
category     = "bug" | "feedback"            (défaut: "bug")
pubkey       = "npub1xxx..."                 (optionnel — identifiant NOSTR)
app_version  = "1.2.3"                       (optionnel)
platform     = "android" | "ios" | "web"    (optionnel)
```

### Routing source → dépôt

Le champ `source` est normalisé en minuscules et préfixé par `GIT_OWNER` :

| source      | dépôt créé          |
|-------------|---------------------|
| `zelkova`   | `papiche/zelkova`   |
| `troczen`   | `papiche/troczen`   |
| `coracle`   | `papiche/coracle`   |
| `G1BILLET`  | `papiche/g1billet`  |
| *(absent)*  | `papiche/zelkova`   |

### Réponse

Issue créée sur Git :
```json
{
  "ok": true,
  "stored": "git",
  "issue_url": "https://github.com/papiche/zelkova/issues/42",
  "issue_number": 42,
  "repo": "papiche/zelkova",
  "provider": "github"
}
```

Git absent → fallback email Mailjet au capitaine :
```json
{ "ok": true, "stored": "email", "message": "Feedback envoyé par email au capitaine" }
```

Git et Mailjet absents → stockage local silencieux :
```json
{ "ok": true, "stored": "local", "message": "Feedback reçu" }
```

Le champ `ok` est **toujours `true`** — le feedback ne bloque jamais l'utilisateur.

---

## Intégration Flutter (Dart)

### Pattern FeedbackService (utilisé par zelkova et TrocZen)

```dart
import 'dart:convert';
import 'package:http/http.dart' as http;

class FeedbackService {
  final String upassportUrl;
  const FeedbackService(this.upassportUrl);

  Future<FeedbackResult> send({
    required String title,
    required String description,
    required String source,      // nom du dépôt, ex: "coracle"
    String category = 'bug',
    String? pubkey,
    String? appVersion,
    String? platform,
  }) async {
    final resp = await http.post(
      Uri.parse('$upassportUrl/api/feedback'),
      body: {
        'title': title,
        'description': description,
        'source': source,
        'category': category,
        if (pubkey != null) 'pubkey': pubkey,
        if (appVersion != null) 'app_version': appVersion,
        if (platform != null) 'platform': platform,
      },
    );
    final data = jsonDecode(resp.body) as Map<String, dynamic>;
    return FeedbackResult(
      ok: data['ok'] as bool? ?? false,
      stored: data['stored'] as String? ?? 'local',
      issueUrl: data['issue_url'] as String?,
      issueNumber: data['issue_number'] as int?,
    );
  }
}

class FeedbackResult {
  const FeedbackResult({
    required this.ok,
    required this.stored,
    this.issueUrl,
    this.issueNumber,
  });
  final bool ok;
  final String stored;   // "git" | "local"
  final String? issueUrl;
  final int? issueNumber;
}
```

### Usage dans un écran Flutter

```dart
final feedback = FeedbackService(Env.upassportUrl);

final result = await feedback.send(
  title: 'Crash au démarrage',
  description: stackTrace,
  source: 'coracle',   // → papiche/coracle sur GitHub
  category: 'bug',
  pubkey: currentUser.npub,
  appVersion: packageInfo.version,
  platform: Platform.operatingSystem,
);

if (result.ok && result.issueUrl != null) {
  showSnackBar('Issue créée : ${result.issueUrl}');
}
```

---

## Intégration JavaScript (Web / UPlanet earth/)

```javascript
async function sendFeedback({ title, description, source, pubkey, category = 'bug' }) {
    const upassportUrl = window.upassportUrl || getUSPOTUrl();
    const body = new URLSearchParams({ title, description, source, category });
    if (pubkey) body.append('pubkey', pubkey);

    const resp = await fetch(`${upassportUrl}/api/feedback`, {
        method: 'POST',
        body,
    });
    return resp.json();
}

// Exemple
const result = await sendFeedback({
    title: 'Carte ne charge pas',
    description: 'welcome.html — la carte Leaflet reste vide sur Firefox 125',
    source: 'UPlanet',   // → papiche/uplanet (si ce dépôt existe)
    pubkey: window.userPubkey,
});
```

---

## Intégration Shell (Bash / Astroport)

```bash
USPOT="https://u.$(hostname -d):54321"

curl -s -X POST "${USPOT}/api/feedback" \
  -F "title=Script crash: heartbox_analysis.sh" \
  -F "description=$(cat /tmp/error.log)" \
  -F "source=Astroport.ONE" \
  -F "category=bug"
```

---

## Intégration Python (scripts Astroport / UPassport)

```python
import httpx

async def send_feedback(upassport_url: str, title: str, description: str,
                        source: str, category: str = "bug") -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{upassport_url}/api/feedback",
            data={"title": title, "description": description,
                  "source": source, "category": category},
        )
    return resp.json()
```

---

## Ajouter un nouveau projet au swarm

Pour que les issues d'un nouveau projet `papiche/monprojet` arrivent au bon endroit :

1. Créer le dépôt sur GitHub/GitLab sous le compte `GIT_OWNER`
2. Passer `source="monprojet"` dans les appels `/api/feedback` de l'app
3. S'assurer que le PAT enregistré dans kind 30800 a accès à ce dépôt

Aucun changement de configuration côté UPassport n'est nécessaire — le routing est
entièrement dynamique.

---

## Sécurité

- `GIT_TOKEN` est chiffré AES-256-CBC dans le kind 30800 (clé = `UPLANETNAME`).
- Le endpoint `/api/feedback` est public (pas d'auth requise) mais rate-limité par
  `RateLimitMiddleware` comme tous les endpoints UPassport.
- La valeur `source` est validée par regex `[a-z0-9][a-z0-9\-\.]{0,98}` avant usage.
- En cas d'erreur réseau ou de token invalide, le feedback est accepté silencieusement
  (`stored: "local"`) pour ne jamais bloquer l'utilisateur.
