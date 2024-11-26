L'API Jaklis est une interface pour interagir avec les commandes de Jaklis, un système de messagerie, des profils utilisateur, et le trading GVA (Zen Cash). Voici un aperçu détaillé de chaque fonctionnalité :

### 1. Commande `read`
Lecture des messages.

**Sous-arguments :**
- `-n`, `--number` : Nombre de messages à afficher (par défaut: 3).
- `-j`, `--json` : Affiche le résultat au format JSON.
- `-o`, `--outbox` : Lit les messages envoyés plutôt que ceux reçus.

**Exemple :**
```bash
jaklis.py read -n 5 -j
```

### 2. Commande `send`
Envoi d'un message.

**Sous-arguments :**
- `-d`, `--destinataire` : Destinataire du message (obligatoire).
- `-t`, `--titre` : Titre du message à envoyer.
- `-m`, `--message` : Message à envoyer.
- `-f`, `--fichier` : Envoyer le message contenu dans un fichier.
- `-o`, `--outbox` : Envoi le message sur la boite d'envoi.

**Exemple :**
```bash
jaklis.py send -d utilisateur123 -t "Nouveau message" -m "Salut, ça va?"
```

### 3. Commande `delete`
Suppression d'un message.

**Sous-arguments :**
- `-i`, `--id` : ID(s) du/des message(s) à supprimer (obligatoire).
- `-o`, `--outbox` : Suppression d'un message envoyé plutôt qu’un reçu.

**Exemple :**
```bash
jaklis.py delete -i 12345
```

### 4. Commande `set`
Configurer son profile Cesium+.

**Sous-arguments :**
- `-n`, `--name` : Nom du profile.
- `-d`, `--description` : Description du profile.
- `-v`, `--ville` : Ville associée au profil.
- `-a`, `--adresse` : Adresse associée au profil.
- `-p`, `--position` : Coordonnées géographiques (latitude, longitude).
- `-s`, `--site` : Site web ou URL associée au profile.
- `-t`, `--avatar` : Chemin vers l’image de profil.

**Exemple :**
```bash
jaklis.py set -n "Utilisateur123" -d "Description du profile" -v "Marseille" -a "Rue des Étoiles" -p "48.8566,2.3522" -s "http://example.com" -t "/path/to/avatar.jpg"
```

### 5. Commande `get`
Afficher les informations d'un profile.

**Sous-arguments :**
- `-p`, `--profile` : Nom du profile (obligatoire).
- `-a`, `--avatar` : Affiche l'avatar associé au profil plutôt que les autres informations.

**Exemple :**
```bash
jaklis.py get -p "Utilisateur123"
```

### 6. Commande `erase`
Effacer son profile Cesium+.

**Exemple :**
```bash
jaklis.py erase
```

### 7. Commande `stars`
Gérer les likes sur un message (enregistrer un like).

**Sous-arguments :**
- `-n`, `--number` : Nombre de likes à afficher.
- `-p`, `--profile` : Nom du profile dont on veut voir les likes.

**Exemple :**
```bash
jaklis.py stars -n 10
```

### 8. Commande `unstars`
Annuler un like sur un message.

**Sous-arguments :**
- `-p`, `--profile` : Nom du profile dont on veut annuler le like.

**Exemple :**
```bash
jaklis.py unstars -p "Utilisateur123"
```

### 9. Commande `getoffer`
Afficher les détails d'une offre GVA.

**Sous-arguments :**
- `-i`, `--id` : ID de l'offre à afficher (obligatoire).

**Exemple :**
```bash
jaklis.py getoffer -i 12345
```

### 10. Commande `setoffer`
Configurer une nouvelle offre GVA.

**Sous-arguments :**
- `-t`, `--title` : Titre de l'offre.
- `-d`, `--description` : Description de l'offre.
- `-c`, `--city` : Ville où se trouve l’offre.
- `-l`, `--localisation` : Coordonnées géographiques (latitude, longitude) de l'offre.
- `-e`, `--category` : Catégorie de l'offre.
- `-p`, `--price` : Prix de l'offre.
- `-i`, `--picture` : Chemin vers l'image de l'offre.

**Exemple :**
```bash
jaklis.py setoffer -t "Vente d'un vélo" -d "Vélo en bon état" -c "Lyon" -l "48.7214,2.3569" -e "Vélos" -p 100 -i "/path/to/bicycle.jpg"
```

### 11. Commande `deleteoffer`
Supprimer une offre GVA.

**Sous-arguments :**
- `-i`, `--id` : ID de l'offre à supprimer (obligatoire).

**Exemple :**
```bash
jaklis.py deleteoffer -i 12345
```

### 12. Commande `pay`
Effectuer un paiement via GVA.

**Sous-arguments :**
- `-p`, `--pubkey` : Pubkey du destinataire (obligatoire).
- `-a`, `--amount` : Montant du paiement (obligatoire).
- `-c`, `--comment` : Commentaire associé au paiement.
- `-m`, `--mempool` : Utiliser les sources en mempool plutôt que la blockchain principale.
- `-v`, `--verbose` : Afficher le résultat JSON de la transaction.

**Exemple :**
```bash
jaklis.py pay -p "pubkey_utilisateur" -a 10.50 -c "Paiement pour le vélo"
```

### 13. Commande `history`
Afficher l'historique des transactions GVA.

**Sous-arguments :**
- `-p`, `--pubkey` : Clé publique du compte visé.
- `-n`, `--number` : Nombre de transactions à afficher (par défaut: 10).
- `-j`, `--json` : Afficher le résultat au format JSON.
- `--nocolors` : Afficher le résultat en noir et blanc.

**Exemple :**
```bash
jaklis.py history -p "pubkey_utilisateur" -n 5 -j --nocolors
```

### 14. Commande `balance`
Afficher l'équilibre du compte GVA.

**Sous-arguments :**
- `-p`, `--pubkey` : Clé publique du compte visé.
- `-m`, `--mempool` : Utiliser les sources en mempool plutôt que la blockchain principale.

**Exemple :**
```bash
jaklis.py balance -p "pubkey_utilisateur"
```

### 15. Commande `id`
Afficher les informations d'un utilisateur GVA via sa clé publique ou son username.

**Sous-arguments :**
- `-p`, `--pubkey` : Clé publique de l'utilisateur (obligatoire si `-u` non défini).
- `-u`, `--username` : Username de l’utilisateur (obligatoire si `-p` non défini).

**Exemple :**
```bash
jaklis.py id -p "pubkey_utilisateur"
```

### 16. Commande `idBalance`
Afficher les informations d’un utilisateur GVA et son équilibre.

**Sous-arguments :**
- `-p`, `--pubkey` : Clé publique de l'utilisateur (obligatoire).

**Exemple :**
```bash
jaklis.py idBalance -p "pubkey_utilisateur"
```

### 17. Commande `currentUd`
Afficher le taux de change actuel du USD/ZEN.

**Sous-arguments :**
- `-p`, `--pubkey` : Clé publique du compte visé (non utilisé dans cette commande).

**Exemple :**
```bash
jaklis.py currentUd
```

### Généralités :
- La commande nécessite une clé de trousseau (`dunikey`) pour s'authentifier. Elle peut être fournie via l'argument `-k` ou définie dans les variables d'environnement `DUNIKEY`.
- Les commandes peuvent être exécutées directement à partir du terminal.
- Certains sous-arguments sont obligatoires et doivent être spécifiés pour que la commande fonctionne correctement.

Cette API Jaklis offre une gamme complète de fonctionnalités pour gérer les messages, les profils utilisateur, et les transactions GVA, facilitant ainsi l'interaction avec le système.
