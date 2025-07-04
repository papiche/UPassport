# Protection DOS - UPassport API

## Vue d'ensemble

Ce système protège l'API UPassport contre les attaques de déni de service (DOS) en limitant le nombre de requêtes par adresse IP.

## Configuration

### Paramètres de rate limiting

```python
RATE_LIMIT_REQUESTS = 12  # Maximum requests per minute
RATE_LIMIT_WINDOW = 60    # Time window in seconds (1 minute)
RATE_LIMIT_CLEANUP_INTERVAL = 300  # Cleanup old entries every 5 minutes
```

### IPs de confiance

Les IPs suivantes sont exemptées du rate limiting :

```python
TRUSTED_IPS = {
    "127.0.0.1",      # localhost
    "::1",            # localhost IPv6
    "192.168.1.1",    # Example: your router
    # Add more trusted IPs as needed
}

# Trusted IP ranges (CIDR)
TRUSTED_IP_RANGES = [
    "10.99.99.0/24",  # WireGuard VPN range
]
```

### Fonction de vérification des IPs de confiance

Le système utilise une fonction `is_trusted_ip(ip)` qui vérifie :
- Les correspondances exactes dans `TRUSTED_IPS`
- Les ranges CIDR dans `TRUSTED_IP_RANGES`

## Fonctionnalités

### 1. Rate Limiting par IP
- **Limite** : 12 requêtes par minute par adresse IP
- **Fenêtre glissante** : 60 secondes
- **Nettoyage automatique** : Suppression des anciennes entrées toutes les 5 minutes

### 2. Détection d'IP réelle
Le système détecte automatiquement l'IP réelle du client en vérifiant :
- `X-Forwarded-For` (pour les proxies)
- `X-Real-IP` (pour les load balancers)
- IP directe (fallback)

### 3. Headers de réponse
Chaque réponse inclut des headers informatifs :
```
X-RateLimit-Limit: 12
X-RateLimit-Remaining: 8
X-RateLimit-Reset: 1640995200
X-RateLimit-Client-IP: 192.168.1.100
```

**Note** : Pour les IPs de confiance, `X-RateLimit-Remaining` affiche `∞`.

### 4. Gestion des erreurs
- **HTTP 429** : Rate limit dépassé
- **Message détaillé** : Temps d'attente et informations de reset
- **Logging** : Toutes les violations sont loggées

## Endpoints de surveillance

### `/health`
Vérification de l'état du serveur (soumis au rate limiting pour les tests)

```json
{
  "status": "healthy",
  "timestamp": "2024-01-01T12:00:00",
  "rate_limiter_stats": {
    "active_ips": 5,
    "rate_limit": 12,
    "window_seconds": 60
  }
}
```

### `/rate-limit-status`
Statut du rate limiting pour l'IP du client

```json
{
  "client_ip": "192.168.1.100",
  "remaining_requests": 8,
  "rate_limit": 12,
  "window_seconds": 60,
  "reset_time": 1640995200,
  "reset_time_iso": "2024-01-01T12:01:00",
  "is_blocked": false
}
```

## Tests

### Exécuter les tests

**Test local (défaut) :**
```bash
python3 test_rate_limit.py
```

**Test sur serveur distant :**
```bash
python3 test_rate_limit.py https://u.copylaradio.com
```

### Tests inclus
1. **Requêtes normales** : Vérification du fonctionnement normal
2. **Dépassement de limite** : Test des erreurs 429
3. **Reset automatique** : Vérification de la réinitialisation
4. **Statut du rate limiting** : Test de l'endpoint de surveillance
5. **IPs de confiance** : Vérification des exemptions

### Test agressif
```bash
python3 test_rate_limit_aggressive.py
```
Ce script force le dépassement de limite avec des requêtes concurrentes.

## Monitoring et logs

### Logs automatiques
- Violations de rate limiting
- Nettoyage des anciennes entrées
- IPs de confiance détectées

### Exemple de log
```
2024-01-01 12:00:00 - WARNING - Rate limit exceeded for IP 192.168.1.100: 12 requests per minute limit
2024-01-01 12:05:00 - INFO - Rate limiter cleanup: removed 3 IPs, 5 active IPs
2024-01-01 12:00:30 - INFO - Trusted IP 10.99.99.5 - skipping rate limiting
```

### Monitoring en temps réel
```bash
./monitor_rate_limits.sh
```

## Personnalisation

### Modifier les limites
```python
# Dans 54321.py
RATE_LIMIT_REQUESTS = 20  # Augmenter à 20 requêtes/minute
RATE_LIMIT_WINDOW = 120   # Fenêtre de 2 minutes
```

### Ajouter des IPs de confiance individuelles
```python
TRUSTED_IPS = {
    "127.0.0.1",
    "::1",
    "192.168.1.1",
    "10.0.0.5",    # Nouvelle IP de confiance
    "172.16.0.10"  # Autre IP de confiance
}
```

### Ajouter des ranges CIDR
```python
TRUSTED_IP_RANGES = [
    "10.99.99.0/24",  # Range WireGuard
    "192.168.0.0/16", # Range réseau local
    "172.16.0.0/12",  # Range privé
]
```

### Exclure des endpoints
Modifier le middleware pour exclure certains endpoints :
```python
if request.url.path.startswith("/static") or request.url.path in ["/api/public"]:
    response = await call_next(request)
    return response
```

## Sécurité

### Protection contre les contournements
- Détection d'IP réelle (pas de spoofing via headers)
- Fenêtre glissante (pas de contournement par timing)
- Nettoyage automatique (pas d'accumulation mémoire)
- Support des ranges CIDR pour les VPNs

### Limitations
- Rate limiting en mémoire (perdu au redémarrage)
- Pas de persistance des violations
- Pas de blacklist permanente

## Dépannage

### Problèmes courants

1. **Rate limiting trop strict**
   - Augmenter `RATE_LIMIT_REQUESTS`
   - Ajouter l'IP à `TRUSTED_IPS` ou le range à `TRUSTED_IP_RANGES`

2. **IPs incorrectes détectées**
   - Vérifier la configuration proxy/load balancer
   - Ajuster `get_client_ip()` si nécessaire

3. **Mémoire excessive**
   - Réduire `RATE_LIMIT_CLEANUP_INTERVAL`
   - Vérifier les logs de nettoyage

4. **IPs de confiance non reconnues**
   - Vérifier la syntaxe des ranges CIDR
   - Tester avec `is_trusted_ip()` directement

### Debug
```bash
# Vérifier le statut du rate limiting
curl http://localhost:54321/rate-limit-status

# Vérifier la santé du serveur
curl http://localhost:54321/health

# Tester le rate limiting local
python3 test_rate_limit.py

# Tester sur serveur distant
python3 test_rate_limit.py https://u.copylaradio.com

# Monitoring en temps réel
./monitor_rate_limits.sh
```

## Intégration avec WireGuard

Le système supporte automatiquement les IPs du range WireGuard `10.99.99.0/24`. Pour ajouter d'autres ranges VPN :

1. Ajouter le range CIDR dans `TRUSTED_IP_RANGES`
2. Redémarrer le serveur
3. Les IPs du range seront automatiquement exemptées du rate limiting

### Exemple d'ajout de range WireGuard
```python
TRUSTED_IP_RANGES = [
    "10.99.99.0/24",  # Range WireGuard principal
    "10.99.100.0/24", # Range WireGuard secondaire
]
```

# Test local (défaut)
python3 test_rate_limit_aggressive.py

# Test sur serveur distant
python3 test_rate_limit_aggressive.py https://u.copylaradio.com

# Test sur autre serveur
python3 test_rate_limit_aggressive.py https://u.example.com (respect UPlanet DNS subdomain nomenclature)
