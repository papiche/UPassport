#!/usr/bin/env python3
import re
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, ValidationError
from dotenv import load_dotenv
from typing import Optional
import asyncio
import aiofiles
from vosk import Model, KaldiRecognizer
import wave
import json
import os
import logging
import base64
import subprocess
import magic
import time
import hashlib
from datetime import datetime
from urllib.parse import unquote, urlparse, parse_qs

# Obtenir le timestamp Unix actuel
unix_timestamp = int(time.time())

# Configure le logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = FastAPI()

templates = Jinja2Templates(directory="templates")

# ~ # Configurer CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins or restrict
    # ~ allow_origins=["https://ipfs.astroport.com", "https://u.astroport.com"],
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

# Charger les variables d'environnement depuis le fichier .env
load_dotenv()

# Récupérer la valeur de OBSkey depuis l'environnement
OBSkey = os.getenv("OBSkey")

def is_obs_running():
    """Vérifie si OBS Studio est en cours d'exécution."""
    process = subprocess.run(['pgrep', 'obs'], capture_output=True, text=True)
    return process.returncode == 0

def start_obs():
    """Démarre OBS Studio."""
    try:
        # Essayez de lancer OBS Studio en utilisant sa commande standard
        subprocess.Popen(['obs'])
    except FileNotFoundError:
        try:
            # Si la commande 'obs' ne fonctionne pas, essayez le chemin complet
            subprocess.Popen(['/usr/bin/obs'])
        except FileNotFoundError:
            print("OBS Studio n'a pas pu être trouvé. Assurez-vous qu'il est installé et que son chemin est correct.")

class QRCodeData(BaseModel):
    qrcode: str
    passphrase: str

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

async def run_script(script_path, *args, log_file_path="./tmp/54321.log"):
    """
    Fonction générique pour exécuter des scripts shell avec gestion des logs

    Args:
        script_path (str): Chemin du script à exécuter
        *args: Arguments à passer au script
        log_file_path (str): Chemin du fichier de log

    Returns:
        tuple: Code de retour et dernière ligne de sortie
    """
    logging.info(f"Running script: {script_path} with args: {args}")

    process = await asyncio.create_subprocess_exec(
        script_path, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )

    last_line = ""
    async with aiofiles.open(log_file_path, "a") as log_file:
        async for line in process.stdout:
            line = line.decode().strip()
            last_line = line
            await log_file.write(line + "\n")
            logging.info(f"Script output: {line}")

    return_code = await process.wait()
    logging.info(f"Script finished with return code: {return_code}")

    return return_code, last_line

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

## DEFAULT = MULTIPASS QR CODE SCANNER
@app.get("/")
async def get_root(request: Request):
    return templates.TemplateResponse("scan_new.html", {"request": request})

## CHECK G1PUB BALANCE
def check_balance(g1pub):
    result = subprocess.run(["tools/COINScheck.sh", g1pub], capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError("Erreur dans COINScheck.sh: " + result.stderr)
    balance_line = result.stdout.strip().splitlines()[-1]
    return balance_line

@app.get("/check_balance")
async def check_balance_route(g1pub: str):
    try:
        balance = check_balance(g1pub)
        return {"balance": balance}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

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

@app.post("/upassport")
async def scan_qr(parametre: str = Form(...), imageData: str = Form(None)):
    image_dir = "./tmp"

    # Ensure the image directory exists
    os.makedirs(image_dir, exist_ok=True)

    # Vérification si imageData est un PIN de 4 chiffres
    if imageData and imageData.isdigit() and len(imageData) == 4:
        logging.info(f"Received a PIN: {imageData}")
        image_path = imageData
    else:
        # Génération du nom de fichier à partir du hash de parametre
        image_filename = f"qr_image_{hashlib.sha256(parametre.encode()).hexdigest()[:10]}.png"
        image_path = os.path.join(image_dir, image_filename)

        if imageData:
            try:
                # Remove the data URL prefix if present
                if ',' in imageData:
                    image_data = imageData.split(',')[1]
                else:
                    image_data = imageData

                # Decode and save the image
                with open(image_path, "wb") as image_file:
                    image_file.write(base64.b64decode(image_data))
                    logging.info("Saved image to: %s", image_path)

            except Exception as e:
                logging.error("Error saving image: %s", e)

    ## Running External Script > get last line > send file content back to client.
    script_path = "./upassport.sh"
    return_code, last_line = await run_script(script_path, parametre, image_path)

    if return_code == 0:
        returned_file_path = last_line.strip()
        logging.info(f"Returning file: {returned_file_path}")
        return FileResponse(returned_file_path)
    else:
        error_message = f"Une erreur s'est produite lors de l'exécution du script. Veuillez consulter les logs."
        logging.error(error_message)
        return {"error": error_message}

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

        # Traitement du message
        result = await process_message(message_data)

        # Vérifiez le type de résultat retourné par process_message
        if isinstance(result, FileResponse):
            async with aiofiles.open(result.file, "rb") as file:
                file_content = await file.read()
            return JSONResponse(content={"status": "success", "file_path": str(result.file), "file_content": file_content.decode('utf-8')})
        elif isinstance(result, dict) and "error" in result:
            raise HTTPException(status_code=500, detail={"status": "error", "message": result["error"]})
        else:
            return JSONResponse(content={"status": "success", "message": "Message sent successfully"})
    except Exception as e:
        logging.error(f"Error processing message: {e}")
        raise HTTPException(status_code=500, detail={"status": "error", "message": str(e)})

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
    zlat = message_data.ulat
    zlon = message_data.ulon
    pubkey = message_data.pubkeyUpassport
    comment = message_data.message
    amount = "-1"  # API command
    date = str(unix_timestamp)  #  timestamp Unix
    zerocard = message_data.email if message_data.email else f"MEMBER:{message_data.pubkey}" # send email OR actual N1 App MEMBER G1PUB

    return_code, last_line = await run_script(script_path, pubkey, comment, amount, date, zerocard, zlat, zlon, log_file_path=log_file_path)

    if return_code == 0:
        returned_file_path = last_line.strip()
        logging.info(f"Returning file: {returned_file_path}")
        return FileResponse(returned_file_path)
    else:
        return {"error": f"Une erreur s'est produite lors de l'exécution du script. Veuillez consulter les logs dans {log_file_path}."}

###############################################################################
## Collect UPassport SSSS KEY and match ot with CAPTAIN parts or SWARM key copy
## Can also receive DRIVE KEY IPNS httt.../12D
##################################################./check_ssss.sh #############
@app.post("/ssss")
async def ssss(request: Request):
    form_data = await request.form()
    cardns = form_data.get("cardns")
    ssss = form_data.get("ssss")
    zerocard = form_data.get("zerocard")

    logging.info(f"Received Card NS: {cardns}")
    logging.info(f"Received SSSS key: {ssss}")
    logging.info(f"ZEROCARD: {zerocard}")

    script_path = "./check_ssss.sh"
    return_code, last_line = await run_script(script_path, cardns, ssss, zerocard)

    if return_code == 0:
        returned_file_path = last_line.strip()
        return FileResponse(returned_file_path)
    else:
        return {"error": f"Une erreur s'est produite lors de l'exécution du script. Veuillez consulter les logs."}

@app.post("/zen_send")
async def zen_send(request: Request):
    form_data = await request.form()
    zen = form_data.get("zen")
    g1source = form_data.get("g1source")
    g1dest = form_data.get("g1dest")

    logging.info(f"Zen Amount : {zen}")
    logging.info(f"Source : {g1source}")
    logging.info(f"Destination : {g1dest}")

    script_path = "./zen_send.sh"
    return_code, last_line = await run_script(script_path, zen, g1source, g1dest)

    if return_code == 0:
        returned_file_path = last_line.strip()
        return FileResponse(returned_file_path)
    else:
        return {"error": f"Une erreur s'est produite lors de l'exécution du script. Veuillez consulter les logs dans ./tmp/54321.log."}

###################################################
######### REC / STOP - NODE OBS STUDIO -
# Store the OBS Studio recording process object
recording_process = None

@app.get("/rec")
async def start_recording(player: Optional[str] = None):
    global recording_process
    if recording_process:
        raise HTTPException(status_code=400, detail="Recording is already in progress.")

    if not player:
        return {"message": "No player provided. What is your email?"}

    if not re.match(r"[^@]+@[^@]+\.[^@]+", player):
        raise HTTPException(status_code=400, detail="Invalid email address provided.")

    script_path = "./startrec.sh"
    return_code, last_line = await run_script(script_path, player)

    if return_code == 0:
        obsws_url = f"obsws://127.0.0.1:4455/{OBSkey}"
        getlog = subprocess.run(
            ["obs-cmd", "--websocket", obsws_url, "recording", "start"],
            capture_output=True, text=True
        )

        if getlog.returncode == 0:
            recording_process = True
            return {"message": "Recording started successfully.", "player_info": last_line.strip(), "obs_output": getlog.stdout.strip()}
        else:
            return {"error": f"Failed to start OBS recording. Error: {getlog.stderr.strip()}"}
    else:
        return {"error": f"Script execution failed. {last_line.strip()}"}

@app.get("/stop")
async def stop_recording(player: Optional[str] = None):
    global recording_process
    if not recording_process:
        raise HTTPException(status_code=400, detail="No recording in progress to stop.")

    if not player:
        return {"message": "No player provided. Recording not stopped."}

    if not re.match(r"[^@]+@[^@]+\.[^@]+", player):
        raise HTTPException(status_code=400, detail="Invalid email address provided.")

    script_path = "./stoprec.sh"
    return_code, last_line = await run_script(script_path, player)

    if return_code == 0:
            recording_process = None
            return {"message": "Recording stopped successfully.", "player": player, "info": last_line.strip(), "debug": getlog.stdout.strip()}
        else:
            return {"error": f"Failed to stop OBS recording. Error: {last_line.strip()}"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=54321)
