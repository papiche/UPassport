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

if [ -z "$FILE_PATH" ]; then
  echo '{"status": "error", "message": "No file path provided.", "debug": "FILE_PATH is empty"}' > "$OUTPUT_FILE"
  exit 1
fi

if [ -z "$OUTPUT_FILE" ]; then
  echo '{"status": "error", "message": "No temporary file path provided.", "debug": "OUTPUT_FILE is empty"}' > "$OUTPUT_FILE"
  exit 1
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

# Attempt to add the file to IPFS and capture the output
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

# Calculate file hash (ox)
FILE_HASH=$(sha256sum "$FILE_PATH" | awk '{print $1}')

# Create info.json with all detected metadata
# Use a temporary file in the same directory as the original file
INFO_JSON_FILE="$(dirname "$FILE_PATH")/$(basename "$FILE_PATH" | sed 's/\.[^.]*$//').info.json"

# Build NIP94 tags array string
NIP94_TAGS_STR="[\"url\", \"/ipfs/$CID/$FILE_NAME\"], [\"x\", \"$FILE_HASH\"], [\"ox\", \"$FILE_HASH\"], [\"m\", \"$FILE_TYPE\"]"
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
    \"dimensions\": \"$VIDEO_DIMENSIONS\""; fi)
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
  }$IMAGE_SECTION$MEDIA_SECTION,
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

# Construct JSON output
NIP94_JSON="{
    \"tags\": [
      [\"url\", \"$myIPFS/ipfs/$CID/$FILE_NAME\" ],
      [\"x\", \"$FILE_HASH\" ],
      [\"ox\", \"$FILE_HASH\" ],
      [\"m\", \"$FILE_TYPE\"]
      $NIP94_TAGS
    ],
    \"content\": \"\"
}"

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
  \"info\": \"$INFO_CID\",
  \"unode\": \"$IPFSNODEID\",
  \"date\": \"$DATE\",
  \"description\": \"$DESCRIPTION\",
  \"text\": \"$TEXT\",
  \"title\": \"\$:/$IDISK/$CID/$FILE_NAME\"
}"

# Log JSON output to stderr before writing to temp file
echo "DEBUG: JSON_OUTPUT: $JSON_OUTPUT" >&2

# UNPIN
ipfs pin rm "$CID" >&2 ## UNPIN

# Write the JSON to the temp file
echo "$JSON_OUTPUT" > "$OUTPUT_FILE"

exit 0
