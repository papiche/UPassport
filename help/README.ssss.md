# README.ssss.md

 A relire et corriger


## Description

Ce document décrit le fonctionnement de l'API et du script associés à la vérification et à la validation de clés SSSS (Shamir's Secret Sharing Scheme). L'API est implémentée en utilisant FastAPI et le script est un script Bash qui effectue diverses opérations de vérification et de combinaison de clés.

### API Endpoint

L'API expose un point d'entrée `/ssss` qui permet de soumettre des données via une requête POST. Les données soumises incluent :

- `cardns`: Un identifiant unique pour une carte (Adresse IPNS).
- `ssss`: La partie de clé SSSS à valider.
- `zerocard`: Un identifiant optionnel pour la ZEROCARD.

### Fonctionnement de l'API

1. **Récupération des données du formulaire** : L'API extrait les valeurs des champs `cardns`, `ssss`, et `zerocard` à partir des données du formulaire soumis.

2. **Exécution du script** : L'API appelle un script Bash (`check_ssss.sh`) en passant les valeurs récupérées comme arguments.

3. **Capture des logs** : Le script écrit ses sorties dans un fichier log pour le suivi.

4. **Validation et réponse** : Si le script s'exécute avec succès, l'API retourne le fichier généré par le script. En cas d'erreur, elle renvoie un message d'erreur avec un chemin vers les logs.

### Fonctionnement du Script Bash

Le script `check_ssss.sh` réalise plusieurs tâches :

1. **Validation des arguments** : Vérifie que trois arguments sont fournis, sinon affiche un message d'utilisation et quitte.

2. **Validation de la clé SSSS** : Vérifie que la clé SSSS respecte un format spécifique (doit commencer par "3-" suivi d'une chaîne hexadécimale).

3. **Récupération de CARDNS** : Utilise IPFS pour résoudre l'identifiant CARDNS et détermine s'il s'agit d'un fichier ou d'un répertoire.

4. **Combinaison avec CAPTAIN Part** : Tente de combiner la clé SSSS avec une partie stockée localement pour valider la clé complète.

5. **Génération du résultat HTML** : Crée un fichier HTML affichant le résultat de la validation.

## Commandes de Test avec cURL

Pour tester l'API, vous pouvez utiliser les commandes cURL suivantes :

### Exemple de requête réussie

```bash
curl -X POST http://localhost:54321/ssss \
     -F "cardns=exampleCardNS" \
     -F "ssss=3-c9ac213472a72bfd1ea1a7780f18914..." \
     -F "zerocard=exampleZeroCard"
```

### Exemple de requête avec erreur (manque d'arguments)

```bash
curl -X POST http://localhost:54321/ssss \
     -F "cardns=exampleCardNS" \
     -F "ssss=3-c9ac213472a72bfd1ea1a7780f18914..."
```

### Exemple de requête avec clé invalide

```bash
curl -X POST http://localhost:54321/ssss \
     -F "cardns=exampleCardNS" \
     -F "ssss=invalidKeyFormat" \
     -F "zerocard=exampleZeroCard"
```

Ces commandes permettent de simuler différentes situations pour vérifier le bon fonctionnement de l'API et du script associé. Assurez-vous que le serveur FastAPI est en cours d'exécution sur `localhost:54321` avant d'exécuter ces commandes.
