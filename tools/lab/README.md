# Assistant Email Intelligent avec Ollama

Ce projet implémente un assistant email intelligent utilisant Ollama pour générer des réponses automatiques aux emails entrants. Le système apprend continuellement des interactions passées pour améliorer ses réponses futures.

## Fonctionnalités Principales

1. **Lecture des Emails** : Connexion à un serveur IMAP pour lire les emails non lus.
2. **Génération de Réponses** : Utilisation d'Ollama pour créer des réponses pertinentes basées sur le contenu de l'email et le contexte historique.
3. **Brouillons Intelligents** : Sauvegarde des réponses générées comme brouillons pour révision humaine.
4. **Apprentissage Continu** : Extraction et utilisation des paires email/réponse pour améliorer les futures générations.
5. **Gestion de Contexte** : Maintien d'un contexte global pour des réponses plus cohérentes.

## Configuration

1. Copiez `.env.template` en `.env` et remplissez les variables :

```
IMAP_SERVER=mail.example.com
SMTP_SERVER=mail.example.com
SMTP_PORT=587
EMAIL=votre@email.com
PASSWORD=votremotdepasse
SENTDIR=INBOX.Sent
DRAFTDIR=INBOX.Draft
OLLAMA_API_URL=http://localhost:11434
MODEL=llama2
CONTEXT=./context.txt
```

2. Assurez-vous qu'Ollama est installé et en cours d'exécution sur votre système.

## Installation

```bash
pip install -r requirements.txt
```

## Utilisation

Exécutez le script principal :

```bash
python email3llama.py
```

Le script effectuera les actions suivantes en boucle :
1. Lire les nouveaux emails
2. Générer des réponses avec Ollama
3. Sauvegarder les réponses comme brouillons
4. Mettre à jour le dataset d'apprentissage avec les emails envoyés
5. Régénérer les embeddings du dataset

## Fonctionnement

- Les emails non lus sont traités automatiquement.
- Les réponses générées sont sauvegardées comme brouillons dans le dossier spécifié (DRAFTDIR).
- L'utilisateur peut modifier et envoyer les brouillons manuellement.
- Les emails envoyés sont utilisés pour mettre à jour le dataset d'apprentissage.
- Le système utilise ce dataset mis à jour pour améliorer ses futures réponses.

## Contribution

Les contributions sont les bienvenues. Veuillez ouvrir une issue ou un pull request pour toute suggestion ou amélioration.

## Licence

AGPL
