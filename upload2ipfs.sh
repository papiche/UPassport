#!/bin/bash
####################################################
## Receives a file and a temp file path
## Add to IPFS produce a json with all details
######################################################
## NIP-96/NIP-94 compatibility
## Add https://domain.tld/.well-known/nostr/nip96.json
## { "api_url": "https://u.domain.tld/api/upload2ipfs" }
####################################################

MY_PATH="`dirname \"$0\"`"              # relative
MY_PATH="`( cd \"$MY_PATH\" && pwd )`"  # absolutized and normalized
ME="${0##*/}"

[[ -s "${HOME}/.zen/Astroport.ONE/tools/my.sh" ]] \
    && source "${HOME}/.zen/Astroport.ONE/tools/my.sh"

# Parse arguments
YOUTUBE_METADATA_FILE=""
FILE_PATH=""
OUTPUT_FILE=""
USER_PUBKEY_HEX=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --metadata)
            YOUTUBE_METADATA_FILE="$2"
            shift 2
            ;;
        *)
            if [ -z "$FILE_PATH" ]; then
FILE_PATH="$1"
            elif [ -z "$OUTPUT_FILE" ]; then
                OUTPUT_FILE="$1"
            elif [ -z "$USER_PUBKEY_HEX" ]; then
                USER_PUBKEY_HEX="$1"
            fi
            shift
            ;;
    esac
done

if [ -z "$FILE_PATH" ]; then
  echo '{"status": "error", "message": "No file path provided.", "debug": "FILE_PATH is empty"}' > "$OUTPUT_FILE"
  exit 1
fi

if [ -z "$OUTPUT_FILE" ]; then
  echo '{"status": "error", "message": "No temporary file path provided.", "debug": "OUTPUT_FILE is empty"}' > "$OUTPUT_FILE"
  exit 1
fi

# Log user pubkey if provided (for provenance tracking)
if [ -n "$USER_PUBKEY_HEX" ]; then
  echo "DEBUG: User pubkey: ${USER_PUBKEY_HEX:0:16}... (provenance tracking enabled)" >&2
else
  echo "DEBUG: No user pubkey provided (provenance tracking disabled)" >&2
fi

# Load YouTube metadata if provided
YOUTUBE_METADATA_JSON=""
if [ -n "$YOUTUBE_METADATA_FILE" ] && [ -f "$YOUTUBE_METADATA_FILE" ]; then
    echo "DEBUG: Loading YouTube metadata from: $YOUTUBE_METADATA_FILE" >&2
    if command -v jq &> /dev/null; then
        # Validate JSON and extract relevant fields
        if jq . "$YOUTUBE_METADATA_FILE" >/dev/null 2>&1; then
            YOUTUBE_METADATA_JSON=$(cat "$YOUTUBE_METADATA_FILE")
            echo "DEBUG: ✅ YouTube metadata loaded successfully" >&2
        else
            echo "WARNING: Invalid JSON in YouTube metadata file: $YOUTUBE_METADATA_FILE" >&2
        fi
    else
        echo "WARNING: jq not available, cannot parse YouTube metadata" >&2
    fi
elif [ -n "$YOUTUBE_METADATA_FILE" ]; then
    echo "WARNING: YouTube metadata file not found: $YOUTUBE_METADATA_FILE" >&2
fi


if [ ! -f "$FILE_PATH" ]; then
  echo '{"status": "error", "message": "File not found.", "debug": "File does not exist"}' > "$OUTPUT_FILE"
  exit 1
fi

# Get file information
FILE_SIZE=$(stat -c%s "$FILE_PATH")
FILE_TYPE=$(file -b --mime-type "$FILE_PATH")
FILE_NAME=$(basename "$FILE_PATH")

# Log file information
echo "DEBUG: FILE_SIZE: $FILE_SIZE, FILE_TYPE: $FILE_TYPE, FILE_NAME: $FILE_NAME" >&2

# Check if file size exceeds 650MB (according to CD standard limits per format)
MAX_FILE_SIZE=$((650 * 1024 * 1024)) # 650MB in bytes
TARGET_FILE_SIZE=$((600 * 1024 * 1024)) # 600MB target (margin below 650MB limit)

# CRITICAL: Reduce video resolution BEFORE calculating hash (if file exceeds limit)
# This ensures the hash matches the actual file that will be uploaded
if [[ "$FILE_TYPE" == "video/"* ]] && [ "$FILE_SIZE" -gt "$MAX_FILE_SIZE" ]; then
    echo "DEBUG: Video size exceeds limit, reducing resolution BEFORE hash calculation..." >&2
    if reduce_video_if_needed "$FILE_PATH" "$FILE_SIZE" "$MAX_FILE_SIZE" "$TARGET_FILE_SIZE"; then
        echo "DEBUG: ✅ Video resized successfully, continuing..." >&2
        # Re-read file size after resize
        FILE_SIZE=$(stat -c%s "$FILE_PATH")
        echo "DEBUG: New file size: $FILE_SIZE bytes" >&2
    else
        echo '{"status": "error", "message": "File size exceeds 650MB limit and could not be reduced.", "debug": "Video resize failed", "fileSize": "'"$FILE_SIZE"'"}' > "$OUTPUT_FILE"
    exit 1
    fi
fi

# Calculate file hash (AFTER potential resize) for provenance tracking
FILE_HASH=$(sha256sum "$FILE_PATH" | awk '{print $1}')
echo "DEBUG: File hash (SHA256): $FILE_HASH" >&2

# Initialize SKIP_IPFS_UPLOAD flag (will be set to true if provenance tracking finds existing upload)
SKIP_IPFS_UPLOAD=false

################################################################################
# Helper: Build upload_chain array with timestamps
# Converts string chain (old format) or array (new format) to array with timestamps
################################################################################
build_upload_chain_array() {
    local existing_chain="$1"  # Can be string (old) or JSON array (new)
    local current_user="$2"
    local current_timestamp="$3"  # ISO 8601 format
    
    if [ -z "$current_user" ]; then
        echo "[]"
        return
    fi
    
    # Check if existing_chain is a JSON array (new format)
    if echo "$existing_chain" | jq -e '. | type == "array"' >/dev/null 2>&1; then
        # It's already an array, extract it and add current user
        local chain_array="$existing_chain"
    elif [ -n "$existing_chain" ] && [ "$existing_chain" != "null" ]; then
        # It's a string (old format), convert to array
        local chain_array="["
        IFS=',' read -ra PUBKEYS <<< "$existing_chain"
        local first=true
        for pubkey in "${PUBKEYS[@]}"; do
            pubkey=$(echo "$pubkey" | xargs)  # Trim whitespace
            if [ -n "$pubkey" ]; then
                if [ "$first" = true ]; then
                    chain_array="$chain_array{\"pubkey\":\"$pubkey\",\"timestamp\":null}"
                    first=false
                else
                    chain_array="$chain_array,{\"pubkey\":\"$pubkey\",\"timestamp\":null}"
                fi
            fi
        done
        chain_array="$chain_array]"
    else
        # No existing chain, start new
        chain_array="[]"
    fi
    
    # Check if current user is already in chain
    local user_exists=$(echo "$chain_array" | jq -r --arg user "$current_user" '[.[] | select(.pubkey == $user)] | length' 2>/dev/null || echo "0")
    
    if [ "$user_exists" = "0" ]; then
        # Add current user with timestamp
        echo "$chain_array" | jq --arg user "$current_user" --arg ts "$current_timestamp" '. + [{"pubkey":$user,"timestamp":$ts}]' 2>/dev/null || echo "[{\"pubkey\":\"$current_user\",\"timestamp\":\"$current_timestamp\"}]"
    else
        # User already in chain, return as-is
        echo "$chain_array"
    fi
}

################################################################################
# PROVENANCE TRACKING: Check if this file (hash) already exists in NOSTR
# THIS HAPPENS **BEFORE** IPFS UPLOAD TO AVOID REDUNDANT UPLOADS
################################################################################
ORIGINAL_EVENT_ID=""
ORIGINAL_AUTHOR=""
ORIGINAL_EVENT_JSON=""
UPLOAD_CHAIN=""
UPLOAD_CHAIN_ARRAY=""
EXISTING_UPLOAD_CHAIN_JSON=""
PROVENANCE_TAGS=""

if [ -n "$USER_PUBKEY_HEX" ]; then
    echo "DEBUG: Checking for existing NOSTR events with this file hash..." >&2
    
    # Path to nostr_get_events.sh (in Astroport.ONE/tools or current directory)
    NOSTR_GET_EVENTS="${HOME}/.zen/Astroport.ONE/tools/nostr_get_events.sh"
    
    if [ -f "$NOSTR_GET_EVENTS" ]; then
        # Search for events with this file hash
        echo "DEBUG: Searching for hash $FILE_HASH in NOSTR events..." >&2
        
        # Determine file type to optimize search
        # OPTIMIZATION: Search directly by file hash using #x tag filter (NIP-01)
        # This is MUCH more efficient than fetching 1000+ events and filtering client-side
        if [[ "$FILE_TYPE" == "video/"* ]]; then
            # For videos: search in kind 21/22 (NIP-71 video events) with #x tag filter
            echo "DEBUG: Video file detected, searching by hash in kind 21/22 (NIP-71)..." >&2
            echo "DEBUG: Hash filter: #x=$FILE_HASH" >&2
            
            # Search for events with this exact hash in both kind 21 and 22
            # Using --tag-x filter for precise hash matching (avoids fetching 1000 events)
            EXISTING_EVENTS_21=$(bash "$NOSTR_GET_EVENTS" --kind 21 --tag-x "$FILE_HASH" --limit 1 2>/dev/null || echo "")
            EXISTING_EVENTS_22=$(bash "$NOSTR_GET_EVENTS" --kind 22 --tag-x "$FILE_HASH" --limit 1 2>/dev/null || echo "")
            # Combine both results
            EXISTING_EVENTS="$EXISTING_EVENTS_21"$'\n'"$EXISTING_EVENTS_22"
        else
            # For other files: search in kind 1063 (NIP-94 file metadata) with #x tag filter
            echo "DEBUG: Non-video file detected, searching by hash in kind 1063 (NIP-94)..." >&2
            echo "DEBUG: Hash filter: #x=$FILE_HASH" >&2
            EXISTING_EVENTS=$(bash "$NOSTR_GET_EVENTS" --kind 1063 --tag-x "$FILE_HASH" --limit 1 2>/dev/null || echo "")
        fi
        
        if [ -n "$EXISTING_EVENTS" ]; then
            echo "DEBUG: Found NOSTR events, checking for matching hash..." >&2
            
            # Filter events with matching 'x' tag (file hash)
            if command -v jq &> /dev/null; then
                # Use jq to find events with matching hash in tags
                MATCHING_EVENT=$(echo "$EXISTING_EVENTS" | jq -r --arg hash "$FILE_HASH" '
                    select(.tags[]? | select(.[0] == "x" and .[1] == $hash))
                ' | head -n 1)
                
                if [ -n "$MATCHING_EVENT" ]; then
                    ORIGINAL_EVENT_ID=$(echo "$MATCHING_EVENT" | jq -r '.id')
                    ORIGINAL_AUTHOR=$(echo "$MATCHING_EVENT" | jq -r '.pubkey')
                    ORIGINAL_EVENT_JSON="$MATCHING_EVENT"
                    
                    echo "DEBUG: ✅ Found original event! ID: ${ORIGINAL_EVENT_ID:0:16}..., Author: ${ORIGINAL_AUTHOR:0:16}..." >&2
                    
                    # Extract existing CID and metadata from original event
                    ORIGINAL_CID=""
                    ORIGINAL_URL=$(echo "$MATCHING_EVENT" | jq -r '.tags[]? | select(.[0] == "url") | .[1]' 2>/dev/null || echo "")
                    if [ -n "$ORIGINAL_URL" ]; then
                        # Extract CID from URL (format: /ipfs/QmXXX/filename or https://gateway/ipfs/QmXXX/filename)
                        ORIGINAL_CID=$(echo "$ORIGINAL_URL" | grep -oP '(?<=ipfs/)[^/]+' | head -n 1)
                        if [ -n "$ORIGINAL_CID" ]; then
                            echo "DEBUG: 💾 Original CID found: $ORIGINAL_CID" >&2
                            echo "DEBUG: 🚀 Skipping IPFS upload, reusing existing CID..." >&2
                            
                            # Reuse the existing CID instead of uploading again
                            CID="$ORIGINAL_CID"
                            
                            # Try to retrieve existing metadata from original event
                            # Check if there's an 'info' tag with info.json CID
                            ORIGINAL_INFO_CID=$(echo "$MATCHING_EVENT" | jq -r '.tags[]? | select(.[0] == "info") | .[1]' 2>/dev/null || echo "")
                            if [ -n "$ORIGINAL_INFO_CID" ] && [ "$ORIGINAL_INFO_CID" != "null" ]; then
                                echo "DEBUG: 📋 Original info.json CID found: $ORIGINAL_INFO_CID" >&2
                                
                                # Try to fetch existing metadata from IPFS using ipfs get (downloads AND pins)
                                if command -v ipfs &> /dev/null; then
                                    echo "DEBUG: 📥 Downloading info.json from IPFS (this also pins it)..." >&2
                                    
                                    # Create temporary file for info.json
                                    TEMP_INFO_FILE="${HOME}/.zen/tmp/info_${ORIGINAL_INFO_CID}.json"
                                    mkdir -p "$(dirname "$TEMP_INFO_FILE")"
                                    
                                    # Download info.json via ipfs get (automatically pins it)
                                    if ipfs get -o "$TEMP_INFO_FILE" "$ORIGINAL_INFO_CID" >/dev/null 2>&1; then
                                        echo "DEBUG: ✅ Downloaded and pinned info.json" >&2
                                        
                                        # Read the downloaded file
                                        ORIGINAL_METADATA=$(cat "$TEMP_INFO_FILE" 2>/dev/null || echo "")
                                        
                                        # Clean up temporary file
                                        rm -f "$TEMP_INFO_FILE"
                                        
                                        if [ -n "$ORIGINAL_METADATA" ]; then
                                            # Extract metadata from original info.json
                                            if command -v jq &> /dev/null; then
                                                # Extract all metadata to avoid re-extraction
                                                DURATION=$(echo "$ORIGINAL_METADATA" | jq -r '.media.duration // 0' 2>/dev/null || echo "0")
                                                VIDEO_DIMENSIONS=$(echo "$ORIGINAL_METADATA" | jq -r '.media.dimensions // ""' 2>/dev/null || echo "")
                                                VIDEO_CODECS=$(echo "$ORIGINAL_METADATA" | jq -r '.media.video_codecs // ""' 2>/dev/null || echo "")
                                                AUDIO_CODECS=$(echo "$ORIGINAL_METADATA" | jq -r '.media.audio_codecs // ""' 2>/dev/null || echo "")
                                                THUMBNAIL_CID=$(echo "$ORIGINAL_METADATA" | jq -r '.media.thumbnail_ipfs // ""' 2>/dev/null || echo "")
                                                GIFANIM_CID=$(echo "$ORIGINAL_METADATA" | jq -r '.media.gifanim_ipfs // ""' 2>/dev/null || echo "")
                                                IMAGE_DIMENSIONS=$(echo "$ORIGINAL_METADATA" | jq -r '.image.dimensions // ""' 2>/dev/null || echo "")
                                                DESCRIPTION=$(echo "$ORIGINAL_METADATA" | jq -r '.metadata.description // ""' 2>/dev/null || echo "")
                                                FILE_TYPE=$(echo "$ORIGINAL_METADATA" | jq -r '.file.type // ""' 2>/dev/null || echo "$FILE_TYPE")
                                                IDISK=$(echo "$ORIGINAL_METADATA" | jq -r '.metadata.type // ""' 2>/dev/null || echo "")
                                                
                                                echo "DEBUG: ✅ Reused metadata: duration=$DURATION, dimensions=$VIDEO_DIMENSIONS, thumbnail=$THUMBNAIL_CID, gifanim=$GIFANIM_CID" >&2
                                                
                                                # Download and pin the main CID, thumbnail, and GIF using ipfs get
                                                echo "DEBUG: 📥 Downloading and pinning main file and assets..." >&2
                                                
                                                # Create temporary directory for downloads
                                                TEMP_GET_DIR="${HOME}/.zen/tmp/ipfs_get_$$"
                                                mkdir -p "$TEMP_GET_DIR"
                                                
                                                # Download main file (this also pins it automatically)
                                                if ipfs get -o "$TEMP_GET_DIR/main" "$CID" >/dev/null 2>&1; then
                                                    echo "DEBUG: ✅ Downloaded and pinned main file: $CID" >&2
                                                    rm -rf "$TEMP_GET_DIR/main"
                                                else
                                                    echo "DEBUG: ⚠️ Could not download main CID (may not be available on network)" >&2
                                                fi
                                                
                                                # Download thumbnail if available
                                                if [ -n "$THUMBNAIL_CID" ]; then
                                                    if ipfs get -o "$TEMP_GET_DIR/thumb" "$THUMBNAIL_CID" >/dev/null 2>&1; then
                                                        echo "DEBUG: ✅ Downloaded and pinned thumbnail: $THUMBNAIL_CID" >&2
                                                        rm -rf "$TEMP_GET_DIR/thumb"
                                                    fi
                                                fi
                                                
                                                # Download animated GIF if available
                                                if [ -n "$GIFANIM_CID" ]; then
                                                    if ipfs get -o "$TEMP_GET_DIR/gif" "$GIFANIM_CID" >/dev/null 2>&1; then
                                                        echo "DEBUG: ✅ Downloaded and pinned animated GIF: $GIFANIM_CID" >&2
                                                        rm -rf "$TEMP_GET_DIR/gif"
                                                    fi
                                                fi
                                                
                                                # Clean up temporary directory
                                                rm -rf "$TEMP_GET_DIR"
                                                
                                                # Extract upload_chain from original info.json (if available)
                                                # It can be either a string (old format) or an array (new format)
                                                EXISTING_UPLOAD_CHAIN_JSON=$(echo "$ORIGINAL_METADATA" | jq -c '.provenance.upload_chain // empty' 2>/dev/null || echo "")
                                                if [ -n "$EXISTING_UPLOAD_CHAIN_JSON" ] && [ "$EXISTING_UPLOAD_CHAIN_JSON" != "null" ]; then
                                                    echo "DEBUG: 📋 Found existing upload_chain in info.json: ${EXISTING_UPLOAD_CHAIN_JSON:0:100}..." >&2
                                                fi
                                                
                                                # DO NOT reuse info.json CID - create new one with updated provenance
                                                # This allows the upload history to evolve with each re-publication
                                                # INFO_CID will be created later with new timestamp and upload_chain
                                                echo "DEBUG: 📝 Will create new info.json with updated provenance..." >&2
                                                
                                                # Skip IPFS upload of main file (reuse CID) but CREATE NEW info.json
                                                SKIP_IPFS_UPLOAD=true
                                            fi
                                        fi
                                    else
                                        echo "DEBUG: ⚠️ Could not download info.json from IPFS (may not be available)" >&2
                                    fi
                                fi
                            fi
                        fi
                    fi
                    
                    # Check if there's already an upload chain in the original event or info.json
                    # Priority: info.json (new format) > Nostr tag (old format)
                    EXISTING_CHAIN_STRING=$(echo "$MATCHING_EVENT" | jq -r '.tags[]? | select(.[0] == "upload_chain") | .[1]' 2>/dev/null || echo "")
                    
                    # Use upload_chain from info.json if available (new format with timestamps)
                    if [ -n "$EXISTING_UPLOAD_CHAIN_JSON" ] && [ "$EXISTING_UPLOAD_CHAIN_JSON" != "null" ]; then
                        EXISTING_CHAIN_FOR_BUILD="$EXISTING_UPLOAD_CHAIN_JSON"
                        echo "DEBUG: Using upload_chain from info.json (new format)" >&2
                    elif [ -n "$EXISTING_CHAIN_STRING" ] && [ "$EXISTING_CHAIN_STRING" != "null" ]; then
                        EXISTING_CHAIN_FOR_BUILD="$EXISTING_CHAIN_STRING"
                        echo "DEBUG: Using upload_chain from Nostr tag (old format, will convert)" >&2
                    else
                        # No existing chain, start with original author if different from current user
                        if [ -n "$ORIGINAL_AUTHOR" ] && [ "$ORIGINAL_AUTHOR" != "$USER_PUBKEY_HEX" ]; then
                            EXISTING_CHAIN_FOR_BUILD="$ORIGINAL_AUTHOR"
                            echo "DEBUG: Starting new chain with original author" >&2
                        else
                            EXISTING_CHAIN_FOR_BUILD=""
                            echo "DEBUG: Starting new chain with current user only" >&2
                        fi
                    fi
                    
                    # Build upload_chain array with timestamps using helper function
                    CURRENT_TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
                    UPLOAD_CHAIN_ARRAY=$(build_upload_chain_array "$EXISTING_CHAIN_FOR_BUILD" "$USER_PUBKEY_HEX" "$CURRENT_TIMESTAMP")
                    echo "DEBUG: Built upload_chain array with timestamps: ${UPLOAD_CHAIN_ARRAY:0:150}..." >&2
                    
                    # Keep old string format for Nostr tags (backward compatibility)
                    # Extract pubkeys from array for Nostr tag
                    if [ -n "$EXISTING_CHAIN_STRING" ] && [ "$EXISTING_CHAIN_STRING" != "null" ]; then
                        if [[ "$EXISTING_CHAIN_STRING" != *"$USER_PUBKEY_HEX"* ]]; then
                            UPLOAD_CHAIN="$EXISTING_CHAIN_STRING,$USER_PUBKEY_HEX"
                        else
                            UPLOAD_CHAIN="$EXISTING_CHAIN_STRING"
                        fi
                    else
                        # Build string from array for Nostr tag
                        UPLOAD_CHAIN=$(echo "$UPLOAD_CHAIN_ARRAY" | jq -r '[.[].pubkey] | join(",")' 2>/dev/null || echo "$USER_PUBKEY_HEX")
                    fi
                    echo "DEBUG: Upload chain (string for Nostr): ${UPLOAD_CHAIN:0:80}..." >&2
                    
                    # Build provenance tags for NIP-94 event
                    # Only add tags if we have valid event ID and author
                    if [ -n "$ORIGINAL_EVENT_ID" ] && [ "$ORIGINAL_EVENT_ID" != "null" ]; then
                        PROVENANCE_TAGS=", [\"e\", \"$ORIGINAL_EVENT_ID\", \"\", \"mention\"]"
                        if [ -n "$ORIGINAL_AUTHOR" ] && [ "$ORIGINAL_AUTHOR" != "null" ] && [ "$ORIGINAL_AUTHOR" != "$USER_PUBKEY_HEX" ]; then
                            PROVENANCE_TAGS="$PROVENANCE_TAGS, [\"p\", \"$ORIGINAL_AUTHOR\"]"
                        fi
                        echo "DEBUG: Provenance tags created for original event reference" >&2
                    else
                        echo "DEBUG: No valid original event ID, skipping provenance tags" >&2
                    fi
                else
                    echo "DEBUG: No matching events found with this hash" >&2
                fi
            else
                echo "WARNING: jq not available, skipping provenance check" >&2
            fi
        else
            echo "DEBUG: No NIP-94/NIP-71 events found in relay" >&2
        fi
    else
        echo "WARNING: nostr_get_events.sh not found, skipping provenance check" >&2
        echo "WARNING: Searched paths: ${HOME}/.zen/Astroport.ONE/tools/nostr_get_events.sh, ${MY_PATH}/../Astroport.ONE/tools/nostr_get_events.sh" >&2
    fi
else
    echo "DEBUG: Provenance tracking disabled (no user pubkey provided)" >&2
fi

# Log provenance results
if [ -n "$ORIGINAL_EVENT_ID" ]; then
    echo "DEBUG: 🔗 Provenance established:" >&2
    echo "DEBUG:   - Original event: $ORIGINAL_EVENT_ID" >&2
    echo "DEBUG:   - Original author: $ORIGINAL_AUTHOR" >&2
    echo "DEBUG:   - Upload chain: $UPLOAD_CHAIN" >&2
    echo "DEBUG:   - SKIP_IPFS_UPLOAD: $SKIP_IPFS_UPLOAD" >&2
else
    echo "DEBUG: 📝 First upload of this file (no provenance found)" >&2
    # Initialize upload chain with current user for first upload
    if [ -n "$USER_PUBKEY_HEX" ]; then
        CURRENT_TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        UPLOAD_CHAIN_ARRAY=$(build_upload_chain_array "" "$USER_PUBKEY_HEX" "$CURRENT_TIMESTAMP")
        UPLOAD_CHAIN="$USER_PUBKEY_HEX"
        echo "DEBUG: 👤 Initialized upload chain with current user: ${USER_PUBKEY_HEX:0:16}..." >&2
        echo "DEBUG: Upload chain array: $UPLOAD_CHAIN_ARRAY" >&2
    else
        UPLOAD_CHAIN_ARRAY="[]"
    fi
fi

################################################################################
# END PROVENANCE TRACKING
################################################################################

# Attempt to add the file to IPFS and capture the output
# (This will be skipped if provenance tracking found an existing CID)
if [ "$SKIP_IPFS_UPLOAD" != "true" ]; then
    echo "DEBUG: 📤 Uploading file to IPFS..." >&2
CID_OUTPUT=$(ipfs add -wq "$FILE_PATH" 2>&1)

# Extract the CID
CID=$(echo "$CID_OUTPUT" | tail -n 1)

# Check if ipfs command worked
if [ -z "$CID" ]; then
    echo '{"status": "error", "message": "IPFS add failed.", "debug": "CID is empty", "ipfs_output": "'"$CID_OUTPUT"'"}' > "$OUTPUT_FILE"
    exit 1
fi

# Log the CID
echo "DEBUG: CID: $CID" >&2
fi

# Get current date
DATE=$(date +"%Y-%m-%d %H:%M %z")

# Initialize the description
DESCRIPTION=""

# Initialize additional nip94 tags
NIP94_TAGS=""

# Initialize duration (numeric value for JSON, 0 for non-media files)
DURATION="0"

# Initialize text (empty by default)
TEXT=""

# Initialize thumbnail CID (empty by default, will be set for video files)
THUMBNAIL_CID=""

# Initialize animated GIF CID (empty by default, will be set for video files)
GIFANIM_CID=""

################################################################################
# Function: Reduce video resolution if file size exceeds limit
# Optimized for speed with ffmpeg
################################################################################
reduce_video_if_needed() {
    local video_file="$1"
    local current_size="$2"
    local max_size="$3"
    local target_size="$4"
    
    # Only process video files
    if [[ ! "$FILE_TYPE" == "video/"* ]]; then
        return 0
    fi
    
    # Check if reduction is needed
    if [ "$current_size" -le "$max_size" ]; then
        return 0
    fi
    
    # Check if ffmpeg and ffprobe are available
    if ! command -v ffmpeg &> /dev/null || ! command -v ffprobe &> /dev/null; then
        echo "WARNING: ffmpeg/ffprobe not available, cannot reduce video size" >&2
        return 1
    fi
    
    echo "DEBUG: Video size ($current_size bytes) exceeds limit ($max_size bytes), reducing resolution..." >&2
    
    # Get current video dimensions
    local current_dims=$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=s=x:p=0 "$video_file" 2>/dev/null | head -n 1 | tr -d '\n\r')
    if [[ -z "$current_dims" ]]; then
        echo "WARNING: Could not get video dimensions" >&2
        return 1
    fi
    
    local current_width=$(echo "$current_dims" | cut -d'x' -f1)
    local current_height=$(echo "$current_dims" | cut -d'x' -f2)
    
    if [[ -z "$current_width" ]] || [[ -z "$current_height" ]] || [[ "$current_width" == "0" ]] || [[ "$current_height" == "0" ]]; then
        echo "WARNING: Invalid video dimensions: ${current_width}x${current_height}" >&2
        return 1
    fi
    
    echo "DEBUG: Current resolution: ${current_width}x${current_height}" >&2
    
    # Calculate reduction factor needed
    # Size is approximately proportional to (width * height)
    # So if we want to reduce size by factor R, we need to reduce resolution by factor sqrt(R)
    local size_ratio=$(echo "$current_size $target_size" | awk '{printf "%.3f", $1 / $2}')
    local reduction_factor=$(echo "$size_ratio" | awk '{printf "%.3f", sqrt($1)}')
    
    # Add 10% margin to ensure we're under the limit
    reduction_factor=$(echo "$reduction_factor 1.1" | awk '{printf "%.3f", $1 * $2}')
    
    echo "DEBUG: Size ratio: $size_ratio, Reduction factor: $reduction_factor" >&2
    
    # Calculate new dimensions (must be even for h264)
    local new_width=$(echo "$current_width $reduction_factor" | awk '{w = int($1 / $2); if (w % 2 != 0) w = w - 1; print w}')
    local new_height=$(echo "$current_height $reduction_factor" | awk '{h = int($1 / $2); if (h % 2 != 0) h = h - 1; print h}')
    
    # Ensure minimum dimensions (at least 240p)
    if [[ $new_width -lt 320 ]]; then
        new_width=320
        if [[ $((new_width % 2)) -ne 0 ]]; then
            new_width=320
        fi
    fi
    if [[ $new_height -lt 240 ]]; then
        new_height=240
        if [[ $((new_height % 2)) -ne 0 ]]; then
            new_height=240
        fi
    fi
    
    # Ensure dimensions are even (required for h264)
    if [[ $((new_width % 2)) -ne 0 ]]; then
        new_width=$((new_width - 1))
    fi
    if [[ $((new_height % 2)) -ne 0 ]]; then
        new_height=$((new_height - 1))
    fi
    
    echo "DEBUG: Target resolution: ${new_width}x${new_height}" >&2
    
    # Create temporary output file
    local temp_output="${video_file}.resized.$$"
    
    # Build ffmpeg command optimized for speed
    # Use ultrafast preset, hardware acceleration if available, and fast encoding settings
    local ffmpeg_cmd="ffmpeg -loglevel error -i \"$video_file\""
    
    # Try hardware acceleration first (NVIDIA CUDA)
    if command -v nvidia-smi &> /dev/null && nvidia-smi &>/dev/null; then
        echo "DEBUG: Using NVIDIA CUDA hardware acceleration for faster encoding..." >&2
        # Use nvenc encoder with ultrafast preset (p1 = fastest)
        # Note: We use software scaling (-vf scale) as it's more reliable than GPU scaling
        ffmpeg_cmd="$ffmpeg_cmd -c:v h264_nvenc -preset p1 -tune ll -crf 23"
    else
        # Software encoding with ultrafast preset for maximum speed
        echo "DEBUG: Using software encoding with ultrafast preset..." >&2
        ffmpeg_cmd="$ffmpeg_cmd -c:v libx264 -preset ultrafast -tune fastdecode -crf 23"
    fi
    
    # Audio: copy if possible, otherwise re-encode with fast settings
    ffmpeg_cmd="$ffmpeg_cmd -c:a aac -b:a 128k"
    
    # Scale filter
    ffmpeg_cmd="$ffmpeg_cmd -vf \"scale=${new_width}:${new_height}\""
    
    # Output file
    ffmpeg_cmd="$ffmpeg_cmd -y \"$temp_output\""
    
    echo "DEBUG: Executing: $ffmpeg_cmd" >&2
    
    # Execute ffmpeg command
    if eval "$ffmpeg_cmd" 2>&1; then
        # Check if output file was created and is smaller
        if [[ -f "$temp_output" ]] && [[ -s "$temp_output" ]]; then
            local new_size=$(stat -c%s "$temp_output" 2>/dev/null || echo "0")
            
            if [[ $new_size -gt 0 ]] && [[ $new_size -lt "$current_size" ]]; then
                # Replace original file with resized version
                if mv "$temp_output" "$video_file" 2>/dev/null; then
                    echo "DEBUG: ✅ Video resized successfully: ${current_width}x${current_height} -> ${new_width}x${new_height}" >&2
                    echo "DEBUG: ✅ File size reduced: $current_size -> $new_size bytes" >&2
                    
                    # Update FILE_SIZE for rest of script
                    # Note: FILE_HASH will be recalculated after this function returns
                    FILE_SIZE=$new_size
                    return 0
                else
                    echo "WARNING: Failed to replace original file with resized version" >&2
                    rm -f "$temp_output"
                    return 1
                fi
            else
                echo "WARNING: Resized file is not smaller or invalid (size: $new_size)" >&2
                rm -f "$temp_output"
                return 1
            fi
        else
            echo "WARNING: Resized file was not created or is empty" >&2
            rm -f "$temp_output"
            return 1
        fi
    else
        echo "WARNING: ffmpeg resize failed" >&2
        rm -f "$temp_output"
        return 1
    fi
}

# Text file check
if [[ "$FILE_TYPE" == "text/"* ]]; then
  DESCRIPTION="Plain text file" && IDISK="text"

# PDF file check
elif [[ "$FILE_TYPE" == "application/pdf" ]]; then
  DESCRIPTION="PDF Document" && IDISK="pdf"

# Image file check
elif [[ "$FILE_TYPE" == "image/"* ]]; then
  IMAGE_DIMENSIONS=$(identify -format "%wx%h" "$FILE_PATH" 2>/dev/null)
  DESCRIPTION="Image, Dimensions: $IMAGE_DIMENSIONS"
  IDISK="image"
    NIP94_TAGS="$NIP94_TAGS, [\"dim\", \"$IMAGE_DIMENSIONS\"]"
    
    # Extract EXIF metadata (GPS, comments, camera info, etc.)
    IMAGE_EXIF_JSON=""
    GPS_LATITUDE=""
    GPS_LONGITUDE=""
    IMAGE_COMMENT=""
    IMAGE_DESCRIPTION=""
    DATETIME_ORIGINAL=""
    CAMERA_MAKE=""
    CAMERA_MODEL=""
    
    # Try exiftool first (most comprehensive)
    if command -v exiftool &> /dev/null; then
        echo "DEBUG: Extracting EXIF metadata using exiftool..." >&2
        # Extract GPS coordinates
        GPS_LATITUDE=$(exiftool -s3 -GPSLatitude "$FILE_PATH" 2>/dev/null | head -n 1)
        GPS_LONGITUDE=$(exiftool -s3 -GPSLongitude "$FILE_PATH" 2>/dev/null | head -n 1)
        
        # Convert GPS coordinates from DMS (Degrees Minutes Seconds) to decimal if needed
        if [[ -n "$GPS_LATITUDE" ]] && [[ "$GPS_LATITUDE" =~ ^[0-9]+[°\ ] ]]; then
            # Convert DMS to decimal (e.g., "43° 36' 0.00\" N" -> "43.6")
            GPS_LAT_DECIMAL=$(exiftool -s3 -n -GPSLatitude "$FILE_PATH" 2>/dev/null | head -n 1)
            if [[ -n "$GPS_LAT_DECIMAL" ]]; then
                GPS_LATITUDE="$GPS_LAT_DECIMAL"
            fi
        fi
        if [[ -n "$GPS_LONGITUDE" ]] && [[ "$GPS_LONGITUDE" =~ ^[0-9]+[°\ ] ]]; then
            GPS_LON_DECIMAL=$(exiftool -s3 -n -GPSLongitude "$FILE_PATH" 2>/dev/null | head -n 1)
            if [[ -n "$GPS_LON_DECIMAL" ]]; then
                GPS_LONGITUDE="$GPS_LON_DECIMAL"
            fi
        fi
        
        # Extract comments and descriptions
        IMAGE_COMMENT=$(exiftool -s3 -UserComment "$FILE_PATH" 2>/dev/null | head -n 1)
        if [[ -z "$IMAGE_COMMENT" ]]; then
            IMAGE_COMMENT=$(exiftool -s3 -Comment "$FILE_PATH" 2>/dev/null | head -n 1)
        fi
        IMAGE_DESCRIPTION=$(exiftool -s3 -ImageDescription "$FILE_PATH" 2>/dev/null | head -n 1)
        
        # Extract date/time
        DATETIME_ORIGINAL=$(exiftool -s3 -DateTimeOriginal "$FILE_PATH" 2>/dev/null | head -n 1)
        if [[ -z "$DATETIME_ORIGINAL" ]]; then
            DATETIME_ORIGINAL=$(exiftool -s3 -CreateDate "$FILE_PATH" 2>/dev/null | head -n 1)
        fi
        
        # Extract camera info
        CAMERA_MAKE=$(exiftool -s3 -Make "$FILE_PATH" 2>/dev/null | head -n 1)
        CAMERA_MODEL=$(exiftool -s3 -Model "$FILE_PATH" 2>/dev/null | head -n 1)
        
        # Build EXIF JSON section using jq for proper JSON escaping
        EXIF_JSON_OBJ="{}"
        HAS_EXIF=false
        
        # Add GPS coordinates
        if [[ -n "$GPS_LATITUDE" ]] && [[ -n "$GPS_LONGITUDE" ]]; then
            # Validate GPS coordinates are numeric
            if [[ "$GPS_LATITUDE" =~ ^-?[0-9]+\.?[0-9]*$ ]] && [[ "$GPS_LONGITUDE" =~ ^-?[0-9]+\.?[0-9]*$ ]]; then
                EXIF_JSON_OBJ=$(echo "$EXIF_JSON_OBJ" | jq --arg lat "$GPS_LATITUDE" --arg lon "$GPS_LONGITUDE" '. + {gps: {latitude: ($lat | tonumber), longitude: ($lon | tonumber)}}' 2>/dev/null || echo "$EXIF_JSON_OBJ")
                HAS_EXIF=true
                # Add GPS to NIP-94 tags for geolocation
                NIP94_TAGS="$NIP94_TAGS, [\"g\", \"$GPS_LATITUDE,$GPS_LONGITUDE\"]"
            fi
        fi
        
        # Add comment
        if [[ -n "$IMAGE_COMMENT" ]]; then
            EXIF_JSON_OBJ=$(echo "$EXIF_JSON_OBJ" | jq --arg comment "$IMAGE_COMMENT" '. + {comment: $comment}' 2>/dev/null || echo "$EXIF_JSON_OBJ")
            HAS_EXIF=true
        fi
        
        # Add description
        if [[ -n "$IMAGE_DESCRIPTION" ]]; then
            EXIF_JSON_OBJ=$(echo "$EXIF_JSON_OBJ" | jq --arg desc "$IMAGE_DESCRIPTION" '. + {description: $desc}' 2>/dev/null || echo "$EXIF_JSON_OBJ")
            HAS_EXIF=true
        fi
        
        # Add datetime
        if [[ -n "$DATETIME_ORIGINAL" ]]; then
            EXIF_JSON_OBJ=$(echo "$EXIF_JSON_OBJ" | jq --arg dt "$DATETIME_ORIGINAL" '. + {datetime_original: $dt}' 2>/dev/null || echo "$EXIF_JSON_OBJ")
            HAS_EXIF=true
        fi
        
        # Add camera info
        if [[ -n "$CAMERA_MAKE" ]] || [[ -n "$CAMERA_MODEL" ]]; then
            CAMERA_OBJ="{}"
            if [[ -n "$CAMERA_MAKE" ]]; then
                CAMERA_OBJ=$(echo "$CAMERA_OBJ" | jq --arg make "$CAMERA_MAKE" '. + {make: $make}' 2>/dev/null || echo "$CAMERA_OBJ")
            fi
            if [[ -n "$CAMERA_MODEL" ]]; then
                CAMERA_OBJ=$(echo "$CAMERA_OBJ" | jq --arg model "$CAMERA_MODEL" '. + {model: $model}' 2>/dev/null || echo "$CAMERA_OBJ")
            fi
            EXIF_JSON_OBJ=$(echo "$EXIF_JSON_OBJ" | jq --argjson camera "$CAMERA_OBJ" '. + {camera: $camera}' 2>/dev/null || echo "$EXIF_JSON_OBJ")
            HAS_EXIF=true
        fi
        
        # Format for insertion into image section
        if [[ "$HAS_EXIF" == "true" ]]; then
            # Get JSON content without outer braces, add comma prefix
            IMAGE_EXIF_JSON_STR=$(echo "$EXIF_JSON_OBJ" | jq -c '.' 2>/dev/null | sed 's/^{//' | sed 's/}$//')
            if [[ -n "$IMAGE_EXIF_JSON_STR" ]]; then
                IMAGE_EXIF_JSON=", $IMAGE_EXIF_JSON_STR"
            fi
            echo "DEBUG: ✅ Extracted EXIF metadata: GPS=$GPS_LATITUDE,$GPS_LONGITUDE, Comment=$IMAGE_COMMENT" >&2
        fi
    # Fallback to ImageMagick identify -verbose if exiftool not available
    elif command -v identify &> /dev/null; then
        echo "DEBUG: Extracting EXIF metadata using ImageMagick identify..." >&2
        # ImageMagick can extract some EXIF but less comprehensive
        IDENTIFY_OUTPUT=$(identify -verbose "$FILE_PATH" 2>/dev/null)
        
        # Extract GPS from ImageMagick output (format: "exif:GPSLatitude: 43.6")
        GPS_LATITUDE=$(echo "$IDENTIFY_OUTPUT" | grep -i "exif:GPSLatitude" | head -n 1 | sed 's/.*exif:GPSLatitude: *//' | sed 's/ .*//')
        GPS_LONGITUDE=$(echo "$IDENTIFY_OUTPUT" | grep -i "exif:GPSLongitude" | head -n 1 | sed 's/.*exif:GPSLongitude: *//' | sed 's/ .*//')
        
        if [[ -n "$GPS_LATITUDE" ]] && [[ -n "$GPS_LONGITUDE" ]]; then
            IMAGE_EXIF_JSON=", \"gps\": {\"latitude\": $GPS_LATITUDE, \"longitude\": $GPS_LONGITUDE}"
            NIP94_TAGS="$NIP94_TAGS, [\"g\", \"$GPS_LATITUDE,$GPS_LONGITUDE\"]"
            echo "DEBUG: ✅ Extracted GPS from ImageMagick: $GPS_LATITUDE,$GPS_LONGITUDE" >&2
        fi
    fi
    
    # Generate JPG thumbnail for non-JPG images (skip if already have from provenance)
    if [ -z "$THUMBNAIL_CID" ] && command -v convert &> /dev/null; then
        # Check if image is not already JPG/JPEG
        if [[ ! "$FILE_TYPE" =~ ^image/jpe?g$ ]]; then
            echo "DEBUG: Generating JPG thumbnail for image..." >&2
            THUMBNAIL_PATH="$(dirname "$FILE_PATH")/$(basename "$FILE_PATH" | sed 's/\.[^.]*$//').thumb.jpg"
            
            # Convert to JPG with quality 85, max dimension 1200px
            if convert "$FILE_PATH" -resize 1200x1200\> -quality 85 -strip "$THUMBNAIL_PATH" 2>/dev/null; then
                if [[ -f "$THUMBNAIL_PATH" ]]; then
                    echo "DEBUG: Thumbnail generated, adding to IPFS..." >&2
                    THUMBNAIL_CID_OUTPUT=$(ipfs add -q "$THUMBNAIL_PATH" 2>&1)
                    THUMBNAIL_CID=$(echo "$THUMBNAIL_CID_OUTPUT" | tail -n 1)
                    
                    if [[ -n "$THUMBNAIL_CID" ]]; then
                        echo "DEBUG: Thumbnail CID: $THUMBNAIL_CID" >&2
                        # Unpin thumbnail to save space
                        ipfs pin rm "$THUMBNAIL_CID" >&2
                    else
                        echo "WARNING: Failed to get thumbnail CID from IPFS" >&2
                    fi
                    
                    # Clean up temporary thumbnail file
                    rm -f "$THUMBNAIL_PATH"
                else
                    echo "WARNING: Thumbnail file was not created" >&2
                fi
            else
                echo "WARNING: Failed to generate thumbnail with convert" >&2
            fi
        else
            echo "DEBUG: Image is already JPG, no thumbnail needed" >&2
        fi
    elif [ -z "$THUMBNAIL_CID" ]; then
        echo "WARNING: ImageMagick convert not available, cannot generate thumbnail" >&2
    fi

# Video file check (using ffprobe)
# Note: Video size reduction is already done BEFORE hash calculation (above)
elif [[ "$FILE_TYPE" == "video/"* ]]; then
    if command -v ffprobe &> /dev/null; then
        DURATION_RAW=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$FILE_PATH" 2>/dev/null)
        # Validate DURATION is numeric, default to 0 if not
        if [[ -z "$DURATION_RAW" ]] || ! [[ "$DURATION_RAW" =~ ^[0-9]+\.?[0-9]*$ ]]; then
            DURATION="0"
            DURATION_DESC="N/A"
        else
            DURATION="$DURATION_RAW"
            DURATION_DESC="$DURATION"
         fi
        VIDEO_CODECS=$(ffprobe -v error -select_streams v -show_entries stream=codec_name -of csv=p=0 "$FILE_PATH" 2>/dev/null | sed -z 's/\n/, /g;s/, $//')
        VIDEO_DIMENSIONS=$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=s=x:p=0 "$FILE_PATH" 2>/dev/null | head -n 1 | tr -d '\n\r')
        DESCRIPTION="Video, Duration: $DURATION_DESC seconds, Codecs: $VIDEO_CODECS"
        IDISK="video"
         if [[ -n "$VIDEO_DIMENSIONS" ]]; then
            NIP94_TAGS="$NIP94_TAGS, [\"dim\", \"$VIDEO_DIMENSIONS\"]"
        fi
        
        # Generate thumbnail for video files (skip if already have from provenance)
        if [ -z "$THUMBNAIL_CID" ] && command -v ffmpeg &> /dev/null; then
            echo "DEBUG: Generating thumbnail for video..." >&2
            THUMBNAIL_PATH="$(dirname "$FILE_PATH")/$(basename "$FILE_PATH" | sed 's/\.[^.]*$//').thumb.jpg"
            
            # Extract thumbnail at 1 second (or 10% of duration if longer than 10 seconds)
            THUMBNAIL_TIME="00:00:01"
            if [[ -n "$DURATION_RAW" ]] && [[ "$DURATION_RAW" =~ ^[0-9]+\.?[0-9]*$ ]]; then
                DURATION_SEC=$(echo "$DURATION_RAW" | awk '{print int($1)}')
                if [[ $DURATION_SEC -gt 10 ]]; then
                    # Use 10% of duration for thumbnail (use awk for floating point math, no bc dependency)
                    THUMBNAIL_SEC=$(echo "$DURATION_RAW" | awk '{print int($1 * 0.1)}')
                    HOURS=$((THUMBNAIL_SEC / 3600))
                    MINS=$(((THUMBNAIL_SEC % 3600) / 60))
                    SECS=$((THUMBNAIL_SEC % 60))
                    THUMBNAIL_TIME=$(printf "%02d:%02d:%02d" $HOURS $MINS $SECS)
                fi
            fi
            
            # Generate thumbnail using ffmpeg
            if ffmpeg -i "$FILE_PATH" -ss "$THUMBNAIL_TIME" -vframes 1 -y "$THUMBNAIL_PATH" 2>/dev/null; then
                if [[ -f "$THUMBNAIL_PATH" ]]; then
                    echo "DEBUG: Thumbnail generated, adding to IPFS..." >&2
                    THUMBNAIL_CID_OUTPUT=$(ipfs add -q "$THUMBNAIL_PATH" 2>&1)
                    THUMBNAIL_CID=$(echo "$THUMBNAIL_CID_OUTPUT" | tail -n 1)
                    
                    if [[ -n "$THUMBNAIL_CID" ]]; then
                        echo "DEBUG: Thumbnail CID: $THUMBNAIL_CID" >&2
                        # Unpin thumbnail to save space (it will be pinned by the user if needed)
                        ipfs pin rm "$THUMBNAIL_CID" >&2
                    else
                        echo "WARNING: Failed to get thumbnail CID from IPFS" >&2
                    fi
                    
                    # Clean up temporary thumbnail file
                    rm -f "$THUMBNAIL_PATH"
                else
                    echo "WARNING: Thumbnail file was not created" >&2
                fi
            else
                echo "WARNING: Failed to generate thumbnail with ffmpeg" >&2
            fi
        else
            echo "DEBUG: Thumbnail exists from provenance, skipping generation" >&2
        fi
        
        # Generate animated GIF for video files (skip if already have from provenance)
        # This should be independent of thumbnail generation
        if [ -z "$GIFANIM_CID" ] && command -v ffmpeg &> /dev/null; then
            echo "DEBUG: Generating animated GIF for video..." >&2
            GIFANIM_PATH="$(dirname "$FILE_PATH")/$(basename "$FILE_PATH" | sed 's/\.[^.]*$//').gif"
            
            # Calculate PROBETIME at phi ratio (0.618) of duration
            PROBETIME="1"
            if [[ -n "$DURATION_RAW" ]] && [[ "$DURATION_RAW" =~ ^[0-9]+\.?[0-9]*$ ]]; then
                # Use awk for floating point math (phi * duration), no bc dependency
                PROBETIME_SEC=$(echo "$DURATION_RAW" | awk '{print int($1 * 0.618)}')
                if [[ $PROBETIME_SEC -lt 1 ]]; then
                    PROBETIME_SEC=1
                fi
                HOURS=$((PROBETIME_SEC / 3600))
                MINS=$(((PROBETIME_SEC % 3600) / 60))
                SECS=$((PROBETIME_SEC % 60))
                PROBETIME=$(printf "%02d:%02d:%02d" $HOURS $MINS $SECS)
            fi
            
            # Generate animated GIF using ffmpeg (1.6 seconds starting at phi ratio)
            if ffmpeg -loglevel quiet -ss "$PROBETIME" -t 1.6 -i "$FILE_PATH" -y "$GIFANIM_PATH" 2>/dev/null; then
                if [[ -f "$GIFANIM_PATH" ]] && [[ -s "$GIFANIM_PATH" ]]; then
                    echo "DEBUG: Animated GIF generated, adding to IPFS..." >&2
                    GIFANIM_CID_OUTPUT=$(ipfs add -q "$GIFANIM_PATH" 2>&1)
                    GIFANIM_CID=$(echo "$GIFANIM_CID_OUTPUT" | tail -n 1)
                    
                    if [[ -n "$GIFANIM_CID" ]]; then
                        echo "DEBUG: Animated GIF CID: $GIFANIM_CID" >&2
                        # Unpin GIF to save space (it will be pinned by the user if needed)
                        ipfs pin rm "$GIFANIM_CID" >&2
                    else
                        echo "WARNING: Failed to get animated GIF CID from IPFS" >&2
                    fi
                    
                    # Clean up temporary GIF file
                    rm -f "$GIFANIM_PATH"
                else
                    echo "WARNING: Animated GIF file was not created or is empty" >&2
                fi
            else
                echo "WARNING: Failed to generate animated GIF with ffmpeg" >&2
            fi
        else
            echo "DEBUG: Animated GIF exists from provenance or ffmpeg not available, skipping generation" >&2
        fi
    elif [ "$SKIP_IPFS_UPLOAD" == "true" ]; then
        # Metadata already loaded from provenance tracking
        echo "DEBUG: ✅ Using metadata from original upload (provenance tracking)" >&2
        DESCRIPTION="Video (reused from provenance)"
        IDISK="video"
    else
      DURATION="0"
      DURATION_DESC="N/A"
      DESCRIPTION="Video, Could not get duration (ffprobe missing)"
    fi

 # Audio file check (using ffprobe)
elif [[ "$FILE_TYPE" == "audio/"* ]]; then
    if command -v ffprobe &> /dev/null; then
       DURATION_RAW=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$FILE_PATH" 2>/dev/null)
       # Validate DURATION is numeric, default to 0 if not
       if [[ -z "$DURATION_RAW" ]] || ! [[ "$DURATION_RAW" =~ ^[0-9]+\.?[0-9]*$ ]]; then
            DURATION="0"
           DURATION_DESC="N/A"
       else
           DURATION="$DURATION_RAW"
           DURATION_DESC="$DURATION"
        fi
        AUDIO_CODECS=$(ffprobe -v error -select_streams a -show_entries stream=codec_name -of csv=p=0 "$FILE_PATH" 2>/dev/null | sed -z 's/\n/, /g;s/, $//')
        DESCRIPTION="Audio, Duration: $DURATION_DESC seconds, Codecs: $AUDIO_CODECS"
        IDISK="audio"
    else
         DURATION="0"
         DURATION_DESC="N/A"
         DESCRIPTION="Audio, Could not get duration (ffprobe missing)"
    fi
# Generic file check
else
    DESCRIPTION="Other file type"
    IDISK="other"
fi

# Note: Provenance tracking already done above (before IPFS upload)
# No need to recalculate hash or search NOSTR again

# Create info.json with all detected metadata
# Use a temporary file in the same directory as the original file
INFO_JSON_FILE="$(dirname "$FILE_PATH")/$(basename "$FILE_PATH" | sed 's/\.[^.]*$//').info.json"

# Build NIP94 tags array string
# Note: We only use 'x' (not 'ox') because we don't transform files
# NIP-94: 'ox' is for original file hash before transformations
#         'x' is for file hash after transformations
# Since upload2ipfs.sh doesn't transform files, ox would be redundant with x
NIP94_TAGS_STR="[\"url\", \"/ipfs/$CID/$FILE_NAME\"], [\"x\", \"$FILE_HASH\"], [\"m\", \"$FILE_TYPE\"]"
if [[ -n "$IMAGE_DIMENSIONS" ]] || [[ -n "$VIDEO_DIMENSIONS" ]]; then
    DIM_VALUE="${IMAGE_DIMENSIONS:-$VIDEO_DIMENSIONS}"
    NIP94_TAGS_STR="$NIP94_TAGS_STR, [\"dim\", \"$DIM_VALUE\"]"
fi

# Build image section if available
IMAGE_SECTION=""
if [[ -n "$IMAGE_DIMENSIONS" ]]; then
    IMAGE_SECTION=",
  \"image\": {
    \"dimensions\": \"$IMAGE_DIMENSIONS\"${IMAGE_EXIF_JSON}
  }"
elif [[ -n "$IMAGE_EXIF_JSON" ]]; then
    # EXIF metadata without dimensions (shouldn't happen, but handle it)
    IMAGE_SECTION=",
  \"image\": {${IMAGE_EXIF_JSON:2}
  }"
fi

# Build media section if available
MEDIA_SECTION=""
if [[ "$FILE_TYPE" == "video/"* ]] || [[ "$FILE_TYPE" == "audio/"* ]]; then
    MEDIA_SECTION=",
  \"media\": {
    \"duration\": ${DURATION:-0}$(if [[ -n "$VIDEO_CODECS" ]]; then echo ",
    \"video_codecs\": \"$VIDEO_CODECS\""; fi)$(if [[ -n "$AUDIO_CODECS" ]]; then echo ",
    \"audio_codecs\": \"$AUDIO_CODECS\""; fi)$(if [[ -n "$VIDEO_DIMENSIONS" ]]; then echo ",
    \"dimensions\": \"$VIDEO_DIMENSIONS\""; fi)$(if [[ -n "$THUMBNAIL_CID" ]]; then echo ",
    \"thumbnail_ipfs\": \"$THUMBNAIL_CID\""; fi)$(if [[ -n "$GIFANIM_CID" ]]; then echo ",
    \"gifanim_ipfs\": \"$GIFANIM_CID\""; fi)
  }"
fi

# Initialize UPLOAD_CHAIN_ARRAY if not set (should be set by provenance tracking)
if [ -z "$UPLOAD_CHAIN_ARRAY" ]; then
    if [ -n "$USER_PUBKEY_HEX" ]; then
        CURRENT_TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        UPLOAD_CHAIN_ARRAY=$(build_upload_chain_array "" "$USER_PUBKEY_HEX" "$CURRENT_TIMESTAMP")
    else
        UPLOAD_CHAIN_ARRAY="[]"
    fi
fi

# Build provenance section
# Always include provenance if we have upload_chain_array (even for first upload)
PROVENANCE_SECTION=""
if [ -n "$UPLOAD_CHAIN_ARRAY" ] && [ "$UPLOAD_CHAIN_ARRAY" != "[]" ]; then
    if [ -n "$ORIGINAL_EVENT_ID" ]; then
        # Re-upload: include original event info
        PROVENANCE_SECTION=",
  \"provenance\": {
    \"original_event_id\": \"$ORIGINAL_EVENT_ID\",
    \"original_author\": \"$ORIGINAL_AUTHOR\",
    \"upload_chain\": $UPLOAD_CHAIN_ARRAY,
    \"is_reupload\": true
  }"
    else
        # First upload: just include upload_chain
        PROVENANCE_SECTION=",
  \"provenance\": {
    \"upload_chain\": $UPLOAD_CHAIN_ARRAY,
    \"is_reupload\": false
  }"
    fi
fi

# Build YouTube or TMDB metadata section if available
YOUTUBE_SECTION=""
TMDB_SECTION=""

if [ -n "$YOUTUBE_METADATA_JSON" ] && command -v jq &> /dev/null; then
    # Check if it's TMDB metadata (has tmdb_id) or YouTube metadata
    TMDB_ID=$(echo "$YOUTUBE_METADATA_JSON" | jq -r '.tmdb_id // empty' 2>/dev/null)
    
    if [[ -n "$TMDB_ID" ]]; then
        # This is TMDB metadata - extract ALL fields from scraper.TMDB.py output
        echo "DEBUG: Extracting comprehensive TMDB metadata for info.json..." >&2
        
        # Extract all TMDB metadata fields (preserve complete structure from scraper.TMDB.py)
        # Use jq to merge the entire TMDB metadata object into info.json
        # This preserves all fields: genres, director, creator, runtime, vote_average, etc.
        TMDB_FULL_JSON=$(echo "$YOUTUBE_METADATA_JSON" | jq -c '. | select(.tmdb_id != null)' 2>/dev/null)
        
        if [[ -n "$TMDB_FULL_JSON" ]]; then
            # Build TMDB section by including the entire metadata object
            # This ensures all fields from scraper.TMDB.py are preserved
            TMDB_JSON_STR=$(echo "$TMDB_FULL_JSON" | jq -c '.' 2>/dev/null | sed 's/^{//' | sed 's/}$//')
            
            if [[ -n "$TMDB_JSON_STR" ]]; then
                TMDB_SECTION=",
  \"tmdb\": {
    $TMDB_JSON_STR
  }"
                echo "DEBUG: ✅ Comprehensive TMDB metadata section created (includes all scraper fields)" >&2
            fi
        else
            # Fallback: extract basic fields if full JSON merge fails
            echo "DEBUG: ⚠️ Full TMDB JSON merge failed, using basic fields..." >&2
            TMDB_MEDIA_TYPE=$(echo "$YOUTUBE_METADATA_JSON" | jq -r '.media_type // empty' 2>/dev/null)
            TMDB_TITLE=$(echo "$YOUTUBE_METADATA_JSON" | jq -r '.title // empty' 2>/dev/null)
            TMDB_YEAR=$(echo "$YOUTUBE_METADATA_JSON" | jq -r '.year // empty' 2>/dev/null)
            TMDB_URL=$(echo "$YOUTUBE_METADATA_JSON" | jq -r '.tmdb_url // empty' 2>/dev/null)
            
            # Build TMDB section JSON using jq for proper escaping
            TMDB_OBJ="{}"
            if [[ -n "$TMDB_ID" ]]; then
                TMDB_OBJ=$(echo "$TMDB_OBJ" | jq --argjson id "$TMDB_ID" '. + {tmdb_id: $id}' 2>/dev/null || echo "$TMDB_OBJ")
            fi
            if [[ -n "$TMDB_MEDIA_TYPE" ]]; then
                TMDB_OBJ=$(echo "$TMDB_OBJ" | jq --arg type "$TMDB_MEDIA_TYPE" '. + {media_type: $type}' 2>/dev/null || echo "$TMDB_OBJ")
            fi
            if [[ -n "$TMDB_TITLE" ]]; then
                TMDB_OBJ=$(echo "$TMDB_OBJ" | jq --arg title "$TMDB_TITLE" '. + {title: $title}' 2>/dev/null || echo "$TMDB_OBJ")
            fi
            if [[ -n "$TMDB_YEAR" ]]; then
                TMDB_OBJ=$(echo "$TMDB_OBJ" | jq --arg year "$TMDB_YEAR" '. + {year: $year}' 2>/dev/null || echo "$TMDB_OBJ")
            fi
            if [[ -n "$TMDB_URL" ]]; then
                TMDB_OBJ=$(echo "$TMDB_OBJ" | jq --arg url "$TMDB_URL" '. + {tmdb_url: $url}' 2>/dev/null || echo "$TMDB_OBJ")
            fi
            
            # Convert to string and format for insertion
            if [[ "$TMDB_OBJ" != "{}" ]]; then
                TMDB_JSON_STR=$(echo "$TMDB_OBJ" | jq -c '.' 2>/dev/null | sed 's/^{//' | sed 's/}$//')
                if [[ -n "$TMDB_JSON_STR" ]]; then
                    TMDB_SECTION=",
  \"tmdb\": {
    $TMDB_JSON_STR
  }"
                    echo "DEBUG: ✅ Basic TMDB metadata section created" >&2
                fi
            fi
        fi
    else
        # This is YouTube metadata
        echo "DEBUG: Extracting comprehensive YouTube metadata for info.json..." >&2
        
        # The metadata is already structured by transform_youtube_metadata_to_structured() in 54321.py
        # It contains: channel_info, content_info, technical_info, statistics, dates, media_info, 
        # playlist_info, thumbnails, etc. at the root level (not nested in "youtube" section)
        YOUTUBE_FULL_JSON=$(echo "$YOUTUBE_METADATA_JSON" | jq -c '.' 2>/dev/null)
        
        if [[ -n "$YOUTUBE_FULL_JSON" ]] && [[ "$YOUTUBE_FULL_JSON" != "{}" ]]; then
            # Metadata is structured - place it at root level of info.json
            # This format is compatible with both enrichTrackWithInfoJson (nostrify.enhancements.js) 
            # and loadInfoJsonMetadata (youtube.enhancements.js)
            echo "DEBUG: ✅ YouTube metadata is structured (from transform_youtube_metadata_to_structured)" >&2
            
            # Extract structured fields to place at root level
            YOUTUBE_ROOT_JSON_STR=$(echo "$YOUTUBE_FULL_JSON" | jq -c '.' 2>/dev/null | sed 's/^{//' | sed 's/}$//')
            
            if [[ -n "$YOUTUBE_ROOT_JSON_STR" ]]; then
                # Place structured metadata at root level (not in "youtube" section)
                # This allows both mp3.html and youtube.html to use the same structure
                YOUTUBE_SECTION=",
    $YOUTUBE_ROOT_JSON_STR"
                echo "DEBUG: ✅ Structured YouTube metadata placed at root level (compatible with mp3.html and youtube.html)" >&2
            fi
        else
            echo "WARNING: Empty or invalid YouTube metadata JSON" >&2
        fi
    fi
fi

# Construct info.json content
# CRITICAL: Add protocol version for compatibility tracking
# Protocol version follows semantic versioning: MAJOR.MINOR.PATCH
# - MAJOR: Breaking changes to structure
# - MINOR: New fields added (backward compatible)
# - PATCH: Bug fixes
PROTOCOL_VERSION="1.0.0"

INFO_JSON_CONTENT="{
  \"protocol\": {
    \"name\": \"UPlanet File Management Contract\",
    \"version\": \"$PROTOCOL_VERSION\",
    \"specification\": \"https://github.com/papiche/Astroport.ONE/blob/main/Astroport.ONE/docs/UPlanet_FILE_CONTRACT.md\"
  },
  \"file\": {
    \"name\": \"$FILE_NAME\",
    \"size\": $FILE_SIZE,
    \"type\": \"$FILE_TYPE\",
    \"hash\": \"$FILE_HASH\"
  },
  \"ipfs\": {
    \"cid\": \"$CID\",
    \"url\": \"/ipfs/$CID/$FILE_NAME\",
    \"date\": \"$DATE\"$(if [ -n "$IPFSNODEID" ]; then echo ",
    \"node_id\": \"$IPFSNODEID\""; fi)
  }$IMAGE_SECTION$MEDIA_SECTION$PROVENANCE_SECTION$YOUTUBE_SECTION$TMDB_SECTION,
  \"metadata\": {
    \"description\": \"$DESCRIPTION\",
    \"type\": \"$IDISK\",
    \"title\": \"\$:/$IDISK/$CID/$FILE_NAME\"
  },
  \"nostr\": {
    \"nip94_tags\": [
      $NIP94_TAGS_STR
    ]
  }
}"

# Write info.json to temporary location (non-canonical first)
echo "$INFO_JSON_CONTENT" > "$INFO_JSON_FILE"

# CRITICAL: Canonicalize JSON according to RFC 8785 (JCS) before IPFS upload
# This ensures signature consistency and deterministic CID generation
CANONICALIZE_SCRIPT="${HOME}/.zen/Astroport.ONE/tools/canonicalize_json.py"
if [ -f "$CANONICALIZE_SCRIPT" ]; then
    echo "DEBUG: Canonicalizing info.json according to RFC 8785 (JCS)..." >&2
    # Create temporary file for canonical JSON
    CANONICAL_TEMP="${INFO_JSON_FILE}.canonical"
    python3 "$CANONICALIZE_SCRIPT" "$INFO_JSON_FILE" "$CANONICAL_TEMP" 2>/dev/null
    if [ -f "$CANONICAL_TEMP" ]; then
        # Replace original with canonical version
        mv "$CANONICAL_TEMP" "$INFO_JSON_FILE"
        echo "DEBUG: ✅ info.json canonicalized (RFC 8785)" >&2
    else
        echo "WARNING: Failed to canonicalize info.json, using original" >&2
    fi
else
    echo "WARNING: canonicalize_json.py not found at $CANONICALIZE_SCRIPT" >&2
    echo "WARNING: info.json will not be canonicalized (RFC 8785 compliance not guaranteed)" >&2
fi

# Add info.json to IPFS
INFO_CID_OUTPUT=$(ipfs add -q "$INFO_JSON_FILE" 2>&1)
INFO_CID=$(echo "$INFO_CID_OUTPUT" | tail -n 1)

# Check if info.json was added successfully
if [ -z "$INFO_CID" ]; then
    echo "WARNING: Failed to add info.json to IPFS" >&2
    INFO_CID_URL=""
else
    INFO_CID_URL="$myIPFS/ipfs/$INFO_CID"
    echo "DEBUG: info.json CID: $INFO_CID, URL: $INFO_CID_URL" >&2
    # KEEP info.json PINNED - it's needed for video metadata retrieval
    # ipfs pin rm "$INFO_CID" >&2 ## DISABLED - info.json must remain available
fi

# Clean up temporary info.json file
rm -f "$INFO_JSON_FILE"

# Construct JSON output with provenance tags
# Note: We only use 'x' (not 'ox') because we don't transform files
NIP94_JSON="{
    \"tags\": [
      [\"url\", \"$myIPFS/ipfs/$CID/$FILE_NAME\" ],
      [\"x\", \"$FILE_HASH\" ],
      [\"m\", \"$FILE_TYPE\"]"

# Add info tag if we have an info.json CID
if [ -n "$INFO_CID" ]; then
    NIP94_JSON="$NIP94_JSON, [\"info\", \"$INFO_CID\"]"
fi

NIP94_JSON="$NIP94_JSON$NIP94_TAGS$PROVENANCE_TAGS"

if [ -n "$UPLOAD_CHAIN" ]; then
    NIP94_JSON="$NIP94_JSON, [\"upload_chain\", \"$UPLOAD_CHAIN\"]"
fi
NIP94_JSON="$NIP94_JSON
    ],
    \"content\": \"\"
}"

# Build provenance JSON section
PROVENANCE_JSON=""
if [ -n "$ORIGINAL_EVENT_ID" ]; then
    PROVENANCE_JSON=",
  \"provenance\": {
    \"original_event_id\": \"$ORIGINAL_EVENT_ID\",
    \"original_author\": \"$ORIGINAL_AUTHOR\",
    \"upload_chain\": \"$UPLOAD_CHAIN\",
    \"is_reupload\": true
  }"
fi

JSON_OUTPUT="{
  \"status\": \"success\",
  \"message\": \"Upload successful.\",
  \"nip94_event\": $NIP94_JSON,
  \"created\": \"$(date -u +"%Y%m%d%H%M%S%4N")\",
  \"cid\": \"$CID\",
  \"mimeType\": \"$FILE_TYPE\",
  \"duration\": $(echo "$DURATION" | awk '{print int($1)}'),
  \"fileSize\": ${FILE_SIZE:-0},
  \"fileName\": \"$FILE_NAME\",
  \"fileHash\": \"$FILE_HASH\",
  \"info\": \"$INFO_CID\",
  \"thumbnail_ipfs\": \"$THUMBNAIL_CID\",
  \"gifanim_ipfs\": \"$GIFANIM_CID\",
  \"dimensions\": \"${VIDEO_DIMENSIONS:-${IMAGE_DIMENSIONS:-}}\",
  \"upload_chain\": \"${UPLOAD_CHAIN:-}\",
  \"unode\": \"$IPFSNODEID\",
  \"date\": \"$DATE\",
  \"description\": \"$DESCRIPTION\",
  \"text\": \"$TEXT\",
  \"title\": \"\$:/$IDISK/$CID/$FILE_NAME\"$PROVENANCE_JSON
}"

# Log JSON output to stderr before writing to temp file
echo "DEBUG: JSON_OUTPUT: $JSON_OUTPUT" >&2

# KEEP PINNED - Files must remain available on IPFS for playback
# Note: Thumbnails and GIFs are unpinned to save space, but main files must stay pinned
# ipfs pin rm "$CID" >&2 ## DISABLED - Files need to remain available

# Write the JSON to the temp file
echo "$JSON_OUTPUT" > "$OUTPUT_FILE"

exit 0
