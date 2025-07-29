#!/usr/bin/env python3
"""
Script pour vérifier que toutes les routes sont soumises au rate limiting
"""

import requests
import time
import sys
from datetime import datetime

# Configuration
if len(sys.argv) > 1:
    BASE_URL = sys.argv[1].rstrip('/')
else:
    BASE_URL = "http://127.0.0.1:54321"

# Liste complète des routes basée sur l'analyse du code
ROUTES_TO_TEST = [
    # Routes GET
    ("GET", "/"),
    ("GET", "/scan"),
    ("GET", "/nostr"),
    ("GET", "/blog"),
    ("GET", "/g1"),
    ("GET", "/check_balance"),
    ("GET", "/rec"),
    ("GET", "/webcam"),
    ("GET", "/stop"),
    ("GET", "/health"),
    ("GET", "/rate-limit-status"),
    ("GET", "/index"),
    ("GET", "/upload"),
    
    # Routes POST (avec données minimales)
    ("POST", "/g1nostr"),
    ("POST", "/upassport"),
    ("POST", "/ssss"),
    ("POST", "/zen_send"),
    ("POST", "/rec"),
    ("POST", "/ping"),
    ("POST", "/upload2ipfs"),
    ("POST", "/api/upload"),
    ("POST", "/api/upload_from_drive"),
    ("POST", "/api/delete"),
    ("POST", "/api/test-nostr"),
]

def test_route(method, path, test_number):
    """Tester une route et vérifier les headers de rate limiting"""
    try:
        url = f"{BASE_URL}{path}"
        
        # Préparer les données minimales pour les POST
        data = None
        files = None
        headers = {}
        
        if method == "POST":
            if path == "/g1nostr":
                data = {"email": "test@example.com", "lang": "en", "lat": "0", "lon": "0"}
            elif path == "/upassport":
                data = {"parametre": "test"}
            elif path == "/ssss":
                data = {"cardns": "test", "ssss": "test", "zerocard": "test"}
            elif path == "/zen_send":
                data = {"zen": "1", "g1source": "test", "g1dest": "test"}
            elif path == "/rec":
                data = {"player": "test@example.com"}
            elif path == "/ping":
                data = {"test": "data"}
            elif path == "/upload2ipfs":
                # Skip - nécessite un fichier
                return f"  ⏭️  Route {test_number}: {method} {path} - SKIP (nécessite fichier)"
            elif path == "/api/upload":
                # Skip - nécessite un fichier
                return f"  ⏭️  Route {test_number}: {method} {path} - SKIP (nécessite fichier)"
            elif path == "/api/upload_from_drive":
                data = {"ipfs_link": "test", "npub": "test"}
            elif path == "/api/delete":
                data = {"file_path": "test", "npub": "test"}
            elif path == "/api/test-nostr":
                data = {"npub": "test"}
            else:
                data = {"test": "data"}
        
        # Faire la requête
        if method == "GET":
            response = requests.get(url, timeout=5, headers=headers)
        else:
            response = requests.post(url, data=data, files=files, timeout=5, headers=headers)
        
        # Vérifier les headers de rate limiting
        rate_limit_headers = [
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining", 
            "X-RateLimit-Client-IP"
        ]
        
        missing_headers = []
        for header in rate_limit_headers:
            if header not in response.headers:
                missing_headers.append(header)
        
        if missing_headers:
            return f"  ❌ Route {test_number}: {method} {path} - MANQUE: {', '.join(missing_headers)}"
        else:
            remaining = response.headers.get("X-RateLimit-Remaining", "unknown")
            return f"  ✅ Route {test_number}: {method} {path} - OK (Remaining: {remaining})"
            
    except requests.exceptions.RequestException as e:
        return f"  ❌ Route {test_number}: {method} {path} - ERREUR: {str(e)[:50]}"
    except Exception as e:
        return f"  ❌ Route {test_number}: {method} {path} - EXCEPTION: {str(e)[:50]}"

def test_static_files():
    """Tester que les fichiers statiques sont bien exclus du rate limiting"""
    try:
        # Tester un fichier statique (qui devrait être exclu)
        response = requests.get(f"{BASE_URL}/static/test.css", timeout=5)
        
        # Vérifier que les headers de rate limiting ne sont PAS présents
        rate_limit_headers = [
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining", 
            "X-RateLimit-Client-IP"
        ]
        
        present_headers = []
        for header in rate_limit_headers:
            if header in response.headers:
                present_headers.append(header)
        
        if present_headers:
            return f"  ❌ Fichiers statiques: Rate limiting appliqué (ne devrait pas l'être) - Headers: {', '.join(present_headers)}"
        else:
            return f"  ✅ Fichiers statiques: Correctement exclus du rate limiting"
            
    except Exception as e:
        return f"  ⚠️  Fichiers statiques: Erreur de test - {str(e)[:50]}"

def main():
    print("🔍 Vérification de la couverture du rate limiting")
    print("=" * 60)
    print(f"📍 Target: {BASE_URL}")
    print(f"⏰ Début: {datetime.now().isoformat()}")
    print("")
    
    # Vérifier que le serveur répond
    try:
        response = requests.get(f"{BASE_URL}/health", timeout=5)
        if response.status_code != 200:
            print(f"❌ Serveur non accessible (status: {response.status_code})")
            return
        print("✅ Serveur accessible")
    except Exception as e:
        print(f"❌ Impossible de se connecter au serveur: {e}")
        return
    
    print("")
    print("📊 Test de toutes les routes...")
    print("")
    
    results = []
    for i, (method, path) in enumerate(ROUTES_TO_TEST, 1):
        result = test_route(method, path, i)
        results.append(result)
        print(result)
        time.sleep(0.1)  # Petit délai entre les requêtes
    
    print("")
    print("📁 Test des fichiers statiques...")
    print("")
    
    static_result = test_static_files()
    print(static_result)
    
    print("")
    print("📈 Résumé:")
    print("=" * 60)
    
    ok_count = sum(1 for r in results if "✅" in r)
    skip_count = sum(1 for r in results if "⏭️" in r)
    error_count = sum(1 for r in results if "❌" in r)
    
    print(f"✅ Routes OK: {ok_count}")
    print(f"⏭️  Routes skip: {skip_count}")
    print(f"❌ Routes en erreur: {error_count}")
    print(f"📊 Total testées: {len(results)}")
    
    # Vérifier les fichiers statiques
    if "✅" in static_result:
        print("✅ Fichiers statiques: Correctement exclus")
    else:
        print("❌ Fichiers statiques: Problème détecté")
    
    print("")
    print("🔍 Analyse du middleware:")
    print("- Seules les routes commençant par /static sont exclues")
    print("- Toutes les autres routes sont soumises au rate limiting")
    print("- Les IPs de confiance (127.0.0.1, 10.99.99.0/24) ont un accès illimité")
    
    if error_count == 0 and "✅" in static_result:
        print("🎉 Configuration du rate limiting parfaite !")
    else:
        print("⚠️  Des problèmes ont été détectés dans la configuration.")
    
    print("")
    print("📝 Notes:")
    print("- Les routes nécessitant des fichiers sont marquées SKIP")
    print("- Les erreurs peuvent être normales si les données sont invalides")
    print("- L'important est la présence des headers X-RateLimit-*")
    print("- Les fichiers statiques doivent être exclus du rate limiting")

if __name__ == "__main__":
    main() 