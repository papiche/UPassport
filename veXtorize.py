#!/usr/bin/env python3
import os
import argparse
import json
import subprocess
from tqdm import tqdm
import logging
import faiss
import numpy as np
from transformers import AutoTokenizer, AutoModel, Trainer, TrainingArguments
from datasets import Dataset
import torch
from vosk import Model, KaldiRecognizer, SetLogLevel
import fitz  # PyMuPDF
from PIL import Image
import pytesseract
from bs4 import BeautifulSoup
import requests
from multiprocessing import Pool
from sklearn.manifold import TSNE
import matplotlib.pyplot as plt
import torchtune
import multiprocessing

# Set the start method to 'spawn'
multiprocessing.set_start_method('spawn', force=True)

# Configuration du logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration
global device
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
OLLAMA_API_URL = "http://localhost:11434/api/generate"

def load_models(models_dir):
    tokenizer = AutoTokenizer.from_pretrained(os.path.join(models_dir, "tokenizer"))
    model = AutoModel.from_pretrained(os.path.join(models_dir, "model")).to(device)
    return tokenizer, model

def extract_audio(file_path, output_path):
    command = [
        "ffmpeg", "-i", file_path,
        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
        output_path
    ]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def transcribe_audio(audio_path, vosk_model_path):
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

def generate_embedding(text, tokenizer, model):
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
                pix = page.get_pixmap()
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                text += pytesseract.image_to_string(img)
    return text

def extract_text_from_html(file_path):
    with open(file_path, 'r', encoding='utf-8') as file:
        soup = BeautifulSoup(file, 'html.parser')
        text = soup.get_text()
        images = soup.find_all('img')
        for img in images:
            src = img.get('src')
            if src and os.path.exists(src):
                text += f"\nImage analysis: {extract_text_from_image(src)}"
    return text

def chunk_text(text, chunk_size=1000):
    return [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]

def process_large_document(text, tokenizer, model):
    chunks = chunk_text(text)
    embeddings = [generate_embedding(chunk, tokenizer, model) for chunk in chunks]
    return np.mean(embeddings, axis=0)

def enrich_metadata(file_path, content):
    return {
        "file_path": file_path,
        "type": os.path.splitext(file_path)[1],
        "content": content,
        "creation_date": os.path.getctime(file_path),
        "modification_date": os.path.getmtime(file_path),
        "size": os.path.getsize(file_path),
    }

def process_file(file_path, vosk_model_path, tokenizer, model):
    file_extension = os.path.splitext(file_path)[1].lower()

    try:
        if file_extension in ['.mp3', '.wav', '.flac', '.mp4', '.avi', '.mov']:
            temp_audio_path = "temp_audio.wav"
            extract_audio(file_path, temp_audio_path)
            text = transcribe_audio(temp_audio_path, vosk_model_path)
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

        if len(text) > 1000:
            embedding = process_large_document(text, tokenizer, model)
        else:
            embedding = generate_embedding(text, tokenizer, model)

        metadata = enrich_metadata(file_path, text)
        return embedding, metadata
    except Exception as e:
        logger.error(f"Error processing file {file_path}: {str(e)}")
        return None, None

def process_file_wrapper(args):
    file, vosk_model_path, tokenizer, model = args
    return process_file(file, vosk_model_path, tokenizer, model)

def process_files_parallel(file_list, vosk_model_path, tokenizer, model):
    # Déplacer l'initialisation du device ici
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = model.to(device)

    args_list = [(file, vosk_model_path, tokenizer, model) for file in file_list]

    with Pool() as pool:
        results = pool.map(process_file_wrapper, args_list)
    return [r for r in results if r is not None]

def set_cuda_device():
    if torch.cuda.is_available():
        torch.cuda.set_device(0)  # or whichever GPU you want to use

def fine_tune_model(model, tokenizer, train_texts, train_labels):
    train_encodings = tokenizer(train_texts, truncation=True, padding=True)
    train_dataset = Dataset.from_dict(train_encodings).add_column("labels", train_labels)

    training_args = TrainingArguments(
        output_dir="./results",
        num_train_epochs=3,
        per_device_train_batch_size=16,
        learning_rate=2e-5,
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
    )

    trainer.train()

def semantic_search(query, index, tokenizer, model, metadata, k=5):
    query_embedding = generate_embedding(query, tokenizer, model)
    D, I = index.search(np.array([query_embedding]), k)
    results = []
    for i, score in zip(I[0], D[0]):
        results.append({
            "score": 1 / (1 + score),  # Convert distance to similarity score
            "metadata": metadata[i]
        })
    return results

def visualize_embeddings(embeddings, labels):
    tsne = TSNE(n_components=2, random_state=42)
    embeddings_2d = tsne.fit_transform(embeddings)
    plt.figure(figsize=(12, 8))
    scatter = plt.scatter(embeddings_2d[:, 0], embeddings_2d[:, 1], c=labels)
    plt.colorbar(scatter)
    plt.title("t-SNE visualization of document embeddings")
    plt.show()

def update_vector_database(new_files, existing_index, existing_metadata, vosk_model_path, tokenizer, model):
    new_embeddings, new_metadata = zip(*process_files_parallel(new_files, vosk_model_path, tokenizer, model))
    existing_index.add(np.array(new_embeddings))
    existing_metadata.extend(new_metadata)
    return existing_index, existing_metadata

def create_vector_database(directory, output_dir, vosk_model_path, models_dir):
    logger.info(f"Creating vector database from directory: {directory}")
    tokenizer, model = load_models(models_dir)

    file_list = []
    for root, _, files in os.walk(directory):
        for file in files:
            file_list.append(os.path.join(root, file))

    results = process_files_parallel(file_list, vosk_model_path, tokenizer, model)
    embeddings, metadata = zip(*results)

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

        # Visualize embeddings
        visualize_embeddings(embeddings, [m['type'] for m in metadata])
    else:
        logger.warning("No embeddings were created. Check your input files and processing steps.")

def export_to_gguf(model, tokenizer, output_path):
    logger.info(f"Exporting model to GGUF format: {output_path}")
    try:
        torchtune.export_gguf(model, tokenizer, output_path)
        logger.info(f"Model successfully exported to GGUF format: {output_path}")
    except Exception as e:
        logger.error(f"Error exporting model to GGUF: {str(e)}")

def menu():
    print("\nveXtorize.py - Vector Database Creation and Management")
    print("1. Create new vector database")
    print("2. Update existing vector database")
    print("3. Perform semantic search")
    print("4. Visualize embeddings")
    print("5. Fine-tune model")
    print("6. Export model to GGUF")
    print("7. Exit")
    return input("Choose an option: ")

def main():
    if torch.cuda.is_available():
        torch.multiprocessing.set_start_method('spawn', force=True)
    parser = argparse.ArgumentParser(description="Vector database creation and management")
    parser.add_argument("--models_dir", default="./models", help="Directory containing the downloaded models")
    parser.add_argument("--vosk_model", default="./vosk_model", help="Path to the Vosk model")
    parser.add_argument("--log", choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'], default='INFO', help="Set the logging level")
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log)
    if torch.cuda.is_available():
        torch.multiprocessing.set_start_method('spawn', force=True)

    while True:
        choice = menu()
        if choice == '1':
            directory = input("Enter the directory containing the documents: ")
            output_dir = input("Enter the output directory for the vector database: ")
            create_vector_database(directory, output_dir, args.vosk_model, args.models_dir)
        elif choice == '2':
            index_path = input("Enter the path to the existing FAISS index: ")
            metadata_path = input("Enter the path to the existing metadata file: ")
            new_files = input("Enter the path to the new files (comma-separated): ").split(',')

            index = faiss.read_index(index_path)
            with open(metadata_path, 'r') as f:
                metadata = json.load(f)

            tokenizer, model = load_models(args.models_dir)
            index, metadata = update_vector_database(new_files, index, metadata, args.vosk_model, tokenizer, model)

            faiss.write_index(index, index_path)
            with open(metadata_path, 'w') as f:
                json.dump(metadata, f)
            logger.info("Vector database updated successfully")
        elif choice == '3':
            index_path = input("Enter the path to the FAISS index: ")
            metadata_path = input("Enter the path to the metadata file: ")
            query = input("Enter your search query: ")

            index = faiss.read_index(index_path)
            with open(metadata_path, 'r') as f:
                metadata = json.load(f)

            tokenizer, model = load_models(args.models_dir)
            results = semantic_search(query, index, tokenizer, model, metadata)

            for result in results:
                print(f"Score: {result['score']}")
                print(f"File: {result['metadata']['file_path']}")
                print(f"Content: {result['metadata']['content'][:200]}...")
                print()
        elif choice == '4':
            index_path = input("Enter the path to the FAISS index: ")
            metadata_path = input("Enter the path to the metadata file: ")

            index = faiss.read_index(index_path)
            with open(metadata_path, 'r') as f:
                metadata = json.load(f)

            embeddings = index.reconstruct_n(0, index.ntotal)
            visualize_embeddings(embeddings, [m['type'] for m in metadata])
        elif choice == '5':
            index_path = input("Enter the path to the FAISS index: ")
            metadata_path = input("Enter the path to the metadata file: ")

            with open(metadata_path, 'r') as f:
                metadata = json.load(f)

            tokenizer, model = load_models(args.models_dir)
            texts = [m['content'] for m in metadata]
            labels = [m['type'] for m in metadata]
            fine_tune_model(model, tokenizer, texts, labels)
            logger.info("Fine-tuning completed")
        elif choice == '6':
            output_path = input("Enter the output path for the GGUF file: ")
            tokenizer, model = load_models(args.models_dir)
            export_to_gguf(model, tokenizer, output_path)
        elif choice == '7':
            break
        else:
            print("Invalid choice. Please try again.")

if __name__ == "__main__":
    main()
