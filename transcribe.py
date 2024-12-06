import sys
import subprocess
import json
import wave
import os
from vosk import Model, KaldiRecognizer, SetLogLevel
import mimetypes

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

def extract_audio(input_file, output_file):
    """Extrait la bande son d'une vidéo."""
    try:
        subprocess.run([
            'ffmpeg', '-i', input_file, '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1', output_file,
            '-loglevel', 'quiet'
        ], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Erreur lors de l'extraction audio : {e}")
        sys.exit(1)

def convert_to_wav(input_file, output_file):
    """Convertit un fichier audio en WAV 16kHz mono si nécessaire."""
     # Si déjà en WAV, copie simplement le fichier
    if input_file.lower().endswith(".wav"):
        try:
            subprocess.run(['cp', input_file, output_file], check=True)
            return
        except subprocess.CalledProcessError as e:
            print(f"Erreur lors de la copie du fichier WAV : {e}")
            sys.exit(1)

    try:
        subprocess.run([
            'ffmpeg', '-i', input_file, '-ar', '16000', '-ac', '1', '-acodec', 'pcm_s16le', output_file,
            '-loglevel', 'quiet'
        ], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Erreur lors de la conversion en WAV : {e}")
        sys.exit(1)


def transcribe_wav(wav_file, model):
    """Transcrit un fichier WAV avec Vosk."""
    try:
        wf = wave.open(wav_file, "rb")
        rec = KaldiRecognizer(model, wf.getframerate())

        results = []
        while True:
            data = wf.readframes(4000)
            if len(data) == 0:
                break
            if rec.AcceptWaveform(data):
                results.append(json.loads(rec.Result()))

        results.append(json.loads(rec.FinalResult()))

        transcription = ""
        for result in results:
            transcription += result.get("text", "") + " "
        return transcription.strip()

    except Exception as e:
        print(f"Erreur lors de la transcription : {e}")
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

    print(f"Transcription sauvegardée dans : {output_transcription_file}")

    os.remove(wav_file)
