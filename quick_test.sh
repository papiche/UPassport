#!/bin/bash

# Test rapide du service UPassport avec protection DOS
echo "🚀 Test rapide du service UPassport"
echo "=================================="

# Test 1: Service systemd
echo ""
echo "1️⃣  Test du service systemd..."
if sudo systemctl is-active --quiet upassport; then
    echo "   ✅ Service actif"
else
    echo "   ❌ Service inactif"
    echo "   💡 Lancez: ./setup_systemd.sh"
    exit 1
fi

# Test 2: Connectivité API
echo ""
echo "2️⃣  Test de connectivité API..."
if curl -s http://localhost:54321/health > /dev/null 2>&1; then
    echo "   ✅ API répond"
else
    echo "   ❌ API ne répond pas"
    exit 1
fi

# Test 3: Protection DOS
echo ""
echo "3️⃣  Test de la protection DOS..."
status_data=$(curl -s http://localhost:54321/rate-limit-status 2>/dev/null)
if [ $? -eq 0 ]; then
    rate_limit=$(echo "$status_data" | grep -o '"rate_limit":[0-9]*' | cut -d: -f2)
    remaining=$(echo "$status_data" | grep -o '"remaining_requests":[0-9]*' | cut -d: -f2)
    echo "   ✅ Rate limiting actif: $rate_limit req/min"
    echo "   📊 Requêtes restantes: $remaining"
else
    echo "   ❌ Rate limiting non fonctionnel"
    exit 1
fi

# Test 4: Test de rate limiting
echo ""
echo "4️⃣  Test de rate limiting (5 requêtes rapides)..."
for i in {1..5}; do
    response=$(curl -s -w "%{http_code}" http://localhost:54321/health 2>/dev/null)
    http_code="${response: -3}"
    if [ "$http_code" = "200" ]; then
        echo "   ✅ Requête $i: OK"
    elif [ "$http_code" = "429" ]; then
        echo "   🚫 Requête $i: Rate limited"
    else
        echo "   ❌ Requête $i: Erreur $http_code"
    fi
    sleep 0.1
done

# Test 5: Logs
echo ""
echo "5️⃣  Vérification des logs..."
recent_logs=$(sudo journalctl -u upassport -n 3 --no-pager 2>/dev/null)
if [ -n "$recent_logs" ]; then
    echo "   ✅ Logs disponibles"
    echo "   📝 Dernières lignes:"
    echo "$recent_logs" | tail -3 | sed 's/^/      /'
else
    echo "   ⚠️  Aucun log récent"
fi

echo ""
echo "🎉 Tests terminés avec succès!"
echo ""
echo "📋 Commandes utiles:"
echo "   ./monitor_service.sh     # Monitoring en temps réel"
echo "   sudo systemctl status upassport    # Statut du service"
echo "   python3 test_rate_limit.py         # Test complet du rate limiting"
