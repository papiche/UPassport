# Cookie Manager i18n System

## Architecture

Le système d'internationalisation (i18n) de Cookie Manager utilise:

1. **`/UPassport/static/js/simpl-i18n.js`** - Bibliothèque JavaScript légère pour charger et appliquer les traductions
2. **`/UPassport/static/i18n/cookie_translations.json`** - Fichier JSON contenant toutes les traductions
3. **Attributs `data-i18n`** dans le HTML pour marquer les éléments à traduire

## Comment ça fonctionne

### 1. Chargement des traductions

```javascript
const i18n = new SimplI18n('/static/i18n/cookie_translations.json', 'fr');
await i18n.init();
```

### 2. Application automatique

Les éléments HTML avec l'attribut `data-i18n` sont automatiquement traduits:

```html
<h1 data-i18n="hero_title">🍪 COOKIE MANAGER</h1>
<p data-i18n="hero_subtitle">Share Your Authenticated Sessions...</p>
```

### 3. Utilisation programmatique

Dans le code JavaScript, utilisez `i18n.t()`:

```javascript
alert(i18n.t('upload_connecting_first'));
status.innerHTML = `<div>⏳ ${i18n.t('auth_connecting')}</div>`;
```

## Ajouter une nouvelle langue

### Étape 1: Modifier le fichier JSON

Ouvrez `/UPassport/static/i18n/cookie_translations.json` et ajoutez une nouvelle clé de langue avec toutes les traductions:

```json
{
  "fr": {
    "title": "Cookie Manager - UPlanet MULTIPASS",
    "hero_title": "COOKIE MANAGER",
    ...
  },
  "en": {
    "title": "Cookie Manager - UPlanet MULTIPASS",
    "hero_title": "COOKIE MANAGER",
    ...
  },
  "de": {
    "title": "Cookie-Manager - UPlanet MULTIPASS",
    "hero_title": "COOKIE-MANAGER",
    "hero_subtitle": "Teilen Sie Ihre authentifizierten Sitzungen mit Ihrer UPlanet-KI",
    ...
  }
}
```

### Étape 2: Ajouter l'onglet de langue

Dans `/UPassport/templates/cookie.html`, ajoutez un nouvel onglet:

```html
<div class="language-tabs">
    <div class="language-tab active" data-lang="fr">Français</div>
    <div class="language-tab" data-lang="en">English</div>
    <div class="language-tab" data-lang="es">Español</div>
    <div class="language-tab" data-lang="de">Deutsch</div>  <!-- Nouvelle langue -->
</div>
```

### Étape 3: Tester

1. Rechargez la page
2. Cliquez sur le nouvel onglet de langue
3. Vérifiez que tous les textes sont traduits

## Structure du fichier de traduction

Le fichier `cookie_translations.json` contient les sections suivantes:

### Métadonnées
- `title` - Titre de la page (affiché dans l'onglet du navigateur)

### Navigation
- `nav_home` - Lien "Home"
- `nav_udrive` - Lien "uDRIVE"

### En-tête
- `hero_title` - Titre principal
- `hero_subtitle` - Sous-titre

### Authentification
- `auth_title` - Titre de la section d'authentification
- `auth_subtitle` - Description
- `auth_button` - Texte du bouton
- `auth_connecting` - Message pendant la connexion
- `auth_connected` - Message après connexion réussie
- `auth_error_extension` - Message d'erreur si l'extension NOSTR n'est pas trouvée

### Upload
- `upload_title` - Titre de la section d'upload
- `upload_drop` - Texte "Déposez votre"
- `upload_here` - Texte "ici"
- `upload_or_click` - Texte "ou cliquez pour sélectionner"
- `upload_format` - Description du format
- `upload_connecting_first` - Message si non connecté
- `upload_in_progress` - Message pendant l'upload
- `upload_success` - Message de succès
- `upload_failed` - Message d'échec

### Explications
- `what_cookies_title` - Titre "À quoi servent les cookies?"
- `what_cookies_intro` - Introduction
- `what_cookies_youtube` - Description YouTube
- `what_cookies_leboncoin` - Description Leboncoin
- `what_cookies_any` - Description générale
- `what_cookies_security` - Message de sécurité

### Instructions (5 étapes)
- `step1_title` - "Installer l'Extension Navigateur"
- `step1_content` - Instructions étape 1
- `step1_info` - Info supplémentaire
- `step2_title` - "Ouvrir une Fenêtre Privée"
- `step2_important` - Texte "IMPORTANT"
- `step2_content` - Instructions étape 2
- `step2_chrome` - Raccourcis Chrome
- `step2_firefox` - Raccourcis Firefox
- `step2_why` - "Pourquoi en privé?"
- `step2_why_desc` - Explication
- `step3_title` - "Se Connecter au Site"
- `step3_content` - Instructions étape 3
- `step3_1` à `step3_4` - Sous-étapes
- `step3_tip` - Conseil
- `step4_title` - "Exporter les Cookies"
- `step4_content` - Instructions étape 4
- `step4_1` à `step4_4` - Sous-étapes
- `step4_warning` - Avertissement
- `step5_title` - "Uploader vers MULTIPASS"
- `step5_content` - Instructions étape 5
- `step5_auto` - Message automatique
- `step5_button` - Texte du bouton

### FAQ
- `faq_title` - Titre de la FAQ
- `faq_q1` à `faq_q5` - Questions
- `faq_a1` à `faq_a5` - Réponses
- `faq_a5_title` - Titre de réponse 5
- `faq_a5_youtube`, `faq_a5_leboncoin`, `faq_a5_other` - Sous-réponses

## Langues actuellement supportées

1. **Français (fr)** - Langue par défaut
2. **Anglais (en)** - English
3. **Espagnol (es)** - Español

## Bonnes pratiques

1. **Clés cohérentes** - Utilisez des clés descriptives (ex: `auth_button` au lieu de `btn1`)
2. **HTML dans les traductions** - Si nécessaire, utilisez du HTML dans les traductions (ex: `<strong>texte</strong>`)
3. **Variables** - Pour les textes dynamiques, utilisez la concaténation JavaScript:
   ```javascript
   status.innerHTML = `${i18n.t('auth_connected')} ${username}`;
   ```
4. **Emojis** - Les emojis dans le HTML sont préservés automatiquement
5. **Fallback** - Si une traduction manque, la bibliothèque retourne la clé ou l'anglais par défaut

## Préférence de langue

La préférence de langue de l'utilisateur est:
- Sauvegardée dans `localStorage` sous la clé `preferred_language`
- Restaurée automatiquement lors du prochain chargement de la page
- Par défaut: français (`fr`)

## API de la bibliothèque i18n

### `new SimplI18n(translationsUrl, defaultLang)`
Crée une nouvelle instance du système i18n.

### `await i18n.init()`
Charge les traductions et initialise les onglets de langue.

### `i18n.t(key, lang?)`
Récupère une traduction pour une clé donnée.

### `i18n.setLanguage(lang)`
Change la langue courante et applique les traductions.

### `i18n.getCurrentLanguage()`
Retourne la langue courante.

### `i18n.getAvailableLanguages()`
Retourne la liste des langues disponibles.

## Exemple complet

```html
<!DOCTYPE html>
<html>
<head>
    <script src="/static/js/simpl-i18n.js"></script>
</head>
<body>
    <!-- Onglets de langue -->
    <div class="language-tabs">
        <div class="language-tab active" data-lang="fr">Français</div>
        <div class="language-tab" data-lang="en">English</div>
    </div>

    <!-- Contenu traduit -->
    <h1 data-i18n="welcome_title">Bienvenue</h1>
    <p data-i18n="welcome_text">Ceci est un exemple</p>

    <script>
        const i18n = new SimplI18n('/static/i18n/translations.json', 'fr');
        
        document.addEventListener('DOMContentLoaded', async function() {
            await i18n.init();
            
            // Utilisation programmatique
            console.log(i18n.t('welcome_title'));
        });
    </script>
</body>
</html>
```

## Dépannage

### Les traductions ne s'appliquent pas
- Vérifiez que le fichier JSON est accessible (pas d'erreur 404)
- Vérifiez la console JavaScript pour les erreurs
- Assurez-vous que `i18n.init()` est appelé avant d'utiliser `i18n.t()`

### Une langue ne fonctionne pas
- Vérifiez que la langue existe dans le fichier JSON
- Vérifiez que toutes les clés de traduction sont présentes
- Vérifiez l'attribut `data-lang` de l'onglet

### Le HTML n'est pas affiché correctement
- Si votre traduction contient du HTML, assurez-vous qu'il est valide
- La bibliothèque utilise `innerHTML` pour les traductions avec HTML

## Support

Pour toute question ou problème, consultez la documentation complète ou ouvrez un ticket sur le projet UPlanet.

