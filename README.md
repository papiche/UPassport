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

## 🤝 How to Contribute

Contributions are highly appreciated! Here’s how you can help:

1.  🐛 Report issues and bugs.
2.  💡 Suggest new features and improvements.
3.  🔧 Submit pull requests for fixes and enhancements.
4.  📚 Improve the documentation.
5.  🧪 Help with testing.

## 📄 License

This project is licensed under the AGPL-3.0 License. See the [LICENSE](LICENSE) file for more details.

## 👤 Author

Fred (DsEx1pS33vzYZg4MroyBV9hCw98j1gtHEhwiZ5tK7ech)

## 🙏 Acknowledgements

*   Ğ1 and Duniter community
*   Contributors to the utilized libraries.
*   Vosk team for speech recognition tools.

## 📚 Documentation

We invite you to be part of this project shaping the future of digital identity, and multimedia management within the Ğ1 ecosystem!
