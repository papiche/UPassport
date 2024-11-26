Bien sûr ! Voici comment vous pouvez utiliser des commandes `curl` pour tester les différentes commandes de l'API `/jaklis` :

### Exemple de commande `read`
Lire les messages.

```bash
curl -X POST http://localhost:8000/jaklis \
-H "Authorization: Bearer <votre_dunikey>" \
-H "Content-Type: application/json" \
-d '{"command": "read", "params": {"number": 5, "json": true, "outbox": false}}'
```

### Exemple de commande `send`
Envoyer un message.

```bash
curl -X POST http://localhost:8000/jaklis \
-H "Authorization: Bearer <votre_dunikey>" \
-H "Content-Type: application/json" \
-d '{"command": "send", "params": {"destinataire": "utilisateur123", "titre": "Nouveau message", "message": "Salut, ça va?"}}'
```

### Exemple de commande `delete`
Supprimer un message.

```bash
curl -X POST http://localhost:8000/jaklis \
-H "Authorization: Bearer <votre_dunikey>" \
-H "Content-Type: application/json" \
-d '{"command": "delete", "params": {"id": 12345, "outbox": false}}'
```

### Exemple de commande `get`
Afficher les informations d'un profile.

```bash
curl -X POST http://localhost:8000/jaklis \
-H "Authorization: Bearer <votre_dunikey>" \
-H "Content-Type: application/json" \
-d '{"command": "get", "params": {"profile": "Utilisateur123"}}'
```

### Exemple de commande `set`
Configurer un profile Cesium+.

```bash
curl -X POST http://localhost:8000/jaklis \
-H "Authorization: Bearer <votre_dunikey>" \
-H "Content-Type: application/json" \
-d '{"command": "set", "params": {"name": "Utilisateur123", "description": "Description du profile", "ville": "Marseille", "adresse": "Rue des Étoiles", "position": "48.8566,2.3522", "site": "http://example.com", "avatar": "/path/to/avatar.jpg"}}'
```

### Exemple de commande `erase`
Effacer un profile Cesium+.

```bash
curl -X POST http://localhost:8000/jaklis \
-H "Authorization: Bearer <votre_dunikey>" \
-H "Content-Type: application/json" \
-d '{"command": "erase"}'
```

### Exemple de commande `stars`
Gérer les likes sur un message.

```bash
curl -X POST http://localhost:8000/jaklis \
-H "Authorization: Bearer <votre_dunikey>" \
-H "Content-Type: application/json" \
-d '{"command": "stars", "params": {"number": 10}}'
```

### Exemple de commande `unstars`
Annuler un like sur un message.

```bash
curl -X POST http://localhost:8000/jaklis \
-H "Authorization: Bearer <votre_dunikey>" \
-H "Content-Type: application/json" \
-d '{"command": "unstars", "params": {"profile": "Utilisateur123"}}'
```

### Exemple de commande `getoffer`
Afficher les détails d'une offre GVA.

```bash
curl -X POST http://localhost:8000/jaklis \
-H "Authorization: Bearer <votre_dunikey>" \
-H "Content-Type: application/json" \
-d '{"command": "getoffer", "params": {"id": 12345}}'
```

### Exemple de commande `setoffer`
Configurer une offre GVA.

```bash
curl -X POST http://localhost:8000/jaklis \
-H "Authorization: Bearer <votre_dunikey>" \
-H "Content-Type: application/json" \
-d '{"command": "setoffer", "params": {"id": 12345, "details": {"description": "Détails de l'offre", "price": 100}}}'
```

### Exemple de commande `deleteoffer`
Supprimer une offre GVA.

```bash
curl -X POST http://localhost:8000/jaklis \
-H "Authorization: Bearer <votre_dunikey>" \
-H "Content-Type: application/json" \
-d '{"command": "deleteoffer", "params": {"id": 12345}}'
```

### Exemple de commande `pay`
Effectuer une transaction GVA.

```bash
curl -X POST http://localhost:8000/jaklis \
-H "Authorization: Bearer <votre_dunikey>" \
-H "Content-Type: application/json" \
-d '{"command": "pay", "params": {"amount": 100, "recipient": "utilisateur123"}}'
```

### Exemple de commande `history`
Afficher l'historique des transactions.

```bash
curl -X POST http://localhost:8000/jaklis \
-H "Authorization: Bearer <votre_dunikey>" \
-H "Content-Type: application/json" \
-d '{"command": "history", "params": {"number": 10}}'
```

### Exemple de commande `balance`
Afficher le solde.

```bash
curl -X POST http://localhost:8000/jaklis \
-H "Authorization: Bearer <votre_dunikey>" \
-H "Content-Type: application/json" \
-d '{"command": "balance"}'
```

### Exemple de commande `id`
Afficher les informations d'un utilisateur.

```bash
curl -X POST http://localhost:8000/jaklis \
-H "Authorization: Bearer <votre_dunikey>" \
-H "Content-Type: application/json" \
-d '{"command": "id", "params": {"username": "utilisateur123"}}'
```

### Exemple de commande `idBalance`
Afficher le solde d'un utilisateur.

```bash
curl -X POST http://localhost:8000/jaklis \
-H "Authorization: Bearer <votre_dunikey>" \
-H "Content-Type: application/json" \
-d '{"command": "idBalance", "params": {"username": "utilisateur123"}}'
```

### Exemple de commande `currentUd`
Afficher l'unité de devis actuelle.

```bash
curl -X POST http://localhost:8000/jaklis \
-H "Authorization: Bearer <votre_dunikey>" \
-H "Content-Type: application/json" \
-d '{"command": "currentUd"}'
```

### Notes
1. Remplacez `<votre_dunikey>` par votre clé de trousseau.
2. Assurez-vous que l'API Jaklis est en cours d'exécution et accessible à `http://localhost:8000`.
3. La réponse de chaque commande sera renvoyée sous forme de JSON.

Utilisez ces commandes pour tester les différentes fonctionnalités de l'API Jaklis et vérifier si elles fonctionnent correctement.
