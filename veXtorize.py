import os
import argparse
import json
import subprocess
from tqdm import tqdm
import logging
import faiss
import numpy as np
from transformers import AutoTokenizer, AutoModel
import torch
from vosk import Model, KaldiRecognizer, SetLogLevel
import fitz  # PyMuPDF
from PIL import Image
import pytesseract
from bs4 import BeautifulSoup
import requests

# Configuration du logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
tokenizer = AutoTokenizer.from_pretrained("sentence-transformers/all-MiniLM-L6-v2")
model = AutoModel.from_pretrained("sentence-transformers/all-MiniLM-L6-v2").to(device)
OLLAMA_API_URL = "http://localhost:11434/api/generate"

def extract_audio(file_path, output_path):
    command = [
        "ffmpeg", "-i", file_path,
        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
        output_path
    ]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def transcribe_audio(audio_path, vosk_model_path, lang):
    SetLogLevel(-1)
    sample_rate = 16000
    model = Model(vosk_model_path)
    rec = KaldiRecognizer(model, sample_rate)

    process = subprocess.Popen(['ffmpeg', '-loglevel', 'quiet', '-i',
                                audio_path,
                                '-ar', str(sample_rate) , '-ac', '1', '-f', 's16le', '-'],
                                stdout=subprocess.PIPE)

    results = []
    while True:
        data = process.stdout.read(4000)
        if len(data) == 0:
            break
        if rec.AcceptWaveform(data):
            part_result = json.loads(rec.Result())
            results.append(part_result)
    part_result = json.loads(rec.FinalResult())
    results.append(part_result)

    return " ".join([r.get('text', '') for r in results if 'text' in r])

def generate_embedding(text):
    inputs = tokenizer(text, return_tensors="pt", truncation=True, max_length=512, padding=True).to(device)
    with torch.no_grad():
        outputs = model(**inputs)
    return outputs.last_hidden_state.mean(dim=1).squeeze().cpu().numpy()

def analyze_image_with_moondream(image_path):
    with open(image_path, "rb") as image_file:
        response = requests.post(
            OLLAMA_API_URL,
            json={
                "model": "moondream",
                "prompt": "Analyze this image and describe its content in detail.",
                "images": [image_file.read().decode('latin-1')]
            }
        )
    return response.json()["response"]

def extract_text_from_image(file_path):
    moondream_analysis = analyze_image_with_moondream(file_path)
    image = Image.open(file_path)
    ocr_text = pytesseract.image_to_string(image)
    return f"Moondream analysis: {moondream_analysis}\nOCR text: {ocr_text}"

def extract_text_from_pdf(file_path):
    text = ""
    with fitz.open(file_path) as doc:
        for page in doc:
            if page.get_text().strip():
                text += page.get_text()
            else:
                # Page contains only images, apply OCR
                pix = page.get_pixmap()
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                text += pytesseract.image_to_string(img)
    return text

def extract_text_from_html(file_path):
    with open(file_path, 'r', encoding='utf-8') as file:
        soup = BeautifulSoup(file, 'html.parser')
        text = soup.get_text()
        # Extract and analyze images in HTML
        images = soup.find_all('img')
        for img in images:
            src = img.get('src')
            if src and os.path.exists(src):
                text += f"\nImage analysis: {extract_text_from_image(src)}"
    return text

def process_file(file_path, vosk_model_path, lang):
    file_extension = os.path.splitext(file_path)[1].lower()

    try:
        if file_extension in ['.mp3', '.wav', '.flac', '.mp4', '.avi', '.mov']:
            temp_audio_path = "temp_audio.wav"
            extract_audio(file_path, temp_audio_path)
            text = transcribe_audio(temp_audio_path, vosk_model_path, lang)
            os.remove(temp_audio_path)
        elif file_extension == '.pdf':
            text = extract_text_from_pdf(file_path)
        elif file_extension in ['.jpg', '.jpeg', '.png', '.gif']:
            text = extract_text_from_image(file_path)
        elif file_extension == '.html':
            text = extract_text_from_html(file_path)
        elif file_extension in ['.md', '.txt']:
            with open(file_path, 'r', encoding='utf-8') as file:
                text = file.read()
        else:
            logger.warning(f"Unsupported file type: {file_extension}")
            return None, None

        embedding = generate_embedding(text)
        return embedding, {"file_path": file_path, "type": file_extension, "content": text}
    except Exception as e:
        logger.error(f"Error processing file {file_path}: {str(e)}")
        return None, None

def create_vector_database(directory, output_dir, vosk_model_path, lang):
    logger.info(f"Creating vector database from directory: {directory}")
    embeddings = []
    metadata = []

    for root, _, files in os.walk(directory):
        for file in tqdm(files, desc="Processing files"):
            file_path = os.path.join(root, file)
            embedding, meta = process_file(file_path, vosk_model_path, lang)
            if embedding is not None:
                embeddings.append(embedding)
                metadata.append(meta)

    logger.info(f"Processed {len(embeddings)} files successfully")

    if embeddings:
        dim = len(embeddings[0])
        index = faiss.IndexFlatL2(dim)
        index.add(np.array(embeddings))
        os.makedirs(output_dir, exist_ok=True)
        index_path = os.path.join(output_dir, "vector_index.faiss")
        faiss.write_index(index, index_path)
        logger.info(f"FAISS index saved to: {index_path}")

        metadata_path = os.path.join(output_dir, "metadata.json")
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f)
        logger.info(f"Metadata saved to: {metadata_path}")
    else:
        logger.warning("No embeddings were created. Check your input files and processing steps.")

def main():
    parser = argparse.ArgumentParser(description="Create vector database from various document types including audio and video")
    parser.add_argument("directory", help="Directory containing the documents")
    parser.add_argument("output_dir", help="Output directory for the vector database")
    parser.add_argument("--vosk_model", required=True, help="Path to the Vosk model")
    parser.add_argument("--lang", default="fr", help="Language for speech recognition (default: fr)")
    parser.add_argument("--log", choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'], default='INFO', help="Set the logging level")
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log)

    create_vector_database(args.directory, args.output_dir, args.vosk_model, args.lang)
    logger.info(f"Vector database creation completed. Output directory: {args.output_dir}")

if __name__ == "__main__":
    main()
