#!/usr/bin/env python3
"""
Test agressif du rate limiting - déclenche vraiment les limites
"""

import requests
import time
import threading
from datetime import datetime
import sys

# Configuration
if len(sys.argv) > 1:
    BASE_URL = sys.argv[1].rstrip('/')
else:
    BASE_URL = "http://127.0.0.1:54321"

def make_request(thread_id, request_count):
    """Faire une requête et retourner le résultat"""
    try:
        response = requests.get(f"{BASE_URL}/health", timeout=2)
        remaining = response.headers.get("X-RateLimit-Remaining", "unknown")
        status = response.status_code
        
        if status == 429:
            return f"Thread {thread_id}, Request {request_count}: 🚫 RATE LIMITED (429)"
        elif status == 200:
            return f"Thread {thread_id}, Request {request_count}: ✅ Success (Remaining: {remaining})"
        else:
            return f"Thread {thread_id}, Request {request_count}: ❌ Status {status}"
    except Exception as e:
        return f"Thread {thread_id}, Request {request_count}: ❌ Error - {e}"

def worker(thread_id, num_requests):
    """Worker thread pour faire des requêtes"""
    results = []
    for i in range(num_requests):
        result = make_request(thread_id, i + 1)
        results.append(result)
        time.sleep(0.01)  # Très rapide
    return results

def main():
    print("🚀 Test Agressif du Rate Limiting")
    print("=" * 50)
    print(f"📍 Target: {BASE_URL}")
    print(f"⏰ Début: {datetime.now().isoformat()}")
    print("")
    
    # Test 1: Requêtes séquentielles rapides
    print("📊 Test 1: Requêtes séquentielles rapides (15 requêtes)")
    for i in range(15):
        result = make_request(1, i + 1)
        print(f"  {result}")
        time.sleep(0.05)  # Très rapide
    
    print("")
    
    # Test 2: Requêtes concurrentes
    print("📊 Test 2: Requêtes concurrentes (3 threads, 10 requêtes chacun)")
    threads = []
    all_results = []
    
    for i in range(3):
        thread = threading.Thread(target=lambda tid=i: all_results.extend(worker(tid + 1, 10)))
        threads.append(thread)
        thread.start()
    
    # Attendre que tous les threads terminent
    for thread in threads:
        thread.join()
    
    # Afficher les résultats
    for result in all_results:
        print(f"  {result}")
    
    print("")
    
    # Test 3: Vérifier le statut après
    print("📊 Test 3: Statut après les tests")
    try:
        response = requests.get(f"{BASE_URL}/rate-limit-status", timeout=5)
        if response.status_code == 200:
            data = response.json()
            print(f"  IP: {data.get('client_ip')}")
            print(f"  Requêtes restantes: {data.get('remaining_requests')}")
            print(f"  Bloqué: {data.get('is_blocked')}")
        else:
            print(f"  ❌ Erreur: {response.status_code}")
    except Exception as e:
        print(f"  ❌ Erreur: {e}")
    
    print("")
    print("🏁 Test terminé!")

if __name__ == "__main__":
    main()
