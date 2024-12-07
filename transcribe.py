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

def check_file_type(input_file):
    """Vérifie le type de fichier et retourne 'audio', 'video' ou None."""
    mime_type = mimetypes.guess_type(input_file)[0]
    if mime_type is None:
        return None  # Type de fichier inconnu

    if mime_type.startswith('audio/'):
        return 'audio'
    elif mime_type.startswith('video/'):
        return 'video'
    else:
        return None

def run_command_with_progress(command, process_name):
    """Exécute une commande et affiche la progression et le temps écoulé."""
    start_time = time.time()
    process = subprocess.Popen(command, stderr=subprocess.PIPE, universal_newlines=True)

    while True:
        output = process.stderr.readline()
        if output == '' and process.poll() is not None:
            break
        if output:
            elapsed_time = time.time() - start_time
            # Extraire le temps et la durée totale de la sortie ffmpeg
            match = re.search(r"time=(\d+:\d+:\d+\.\d+)", output)
            current_time_str = match.group(1) if match else None

            match = re.search(r"Duration:\s+(\d+:\d+:\d+\.\d+)", output)
            total_duration_str = match.group(1) if match else None

            progress_message = f"{process_name} - Temps écoulé: {elapsed_time:.2f}s"
            if current_time_str and total_duration_str:
                progress_message += f" - {current_time_str}/{total_duration_str}"
            logging.info(progress_message)

    rc = process.poll()
    if rc != 0:
        logging.error(f"{process_name} a échoué avec le code {rc}")
        sys.exit(1)

def extract_audio(input_file, output_file):
    """Extrait la bande son d'une vidéo avec affichage de la progression."""
    command = [
        'ffmpeg', '-i', input_file, '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1', output_file,
        '-loglevel', 'error', '-progress', 'pipe:2' # pour la progression
    ]
    run_command_with_progress(command, "Extraction audio")

def convert_to_wav(input_file, output_file):
    """Convertit un fichier audio en WAV avec affichage de la progression."""
    if input_file.lower().endswith(".wav"):
        try:
            subprocess.run(['cp', input_file, output_file], check=True)
            return
        except subprocess.CalledProcessError as e:
            logging.error(f"Erreur lors de la copie du fichier WAV : {e}")
            sys.exit(1)

    command = [
        'ffmpeg', '-i', input_file, '-ar', '16000', '-ac', '1', '-acodec', 'pcm_s16le', output_file,
        '-loglevel', 'error',  '-progress', 'pipe:2' # pour la progression
    ]
    run_command_with_progress(command, "Conversion en WAV")

def transcribe_wav(wav_file, model):
    """Transcrit un fichier WAV avec Vosk et affiche une barre de progression."""
    try:
        logging.info(f"Début de la transcription du fichier : {wav_file}")
        wf = wave.open(wav_file, "rb")
        rec = KaldiRecognizer(model, wf.getframerate())
        nframes = wf.getnframes()
        start_time = time.time()

        results = []
        processed_frames = 0

        while True:
            data = wf.readframes(4000)  # Taille du chunk audio
            if len(data) == 0:
                break

            if rec.AcceptWaveform(data):
                results.append(json.loads(rec.Result()))

            processed_frames += 4000 # Incrémente le nombre de frames traitées
            progress = int(processed_frames / nframes * 100)  # Calcul du pourcentage
            elapsed_time = time.time() - start_time
            logging.info(f"Transcription: {progress}% - Temps écoulé: {elapsed_time:.2f}s")

        results.append(json.loads(rec.FinalResult())) # final result

        transcription = ""
        for result in results:
            transcription += result.get("text", "") + " "
        logging.info(f"Transcription terminée : {transcription.strip()}")
        return transcription.strip()

    except Exception as e:
        logging.error(f"Erreur lors de la transcription : {e}")
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2 or len(sys.argv) > 3:
        print("Usage: python transcribe.py <fichier_entrée> [fichier_sortie]")
        sys.exit(1)

    input_file = sys.argv[1]
    output_transcription_file = sys.argv[2] if len(sys.argv) == 3 else "transcription.txt"

    file_type = check_file_type(input_file)
    if file_type is None:
        print("Erreur: Type de fichier non pris en charge. Utilisez un fichier audio ou vidéo.")
        sys.exit(1)

    wav_file = "temp.wav"
    if file_type == 'video':
        extract_audio(input_file, wav_file)
    elif file_type == 'audio':
        convert_to_wav(input_file, wav_file)

    model_path = "./vosk_model/selected"
    if not os.path.exists(model_path):
        print(f"Erreur : Le modèle Vosk n'existe pas : {model_path}")
        sys.exit(1)
    try:
        model = Model(model_path)
    except Exception as e:
        print(f"Erreur lors du chargement du modèle : {e}")
        sys.exit(1)

    transcription = transcribe_wav(wav_file, model)

    with open(output_transcription_file, "w") as f:
        f.write(transcription)

    logging.info(f"Fichier d'entrée : {input_file}")
    logging.info(f"Fichier de sortie : {output_transcription_file}")

    os.remove(wav_file)
