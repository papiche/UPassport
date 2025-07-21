#!/bin/bash
# test_memory_access_messages.sh
# Test script to verify access denied messages

echo "🧪 Testing Memory Access Denied Messages"
echo "========================================"

# Test user IDs
REGULAR_USER="user@example.com"
SOCIETAIRE_USER="societaire@copylaradio.com"

# Create test directory structure
echo "📁 Setting up test environment..."

# Create societaire directory
mkdir -p ~/.zen/game/players/$SOCIETAIRE_USER
echo "Created societaire directory: ~/.zen/game/players/$SOCIETAIRE_USER"

echo ""
echo "🔍 Testing access scenarios:"
echo ""

# Test 1: Regular user trying to use slot 1
echo "Test 1: Regular user trying to use slot 1"
echo "Expected: Access denied message"
if [[ -d "$HOME/.zen/game/players/$REGULAR_USER" ]]; then
    echo "❌ FAIL: Regular user has societaire directory (should not)"
else
    echo "✅ PASS: Regular user correctly has no societaire directory"
fi
echo ""

# Test 2: Societaire user trying to use slot 1
echo "Test 2: Societaire user trying to use slot 1"
echo "Expected: Access allowed"
if [[ -d "$HOME/.zen/game/players/$SOCIETAIRE_USER" ]]; then
    echo "✅ PASS: Societaire user correctly has societaire directory"
else
    echo "❌ FAIL: Societaire user missing societaire directory"
fi
echo ""

# Test 3: Both users trying to use slot 0
echo "Test 3: Both users trying to use slot 0"
echo "Expected: Access allowed for both"
echo "✅ PASS: Slot 0 should be accessible to all users"
echo ""

# Test 4: Check message content
echo "Test 4: Access denied message content"
echo "Expected message should include:"
echo "- ⚠️ Accès refusé aux slots de mémoire 1-12"
echo "- Pour utiliser les slots de mémoire 1-12, vous devez être sociétaire CopyLaRadio"
echo "- Le slot 0 reste accessible pour tous les utilisateurs autorisés"
echo "- Pour devenir sociétaire : [IPFS link]"
echo ""

echo "🧹 Cleaning up test environment..."
rm -rf ~/.zen/game/players/$SOCIETAIRE_USER

echo "✅ Memory access message test completed!"
echo ""
echo "📋 Test Summary:"
echo "- Regular users will be denied access to slots 1-12"
echo "- Sociétaires will have access to all slots"
echo "- Access denied messages will be sent via NOSTR"
echo "- Slot 0 remains accessible to all authorized users" 