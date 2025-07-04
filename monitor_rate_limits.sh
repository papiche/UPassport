#!/bin/bash

# Script de monitoring en temps réel du rate limiting
echo "📊 Monitoring du rate limiting UPassport"
echo "Appuyez sur Ctrl+C pour arrêter"
echo ""

# Configuration
BASE_URL="http://127.0.0.1:54321"
REFRESH_INTERVAL=5  # Rafraîchir toutes les 5 secondes

# Fonction pour afficher le statut
show_status() {
    clear
    echo "📊 Monitoring du rate limiting UPassport - $(date)"
    echo "=" * 60
    echo ""
    
    # Statut de santé
    echo "🏥 Statut du serveur:"
    if curl -s "$BASE_URL/health" > /dev/null 2>&1; then
        echo "   ✅ Serveur en ligne"
    else
        echo "   ❌ Serveur hors ligne"
        return
    fi
    
    # Statistiques du rate limiter
    echo ""
    echo "📈 Statistiques du rate limiter:"
    health_data=$(curl -s "$BASE_URL/health" 2>/dev/null)
    if [ $? -eq 0 ]; then
        active_ips=$(echo "$health_data" | grep -o '"active_ips":[0-9]*' | cut -d: -f2)
        rate_limit=$(echo "$health_data" | grep -o '"rate_limit":[0-9]*' | cut -d: -f2)
        window_seconds=$(echo "$health_data" | grep -o '"window_seconds":[0-9]*' | cut -d: -f2)
        
        echo "   IPs actives: $active_ips"
        echo "   Limite: $rate_limit requêtes/minute"
        echo "   Fenêtre: $window_seconds secondes"
    else
        echo "   ❌ Impossible de récupérer les statistiques"
    fi
    
    # Statut personnel
    echo ""
    echo "👤 Mon statut:"
    status_data=$(curl -s "$BASE_URL/rate-limit-status" 2>/dev/null)
    if [ $? -eq 0 ]; then
        client_ip=$(echo "$status_data" | grep -o '"client_ip":"[^"]*"' | cut -d'"' -f4)
        remaining=$(echo "$status_data" | grep -o '"remaining_requests":[0-9]*' | cut -d: -f2)
        is_blocked=$(echo "$status_data" | grep -o '"is_blocked":[^,]*' | cut -d: -f2)
        reset_time=$(echo "$status_data" | grep -o '"reset_time_iso":"[^"]*"' | cut -d'"' -f4)
        
        echo "   Mon IP: $client_ip"
        echo "   Requêtes restantes: $remaining"
        if [ "$is_blocked" = "true" ]; then
            echo "   🔒 Statut: BLOQUÉ"
            if [ "$reset_time" != "null" ]; then
                echo "   ⏰ Reset: $reset_time"
            fi
        else
            echo "   ✅ Statut: AUTORISÉ"
        fi
    else
        echo "   ❌ Impossible de récupérer mon statut"
    fi
    
    # Logs récents (dernières 10 lignes)
    echo ""
    echo "📝 Logs récents:"
    log_file="$HOME/.zen/tmp/54321.log"
    if [ -f "$log_file" ]; then
        tail -10 "$log_file" 2>/dev/null | grep -E "(Rate limit|Trusted IP|cleanup)" || echo "   Aucun log de rate limiting récent"
    else
        echo "   Fichier de log non trouvé: $log_file"
        echo "   Création du fichier de log..."
        mkdir -p "$HOME/.zen/tmp"
        touch "$log_file"
    fi
    
    echo ""
    echo "🔄 Rafraîchissement dans $REFRESH_INTERVAL secondes... (Ctrl+C pour arrêter)"
}

# Boucle principale
trap 'echo ""; echo "🛑 Monitoring arrêté"; exit 0' INT TERM

while true; do
    show_status
    sleep $REFRESH_INTERVAL
done
