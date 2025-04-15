#!/usr/bin/env python3*
import uuid
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


class MessageData(BaseModel):
    ulat: str
    ulon: str
    pubkey: str
    uid: str
    relation: str
    pubkeyUpassport: str
    email: str
    message: str


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

## CHECK G1PUB BALANCE
def check_balance(g1pub):
    result = subprocess.run(["tools/COINScheck.sh", g1pub], capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError("Erreur dans COINScheck.sh: " + result.stderr)
    balance_line = result.stdout.strip().splitlines()[-1]
    return balance_line

## DEFAULT = UPlanet Status
@app.get("/")
async def zen_send(request: Request):

    script_path = os.path.expanduser("~/.zen/Astroport.ONE/Ustats.sh")
    return_code, last_line = await run_script(script_path)

    if return_code == 0:
        returned_file_path = last_line.strip()
        return FileResponse(returned_file_path)
    else:
        return {"error": f"Une erreur s'est produite lors de l'exécution du script. Veuillez consulter les logs dans ./tmp/54321.log."}

@app.get("/scan")
async def get_root(request: Request):
    return templates.TemplateResponse("scan_new.html", {"request": request})

@app.get("/nostr")
async def get_root(request: Request):
    return templates.TemplateResponse("nostr.html", {"request": request})

@app.get("/check_balance")
async def check_balance_route(g1pub: str):
    try:
        balance = check_balance(g1pub)
        return {"balance": balance}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

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
    return templates.TemplateResponse("rec_form.html", {"request": request, "recording": False})

@app.get("/webcam", response_class=HTMLResponse)
async def rec_form(request: Request):
    return templates.TemplateResponse("webcam.html", {"request": request, "recording": False})

@app.post("/rec", response_class=HTMLResponse)
async def start_recording(request: Request, player: str = Form(...), link: str = Form(default=""), file: UploadFile = File(None), video_blob: str = Form(default="")):
    global recording_process, current_player

    if not player:
        return templates.TemplateResponse("rec_form.html", {"request": request, "error": "No player provided. What is your email?", "recording": False})

    if not re.match(r"[^@]+@[^@]+\.[^@]+", player):
        return templates.TemplateResponse("rec_form.html", {"request": request, "error": "Invalid email address provided.", "recording": False})

    script_path = "./startrec.sh"

    # Cas 1: Enregistrement webcam
    if video_blob:
        try:
            # Vérifier si le blob contient une virgule (format data URL)
            if ',' in video_blob:
                # Extraire la partie après la virgule
                _, video_data_base64 = video_blob.split(',', 1)
                video_data = base64.b64decode(video_data_base64)
            else:
                # Si pas de virgule, supposer que c'est directement en base64
                video_data = base64.b64decode(video_blob)

            file_location = f"tmp/{player}_{int(time.time())}.webm"
            with open(file_location, 'wb') as f:
                f.write(video_data)

            return_code, last_line = await run_script(script_path, player, f"blob={file_location}")

            if return_code == 0:
                return templates.TemplateResponse("webcam.html", {"request": request, "message": f"Operation completed successfully {last_line.strip()}", "recording": False})
            else:
                return templates.TemplateResponse("webcam.html", {"request": request, "error": f"Script execution failed: {last_line.strip()}", "recording": False})

        except Exception as e:
            # Gérer toute exception qui pourrait se produire lors du traitement du blob
            return templates.TemplateResponse("webcam.html", {"request": request, "error": f"Error processing video data: {str(e)}", "recording": False})

    # Cas 2: Upload de fichier
    if file and file.filename:
        file_size = len(await file.read())
        await file.seek(0)  # reset file pointer
        if file_size > 1024 * 1024 * 1024:
            return templates.TemplateResponse("rec_form.html", {"request": request, "error": "File size exceeds the limit of 1GB.", "recording": False})

        file_location = f"tmp/{file.filename}"
        async with aiofiles.open(file_location, 'wb') as out_file:
            content = await file.read()
            await out_file.write(content)

        return_code, last_line = await run_script(script_path, player, f"upload={file_location}")

    # Cas 3: Lien YouTube
    elif link:
        return_code, last_line = await run_script(script_path, player, f"link={link}")

    # Cas 4: Enregistrement OBS
    else:
        if recording_process:
            return templates.TemplateResponse("rec_form.html", {"request": request, "error": "Recording is already in progress.", "recording": True, "current_player": current_player})

        return_code, last_line = await run_script(script_path, player)

        if return_code == 0:
            obsws_url = f"obsws://127.0.0.1:4455/{OBSkey}"
            getlog = subprocess.run(
                ["obs-cmd", "--websocket", obsws_url, "recording", "start"],
                capture_output=True, text=True
            )

            if getlog.returncode == 0:
                recording_process = True
                current_player = player
                return templates.TemplateResponse("rec_form.html", {"request": request, "message": "Recording started successfully.", "player_info": last_line.strip(), "obs_output": getlog.stdout.strip(), "recording": True, "current_player": current_player})
            else:
                return templates.TemplateResponse("rec_form.html", {"request": request, "error": f"Failed to start OBS recording. Error: {getlog.stderr.strip()}", "recording": False})

    if return_code == 0:
        return templates.TemplateResponse("rec_form.html", {"request": request, "message": f"Operation completed successfully {last_line.strip()}", "recording": False})
    else:
        return templates.TemplateResponse("rec_form.html", {"request": request, "error": f"Script execution failed: {last_line.strip()}", "recording": False})

@app.get("/stop")
async def stop_recording(request: Request, player: Optional[str] = None):
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
        return templates.TemplateResponse(
            "rec_form.html",
            {"request": request, "message": f"Operation completed successfully {last_line.strip()}", "recording": False}
        )
    else:
        return templates.TemplateResponse(
            "rec_form.html",
            {"request": request, "error": f"Script execution failed: {last_line.strip()}", "recording": False}
        )

############# API DESCRIPTION PAGE
@app.get("/index", response_class=HTMLResponse)
async def welcomeuplanet(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/uplanet", response_class=HTMLResponse)
async def welcomeuplanet(request: Request):
    return templates.TemplateResponse("uplanet.html", {"request": request})

@app.post("/uplanet")
async def uplanet(request: Request):
    try:
        data = await request.json()
        email = data.get('email')
        latitude = data.get('latitude')
        longitude = data.get('longitude')

        # Logging
        logging.info(f"email account : {email}")
        logging.info(f"latitude : {latitude}")
        logging.info(f"longitude : {longitude}")

        # Appel du script UPLANET.sh
        script_path = "~/.zen/Astroport.ONE/API/UPLANET.sh"
        return_code, last_line = await run_script(script_path, email, "zlat", latitude, "zlon", longitude)

        if return_code == 0:
            returned_file_path = last_line.strip()
            logging.info(f"Returning file: {last_line}")
            return FileResponse(last_line)
        else:
            logging.error(f"UPLANET.sh script error: {result.stderr}")
            raise HTTPException(status_code=500, detail={"status": "error", "message": "Failed to create UPlanet account"})

    except Exception as e:
        logging.error(f"Error processing uplanet request: {e}")
        raise HTTPException(status_code=500, detail={"status": "error", "message": str(e)})

@app.post('/ping')
async def get_webhook(request: Request):
    if request.method == 'POST':
        try:
            # Générer un nom de fichier avec un timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_filename = f"/tmp/ping_{timestamp}.log"

            # Récupérer les données de la requête
            data = await request.json()  # Récupérer le corps de la requête en JSON
            referer = request.headers.get("referer")  # Récupérer l'en-tête Referer

            # Écrire les données dans le fichier
            with open(log_filename, "w") as log_file:
                log_file.write(f"Received PING: {data}, Referer: {referer}\n")

            # Appeler le script mailjet.sh avec les arguments appropriés
            subprocess.run([
                os.path.expanduser("~/.zen/Astroport.ONE/tools/mailjet.sh"),
                "sagittarius@g1sms.fr",
                log_filename,
                "PING RECEIVED"
            ])

            # Supprimer le fichier après l'appel
            os.remove(log_filename)

            return {"received": data, "referer": referer}
        except Exception as e:
            # Supprimer le fichier en cas d'erreur (s'il existe)
            if os.path.exists(log_filename):
                os.remove(log_filename)
            raise HTTPException(status_code=400, detail=f"Invalid request data: {e}")

    else:
        raise HTTPException(status_code=400, detail="Invalid method.")

@app.get("/upload", response_class=HTMLResponse)
async def upload_form(request: Request):
    return templates.TemplateResponse("upload2ipfs.html", {"request": request})


@app.post("/upload2ipfs")
async def upload_to_ipfs(request: Request, file: UploadFile = File(...)):
    if not file:
        raise HTTPException(status_code=400, detail="No file uploaded.")

    file_location = f"tmp/{file.filename}"
    try:
        async with aiofiles.open(file_location, 'wb') as out_file:
            content = await file.read()
            await out_file.write(content)

        # Generate a unique temporary file path
        temp_file_path = f"tmp/temp_{uuid.uuid4()}.json"

        script_path = "./upload2ipfs.sh"
        return_code, last_line = await run_script(script_path, file_location, temp_file_path)

        if return_code == 0:
          try:
                async with aiofiles.open(temp_file_path, mode="r") as temp_file:
                    json_content = await temp_file.read()
                json_output = json.loads(json_content.strip()) # Remove extra spaces/newlines

                # Delete the temporary files
                os.remove(temp_file_path)
                os.remove(file_location)
                return JSONResponse(content=json_output)
          except (json.JSONDecodeError, FileNotFoundError) as e:
                logging.error(f"Failed to decode JSON from temp file: {temp_file_path}, Error: {e}")
                return JSONResponse(
                  content={
                      "error": "Failed to process script output, JSON decode error.",
                       "exception": str(e),
                       "temp_file_path": temp_file_path,
                      },
                   status_code=500
               )
          finally:
                if os.path.exists(temp_file_path):
                   os.remove(temp_file_path) # Ensure file deletion in case of error
                if os.path.exists(file_location):
                  os.remove(file_location) # Ensure file deletion in case of error
        else:
           logging.error(f"Script execution failed: {last_line.strip()}")
           return JSONResponse(
                content={
                    "error": f"Script execution failed.",
                    "raw_output": last_line.strip()
                  },
                  status_code=500
               )
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")
        return JSONResponse(
            content={
                "error": "An unexpected error occurred.",
                "exception": str(e)
                },
            status_code=500
        )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=54321)
