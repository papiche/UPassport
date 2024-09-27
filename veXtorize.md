Voici un README détaillé pour le projet :

# Créateur de Base de Données Vectorielle pour Documents

## Description

Ce projet est un outil Python conçu pour créer une base de données vectorielle à partir de divers types de documents (PDF, images, HTML, markdown, texte). Il utilise des techniques avancées de traitement du langage naturel et de vision par ordinateur pour extraire et analyser le contenu des documents, puis crée des embeddings vectoriels pour une recherche et une récupération efficaces.

## Fonctionnalités

- Traitement de plusieurs types de fichiers : PDF, images (JPG, PNG), HTML, markdown, et texte brut
- Analyse d'images utilisant le modèle Moondream via Ollama
- OCR (Reconnaissance Optique de Caractères) pour les images contenant du texte
- Génération d'embeddings vectoriels pour chaque document
- Stockage efficace des embeddings dans une base de données FAISS
- Sauvegarde des métadonnées associées à chaque document

## Prérequis

- Python 3.7+
- CUDA-compatible GPU (recommandé pour de meilleures performances)

## Installation

1. Clonez ce dépôt :
   ```
   git clone https://github.com/votre-nom/createur-base-vectorielle.git
   cd createur-base-vectorielle
   ```

2. Installez les dépendances :
   ```
   pip install -r requirements.txt
   ```

3. Assurez-vous que Ollama est installé et en cours d'exécution sur votre machine avec le modèle Moondream disponible.

## Utilisation

Pour créer une base de données vectorielle à partir d'un répertoire de documents :

```
python main.py /chemin/vers/documents /chemin/vers/sortie
```

Où :
- `/chemin/vers/documents` est le chemin du répertoire contenant vos documents
- `/chemin/vers/sortie` est le répertoire où la base de données vectorielle et les métadonnées seront sauvegardées

## Structure du Projet

- `main.py` : Script principal pour exécuter le processus de création de la base de données
- `requirements.txt` : Liste des dépendances Python
- `README.md` : Ce fichier

## Fonctionnement

1. Le script parcourt récursivement le répertoire spécifié.
2. Pour chaque fichier :
   - Le type de fichier est détecté automatiquement.
   - Le contenu est extrait en fonction du type de fichier.
   - Pour les images, une analyse avec Moondream est effectuée avant de décider d'appliquer l'OCR.
3. Les embeddings sont générés pour chaque document.
4. Les embeddings sont stockés dans un index FAISS.
5. Les métadonnées sont sauvegardées dans un fichier JSON.

## Sortie

Le script génère deux fichiers dans le répertoire de sortie :
- `vector_index.faiss` : L'index FAISS contenant les embeddings vectoriels
- `metadata.json` : Un fichier JSON contenant les métadonnées associées à chaque document

## Dépendances Principales

- PyMuPDF (fitz) : Pour le traitement des PDF
- Pillow : Pour le traitement d'images
- pytesseract : Pour l'OCR
- BeautifulSoup : Pour le parsing HTML
- Transformers : Pour les modèles de langage et d'image
- SentenceTransformers : Pour la génération d'embeddings
- FAISS : Pour le stockage et la recherche d'embeddings vectoriels

## Contribution

Les contributions à ce projet sont les bienvenues. N'hésitez pas à ouvrir une issue ou à soumettre une pull request.
