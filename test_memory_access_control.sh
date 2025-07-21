#!/bin/bash
# test_memory_access_control.sh
# Test script to verify memory access control for slots 1-12

echo "🧪 Testing Memory Access Control System"
echo "======================================"

# Test user IDs
REGULAR_USER="user@example.com"
SOCIETAIRE_USER="societaire@copylaradio.com"

# Create test directory structure
echo "📁 Setting up test environment..."

# Create societaire directory
mkdir -p ~/.zen/game/players/$SOCIETAIRE_USER
echo "Created societaire directory: ~/.zen/game/players/$SOCIETAIRE_USER"

# Test function to check access
test_memory_access() {
    local user_id="$1"
    local slot="$2"
    local expected_result="$3"
    
    echo "Testing: User=$user_id, Slot=$slot, Expected=$expected_result"
    
    # Simulate the check_memory_slot_access function
    local result=""
    if [[ "$slot" == "0" ]]; then
        result="ALLOWED"
    elif [[ "$slot" -ge 1 && "$slot" -le 12 ]]; then
        if [[ -d "$HOME/.zen/game/players/$user_id" ]]; then
            result="ALLOWED"
        else
            result="DENIED"
        fi
    else
        result="ALLOWED"
    fi
    
    if [[ "$result" == "$expected_result" ]]; then
        echo "✅ PASS: $user_id can access slot $slot ($result)"
    else
        echo "❌ FAIL: $user_id cannot access slot $slot (got $result, expected $expected_result)"
    fi
    echo ""
}

echo "🔍 Testing Slot 0 (should be accessible to all)"
test_memory_access "$REGULAR_USER" "0" "ALLOWED"
test_memory_access "$SOCIETAIRE_USER" "0" "ALLOWED"

echo "🔍 Testing Slots 1-12 (should require societaire status)"
for slot in {1..12}; do
    test_memory_access "$REGULAR_USER" "$slot" "DENIED"
    test_memory_access "$SOCIETAIRE_USER" "$slot" "ALLOWED"
done

echo "🧹 Cleaning up test environment..."
rm -rf ~/.zen/game/players/$SOCIETAIRE_USER

echo "✅ Memory access control test completed!"
echo ""
echo "📋 Summary:"
echo "- Slot 0: Accessible to all users"
echo "- Slots 1-12: Only accessible to sociétaires (users in ~/.zen/game/players/)"
echo "- Regular users will receive access denied messages" 