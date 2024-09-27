# Multi-Format Document Vectorizer

Ce script Python crée une base de données vectorielle à partir de divers types de documents, y compris des fichiers texte, PDF, images, HTML, audio et vidéo. Il utilise des techniques avancées de traitement du langage naturel et de reconnaissance vocale pour extraire le contenu de ces documents et les convertir en embeddings vectoriels.

## Fonctionnalités

- Traitement de multiples formats de fichiers : PDF, images (JPG, PNG, GIF), HTML, texte brut, audio (MP3, WAV, FLAC) et vidéo (MP4, AVI, MOV)
- Extraction de texte à partir de PDF et d'images à l'aide d'OCR
- Analyse d'images avec le modèle Moondream via l'API Ollama
- Transcription audio et vidéo utilisant Vosk et FFmpeg
- Génération d'embeddings vectoriels à l'aide de modèles de transformers
- Stockage efficace des embeddings avec FAISS

Pour utiliser ce script, assurez-vous d'avoir installé toutes les dépendances nécessaires et d'avoir configuré correctement l'environnement (notamment FFmpeg, Vosk, et Ollama pour Moondream).

## Prérequis

- Python 3.7+
- FFmpeg installé sur le système
- Un modèle Vosk pour la reconnaissance vocale (à télécharger séparément)
- GPU recommandé pour de meilleures performances (mais non obligatoire)

## Installation

1. Clonez ce dépôt...

2. Téléchargez un modèle Vosk approprié pour votre langue depuis le [site officiel de Vosk](https://alphacephei.com/vosk/models).

## Utilisation

Exécutez le script en spécifiant le répertoire contenant vos documents, le répertoire de sortie pour la base de données vectorielle, et le chemin vers le modèle Vosk :

```
python veXtorize.py /chemin/vers/documents /chemin/vers/sortie --vosk_model /chemin/vers/modele_vosk --lang fr-fr
```

Options :
- `directory` : Répertoire contenant les documents à traiter
- `output_dir` : Répertoire où seront stockés l'index FAISS et les métadonnées
- `--vosk_model` : Chemin vers le modèle Vosk pour la reconnaissance vocale
- `--lang` : Code de langue pour la reconnaissance vocale (par défaut : en-us)
- `--log` : Niveau de logging (DEBUG, INFO, WARNING, ERROR, CRITICAL)

## Structure de sortie

Le script génère deux fichiers dans le répertoire de sortie :
1. `vector_index.faiss` : L'index FAISS contenant les embeddings vectoriels
2. `metadata.json` : Un fichier JSON contenant les métadonnées pour chaque document traité

## Dépendances principales

- transformers : Pour les modèles de langage et la génération d'embeddings
- faiss-cpu : Pour le stockage et la recherche efficace des vecteurs
- vosk : Pour la reconnaissance vocale
- PyMuPDF : Pour l'extraction de texte à partir de PDF
- pytesseract : Pour l'OCR sur les images
- Pillow : Pour le traitement d'images
- BeautifulSoup : Pour l'analyse HTML
- requests : Pour les appels API à Ollama

## Remarques

- Assurez-vous que l'API Ollama est en cours d'exécution localement pour l'analyse d'images avec Moondream.
- Le traitement de grands volumes de données peut prendre du temps. Utilisez un GPU si possible pour de meilleures performances.
- Ajustez les paramètres de logging selon vos besoins de débogage.


## Contribution

Les contributions à ce projet sont les bienvenues. Veuillez ouvrir une issue pour discuter des changements majeurs avant de soumettre un pull request.
