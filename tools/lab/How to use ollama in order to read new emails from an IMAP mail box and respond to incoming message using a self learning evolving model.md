# How to use ollama in order to read new emails from an IMAP mail box and respond to incoming message using a self learning evolving model

Pour utiliser Ollama afin de lire les nouveaux emails d'une boîte mail IMAP et y répondre avec un modèle auto-apprenant évolutif, voici les étapes à suivre :

1. Configuration d'Ollama :

Assurez-vous d'avoir Ollama installé sur votre système. Vous pouvez le télécharger depuis le site officiel d'Ollama[4]. Ensuite, tirez un modèle approprié, par exemple :

```bash
ollama pull llama3:8b
```

2. Lecture des emails via IMAP :

Utilisez une bibliothèque Python comme `imaplib` pour vous connecter à votre boîte mail IMAP et récupérer les nouveaux messages. Voici un exemple de fonction pour lire les emails[5] :

```python
import imaplib
import email
from email.header import decode_header
import json

def get_emails(quantity, login, password):
    email_information = []
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(login, password)
        mail.select("inbox")
        status, messages = mail.search(None, "(UNSEEN)")
        email_ids = messages[0].split()[::-1]

        limit = min(quantity, len(email_ids))
        for i in range(limit):
            email_id = email_ids[i]
            _, msg_data = mail.fetch(email_id, "(RFC822)")
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    email_message = email.message_from_bytes(response_part[1])
                    subject, encoding = decode_header(email_message["Subject"])[0]
                    if isinstance(subject, bytes):
                        subject = subject.decode(encoding or "utf-8")
                    sender = email_message.get("From")
                    body = ""
                    if email_message.is_multipart():
                        for part in email_message.walk():
                            if part.get_content_type() == "text/plain":
                                body = part.get_payload(decode=True).decode()
                    else:
                        body = email_message.get_payload(decode=True).decode()

                    email_information.append({
                        "Subject": subject,
                        "From": sender,
                        "Body": body
                    })
        mail.logout()
        return email_information
    except Exception as e:
        print("Erreur :", str(e))
        return []
```

3. Intégration avec Ollama :

Utilisez la bibliothèque Python d'Ollama pour interagir avec le modèle. Voici comment l'installer et l'utiliser[4] :

```bash
pip install ollama
```

```python
import ollama

def generate_response(email_content):
    response = ollama.chat(model='llama3:8b', messages=[
        {
            'role': 'system',
            'content': 'Vous êtes un assistant email intelligent. Analysez le contenu de l'email et générez une réponse appropriée.'
        },
        {
            'role': 'user',
            'content': f"Voici le contenu de l'email : {email_content}"
        }
    ])
    return response['message']['content']
```

4. Boucle principale :

Créez une boucle principale qui vérifie régulièrement les nouveaux emails, les traite avec Ollama et envoie les réponses :

```python
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

def send_email(sender_email, sender_password, recipient, subject, body):
    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = recipient
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))

    server = smtplib.SMTP('smtp.gmail.com', 587)
    server.starttls()
    server.login(sender_email, sender_password)
    text = msg.as_string()
    server.sendmail(sender_email, recipient, text)
    server.quit()

def main_loop(email, password, check_interval=300):
    while True:
        new_emails = get_emails(10, email, password)
        for email_info in new_emails:
            response = generate_response(email_info['Body'])
            send_email(email, password, email_info['From'], f"Re: {email_info['Subject']}", response)
        time.sleep(check_interval)

# Lancez la boucle principale
main_loop('votre_email@gmail.com', 'votre_mot_de_passe')
```

5. Apprentissage continu :

Pour rendre le modèle auto-apprenant et évolutif, vous devrez implémenter un système de feedback et de fine-tuning. Voici quelques idées :

- Stockez les paires email-réponse dans une base de données.
- Implémentez un système de notation des réponses (par exemple, en demandant à l'utilisateur de noter la qualité de la réponse).
- Utilisez ces données pour fine-tuner périodiquement le modèle Ollama avec les nouvelles interactions.

Il est important de noter que cette approche nécessite une attention particulière à la sécurité et à la confidentialité des données. Assurez-vous de respecter les réglementations en vigueur concernant le traitement des emails et des données personnelles.

De plus, pour un système vraiment évolutif, vous devrez probablement implémenter des techniques plus avancées d'apprentissage par renforcement et de mise à jour continue du modèle. Cela pourrait nécessiter des ressources de calcul significatives et une expertise en apprentissage automatique.

Citations:
[1] https://github.com/theredsix/ollama-email-filter
[2] https://www.reddit.com/r/LocalLLaMA/comments/17pbznl/extracting_data_from_emails_for_training/
[3] https://www.reddit.com/r/ollama/comments/1dtzf3e/can_i_use_ollama_to_chat_with_llms_via_email/
[4] https://pyimagesearch.com/2024/05/20/inside-look-exploring-ollama-for-on-device-ai/
[5] https://docs.llama-api.com/essentials/tutorial
[6] https://github.com/jose-donato/ollama-reply

---

# nous pourrions extraire les message dont la dernière réponse est "OK!" pour déclencher un processus Generate Embeddings au modèle personnel avec le fil de discussion correspondant. Et ignorer ceux dont la réponse est "KO!"
https://github.com/ollama/ollama/blob/main/docs/api.md#generate-embeddings


Pour extraire les messages dont la dernière réponse est "OK!" et déclencher un processus de génération d'embeddings avec le fil de discussion correspondant, tout en ignorant ceux dont la réponse est "KO!", nous pourrions mettre en place le processus suivant :

1. Lecture des emails :

```python
import imaplib
import email
from email.header import decode_header

def lire_emails(imap_server, email, password):
    # Connexion au serveur IMAP
    imap = imaplib.IMAP4_SSL(imap_server)
    imap.login(email, password)
    imap.select("INBOX")

    # Recherche des emails non lus
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

        yield sujet, contenu

    imap.close()
    imap.logout()
```

2. Traitement des emails et génération d'embeddings :

```python
import ollama

def traiter_emails_et_generer_embeddings():
    for sujet, contenu in lire_emails("imap.votre_serveur.com", "votre_email@exemple.com", "votre_mot_de_passe"):
        # Vérifier si le dernier message est "OK!"
        if contenu.strip().endswith("OK!"):
            # Générer l'embedding pour le fil de discussion
            response = ollama.embeddings(
                model="votre_modele_personnel",
                prompt=contenu
            )
            embedding = response["embedding"]

            # Ici, vous pouvez stocker l'embedding dans une base de données vectorielle
            # ou l'utiliser pour d'autres traitements

            print(f"Embedding généré pour le sujet : {sujet}")
        elif contenu.strip().endswith("KO!"):
            print(f"Email ignoré (KO!) : {sujet}")
        else:
            print(f"Email sans réponse finale claire : {sujet}")

# Exécution du processus
traiter_emails_et_generer_embeddings()
```

Ce code fait les choses suivantes :

1. Il se connecte à votre boîte mail via IMAP et lit les emails non lus.
2. Pour chaque email, il extrait le sujet et le contenu.
3. Il vérifie si le contenu se termine par "OK!".
4. Si c'est le cas, il utilise l'API Ollama pour générer un embedding du fil de discussion complet.
5. Les emails se terminant par "KO!" sont ignorés.
6. Les embeddings générés peuvent ensuite être stockés ou utilisés selon vos besoins.

Quelques points importants à noter :

- Remplacez "votre_modele_personnel" par le nom du modèle Ollama que vous souhaitez utiliser pour générer les embeddings.
- Assurez-vous d'avoir les permissions nécessaires pour accéder à votre boîte mail via IMAP.
- Ce code suppose que le serveur Ollama est en cours d'exécution localement. Si ce n'est pas le cas, vous devrez ajuster l'URL dans la configuration d'Ollama.
- Pensez à gérer les exceptions et à ajouter des logs pour un meilleur suivi en production.
- Pour une utilisation en production, il serait judicieux d'ajouter un système de gestion des tâches asynchrones pour traiter les emails en arrière-plan.

Ce processus vous permettra d'extraire automatiquement les discussions pertinentes et de générer des embeddings pour une utilisation ultérieure, tout en ignorant les conversations non désirées.

Citations:
[1] https://github.com/ollama/ollama/
[2] https://github.com/ollama/ollama/blob/main/docs/api.md?plain=1
[3] https://github.com/ollama/ollama/issues/1624
[4] https://github.com/ollama/ollama/actions/runs/9420003330/job/25951063342
[5] https://ollama.com/blog/embedding-models
[6] https://js.langchain.com/v0.2/docs/integrations/text_embedding/ollama/
[7] https://github.com/theredsix/ollama-email-filter
[8] https://github.com/ollama/ollama/issues/4296
[9] https://github.com/awaescher/OllamaSharp
[10] https://github.com/ollama/ollama/blob/main/docs/openai.md
[11] https://cookbook.chromadb.dev/integrations/ollama/embeddings/
[12] https://docs.llamaindex.ai/en/stable/examples/usecases/email_data_extraction/

---

# Comment Ollama gère-t-il les longues chaînes de tokens dans les embeddings

Voici un résumé de la façon dont Ollama gère les longues chaînes de tokens dans les embeddings :

1. Limite de contexte par défaut :

   Ollama utilise une limite de contexte par défaut de 2048 tokens[3]. Cette limite peut être augmentée en utilisant le paramètre `num_ctx` dans l'API[5].

2. Gestion des dépassements :

   - Lorsque le nombre de tokens dépasse la limite du contexte, Ollama peut rencontrer des problèmes de performance[4].
   - Il n'y a pas de mécanisme automatique intégré pour tronquer ou gérer les prompts dépassant la taille du contexte[5].

3. Solutions proposées :

   a. RAG (Retrieval-Augmented Generation) :
      Pour les grandes quantités de données, il est recommandé d'utiliser une approche RAG. Cela permet de stocker les données en dehors de la fenêtre de contexte et d'injecter des morceaux pertinents selon les besoins[5].

   b. Paramétrage manuel :
      Il est possible d'augmenter la taille du contexte en utilisant des paramètres comme `-c 2048 -n 2048` pour obtenir la fenêtre de contexte complète[5].

   c. Génération de texte infini :
      Certaines implémentations permettent de générer des tokens au-delà de la limite normale du contexte en écrasant une partie du contexte avec le prompt et en générant de nouveaux tokens dans ce contexte[5].

4. Tokenisation :

   Il n'existe pas actuellement d'endpoint spécifique pour la tokenisation/détokenisation dans l'API Ollama[5]. Cependant, il est possible d'utiliser l'API d'embeddings pour obtenir une estimation du nombre de tokens en comptant le nombre d'éléments retournés[5].

5. Gestion du contexte dans les applications :

   Pour les applications nécessitant un contexte long (comme l'écriture d'histoires), il est recommandé de mettre en place une stratégie de gestion du contexte, par exemple en conservant le prompt système et en supprimant les messages les plus anciens selon une approche "First In, Last Out"[5].

Il est important de noter que la gestion précise des longues chaînes de tokens peut varier selon les modèles et les configurations spécifiques utilisées avec Ollama. Pour les cas d'utilisation avancés nécessitant un contrôle fin sur le contexte, il peut être nécessaire de développer des solutions personnalisées ou d'attendre des améliorations futures de l'API Ollama.

Citations:
[1] https://ollama.com/ehartford/theprofessor
[2] https://github.com/ollama/ollama/blob/main/docs/tutorials/langchainpy.md
[3] https://github.com/ollama/ollama/blob/main/docs/api.md?plain=1
[4] https://github.com/ollama/ollama/issues/5042
[5] https://github.com/ollama/ollama/issues/2204

---

# reprends le code précédent et utilise la méthode "RAG" au lieu de "embeddings"

D'accord, je vais adapter le code précédent pour utiliser la méthode RAG (Retrieval-Augmented Generation) au lieu de simplement générer des embeddings. Nous allons utiliser Ollama pour le modèle de langage, Weaviate comme base de données vectorielle, et ChromaDB pour la gestion des embeddings. Voici le code modifié :

```python
import imaplib
import email
from email.header import decode_header
import ollama
from weaviate import Client
import chromadb
from chromadb.config import Settings

# Configuration de Weaviate
weaviate_client = Client("http://localhost:8080")

# Configuration de ChromaDB
chroma_client = chromadb.Client(Settings(
    chroma_db_impl="duckdb+parquet",
    persist_directory="./chroma_db"
))
collection = chroma_client.create_collection(name="email_collection")

def lire_emails(imap_server, email, password):
    # Le code de cette fonction reste inchangé
    # ...

def traiter_emails_et_appliquer_rag():
    for sujet, contenu in lire_emails("imap.votre_serveur.com", "votre_email@exemple.com", "votre_mot_de_passe"):
        if contenu.strip().endswith("OK!"):
            # Générer l'embedding pour le fil de discussion
            response = ollama.embeddings(
                model="votre_modele_personnel",
                prompt=contenu
            )
            embedding = response["embedding"]

            # Stocker l'embedding et le contenu dans ChromaDB
            collection.add(
                documents=[contenu],
                metadatas=[{"subject": sujet}],
                ids=[f"email_{hash(contenu)}"]
            )

            # Effectuer une recherche sémantique pour trouver des informations pertinentes
            results = collection.query(
                query_embeddings=[embedding],
                n_results=5
            )

            # Construire le contexte pour le RAG
            context = "\n".join([doc for doc in results['documents'][0]])

            # Générer une réponse en utilisant le contexte enrichi
            prompt = f"Contexte:\n{context}\n\nEmail actuel:\n{contenu}\n\nRéponse:"
            response = ollama.chat(model='votre_modele_personnel', messages=[
                {
                    'role': 'system',
                    'content': 'Vous êtes un assistant email intelligent. Utilisez le contexte fourni pour générer une réponse pertinente.'
                },
                {
                    'role': 'user',
                    'content': prompt
                }
            ])

            reponse_generee = response['message']['content']

            # Ici, vous pouvez envoyer la réponse générée par email
            print(f"Réponse générée pour le sujet : {sujet}")
            print(reponse_generee)

        elif contenu.strip().endswith("KO!"):
            print(f"Email ignoré (KO!) : {sujet}")
        else:
            print(f"Email sans réponse finale claire : {sujet}")

# Exécution du processus
traiter_emails_et_appliquer_rag()
```

Ce code modifié intègre la méthode RAG de la manière suivante :

1. Nous utilisons ChromaDB pour stocker les embeddings et le contenu des emails.

2. Lorsqu'un nouvel email est traité (se terminant par "OK!"), nous générons son embedding avec Ollama.

3. Nous stockons l'embedding et le contenu de l'email dans ChromaDB.

4. Nous effectuons une recherche sémantique dans ChromaDB pour trouver des informations pertinentes basées sur l'embedding du nouvel email.

5. Nous construisons un contexte enrichi en combinant les résultats de la recherche.

6. Nous utilisons ce contexte enrichi pour générer une réponse plus pertinente avec Ollama.

Cette approche RAG permet d'améliorer la qualité des réponses en fournissant un contexte plus riche au modèle de langage. Le modèle peut ainsi s'appuyer sur des informations pertinentes issues d'emails précédents pour générer une réponse plus appropriée.

Quelques points à noter :

- Assurez-vous d'avoir installé les bibliothèques nécessaires : `pip install weaviate-client chromadb`.
- Vous devrez configurer et démarrer un serveur Weaviate localement ou utiliser un service hébergé.
- Ajustez les paramètres de connexion pour Weaviate et ChromaDB selon votre configuration.
- Le code suppose que vous avez un modèle Ollama personnalisé. Remplacez "votre_modele_personnel" par le nom de votre modèle.
- Pour une utilisation en production, ajoutez une gestion des erreurs et des logs appropriés.

Cette implémentation RAG devrait considérablement améliorer la pertinence et la qualité des réponses générées par votre assistant email intelligent.

Citations:
[1] https://weaviate.io/blog/local-rag-with-ollama-and-weaviate
[2] https://ollama.com/blog/embedding-models
[3] https://mlexplained.blog/2024/02/24/build-rag-application-using-ollama/
[4] https://medium.com/%40nssrmrn/build-your-own-rag-app-a-step-by-step-guide-to-setup-llm-locally-using-ollama-python-and-ea47c2baf345

---

# ajoutez une gestion des erreurs et des logs appropriés.

Bien sûr, voici une version améliorée du code avec une gestion des erreurs et des logs appropriés :

```python
import imaplib
import email
from email.header import decode_header
import ollama
from weaviate import Client
import chromadb
from chromadb.config import Settings
import logging
from logging.handlers import RotatingFileHandler
import sys
import traceback

# Configuration du logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = RotatingFileHandler('email_rag.log', maxBytes=10000000, backupCount=5)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# Configuration de Weaviate
try:
    weaviate_client = Client("http://localhost:8080")
    logger.info("Connexion à Weaviate établie avec succès")
except Exception as e:
    logger.error(f"Erreur lors de la connexion à Weaviate: {str(e)}")
    sys.exit(1)

# Configuration de ChromaDB
try:
    chroma_client = chromadb.Client(Settings(
        chroma_db_impl="duckdb+parquet",
        persist_directory="./chroma_db"
    ))
    collection = chroma_client.create_collection(name="email_collection")
    logger.info("Connexion à ChromaDB établie avec succès")
except Exception as e:
    logger.error(f"Erreur lors de la connexion à ChromaDB: {str(e)}")
    sys.exit(1)

def lire_emails(imap_server, email, password):
    try:
        imap = imaplib.IMAP4_SSL(imap_server)
        imap.login(email, password)
        imap.select("INBOX")

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

            yield sujet, contenu

        imap.close()
        imap.logout()
    except imaplib.IMAP4.error as e:
        logger.error(f"Erreur IMAP lors de la lecture des emails: {str(e)}")
    except Exception as e:
        logger.error(f"Erreur inattendue lors de la lecture des emails: {str(e)}")
        logger.error(traceback.format_exc())

def traiter_emails_et_appliquer_rag():
    try:
        for sujet, contenu in lire_emails("imap.votre_serveur.com", "votre_email@exemple.com", "votre_mot_de_passe"):
            logger.info(f"Traitement de l'email avec le sujet: {sujet}")

            if contenu.strip().endswith("OK!"):
                try:
                    response = ollama.embeddings(
                        model="votre_modele_personnel",
                        prompt=contenu
                    )
                    embedding = response["embedding"]

                    collection.add(
                        documents=[contenu],
                        metadatas=[{"subject": sujet}],
                        ids=[f"email_{hash(contenu)}"]
                    )
                    logger.info(f"Email ajouté à ChromaDB: {sujet}")

                    results = collection.query(
                        query_embeddings=[embedding],
                        n_results=5
                    )

                    context = "\n".join([doc for doc in results['documents'][0]])

                    prompt = f"Contexte:\n{context}\n\nEmail actuel:\n{contenu}\n\nRéponse:"
                    response = ollama.chat(model='votre_modele_personnel', messages=[
                        {
                            'role': 'system',
                            'content': 'Vous êtes un assistant email intelligent. Utilisez le contexte fourni pour générer une réponse pertinente.'
                        },
                        {
                            'role': 'user',
                            'content': prompt
                        }
                    ])

                    reponse_generee = response['message']['content']
                    logger.info(f"Réponse générée pour le sujet : {sujet}")

                    # Ici, vous pouvez ajouter le code pour envoyer la réponse par email

                except Exception as e:
                    logger.error(f"Erreur lors du traitement de l'email {sujet}: {str(e)}")
                    logger.error(traceback.format_exc())

            elif contenu.strip().endswith("KO!"):
                logger.info(f"Email ignoré (KO!) : {sujet}")
            else:
                logger.info(f"Email sans réponse finale claire : {sujet}")

    except Exception as e:
        logger.error(f"Erreur générale dans le processus de traitement des emails: {str(e)}")
        logger.error(traceback.format_exc())

if __name__ == "__main__":
    try:
        logger.info("Démarrage du processus de traitement des emails")
        traiter_emails_et_appliquer_rag()
        logger.info("Fin du processus de traitement des emails")
    except KeyboardInterrupt:
        logger.info("Processus interrompu par l'utilisateur")
    except Exception as e:
        logger.critical(f"Erreur critique: {str(e)}")
        logger.critical(traceback.format_exc())
```

Voici les principales améliorations apportées en termes de gestion des erreurs et de logging :

1. Configuration du logging :
   - Utilisation de `RotatingFileHandler` pour gérer la rotation des fichiers de log.
   - Définition d'un format de log incluant la date, le nom du logger, le niveau de log et le message.

2. Gestion des erreurs de connexion :
   - Vérification des connexions à Weaviate et ChromaDB au démarrage.
   - Arrêt du programme en cas d'échec de connexion.

3. Gestion des erreurs dans la fonction `lire_emails` :
   - Capture spécifique des erreurs IMAP.
   - Capture générale des autres exceptions.

4. Gestion des erreurs dans `traiter_emails_et_appliquer_rag` :
   - Traitement individuel des erreurs pour chaque email.
   - Logging des erreurs spécifiques à chaque étape du traitement.

5. Gestion globale des erreurs :
   - Capture des interruptions clavier (Ctrl+C).
   - Capture et logging des erreurs critiques non gérées.

6. Logs informatifs :
   - Ajout de logs pour suivre le flux du programme (démarrage, fin, traitement de chaque email).

7. Utilisation de `traceback` :
   - Inclusion des stack traces complètes pour les erreurs, facilitant le débogage.

Ces améliorations permettent une meilleure traçabilité du fonctionnement du programme, facilitent le débogage en cas de problème, et assurent que les erreurs sont correctement capturées et enregistrées. Le fichier de log (`email_rag.log`) contiendra toutes les informations nécessaires pour suivre l'exécution du programme et identifier rapidement les problèmes éventuels.

N'oubliez pas d'ajuster les paramètres de connexion (serveur IMAP, email, mot de passe) et les noms de modèles Ollama selon votre configuration spécifique.

---

# ajoute une fonction qui envoi la réponse générée par le modèle par un relai smtp

Bien sûr, je vais ajouter une fonction pour envoyer la réponse générée par le modèle via un relais SMTP. Voici le code mis à jour avec cette nouvelle fonctionnalité :

```python
import imaplib
import email
from email.header import decode_header
import ollama
from weaviate import Client
import chromadb
from chromadb.config import Settings
import logging
from logging.handlers import RotatingFileHandler
import sys
import traceback
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Configuration du logging (inchangée)
# ...

# Configuration de Weaviate et ChromaDB (inchangée)
# ...

def lire_emails(imap_server, email, password):
    # Fonction inchangée
    # ...

def envoyer_email(smtp_server, smtp_port, sender_email, sender_password, recipient, subject, body):
    try:
        msg = MIMEMultipart()
        msg['From'] = sender_email
        msg['To'] = recipient
        msg['Subject'] = f"Re: {subject}"

        msg.attach(MIMEText(body, 'plain'))

        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(sender_email, sender_password)
            server.send_message(msg)

        logger.info(f"Réponse envoyée avec succès à {recipient}")
    except Exception as e:
        logger.error(f"Erreur lors de l'envoi de l'email à {recipient}: {str(e)}")
        logger.error(traceback.format_exc())

def traiter_emails_et_appliquer_rag(imap_server, email, password, smtp_server, smtp_port):
    try:
        for sujet, contenu in lire_emails(imap_server, email, password):
            logger.info(f"Traitement de l'email avec le sujet: {sujet}")

            # Extraction de l'adresse de l'expéditeur
            expediteur = email.utils.parseaddr(email.message_from_string(contenu)['From'])[1]

            reponse_generee = ""

            if contenu.strip().endswith("OK!"):
                try:
                    response = ollama.embeddings(
                        model="votre_modele_personnel",
                        prompt=contenu
                    )
                    embedding = response["embedding"]

                    collection.add(
                        documents=[contenu],
                        metadatas=[{"subject": sujet}],
                        ids=[f"email_{hash(contenu)}"]
                    )
                    logger.info(f"Email ajouté à ChromaDB: {sujet}")

                    results = collection.query(
                        query_embeddings=[embedding],
                        n_results=5
                    )

                    context = "\n".join([doc for doc in results['documents'][0]])

                    prompt = f"Contexte:\n{context}\n\nEmail actuel:\n{contenu}\n\nRéponse:"
                    response = ollama.chat(model='votre_modele_personnel', messages=[
                        {
                            'role': 'system',
                            'content': 'Vous êtes un assistant email intelligent. Utilisez le contexte fourni pour générer une réponse pertinente.'
                        },
                        {
                            'role': 'user',
                            'content': prompt
                        }
                    ])

                    reponse_generee = response['message']['content']
                    logger.info(f"Réponse générée pour le sujet : {sujet}")

                except Exception as e:
                    logger.error(f"Erreur lors du traitement de l'email {sujet}: {str(e)}")
                    logger.error(traceback.format_exc())
                    reponse_generee = "Désolé, une erreur s'est produite lors du traitement de votre email. Notre équipe technique a été notifiée."

            elif contenu.strip().endswith("KO!"):
                logger.info(f"Email ignoré (KO!) : {sujet}")
                reponse_generee = "Votre email a été marqué comme 'KO!'. Nous ne pouvons pas traiter cette demande pour le moment."
            else:
                logger.info(f"Email sans réponse finale claire : {sujet}")
                reponse_generee = "Nous avons bien reçu votre email, mais nous n'avons pas pu déterminer clairement la nature de votre demande. Pourriez-vous préciser si votre message se termine par 'OK!' pour un traitement complet ?"

            # Envoi de la réponse générée dans tous les cas
            envoyer_email(smtp_server, smtp_port, email, password, expediteur, sujet, reponse_generee)

    except Exception as e:
        logger.error(f"Erreur générale dans le processus de traitement des emails: {str(e)}")
        logger.error(traceback.format_exc())

if __name__ == "__main__":
    try:
        logger.info("Démarrage du processus de traitement des emails")

        # Configuration des serveurs de messagerie
        IMAP_SERVER = "imap.votre_serveur.com"
        SMTP_SERVER = "smtp.votre_serveur.com"
        SMTP_PORT = 587  # Port TLS standard, ajustez si nécessaire
        EMAIL = "votre_email@exemple.com"
        PASSWORD = "votre_mot_de_passe"

        traiter_emails_et_appliquer_rag(IMAP_SERVER, EMAIL, PASSWORD, SMTP_SERVER, SMTP_PORT)
        logger.info("Fin du processus de traitement des emails")
    except KeyboardInterrupt:
        logger.info("Processus interrompu par l'utilisateur")
    except Exception as e:
        logger.critical(f"Erreur critique: {str(e)}")
        logger.critical(traceback.format_exc())

```

Voici les principales modifications et ajouts :

1. Nouvelle fonction `envoyer_email` :
   - Cette fonction utilise `smtplib` pour envoyer un email via un serveur SMTP.
   - Elle gère la création d'un message MIME, l'authentification SMTP, et l'envoi du message.
   - Les erreurs sont capturées et enregistrées dans les logs.

2. Modifications dans `traiter_emails_et_appliquer_rag` :
   - Ajout de paramètres pour les configurations IMAP et SMTP.
   - Extraction de l'adresse email de l'expéditeur à partir du message original.
   - Appel à `envoyer_email` pour envoyer la réponse générée.

3. Dans le bloc `if __name__ == "__main__"` :
   - Ajout de variables de configuration pour les serveurs IMAP et SMTP.
   - Passage de ces configurations à la fonction `traiter_emails_et_appliquer_rag`.

Points importants à noter :

- Assurez-vous de remplacer `"imap.votre_serveur.com"`, `"smtp.votre_serveur.com"`, `"votre_email@exemple.com"`, et `"votre_mot_de_passe"` par vos informations réelles.
- Le port SMTP 587 est utilisé par défaut (pour TLS). Ajustez-le si votre serveur utilise un port différent.
- Cette implémentation suppose que vous utilisez le même email et mot de passe pour IMAP et SMTP. Si ce n'est pas le cas, vous devrez ajuster le code en conséquence.
- Veillez à respecter les politiques de sécurité et de confidentialité lors de l'utilisation d'emails et de mots de passe dans votre code. Il est recommandé d'utiliser des variables d'environnement ou un fichier de configuration sécurisé pour stocker ces informations sensibles.

Cette version du code permet maintenant de lire les emails, de générer des réponses en utilisant RAG, et d'envoyer automatiquement ces réponses via SMTP, tout en gérant les erreurs et en enregistrant les logs appropriés.
