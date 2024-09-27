import os
import argparse
import fitz  # PyMuPDF
from PIL import Image
import pytesseract
from bs4 import BeautifulSoup
import markdown
import torch
from transformers import AutoTokenizer, AutoModel, pipeline
from sentence_transformers import SentenceTransformer
import faiss
import json
import requests
import numpy as np
import logging
from tqdm import tqdm

# Configuration du logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
tokenizer = AutoTokenizer.from_pretrained("sentence-transformers/all-MiniLM-L6-v2")
model = AutoModel.from_pretrained("sentence-transformers/all-MiniLM-L6-v2").to(device)
image_model = SentenceTransformer('clip-ViT-B-32')
OLLAMA_API_URL = "http://localhost:11434/api/generate"

def analyze_image_with_moondream(image_path):
    logger.info(f"Analyzing image with Moondream: {image_path}")
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
    logger.info(f"Processing image: {file_path}")
    moondream_analysis = analyze_image_with_moondream(file_path)

    if "text" in moondream_analysis.lower() or "writing" in moondream_analysis.lower():
        logger.info("Text detected in image. Applying OCR.")
        image = Image.open(file_path)
        ocr_text = pytesseract.image_to_string(image)
        return f"Moondream analysis: {moondream_analysis}\nOCR text: {ocr_text}"
    else:
        logger.info("No text detected in image. Skipping OCR.")
        return f"Moondream analysis: {moondream_analysis}"

def extract_text_from_pdf(file_path):
    logger.info(f"Extracting text from PDF: {file_path}")
    text = ""
    with fitz.open(file_path) as doc:
        for page in doc:
            text += page.get_text()
    return text

def extract_text_from_html(file_path):
    logger.info(f"Extracting text from HTML: {file_path}")
    with open(file_path, 'r', encoding='utf-8') as file:
        soup = BeautifulSoup(file, 'html.parser')
        return soup.get_text()

def convert_to_markdown(file_path):
    logger.info(f"Converting to markdown: {file_path}")
    with open(file_path, 'r', encoding='utf-8') as file:
        content = file.read()
        return markdown.markdown(content)

def generate_embedding(text):
    logger.debug("Generating embedding")
    inputs = tokenizer(text, return_tensors="pt", truncation=True, max_length=512, padding=True).to(device)
    with torch.no_grad():
        outputs = model(**inputs)
    return outputs.last_hidden_state.mean(dim=1).squeeze().cpu().numpy()

def process_file(file_path):
    file_extension = os.path.splitext(file_path)[1].lower()
    logger.info(f"Processing file: {file_path}")

    if file_extension == '.pdf':
        text = extract_text_from_pdf(file_path)
    elif file_extension in ['.jpg', '.jpeg', '.png']:
        text = extract_text_from_image(file_path)
    elif file_extension == '.html':
        text = extract_text_from_html(file_path)
    elif file_extension in ['.md', '.txt']:
        text = convert_to_markdown(file_path)
    else:
        logger.warning(f"Unsupported file type: {file_extension}")
        return None, None

    embedding = generate_embedding(text)

    return embedding, {"file_path": file_path, "type": file_extension, "content": text}

def create_vector_database(directory, output_dir):
    logger.info(f"Creating vector database from directory: {directory}")
    embeddings = []
    metadata = []

    files = [os.path.join(root, file) for root, _, files in os.walk(directory) for file in files]

    for file_path in tqdm(files, desc="Processing files"):
        embedding, meta = process_file(file_path)
        if embedding is not None:
            embeddings.append(embedding)
            metadata.append(meta)

    logger.info(f"Processed {len(embeddings)} files successfully")

    # Créer et sauvegarder l'index FAISS
    dim = len(embeddings[0])
    index = faiss.IndexFlatL2(dim)
    index.add(np.array(embeddings))
    index_path = os.path.join(output_dir, "vector_index.faiss")
    faiss.write_index(index, index_path)
    logger.info(f"FAISS index saved to: {index_path}")

    # Sauvegarder les métadonnées
    metadata_path = os.path.join(output_dir, "metadata.json")
    with open(metadata_path, 'w') as f:
        json.dump(metadata, f)
    logger.info(f"Metadata saved to: {metadata_path}")

def main():
    parser = argparse.ArgumentParser(description="Create vector database from documents")
    parser.add_argument("directory", help="Directory containing the documents")
    parser.add_argument("output_dir", help="Output directory for the vector database")
    parser.add_argument("--log", choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'], default='INFO', help="Set the logging level")
    args = parser.parse_args()

    # Set logging level
    logging.getLogger().setLevel(args.log)

    create_vector_database(args.directory, args.output_dir)
    logger.info(f"Vector database creation completed. Output directory: {args.output_dir}")

if __name__ == "__main__":
    main()
