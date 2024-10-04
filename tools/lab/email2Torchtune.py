import imaplib
import email
from email.header import decode_header
import requests
import logging
from logging.handlers import RotatingFileHandler
import sys
import traceback
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
import os
import torch
from torch.utils.data import DataLoader, TensorDataset
from torch.optim import AdamW

# Charger les variables d'environnement
load_dotenv()

# Configuration du logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = RotatingFileHandler('email_rag.log', maxBytes=10000000, backupCount=5)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# Vérification des variables d'environnement
required_env_vars = ['IMAP_SERVER', 'SMTP_SERVER', 'SMTP_PORT', 'EMAIL', 'PASSWORD']
for var in required_env_vars:
    if not os.getenv(var):
        logger.error(f"La variable d'environnement {var} n'est pas définie")
        sys.exit(1)

def lire_emails(imap_server, email_address, password):
    logger.info(f"Tentative de connexion au serveur IMAP: {imap_server}")
    try:
        imap = imaplib.IMAP4_SSL(imap_server)
        logger.info("Connexion SSL établie, tentative de login")
        imap.login(email_address, password)
        logger.info("Login réussi")

        # Sélectionner la boîte de réception
        status, messages = imap.select("INBOX")
        if status != "OK":
            logger.error(f"Impossible de sélectionner la boîte de réception: {messages}")
            return

        logger.info("Boîte de réception sélectionnée avec succès")

        _, message_numbers = imap.search(None, "UNSEEN")

        for num in message_numbers[0].split():
            _, msg_data = imap.fetch(num, "(RFC822)")
            email_body = msg_data[0][1]
            email_message = email.message_from_bytes(email_body)

            sujet = decode_header(email_message["Subject"])[0][0]
            if isinstance(sujet, bytes):
                sujet = sujet.decode()

            contenu = ""
            if email_message.is_multipart():
                for part in email_message.walk():
                    if part.get_content_type() == "text/plain":
                        contenu = part.get_payload(decode=True).decode()
            else:
                contenu = email_message.get_payload(decode=True).decode()

            yield sujet, contenu, email_message

        imap.close()
        imap.logout()
    except imaplib.IMAP4.error as e:
        logger.error(f"Erreur IMAP lors de la lecture des emails: {str(e)}")
    except Exception as e:
        logger.error(f"Erreur inattendue lors de la lecture des emails: {str(e)}")
        logger.error(traceback.format_exc())


def envoyer_email(smtp_server, smtp_port, sender_email, sender_password, recipient, subject, body):
    try:
        msg = MIMEMultipart()
        msg['From'] = sender_email
        msg['To'] = recipient
        msg['Subject'] = f"Re: {subject}"
        msg['Bcc'] = sender_email
        msg.attach(MIMEText(body, 'plain'))

        context = ssl.create_default_context()

        try:
            with smtplib.SMTP_SSL(smtp_server, smtp_port, context=context) as server:
                server.login(sender_email, sender_password)
                server.send_message(msg)
            logger.info(f"Réponse envoyée avec succès à {recipient} en utilisant SSL")
        except Exception as e:
            logger.warning(f"Échec de l'envoi avec SSL: {str(e)}. Tentative avec STARTTLS...")

        # Essai avec STARTTLS
        try:
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.ehlo()
                if server.has_extn('STARTTLS'):
                    server.starttls(context=context)
                    server.ehlo()
                server.login(sender_email, sender_password)
                server.send_message(msg)
            logger.info(f"Réponse envoyée avec succès à {recipient} en utilisant STARTTLS")
        except Exception as e:
            logger.warning(f"Échec de l'envoi avec STARTTLS: {str(e)}. Tentative sans chiffrement...")

            # Si STARTTLS échoue, essayer sans chiffrement
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.login(sender_email, sender_password)
                server.send_message(msg)
            logger.info(f"Réponse envoyée avec succès à {recipient} sans chiffrement")

    except Exception as e:
        logger.error(f"Erreur lors de l'envoi de l'email à {recipient}: {str(e)}")
        logger.error(traceback.format_exc())

def generer_reponse(sujet, contenu, model_name):
    try:
        # Générer l'embedding
        embedding_data = {
            "model": model_name,
            "prompt": contenu
        }
        embedding_response = requests.post("http://localhost:11434/api/embeddings", json=embedding_data)
        embedding = embedding_response.json()["embedding"]

        # Ici, nous n'utilisons plus ChromaDB pour récupérer les exemples positifs
        # À la place, nous pourrions utiliser une autre méthode pour stocker et récupérer ces exemples
        # Pour cet exemple, nous allons simplement utiliser un contexte fixe
        contexte_exemples = "Exemple 1: ...\nExemple 2: ...\nExemple 3: ..."

        prompt = f"Exemples précédents:\n{contexte_exemples}\n\nEmail actuel:\nSujet: {sujet}\nContenu: {contenu}\n\nRéponse:"
        logger.debug(f"prompt : {prompt}")

        # Générer la réponse
        generate_data = {
            "model": model_name,
            "prompt": prompt,
            "system": "Vous êtes ASTRO un assistant intelligent qui lit et réponds aux messages. Utilisez les exemples précédents et le contexte fourni pour générer une réponse pertinente. Terminez en signalant que vous êtes un assistant et signez la réponse avec votre nom ASTRO.",
            "stream": False
        }

        response = requests.post("http://localhost:11434/api/generate", json=generate_data)
        logger.debug(f"Réponse brute de l'API Ollama : {response.text}")

        # Tentative de décodage JSON
        try:
            response_json = response.json()
            return response_json['response']
        except json.JSONDecodeError as json_error:
            logger.error(f"Erreur de décodage JSON : {str(json_error)}")
            logger.error(f"Contenu de la réponse : {response.text}")
            return "Désolé, une erreur s'est produite lors de la génération de la réponse."

    except Exception as e:
        logger.error(f"Erreur lors de la génération de la réponse par le modèle {model_name}: {str(e)}")
        logger.error(traceback.format_exc())
        return "Désolé, une erreur s'est produite lors de la génération de la réponse."

def fine_tune_model(model, dataset):
    try:
        # Préparer les données
        inputs, labels = dataset[0]
        dataset = TensorDataset(inputs['input_ids'], inputs['attention_mask'], labels)
        dataloader = DataLoader(dataset, batch_size=1, shuffle=True)

        # Définir l'optimiseur
        optimizer = AdamW(model.parameters(), lr=1e-5)

        # Fine-tuning
        model.train()
        for epoch in range(1):  # Nous faisons seulement une époque par email
            for batch in dataloader:
                input_ids, attention_mask, labels = batch
                outputs = model(input_ids, attention_mask=attention_mask, labels=labels)
                loss = outputs.loss
                loss.backward()
                optimizer.step()
                optimizer.zero_grad()

        logger.info("Fine-tuning du modèle effectué avec succès")
    except Exception as e:
        logger.error(f"Erreur lors du fine-tuning du modèle: {str(e)}")
        logger.error(traceback.format_exc())


def traiter_emails_et_appliquer_rag(imap_server, email_address, password, smtp_server, smtp_port, model_name):
    emails_traites = 0
    try:
        for sujet, contenu, email_message in lire_emails(imap_server, email_address, password):
            expediteur = email.utils.parseaddr(email_message['From'])[1]
            logger.info(f"Traitement de l'email de {expediteur} avec le sujet: {sujet}")

            reponse_generee = generer_reponse(sujet, contenu, model_name)
            logger.info(f"Reponse : {reponse_generee}")

            # envoyer_email(smtp_server, smtp_port, email_address, password, expediteur, sujet, reponse_generee)


    except Exception as e:
        logger.error(f"Erreur générale dans le processus de traitement des emails: {str(e)}")
        logger.error(traceback.format_exc())

if __name__ == "__main__":
    try:
        logger.info("Démarrage du processus de traitement des emails")

        IMAP_SERVER = os.getenv("IMAP_SERVER")
        SMTP_SERVER = os.getenv("SMTP_SERVER")
        SMTP_PORT = int(os.getenv("SMTP_PORT"))
        EMAIL = os.getenv("EMAIL")
        PASSWORD = os.getenv("PASSWORD")
        MODEL = os.getenv("MODEL")

        traiter_emails_et_appliquer_rag(IMAP_SERVER, EMAIL, PASSWORD, SMTP_SERVER, SMTP_PORT, "llama3.2")

        logger.info("Fin du processus de traitement des emails")
    except KeyboardInterrupt:
        logger.info("Processus interrompu par l'utilisateur")
    except Exception as e:
        logger.critical(f"Erreur critique: {str(e)}")
        logger.critical(traceback.format_exc())
