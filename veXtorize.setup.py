#!/usr/bin/env python3
import subprocess
import sys
import os
import requests
from tqdm import tqdm
import zipfile

def install(package):
    subprocess.check_call([sys.executable, "-m", "pip", "install", package])

def download_file(url, filename):
    response = requests.get(url, stream=True)
    total_size = int(response.headers.get('content-length', 0))
    block_size = 1024
    with open(filename, "wb") as f, tqdm(
        desc=filename,
        total=total_size,
        unit='iB',
        unit_scale=True,
        unit_divisor=1024,
    ) as progress_bar:
        for data in response.iter_content(block_size):
            size = f.write(data)
            progress_bar.update(size)

def setup():
    print("Installing required packages...")
    packages = ["transformers", "torch", "sentencepiece", "vosk", "tqdm"]
    for package in packages:
        install(package)

    print("\nDownloading pre-trained models...")
    model_name = "sentence-transformers/all-MiniLM-L6-v2"

    print(f"Downloading tokenizer for {model_name}")
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    tokenizer.save_pretrained("./models/tokenizer")

    print(f"Downloading model for {model_name}")
    from transformers import AutoModel
    model = AutoModel.from_pretrained(model_name)
    model.save_pretrained("./models/model")

    # Vosk model selection and download
    vosk_models = {
        "fr": {
            "small": {
                "name": "vosk-model-small-fr-0.22",
                "url": "https://alphacephei.com/vosk/models/vosk-model-small-fr-0.22.zip",
                "size": "41M"
            },
            "big": {
                "name": "vosk-model-fr-0.22",
                "url": "https://alphacephei.com/vosk/models/vosk-model-fr-0.22.zip",
                "size": "1.4G"
            }
        },
        "en": {
            "small": {
                "name": "vosk-model-small-en-us-0.15",
                "url": "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip",
                "size": "40M"
            },
            "big": {
                "name": "vosk-model-en-us-0.22",
                "url": "https://alphacephei.com/vosk/models/vosk-model-en-us-0.22.zip",
                "size": "1.8G"
            }
        }
    }

    print("\nVosk model selection:")
    lang = input("Choose language (fr/en): ").lower()
    while lang not in vosk_models:
        lang = input("Invalid choice. Choose language (fr/en): ").lower()

    size = input("Choose model size (small/big): ").lower()
    while size not in vosk_models[lang]:
        size = input("Invalid choice. Choose model size (small/big): ").lower()

    selected_model = vosk_models[lang][size]
    print(f"\nDownloading Vosk model: {selected_model['name']} ({selected_model['size']})")

    model_dir = "./vosk_model"
    os.makedirs(model_dir, exist_ok=True)
    zip_path = os.path.join(model_dir, f"{selected_model['name']}.zip")

    download_file(selected_model['url'], zip_path)

    print("Extracting Vosk model...")
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(model_dir)

    os.remove(zip_path)

    # Update the symbolic link for the selected model
    selected_link = os.path.join(model_dir, "selected")
    if os.path.islink(selected_link) or os.path.exists(selected_link):
        os.remove(selected_link)  # Remove existing link or file
    os.symlink(selected_model['name'], selected_link)  # Corrected line

    print("\nSetup complete!")

if __name__ == "__main__":
    setup()

print("\nConcepts clés :")
print("1. AutoTokenizer : Classe qui charge automatiquement le bon tokenizer pour un modèle donné.")
print("2. AutoModel : Classe qui charge automatiquement la bonne architecture de modèle pour un modèle donné.")
print("3. from_pretrained() : Méthode pour charger des modèles pré-entraînés à partir de Hugging Face.")
print("4. Vosk : Bibliothèque de reconnaissance vocale offline utilisant des modèles pré-entraînés.")
print("\nUtilisation :")
print("tokenizer = AutoTokenizer.from_pretrained('./models/tokenizer')")
print("model = AutoModel.from_pretrained('./models/model').to(device)")
print("vosk_model = Model('./vosk_model/selected/vosk-model-xx-xx-x.xx')")
print("\nCes lignes chargent le tokenizer, le modèle de langage et le modèle Vosk à partir des fichiers téléchargés localement.")
