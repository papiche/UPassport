#!/bin/bash
####################################################
## Receives a file and a temp file path
## Add to IPFS produce a json with all details
######################################################
## NIP-96/NIP-94 compatibility
## Add https://domain.tld/.well-known/nostr/nip96.json
## { "api_url": "https://u.domain.tld/upload2ipfs" }
####################################################

MY_PATH="`dirname \"$0\"`"              # relative
MY_PATH="`( cd \"$MY_PATH\" && pwd )`"  # absolutized and normalized
ME="${0##*/}"

[[ -s "${HOME}/.zen/Astroport.ONE/tools/my.sh" ]] \
    && source "${HOME}/.zen/Astroport.ONE/tools/my.sh"

FILE_PATH="$1"
OUTPUT_FILE="$2"
USER_PUBKEY_HEX="$3"  # Optional: user's hex public key for provenance tracking

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

# Check if file size exceeds 100MB
MAX_FILE_SIZE=$((100 * 1024 * 1024)) # 100MB in bytes
if [ "$FILE_SIZE" -gt "$MAX_FILE_SIZE" ]; then
    echo '{"status": "error", "message": "File size exceeds 100MB limit.", "debug": "File too large", "fileSize": "'"$FILE_SIZE"'"}' > "$OUTPUT_FILE"
    
    exit 1
fi

# Calculate file hash FIRST (before IPFS upload) for provenance tracking
FILE_HASH=$(sha256sum "$FILE_PATH" | awk '{print $1}')
echo "DEBUG: File hash (SHA256): $FILE_HASH" >&2

# Initialize SKIP_IPFS_UPLOAD flag (will be set to true if provenance tracking finds existing upload)
SKIP_IPFS_UPLOAD=false

################################################################################
# PROVENANCE TRACKING: Check if this file (hash) already exists in NOSTR
# THIS HAPPENS **BEFORE** IPFS UPLOAD TO AVOID REDUNDANT UPLOADS
################################################################################
ORIGINAL_EVENT_ID=""
ORIGINAL_AUTHOR=""
ORIGINAL_EVENT_JSON=""
UPLOAD_CHAIN=""
PROVENANCE_TAGS=""

if [ -n "$USER_PUBKEY_HEX" ]; then
    echo "DEBUG: Checking for existing NOSTR events with this file hash..." >&2
    
    # Path to nostr_get_events.sh (in Astroport.ONE/tools or current directory)
    NOSTR_GET_EVENTS="${HOME}/.zen/Astroport.ONE/tools/nostr_get_events.sh"
    
    if [ -f "$NOSTR_GET_EVENTS" ]; then
        # Search for events with this file hash
        echo "DEBUG: Searching for hash $FILE_HASH in NOSTR events..." >&2
        
        # Determine file type to optimize search
        if [[ "$FILE_TYPE" == "video/"* ]]; then
            # For videos: search in kind 21/22 (NIP-71 video events)
            echo "DEBUG: Video file detected, searching in kind 21/22 (NIP-71)..." >&2
            EXISTING_EVENTS_21=$(bash "$NOSTR_GET_EVENTS" --kind 21 --limit 1000 2>/dev/null || echo "")
            EXISTING_EVENTS_22=$(bash "$NOSTR_GET_EVENTS" --kind 22 --limit 1000 2>/dev/null || echo "")
            # Combine both results
            EXISTING_EVENTS="$EXISTING_EVENTS_21"$'\n'"$EXISTING_EVENTS_22"
        else
            # For other files: search in kind 1063 (NIP-94 file metadata)
            echo "DEBUG: Non-video file detected, searching in kind 1063 (NIP-94)..." >&2
            EXISTING_EVENTS=$(bash "$NOSTR_GET_EVENTS" --kind 1063 --limit 1000 2>/dev/null || echo "")
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
                                                
                                                # Reuse existing info.json CID
                                                INFO_CID="$ORIGINAL_INFO_CID"
                                                
                                                # Skip IPFS upload and metadata extraction
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
                    
                    # Check if there's already an upload chain in the original event
                    EXISTING_CHAIN=$(echo "$MATCHING_EVENT" | jq -r '.tags[]? | select(.[0] == "upload_chain") | .[1]' 2>/dev/null || echo "")
                    
                    if [ -n "$EXISTING_CHAIN" ] && [ "$EXISTING_CHAIN" != "null" ]; then
                        # Append current user to existing chain
                        if [[ "$EXISTING_CHAIN" != *"$USER_PUBKEY_HEX"* ]]; then
                            UPLOAD_CHAIN="$EXISTING_CHAIN,$USER_PUBKEY_HEX"
                            echo "DEBUG: Extended upload chain: ${UPLOAD_CHAIN:0:80}..." >&2
                        else
                            UPLOAD_CHAIN="$EXISTING_CHAIN"
                            echo "DEBUG: User already in chain, keeping existing chain" >&2
                        fi
                    else
                        # Create new chain with original author and current user
                        if [ "$ORIGINAL_AUTHOR" != "$USER_PUBKEY_HEX" ]; then
                            UPLOAD_CHAIN="$ORIGINAL_AUTHOR,$USER_PUBKEY_HEX"
                            echo "DEBUG: Created new upload chain: ${UPLOAD_CHAIN:0:50}..." >&2
                        else
                            UPLOAD_CHAIN="$ORIGINAL_AUTHOR"
                            echo "DEBUG: Same user re-uploading, single entry in chain" >&2
                        fi
                    fi
                    
                    # Build provenance tags for NIP-94 event
                    PROVENANCE_TAGS=", [\"e\", \"$ORIGINAL_EVENT_ID\", \"\", \"mention\"]"
                    if [ "$ORIGINAL_AUTHOR" != "$USER_PUBKEY_HEX" ]; then
                        PROVENANCE_TAGS="$PROVENANCE_TAGS, [\"p\", \"$ORIGINAL_AUTHOR\"]"
                    fi
                    
                    echo "DEBUG: Provenance tags created for original event reference" >&2
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
        VIDEO_DIMENSIONS=$(ffprobe -v error -select_streams v -show_entries stream=width,height -of csv=s=x:p=0 "$FILE_PATH" 2>/dev/null)
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
            
            # Generate animated GIF for video files (skip if already have from provenance)
            if [ -z "$GIFANIM_CID" ]; then
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
            fi  # End of GIFANIM_CID check
        else
            echo "WARNING: ffmpeg not available, cannot generate thumbnail or animated GIF" >&2
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
    \"dimensions\": \"$IMAGE_DIMENSIONS\"
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

# Build provenance section if available
PROVENANCE_SECTION=""
if [ -n "$ORIGINAL_EVENT_ID" ]; then
    PROVENANCE_SECTION=",
  \"provenance\": {
    \"original_event_id\": \"$ORIGINAL_EVENT_ID\",
    \"original_author\": \"$ORIGINAL_AUTHOR\",
    \"upload_chain\": \"$UPLOAD_CHAIN\",
    \"is_reupload\": true
  }"
fi

# Construct info.json content
INFO_JSON_CONTENT="{
  \"file\": {
    \"name\": \"$FILE_NAME\",
    \"size\": $FILE_SIZE,
    \"type\": \"$FILE_TYPE\",
    \"hash\": \"$FILE_HASH\"
  },
  \"ipfs\": {
    \"cid\": \"$CID\",
    \"url\": \"/ipfs/$CID/$FILE_NAME\",
    \"date\": \"$DATE\"
  }$IMAGE_SECTION$MEDIA_SECTION$PROVENANCE_SECTION,
  \"metadata\": {
    \"description\": \"$DESCRIPTION\",
    \"type\": \"$IDISK\",
    \"title\": \"\\\$:/$IDISK/$CID/$FILE_NAME\"
  },
  \"nostr\": {
    \"nip94_tags\": [
      $NIP94_TAGS_STR
    ]
  }
}"

# Write info.json to temporary location
echo "$INFO_JSON_CONTENT" > "$INFO_JSON_FILE"

# Add info.json to IPFS
INFO_CID_OUTPUT=$(ipfs add -q "$INFO_JSON_FILE" 2>&1)
INFO_CID=$(echo "$INFO_CID_OUTPUT" | tail -n 1)

# Check if info.json was added successfully
if [ -z "$INFO_CID" ]; then
    echo "WARNING: Failed to add info.json to IPFS" >&2
    INFO_CID_URL=""
else
    INFO_CID_URL="$myIPFS/ipfs/$INFO_CID/info.json"
    echo "DEBUG: info.json CID: $INFO_CID, URL: $INFO_CID_URL" >&2
    # Unpin info.json
    ipfs pin rm "$INFO_CID" >&2
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
  \"duration\": ${DURATION:-0},
  \"fileSize\": ${FILE_SIZE:-0},
  \"fileName\": \"$FILE_NAME\",
  \"fileHash\": \"$FILE_HASH\",
  \"info\": \"$INFO_CID\",
  \"thumbnail_ipfs\": \"$THUMBNAIL_CID\",
  \"gifanim_ipfs\": \"$GIFANIM_CID\",
  \"dimensions\": \"${VIDEO_DIMENSIONS:-${IMAGE_DIMENSIONS:-}}\",
  \"unode\": \"$IPFSNODEID\",
  \"date\": \"$DATE\",
  \"description\": \"$DESCRIPTION\",
  \"text\": \"$TEXT\",
  \"title\": \"\$:/$IDISK/$CID/$FILE_NAME\"$PROVENANCE_JSON
}"

# Log JSON output to stderr before writing to temp file
echo "DEBUG: JSON_OUTPUT: $JSON_OUTPUT" >&2

# UNPIN
ipfs pin rm "$CID" >&2 ## UNPIN

# Write the JSON to the temp file
echo "$JSON_OUTPUT" > "$OUTPUT_FILE"

exit 0
