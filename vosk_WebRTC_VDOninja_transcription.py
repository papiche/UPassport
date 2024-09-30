#!/usr/bin/env python3
## NOT WORKING... Could be password missing
import asyncio
import websockets
import json
import argparse
import logging
from vosk import Model, KaldiRecognizer
from urllib.parse import urlparse, parse_qs

# Configuration du logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

# Configuration par défaut
DEFAULT_ROOM_NAME = "UPLANET"
DEFAULT_WSS_SERVER = "wss://wss.vdo.ninja/"
VOSK_MODEL_PATH = "vosk_model/selected"

# Fonction pour parser les arguments
def parse_arguments():
    parser = argparse.ArgumentParser(description="VDO.Ninja STT avec Vosk")
    parser.add_argument("--room", default=DEFAULT_ROOM_URL, help="URL de la room WebRTC")
    parser.add_argument("--wss", default=DEFAULT_WSS_SERVER, help="Serveur WebSocket WebRTC")
    return parser.parse_args()

# Initialiser le modèle Vosk
logging.info(f"Initializing Vosk model from {VOSK_MODEL_PATH}")
model = Model(VOSK_MODEL_PATH)
recognizer = KaldiRecognizer(model, 16000)
logging.info("Vosk model initialized")

async def process_audio(websocket, path):
    logging.info("Starting audio processing")
    async for message in websocket:
        logging.debug(f"Received message: {message[:100]}...")  # Log only first 100 chars
        try:
            data = json.loads(message)
            if data['type'] == 'audio':
                logging.debug("Processing audio data")
                audio_data = data['data']
                if recognizer.AcceptWaveform(audio_data):
                    result = json.loads(recognizer.Result())
                    logging.info(f"Transcription: {result['text']}")
            else:
                logging.debug(f"Received non-audio message of type: {data['type']}")
        except json.JSONDecodeError:
            logging.error("Failed to decode JSON message")
        except KeyError:
            logging.error("Message does not contain expected keys")

async def connect_to_vdo_ninja(room_id, wss_server):
    uri = f"{wss_server}ws?room={room_id}"
    logging.info(f"Connecting to WebSocket at {uri}")
    try:
        async with websockets.connect(uri) as websocket:
            logging.info("Connected to WebSocket")
            await websocket.send(json.dumps({
                "type": "join",
                "room": room_id
            }))
            logging.info(f"Joined room: {room_id}")
            await process_audio(websocket, None)
    except websockets.exceptions.WebSocketException as e:
        logging.error(f"WebSocket connection failed: {e}")

async def main(room_url, wss_server):
    logging.info("Starting main function")
    server = await websockets.serve(process_audio, "localhost", 8765)
    logging.info("Local WebSocket server started on localhost:8765")
    await asyncio.gather(
        server.wait_closed(),
        connect_to_vdo_ninja(room_url, wss_server)
    )

if __name__ == "__main__":
    args = parse_arguments()
    logging.info(f"Starting program with room URL: {args.room} and WSS server: {args.wss}")
    asyncio.run(main(args.room, args.wss))
