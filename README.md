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
    *   **UMAP Geographic Discovery**: Provides geographic-based NOSTR message discovery using adjacent UMAPs (Universal Maps), enabling local community building and geographic social networks.

*   **Ğ1 (Duniter) Ecosystem Interaction**:
    *   **Balance Checking**: Verifies Ğ1 account balances using public keys via the `COINScheck.sh` script.
    *   **ZEN Payments (PalPay)**: Facilitates ZEN (Ẑen) cryptocurrency transactions using ZenCards, with a dedicated "PalPay" terminal.
    *   **Transaction Processing**: Executes DUNITER payments securely using the `PAYforSURE.sh` script.
    *   **Wallet History**: Retrieves and displays transaction history for Ğ1 public keys.

*   **Multimedia Processing & Recording**:
    *   **OBS Studio Integration**: Starts and stops recordings in OBS Studio remotely via a web interface, enabling multimedia content creation within the UPassport ecosystem.
    *   **Webcam Recording**: Captures and processes video and audio blobs directly from user webcams.
    *   **File Upload Processing**: Handles uploads of local video and audio files for processing within the Astroport pipeline.
    *   **YouTube Link Downloading**: Downloads and processes videos from YouTube links.
    *   **IPFS Integration for Media**: Integrates with IPFS for decentralized storage and distribution of recorded and processed multimedia content.

*   **Backend Scripting & Asynchronous Operations**:
    *   **Shell Script Execution**: Relies on Bash scripts (`upassport.sh`, `zen_send.sh`, `check_ssss.sh`, `startrec.sh`, `stoprec.sh`, `u.command.sh`, `upload2ipfs.sh`) for core functionalities, ensuring robust and secure operations.
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

    *   **UPlanet Status API (`/`)**:  Returns JSON data about the local UPlanet ecosystem (players, NOSTR multipass, UMAPs, swarm nodes). Supports geographic filtering with query parameters: `?lat=XX.XX&lon=YY.YY&deg=Z.Z` to filter results by geographic area.
    *   **QR Code Scanner (`/scan`)**: Web interface for general QR code scanning, UPassport actions, and NOSTR Card interactions.
    *   **MULTIPASS Payment Terminal (`/scan_multipass_payment.html`**: For initiating ẐEN payments between MULTIPASS wallets (stored in ~/.zen/game/nostr/).
    *   **Security Scanner (`/scan_ssss.html` - accessed internally)**: For UPassport security verification, used by station CAPTAINs.
    *   **NOSTR Card Interface (`/nostr`)**: For exploring NOSTR functionalities and potentially managing NOSTR Cards. Includes UMAP geographic discovery mode for finding messages from adjacent geographic zones.
    *   **NOSTR UPlanet Interface (`/nostr?type=uplanet`)**: Specialized interface for UPlanet SCIC Cooperative project proposals with UMAP geographic discovery capabilities.
    *   **Recording Interface (`/rec`)**: For starting and stopping OBS Studio recordings, uploading video files, or processing YouTube links.
    *   **Webcam Recording (`/webcam`)**: For capturing and processing video directly from your webcam.
    *   **File Upload to IPFS (`/upload`)**: For uploading files to IPFS and obtaining IPFS links.
    *   **UPlanet Account Creation (`/uplanet` or `/uplanet.html`)**: For creating UPlanet accounts (functionality may be limited in the provided code).
    *   **API Description (`/index` or `/uplanet`)**: Provides a basic API description and welcome page.

## 📱 QR Code Types & Processing Workflows

UPassport's `/scan` terminal (`upassport.sh`) processes multiple types of QR codes, each triggering a specific workflow. Here's a comprehensive guide to supported QR code formats and their handling:

### 1. 🔑 IPNS TiddlyWiki Key

**Format:** `k51qzi5uqu5d[ipns_hash]`

**Workflow:**
1. Detects IPNS key in TiddlyWiki format
2. **Direct redirect** to `${myIPFS}/ipns/${TWNS}`

**Use Case:** Quick access to decentralized TiddlyWiki notebooks stored on IPFS.

---

### 2. 🌐 HTTP/HTTPS Links

#### 2a. ZeroCard IPNS Link (with 12D3Koo)

**Format:** `http(s)://[domain]/ipns/12D3Koo[hash]`

**Workflow:**
1. Extracts IPNS 12D3Koo identifier (CARDNS)
2. Converts to G1 pubkey: `ipfs_to_g1.py $CARDNS`
3. Searches for owner member in `${MY_PATH}/pdf/`
4. If found: retrieves associated ZEROCARD
5. **Redirects to** `scan_ssss.html` for SSSS security QR scanning

**Use Case:** UPassport verification and security check by station CAPTAINs.

#### 2b. Generic HTTP Link

**Format:** Any `http://` or `https://` URL

**Workflow:**
1. **Direct redirect** to the URL

**Use Case:** Simple link sharing and web navigation.

---

### 3. 📧 EMAIL - NOSTR Card Creation/Management

**Format:** `email@domain.tld`

#### New MULTIPASS Creation:
1. Validates email format
2. Checks for existing MULTIPASS
3. Calls `make_NOSTRCARD.sh "${EMAIL}" "$PASS" "${LAT}" "${LON}"`
4. Generates NOSTR keypair and G1 wallet
5. Sends MULTIPASS via `mailjet.sh`
6. **Returns** `.nostr.zine.html` with printable MULTIPASS

#### PASS = "0000" (Account Deletion):
- Verifies account was created today (TODATE check)
- Destroys NOSTR TW: `nostr_DESTROY_TW.sh`
- Marks as "DELETED" in HTML
- **Returns** deletion confirmation

#### Existing Account:
- **Returns** "ALREADY EXISTING" message
- Shows again if created on same day (TODATE = today)

**Use Case:** Decentralized identity creation linked to email, with same-day deletion capability.

---

### 4. 🎫 MULTIPASS SSSS Authentication

**Format:** 
- `M-[base58_encoded]:k51qzi5uqu5d[ipns_hash]` (Base58)
- `1-[hex_encoded]:k51qzi5uqu5d[ipns_hash]` (Hex)

#### Decoding Process:
1. **M-**: Decodes Base58 using `Mbase58.py decode`
2. **1-**: Uses hex directly
3. Extracts `SSSS1` (share 1) and `IPNSVAULT` (IPNS key)

#### Authentication:
1. Searches for PLAYER via `get_NOSTRNS_directory()`
2. Decrypts `ssss.tail.uplanet.enc` with UPLANET key
3. Combines SSSS shares: `ssss-combine -t 2`
4. Recovers `salt` and `pepper`
5. Generates NSEC from recovered secrets

#### Actions by PASS Code:

**PASS = "" or "[lang]" (Quick Message - Default):**
- Generates NSEC with salt/pepper: `keygen -t nostr "${salt}" "${pepper}" -s`
- Creates `nostr.html` with pre-filled nsec
- **Returns** simple NOSTR message interface
- Default mode for quick geographic messages

**PASS = "1111" (Full Access):**
- Generates NSEC with salt/pepper
- Creates `astro_base.html` with nsec auto-filled
- Injects JavaScript for automatic authentication
- Auto-selects nsec authentication mode
- **Returns** complete Astro Base interface with full NOSTR features

**PASS = "0000" (Cash Back & Account Closure):**
- Generates dunikey with salt/pepper
- Retrieves G1PUBNOSTR balance
- Empties wallet to G1PRIME: `PAYforSURE.sh`
- Updates TODATE (one day to reactivate)
- **Returns** cash back confirmation message

**Custom PASS codes:**
- Reserved for future features and extensions

**Use Case:** Secure MULTIPASS authentication with Shamir Secret Sharing (3-of-2 threshold), enabling multiple interface modes based on PASS code.

---

### 5. 💰 G1 Public Key (Duniter)

**Format:** 43-44 character base58 string (G1 pubkey)

#### First Scan (No ZEROCARD):
1. Validates G1 format: `g1_to_ipfs.py ${PUBKEY}`
2. Retrieves TX history: `G1history.sh ${PUBKEY} 25`
3. Checks for MEMBER status: `/wot/lookup/$PUBKEY`
4. **If simple wallet:** displays balance only
5. **If member (has UID):**
   - Analyzes N1 network (P2P, 12P, P21 certifications)
   - Creates ZEROCARD with SSSS key splitting
   - Generates IPNS key for decentralized storage
   - Creates UPassport HTML with QR codes
   - **Returns** `_index.html` (fac-simile UPassport)

#### Second Scan (ZEROCARD Exists, Not Activated):
1. Checks last transaction
2. **If TX to ZEROCARD:** activates UPassport
   - Publishes to IPNS
   - Creates symlink to `~/.zen/game/passport/`
   - Updates NOSTR profile with G1PUB (if WoT authenticated)
   - Encrypts with UPLANETNAME
3. **Returns** activation confirmation page

#### Third+ Scans (ZEROCARD Activated):
1. **If COMM present and DEST == ZEROCARD:**
   - Executes command: `u.command.sh "$PUBKEY" "$COMM" "$LASTX" "$TXDATE" "$ZEROCARD"`
2. **If DRIVESTATE exists:**
   - **Redirects** to current IPFS DRIVESTATE
3. **Otherwise:** shows fac-simile

**Use Case:** Complete UPassport lifecycle from member discovery to activation and ongoing management.

---

## 📊 QR Code Processing Flow Summary

```
┌─────────────────────┬──────────────────────────────────────────┐
│ QR Code Type        │ Destination/Action                       │
├─────────────────────┼──────────────────────────────────────────┤
│ k51qzi5uqu5d...     │ → /ipns/[key] (IPFS redirect)           │
│ http://...12D3Koo   │ → scan_ssss.html (security verification)│
│ http://...          │ → Direct URL redirect                    │
│ email@domain.tld    │ → NOSTRCARD creation + email            │
│ M-...:k51qzi...     │ → nostr.html or astro_base.html         │
│   (PASS=""/lang)    │   → Quick Message interface             │
│   (PASS=1111)       │   → Full Astro Base interface           │
│   (PASS=9999)       │   → MULTIPASS Payment Terminal          │
│   (PASS=0000)       │   → Cash back + destroy                 │
│ ~~~~~...            │ → /check_zencard API (ZEN Card History) │
│   (with PASS)       │   → Redirect to API for history display │
│                     │   → 3 years blockchain analysis         │
│                     │   → Valid balance vs total received     │
│ [G1 PUBKEY]         │ → UPassport creation/management         │
│   (1st scan)        │   → Create passport (member)            │
│   (2nd scan)        │   → Activate passport (TX verification) │
│   (3rd+ scan)       │   → Command execution / DRIVESTATE      │
└─────────────────────┴──────────────────────────────────────────┘
```

**Note:** ZEN Cards (used by `UPLANET.official.sh`) are for social shares accounting via blockchain history, not for holding funds. MULTIPASS (NOSTR Cards) hold the actual ẐEN for payments.

### MULTIPASS PASS Codes Reference

| PASS Code | Interface      | Use Case                           |
|-----------|----------------|----------------------------------- |
| `""`      | nostr.html     | Quick geographic message (default) |
| `"fr"/"en"`| nostr.html    | Language-specific quick message    |
| `"1111"`  | astro_base.html| Full NOSTR messenger with all features |
| `"9999"`  | scan_multipass_payment.html | MULTIPASS Payment Terminal |
| `"0000"`  | Cash back      | Account closure + wallet emptying  |
| Custom    | Reserved       | Future features                    |

### ZEN Card Social Shares History

**ZEN Cards** are used by `UPLANET.official.sh` for tracking social shares (parts sociales) received from the cooperative's SOCIETY wallet. Unlike MULTIPASS which holds ẐEN for payments, ZEN Cards maintain the blockchain history of cooperative membership contributions.

**How it works:**
1. **Scan ZEN Card QR code** (encrypted format starting with `~~~~~`) on `/scan`
2. **Enter your personal PASS code** to decrypt the card
3. **Automatic redirect** to `/check_zencard?email=EMAIL&html` API endpoint
4. **View comprehensive history** with:
   - **Valid Balance**: Current valid social shares (Satellite: 1 year, Constellation: 3 years)
   - **Total Received**: All social shares received over 3 years
   - **Valid vs Expired**: Clear indication of expired Satellite shares

**Technical details:**
- ZEN Card QR codes are **GPG-encrypted** with a personal PASS code
- Format: `~~~~~` followed by encrypted salt/pepper parameters
- ZEN Cards are stored in `~/.zen/game/players/EMAIL/`
- Decryption in `upassport.sh` reveals G1 wallet keys for email lookup
- **Redirects to FastAPI route** `/check_zencard?email=EMAIL&html`
- API calls `G1zencard_history.sh` to retrieve blockchain history
- **Always analyzes 3 years** of SOCIETY wallet transactions
- **Automatic type detection** from blockchain comments (satellite/constellation)
- **Smart balance calculation**:
  - Satellite shares: Valid only for current year
  - Constellation shares: Valid for 3 years
  - Expired shares shown with visual indicators

**API Integration:**
- **Route**: `GET /check_zencard?email=EMAIL&html`
- **Script**: `G1zencard_history.sh` (backend processing)
- **Template**: `zencard_api.html` (Jinja2 server-side rendering)
- **JSON mode**: Add `&html` parameter for web display, omit for raw JSON

**Security:**
- Each ZEN Card has a **unique PASS code** known only to the member
- Wrong PASS code = decryption fails, no access to history
- QR code can be printed and carried physically (secure wallet card)
- API validates email and filters only SOCIETY transactions

**Use Case:** Cooperative members can verify their social shares history and contribution status at any time by scanning their encrypted ZEN Card QR code with their personal PASS code. The system automatically calculates valid balance based on share type (Satellite/Constellation) and displays a clear, responsive interface with expired shares marked distinctly.

### Security Features

- **SSSS (Shamir Secret Sharing)**: MULTIPASS keys split into 3 shares (HEAD/MIDDLE/TAIL), requiring 2-of-3 for reconstruction
- **Encrypted Storage**: ZenCard uses GPG AES encryption with password
- **UPLANET Key**: MIDDLE share encrypted with UPLANET name for geographic binding
- **CAPTAIN Key**: TAIL share encrypted with CAPTAIN pubkey for administrative recovery
- **MEMBER Key**: HEAD share encrypted with member's G1 pubkey for personal control
- **Time-based Deletion**: Accounts can only be deleted on creation day (TODATE check)

---

## 🌐 UPassport API (Port 54321)

The UPassport API provides secure, decentralized file and identity management for your uDRIVE, leveraging NOSTR authentication (NIP42) and IPFS. All endpoints are available at `http://localhost:54321/api/`.

### Endpoints Overview

| Endpoint                  | Method | Description                                 | Auth Required | Example Payload/Params         |
|---------------------------|--------|---------------------------------------------|---------------|-------------------------------|
| `/`                       | GET    | UPlanet ecosystem status (Ustats.sh)       | No            | `lat`, `lon`, `deg` (query params) |
| `/api/upload`             | POST   | Upload a file to your uDRIVE (IPFS)         | Yes (npub)    | `file`, `npub` (form-data)    |
| `/api/upload_from_drive`  | POST   | Sync a file from IPFS to your uDRIVE        | Yes (npub)    | `ipfs_link`, `npub` (JSON)    |
| `/api/delete`             | POST   | Delete a file from your uDRIVE              | Yes (npub)    | `file_path`, `npub` (JSON)    |
| `/api/getN2`              | GET    | Analyze N2 network (friends of friends)     | No            | `hex`, `range`, `output` (query params) |
| `/api/test-nostr`         | POST   | Test NOSTR authentication for a pubkey      | No            | `npub` (form-data)            |
| `/api/umap/geolinks`      | GET    | Get UMAP adjacent zones geolinks            | No            | `lat`, `lon` (query params)   |
| `/check_zencard`          | GET    | View ZEN Card social shares history         | No            | `email`, `html` (query params) |
| `/check_society`          | GET    | View SOCIETY wallet capital contributions   | No            | `html` (query param)          |
| `/check_revenue`          | GET    | View ZENCOIN transactions revenue (CA)       | No            | `html`, `year` (query params) |

---

### Endpoint Details

#### `GET /`
- **Description:** Returns comprehensive JSON data about the local UPlanet ecosystem, including active players, NOSTR multipass holders, UMAPs (Universal Maps), and swarm nodes. This endpoint executes the `Ustats.sh` script and provides real-time statistics about the decentralized network.
- **Authentication:** Not required.
- **Query Parameters:**
  - `lat`: Latitude coordinate for geographic filtering (optional, decimal format).
  - `lon`: Longitude coordinate for geographic filtering (optional, decimal format).
  - `deg`: Degree range for geographic area filtering (optional, decimal format).
- **Returns:** JSON object containing ecosystem statistics, player data, NOSTR profiles, UMAPs, and swarm information.

**Examples:**
```bash
# Get all ecosystem data
curl "http://localhost:54321/"

# Filter by geographic area (latitude 45.75, longitude 4.85, 1 degree range)
curl "http://localhost:54321/?lat=45.75&lon=4.85&deg=1.0"
```

**JSON Response Structure:**
```json
{
  "version": "1.1",
  "DATE": "2024-01-15 10:30:00 UTC",
  "IPFSNODEID": "12D3KooW...",
  "myIPFS": "http://127.0.0.1:8080",
  "UPLANETG1PUB": "...",
  "G1": "150.45",
  "ZEN": "1494",
  "BILAN": "42",
  "CENTER": {
    "LAT": "45.75",
    "LON": "4.85", 
    "DEG": "1.0"
  },
  "CLOSEST_UMAPs": [...],
  "SWARM": [...],
  "NOSTR": [...],
  "PLAYERs": [...],
  "UMAPs": [...],
  "GENERATION_TIME": "3"
}
```

---

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

#### `GET /api/getN2`
- **Description:** Analyze the N2 network (friends of friends) for a given NOSTR public key. Returns network topology with N1 (direct connections) and N2 (friends of friends) relationships.
- **Authentication:** Not required.
- **Query Parameters:**
  - `hex`: NOSTR public key in hexadecimal format (64 characters, required).
  - `range`: Analysis mode - `"default"` (only mutual connections) or `"full"` (all N1 connections). Default: `"default"`.
  - `output`: Response format - `"json"` (JSON data) or `"html"` (interactive visualization). Default: `"json"`.
- **Returns:** 
  - **JSON mode:** Network analysis with nodes, connections, statistics, and processing time.
  - **HTML mode:** Interactive p5.js visualization with network graph, controls, and node information.

**Examples:**
```bash
# Basic N2 analysis (JSON)
curl "http://localhost:54321/api/getN2?hex=$CAPTAINHEX"

# Full network analysis (JSON)
curl "http://localhost:54321/api/getN2?hex=$CAPTAINHEX&range=full"

# Interactive visualization (HTML)
curl "http://localhost:54321/api/getN2?hex=$CAPTAINHEX&output=html"
```

**JSON Response Structure:**
```json
{
  "center_pubkey": "$CAPTAINHEX",
  "total_n1": 25,
  "total_n2": 150,
  "total_nodes": 176,
  "range_mode": "default",
  "nodes": [
    {
      "pubkey": "...",
      "level": 1,
      "is_follower": true,
      "is_followed": true,
      "mutual": true,
      "connections": ["..."]
    }
  ],
  "connections": [
    {"from": "...", "to": "..."}
  ],
  "timestamp": "2024-01-15T10:30:00Z",
  "processing_time_ms": 1250
}
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

#### `GET /api/umap/geolinks`
- **Description:** Retrieve geographic links of adjacent UMAPs (Universal Maps) using the `Umap_geonostr.sh` script. This endpoint calculates the hex public keys of neighboring UMAPs (north, south, east, west, etc.) from the central UMAP coordinates. The client application can then use these hex keys to make NOSTR queries directly on the relays it's already connected to.
- **Authentication:** Not required.
- **Query Parameters:**
  - `lat`: Latitude of the central UMAP (decimal format, -90 to 90, required).
  - `lon`: Longitude of the central UMAP (decimal format, -180 to 180, required).
- **Returns:** JSON with UMAP geolinks, coordinates metadata, and processing information.

**Example:**
```bash
curl "http://localhost:54321/api/umap/geolinks?lat=48.8566&lon=2.3522"
```

**JSON Response Structure:**
```json
{
  "success": true,
  "message": "Liens géographiques récupérés pour UMAP (48.8566, 2.3522)",
  "umap_coordinates": {
    "lat": 48.8566,
    "lon": 2.3522
  },
  "geolinks": {
    "north": "abc123def456...",
    "south": "def456ghi789...",
    "east": "ghi789jkl012...",
    "west": "jkl012mno345...",
    "northeast": "mno345pqr678...",
    "northwest": "pqr678stu901...",
    "southeast": "stu901vwx234...",
    "southwest": "vwx234yz567...",
    "here": "yz567abc890..."
  },
  "total_adjacent": 8,
  "timestamp": "2024-01-15T10:30:00Z",
  "processing_time_ms": 150
}
```

**Use Case:**
This endpoint is particularly useful for:
- **Geographic NOSTR Discovery**: Finding NOSTR messages from adjacent geographic zones
- **Local Community Building**: Discovering local projects and proposals in neighboring UMAPs
- **Decentralized Geographic Networks**: Building location-based social networks using NOSTR
- **UPlanet Ecosystem Integration**: Connecting with nearby UMAPs in the UPlanet decentralized network

The client application can use the returned hex keys to query NOSTR relays for messages from these adjacent UMAPs, enabling geographic discovery and local community interactions.

---

#### `GET /check_zencard`
- **Description:** View ZEN Card social shares history for a cooperative member. This endpoint retrieves and displays the transaction history from the SOCIETY wallet (`UPLANETNAME_SOCIETY`) to the member's ZEN Card. It analyzes 3 years of blockchain data, automatically detects share types (satellite/constellation) from transaction comments, and calculates valid balance based on expiration rules.
- **Authentication:** Not required.
- **Query Parameters:**
  - `email`: Email of the ZEN Card holder (required).
  - `html`: If present (any value), returns HTML page. If omitted, returns JSON data.
- **Returns:**
  - **HTML mode:** Interactive responsive page with valid balance, total received, and transaction list with expired shares marked.
  - **JSON mode:** Structured data with shares array, totals, and validation status.

**Examples:**
```bash
# Get JSON data
curl "http://localhost:54321/check_zencard?email=member@example.com"

# Get HTML page
curl "http://localhost:54321/check_zencard?email=member@example.com&html"
```

**JSON Response Structure:**
```json
{
  "zencard_email": "member@example.com",
  "zencard_g1pub": "...",
  "filter_years": 3,
  "filter_period": "Dernières 3 années (2023-2025)",
  "total_received_g1": 54.0,
  "total_received_zen": 540.0,
  "valid_balance_g1": 54.0,
  "valid_balance_zen": 540.0,
  "total_transfers": 3,
  "valid_transfers": 3,
  "transfers": [
    {
      "date": "2025-01-15",
      "year": 2025,
      "amount_g1": 54.0,
      "amount_zen": 540.0,
      "part_type": "constellation",
      "is_valid": true,
      "ipfs_node": "12D3KooW...",
      "comment": "Parts Constellation - UPLANET:xxx:SOCIETY:member@example.com:constellation:12D3KooW..."
    }
  ],
  "timestamp": "2025-10-12T15:30:00Z"
}
```

**Share Types & Validity:**
- **Constellation** (540Ẑ/3years): Valid for 3 years from transaction date
- **Satellite** (50Ẑ/year): Valid only for current year, expires annually
- Expired shares are included in `total_received` but excluded from `valid_balance`

**Backend Processing:**
- Calls `G1zencard_history.sh` script for blockchain analysis
- Filters transactions from `UPLANETNAME_SOCIETY` wallet only
- Parses transaction comments created by `UPLANET.official.sh`
- HTML rendering uses `zencard_api.html` Jinja2 template

**Use Case:**
This endpoint is automatically called when a ZEN Card QR code (format `~~~~~...`) is scanned on `/scan` with the correct PASS code. It's also accessible directly via API for dashboard integration, mobile apps, or administrative monitoring of cooperative members' social shares.

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
    *   **Découverte Géographique UMAP** : Fournit une découverte de messages NOSTR basée sur la géographie en utilisant les UMAPs (Cartes Universelles) adjacentes, permettant la construction de communautés locales et de réseaux sociaux géographiques.

*   **Interaction avec l'Écosystème Ğ1 (Duniter)** :
    *   **Vérification de Solde** : Vérifie les soldes des comptes Ğ1 en utilisant les clés publiques via le script `COINScheck.sh`.
    *   **Paiements ZEN (PalPay)** : Facilite les transactions en cryptomonnaie ZEN (Ẑen) en utilisant les ZenCards, avec un terminal "PalPay" dédié.
    *   **Traitement des Transactions** : Exécute les paiements DUNITER de manière sécurisée en utilisant le script `PAYforSURE.sh`.
    *   **Historique du Portefeuille** : Récupère et affiche l'historique des transactions pour les clés publiques Ğ1.

*   **Traitement et Enregistrement Multimédia** :
    *   **Intégration OBS Studio** : Démarre et arrête les enregistrements dans OBS Studio à distance via une interface web, permettant la création de contenu multimédia au sein de l'écosystème UPassport.
    *   **Enregistrement Webcam** : Capture et traite les blobs vidéo et audio directement depuis les webcams des utilisateurs.
    *   **Traitement du Téléversement de Fichiers** : Gère les téléversements de fichiers vidéo et audio locaux pour le traitement dans le pipeline Astroport.
    *   **Téléchargement de Liens YouTube** : Télécharge et traite les vidéos à partir de liens YouTube.
    *   **Intégration IPFS pour le Multimédia** : Intègre IPFS pour le stockage et la distribution décentralisés des contenus multimédias enregistrés et traités.

*   **Scripts Backend et Opérations Asynchrones** :
    *   **Exécution de Scripts Shell** : Repose sur des scripts Bash (`upassport.sh`, `zen_send.sh`, `check_ssss.sh`, `startrec.sh`, `stoprec.sh`, `u.command.sh`, `upload2ipfs.sh`) pour les fonctionnalités de base, assurant des opérations robustes et sécurisées.
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
    *   **Node.js et npm** : Pour la gestion des actifs frontend (utilisé par le stockage TiddlyWiki associé à la ZenCard).
    *   **FFmpeg** : Pour les tâches de traitement multimédia.
    *   **ImageMagick** : Pour la manipulation d'images, en particulier la génération de QR codes et le traitement d'images.
    *   **IPFS (InterPlanetary File System)** : Assurez-vous qu'IPFS est installé et fonctionne en tant que démon pour le stockage décentralisé.
    *   **GPG (GNU Privacy Guard)** : Pour les opérations cryptographiques, en particulier le déchiffrement et la gestion des clés https://foopgp.org.
    *   **NaCl (libsodium)** : Pour les primitives cryptographiques utilisées par `keygen` (@aya).
    *   **OBS Studio (Expérimental)** : Combiner des fonctionnalités d'enregistrement sur les flux WebRTC https://vdo.ninja.
    *   **`amzqr`** : Générateur de QR codes évolé en ligne de commande (`pip install amzqr`).
    *   **`ssss-split`, `ssss-combine` (outils Shamir's Secret Sharing Scheme)** : découpage des clefs en morceaux permettant de construire la relation 3x1/3 de confiance.
    *   **`natools.py`** : Outils de chiffrement et de déchiffrement faisant partie de l'ensemble d'outils Astroport.ONE (@tuxmain).
    *   **`jaklis.py`** : Interface en ligne de commande DUNITER et Cesium+ (@poka)).

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

    *   **API Statut UPlanet (`/`)** : Retourne des données JSON sur l'écosystème UPlanet local (joueurs, multipass NOSTR, UMAPs, nœuds swarm). Supporte le filtrage géographique avec les paramètres de requête : `?lat=XX.XX&lon=YY.YY&deg=Z.Z` pour filtrer les résultats par zone géographique.
    *   **Scanner QR Code (`/scan`)** : Interface web pour le scan de QR codes général, les actions UPassport et les interactions avec les Cartes NOSTR.
    *   **Terminal de Paiement MULTIPASS (`/scan_multipass_payment.html` - accessible en interne)** : Pour initier des paiements ẐEN entre portefeuilles MULTIPASS (stockés dans ~/.zen/game/nostr/).
    *   **Scanner de Sécurité (`/scan_ssss.html` - accessible en interne)** : Pour la vérification de sécurité UPassport, utilisé par les CAPITAINES de station.
    *   **Interface Carte NOSTR (`/nostr`)** : Pour explorer les fonctionnalités NOSTR selon différents types d'applications (signalées par #BRO + #hashtag).
    *   **Interface NOSTR UPlanet (`/nostr?type=uplanet`)** : Interface spécialisée pour les propositions de projets de la SCIC Cooperative UPlanet avec des capacités de découverte géographique UMAP.
    *   **Interface d'Enregistrement (`/rec`)** : Pour démarrer et arrêter les enregistrements OBS Studio, téléverser des fichiers vidéo ou traiter des liens YouTube.
    *   **Enregistrement Webcam (`/webcam`)** : Pour capturer et traiter la vidéo directement depuis votre webcam.
    *   **Téléversement de Fichiers vers IPFS (`/upload`)** : Pour téléverser des fichiers vers IPFS et obtenir des liens IPFS.
    *   **Création de Compte UPlanet (`/uplanet` ou `/uplanet.html`)** : Pour créer des comptes UPlanet (la fonctionnalité peut être limitée dans le code fourni).
    *   **Description de l'API (`/index` ou `/uplanet`)** : Fournit une description de l'API de base et une page de bienvenue.

## 📱 Types de QR Codes & Workflows de Traitement

Le terminal `/scan` d'UPassport (`upassport.sh`) traite plusieurs types de QR codes, chacun déclenchant un workflow spécifique. Voici un guide complet des formats de QR codes supportés et de leur traitement :

### 1. 🔑 Clé IPNS TiddlyWiki

**Format :** `k51qzi5uqu5d[hash_ipns]`

**Workflow :**
1. Détecte la clé IPNS au format TiddlyWiki
2. **Redirection directe** vers `${myIPFS}/ipns/${TWNS}`

**Cas d'usage :** Accès rapide aux carnets TiddlyWiki décentralisés stockés sur IPFS.

---

### 2. 🌐 Liens HTTP/HTTPS

#### 2a. Lien IPNS ZeroCard (avec 12D3Koo)

**Format :** `http(s)://[domaine]/ipns/12D3Koo[hash]`

**Workflow :**
1. Extrait l'identifiant IPNS 12D3Koo (CARDNS)
2. Convertit en clé publique G1 : `ipfs_to_g1.py $CARDNS`
3. Recherche le membre propriétaire dans `${MY_PATH}/pdf/`
4. Si trouvé : récupère la ZEROCARD associée
5. **Redirige vers** `scan_ssss.html` pour le scan du QR de sécurité SSSS

**Cas d'usage :** Vérification d'UPassport et contrôle de sécurité par les CAPITAINEs de station.

#### 2b. Lien HTTP Générique

**Format :** N'importe quelle URL `http://` ou `https://`

**Workflow :**
1. **Redirection directe** vers l'URL

**Cas d'usage :** Partage de liens simple et navigation web.

---

### 3. 📧 EMAIL - Création/Gestion de Carte NOSTR

**Format :** `email@domaine.tld`

#### Création de nouveau MULTIPASS :
1. Valide le format email
2. Vérifie l'existence d'un MULTIPASS existant
3. Appelle `make_NOSTRCARD.sh "${EMAIL}" "$PASS" "${LAT}" "${LON}"`
4. Génère une paire de clés NOSTR et un portefeuille G1
5. Envoie le MULTIPASS via `mailjet.sh`
6. **Retourne** `.nostr.zine.html` avec MULTIPASS imprimable

#### PASS = "0000" (Suppression de compte) :
- Vérifie que le compte a été créé aujourd'hui (contrôle TODATE)
- Détruit le TW NOSTR : `nostr_DESTROY_TW.sh`
- Marque comme "DELETED" dans le HTML
- **Retourne** confirmation de suppression

#### Compte existant :
- **Retourne** message "ALREADY EXISTING"
- Réaffiche si créé le même jour (TODATE = aujourd'hui)

**Cas d'usage :** Création d'identité décentralisée liée à l'email, avec capacité de suppression le jour même.

---

### 4. 🎫 Authentification MULTIPASS SSSS

**Format :** 
- `M-[encodé_base58]:k51qzi5uqu5d[hash_ipns]` (Base58)
- `1-[encodé_hex]:k51qzi5uqu5d[hash_ipns]` (Hex)

#### Processus de décodage :
1. **M-** : Décode en Base58 avec `Mbase58.py decode`
2. **1-** : Utilise l'hex directement
3. Extrait `SSSS1` (part 1) et `IPNSVAULT` (clé IPNS)

#### Authentification :
1. Recherche le PLAYER via `get_NOSTRNS_directory()`
2. Déchiffre `ssss.tail.uplanet.enc` avec la clé UPLANET
3. Combine les parts SSSS : `ssss-combine -t 2`
4. Récupère `salt` et `pepper`
5. Génère le NSEC à partir des secrets récupérés

#### Actions selon le code PASS :

**PASS = "" ou "[lang]" (Message Rapide - Par défaut) :**
- Génère le NSEC avec salt/pepper : `keygen -t nostr "${salt}" "${pepper}" -s`
- Crée `nostr.html` avec nsec pré-rempli
- **Retourne** interface de message NOSTR simple
- Mode par défaut pour les messages géographiques rapides

**PASS = "1111" (Accès Complet) :**
- Génère le NSEC avec salt/pepper
- Crée `astro_base.html` avec nsec auto-rempli
- Injecte JavaScript pour authentification automatique
- Sélectionne automatiquement le mode d'authentification nsec
- **Retourne** interface Astro Base complète avec toutes les fonctionnalités NOSTR

**PASS = "0000" (Remboursement & Fermeture de Compte) :**
- Génère la dunikey avec salt/pepper
- Récupère le solde G1PUBNOSTR
- Vide le portefeuille vers G1PRIME : `PAYforSURE.sh`
- Met à jour TODATE (un jour pour réactiver)
- **Retourne** message de confirmation de remboursement

**Codes PASS personnalisés :**
- Réservés pour les fonctionnalités et extensions futures

**Cas d'usage :** Authentification MULTIPASS sécurisée avec partage de secret Shamir (seuil 2-sur-3), permettant plusieurs modes d'interface selon le code PASS.

---

### 5. 💰 Clé Publique G1 (Duniter)

**Format :** Chaîne base58 de 43-44 caractères (clé pub G1)

#### Premier Scan (Pas de ZEROCARD) :
1. Valide le format G1 : `g1_to_ipfs.py ${PUBKEY}`
2. Récupère l'historique TX : `G1history.sh ${PUBKEY} 25`
3. Vérifie le statut MEMBER : `/wot/lookup/$PUBKEY`
4. **Si simple portefeuille :** affiche seulement le solde
5. **Si membre (a un UID) :**
   - Analyse le réseau N1 (certifications P2P, 12P, P21)
   - Crée la ZEROCARD avec découpage de clé SSSS
   - Génère la clé IPNS pour le stockage décentralisé
   - Crée l'HTML UPassport avec QR codes
   - **Retourne** `_index.html` (fac-similé UPassport)

#### Deuxième Scan (ZEROCARD Existe, Pas Activée) :
1. Vérifie la dernière transaction
2. **Si TX vers ZEROCARD :** active l'UPassport
   - Publie sur IPNS
   - Crée un lien symbolique vers `~/.zen/game/passport/`
   - Met à jour le profil NOSTR avec G1PUB (si authentifié WoT)
   - Chiffre avec UPLANETNAME
3. **Retourne** page de confirmation d'activation

#### Troisième+ Scans (ZEROCARD Activée) :
1. **Si COMM présent et DEST == ZEROCARD :**
   - Exécute la commande : `u.command.sh "$PUBKEY" "$COMM" "$LASTX" "$TXDATE" "$ZEROCARD"`
2. **Si DRIVESTATE existe :**
   - **Redirige** vers le DRIVESTATE IPFS actuel
3. **Sinon :** affiche le fac-similé

**Cas d'usage :** Cycle de vie complet de l'UPassport depuis la découverte du membre jusqu'à l'activation et la gestion continue.

---

## 📊 Résumé des Flux de Traitement QR Code

```
┌─────────────────────┬──────────────────────────────────────────┐
│ Type QR Code        │ Destination/Action                       │
├─────────────────────┼──────────────────────────────────────────┤
│ k51qzi5uqu5d...     │ → /ipns/[clé] (redirection IPFS)        │
│ http://...12D3Koo   │ → scan_ssss.html (vérification sécurité)│
│ http://...          │ → Redirection URL directe                │
│ email@domaine.tld   │ → Création NOSTRCARD + email            │
│ M-...:k51qzi...     │ → nostr.html ou astro_base.html         │
│   (PASS=""/lang)    │   → Interface Message Rapide            │
│   (PASS=1111)       │   → Interface Astro Base complète       │
│   (PASS=9999)       │   → Terminal de Paiement MULTIPASS     │
│   (PASS=0000)       │   → Remboursement + destruction         │
│ ~~~~~...            │ → API /check_zencard (Historique ZEN Card) │
│   (with PASS)       │   → Redirection vers API pour affichage │
│                     │   → Analyse blockchain 3 ans            │
│                     │   → Solde valide vs total reçu          │
│ [CLÉ PUB G1]        │ → Création/gestion UPassport            │
│   (1er scan)        │   → Créer passport (membre)             │
│   (2ème scan)       │   → Activer passport (vérif TX)         │
│   (3ème+ scan)      │   → Exécution commande / DRIVESTATE     │
└─────────────────────┴──────────────────────────────────────────┘
```

**Note :** Les ZEN Cards (utilisées par `UPLANET.official.sh`) servent à la comptabilité des parts sociales via l'historique blockchain, pas à détenir des fonds. Les MULTIPASS (Cartes NOSTR) détiennent les ẐEN réels pour les paiements.

### Référence des Codes PASS MULTIPASS

| Code PASS | Interface      | Cas d'usage                              |
|-----------|----------------|------------------------------------------|
| `""`      | nostr.html     | Message géographique rapide (par défaut) |
| `"fr"/"en"`| nostr.html    | Message rapide spécifique à la langue    |
| `"1111"`  | astro_base.html| Messagerie NOSTR complète avec toutes fonctionnalités |
| `"9999"`  | scan_multipass_payment.html | Terminal de Paiement MULTIPASS |
| `"0000"`  | Remboursement  | Fermeture compte + vidage portefeuille   |
| Personnalisé | Réservé     | Fonctionnalités futures                  |

### Fonctionnalités de Sécurité

- **SSSS (Shamir Secret Sharing)** : Clés MULTIPASS divisées en 3 parts (HEAD/MIDDLE/TAIL), nécessitant 2-sur-3 pour la reconstruction
- **Stockage Chiffré** : ZenCard utilise le chiffrement GPG AES avec mot de passe
- **Clé UPLANET** : Part MIDDLE chiffrée avec le nom UPLANET pour liaison géographique
- **Clé CAPTAIN** : Part TAIL chiffrée avec la clé pub CAPTAIN pour récupération administrative
- **Clé MEMBER** : Part HEAD chiffrée avec la clé pub G1 du membre pour contrôle personnel
- **Suppression Temporelle** : Les comptes ne peuvent être supprimés que le jour de création (contrôle TODATE)

---

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
| `/`                       | GET     | Statut écosystème UPlanet (Ustats.sh)      | Non          | `lat`, `lon`, `deg` (query params) |
| `/api/upload`             | POST    | Téléverser un fichier vers votre uDRIVE (IPFS) | Oui (npub) | `file`, `npub` (form-data)    |
| `/api/upload_from_drive`  | POST    | Synchroniser un fichier depuis IPFS vers votre uDRIVE | Oui (npub) | `ipfs_link`, `npub` (JSON)    |
| `/api/delete`             | POST    | Supprimer un fichier de votre uDRIVE        | Oui (npub) | `file_path`, `npub` (JSON)    |
| `/api/getN2`              | GET     | Analyser le réseau N2 (amis d'amis)        | Non          | `hex`, `range`, `output` (query params) |
| `/api/test-nostr`         | POST    | Tester l'authentification NOSTR pour une clé publique | Non | `npub` (form-data)            |
| `/api/umap/geolinks`      | GET     | Obtenir les liens géographiques des UMAPs adjacentes | Non | `lat`, `lon` (query params)   |
| `/check_zencard`          | GET     | Voir l'historique des parts sociales ZEN Card | Non    | `email`, `html` (query params) |
| `/check_society`          | GET     | Voir les contributions capital SOCIETY     | Non          | `html` (query param)          |
| `/check_revenue`          | GET     | Voir le chiffre d'affaires ZENCOIN          | Non          | `html`, `year` (query params) |

---

### Détails des Endpoints

#### `GET /`
- **Description :** Retourne des données JSON complètes sur l'écosystème UPlanet local, incluant les joueurs actifs, les détenteurs de multipass NOSTR, les UMAPs (Cartes Universelles), et les nœuds swarm. Cet endpoint exécute le script `Ustats.sh` et fournit des statistiques en temps réel sur le réseau décentralisé.
- **Authentification :** Non requise.
- **Paramètres de requête :**
  - `lat` : Coordonnée de latitude pour le filtrage géographique (optionnel, format décimal).
  - `lon` : Coordonnée de longitude pour le filtrage géographique (optionnel, format décimal).
  - `deg` : Plage de degrés pour le filtrage de zone géographique (optionnel, format décimal).
- **Retourne :** Objet JSON contenant les statistiques de l'écosystème, données des joueurs, profils NOSTR, UMAPs, et informations swarm.

**Exemples :**
```bash
# Obtenir toutes les données de l'écosystème
curl "http://localhost:54321/"

# Filtrer par zone géographique (latitude 45.75, longitude 4.85, plage de 1 degré)
curl "http://localhost:54321/?lat=45.75&lon=4.85&deg=1.0"
```

**Structure de réponse JSON :**
```json
{
  "version": "1.1",
  "DATE": "2024-01-15 10:30:00 UTC",
  "IPFSNODEID": "12D3KooW...",
  "myIPFS": "http://127.0.0.1:8080",
  "UPLANETG1PUB": "...",
  "G1": "150.45",
  "ZEN": "1494",
  "BILAN": "42",
  "CENTER": {
    "LAT": "45.75",
    "LON": "4.85", 
    "DEG": "1.0"
  },
  "CLOSEST_UMAPs": [...],
  "SWARM": [...],
  "NOSTR": [...],
  "PLAYERs": [...],
  "UMAPs": [...],
  "GENERATION_TIME": "3"
}
```

---

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

#### `GET /api/getN2`
- **Description :** Analyser le réseau N2 (amis d'amis) pour une clé publique NOSTR donnée. Retourne la topologie du réseau avec les relations N1 (connexions directes) et N2 (amis d'amis).
- **Authentification :** Non requise.
- **Paramètres de requête :**
  - `hex` : Clé publique NOSTR en format hexadécimal (64 caractères, requis).
  - `range` : Mode d'analyse - `"default"` (connexions mutuelles uniquement) ou `"full"` (toutes les connexions N1). Défaut : `"default"`.
  - `output` : Format de réponse - `"json"` (données JSON) ou `"html"` (visualisation interactive). Défaut : `"json"`.
- **Retourne :** 
  - **Mode JSON :** Analyse du réseau avec nœuds, connexions, statistiques et temps de traitement.
  - **Mode HTML :** Visualisation interactive p5.js avec graphique réseau, contrôles et informations des nœuds.

**Exemples :**
```bash
# Analyse N2 basique (JSON)
curl "http://localhost:54321/api/getN2?hex=$CAPTAINHEX"

# Analyse réseau complète (JSON)
curl "http://localhost:54321/api/getN2?hex=$CAPTAINHEX&range=full"

# Visualisation interactive (HTML)
curl "http://localhost:54321/api/getN2?hex=$CAPTAINHEX&output=html"
```

**Structure de réponse JSON :**
```json
{
  "center_pubkey": "$CAPTAINHEX",
  "total_n1": 25,
  "total_n2": 150,
  "total_nodes": 176,
  "range_mode": "default",
  "nodes": [
    {
      "pubkey": "...",
      "level": 1,
      "is_follower": true,
      "is_followed": true,
      "mutual": true,
      "connections": ["..."]
    }
  ],
  "connections": [
    {"from": "...", "to": "..."}
  ],
  "timestamp": "2024-01-15T10:30:00Z",
  "processing_time_ms": 1250
}
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

#### `GET /api/umap/geolinks`
- **Description :** Récupérer les liens géographiques des UMAPs (Cartes Universelles) adjacentes en utilisant le script `Umap_geonostr.sh`. Cet endpoint calcule les clés publiques hex des UMAPs voisines (nord, sud, est, ouest, etc.) à partir des coordonnées de l'UMAP centrale. L'application cliente peut ensuite utiliser ces clés hex pour faire des requêtes NOSTR directement sur les relais auxquels elle est déjà connectée.
- **Authentification :** Non requise.
- **Paramètres de requête :**
  - `lat` : Latitude de l'UMAP centrale (format décimal, -90 à 90, requis).
  - `lon` : Longitude de l'UMAP centrale (format décimal, -180 à 180, requis).
- **Retourne :** JSON avec les liens géographiques des UMAPs, les métadonnées des coordonnées et les informations de traitement.

**Exemple :**
```bash
curl "http://localhost:54321/api/umap/geolinks?lat=48.8566&lon=2.3522"
```

**Structure de réponse JSON :**
```json
{
  "success": true,
  "message": "Liens géographiques récupérés pour UMAP (48.8566, 2.3522)",
  "umap_coordinates": {
    "lat": 48.8566,
    "lon": 2.3522
  },
  "geolinks": {
    "north": "abc123def456...",
    "south": "def456ghi789...",
    "east": "ghi789jkl012...",
    "west": "jkl012mno345...",
    "northeast": "mno345pqr678...",
    "northwest": "pqr678stu901...",
    "southeast": "stu901vwx234...",
    "southwest": "vwx234yz567...",
    "here": "yz567abc890..."
  },
  "total_adjacent": 8,
  "timestamp": "2024-01-15T10:30:00Z",
  "processing_time_ms": 150
}
```

**Cas d'usage :**
Cet endpoint est particulièrement utile pour :
- **Découverte NOSTR Géographique** : Trouver des messages NOSTR depuis des zones géographiques adjacentes
- **Construction de Communautés Locales** : Découvrir des projets et propositions locaux dans les UMAPs voisines
- **Réseaux Géographiques Décentralisés** : Construire des réseaux sociaux basés sur la localisation en utilisant NOSTR
- **Intégration Écosystème UPlanet** : Se connecter avec les UMAPs voisines dans le réseau décentralisé UPlanet

L'application cliente peut utiliser les clés hex retournées pour interroger les relais NOSTR pour des messages de ces UMAPs adjacentes, permettant la découverte géographique et les interactions communautaires locales.

---

#### `GET /check_zencard`
- **Description :** Affiche l'historique des parts sociales (ZEN Card) pour un membre de la coopérative. Cet endpoint récupère et affiche l'historique des transactions depuis le portefeuille SOCIETY (`UPLANETNAME_SOCIETY`) vers la ZEN Card du membre. Il analyse 3 années de données blockchain, détecte automatiquement les types de parts (satellite/constellation) depuis les commentaires de transaction, et calcule le solde valide selon les règles d'expiration.
- **Authentification :** Non requise.
- **Paramètres de requête :**
  - `email` : Email du détenteur de la ZEN Card (requis).
  - `html` : Si présent (n'importe quelle valeur), retourne une page HTML. Si omis, retourne les données JSON.
- **Retourne :**
  - **Mode HTML :** Page interactive responsive avec solde valide, total reçu, et liste des transactions avec parts expirées marquées.
  - **Mode JSON :** Données structurées avec tableau de parts, totaux et statut de validation.

**Exemples :**
```bash
# Obtenir les données JSON
curl "http://localhost:54321/check_zencard?email=membre@example.com"

# Obtenir la page HTML
curl "http://localhost:54321/check_zencard?email=membre@example.com&html"
```

**Structure de réponse JSON :**
```json
{
  "zencard_email": "membre@example.com",
  "zencard_g1pub": "...",
  "filter_years": 3,
  "filter_period": "Dernières 3 années (2023-2025)",
  "total_received_g1": 54.0,
  "total_received_zen": 540.0,
  "valid_balance_g1": 54.0,
  "valid_balance_zen": 540.0,
  "total_transfers": 3,
  "valid_transfers": 3,
  "transfers": [
    {
      "date": "2025-01-15",
      "year": 2025,
      "amount_g1": 54.0,
      "amount_zen": 540.0,
      "part_type": "constellation",
      "is_valid": true,
      "ipfs_node": "12D3KooW...",
      "comment": "Parts Constellation - UPLANET:xxx:SOCIETY:membre@example.com:constellation:12D3KooW..."
    }
  ],
  "timestamp": "2025-10-12T15:30:00Z"
}
```

**Types de Parts & Validité :**
- **Constellation** (540Ẑ/3ans) : Valable 3 ans à partir de la date de transaction
- **Satellite** (50Ẑ/an) : Valable uniquement pour l'année en cours, expire annuellement
- Les parts expirées sont incluses dans `total_received` mais exclues du `valid_balance`

**Traitement Backend :**
- Appelle le script `G1zencard_history.sh` pour l'analyse blockchain
- Filtre uniquement les transactions du portefeuille `UPLANETNAME_SOCIETY`
- Parse les commentaires de transaction créés par `UPLANET.official.sh`
- Rendu HTML utilise le template Jinja2 `zencard_api.html`

**Cas d'usage :**
Cet endpoint est automatiquement appelé lorsqu'un QR code ZEN Card (format `~~~~~...`) est scanné sur `/scan` avec le bon code PASS. Il est aussi accessible directement via API pour intégration dans des tableaux de bord, applications mobiles ou monitoring administratif des parts sociales des membres coopératifs.

---

### Notes d'Authentification
- Toutes les actions spécifiques à l'utilisateur nécessitent un événement d'authentification NOSTR NIP42 récent sur le relai local (`ws://127.0.0.1:7777`).
- Si l'authentification échoue, assurez-vous que votre client NOSTR a publié un événement de type 22242 dans les dernières 24 heures.

---

### Voir Aussi
- [Guide Développeur API Astroport.ONE & UPlanet Swarm](../Astroport.ONE/API.NOSTRAuth.readme.md)
- [Protocole NOSTR](https://github.com/nostr-protocol/nostr)
- [NIP42 - Authentification](https://github.com/nostr-protocol/nips/blob/master/42.md)
