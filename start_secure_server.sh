#!/bin/bash

# Script de démarrage sécurisé pour UPassport avec protection DOS
echo "🚀 Démarrage sécurisé d'UPassport avec protection DOS..."

# Vérifier que Python3 est installé
if ! command -v python3 &> /dev/null; then
    echo "❌ Python3 n'est pas installé"
    exit 1
fi

# Vérifier les dépendances
echo "📦 Vérification des dépendances..."
python3 -c "import fastapi, uvicorn, pydantic, dotenv, aiofiles, websockets" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "❌ Dépendances manquantes. Installez-les avec:"
    echo "   pip3 install fastapi uvicorn pydantic python-dotenv aiofiles websockets"
    exit 1
fi

# Créer le dossier tmp s'il n'existe pas
mkdir -p tmp

# Vérifier les permissions
echo "🔒 Vérification des permissions..."
if [ ! -r "54321.py" ]; then
    echo "❌ Impossible de lire 54321.py"
    exit 1
fi

# Afficher la configuration de sécurité
echo "🛡️  Configuration de sécurité:"
echo "   - Rate limiting: 12 requêtes/minute par IP"
echo "   - IPs de confiance: 127.0.0.1, ::1, 192.168.1.1"
echo "   - Nettoyage automatique: toutes les 5 minutes"
echo "   - Logging des violations: activé"

# Démarrer le serveur
echo "🌐 Démarrage du serveur sur http://0.0.0.0:54321"
echo "   Appuyez sur Ctrl+C pour arrêter"
echo ""

# Lancer le serveur avec gestion des signaux
trap 'echo ""; echo "🛑 Arrêt du serveur..."; exit 0' INT TERM

python3 54321.py
