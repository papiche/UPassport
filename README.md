Here's a `README.md` draft for your upassport.sh:

```markdown
# Zine Passport Generator

## Overview

This upassport.sh automates the creation of a digital Zine Passport using the Duniter blockchain and Cesium+ profiles. It fetches information from public nodes, generates QR codes, creates a PDF passport, and encrypts data for secure storage. The process includes:

- Fetching blockchain data from Duniter and Cesium+ nodes.
- Generating user-specific QR codes (with UID and geographic coordinates).
- Encrypting sensitive data using Shamir's Secret Sharing scheme.
- Adding images and data to IPFS for decentralized storage.

## Requirements

- **Duniter and Cesium+ nodes**: Ensure the Duniter node (`https://g1.cgeek.fr`) and Cesium+ node (`https://g1.data.e-is.pro`) are accessible.
- **Dependencies**:
  - `jq` for processing JSON.
  - `wget`, `curl` for fetching data.
  - `convert` (ImageMagick) for image manipulation.
  - `amzqr` for QR code generation.
  - `ssss` for Shamir Secret Sharing.
  - `ipfs` for decentralized storage.
  - Custom upassport.shs (`timeout.sh`, `diceware.sh`, `jaklis.py`, `keygen`, `natools.py`).

## Usage

```bash
./upassport.sh.sh <pubkey>
```

- **Argument**: `<pubkey>` is the public key of the member for whom the passport is generated.

### Steps Performed:
1. **Cache management**: Fetches and caches data from Duniter and Cesium+ nodes.
2. **QR code generation**: Creates a personalized QR code containing the user's UID, Cesium+ profile image, and coordinates.
3. **Certification data**: Compares certifications received and given by the user.
4. **IPNS key creation**: Generates IPNS keys for secure decentralized storage.
5. **Zine Passport generation**: Merges images and data into a Zine Passport HTML file and opens it.

### Output Files:
- **PDFs** and **QR codes** stored in `./pdf/<pubkey>/`.
- An HTML file (`PASSPORT.<uid>.html`) displaying the passport.

## Encryption

- **Shamir's Secret Sharing**: The upassport.sh uses this method to split a secret (such as a password or a key) into parts, only a subset of which is required to reconstruct the secret.

## Example

```bash
./upassport.sh.sh DsEx1pS33vzYZg4MroyBV9hCw98j1gtHEhwiZ5tK7ech
```

## License

This project is licensed under the AGPL-3.0 License. See the [LICENSE](https://choosealicense.com/licenses/agpl-3.0/) for more details.
```

Let me know if you need any specific details added!

