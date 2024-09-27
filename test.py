import os
import sys
import unittest
import torch
import numpy as np
from vosk import Model
import requests
from PIL import Image
import fitz
import tempfile
import wave
import struct
import json
from unittest.mock import patch, MagicMock

# Importer les fonctions du programme principal
from main import (
    load_models,
    extract_audio,
    transcribe_audio,
    generate_embedding,
    analyze_image_with_moondream,
    extract_text_from_image,
    extract_text_from_pdf,
    extract_text_from_html,
    process_file,
    create_vector_database
)

class TestVectorDatabaseCreation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.models_dir = "./models"
        cls.vosk_model_path = "./vosk_model"
        cls.test_dir = "./test_files"
        cls.output_dir = "./test_output"

        os.makedirs(cls.test_dir, exist_ok=True)
        os.makedirs(cls.output_dir, exist_ok=True)

        # Créer un fichier texte de test
        with open(os.path.join(cls.test_dir, "test.txt"), "w") as f:
            f.write("This is a test file.")

        # Créer un fichier audio de test
        cls.create_test_wav()

        # Créer un fichier PDF de test
        cls.create_test_pdf()

        # Créer une image de test
        cls.create_test_image()

        # Créer un fichier HTML de test
        cls.create_test_html()

        assert os.path.exists(cls.models_dir), "Models directory not found"
        assert os.path.exists(cls.vosk_model_path), "Vosk model not found"

    @classmethod
    def create_test_wav(cls):
        audio_path = os.path.join(cls.test_dir, "test_audio.wav")
        with wave.open(audio_path, "w") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            for _ in range(16000):  # 1 second of silence
                value = struct.pack('<h', 0)
                wav_file.writeframes(value)

    @classmethod
    def create_test_pdf(cls):
        pdf_path = os.path.join(cls.test_dir, "test.pdf")
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((50, 50), "This is a test PDF file.")
        doc.save(pdf_path)
        doc.close()

    @classmethod
    def create_test_image(cls):
        image_path = os.path.join(cls.test_dir, "test_image.png")
        image = Image.new('RGB', (100, 30), color = (73, 109, 137))
        image.save(image_path)

    @classmethod
    def create_test_html(cls):
        html_content = "<html><body><p>Test HTML content</p></body></html>"
        html_path = os.path.join(cls.test_dir, "test.html")
        with open(html_path, "w") as f:
            f.write(html_content)

    def test_load_models(self):
        tokenizer, model = load_models(self.models_dir)
        self.assertIsNotNone(tokenizer)
        self.assertIsNotNone(model)

    def test_extract_audio(self):
        input_path = os.path.join(self.test_dir, "test_audio.wav")
        output_path = os.path.join(self.test_dir, "extracted_audio.wav")
        extract_audio(input_path, output_path)
        self.assertTrue(os.path.exists(output_path))

    def test_transcribe_audio(self):
        audio_path = os.path.join(self.test_dir, "test_audio.wav")
        text = transcribe_audio(audio_path, self.vosk_model_path)
        self.assertIsInstance(text, str)

    def test_generate_embedding(self):
        tokenizer, model = load_models(self.models_dir)
        embedding = generate_embedding("Test text", tokenizer, model)
        self.assertIsInstance(embedding, np.ndarray)

    @patch('requests.post')
    def test_analyze_image_with_moondream(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {"response": "This is a test image analysis"}
        mock_post.return_value = mock_response

        image_path = os.path.join(self.test_dir, "test_image.png")
        analysis = analyze_image_with_moondream(image_path)
        self.assertEqual(analysis, "This is a test image analysis")

    def test_extract_text_from_image(self):
        image_path = os.path.join(self.test_dir, "test_image.png")
        text = extract_text_from_image(image_path)
        self.assertIsInstance(text, str)

    def test_extract_text_from_pdf(self):
        pdf_path = os.path.join(self.test_dir, "test.pdf")
        text = extract_text_from_pdf(pdf_path)
        self.assertIn("This is a test PDF file.", text)

    def test_extract_text_from_html(self):
        html_path = os.path.join(self.test_dir, "test.html")
        text = extract_text_from_html(html_path)
        self.assertIn("Test HTML content", text)

    def test_process_file(self):
        tokenizer, model = load_models(self.models_dir)
        file_path = os.path.join(self.test_dir, "test.txt")
        embedding, meta = process_file(file_path, self.vosk_model_path, tokenizer, model)
        self.assertIsInstance(embedding, np.ndarray)
        self.assertIsInstance(meta, dict)

    def test_create_vector_database(self):
        create_vector_database(self.test_dir, self.output_dir, self.vosk_model_path, self.models_dir)
        self.assertTrue(os.path.exists(os.path.join(self.output_dir, "vector_index.faiss")))
        self.assertTrue(os.path.exists(os.path.join(self.output_dir, "metadata.json")))

if __name__ == "__main__":
    unittest.main()
