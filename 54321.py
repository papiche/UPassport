#!/usr/bin/env python3
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
# ~ from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, ValidationError
from typing import Optional
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
import time
from datetime import datetime

# Obtenir le timestamp Unix actuel
unix_timestamp = int(time.time())

# Configure le logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# ~ # Configurer CORS
# ~ app.add_middleware(
    # ~ CORSMiddleware,
    # ~ allow_origins=["*"],  # Permet toutes les origines
    # ~ allow_credentials=True,
    # ~ allow_methods=["*"],  # Permet toutes les méthodes
    # ~ allow_headers=["*"],  # Permet tous les headers
# ~ )

class MessageData(BaseModel):
    ulat: str
    ulon: str
    pubkey: str
    uid: str
    relation: str
    pubkeyUpassport: str
    email: str
    message: str

# chemin vers le modèle Vosk
model = Model("./vosk_model/selected")
logging.info(f"Vosk model loaded")

# Créez le dossier 'tmp' s'il n'existe pas
if not os.path.exists('tmp'):
    os.makedirs('tmp')

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
    file_location = f"tmp/{file.filename}"
    wav_file_location = f"tmp/converted_{file.filename}.wav"
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
    chunk_location = f"tmp/chunk_{file.filename}"
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

@app.post("/sendmsg")
async def send_message(
    ulat: str = Form(...),
    ulon: str = Form(...),
    pubkey: str = Form(...),
    uid: str = Form(...),
    relation: str = Form(...),
    pubkeyUpassport: str = Form(...),
    email: str = Form(default=""),
    message: str = Form(...)
):
    try:
        # Validation des données avec Pydantic
        message_data = MessageData(
            ulat=ulat,
            ulon=ulon,
            pubkey=pubkey,
            uid=uid,
            relation=relation,
            pubkeyUpassport=pubkeyUpassport,
            email=email,
            message=message
        )

        # Traitement
        result = await process_message(message_data)  # Utilisez await ici

        # Vérifiez le type de résultat retourné par process_message
        if isinstance(result, FileResponse):
            return result
        elif isinstance(result, dict) and "error" in result:
            return JSONResponse(
                status_code=500,
                content={"status": "error", "message": result["error"]}
            )
        else:
            # Retourner une réponse de succès
            return JSONResponse(
                status_code=200,
                content={"status": "success", "message": "Message sent successfully"}
            )

    except ValidationError as ve:
        # Gestion des erreurs de validation Pydantic
        logging.error(f"Validation error: {str(ve)}")
        return JSONResponse(
            status_code=422,
            content={"status": "error", "message": "Invalid input data", "details": ve.errors()}
        )

    except Exception as e:
        # Gestion des autres erreurs
        logging.error(f"Error processing message: {str(e)}")
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": "An unexpected error occurred"}
        )

async def process_message(message_data: MessageData):
    logging.info(f"Message from pubkeyUpassport: {message_data.pubkeyUpassport}")
    logging.info(f"To [N1] UID: {message_data.uid}")
    logging.info(f"Pubkey: {message_data.pubkey}")
    logging.info(f"Record to UPlanet GEOKEY : {message_data.ulat}:{message_data.ulon}")

    logging.info(f"Message: {message_data.message}")

    if message_data.email:
        logging.info(f"ZEROCARD+ : {message_data.email}")
    else:
        logging.info(f"No PLAYER email")

    script_path = "./command.sh"
    log_file_path = "./tmp/54321.log"

    # Préparer les arguments pour le script
    pubkey = message_data.pubkeyUpassport
    comment = message_data.message
    amount = "-1"  # API command
    date = str(unix_timestamp)  #  timestamp Unix
    zerocard = message_data.email if message_data.email else f"MEMBER:{message_data.pubkey}"

    async def run_script():
        process = await asyncio.create_subprocess_exec(
            script_path, pubkey, comment, amount, date, zerocard,
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

@app.post("/ssss")
async def ssss(request: Request):
    # Récupère les données du formulaire
    form_data = await request.form()

    # Extraire les valeurs des champs du formulaire
    cardns = form_data.get("cardns")
    ssss = form_data.get("ssss")
    zerocard = form_data.get("zerocard")

    logging.info(f"Received Card NS: {cardns}")
    logging.info(f"Received SSSS key: {ssss}")
    logging.info(f"ZEROCARD: {zerocard}")

    # Préparation des arguments pour l'exécution du script
    script_path = "./check_ssss.sh"
    log_file_path = "./tmp/54321.log"

    async def run_script():
        process = await asyncio.create_subprocess_exec(
            script_path, cardns, ssss, zerocard,
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

    # Exécuter le script et capturer la dernière ligne
    return_code, last_line = await run_script()

    # Déterminer si le script a réussi
    if return_code == 0:
        html_file_path = last_line.strip()  # Le chemin HTML est supposé être dans `last_line`
        # Retourner le fichier HTML généré en tant que réponse
        return FileResponse(html_file_path)
    else:
        return {"error": f"Une erreur s'est produite lors de l'exécution du script. Veuillez consulter les logs dans {log_file_path}."}



if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=54321)
