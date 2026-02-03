#!/bin/bash
# Test script for /ping analytics endpoint
# Tests the analytics endpoint at http://127.0.0.1:54321/ping
# Usage: test_analytics_ping.sh [--del] [--force]
#   --del: Delete test events after verification
#   --force: Skip confirmation when using --del

set -e

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Parse arguments
DELETE_MODE=false
FORCE_MODE=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --del)
            DELETE_MODE=true
            shift
            ;;
        --force)
            FORCE_MODE=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [--del] [--force]"
            echo "  --del: Delete test events after verification"
            echo "  --force: Skip confirmation when using --del"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

# Configuration
PING_URL="http://127.0.0.1:54321/ping"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%S.000Z")
TEST_START_TIME=$(date +%s)  # Unix timestamp for filtering events
NOSTR_GET_EVENTS="${HOME}/.zen/Astroport.ONE/tools/nostr_get_events.sh"

# Check if nostr_get_events.sh exists
if [[ ! -f "$NOSTR_GET_EVENTS" ]]; then
    echo -e "${YELLOW}⚠️  nostr_get_events.sh not found at $NOSTR_GET_EVENTS${NC}"
    echo -e "${YELLOW}   Verification step will be skipped${NC}"
    NOSTR_GET_EVENTS=""
fi

echo -e "${YELLOW}🧪 Testing /ping analytics endpoint${NC}"
echo "URL: $PING_URL"
echo "Test start time: $(date -u -d "@$TEST_START_TIME" +"%Y-%m-%dT%H:%M:%S.000Z" 2>/dev/null || date -u -r "$TEST_START_TIME" +"%Y-%m-%dT%H:%M:%S.000Z" 2>/dev/null || date -u +"%Y-%m-%dT%H:%M:%S.000Z")"
if [[ "$DELETE_MODE" == "true" ]]; then
    echo -e "${YELLOW}⚠️  Delete mode enabled - test events will be deleted after verification${NC}"
fi
echo ""

# Test 1: Basic page view analytics
echo -e "${GREEN}Test 1: Basic page view analytics${NC}"
curl -X POST "$PING_URL" \
  -H "Content-Type: application/json" \
  -H "Referer: https://u.copylaradio.com/test" \
  -d "{
    \"type\": \"page_view\",
    \"source\": \"web\",
    \"timestamp\": \"$TIMESTAMP\",
    \"current_url\": \"https://u.copylaradio.com/test\",
    \"user_agent\": \"Mozilla/5.0 (Test Script)\",
    \"viewport\": {
      \"width\": 1920,
      \"height\": 1080
    },
    \"referer\": \"https://example.com\"
  }" \
  -w "\nHTTP Status: %{http_code}\n" \
  -s | jq '.' 2>/dev/null || echo "Response received (not JSON)"
echo ""

# Test 2: Theater video view analytics
echo -e "${GREEN}Test 2: Theater video view analytics${NC}"
curl -X POST "$PING_URL" \
  -H "Content-Type: application/json" \
  -H "Referer: https://u.copylaradio.com/theater?video=test123" \
  -d "{
    \"type\": \"theater_video_view\",
    \"source\": \"web\",
    \"timestamp\": \"$TIMESTAMP\",
    \"video_event_id\": \"c7891113b80978e2e3a820ca7108a31fbb184882f75926e2d44e64e73cfad097\",
    \"video_title\": \"Test Video Title\",
    \"video_author\": \"abc123def456abc123def456abc123def456abc123def456abc123def456abc1\",
    \"video_kind\": 21,
    \"video_duration\": 120,
    \"video_channel\": \"test-channel\",
    \"video_source_type\": \"webcam\",
    \"current_url\": \"https://u.copylaradio.com/theater?video=c7891113b80978e2e3a820ca7108a31fbb184882f75926e2d44e64e73cfad097\"
  }" \
  -w "\nHTTP Status: %{http_code}\n" \
  -s | jq '.' 2>/dev/null || echo "Response received (not JSON)"
echo ""

# Test 3: Theater page view analytics (server-side)
echo -e "${GREEN}Test 3: Theater page view analytics (server-side)${NC}"
curl -X POST "$PING_URL" \
  -H "Content-Type: application/json" \
  -H "Referer: https://u.copylaradio.com/theater" \
  -d "{
    \"type\": \"theater_page_view\",
    \"source\": \"server\",
    \"timestamp\": \"$TIMESTAMP\",
    \"video_event_id\": \"c7891113b80978e2e3a820ca7108a31fbb184882f75926e2d44e64e73cfad097\",
    \"video_title\": \"Test Video Title\",
    \"video_author\": \"abc123def456abc123def456abc123def456abc123def456abc123def456abc1\",
    \"video_kind\": 21,
    \"video_duration\": 0,
    \"video_channel\": \"test-channel\",
    \"video_source_type\": \"webcam\",
    \"has_javascript\": false,
    \"current_url\": \"https://u.copylaradio.com/theater?video=c7891113b80978e2e3a820ca7108a31fbb184882f75926e2d44e64e73cfad097\"
  }" \
  -w "\nHTTP Status: %{http_code}\n" \
  -s | jq '.' 2>/dev/null || echo "Response received (not JSON)"
echo ""

# Test 4: MULTIPASS card usage analytics
echo -e "${GREEN}Test 4: MULTIPASS card usage analytics${NC}"
curl -X POST "$PING_URL" \
  -H "Content-Type: application/json" \
  -H "Referer: https://u.copylaradio.com/scan" \
  -d "{
    \"type\": \"multipass_card_usage\",
    \"source\": \"email\",
    \"timestamp\": \"$TIMESTAMP\",
    \"email\": \"test@example.com\",
    \"g1pubnostr\": \"G1PUB1234567890\",
    \"current_url\": \"https://u.copylaradio.com/scan\"
  }" \
  -w "\nHTTP Status: %{http_code}\n" \
  -s | jq '.' 2>/dev/null || echo "Response received (not JSON)"
echo ""

# Test 5: Button click analytics
echo -e "${GREEN}Test 5: Button click analytics${NC}"
curl -X POST "$PING_URL" \
  -H "Content-Type: application/json" \
  -d "{
    \"type\": \"button_click\",
    \"source\": \"web\",
    \"timestamp\": \"$TIMESTAMP\",
    \"button_id\": \"test_button\",
    \"current_url\": \"https://u.copylaradio.com/test\"
  }" \
  -w "\nHTTP Status: %{http_code}\n" \
  -s | jq '.' 2>/dev/null || echo "Response received (not JSON)"
echo ""

echo -e "${GREEN}✅ All tests completed!${NC}"
echo ""

# Wait a bit for events to be processed and stored
echo -e "${BLUE}⏳ Waiting 3 seconds for events to be processed...${NC}"
sleep 3
echo ""

# Verify events with nostr_get_events.sh
if [[ -n "$NOSTR_GET_EVENTS" ]]; then
    echo -e "${BLUE}🔍 Verifying analytics events in NOSTR relay...${NC}"
    echo ""
    
    # Get captain email to filter by author
    CAPTAIN_EMAIL=""
    CURRENT_LINK="${HOME}/.zen/game/players/.current"
    
    # Try to get email from .current symlink
    if [[ -L "$CURRENT_LINK" ]]; then
        TARGET_PATH=$(readlink -f "$CURRENT_LINK")
        CAPTAIN_EMAIL=$(basename "$TARGET_PATH")
    fi
    
    # Fallback to my.sh
    if [[ -z "$CAPTAIN_EMAIL" ]]; then
        MY_SH="${HOME}/.zen/Astroport.ONE/tools/my.sh"
        if [[ -f "$MY_SH" ]]; then
            CAPTAIN_EMAIL=$(bash -c "source $MY_SH && echo \$CAPTAINEMAIL" 2>/dev/null || echo "")
        fi
    fi
    
    # Fallback to environment variable
    if [[ -z "$CAPTAIN_EMAIL" ]]; then
        CAPTAIN_EMAIL="${CAPTAINEMAIL:-}"
    fi
    
    # Get captain pubkey (hex) from keyfile if email is available
    CAPTAIN_PUBKEY=""
    if [[ -n "$CAPTAIN_EMAIL" ]]; then
        KEYFILE="${HOME}/.zen/game/nostr/${CAPTAIN_EMAIL}/.secret.nostr"
        if [[ -f "$KEYFILE" ]]; then
            # Extract pubkey from keyfile (first line, hex format)
            CAPTAIN_PUBKEY=$(head -n 1 "$KEYFILE" | grep -oE '[0-9a-f]{64}' | head -n 1 || echo "")
        fi
    fi
    
    # Query analytics events (kind 10600; kind 10000 is NIP-51 mute list) created since test start
    echo -e "${GREEN}Querying analytics events (kind 10600) since test start...${NC}"
    
    if [[ -n "$CAPTAIN_PUBKEY" ]]; then
        echo -e "${BLUE}Filtering by author: ${CAPTAIN_PUBKEY:0:16}...${NC}"
        EVENTS=$("$NOSTR_GET_EVENTS" --kind 10600 --tag-t "analytics" --author "$CAPTAIN_PUBKEY" --since "$TEST_START_TIME" --limit 50 2>/dev/null || echo "")
    else
        echo -e "${YELLOW}⚠️  Captain pubkey not found, querying all analytics events...${NC}"
        EVENTS=$("$NOSTR_GET_EVENTS" --kind 10600 --tag-t "analytics" --since "$TEST_START_TIME" --limit 50 2>/dev/null || echo "")
    fi
    
    if [[ -z "$EVENTS" ]]; then
        echo -e "${RED}❌ No analytics events found in NOSTR relay${NC}"
        echo "   This could mean:"
        echo "   - Events were not sent (check server logs)"
        echo "   - Relay is not accessible"
        echo "   - Events were sent to a different relay"
    else
        # Count events
        EVENT_COUNT=$(echo "$EVENTS" | grep -c '"id"' || echo "0")
        echo -e "${GREEN}✅ Found $EVENT_COUNT analytics event(s)${NC}"
        echo ""
        
        # Display events summary
        if command -v jq &> /dev/null; then
            echo -e "${BLUE}Event summary:${NC}"
            echo "$EVENTS" | jq -r '{
                id: .id[0:16],
                kind: .kind,
                created_at: .created_at,
                tags: [.tags[] | select(.[0] == "t") | .[1]] | join(", "),
                content_preview: .content[0:100]
            }' 2>/dev/null || echo "$EVENTS" | head -n 5
        else
            echo -e "${BLUE}First few events:${NC}"
            echo "$EVENTS" | head -n 5
        fi
        echo ""
        
        # Delete events if --del is specified
        if [[ "$DELETE_MODE" == "true" ]]; then
            echo -e "${YELLOW}🗑️  Deleting test events...${NC}"
            
            if [[ "$FORCE_MODE" != "true" ]]; then
                echo -e "${YELLOW}⚠️  This will delete $EVENT_COUNT event(s) from the relay${NC}"
                echo -n "Continue? (yes/NO): "
                read -r CONFIRM
                if [[ "$CONFIRM" != "yes" ]]; then
                    echo -e "${BLUE}Deletion cancelled${NC}"
                    exit 0
                fi
            fi
            
            # Delete events (kind 10600 = UPlanet analytics)
            if [[ -n "$CAPTAIN_PUBKEY" ]]; then
                "$NOSTR_GET_EVENTS" --kind 10600 --tag-t "analytics" --author "$CAPTAIN_PUBKEY" --since "$TEST_START_TIME" --del --force 2>/dev/null || true
            else
                "$NOSTR_GET_EVENTS" --kind 10600 --tag-t "analytics" --since "$TEST_START_TIME" --del --force 2>/dev/null || true
            fi
            
            echo -e "${GREEN}✅ Test events deleted${NC}"
        fi
    fi
else
    echo -e "${YELLOW}⚠️  nostr_get_events.sh not available - skipping verification${NC}"
fi

echo ""
echo -e "${GREEN}✅ Test script completed!${NC}"
echo ""
echo "Note: Check the server logs to verify that NOSTR events were sent to the captain email."
echo "The endpoint should return JSON with 'sent_via': 'nostr' if successful."

