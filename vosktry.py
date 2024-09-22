import sys
import wave
import json
import logging
import subprocess
from vosk import Model, KaldiRecognizer

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def convert_to_required_format(input_path, output_path):
    logging.info(f"Conversion du fichier audio au format requis")
    command = [
        'ffmpeg', '-i', input_path,
        '-acodec', 'pcm_s16le',
        '-ac', '1',
        '-ar', '16000',
        output_path
    ]
    subprocess.run(command, check=True)
    logging.info(f"Conversion terminée : {output_path}")

def transcribe_wav(wav_path):
    logging.info(f"Début de la transcription du fichier: {wav_path}")

    try:
        # Conversion du fichier au format requis
        converted_path = wav_path + ".converted.wav"
        convert_to_required_format(wav_path, converted_path)

        # Chargement du modèle Vosk
        model_path = "./model/vosk-model-small-fr-0.22"
        logging.info(f"Chargement du modèle Vosk depuis: {model_path}")
        model = Model(model_path)
        
        # Ouverture du fichier WAV converti
        logging.info("Ouverture du fichier WAV converti")
        wf = wave.open(converted_path, "rb")
        
        # Création du recognizer
        rec = KaldiRecognizer(model, wf.getframerate())
        rec.SetWords(True)

        # Transcription
        logging.info("Début de la transcription")
        result = ""
        while True:
            data = wf.readframes(4000)
            if len(data) == 0:
                break
            if rec.AcceptWaveform(data):
                part_result = json.loads(rec.Result())
                result += part_result['text'] + " "
                logging.debug(f"Résultat partiel: {part_result['text']}")

        part_result = json.loads(rec.FinalResult())
        result += part_result['text']
        
        logging.info("Transcription terminée")
        logging.info(f"Texte final: {result}")
        
        return result

    except Exception as e:
        logging.error(f"Une erreur s'est produite: {str(e)}")
        return None

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python script.py <chemin_vers_fichier.wav>")
        sys.exit(1)

    wav_path = sys.argv[1]
    transcribed_text = transcribe_wav(wav_path)
    
    if transcribed_text:
        print("Texte transcrit:")
        print(transcribed_text)
    else:
        print("La transcription a échoué. Vérifiez les logs pour plus de détails.")

