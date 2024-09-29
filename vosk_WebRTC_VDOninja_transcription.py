#!/usr/bin/env python3
import asyncio
import websockets
import json
import argparse
from vosk import Model, KaldiRecognizer
from urllib.parse import urlparse, parse_qs

# Configuration par défaut
DEFAULT_ROOM_URL = "https://ipfs.copylaradio.com/ipfs/QmcSkcJ2j7GAsC2XhVqGSNAKVRpXgxfjjvDbhD5YxrncZY/?room=UPLANET&"
DEFAULT_WSS_SERVER = "wss://wss.vdo.ninja/"
VOSK_MODEL_PATH = "vosk_model/selected"

# Fonction pour parser les arguments
def parse_arguments():
    parser = argparse.ArgumentParser(description="VDO.Ninja STT avec Vosk")
    parser.add_argument("--room", default=DEFAULT_ROOM_URL, help="URL de la room VDO.Ninja")
    parser.add_argument("--wss", default=DEFAULT_WSS_SERVER, help="Serveur WebSocket VDO.Ninja")
    return parser.parse_args()

# Initialiser le modèle Vosk
model = Model(VOSK_MODEL_PATH)
recognizer = KaldiRecognizer(model, 16000)

async def process_audio(websocket, path):
    async for message in websocket:
        data = json.loads(message)
        if data['type'] == 'audio':
            audio_data = data['data']
            if recognizer.AcceptWaveform(audio_data):
                result = json.loads(recognizer.Result())
                print(f"Transcription: {result['text']}")

async def connect_to_vdo_ninja(room_url, wss_server):
    parsed_url = urlparse(room_url)
    query_params = parse_qs(parsed_url.query)
    room_id = query_params.get('room', [''])[0]

    if not room_id:
        raise ValueError("Room ID not found in the URL")

    uri = f"{wss_server}ws?room={room_id}"
    async with websockets.connect(uri) as websocket:
        await websocket.send(json.dumps({
            "type": "join",
            "room": room_id
        }))
        await process_audio(websocket, None)

async def main(room_url, wss_server):
    server = await websockets.serve(process_audio, "localhost", 8765)
    await asyncio.gather(
        server.wait_closed(),
        connect_to_vdo_ninja(room_url, wss_server)
    )

if __name__ == "__main__":
    args = parse_arguments()
    asyncio.run(main(args.room, args.wss))
