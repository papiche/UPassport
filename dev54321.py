#!/usr/bin/env python3
import re
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, ValidationError
from dotenv import load_dotenv
from typing import Optional
import asyncio
import aiofiles
import whisper
import torch
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
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

# Charger les variables d'environnement depuis le fichier .env
load_dotenv()
# Récupérer la valeur de OBSkey depuis l'environnement
OBSkey = os.getenv("OBSkey")

app = FastAPI()
# Mount the directory containing static files
app.mount("/static", StaticFiles(directory="static"), name="static")
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

class UserAnswers(BaseModel):
    os: str
    smartphone: str
    storage: str
    data_concern: str
    nextcloud_knowledge: str
    ai_usage: str
    ollama_knowledge: str
    data_management_time: str
    community_importance: str
    tech_interest: str

user_answers = UserAnswers(
    os="", smartphone="", storage="", data_concern="",
    nextcloud_knowledge="", ai_usage="", ollama_knowledge="",
    data_management_time="", community_importance="", tech_interest=""
)

slides = [
{"title": "Made In Zen : Prenez le Contrôle de Vos Données", "content": "Bienvenue ! Made In Zen vous offre une solution unique pour sécuriser vos données et participer à un projet innovant."},
{"title": "Le Problème", "content": "Vous utilisez {os}, et vous êtes préoccupé par {data_concern}. Made In Zen vous apporte la solution."},
{"title": "La Solution : Made In Zen", "content": "Made In Zen combine la sécurité de la blockchain, la puissance du cloud Nextcloud et l'innovation de l'IA pour une gestion optimale de vos données."},
{"title": "Nextcloud Sécurisé", "content": "{nextcloud_content}"},
{"title": "IA Intégrée", "content": "{ai_content}"},
{"title": "Ollama et Made In Zen", "content": "{ollama_content}"},
{"title": "Sécurité et Confidentialité", "content": "Made In Zen assure la sécurité et la confidentialité de vos données grâce à la technologie blockchain et des protocoles de sécurité robustes."},
{"title": "Gouvernance Participative", "content": "Participez à la gouvernance de Made In Zen ! Votre voix compte."},
{"title": "Le Modèle de Redistribution 3 x 1/3", "content": "Un modèle de redistribution équitable et transparent qui soutient l'innovation et la communauté open source."},
{"title": "Le Ẑen : Une Monnaie Numérique Sécurisée", "content": "Utilisez le Ẑen, notre monnaie numérique sécurisée, pour des transactions fluides et transparentes."},
{"title": "Optimisation du Stockage", "content": "Gérez efficacement votre espace de stockage avec Made In Zen, adapté à vos besoins spécifiques."},
{"title": "Collaboration en Temps Réel", "content": "Travaillez ensemble sur vos projets, où que vous soyez, grâce à nos outils de collaboration intégrés."},
{"title": "Automatisation des Tâches", "content": "Simplifiez votre quotidien grâce à l'automatisation intelligente des tâches répétitives."},
{"title": "Intégration avec vos Outils Favoris", "content": "Made In Zen s'intègre parfaitement avec vos applications préférées pour une expérience fluide."},
{"title": "Support Communautaire", "content": "Bénéficiez de l'aide et des conseils de notre communauté active et bienveillante."},
{"title": "Témoignages Clients", "content": "Découvrez ce que nos utilisateurs pensent de Made In Zen et comment cela a transformé leur gestion de données."},
{"title": "Formation et Ressources", "content": "Accédez à nos formations et ressources pour tirer le meilleur parti de Made In Zen."},
{"title": "Tarification Transparente", "content": "Des offres claires et sans surprise, adaptées à vos besoins et à votre budget."},
{"title": "Rejoignez-nous", "content": "Inscrivez-vous dès aujourd'hui et commencez à prendre le contrôle de vos données avec Made In Zen."},
{"title": "Contactez-nous", "content": "Des questions ? Notre équipe est là pour vous aider. Contactez-nous pour plus d'informations."}
]


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
    # Vérifier si le fichier est déjà au format WAV
    if input_file.lower().endswith('.wav'):
        # Si c'est le cas, copiez simplement le fichier
        command = ['cp', input_file, output_file]
    else:
        # Sinon, procédez à la conversion
        command = [
            'ffmpeg',
            '-i', input_file,
            '-acodec', 'pcm_s16le',
            '-ac', '1',
            '-ar', '16000',
            '-f', 'wav',
            output_file
        ]

    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        logging.info(f"File processed: {output_file}")
        logging.debug(f"Command output: {result.stdout}")
    except subprocess.CalledProcessError as e:
        logging.error(f"File processing failed: {e}")
        logging.error(f"Error output: {e.stderr}")
        raise


## DEFAULT = UPlanet Status
@app.get("/")
async def ustats(request: Request):

    script_path = os.path.expanduser("~/.zen/Astroport.ONE/Ustats.sh")
    return_code, last_line = await run_script(script_path)

    if return_code == 0:
        returned_file_path = last_line.strip()
        return FileResponse(returned_file_path)
    else:
        return {"error": f"Une erreur s'est produite lors de l'exécution du script. Veuillez consulter les logs dans ./tmp/54321.log."}

@app.get("/enter")
async def questionnaire(request: Request):
    return templates.TemplateResponse("questionnaire.html", {"request": request})

@app.post("/submit_questionnaire")
async def submit_questionnaire(
    request: Request,
    os: str = Form(...),
    smartphone: str = Form(...),
    storage: str = Form(...),
    data_concern: str = Form(...),
    nextcloud_knowledge: str = Form(...),
    ai_usage: str = Form(...),
    ollama_knowledge: str = Form(...),
    data_management_time: str = Form(...),
    community_importance: str = Form(...),
    tech_interest: str = Form(...)
):
    global user_answers
    user_answers = UserAnswers(
        os=os, smartphone=smartphone, storage=storage, data_concern=data_concern,
        nextcloud_knowledge=nextcloud_knowledge, ai_usage=ai_usage, ollama_knowledge=ollama_knowledge,
        data_management_time=data_management_time, community_importance=community_importance, tech_interest=tech_interest
    )
    return {"redirect": "/presentation/1"}

def get_slide_image(slide_id: int) -> str:
    """Determine which image variation to use based on user answers."""
    if slide_id == 1:
        return f"images/slide_{slide_id:02d}.png"  # Default image for slide 1
    elif slide_id == 2:
        if user_answers.os == "Linux":
             return f"images/slide_{slide_id:02d}_a.png"
        else :
             return f"images/slide_{slide_id:02d}_b.png"
    elif slide_id == 3:
        return f"images/slide_{slide_id:02d}.png"  # Default for slide 3
    elif slide_id == 4:
        if user_answers.nextcloud_knowledge == "Oui":
            return f"images/slide_{slide_id:02d}_a.png"  # User knows Nextcloud
        else:
             return f"images/slide_{slide_id:02d}_b.png"  # User doesn't know Nextcloud
    elif slide_id == 5:
         if user_answers.ai_usage == "Oui":
            return f"images/slide_{slide_id:02d}_a.png"
         else:
            return f"images/slide_{slide_id:02d}_b.png"
    elif slide_id == 6:
        if user_answers.ollama_knowledge == "Oui":
            return f"images/slide_{slide_id:02d}_a.png"  # User knows Ollama
        else:
            return f"images/slide_{slide_id:02d}_b.png" # User doesn't know Ollama
    elif slide_id >= 7 and slide_id <= 20:
         return f"images/slide_{slide_id:02d}.png" # default
    return f"images/slide_{slide_id:02d}.png" # default


@app.get("/presentation/{slide_id}")
async def get_slide(request: Request, slide_id: int):
    if slide_id < 1 or slide_id > len(slides):
        return {"error": "Slide not found"}

    slide = slides[slide_id - 1]

    if slide_id == 2:
        slide["content"] = slide["content"].format(os=user_answers.os, data_concern=user_answers.data_concern)
    elif slide_id == 4:
        slide["content"] = "Découvrez Nextcloud, une solution de stockage cloud sécurisée et open source." if user_answers.nextcloud_knowledge == "Non" else "Améliorez votre expérience Nextcloud grâce à la sécurité de Made In Zen."
    elif slide_id == 5:
        slide["content"] = "Intégrez vos services IA préférés à votre espace de stockage sécurisé." if user_answers.ai_usage == "Oui" else "Découvrez les possibilités de l'IA dans un environnement sécurisé avec Made In Zen."
    elif slide_id == 6:
        slide["content"] = "Optimisez votre workflow avec Ollama et l'espace de stockage sécurisé de Made In Zen." if user_answers.ollama_knowledge == "Oui" else "Découvrez Ollama et les services d'IA avancés, accessibles en toute sécurité via Made In Zen."

    image_path = get_slide_image(slide_id)

    return templates.TemplateResponse("slide.html", {"request": request, "slide": slide, "slide_id": slide_id, "total_slides": len(slides), "image_path": image_path})

@app.get("/scan")
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

def format_time(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    seconds = int(seconds % 60)
    milliseconds = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"

# ~ @app.post("/transcribe")
# ~ async def transcribe_audio(file: UploadFile = File(...)):
    # ~ file_location = f"tmp/{file.filename}"
    # ~ wav_file_location = f"tmp/converted_{file.filename}.wav"
    # ~ try:
        # ~ logging.info(f"Début de la transcription pour le fichier: {file.filename}")

        # ~ # Sauvegarder le fichier uploadé
        # ~ async with aiofiles.open(file_location, 'wb') as out_file:
            # ~ content = await file.read()
            # ~ await out_file.write(content)
        # ~ logging.debug(f"Fichier sauvegardé: {file_location}")

         # ~ # Extraire le flux audio avec ffmpeg directement
        # ~ logging.info(f"Extraction du flux audio depuis: {file_location} -> {wav_file_location}")
        # ~ command = [
            # ~ 'ffmpeg',
            # ~ '-i', file_location,
            # ~ '-vn', # disable video
            # ~ '-acodec', 'pcm_s16le',
            # ~ '-ac', '1',
            # ~ '-ar', '16000',
            # ~ '-f', 'wav',
            # ~ wav_file_location
        # ~ ]
        # ~ try:
            # ~ result = subprocess.run(command, check=True, capture_output=True, text=True)
            # ~ logging.info(f"File processed: {wav_file_location}")
            # ~ logging.debug(f"Command output: {result.stdout}")
        # ~ except subprocess.CalledProcessError as e:
            # ~ logging.error(f"File processing failed: {e}")
            # ~ logging.error(f"Error output: {e.stderr}")
            # ~ raise

        # ~ audio_file = wav_file_location
        # ~ logging.debug(f"Fichier audio: {audio_file}")

        # ~ # Ouvrir le fichier WAV converti
        # ~ with wave.open(audio_file, "rb") as wf:
            # ~ logging.debug(f"Fichier WAV ouvert: {audio_file}")

            # ~ num_channels = wf.getnchannels()
            # ~ sample_rate = wf.getframerate()
            # ~ logging.debug(f"Fichier WAV: Canaux={num_channels}, Fréquence={sample_rate}")

            # ~ # Vérifier si le fichier WAV est valide
            # ~ if num_channels != 1 or sample_rate != 16000:
                  # ~ error_message = f"Fichier WAV invalide: Canaux={num_channels}, Fréquence={sample_rate}"
                  # ~ logging.error(error_message)
                  # ~ return HTMLResponse(error_message, status_code=500)

            # ~ # Créer un recognizer avec des options
            # ~ rec = KaldiRecognizer(model, wf.getframerate())
            # ~ logging.debug(f"Recognizer créé avec le taux d'échantillonnage: {wf.getframerate()}")

           # ~ # Lire toutes les données du fichier en une seule fois
            # ~ data = wf.readframes(wf.getnframes())
            # ~ logging.debug(f"Lecture de {len(data)} octets du fichier.")

            # ~ # Effectuer la reconnaissance sur les données complètes
            # ~ if rec.AcceptWaveform(data):
                # ~ full_result = json.loads(rec.Result())
                # ~ logging.debug(f"Résultat de Vosk (full): {full_result}")
                # ~ if full_result and full_result.get('text'):
                    # ~ transcription = full_result['text'].strip()
                    # ~ logging.debug(f"Transcription: {transcription}")
                    # ~ # Instead of JSON, return a simple string
                    # ~ return HTMLResponse(transcription)
                # ~ else:
                    # ~ error_message = f"Pas de texte dans le résultat de Vosk"
                    # ~ logging.debug(error_message)
                    # ~ return HTMLResponse(error_message)
            # ~ else:
                 # ~ error_message = f"Erreur lors de l'acceptation du waveform"
                 # ~ logging.debug(error_message)
                 # ~ return HTMLResponse(error_message)

    # ~ except Exception as e:
        # ~ logging.error(f"Erreur lors de la transcription: {str(e)}", exc_info=True)
        # ~ return JSONResponse(content={"error": str(e)}, status_code=500)
    # ~ finally:
        # ~ # Supprimer les fichiers temporaires
        # ~ if os.path.exists(file_location):
            # ~ #os.remove(file_location)
            # ~ logging.debug(f"Fichier temporaire supprimé: {file_location}")
        # ~ if os.path.exists(audio_file):
           # ~ # os.remove(audio_file)
            # ~ logging.debug(f"Fichier WAV temporaire supprimé: {wav_file_location}")

@app.post("/transcribe")
async def transcribe_audio(file: UploadFile = File(...)):
    file_location = f"tmp/{file.filename}"
    try:
        logging.info(f"Début de la transcription pour le fichier: {file.filename}")

        # Sauvegarder le fichier uploadé
        async with aiofiles.open(file_location, 'wb') as out_file:
            content = await file.read()
            await out_file.write(content)
        logging.debug(f"Fichier sauvegardé: {file_location}")

        # Déterminer le type du fichier (pour la conversion si necessaire)
        mime = magic.Magic(mime=True)
        file_type = mime.from_file(file_location)

        # Convertir en wav si le fichier n'est pas deja au bon format
        if file_type != 'audio/x-wav':
            wav_file_location = f"tmp/converted_{file.filename}.wav"
            convert_to_wav(file_location, wav_file_location)
            audio_file = wav_file_location
        else :
          audio_file = file_location

        # Charger le modèle Whisper
        device = "cuda" if torch.cuda.is_available() else "cpu"
        logging.info(f"Using device: {device}")
        model = whisper.load_model("medium", device=device)  # Vous pouvez choisir un modèle plus grand pour une meilleure précision

        # Transcrire l'audio
        logging.info(f"whisper Transcribing file: {audio_file}")
        result = model.transcribe(audio_file)

        # Formatter les segments de transcription pour correspondre à l'ancien format
        formatted_result = []
        for segment in result['segments']:
            formatted_result.append({
                'start': format_time(segment['start']),
                'end': format_time(segment['end']),
                'text': segment['text'].strip()
            })

        return JSONResponse(content={"transcription": formatted_result})

    except Exception as e:
        logging.error(f"Erreur lors de la transcription: {str(e)}", exc_info=True)
        return JSONResponse(content={"error": str(e)}, status_code=500)
    finally:
        # Supprimer les fichiers temporaires
        if os.path.exists(file_location):
           # os.remove(file_location)
            logging.debug(f"Fichier temporaire supprimé: {file_location}")
        if os.path.exists(audio_file) and audio_file != file_location :
           # os.remove(audio_file)
            logging.debug(f"Fichier WAV temporaire supprimé: {audio_file}")

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
current_player = None # Pour stocker l'email
@app.get("/rec", response_class=HTMLResponse)
async def rec_form(request: Request):
    """Displays the HTML form to input the email."""
    return templates.TemplateResponse("rec_form.html", {"request": request, "recording": False})

@app.post("/rec", response_class=HTMLResponse)
async def start_recording(request: Request, player: str = Form(...), link: str = Form(default=""), file: UploadFile = File(None)):
    global recording_process, current_player
    if recording_process:
        return templates.TemplateResponse("rec_form.html", {"request": request, "error": "Recording is already in progress.", "recording": True, "current_player": current_player})

    if not player:
        return templates.TemplateResponse("rec_form.html", {"request": request, "error": "No player provided. What is your email?", "recording": False})

    if not re.match(r"[^@]+@[^@]+\.[^@]+", player):
        return templates.TemplateResponse("rec_form.html", {"request": request, "error": "Invalid email address provided.", "recording": False})

    ## UPLOAD
    if file:
      file_size = len(await file.read())
      await file.seek(0) # reset file pointer
      if file_size > 1024 * 1024 * 1024:
            return templates.TemplateResponse("rec_form.html", {"request": request, "error": "File size exceeds the limit of 1GB.", "recording": False})

      mime = magic.Magic(mime=True)
      file_type = mime.from_buffer(await file.read(2048)) # Read a chunk of file
      await file.seek(0) # reset file pointer

      if not file_type.startswith('video/'):
         return templates.TemplateResponse("rec_form.html", {"request": request, "error": f"Uploaded file is not a video. Type: {file_type}", "recording": False})

      file_location = f"tmp/{file.filename}"

      # Sauvegarder le fichier uploadé
      async with aiofiles.open(file_location, 'wb') as out_file:
           content = await file.read()
           await out_file.write(content)
      logging.debug(f"Fichier sauvegardé: {file_location}")

      script_path = "./startrec.sh"
      return_code, last_line = await run_script(script_path, player, f"upload={file_location}") # appel avec le parametre upload=

      if return_code == 0:
         return templates.TemplateResponse("rec_form.html", {"request": request, "message": "Video Uploaded successfully", "recording": False})
      else:
         return templates.TemplateResponse("rec_form.html", {"request": request, "error": f"Script execution failed: {last_line.strip()}", "recording": False})

    ## YOUTUBE LINK
    elif link:
        script_path = "./startrec.sh"
        return_code, last_line = await run_script(script_path, player, f"link={link}") # appel avec le parametre link=

        if return_code == 0:
           return templates.TemplateResponse("rec_form.html", {"request": request, "message": "Video Downloaded successfully", "recording": False})
        else:
           return templates.TemplateResponse("rec_form.html", {"request": request, "error": f"Script execution failed: {last_line.strip()}", "recording": False})
    ## VDO OBS-STUDIO
    else:
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
                current_player = player # stocke l'email
                return templates.TemplateResponse("rec_form.html", {"request": request, "message": "Recording started successfully.", "player_info": last_line.strip(), "obs_output": getlog.stdout.strip(), "recording": True, "current_player": current_player})
            else:
                return templates.TemplateResponse("rec_form.html", {"request": request, "error": f"Failed to start OBS recording. Error: {getlog.stderr.strip()}", "recording": False})
        else:
            return templates.TemplateResponse("rec_form.html", {"request": request, "error": f"Script execution failed. {last_line.strip()}", "recording": False})

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

@app.route('/webhook', methods=['POST'])
def get_webhook():
    if request.method == 'POST':
        print("received data: ", request.json)
        return {"received": request.json}
    else:
        raise HTTPException(status_code=400, detail="Invalid method.")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=54321)
