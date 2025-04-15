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

1.  **Install Astroport.ONE (it will install UPassport):**
    ```bash
    bash <(wget -qO- https://raw.githubusercontent.com/papiche/Astroport.ONE/master/install.sh)
    ```

2.  For developpers, **Clone (Fork) the UPassport Repository:**
    ```bash
    git clone https://github.com/papiche/UPassport.git
    cd <uplanet_directory>
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
