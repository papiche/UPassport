#!/usr/bin/env python3
import torch
import subprocess
import os
import sys
import logging
import shutil

# Configuration du logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def check_gpu_availability():
    """Vérifie si un GPU est disponible via PyTorch."""
    if torch.cuda.is_available():
        logging.info("GPU détecté : Utilisation de OpenAI Whisper sur GPU.")
        return True
    else:
        logging.info("Aucun GPU détecté. Installation de whisper.cpp recommandée.")
        return False

def install_whisper_cpp():
    """Guide l'utilisateur pour installer whisper.cpp si aucun GPU n'est trouvé."""
    logging.info("Détection de l'absence de GPU... Installation de whisper.cpp recommandée.")

    # Vérifie si le dossier whisper.cpp existe
    whisper_cpp_dir = './whisper.cpp'
    if not os.path.exists(whisper_cpp_dir):
        logging.info("whisper.cpp n'est pas trouvé. Clonage du dépôt...")
        try:
            # Cloner le dépôt whisper.cpp depuis GitHub
            subprocess.run(['git', 'clone', 'https://github.com/ggerganov/whisper.cpp.git'], check=True)
            logging.info("whisper.cpp cloné avec succès.")
        except subprocess.CalledProcessError as e:
            logging.error(f"Erreur lors du clonage de whisper.cpp : {e}")
            sys.exit(1)

    # Naviguer dans le dossier whisper.cpp et lancer la compilation
    os.chdir(whisper_cpp_dir)

    if sys.platform == "win32":
        logging.info("Système Windows détecté. Compilation de whisper.cpp avec CMake...")
        try:
            subprocess.run(['mkdir', 'build'], check=True)
            os.chdir('build')
            subprocess.run(['cmake', '..'], check=True)
            subprocess.run(['cmake', '--build', '.', '--config', 'Release'], check=True)
            logging.info("Compilation de whisper.cpp terminée.")
            # Copier le binaire vers ./whisper.bin
            shutil.copy(os.path.join('build', 'whisper.exe'), '../whisper.bin')
            logging.info("Binaire copié vers ./whisper.bin")
        except subprocess.CalledProcessError as e:
            logging.error(f"Erreur lors de la compilation de whisper.cpp : {e}")
            sys.exit(1)
    else:
        logging.info("Système Unix (Linux/macOS) détecté. Compilation de whisper.cpp avec Make...")
        try:
            subprocess.run(['make'], check=True)
            logging.info("Compilation de whisper.cpp terminée.")
            # Copier le binaire vers ./whisper.bin
            shutil.copy('main', '../whisper.bin')
            logging.info("Binaire copié vers ./whisper.bin")
        except subprocess.CalledProcessError as e:
            logging.error(f"Erreur lors de la compilation de whisper.cpp : {e}")
            sys.exit(1)

    logging.info("whisper.cpp est maintenant installé et prêt à l'emploi.")

if __name__ == "__main__":
    logging.info("Démarrage de la vérification de l'GPU et de l'installation de whisper.cpp.")

    if not check_gpu_availability():
        install_whisper_cpp()

    logging.info("Fin du script.")
