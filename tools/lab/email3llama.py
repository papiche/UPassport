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
import json
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

# Charger les variables d'environnement
load_dotenv()

# Configuration du logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = RotatingFileHandler('log.txt', maxBytes=10000000, backupCount=5)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# Vérification des variables d'environnement
required_env_vars = ['IMAP_SERVER', 'SMTP_SERVER', 'SMTP_PORT', 'EMAIL', 'PASSWORD', 'OLLAMA_API_URL', 'MODEL', 'CONTEXT']
for var in required_env_vars:
    if not os.getenv(var):
        logger.error(f"La variable d'environnement {var} n'est pas définie")
        sys.exit(1)

OLLAMA_API_URL = os.getenv("OLLAMA_API_URL")
MODEL = os.getenv("MODEL")


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

        logger.info(f"Tentative d'envoi de l'email à {recipient} en utilisant SSL")
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

def creer_fichier_contextuel(email_address, reponse_generee, contenu):
    try:
        chemin_dossier = os.path.join("./emails/", email_address)
        if not os.path.exists(chemin_dossier):
            os.makedirs(chemin_dossier)

        with open(os.path.join(chemin_dossier, "context.txt"), 'a') as file:
            file.write("Contenu de l'email : \n")
            file.write(contenu)
            file.write("\n\nRéponse générée : \n")
            file.write(reponse_generee)
        logger.info(f"Fichier contextuel créé pour {email_address}")
    except Exception as e:
        logger.error(f"Erreur lors de la création du fichier contextuel : {str(e)}")

def generer_embedding(texte):
    embedding_data = {
        "model": MODEL,
        "prompt": texte
    }
    response = requests.post(f"{OLLAMA_API_URL}/api/embeddings", json=embedding_data)
    return response.json()["embedding"]

def stocker_embedding(expediteur, contenu, embedding):
    chemin_dossier = os.path.join("./emails/", expediteur)
    if not os.path.exists(chemin_dossier):
        os.makedirs(chemin_dossier)

    fichier_embeddings = os.path.join(chemin_dossier, "embeddings.json")

    try:
        with open(fichier_embeddings, 'r') as f:
            embeddings = json.load(f)
    except FileNotFoundError:
        embeddings = []

    embeddings.append({"contenu": contenu, "embedding": embedding})

    with open(fichier_embeddings, 'w') as f:
        json.dump(embeddings, f)

def lire_et_mettre_a_jour_contexte():
    CONTEXT = os.getenv("CONTEXT")
    if not os.path.exists(CONTEXT):
        with open(CONTEXT, 'w') as f:
            f.write("Contexte global initial")

    with open(CONTEXT, 'r') as f:
        contexte_global = f.read()

    return contexte_global

def ajouter_au_contexte_global(nouveau_contenu):
    CONTEXT = os.getenv("CONTEXT")
    with open(CONTEXT, 'a') as f:
        f.write(f"\n\n{nouveau_contenu}")

def generer_reponse(expediteur, sujet, contenu, model_name):
    try:
        embedding_contenu = generer_embedding(contenu)

        contexte_global = lire_et_mettre_a_jour_contexte()
        contexte_complet = contexte_global

        # Récupérer les embeddings précédents
        chemin_embeddings = os.path.join("./emails/", expediteur, "embeddings.json")
        if os.path.exists(chemin_embeddings):
            with open(chemin_embeddings, 'r') as f:
                embeddings_precedents = json.load(f)

            # Calculer les similarités
            similarites = [cosine_similarity([embedding_contenu], [e["embedding"]])[0][0] for e in embeddings_precedents]

            # Sélectionner les 3 requêtes les plus similaires
            indices_tries = np.argsort(similarites)[::-1][:3]
            requetes_similaires = [embeddings_precedents[i]["contenu"] for i in indices_tries]

            # Ajouter les requêtes similaires au contexte
            contexte_complet += "\n\nRequêtes similaires précédentes:\n" + "\n".join(requetes_similaires)

        # Ajouter le contenu du fichier contextuel spécifique à l'expéditeur
        CONTEXT_FILE = os.path.join("./emails/", expediteur, "context.txt")
        if os.path.exists(CONTEXT_FILE):
            with open(CONTEXT_FILE, 'r') as file:
                contexte_expediteur = file.read()
            contexte_complet += f"\n\nHistorique de conversation avec l'expéditeur:\n{contexte_expediteur}"

        prompt = f"Contexte général et spécifique:\n{contexte_complet}\n\nEmail actuel:\nSujet: {sujet}\nContenu: {contenu}\n\nRéponse:"

        # Générer la réponse
        generate_data = {
            "model": model_name,
            "prompt": prompt,
            "system": "Vous êtes ASTRO, un assistant intelligent qui lit et répond aux messages. Utilisez le contexte fourni pour générer une réponse pertinente. Terminez en signalant que vous êtes un 'assistant numérique' mis au point par le G1FabLab. Signez 'ASTRO, le petit robot.'",
            "stream": False
        }
        response = requests.post(f"{OLLAMA_API_URL}/api/generate", json=generate_data)
        response_json = response.json()

        # Stocker le nouvel embedding
        stocker_embedding(expediteur, contenu, embedding_contenu)

        # Ajouter la nouvelle réponse au contexte global
        ajouter_au_contexte_global(f"Email de {expediteur}: {contenu}\nRéponse: {response_json['response']}")

        return response_json['response']
    except Exception as e:
        logger.error(f"Erreur lors de la génération de la réponse: {str(e)}")
        logger.error(traceback.format_exc())
        return "Désolé, une erreur s'est produite lors de la génération de la réponse."


def traiter_emails_et_appliquer_rag(imap_server, email_address, password, smtp_server, smtp_port, model_name):
    try:
        for sujet, contenu, email_message in lire_emails(imap_server, email_address, password):
            expediteur = email.utils.parseaddr(email_message['From'])[1]
            logger.info(f"Traitement de l'email de {expediteur} avec le sujet: {sujet}")

            reponse_generee = generer_reponse(expediteur, sujet, contenu, model_name)

            creer_fichier_contextuel(expediteur, reponse_generee, contenu)

            envoyer_email(smtp_server, smtp_port, email_address, password, expediteur, sujet, reponse_generee)
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

        traiter_emails_et_appliquer_rag(IMAP_SERVER, EMAIL, PASSWORD, SMTP_SERVER, SMTP_PORT, MODEL)
        logger.info("Fin du processus de traitement des emails")
    except KeyboardInterrupt:
        logger.info("Processus interrompu par l'utilisateur")
    except Exception as e:
        logger.critical(f"Erreur critique: {str(e)}")
        logger.critical(traceback.format_exc())