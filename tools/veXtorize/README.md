# 🚀 veXtorize.py : Le Couteau Suisse de vos Documents !

Vous en avez marre de chercher une aiguille dans une botte de foin numérique ? Dites bonjour à veXtorize.py, votre nouveau meilleur ami pour transformer votre chaos documentaire en une base de données vectorielle digne de la NASA ! 🧠💾

## 🌟 Que fait ce petit génie ?

veXtorize.py est comme un mixeur surpuissant pour vos documents. Il avale tout : textes, PDFs, images, HTMLs, audios, vidéos... et les transforme en de jolis vecteurs que même votre IA préférée pourrait comprendre !

### 🎭 Ses super-pouvoirs :

- 📚 Lit TOUS vos documents (même ceux que vous avez oubliés)
- 🔍 Fait de l'OCR comme un pro (adieu les textes illisibles !)
- 🖼️ Analyse les images avec Moondream (comme un critique d'art, mais en plus rapide)
- 🎤 Transcrit l'audio et la vidéo (plus besoin d'écouter ces longues réunions)
- 🧮 Génère des embeddings vectoriels (des maths cool, promis !)
- 🗄️ Range tout ça dans FAISS (comme Marie Kondo, mais pour les données)

## 🛠️ Comment apprivoiser cette bête ?

1. Clonez ce repo (comme si vous adoptiez un animal de compagnie numérique)
2. Installez les dépendances (c'est comme faire les courses pour votre nouveau compagnon)
3. Téléchargez un modèle Vosk (choisissez la langue, comme pour Duolingo, mais en utile)
4. Lancez le script et regardez la magie opérer !

## 🎮 Mode d'emploi pour les aventuriers

Lancez `python veXtorize.py` et suivez le menu interactif. C'est comme un jeu vidéo, mais à la fin, vous aurez une base de données vectorielle au lieu de points d'expérience !

1. **Créer une nouvelle base** : Pour les Marie Kondo des données
2. **Mettre à jour une base** : Quand votre base a besoin d'un rafraîchissement
3. **Recherche sémantique** : Comme Google, mais pour vos propres documents
4. **Visualiser les embeddings** : Pour les amateurs d'art abstrait numérique
5. **Fine-tuner le modèle** : Pour les perfectionnistes qui veulent que leur IA parle leur langage

## 🧙‍♂️ Trucs et astuces de pro

- Utilisez un GPU si possible (c'est comme mettre un turbo sur votre mixeur à documents)
- Ajustez les logs pour déboguer (ou pour avoir l'impression d'être un hacker)
- Préparez du café, ça peut prendre du temps pour de gros volumes de données !

## 🤝 Envie de contribuer ?

Vous avez des idées pour rendre veXtorize.py encore plus cool ? Ouvrez une issue ou proposez un pull request ! On est comme une grande famille ici, mais une famille qui aime les vecteurs et le machine learning.

## 📜 Mot de la fin

Avec veXtorize.py, transformez votre montagne de documents en une base de données vectorielle si propre que même Marie Kondo serait jalouse. Alors, prêt à donner un coup de boost à votre productivité tout en vous amusant ? Let's veXtorize ! 🚀🎉

## Mode d'emploi de veXtorize.py : Maîtrisez votre base de données vectorielle !

on continue avec le guide complet de veXtorize.py, un outil ultime pour transformer vos documents en une base de données vectorielle intelligente. Suivez ce guide pour exploiter toute la puissance de veXtorize.py !

Pour créer l'environnement Python et installer toutes les dépendances nécessaires pour veXtorize.py, suivez ces étapes détaillées :

1. Créer un environnement virtuel :
   ```
   python -m venv vextorize_env
   ```

2. Activer l'environnement virtuel :
   - Sur Windows : `vextorize_env\Scripts\activate`
   - Sur macOS et Linux : `source vextorize_env/bin/activate`

3. Installer les dépendances principales :
   ```
   pip install transformers torch faiss-cpu vosk PyMuPDF Pillow pytesseract beautifulsoup4 requests tqdm scikit-learn matplotlib datasets
   ```

4. Installer les dépendances système :
   - FFmpeg : nécessaire pour le traitement audio/vidéo
   - Tesseract : requis pour l'OCR

   Sur Ubuntu :
   ```
   sudo apt-get update
   sudo apt-get install ffmpeg tesseract-ocr
   ```
   Sur macOS avec Homebrew :
   ```
   brew install ffmpeg tesseract
   ```
   Sur Windows, téléchargez et installez FFmpeg et Tesseract manuellement.

5. Télécharger les modèles nécessaires :

   - Modèle Vosk pour la reconnaissance vocale
   - Modèle de langage pour les embeddings (par exemple, "sentence-transformers/all-MiniLM-L6-v2")

6. Configurer les chemins des modèles dans le script ou via les arguments en ligne de commande.

    Le petit programme pour faciliter ça : ```python model_setup.py```

7. Assurez-vous que l'API Ollama est installée et en cours d'exécution pour l'analyse d'images avec Moondream.

8. Vérifiez que toutes les dépendances sont correctement installées :
   ```
   pip list
   ```

9. Lancez le script pour tester l'installation :
   ```
   python veXtorize.py --models_dir ./models --vosk_model ./vosk_model
   ```

En suivant ces étapes, vous devriez avoir un environnement Python complet et fonctionnel pour exécuter veXtorize.py avec toutes ses dépendances.

## 🚀 Démarrage rapide

1. **Lancement de base** :
   ```bash
   python veXtorize.py /chemin/vers/documents /chemin/vers/sortie
   ```

## 🎛️ Options avancées

Personnalisez votre expérience avec ces options :

- `--models_dir` : Spécifiez le répertoire des modèles (par défaut : "./models")
- `--vosk_model` : Chemin vers le modèle Vosk (par défaut : "./vosk_model")
- `--log` : Niveau de log (DEBUG, INFO, WARNING, ERROR, CRITICAL)
- `--fine_tune` : Active le fine-tuning du modèle

Exemple :
```bash
python veXtorize.py /docs /sortie --models_dir ./mes_modeles --vosk_model ./vosk_fr --log DEBUG --fine_tune
```

## 🧭 Navigation dans le menu interactif

Lancez `python veXtorize.py` sans arguments pour accéder au menu interactif :

1. **Créer une nouvelle base de données** :
   - Entrez le chemin des documents source
   - Spécifiez le répertoire de sortie

2. **Mettre à jour une base existante** :
   - Indiquez le chemin de l'index FAISS existant
   - Fournissez le chemin du fichier de métadonnées
   - Listez les nouveaux fichiers à ajouter

3. **Effectuer une recherche sémantique** :
   - Choisissez l'index FAISS et le fichier de métadonnées
   - Entrez votre requête
   - Obtenez les résultats les plus pertinents

4. **Visualiser les embeddings** :
   - Sélectionnez l'index et les métadonnées
   - Admirez la représentation t-SNE de vos documents

5. **Fine-tuner le modèle** :
   - Choisissez l'index et les métadonnées
   - Laissez le modèle s'adapter à votre corpus

## 🛠️ Utilisation avancée

### Traitement de fichiers spécifiques

veXtorize.py gère divers types de fichiers :

- **Texte** (.txt, .md) : Traitement direct
- **PDF** : Extraction de texte et OCR si nécessaire
- **Images** (.jpg, .png, .gif) : Analyse avec Moondream + OCR
- **Audio/Vidéo** (.mp3, .wav, .mp4, etc.) : Transcription avec Vosk
- **HTML** : Extraction de texte et analyse des images intégrées

### Optimisation des performances

- Utilisez un GPU pour accélérer le traitement
- Ajustez `chunk_size` dans `chunk_text()` pour les gros documents
- Augmentez le nombre de workers dans `Pool()` pour le traitement parallèle

### Personnalisation des embeddings

Modifiez `generate_embedding()` pour utiliser différents modèles ou techniques :

```python
def generate_embedding(text, tokenizer, model):
    # Votre logique personnalisée ici
    pass
```

### Intégration à d'autres systèmes

Utilisez les fonctions individuelles dans vos propres scripts :

```python
from veXtorize import generate_embedding, semantic_search

# Votre code utilisant ces fonctions
```

## 📊 Analyse des résultats

- **Métadonnées** : Explorez `metadata.json` pour des informations détaillées sur chaque document
- **Visualisation** : Utilisez le graphique t-SNE pour identifier des clusters de documents similaires
- **Recherche** : Affinez vos requêtes pour obtenir des résultats plus précis

## 🔧 Dépannage

- **Erreurs d'OCR** : Assurez-vous que Tesseract est correctement installé
- **Problèmes audio** : Vérifiez l'installation de FFmpeg et du modèle Vosk
- **Erreurs CUDA** : Assurez-vous que vos pilotes GPU sont à jour

## 🌟 Astuces pro

- Utilisez `update_vector_database()` pour des mises à jour incrémentales efficaces
- Expérimentez avec différents modèles de langage pour des embeddings spécialisés
- Combinez la recherche sémantique avec des filtres classiques pour des résultats hybrides
Certainement ! Voici une nouvelle section à ajouter au guide concernant l'export GGUF :

## 🔄 Export GGUF : Donnez des ailes à votre modèle

Vous voulez utiliser votre modèle fine-tuné avec Ollama ? L'export GGUF est là pour vous ! Cette fonctionnalité vous permet de transformer votre modèle en un format compatible avec Ollama, ouvrant ainsi de nouvelles possibilités d'utilisation.

### 🚀 Comment ça marche ?

1. **Accédez à l'option d'export** : Dans le menu interactif, choisissez l'option "Export model to GGUF".

2. **Spécifiez le chemin de sortie** : Indiquez où vous souhaitez sauvegarder votre fichier GGUF.

3. **Laissez la magie opérer** : veXtorize.py s'occupe de tout le processus d'export.

### 🛠️ Utilisation avancée

```python
def export_to_gguf(model, tokenizer, output_path):
    logger.info(f"Exporting model to GGUF format: {output_path}")
    try:
        torchtune.export_gguf(model, tokenizer, output_path)
        logger.info(f"Model successfully exported to GGUF format: {output_path}")
    except Exception as e:
        logger.error(f"Error exporting model to GGUF: {str(e)}")
```

### 🌟 Avantages de l'export GGUF

- **Compatibilité Ollama** : Utilisez votre modèle personnalisé directement dans Ollama.
- **Portabilité** : Partagez facilement votre modèle avec d'autres utilisateurs d'Ollama.
- **Optimisation** : Le format GGUF est optimisé pour les inférences rapides.

### 💡 Astuces pour l'export GGUF

- Assurez-vous d'avoir suffisamment d'espace disque pour le fichier GGUF.
- Testez votre modèle exporté dans Ollama pour vérifier sa performance.
- Expérimentez avec différents paramètres de fine-tuning avant l'export pour obtenir les meilleurs résultats.

### 🔮 Et après ?

Une fois votre modèle exporté en GGUF, vous pouvez :
- L'intégrer dans vos applications utilisant Ollama
- Créer des chatbots personnalisés basés sur votre corpus de documents
- Effectuer des inférences rapides sur de nouveaux textes

L'export GGUF ouvre un monde de possibilités pour l'utilisation de vos modèles personnalisés. Profitez-en pour donner vie à vos projets d'IA les plus ambitieux !

## 🔮 Perspectives futures

- Intégration de nouveaux modèles d'IA
- Support de formats de fichiers additionnels
- Fonctionnalités d'analyse avancée et de clustering

Avec ce guide, vous êtes maintenant prêt à explorer toutes les facettes de veXtorize.py. Transformez votre montagne de documents en une base de connaissances vectorielle puissante et intuitive. Bonne veXtorisation ! 🚀📚
