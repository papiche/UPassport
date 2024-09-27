# UPassport

UPassport est un générateur innovant de passeports numériques pour la monnaie libre Ğ1. Il crée des identités visuelles uniques basées sur les clés publiques des utilisateurs, tout en intégrant des fonctionnalités avancées de reconnaissance vocale et de stockage décentralisé.

## 🌟 Fonctionnalités Principales

- 🔐 Génération de passeports numériques personnalisés
- 📱 Interface web responsive (mobile et desktop)
- 🔗 Intégration avec le réseau Duniter et Cesium+
- 🗣️ Reconnaissance vocale pour la transcription audio
- 📊 Création de QR codes pour les relations entre membres
- 🌐 Stockage décentralisé via IPFS
- ++ Apprentissage IA avec [veXtorize](./veXtorize.md) Multi-Format Document Vectorizer

## 🚀 Pourquoi UPassport ?

1. **Innovation Monétaire** : Soutient l'écosystème de la monnaie libre Ğ1.
2. **Identité Numérique Sécurisée** : Crée des identités uniques et vérifiables.
3. **Décentralisation** : Utilise IPFS pour un stockage distribué et résilient.
4. **Accessibilité** : Interface conviviale et support multiplateforme.
5. **Open Source** : Favorise la collaboration et l'amélioration continue.

## 📋 Prérequis

- Python 3.7+
- Node.js et npm
- FFmpeg
- ImageMagick
- IPFS
- GPG
- NaCl

## 🛠️ Installation

Si c'est votre machine personnelle (Linux), créez un nouveau compte utilisateur pour y installer "UPassport" et "Astroport.ONE"

1. Clonez le dépôt UPassport :
   ```
   git clone https://github.com/papiche/UPassport.git
   cd UPassport
   ```

2. Installez **Astroport.ONE** :
   ```
   bash <(wget -qO- https://raw.githubusercontent.com/papiche/Astroport.ONE/master/install.sh)
   ```

   * Créez un compte capitaine ```~/.zen/Astroport.ONE/command.sh```

   OPTION : rejoindre la toile de confiance "Dragons", suivre la procédure http://pad.p2p.legal/s/keygen pour relier votre clef PGP (ou SSH) à la balise IPFS de votre Station AstroportONE


3. Installez les dépendances Python (il peut en manquer, signaler celles qui manquent) :

Vous devriez vous trouver dans l'environnement python ".astro", sinon créer et activer le
    ```
    python -m venv .astro
    . ~/.astro/bin/activate
    ```

   ```
   pip3 install fastapi uvicorn vosk sounddevice pydantic[email]
   ```


## 🖥️ Utilisation

1. Lancez l'application :
   ```
   python ./54321.py

   ```

2. Accédez à `http://localhost:54321` dans votre navigateur.

3. Utilisez l'interface pour :
   - Scanner un QR code
   - Entrer une clé publique Ğ1
   - Essayez la transcription vocale (http://localhost:54321/voice)

## 🛠️ Configuration

- Modifiez les URLs des nœuds dans le fichier `.env`
- Personnalisez les templates HTML dans `templates/`

## 🤝 Comment Contribuer

Nous accueillons chaleureusement toutes les contributions ! Voici comment vous pouvez participer :

1. 🐛 Signalez des bugs ou suggérez des améliorations via les issues GitHub.
2. 💡 Proposez de nouvelles fonctionnalités ou des améliorations de code.
3. 🔧 Soumettez des pull requests pour corriger des bugs ou ajouter des fonctionnalités.
4. 📚 Améliorez la documentation ou ajoutez des traductions.
5. 🧪 Participez aux tests et à l'assurance qualité.

Consultez notre [guide de contribution](CONTRIBUTING.md) pour plus de détails.

## 📄 Licence

Ce projet est sous licence AGPL-3.0. Voir le fichier [LICENSE](LICENSE) pour plus de détails.

## 👤 Auteur

Fred (DsEx1pS33vzYZg4MroyBV9hCw98j1gtHEhwiZ5tK7ech)

## 🙏 Remerciements

- Communauté Ğ1 et Duniter
- Contributeurs des bibliothèques utilisées
- Équipe Vosk pour la reconnaissance vocale

## 📚 Documentation

Pour une documentation complète, visitez notre [wiki](https://github.com/papiche/UPassport/wiki).

Rejoignez-nous dans cette aventure passionnante pour façonner l'avenir de l'identité numérique et de la monnaie libre !
