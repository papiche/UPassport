#!/usr/bin/env python3
import sys
import subprocess
import json
import wave
import os
import logging
import time
import re
import torch
from transformers import WhisperProcessor, WhisperForConditionalGeneration
from pathlib import Path

# Configuration du logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Fonction pour détecter le type de fichier
def detect_file_type_with_ffprobe(file_path):
    logging.info(f"Détection du type de fichier : {file_path}")
    try:
        command = [
            'ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
            'stream=codec_type', '-of', 'csv=p=0', file_path
        ]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0 and result.stdout.strip() == "video":
            logging.info("Type détecté : vidéo")
            return 'video'

        command = [
            'ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries',
            'stream=codec_type', '-of', 'csv=p=0', file_path
        ]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0 and result.stdout.strip() == "audio":
            logging.info("Type détecté : audio")
            return 'audio'

    except Exception as e:
        logging.error(f"Erreur lors de la détection du type de fichier : {e}")

    return None

def check_file_type(input_file):
    logging.info(f"Vérification du type de fichier : {input_file}")
    file_type = detect_file_type_with_ffprobe(input_file)
    if file_type:
        logging.info(f"Type de fichier reconnu : {file_type}")
        return file_type
    else:
        logging.error(f"Type de fichier non reconnu pour : {input_file}")
        return None

def extract_audio(input_file, output_file):
    logging.info(f"Extraction audio depuis : {input_file} vers : {output_file}")
    command = [
        'ffmpeg', '-i', input_file, '-map', '0:a:0', '-vn', '-acodec', 'pcm_s16le',
        '-ar', '16000', '-ac', '1', output_file, '-loglevel', 'quiet'
    ]
    try:
        subprocess.run(command, check=True)
        logging.info(f"Extraction audio terminée et sauvegardée dans : {output_file}")
    except subprocess.CalledProcessError as e:
        logging.error(f"Erreur lors de l'extraction audio : {e}")
        sys.exit(1)

def convert_to_wav(input_file, output_file):
    logging.info(f"Conversion du fichier audio : {input_file} vers WAV : {output_file}")
    if input_file.lower().endswith(".wav"):
        try:
            subprocess.run(['cp', input_file, output_file], check=True)
            logging.info(f"Fichier WAV copié directement : {output_file}")
            return
        except subprocess.CalledProcessError as e:
            logging.error(f"Erreur lors de la copie du fichier WAV : {e}")
            sys.exit(1)

    command = [
        'ffmpeg', '-i', input_file, '-map', '0:a:0', '-vn', '-acodec', 'pcm_s16le',
        '-ar', '16000', '-ac', '1', output_file, '-loglevel', 'quiet'
    ]
    try:
        subprocess.run(command, check=True)
        logging.info(f"Conversion terminée et sauvegardée dans : {output_file}")
    except subprocess.CalledProcessError as e:
        logging.error(f"Erreur lors de la conversion en WAV : {e}")
        sys.exit(1)

def transcribe_with_whispercpp(wav_file):
    logging.info(f"Début de la transcription avec whisper.cpp du fichier WAV : {wav_file}")
    command = [
        './whisper.bin',  # Assurez-vous que whisper.bin est compilé et disponible dans le même répertoire
        '--model', 'small',  # Exemple de modèle, ajustez selon vos besoins
        '--file', wav_file,
        '--language', 'fr'
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        transcription = result.stdout
        logging.info("Transcription terminée avec whisper.cpp")
        return transcription.strip()
    except subprocess.CalledProcessError as e:
        logging.error(f"Erreur lors de la transcription avec whisper.cpp : {e}")
        sys.exit(1)

def transcribe_with_openai_whisper(wav_file):
    logging.info(f"Début de la transcription avec OpenAI Whisper du fichier WAV : {wav_file}")

    # Charger le modèle et le processeur pour Whisper
    processor = WhisperProcessor.from_pretrained("openai/whisper-large")
    model = WhisperForConditionalGeneration.from_pretrained("openai/whisper-large").to("cuda")

    # Charger l'audio
    audio_input = processor(wav_file, return_tensors="pt", sampling_rate=16000).input_values.to("cuda")

    # Effectuer la transcription
    with torch.no_grad():
        logits = model.generate(audio_input)

    transcription = processor.decode(logits[0], skip_special_tokens=True)
    logging.info("Transcription terminée avec OpenAI Whisper")
    return transcription.strip()

if __name__ == "__main__":
    logging.info("Script de transcription démarré")
    if len(sys.argv) < 2 or len(sys.argv) > 3:
        logging.error("Usage incorrect. Fournissez un fichier d'entrée et, optionnellement, un fichier de sortie.")
        sys.exit(1)

    input_file = sys.argv[1]
    output_transcription_file = sys.argv[2] if len(sys.argv) == 3 else "transcription.txt"

    file_type = check_file_type(input_file)
    if file_type is None:
        logging.error("Type de fichier non pris en charge.")
        sys.exit(1)

    wav_file = "temp.wav"
    if file_type == 'video':
        extract_audio(input_file, wav_file)
    elif file_type == 'audio':
        convert_to_wav(input_file, wav_file)

    # Choisissez le mode de transcription (whisper.cpp ou OpenAI Whisper)
    transcription = None
    if Path("./whisper.cpp").exists():
        transcription = transcribe_with_whispercpp(wav_file)
    else:
        transcription = transcribe_with_openai_whisper(wav_file)

    with open(output_transcription_file, "w") as f:
        f.write(transcription)
        logging.info(f"Transcription sauvegardée dans : {output_transcription_file}")

    os.remove(wav_file)
    logging.info("Fichier temporaire supprimé")
    logging.info("Script terminé avec succès")
