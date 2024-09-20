# UPassport

UPassport est un générateur de passeports numériques pour la monnaie libre Ğ1, conçu pour créer des identités visuelles uniques basées sur les clés publiques des utilisateurs.

## Fonctionnalités

- Scan de QR codes contenant des clés publiques Ğ1
- Génération de passeports numériques personnalisés
- Intégration avec le réseau Duniter et Cesium+
- Création de QR codes pour les relations entre membres (certifications)
- Interface web responsive pour une utilisation sur mobile et desktop

## Installation

1. Clonez le dépôt :
   ```
   git clone https://github.com/papiche/UPassport.git
   cd UPassport
   ```

2. Installez les dépendances :
   ```
   pip install -r requirements.txt
   ```

3. Assurez-vous d'avoir les outils suivants installés :
   - ImageMagick
   - IPFS
   - GPG

## Utilisation

1. Lancez l'application FastAPI :
   ```
   uvicorn main:app --host 0.0.0.0 --port 54321
   ```

2. Ouvrez votre navigateur et accédez à `http://localhost:54321`

3. Utilisez l'interface pour scanner un QR code ou entrer manuellement une clé publique Ğ1

4. Le passeport généré sera affiché et pourra être sauvegardé ou partagé

## Configuration

- Modifiez les URLs des nœuds Duniter et Cesium+ dans le fichier `upassport.sh`
- Personnalisez les templates HTML dans le dossier `templates/`

## Contribution

Les contributions sont les bienvenues ! N'hésitez pas à ouvrir une issue ou à soumettre une pull request.

## Licence

Ce projet est sous licence AGPL-3.0. Voir le fichier [LICENSE](LICENSE) pour plus de détails.

## Auteur

Fred (DsEx1pS33vzYZg4MroyBV9hCw98j1gtHEhwiZ5tK7ech)

## Remerciements

- Communauté Ğ1 et Duniter
- Contributeurs des bibliothèques utilisées dans ce projet

Citations:
[1] https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/22988433/408ffeac-6c8b-4f18-b2cb-1ae099fdb046/paste.txt
[2] https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/22988433/d34a8481-b715-4b4b-b781-d727f7ec87b1/paste.txt
[3] https://github.com/papiche/UPassport/
[4] https://h2020-passport.eu
[5] https://dev.to/joshuajee/nodejs-github-authentication-using-passportjs-and-mongodb-2lfd
[6] https://resource.pass-port.com/PBSHelpHTM/1206/pbsdocs/V1206Release.pdf
[7] https://www.passportjs.org/packages/passport-github/
[8] https://www.youtube.com/watch?v=SBC9na-dadE
[9] https://www.globaleducation.vt.edu/Homepage/Passport_Project.html
[10] https://www.thalesgroup.com/en/markets/digital-identity-and-security/government/passport/passport-security-design
[11] https://lepetitmagicien.com/tour-de-cartes/9534-passport-project-yoan-tanuji-.html

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

