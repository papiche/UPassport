#!/usr/bin/env python3
import sys
import subprocess
import json
import wave
import os
from vosk import Model, KaldiRecognizer, SetLogLevel
import mimetypes
import logging
import time
import re

# Configuration du logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

SetLogLevel(-1)

def detect_file_type_with_ffprobe(file_path):
    """Utilise ffprobe pour identifier le type de fichier (audio ou vidéo)."""
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
    """Vérifie le type de fichier avec ffprobe."""
    logging.info(f"Vérification du type de fichier : {input_file}")
    file_type = detect_file_type_with_ffprobe(input_file)
    if file_type:
        logging.info(f"Type de fichier reconnu : {file_type}")
        return file_type
    else:
        logging.error(f"Type de fichier non reconnu pour : {input_file}")
        return None

def run_command_with_progress(command, process_name):
    """Exécute une commande et affiche la progression et le temps estimé restant."""
    logging.info(f"Exécution de la commande : {process_name}")
    start_time = time.time()
    process = subprocess.Popen(command, stderr=subprocess.PIPE, universal_newlines=True)

    total_duration = None
    last_progress = None

    while True:
        output = process.stderr.readline()
        if output == '' and process.poll() is not None:
            break
        if output:
            elapsed_time = time.time() - start_time

            # Extraire la durée totale (une seule fois)
            if total_duration is None:
                match_duration = re.search(r"Duration:\s+(\d+):(\d+):(\d+\.\d+)", output)
                if match_duration:
                    hours, minutes, seconds = map(float, match_duration.groups())
                    total_duration = hours * 3600 + minutes * 60 + seconds
                    logging.info(f"Durée totale détectée : {total_duration:.2f} secondes")

            # Extraire le temps courant
            match_time = re.search(r"out_time_ms=(\d+)", output)
            if match_time:
                current_time = int(match_time.group(1)) / 1_000_000  # Convertir microsecondes en secondes

                # Calcul de la progression
                if total_duration:
                    progress = current_time / total_duration
                    estimated_remaining_time = (elapsed_time / progress) - elapsed_time if progress > 0 else None

                    # Affichage dynamique
                    if last_progress != progress:  # Réduire les mises à jour inutiles
                        sys.stdout.write(
                            f"\r{process_name} - Progression: {progress * 100:.2f}% - Temps écoulé: {elapsed_time:.2f}s"
                        )
                        if estimated_remaining_time is not None:
                            sys.stdout.write(f" - Temps restant estimé: {estimated_remaining_time:.2f}s")
                        sys.stdout.flush()
                        last_progress = progress

    sys.stdout.write("\n")  # Ajouter un saut de ligne après la progression
    rc = process.poll()
    if rc != 0:
        logging.error(f"{process_name} a échoué avec le code {rc}")
        sys.exit(1)
    logging.info(f"{process_name} terminé avec succès")

def extract_audio(input_file, output_file):
    """Extrait la bande son d'une vidéo sans afficher de progression."""
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
    """Convertit un fichier audio en WAV sans afficher de progression."""
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

def transcribe_wav(wav_file, model):
    """Transcrit un fichier WAV avec Vosk sur le fichier entier."""
    logging.info(f"Début de la transcription du fichier WAV : {wav_file}")
    try:
        # Ouverture du fichier WAV
        wf = wave.open(wav_file, "rb")
        rec = KaldiRecognizer(model, wf.getframerate())

        # Lire tout le fichier à la fois
        data = wf.readframes(wf.getnframes())

        # Transcription de tout le fichier
        if rec.AcceptWaveform(data):
            results = json.loads(rec.Result())
        else:
            results = []

        # Ajouter le résultat final
        results.append(json.loads(rec.FinalResult()))

        # Fusionner tous les résultats en une transcription
        transcription = " ".join(result.get("text", "") for result in results)
        logging.info("Transcription terminée")
        return transcription.strip()

    except Exception as e:
        logging.error(f"Erreur lors de la transcription : {e}")
        sys.exit(1)


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

    model_path = "./vosk_model/selected"
    if not os.path.exists(model_path):
        logging.error(f"Modèle Vosk introuvable : {model_path}")
        sys.exit(1)
    try:
        model = Model(model_path)
    except Exception as e:
        logging.error(f"Erreur lors du chargement du modèle Vosk : {e}")
        sys.exit(1)

    transcription = transcribe_wav(wav_file, model)

    with open(output_transcription_file, "w") as f:
        f.write(transcription)
        logging.info(f"Transcription sauvegardée dans : {output_transcription_file}")

    os.remove(wav_file)
    logging.info("Fichier temporaire supprimé")
    logging.info("Script terminé avec succès")
