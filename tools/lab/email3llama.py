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
import time
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

def extraire_dataset():
    try:
        logger.info("Début de l'extraction du dataset")
        imap = imaplib.IMAP4_SSL(os.getenv("IMAP_SERVER"))
        logger.info("Connexion IMAP établie")
        imap.login(os.getenv("EMAIL"), os.getenv("PASSWORD"))
        logger.info("Connexion réussie")

        # Lister tous les dossiers
        logger.info("Listing des dossiers de la boîte email:")
        status, folder_list = imap.list()
        if status == "OK":
            for folder in folder_list:
                folder_parts = folder.decode().split()
                if len(folder_parts) >= 2:
                    folder_name = folder_parts[-1]
                    logger.info(f"Dossier trouvé: {folder_name}")
        else:
            logger.error("Impossible de lister les dossiers")

        dataset = []

        # Sélectionner le dossier INBOX
        status, messages = imap.select("INBOX")
        if status != "OK":
            logger.error(f"Impossible de sélectionner le dossier INBOX: {messages}")
            imap.logout()
            return
        logger.info("Dossier INBOX sélectionné avec succès")

        # Rechercher tous les messages reçus
        logger.info("Recherche de tous les messages")
        status, message_numbers = imap.search(None, "ALL")
        if status != "OK":
            logger.error(f"Erreur lors de la recherche des messages reçus: {message_numbers}")
            imap.logout()
            return
        logger.info(f"Nombre de messages trouvés: {len(message_numbers[0].split())}")

        for num in message_numbers[0].split():
            logger.info(f"Traitement du message numéro {num}")
            status, msg_data = imap.fetch(num, "(RFC822)")
            if status != "OK":
                logger.error(f"Erreur lors de la récupération du message reçu {num}: {msg_data}")
                continue

            email_recu = email.message_from_bytes(msg_data[0][1])
            id_message = email_recu["Message-ID"]
            logger.info(f"Message-ID: {id_message}")

            # Extraire le contenu de l'email reçu
            contenu_recu = get_email_content(email_recu)

            # Chercher la réponse correspondante dans le dossier Sent
            status, _ = imap.select('"INBOX.Sent"')
            if status != "OK":
                logger.error("Impossible de sélectionner le dossier Sent Mail")
                imap.select("INBOX")  # Retour à INBOX
                continue
            logger.info("Dossier Sent Mail sélectionné avec succès")

            if id_message:
                logger.info(f"Recherche de la réponse pour Message-ID: {id_message}")
                # Nettoyer et échapper l'ID du message
                cleaned_id = id_message.strip().replace('"', '\\"')
                search_criteria = f'(OR (HEADER "In-Reply-To" "{cleaned_id}") (HEADER "X-Original-Message-ID" "{cleaned_id}"))'
                status, response_numbers = imap.search(None, search_criteria)
            else:
                logger.info("Message-ID manquant, impossible de rechercher la réponse")
                imap.select("INBOX")  # Retour à INBOX
                continue

            if status != "OK" or not response_numbers[0]:
                # logger.info(f"Aucune réponse trouvée pour le message {num}")
                imap.select("INBOX")  # Retour à INBOX
                continue

            logger.info(f"Réponse trouvée, récupération du contenu")
            status, response_data = imap.fetch(response_numbers[0].split()[-1], "(RFC822)")
            if status != "OK":
                logger.error(f"Erreur lors de la récupération de la réponse: {status}")
                imap.select("INBOX")  # Retour à INBOX
                continue

            email_reponse = email.message_from_bytes(response_data[0][1])

            # Extraire le contenu de l'email de réponse
            contenu_reponse = get_email_content(email_reponse)

            # Appel à creer_fichier_contextuel ici
            creer_fichier_contextuel(email.utils.parseaddr(email_recu["From"])[1], contenu_reponse, contenu_recu)

            dataset.append({
                "input": contenu_recu,
                "output": contenu_reponse
            })
            logger.info(f"Paire d'emails ajoutée au dataset")

            # Retour à INBOX pour le prochain message
            imap.select("INBOX")

        imap.close()
        imap.logout()
        logger.info("Déconnexion IMAP effectuée")

        # Sauvegarder le dataset au format JSON
        with open("email_dataset.json", "w") as f:
            json.dump(dataset, f)

        logger.info(f"Dataset créé avec {len(dataset)} paires d'emails")

    except Exception as e:
        logger.error(f"Erreur lors de l'extraction du dataset: {str(e)}")
        logger.error(traceback.format_exc())


def get_email_content(email_message):
    if email_message.is_multipart():
        for part in email_message.walk():
            if part.get_content_type() == "text/plain":
                return part.get_payload(decode=True).decode()
    else:
        return email_message.get_payload(decode=True).decode()


def sauvegarder_brouillon(imap_server, email_address, password, recipient, subject, body, original_message_id):
    max_retries = 3
    for attempt in range(max_retries):
        try:
            imap = imaplib.IMAP4_SSL(imap_server)
            imap.login(email_address, password)

            msg = MIMEMultipart()
            msg['From'] = email_address
            msg['To'] = recipient
            msg['Subject'] = f"Re: {subject}"
            msg['In-Reply-To'] = original_message_id
            msg['References'] = original_message_id
            msg['X-Original-Message-ID'] = original_message_id
            msg.attach(MIMEText(body, 'plain'))

            # Sélectionner le dossier INBOX.Drafts
            status, _ = imap.select('"INBOX.Drafts"')
            if status != 'OK':
                logger.error(f"Impossible de sélectionner le dossier INBOX.Drafts: {status}")
                imap.logout()
                continue

            # Ajouter le brouillon
            status, _ = imap.append('"INBOX.Drafts"', '\\Draft', imaplib.Time2Internaldate(time.time()), msg.as_bytes())
            if status == 'OK':
                logger.info(f"Brouillon sauvegardé pour {recipient}")
                imap.logout()
                return
            else:
                logger.error(f"Échec de la sauvegarde du brouillon: {status}")

            imap.logout()
        except Exception as e:
            logger.error(f"Tentative {attempt+1}/{max_retries} - Erreur lors de la sauvegarde du brouillon: {str(e)}")
            if attempt == max_retries - 1:
                logger.error("Échec de la sauvegarde du brouillon après plusieurs tentatives")
            time.sleep(2)  # Attendre 2 secondes avant de réessayer


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

def charger_dataset_embeddings():
    with open("email_dataset.json", "r") as f:
        dataset = json.load(f)

    dataset_embeddings = []
    for item in dataset:
        input_embedding = generer_embedding(item["input"])
        output_embedding = generer_embedding(item["output"])
        dataset_embeddings.append({
            "input": item["input"],
            "output": item["output"],
            "input_embedding": input_embedding,
            "output_embedding": output_embedding
        })

    return dataset_embeddings


def generer_embedding(texte):
    embedding_data = {
        "model": MODEL,
        "prompt": texte
    }
    response = requests.post(f"{OLLAMA_API_URL}/api/embeddings", json=embedding_data)
    embedding = response.json().get("embedding")
    if not embedding:
        raise ValueError(f"Embedding vide généré pour le texte: {texte[:50]}...")
    return embedding

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
        contexte_pertinent = lire_et_mettre_a_jour_contexte()

        # Générer l'embedding pour le contenu de l'email actuel
        email_embedding = generer_embedding(contenu)

        # Trouver les entrées les plus similaires dans le dataset
        similarites = [cosine_similarity([email_embedding], [item["input_embedding"]])[0][0] for item in dataset_embeddings]
        indices_tries = np.argsort(similarites)[::-1][:5]  # Prendre les 5 plus similaires

        exemples_similaires = [dataset_embeddings[i]["output"] for i in indices_tries]

        # Construire le prompt avec le contexte pertinent et les exemples similaires
        prompt = f"Contexte pertinent:\n{contexte_pertinent}\n\n"
        prompt += "Exemples de réponses similaires:\n"
        for exemple in exemples_similaires:
            prompt += f"{exemple}\n\n"
        prompt += f"EMAIL ACTUEL:\nExpéditeur: {expediteur}\nSujet: {sujet}\nContenu: {contenu}\n\nRéponse:"

        # Générer la réponse
        generate_data = {
            "model": model_name,
            "prompt": prompt,
            "system": f"Tu es ASTRO, un assistant intelligent qui lit et répond aux messages de la boite email de {expediteur}. Utilise le contexte fourni pour générer une réponse pertinente. Ne formule aucune analyse préalable, donne juste une réponse synthétique à EMAIL ACTUEL",
            "stream": False
        }

        logger.info(f"PROMPT {prompt}")

        response = requests.post(f"{OLLAMA_API_URL}/api/generate", json=generate_data)
        response_json = response.json()

        # Stocker le nouvel embedding
        stocker_embedding(expediteur, contenu, email_embedding)

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
            original_message_id = email_message['Message-ID']
            logger.info(f"Traitement de l'email {original_message_id} de {expediteur} avec le sujet: {sujet}")

            reponse_generee = generer_reponse(expediteur, sujet, contenu, model_name)

            sauvegarder_brouillon(imap_server, email_address, password, expediteur, sujet, reponse_generee, original_message_id)

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

        global dataset_embeddings

        dataset_embeddings = charger_dataset_embeddings()
        logger.info(f"Dataset chargé avec {len(dataset_embeddings)} entrées")

        traiter_emails_et_appliquer_rag(IMAP_SERVER, EMAIL, PASSWORD, SMTP_SERVER, SMTP_PORT, MODEL)
        logger.info("=============== Fin du processus de traitement des emails... ====================")

        logger.info(f"Mise à jour DATASET pour la BAL {EMAIL}")
        extraire_dataset()

    except KeyboardInterrupt:
        logger.info("Processus interrompu par l'utilisateur")
    except Exception as e:
        logger.critical(f"Erreur critique: {str(e)}")
        logger.critical(traceback.format_exc())
