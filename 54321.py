from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import asyncio
import aiofiles
from vosk import Model, KaldiRecognizer
import wave
import json
import os
import logging
import subprocess
import magic

# Configure le logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Assurez-vous que le chemin vers votre modèle Vosk est correct
model = Model("./model/vosk-model-small-fr-0.22")
logging.info(f"Vosk model loaded")

# Créez le dossier 'files' s'il n'existe pas
if not os.path.exists('files'):
    os.makedirs('files')

def convert_to_wav(input_file, output_file):
    command = [
        'ffmpeg',
        '-f', 'webm',  # Explicitly specify input format
        '-i', input_file,
        '-acodec', 'pcm_s16le',
        '-ac', '1',
        '-ar', '16000',
        '-f', 'wav',
        output_file
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        logging.info(f"File converted to WAV: {output_file}")
        logging.debug(f"FFmpeg output: {result.stdout}")
    except subprocess.CalledProcessError as e:
        logging.error(f"FFmpeg conversion failed: {e}")
        logging.error(f"FFmpeg error output: {e.stderr}")
        raise

@app.get("/")
async def get_root(request: Request):
    return templates.TemplateResponse("scan_new.html", {"request": request})

@app.get("/voice")
async def get_vosk(request: Request):
    return templates.TemplateResponse("vosk.html", {"request": request})

@app.post("/transcribe")
async def transcribe_audio(file: UploadFile = File(...)):
    file_location = f"files/{file.filename}"
    wav_file_location = f"files/converted_{file.filename}.wav"
    try:
        logging.info(f"Début de la transcription pour le fichier: {file.filename}")

        # Sauvegarder le fichier uploadé
        async with aiofiles.open(file_location, 'wb') as out_file:
            content = await file.read()
            await out_file.write(content)
        logging.debug(f"Fichier sauvegardé: {file_location}")

        # Convertir le fichier en WAV avec ffmpeg
        convert_to_wav(file_location, wav_file_location)

        # Ouvrir le fichier WAV converti
        with wave.open(wav_file_location, "rb") as wf:
            logging.debug(f"Fichier WAV ouvert: {wav_file_location}")
            # Créer un recognizer
            rec = KaldiRecognizer(model, wf.getframerate())
            logging.debug(f"Recognizer créé avec le taux d'échantillonnage: {wf.getframerate()}")

            # Lire et transcrire l'audio
            result = ""
            while True:
                data = wf.readframes(4000)
                if len(data) == 0:
                    logging.debug("Fin de la lecture du fichier audio")
                    break
                if rec.AcceptWaveform(data):
                    part_result = json.loads(rec.Result())
                    result += part_result['text'] + " "
                    logging.debug(f"Résultat partiel: {part_result['text']}")

            # Obtenir le résultat final
            part_result = json.loads(rec.FinalResult())
            result += part_result['text']
            logging.info(f"Transcription terminée. Résultat final: {result}")

        return JSONResponse(content={"transcription": result})

    except Exception as e:
        logging.error(f"Erreur lors de la transcription: {str(e)}", exc_info=True)
        return JSONResponse(content={"error": str(e)}, status_code=500)
    finally:
        # Supprimer les fichiers temporaires
        if os.path.exists(file_location):
            os.remove(file_location)
            logging.debug(f"Fichier temporaire supprimé: {file_location}")
        if os.path.exists(wav_file_location):
            os.remove(wav_file_location)
            logging.debug(f"Fichier WAV temporaire supprimé: {wav_file_location}")

@app.post("/transcribe_chunk")
async def transcribe_chunk(file: UploadFile = File(...)):
    chunk_location = f"files/chunk_{file.filename}"
    wav_chunk_location = f"{chunk_location}_converted.wav"
    try:
        logging.info(f"Received chunk: {file.filename}")

        # Sauvegarder le chunk audio
        async with aiofiles.open(chunk_location, 'wb') as out_file:
            content = await file.read()
            await out_file.write(content)
        logging.debug(f"Chunk saved: {chunk_location}")

        # Afficher les premiers octets du fichier
        with open(chunk_location, 'rb') as f:
            header = f.read(32)
        logging.info(f"File header (hex): {header.hex()}")

        # Vérifier la taille du fichier
        file_size = os.path.getsize(chunk_location)
        if file_size == 0:
            raise ValueError("Received empty file")
        logging.info(f"File size: {file_size} bytes")

        # Vérifier le format du fichier
        mime = magic.Magic(mime=True)
        file_type = mime.from_file(chunk_location)
        logging.info(f"Detected file type: {file_type}")

        # Convertir le chunk en WAV mono 16 bits 16kHz
        try:
            convert_to_wav(chunk_location, wav_chunk_location)
        except subprocess.CalledProcessError:
            return JSONResponse(content={"error": "Failed to convert audio chunk"}, status_code=500)

        # Transcrire le chunk
        with wave.open(wav_chunk_location, "rb") as wf:
            logging.debug(f"Opened WAV file for transcription: {wav_chunk_location}")
            rec = KaldiRecognizer(model, wf.getframerate())

            while True:
                data = wf.readframes(4000)
                if len(data) == 0:
                    break
                if rec.AcceptWaveform(data):
                    result = json.loads(rec.Result())
                    logging.info(f"Transcription result: {result.get('text', '')}")
                    return JSONResponse(content={"transcription": result.get('text', '')})

            result = json.loads(rec.FinalResult())
            logging.info(f"Final transcription result: {result.get('text', '')}")
            return JSONResponse(content={"transcription": result.get('text', '')})

    except Exception as e:
        logging.error(f"Error during chunk transcription: {str(e)}", exc_info=True)
        return JSONResponse(content={"error": str(e)}, status_code=500)
    finally:
        # Supprimer les fichiers temporaires
        if os.path.exists(chunk_location):
            os.remove(chunk_location)
            logging.debug(f"Temporary file removed: {chunk_location}")
        if os.path.exists(wav_chunk_location):
            os.remove(wav_chunk_location)
            logging.debug(f"Temporary WAV file removed: {wav_chunk_location}")

@app.post("/transcribe_chunk")
async def transcribe_chunk(file: UploadFile = File(...)):
    chunk_location = f"files/chunk_{file.filename}"
    wav_chunk_location = f"{chunk_location}_converted.wav"
    try:
        logging.info(f"Received chunk: {file.filename}")

        # Sauvegarder le chunk audio
        async with aiofiles.open(chunk_location, 'wb') as out_file:
            content = await file.read()
            await out_file.write(content)
        logging.debug(f"Chunk saved: {chunk_location}")

        # Convertir le chunk en WAV mono 16 bits 16kHz
        try:
            convert_to_wav(chunk_location, wav_chunk_location)
        except subprocess.CalledProcessError:
            return JSONResponse(content={"error": "Failed to convert audio chunk"}, status_code=500)

        # Transcrire le chunk
        with wave.open(wav_chunk_location, "rb") as wf:
            logging.debug(f"Opened WAV file for transcription: {wav_chunk_location}")
            rec = KaldiRecognizer(model, wf.getframerate())

            while True:
                data = wf.readframes(4000)
                if len(data) == 0:
                    break
                if rec.AcceptWaveform(data):
                    result = json.loads(rec.Result())
                    logging.info(f"Transcription result: {result.get('text', '')}")
                    return JSONResponse(content={"transcription": result.get('text', '')})

            result = json.loads(rec.FinalResult())
            logging.info(f"Final transcription result: {result.get('text', '')}")
            return JSONResponse(content={"transcription": result.get('text', '')})

    except Exception as e:
        logging.error(f"Error during chunk transcription: {str(e)}", exc_info=True)
        return JSONResponse(content={"error": str(e)}, status_code=500)
    finally:
        # Supprimer les fichiers temporaires
        if os.path.exists(chunk_location):
            os.remove(chunk_location)
            logging.debug(f"Temporary file removed: {chunk_location}")
        if os.path.exists(wav_chunk_location):
            os.remove(wav_chunk_location)
            logging.debug(f"Temporary WAV file removed: {wav_chunk_location}")

@app.post("/upassport")
async def scan_qr(parametre: str = Form(...)):
    script_path = "./upassport.sh"
    log_file_path = "./tmp/54321.log"

    async def run_script():
        process = await asyncio.create_subprocess_exec(
            script_path, parametre,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )

        last_line = ""
        async with aiofiles.open(log_file_path, "a") as log_file:
            async for line in process.stdout:
                line = line.decode().strip()
                last_line = line
                await log_file.write(line + "\n")
                print(line)

        return_code = await process.wait()
        return return_code, last_line

    return_code, last_line = await run_script()

    if return_code == 0:
        html_file_path = last_line.strip()
        return FileResponse(html_file_path)
    else:
        return {"error": f"Une erreur s'est produite lors de l'exécution du script. Veuillez consulter les logs dans {log_file_path}."}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=54321)
