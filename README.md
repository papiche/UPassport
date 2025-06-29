# UPassport: A Decentralized Identity and Multimedia Terminal for the Ğ1 Ecosystem

## 🌟 Overview

UPassport is a Free and Open Source Software (FOSS) web application designed as a versatile terminal for interacting with the Ğ1 (Duniter) cryptocurrency and the decentralized web. It acts as a bridge between user interactions, multimedia handling, and secure identity management within the Ğ1 ecosystem, leveraging the `Astroport.ONE` infrastructure.

UPassport aims to solve the Human/Machine Trust dilemma by providing a FOSS solution for establishing a fully trusted decentralized information system based on NOSTR and IPFS. This system is built to respect space-time equivalent distance for everyone on the internet.

## ✨ Key Features

UPassport offers a range of functionalities centered around decentralized identity, Ğ1 interactions, and multimedia processing:

*   **UPassport Identity Management**:
    *   **QR Code Scanning**: Securely scans and processes QR codes for public keys, ZenCard details, SSSS keys, and other relevant data.
    *   **UPassport Generation**: Facilitates the creation of UPassports, leveraging backend scripts to generate user profiles and associated cryptographic keys.
    *   **SSSS Key Security**: Implements Shamir's Secret Sharing Scheme (SSSS) for enhanced security and key recovery of UPassports.
    *   **ZeroCard Integration**: Manages ZeroCards as part of the UPassport system, enabling secure interactions and identity verification.

*   **NOSTR Card Functionality**:
    *   **NOSTR Card Creation**: Allows users to create NOSTR Cards linked to their email addresses, generating NOSTR key pairs and associated G1 wallets.
    *   **NOSTR Profile Management**: Sets up and updates NOSTR profiles, including metadata and links to IPFS-hosted content.
    *   **NOSTR Vault Storage**: Utilizes IPFS for decentralized storage of NOSTR Card data and related assets.
    *   **NOSTR Event Publishing**: Enables publishing of NOSTR events, including location data and uploaded images, to NOSTR relays.

*   **Ğ1 (Duniter) Ecosystem Interaction**:
    *   **Balance Checking**: Verifies Ğ1 account balances using public keys via the `COINScheck.sh` script.
    *   **ZEN Payments (PalPay)**: Facilitates ZEN (Ẑen) cryptocurrency transactions using ZenCards, with a dedicated "PalPay" terminal.
    *   **Transaction Processing**: Executes DUNITER payments securely using the `PAY4SURE.sh` script.
    *   **Wallet History**: Retrieves and displays transaction history for Ğ1 public keys.

*   **Multimedia Processing & Recording**:
    *   **OBS Studio Integration**: Starts and stops recordings in OBS Studio remotely via a web interface, enabling multimedia content creation within the UPassport ecosystem.
    *   **Webcam Recording**: Captures and processes video and audio blobs directly from user webcams.
    *   **File Upload Processing**: Handles uploads of local video and audio files for processing within the Astroport pipeline.
    *   **YouTube Link Downloading**: Downloads and processes videos from YouTube links.
    *   **IPFS Integration for Media**: Integrates with IPFS for decentralized storage and distribution of recorded and processed multimedia content.

*   **Backend Scripting & Asynchronous Operations**:
    *   **Shell Script Execution**: Relies on Bash scripts (`upassport.sh`, `zen_send.sh`, `check_ssss.sh`, `startrec.sh`, `stoprec.sh`, `command.sh`, `upload2ipfs.sh`) for core functionalities, ensuring robust and secure operations.
    *   **Asynchronous Task Handling**: Uses `asyncio` in the Python backend to manage concurrent requests and script executions, maintaining a responsive user experience.
    *   **Comprehensive Logging**: Implements detailed logging for all backend operations and script executions, aiding in debugging and monitoring.

*   **Data Handling & Validation**:
    *   **Pydantic Validation**: Employs Pydantic models for data validation, ensuring data integrity for incoming requests.
    *   **Temporary File Storage**: Utilizes a `tmp/` directory for temporary file storage during processing, with automated cleanup routines.
    *   **IPFS for Decentralized Storage**: Leverages IPFS for decentralized and persistent storage of user data, NOSTR Cards, and multimedia assets.

## 🚀 Why UPassport?

UPassport is designed to empower users within the Ğ1 ecosystem by providing:

1.  **Seamless Ğ1 Integration**: Direct and intuitive interaction with the Ğ1 cryptocurrency and its ecosystem.
2.  **Comprehensive Multimedia Support**: A full suite of tools for video and audio recording, processing, and decentralized distribution.
3.  **Decentralized and Secure Identity**: Leverages NOSTR and IPFS to build a robust, user-controlled, and censorship-resistant identity system.
4.  **Free and Open Source**: Ensures transparency, community-driven development, and auditable security.
5.  **Extensible Architecture**: Built with modularity in mind, leveraging external scripts for custom logic and easy expansion of functionalities.

## 📋 Prerequisites

Before setting up UPassport, ensure you have the following prerequisites installed and configured:

*   **Software Dependencies**:
    *   **Python**: Version 3.7 or higher.
    *   **Node.js and npm**: For frontend asset management (if needed, though not heavily used in the provided code).
    *   **FFmpeg**: For multimedia processing tasks.
    *   **ImageMagick**: For image manipulation, particularly QR code generation and image processing.
    *   **IPFS (InterPlanetary File System)**:  Ensure IPFS is installed and running as a daemon for decentralized storage.
    *   **GPG (GNU Privacy Guard)**: For cryptographic operations, particularly decryption and key management.
    *   **NaCl (libsodium)**:  For cryptographic primitives used in DUNITER and NOSTR.
    *   **OBS Studio (Optional)**: If you plan to use the recording functionalities.
    *   **`amzqr`**: Command-line QR code generator (likely needs to be installed separately, e.g., via `pip install amzqr`).
    *   **`ssss-split`, `ssss-combine` (Shamir's Secret Sharing Scheme tools)**:  Likely provided by the `ssss` package (install if needed, e.g., via system package manager).
    *   **`natools.py`**:  Encryption and decryption tools (likely part of the Astroport.ONE toolset or a separate utility - ensure it's available in your `$PATH` or adjust script paths).
    *   **`jaklis.py`**:  DUNITER command-line interface (likely part of the Astroport.ONE toolset - ensure it's available or adjust script paths).

*   **Astroport.ONE Installation**:
    *   UPassport heavily relies on the `Astroport.ONE` infrastructure. Install it by running:
        ```bash
        bash <(wget -qO- https://raw.githubusercontent.com/papiche/Astroport.ONE/master/install.sh)
        ```
    *   **Captain User**: Create a captain user within your Astroport.ONE setup by running `~/.zen/Astroport.ONE/command.sh`. This is crucial for administrative functions and key management.
    *   **Dragons Web of Trust (Optional)**: Consider joining the Dragons Web of Trust and linking your SSH key to your Astroport.ONE IPFS for enhanced security and network participation.

## 🛠️ Installation Guide

1.  **Install Astroport.ONE (automatic install ~/.zen/UPassport):**
    ```bash
    bash <(wget -qO- https://raw.githubusercontent.com/papiche/Astroport.ONE/master/install.sh)
    ```

2.  For developpers, **Clone (Fork) the UPassport Repository:**
    ```bash
    git clone https://github.com/papiche/UPassport.git
    cd UPassport
    ```

3.  **Set up a Python Virtual Environment (Recommended):**
    ```bash
    python3 -m venv .astro
    source .astro/bin/activate
    ```

4.  **Install Python Dependencies:**
    ```bash
    pip install fastapi uvicorn aiofiles pydantic python-multipart python-dotenv cryptography base58 aiohttp Jinja2
    ```
    *(Note: This command includes dependencies explicitly identified from the code. You might need to install additional packages depending on the complete Astroport.ONE toolchain and any further functionalities you intend to use.)*

## 🖥️ Getting Started

1.  **Launch the UPassport Application:**
    ```bash
    python 54321.py
    ```

2.  **Access UPassport in your Web Browser**: Open your web browser and navigate to `http://localhost:54321`.

3.  **Explore UPassport Functionalities**: The UPassport web interface provides access to various terminals and tools:

    *   **Main Terminal (`/scan` or `/`)**:  For general QR code scanning, UPassport actions, and NOSTR Card interactions.
    *   **ZenCard Terminal (`/scan_zen.html` - accessed internally)**: For initiating ZEN (Ẑen) payments using ZenCards.
    *   **Security Scanner (`/scan_ssss.html` - accessed internally)**: For UPassport security verification, used by station CAPTAINs.
    *   **NOSTR Card Interface (`/nostr`)**: For exploring NOSTR functionalities and potentially managing NOSTR Cards (functionality may be limited in the provided code).
    *   **Recording Interface (`/rec`)**: For starting and stopping OBS Studio recordings, uploading video files, or processing YouTube links.
    *   **Webcam Recording (`/webcam`)**: For capturing and processing video directly from your webcam.
    *   **File Upload to IPFS (`/upload`)**: For uploading files to IPFS and obtaining IPFS links.
    *   **UPlanet Account Creation (`/uplanet` or `/uplanet.html`)**: For creating UPlanet accounts (functionality may be limited in the provided code).
    *   **API Description (`/index` or `/uplanet`)**: Provides a basic API description and welcome page.

## 🌐 UPassport API (Port 54321)

The UPassport API provides secure, decentralized file and identity management for your uDRIVE, leveraging NOSTR authentication (NIP42) and IPFS. All endpoints are available at `http://localhost:54321/api/`.

### Endpoints Overview

| Endpoint                  | Method | Description                                 | Auth Required | Example Payload/Params         |
|---------------------------|--------|---------------------------------------------|---------------|-------------------------------|
| `/api/upload`             | POST   | Upload a file to your uDRIVE (IPFS)         | Yes (npub)    | `file`, `npub` (form-data)    |
| `/api/upload_from_drive`  | POST   | Sync a file from IPFS to your uDRIVE        | Yes (npub)    | `ipfs_link`, `npub` (JSON)    |
| `/api/delete`             | POST   | Delete a file from your uDRIVE              | Yes (npub)    | `file_path`, `npub` (JSON)    |
| `/api/test-nostr`         | POST   | Test NOSTR authentication for a pubkey      | No            | `npub` (form-data)            |

---

### Endpoint Details

#### `POST /api/upload`
- **Description:** Upload a file to your personal uDRIVE (IPFS-backed). The file is categorized (Images, Music, Videos, Documents) based on type.
- **Authentication:** NOSTR (NIP42) required. Provide your `npub` (NOSTR public key).
- **Payload:** `multipart/form-data` with fields:
  - `file`: The file to upload.
  - `npub`: Your NOSTR public key (npub1... or 64-char hex).
- **Returns:** JSON with upload status, file path, file type, target directory, new IPFS CID, and authentication status.

**Example (curl):**
```bash
curl -F "file=@myphoto.jpg" -F "npub=npub1..." http://localhost:54321/api/upload
```

---

#### `POST /api/upload_from_drive`
- **Description:** Download a file from IPFS and add it to your uDRIVE.
- **Authentication:** NOSTR (NIP42) required.
- **Payload:** JSON body:
  - `ipfs_link`: The IPFS path (e.g., QmHASH/filename.ext).
  - `npub`: Your NOSTR public key.
- **Returns:** JSON with sync status, file path, file type, new IPFS CID, and authentication status.

**Example:**
```bash
curl -X POST http://localhost:54321/api/upload_from_drive \
  -H "Content-Type: application/json" \
  -d '{"ipfs_link":"QmHASH/filename.jpg","npub":"npub1..."}'
```

---

#### `POST /api/delete`
- **Description:** Delete a file from your uDRIVE (requires NOSTR authentication).
- **Authentication:** NOSTR (NIP42) required.
- **Payload:** JSON body:
  - `file_path`: Relative path to the file in your uDRIVE (e.g., `Images/myphoto.jpg`).
  - `npub`: Your NOSTR public key.
- **Returns:** JSON with deletion status, deleted file, new IPFS CID, and authentication status.

**Example:**
```bash
curl -X POST http://localhost:54321/api/delete \
  -H "Content-Type: application/json" \
  -d '{"file_path":"Images/myphoto.jpg","npub":"npub1..."}'
```

---

#### `POST /api/test-nostr`
- **Description:** Test NOSTR authentication for a given public key. Returns status and diagnostic info.
- **Authentication:** Not required.
- **Payload:** Form-data:
  - `npub`: NOSTR public key (npub1... or 64-char hex).
- **Returns:** JSON with authentication status, relay info, and recommendations.

**Example:**
```bash
curl -F "npub=npub1..." http://localhost:54321/api/test-nostr
```

---

### Authentication Notes
- All user-specific actions require a recent NOSTR NIP42 authentication event on the local relay (`ws://127.0.0.1:7777`).
- If authentication fails, ensure your NOSTR client has published a kind 22242 event within the last 24 hours.

---

### See Also
- [Astroport.ONE API & UPlanet Swarm – Developer Guide](../Astroport.ONE/API.NOSTRAuth.readme.md)
- [NOSTR Protocol](https://github.com/nostr-protocol/nostr)
- [NIP42 - Authentication](https://github.com/nostr-protocol/nips/blob/master/42.md)

## 🛠️ Configuration

*   **`.env` File**: Configure environment-specific settings in the `.env` file located in the root directory. This file is crucial for setting:
    *   API endpoints for DUNITER and Cesium+.
    *   IPFS node addresses.
    *   Mailjet API keys (if email notifications are enabled).
    *   Passwords and security-sensitive information (handle with care and consider security best practices).
    *   `OBSkey`:  The WebSocket key for OBS Studio integration.

**Important Notes:**

*   **Security**:  Handle `.env` file and cryptographic keys with utmost care. Ensure proper security measures are in place, especially in production environments.
*   **Astroport.ONE Setup**:  A functional Astroport.ONE installation is essential for UPassport to operate correctly.
*   **Script Paths**:  Verify and adjust script paths within the Python and Bash scripts to match your Astroport.ONE installation and tool locations.
*   **Customization**: UPassport is designed to be extensible. You can customize and extend its functionalities by modifying the Python code, Bash scripts, and HTML templates.

By setting up UPassport, you gain access to a powerful terminal for interacting with the decentralized Ğ1 ecosystem, managing your digital identity, and leveraging multimedia capabilities within a secure and open-source framework.


---

# UPassport : Terminal Multimédia et d'Identité pour l'Écosystème Ğ1

## 🌟 Aperçu

UPassport est une application web Libre et Open Source (FOSS) conçue comme un terminal polyvalent pour interagir avec la cryptomonnaie Ğ1 (Duniter) et le web décentralisé. Il sert de pont entre les interactions utilisateur, la gestion multimédia et la gestion d'identité sécurisée au sein de l'écosystème Ğ1, en tirant parti de l'infrastructure `Astroport.ONE`.

UPassport vise à résoudre le dilemme de la confiance Homme/Machine et à fournir une solution FOSS pour établir un système d'information décentralisé entièrement fiable basé sur NOSTR et IPFS. Ce système est construit pour respecter la distance équivalente espace-temps pour chacun sur Internet.

## ✨ Fonctionnalités Clés

UPassport offre une gamme de fonctionnalités centrées sur l'identité décentralisée, les interactions avec la Ğ1 et le traitement multimédia :

*   **Gestion d'Identité UPassport** :
    *   **Scan de QR Code** : Scanne et traite de manière sécurisée les QR codes pour les clés publiques, les détails ZenCard, les clés SSSS et d'autres données pertinentes.
    *   **Génération d'UPassport** : Facilite la création d'UPassports, en utilisant des scripts backend pour générer des profils utilisateurs et les clés cryptographiques associées.
    *   **Sécurité des Clés SSSS** : Met en œuvre le schéma de partage de secret de Shamir (SSSS) pour une sécurité renforcée et la récupération des clés UPassport.
    *   **Intégration ZeroCard** : Gère les ZeroCards dans le cadre du système UPassport, permettant des interactions sécurisées et la vérification d'identité.

*   **Fonctionnalité Carte NOSTR** :
    *   **Création de Carte NOSTR** : Permet aux utilisateurs de créer des Cartes NOSTR liées à leurs adresses e-mail, en générant des paires de clés NOSTR et des portefeuilles G1 associés.
    *   **Gestion de Profil NOSTR** : Configure et met à jour les profils NOSTR, y compris les métadonnées et les liens vers le contenu hébergé sur IPFS.
    *   **Stockage de Coffre-fort NOSTR** : Utilise IPFS pour le stockage décentralisé des données des Cartes NOSTR et des actifs associés.
    *   **Publication d'Événements NOSTR** : Permet la publication d'événements NOSTR, y compris les données de localisation et les images téléchargées, vers les relais NOSTR.

*   **Interaction avec l'Écosystème Ğ1 (Duniter)** :
    *   **Vérification de Solde** : Vérifie les soldes des comptes Ğ1 en utilisant les clés publiques via le script `COINScheck.sh`.
    *   **Paiements ZEN (PalPay)** : Facilite les transactions en cryptomonnaie ZEN (Ẑen) en utilisant les ZenCards, avec un terminal "PalPay" dédié.
    *   **Traitement des Transactions** : Exécute les paiements DUNITER de manière sécurisée en utilisant le script `PAY4SURE.sh`.
    *   **Historique du Portefeuille** : Récupère et affiche l'historique des transactions pour les clés publiques Ğ1.

*   **Traitement et Enregistrement Multimédia** :
    *   **Intégration OBS Studio** : Démarre et arrête les enregistrements dans OBS Studio à distance via une interface web, permettant la création de contenu multimédia au sein de l'écosystème UPassport.
    *   **Enregistrement Webcam** : Capture et traite les blobs vidéo et audio directement depuis les webcams des utilisateurs.
    *   **Traitement du Téléversement de Fichiers** : Gère les téléversements de fichiers vidéo et audio locaux pour le traitement dans le pipeline Astroport.
    *   **Téléchargement de Liens YouTube** : Télécharge et traite les vidéos à partir de liens YouTube.
    *   **Intégration IPFS pour le Multimédia** : Intègre IPFS pour le stockage et la distribution décentralisés des contenus multimédias enregistrés et traités.

*   **Scripts Backend et Opérations Asynchrones** :
    *   **Exécution de Scripts Shell** : Repose sur des scripts Bash (`upassport.sh`, `zen_send.sh`, `check_ssss.sh`, `startrec.sh`, `stoprec.sh`, `command.sh`, `upload2ipfs.sh`) pour les fonctionnalités de base, assurant des opérations robustes et sécurisées.
    *   **Gestion des Tâches Asynchrones** : Utilise `asyncio` dans le backend Python pour gérer les requêtes concurrentes et les exécutions de scripts, maintenant une expérience utilisateur réactive.
    *   **Journalisation Complète** : Met en œuvre une journalisation détaillée pour toutes les opérations backend et les exécutions de scripts, facilitant le débogage et la surveillance.

*   **Gestion et Validation des Données** :
    *   **Validation Pydantic** : Emploie des modèles Pydantic pour la validation des données, assurant l'intégrité des données pour les requêtes entrantes.
    *   **Stockage Temporaire de Fichiers** : Utilise un répertoire `tmp/` pour le stockage temporaire des fichiers pendant le traitement, avec des routines de nettoyage automatisées.
    *   **IPFS pour le Stockage Décentralisé** : Tire parti d'IPFS pour le stockage décentralisé et persistant des données utilisateur, des Cartes NOSTR et des actifs multimédias.

## 🚀 Pourquoi UPassport ?

UPassport est conçu pour donner du pouvoir aux utilisateurs au sein de l'écosystème Ğ1 en fournissant :

1.  **Intégration Transparente avec la Ğ1** : Interaction directe et intuitive avec la cryptomonnaie Ğ1 et son écosystème.
2.  **Support Multimédia Complet** : Une suite complète d'outils pour l'enregistrement vidéo et audio, le traitement et la distribution décentralisée.
3.  **Identité Décentralisée et Sécurisée** : Tire parti de NOSTR et d'IPFS pour construire un système d'identité robuste, contrôlé par l'utilisateur et résistant à la censure.
4.  **Libre et Open Source** : Assure la transparence, le développement piloté par la communauté et une sécurité auditable.
5.  **Architecture Extensible** : Construit avec la modularité à l'esprit, en utilisant des scripts externes pour une logique personnalisée et une extension facile des fonctionnalités.

## 📋 Prérequis

Avant de configurer UPassport, assurez-vous d'avoir installé et configuré les prérequis suivants :

*   **Dépendances Logiciel** :
    *   **Python** : Version 3.7 ou supérieure.
    *   **Node.js et npm** : Pour la gestion des actifs frontend (si nécessaire, bien que peu utilisé dans le code fourni).
    *   **FFmpeg** : Pour les tâches de traitement multimédia.
    *   **ImageMagick** : Pour la manipulation d'images, en particulier la génération de QR codes et le traitement d'images.
    *   **IPFS (InterPlanetary File System)** : Assurez-vous qu'IPFS est installé et fonctionne en tant que démon pour le stockage décentralisé.
    *   **GPG (GNU Privacy Guard)** : Pour les opérations cryptographiques, en particulier le déchiffrement et la gestion des clés.
    *   **NaCl (libsodium)** : Pour les primitives cryptographiques utilisées dans DUNITER et NOSTR.
    *   **OBS Studio (Optionnel)** : Si vous prévoyez d'utiliser les fonctionnalités d'enregistrement.
    *   **`amzqr`** : Générateur de QR codes en ligne de commande (doit probablement être installé séparément, par exemple via `pip install amzqr`).
    *   **`ssss-split`, `ssss-combine` (outils Shamir's Secret Sharing Scheme)** : Probablement fournis par le paquet `ssss` (installez si nécessaire, par exemple via le gestionnaire de paquets système).
    *   **`natools.py`** : Outils de chiffrement et de déchiffrement (probablement partie de l'ensemble d'outils Astroport.ONE ou un utilitaire séparé - assurez-vous qu'il est disponible dans votre `$PATH` ou ajustez les chemins des scripts).
    *   **`jaklis.py`** : Interface en ligne de commande DUNITER (probablement partie de l'ensemble d'outils Astroport.ONE - assurez-vous qu'il est disponible ou ajustez les chemins des scripts).

*   **Installation d'Astroport.ONE** :
    *   UPassport repose fortement sur l'infrastructure `Astroport.ONE`. Installez-la en exécutant :
        ```bash
        bash <(wget -qO- https://raw.githubusercontent.com/papiche/Astroport.ONE/master/install.sh)
        ```
    *   **Utilisateur Capitaine** : Créez un utilisateur capitaine au sein de votre configuration Astroport.ONE en exécutant `~/.zen/Astroport.ONE/command.sh`. Ceci est crucial pour les fonctions administratives et la gestion des clés.
    *   **Web de Confiance Dragons (Optionnel)** : Envisagez de rejoindre le Web de Confiance Dragons et de lier votre clé SSH à votre IPFS Astroport.ONE pour une sécurité et une participation au réseau améliorées.

## 🛠️ Guide d'Installation

1.  **Installez Astroport.ONE (installe ~/.zen/UPassport automatiquement)** :
    ```bash
    bash <(wget -qO- https://raw.githubusercontent.com/papiche/Astroport.ONE/master/install.sh)
    ```

2.  Pour les développeurs, **Clonez (forkez) le Répertoire UPassport** :
    ```bash
    git clone https://github.com/papiche/UPassport.git
    cd UPassport
    ```

3.  **Configurez un Environnement Virtuel Python (Recommandé)** :
    ```bash
    python3 -m venv .astro
    source .astro/bin/activate
    ```

4.  **Installez les Dépendances Python** :
    ```bash
    pip install fastapi uvicorn aiofiles pydantic python-multipart python-dotenv cryptography base58 aiohttp Jinja2
    ```
    *(Note : Cette commande inclut les dépendances explicitement identifiées dans le code. Vous pourriez avoir besoin d'installer des paquets supplémentaires en fonction de l'ensemble d'outils Astroport.ONE complet et de toute autre fonctionnalité que vous comptez utiliser.)*

## 🖥️ Démarrage

1.  **Lancez l'Application UPassport** :
    ```bash
    python 54321.py
    ```

2.  **Accédez à UPassport dans votre Navigateur Web** : Ouvrez votre navigateur web et naviguez vers `http://localhost:54321`.

3.  **Explorez les Fonctionnalités d'UPassport** : L'interface web d'UPassport donne accès à divers terminaux et outils :

    *   **Terminal Principal (`/scan` ou `/`)** : Pour le scan de QR codes général, les actions UPassport et les interactions avec les Cartes NOSTR.
    *   **Terminal ZenCard (`/scan_zen.html` - accessible en interne)** : Pour initier des paiements ZEN (Ẑen) en utilisant les ZenCards.
    *   **Scanner de Sécurité (`/scan_ssss.html` - accessible en interne)** : Pour la vérification de sécurité UPassport, utilisé par les CAPITAINES de station.
    *   **Interface Carte NOSTR (`/nostr`)** : Pour explorer les fonctionnalités NOSTR et potentiellement gérer les Cartes NOSTR (la fonctionnalité peut être limitée dans le code fourni).
    *   **Interface d'Enregistrement (`/rec`)** : Pour démarrer et arrêter les enregistrements OBS Studio, téléverser des fichiers vidéo ou traiter des liens YouTube.
    *   **Enregistrement Webcam (`/webcam`)** : Pour capturer et traiter la vidéo directement depuis votre webcam.
    *   **Téléversement de Fichiers vers IPFS (`/upload`)** : Pour téléverser des fichiers vers IPFS et obtenir des liens IPFS.
    *   **Création de Compte UPlanet (`/uplanet` ou `/uplanet.html`)** : Pour créer des comptes UPlanet (la fonctionnalité peut être limitée dans le code fourni).
    *   **Description de l'API (`/index` ou `/uplanet`)** : Fournit une description de l'API de base et une page de bienvenue.

## 🛠️ Configuration

*   **Fichier `.env`** : Configurez les paramètres spécifiques à l'environnement dans le fichier `.env` situé dans le répertoire racine. Ce fichier est crucial pour définir :
    *   Les points de terminaison API pour DUNITER et Cesium+.
    *   Les adresses des nœuds IPFS.
    *   Les clés API Mailjet (si les notifications par e-mail sont activées).
    *   Les mots de passe et les informations sensibles (traitez-les avec soin et tenez compte des meilleures pratiques de sécurité).
    *   `OBSkey` : La clé WebSocket pour l'intégration d'OBS Studio.

**Notes Importantes** :

*   **Sécurité** : Manipulez le fichier `.env` et les clés cryptographiques avec le plus grand soin. Assurez-vous que des mesures de sécurité appropriées sont en place, en particulier dans les environnements de production.
*   **Configuration Astroport.ONE** : Une installation Astroport.ONE fonctionnelle est essentielle pour que UPassport fonctionne correctement.
*   **Chemins des Scripts** : Vérifiez et ajustez les chemins des scripts dans les scripts Python et Bash pour qu'ils correspondent à votre installation Astroport.ONE et à l'emplacement des outils.
*   **Personnalisation** : UPassport est conçu pour être extensible. Vous pouvez personnaliser et étendre ses fonctionnalités en modifiant le code Python, les scripts Bash et les modèles HTML.

En configurant UPassport, vous accédez à un terminal puissant pour interagir avec l'écosystème Ğ1 décentralisé, gérer votre identité numérique et tirer parti des capacités multimédias dans un cadre sécurisé et open source.

### Voir Aussi
- [Guide Développeur API Astroport.ONE & UPlanet Swarm](../Astroport.ONE/API.NOSTRAuth.readme.md)
- [Protocole NOSTR](https://github.com/nostr-protocol/nostr)
- [NIP42 - Authentification](https://github.com/nostr-protocol/nips/blob/master/42.md)

---

## 🌐 API UPassport (Port 54321)

L'API UPassport fournit une gestion sécurisée et décentralisée des fichiers et de l'identité pour votre uDRIVE, en tirant parti de l'authentification NOSTR (NIP42) et d'IPFS. Tous les endpoints sont disponibles sur `http://localhost:54321/api/`.

### Aperçu des Endpoints

| Endpoint                  | Méthode | Description                                 | Auth Requise | Exemple Payload/Params         |
|---------------------------|---------|---------------------------------------------|--------------|-------------------------------|
| `/api/upload`             | POST    | Téléverser un fichier vers votre uDRIVE (IPFS) | Oui (npub) | `file`, `npub` (form-data)    |
| `/api/upload_from_drive`  | POST    | Synchroniser un fichier depuis IPFS vers votre uDRIVE | Oui (npub) | `ipfs_link`, `npub` (JSON)    |
| `/api/delete`             | POST    | Supprimer un fichier de votre uDRIVE        | Oui (npub) | `file_path`, `npub` (JSON)    |
| `/api/test-nostr`         | POST    | Tester l'authentification NOSTR pour une clé publique | Non | `npub` (form-data)            |

---

### Détails des Endpoints

#### `POST /api/upload`
- **Description :** Téléverser un fichier vers votre uDRIVE personnel (basé sur IPFS). Le fichier est catégorisé (Images, Musique, Vidéos, Documents) selon son type.
- **Authentification :** NOSTR (NIP42) requise. Fournissez votre `npub` (clé publique NOSTR).
- **Payload :** `multipart/form-data` avec les champs :
  - `file` : Le fichier à téléverser.
  - `npub` : Votre clé publique NOSTR (npub1... ou hex 64 caractères).
- **Retourne :** JSON avec le statut de téléversement, le chemin du fichier, le type de fichier, le répertoire cible, le nouveau CID IPFS et le statut d'authentification.

**Exemple (curl) :**
```bash
curl -F "file=@maphoto.jpg" -F "npub=npub1..." http://localhost:54321/api/upload
```

---

#### `POST /api/upload_from_drive`
- **Description :** Télécharger un fichier depuis IPFS et l'ajouter à votre uDRIVE.
- **Authentification :** NOSTR (NIP42) requise.
- **Payload :** Corps JSON :
  - `ipfs_link` : Le chemin IPFS (ex : QmHASH/fichier.ext).
  - `npub` : Votre clé publique NOSTR.
- **Retourne :** JSON avec le statut de synchronisation, le chemin du fichier, le type de fichier, le nouveau CID IPFS et le statut d'authentification.

**Exemple :**
```bash
curl -X POST http://localhost:54321/api/upload_from_drive \
  -H "Content-Type: application/json" \
  -d '{"ipfs_link":"QmHASH/fichier.jpg","npub":"npub1..."}'
```

---

#### `POST /api/delete`
- **Description :** Supprimer un fichier de votre uDRIVE (nécessite une authentification NOSTR).
- **Authentification :** NOSTR (NIP42) requise.
- **Payload :** Corps JSON :
  - `file_path` : Chemin relatif vers le fichier dans votre uDRIVE (ex : `Images/maphoto.jpg`).
  - `npub` : Votre clé publique NOSTR.
- **Retourne :** JSON avec le statut de suppression, le fichier supprimé, le nouveau CID IPFS et le statut d'authentification.

**Exemple :**
```bash
curl -X POST http://localhost:54321/api/delete \
  -H "Content-Type: application/json" \
  -d '{"file_path":"Images/maphoto.jpg","npub":"npub1..."}'
```

---

#### `POST /api/test-nostr`
- **Description :** Tester l'authentification NOSTR pour une clé publique donnée. Retourne le statut et les informations de diagnostic.
- **Authentification :** Non requise.
- **Payload :** Form-data :
  - `npub` : Clé publique NOSTR (npub1... ou hex 64 caractères).
- **Retourne :** JSON avec le statut d'authentification, les informations du relai et les recommandations.

**Exemple :**
```bash
curl -F "npub=npub1..." http://localhost:54321/api/test-nostr
```

---

### Notes d'Authentification
- Toutes les actions spécifiques à l'utilisateur nécessitent un événement d'authentification NOSTR NIP42 récent sur le relai local (`ws://127.0.0.1:7777`).
- Si l'authentification échoue, assurez-vous que votre client NOSTR a publié un événement de type 22242 dans les dernières 24 heures.

---

### Voir Aussi
- [Guide Développeur API Astroport.ONE & UPlanet Swarm](../Astroport.ONE/API.NOSTRAuth.readme.md)
- [Protocole NOSTR](https://github.com/nostr-protocol/nostr)
- [NIP42 - Authentification](https://github.com/nostr-protocol/nips/blob/master/42.md)
