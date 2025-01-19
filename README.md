# Astroport Studio: A Multimedia and Identity Platform for the Ğ1 Ecosystem

Astroport Studio is a versatile web application designed to interact with the Ğ1 (Junium) cryptocurrency ecosystem. It serves as a bridge between user interactions, multimedia handling, user identity generation, and the underlying Ğ1 network using the `Astroport.ONE` infrastructure.

## 🌟 Core Features

*   **User Interaction**:
    *   Interactive surveys to tailor user experience.
    *   Dynamic presentations based on survey responses.
    *   Intuitive interfaces for various actions.
*   **Identity Management (UPassport)**:
    *   Secure processing of digital passport information.
    *   Processing public keys via QR code scans or direct entry.
    *   Integration with a backend processing script to generate user profiles using `upassport.sh`.
*   **Ğ1 Interaction**:
    *   Check user account balances using public keys via the `COINScheck.sh` script.
    *   Send messages with location and identity information via the `command.sh` script.
    *   Handle secure data sharing.
    *   Processing ZEN cryptocurrency transactions with `zen_send.sh`
*   **Media Processing**:
    *   Start/Stop OBS recording (via web interface), with integration to the underlying recording mechanisms.
    *   Upload local video files to be processed by Astroport pipeline.
    *   Download videos from YouTube links, processing them via Astroport pipeline.
    *   Process audio and video blobs received from webcams using the `startrec.sh` script.
    *   Integration to the underlying recording mechanisms (`startrec.sh` and `stoprec.sh`) including post-processing to handle IPFS integration with TiddlyWiki
*   **Backend Scripting**:
    *   Execution of shell scripts for core operations (IPFS handling, Ğ1 transactions, file processing).
    *   Logging and error handling for all background operations.
*   **Data Handling**:
    *   Temporary storage for files (`tmp/`) and log files.
    *   Use of Pydantic to validate incoming data.
*   **Asynchronous Processing**:
    *   Utilizes `asyncio` to concurrently handle requests and backend processes.
    *   Ensures responsive behavior for end users.

## 🚀 Why Astroport Studio?

1.  **Integration**: Seamless interaction with the Ğ1 currency.
2.  **Multimedia Support**: Handles video recording, uploads, and YouTube link downloads, and webcam recording.
3.  **Decentralization**: Built with distributed storage in mind (IPFS integration).
4.  **User-Centric**: Provides intuitive interfaces for various tasks.
5.  **Extensibility**: Leverages external shell scripts for custom logic.

## 📋 Prerequisites

*   Python 3.7+
*   Node.js and npm
*   FFmpeg
*   ImageMagick
*   IPFS
*   GPG
*   NaCl
*   Astroport.ONE installation (as described below)

## 🛠️ Installation

1.  **Clone the repository:**

2.  **Install Astroport.ONE:**
    ```bash
    bash <(wget -qO- https://raw.githubusercontent.com/papiche/Astroport.ONE/master/install.sh)
    ```
    * Create a captain user by running the command  `~/.zen/Astroport.ONE/command.sh`
    * OPTIONAL : Join the Dragons Web of Trust, link your SSH key to your Astroport.ONE IPFS

3.  **Create and activate a Python virtual environment (optional but highly recommended):**
    ```bash
    python -m venv .astro
    . ~/.astro/bin/activate
    ```

4.  **Install Python dependencies:**
    ```bash
    pip install fastapi uvicorn vosk sounddevice pydantic[email] python-multipart
    ```

## 🖥️ Usage

1.  **Launch the application:**
    ```bash
     python ./54321.py
    ```

2.  Access the application via your browser at `http://localhost:54321`.

3.  Explore the various functionalities including:
    *   Interactive questionnaire at `/enter`.
    *   Video recording and media processing at `/rec`.
    *   QR Code scanning and UPassport features at `/scan`.
    *   Balance checks with G1 public keys at `/check_balance?g1pub=<pubkey>`.

## 🛠️ Configuration

*   Adjust environment settings in the `.env` file (e.g., node URLs).
*   Modify HTML templates in the `templates/` directory for UI customization.
*   Ensure proper permissions for executing shell scripts.

---

Détaillons la gestion fonctionnelle des ZEROCARDS et des NOSTRCARDS dans le script `upassport.sh`.

**ZEROCARD : Identité secondaire et passeport décentralisé**

1.  **Objectif principal :** Une ZEROCARD est une identité secondaire liée à une identité Duniter principale. Elle sert de "passeport numérique" dans un écosystème décentralisé. Elle est conçue pour :
    *   **Représenter une identité sur un espace IPFS:** La Zerocard permet d'avoir un `DRIVESTATE`, c'est à dire une page HTML dynamique stockée sur IPFS, mise à jour via une clé IPNS, liée à un utilisateur principal (un "membre" Duniter).
    *   **Activer un écosystème décentralisé :** Chaque ZEROCARD peut interagir avec d'autres, déclencher des commandes ou contrôler des applications sur IPFS.

2.  **Processus de création et d'activation :**
    *   **Génération de clés :** Une paire de clés Duniter est générée, et une clé IPFS est générée, cette dernière est cryptée et liée à l'identité principale.
    *   **Enregistrement :** Les données de la ZEROCARD (clé publique, etc.) sont stockées dans des fichiers PDF/HTML et mises à jour sur IPFS.
    *   **Initialisation :** L'activation d'une ZEROCARD se fait par un transfert sortant (TX) de la part de l'identité principale. Après une transaction de la part de l'identité principale vers la clé publique de la Zerocard, le script va :
        *   Extraire les données de la ZEROCARD.
        *   Déployer une première DApp sur IPFS.
        *   Ré-organiser les informations dans l'arborescence IPFS.
        *   Chiffrer les données sensibles de la ZEROCARD.
    *   **Mise à jour dynamique:** Les actions (TX et commentaires) de l'utilisateur principal sont interprétées par le script, pour modifier le contenu du driveState. Ce mécanisme permet de mettre en place des commandes vers un espace IPFS.
    *   **Clé IPNS dédiée :** La ZEROCARD est liée à une clé IPNS qui permet de mettre à jour le `DRIVESTATE` associé. Ce `DRIVESTATE` est utilisé comme porte d'entrée vers un contenu IPFS dynamique (par exemple un portfolio, un blog, un e-commerce etc.)
    *   **Protection:** L'accès au contenu de la ZEROCARD, et la gestion du `DRIVESTATE`, est protégée par l'identité principale et un mot de passe (UPLANETNAME), ainsi que par une partie du secret SSSS lié à la création de l'espace ZEROCARD.

3.  **Fonctionnement :**
    *   **Commandes :** L'identité principale peut envoyer des transactions avec des commentaires spécifiques vers la ZEROCARD. Le script interprète ces commentaires comme des commandes (dans le script : `command.sh`), déclenchant des actions sur la ZEROCARD.
    *   **Redirection Drivestate :** Le contenu du `DRIVESTATE` de la ZEROCARD peut rediriger l'utilisateur vers d'autres applications web sur IPFS.
    *   **Stockage distribué :** Toutes les données sont stockées sur IPFS, ce qui garantit leur disponibilité et leur pérennité.
    *   **Sécurité :** Les données sensibles de la ZEROCARD sont chiffrées et protégées. La clé IPNS est elle même chiffrée.
    *   **Gestion du cycle de vie:** Le mécanisme permet de gérer l'activation et la désactivation du contenu.

**NOSTRCARD : Identité Nostr liée à une identité G1**

1.  **Objectif principal :** Une NOSTRCARD permet de lier une identité Nostr à une identité Duniter (G1). Elle est utilisée pour :
    *   **Créer une identité Nostr :** Elle génère des clés Nostr publiques et privées.
    *   **Associer Nostr à Duniter :** Elle lie l'identité Nostr à un membre Duniter et à un e-mail
    *   **Stockage décentralisé :** L'identité Nostr et les données associées sont stockées sur IPFS, et accessibles via un `NOSTRVAULT` en IPNS.
    *   **Utilisation dans l'écosystème décentralisé :** Elle utilise l'adresse mail comme point de liaison vers l'utilisateur principal.
    *   **Récupérer une identité :** L'identité Nostr est récupérable grâce à un ssss-split, partagé entre 3 acteurs (via QR code ou autre).

2.  **Processus de création :**
    *   **Analyse du QR code (email):** L'analyse du QRcode reçu comme une adresse email déclenche la création d'une identité NOSTRCARD.
    *   **Génération de clés :** Une paire de clés Nostr est générée.
    *   **Génération de clés :** Une paire de clés Duniter est générée.
    *   **Distribution des clés :** Les données d'identification (les clés) sont ensuite protégées via une distribution Shamir secret sharing et distribuées (via QR Codes) à 3 acteurs.
    *   **Stockage :** Les clés Nostr sont stockées en local.
    *   **Stockage IPNS :** L'ensemble des données (clé nostr, clé g1, les ssss parts, etc.) sont ensuite regroupées et envoyées sur IPFS et liées à un espace `NOSTRVAULT` en IPNS.
    *   **Publication du vault :** Une clé IPNS est générée pour mettre à jour l'espace `NOSTRVAULT` et est liée à la clé publique duniter.
    *   **Protection :** L'accès aux données sensibles de la NOSTRCARD est protégé par les clés et par un password (UPLANETNAME).

3.  **Fonctionnement :**
    *   **Ecosystème Nostr :** Elle permet à l'utilisateur d'utiliser Nostr avec son identité G1.
    *   **Récupération de l'identité:** Elle permet de retrouver l'accès à son identité Nostr, via les 3 clés ssss.
    *   **Stockage de données :** Toutes les données sont stockées sur IPFS, et disponibles via IPNS.
    *   **Sécurité :** Les données sensibles de la NOSTRCARD sont chiffrées et protégées.

**Comparaison fonctionnelle**

| Caractéristique        | ZEROCARD                                            | NOSTRCARD                                           |
| --------------------- | --------------------------------------------------- | ---------------------------------------------------- |
| **Objectif**          | Identité secondaire, "passeport" décentralisé.     | Identité Nostr liée à une identité G1.              |
| **Déclencheur**        | Envoi d'un QR code de type clé publique G1       | Envoi d'un QR code de type adresse email           |
| **Utilisation**       | Contrôle d'applications et identité sur IPFS      | Accès au réseau Nostr et gestion de l'identité.      |
| **Cycle de vie**    |  Déploiement dynamique via tx de l'identité principale |  Création par ssss-split des données, puis déploiement IPFS  |
| **Structure**        | Structure hiérarchique, mise à jour via transactions  | Structure autonome, avec récupération du vault via SSSS |
| **Interaction**       | Interaction avec l'identité principale via commentaires | Interaction avec le réseau Nostr et récupération.     |
| **Sécurité**           | Clés chiffrées, stockage IPFS, accès à un mot de passe (UPLANETNAME) | Clés Nostr et G1 stockées et chiffrées, accès via SSSS, et via un mot de passe. |
| **Distribution du secret**  | Clé IPNS chiffrée via clé Captain et UPLANETNAME | Clé Nostr et ssss parties distribuées via 3 acteurs. |
| **Gestion**        | Le controle est du coté du G1 | Le contrôle est du coté de l'utilisateur |

**Points clés**

*   **Hiérarchie vs Autonomie :** La ZEROCARD est hiérarchique et contrôlée par l'identité principale, tandis que la NOSTRCARD est autonome, gérée et récupérable par l'utilisateur principal via un système ssss-split.
*   **Focus :** La ZEROCARD est axée sur le contrôle décentralisé des applications et la gestion de l'identité sur IPFS (Drivestate), tandis que la NOSTRCARD se concentre sur l'accès au réseau Nostr et la protection des données.
*   **Complexité :** Les ZEROCARDS ont un cycle de vie plus complexe et sont plus liées à des mécanismes IPFS/blockchain (notamment le `DRIVESTATE`)  alors que les NOSTRCARDS sont plus "simples" dans leurs fonctionnements (un enregistrement IPFS + une gestion de clé).

**En résumé**

Les ZEROCARDS et les NOSTRCARDS sont deux types d'identités différentes dans cet écosystème :

*   La **ZEROCARD** est un outil pour activer un espace IPFS lié à un utilisateur principal via une interface dynamique.
*   La **NOSTRCARD** permet de créer et de récupérer une identité Nostr en la liant à un utilisateur et une identité G1.

Elles utilisent toutes deux IPFS pour le stockage et une forme de protection des données, mais avec des objectifs et des approches distincts.

---

## 🤝 How to Contribute

Contributions are highly appreciated! Here’s how you can help:

1.  🐛 Report issues and bugs.
2.  💡 Suggest new features and improvements.
3.  🔧 Submit pull requests for fixes and enhancements.
4.  📚 Improve the documentation.
5.  🧪 Help with testing.

## 🤝 Comment Contribuer

Votre contribution est la bienvenue ! Voici quelques pistes pour nous aider :

1.  **Signalement des Problèmes :** Signalez les bugs et les problèmes rencontrés.
2.  **Suggestions d'Améliorations :** Proposez de nouvelles fonctionnalités et des améliorations.
3.  **Soumission de Pull Requests :** Soumettez des pull requests pour corriger des bugs et améliorer le code.
4.  **Amélioration de la Documentation :** Aidez à améliorer la documentation du projet.
5.  **Test :** Aidez-nous à tester les nouvelles fonctionnalités.

## 📄 License

This project is licensed under the AGPL-3.0 License. See the [LICENSE](LICENSE) file for more details.

## 👤 Author

Fred (DsEx1pS33vzYZg4MroyBV9hCw98j1gtHEhwiZ5tK7ech)

## 🙏 Acknowledgements

*   Ğ1 and Duniter community
*   Contributors to the utilized libraries.
*   Vosk and openai-whisper team for speech recognition tools.

## 📚 Documentation

We invite you to be part of this project shaping the future of digital identity, and multimedia management within the Ğ1 ecosystem!
