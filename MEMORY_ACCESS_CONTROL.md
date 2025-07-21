# Memory Access Control System

## Overview

The UPlanet IA Bot system now includes access control for memory slots 1-12. Only sociétaires (CopyLaRadio members with ZenCard) can access these premium memory slots, while slot 0 remains accessible to all authorized users.

## Access Control Rules

### Slot 0 (Default)
- **Access**: All authorized users
- **Purpose**: Basic memory storage for all users
- **No restrictions**: Available to everyone who can use the bot

### Slots 1-12 (Premium)
- **Access**: Sociétaires only (CopyLaRadio members with ZenCard)
- **Purpose**: Advanced memory management for premium users
- **Requirement**: User must have a directory in `~/.zen/game/players/`

## How It Works

### Access Verification
The system checks if a user has access to memory slots by verifying the existence of their directory in `~/.zen/game/players/`:

```bash
# Function to check memory slot access
check_memory_slot_access() {
    local user_id="$1"
    local slot="$2"
    
    # Slot 0 is always accessible
    if [[ "$slot" == "0" ]]; then
        return 0
    fi
    
    # For slots 1-12, check if user is in ~/.zen/game/players/
    if [[ "$slot" -ge 1 && "$slot" -le 12 ]]; then
        if [[ -d "$HOME/.zen/game/players/$user_id" ]]; then
            return 0  # User has access
        else
            return 1  # User doesn't have access
        fi
    fi
    
    return 0  # Default allow for other cases
}
```

### Access Denied Messages
When a user tries to access a restricted slot, they receive a NOSTR message explaining the restriction:

```
⚠️ Accès refusé aux slots de mémoire 1-12.

Pour utiliser les slots de mémoire 1-12, vous devez être sociétaire CopyLaRadio et posséder une ZenCard.

Le slot 0 reste accessible pour tous les utilisateurs autorisés.

Pour devenir sociétaire : [IPFS link]

Votre Astroport Captain.
#CopyLaRadio #mem
```

## Protected Operations

The following operations are protected by access control:

### 1. Memory Recording (#rec)
- **Slot 0**: All users can record
- **Slots 1-12**: Only sociétaires can record

### 2. Memory Display (#mem)
- **Slot 0**: All users can view
- **Slots 1-12**: Only sociétaires can view

### 3. Memory Reset (#reset)
- **Slot 0**: All users can reset
- **Slots 1-12**: Only sociétaires can reset

### 4. AI Context (#BRO/#BOT with slot tags)
- **Slot 0**: All users can use for AI context
- **Slots 1-12**: Only sociétaires can use for AI context

### 5. Auto-Recording (#rec2)
- **Slot 0**: All users can auto-record bot responses
- **Slots 1-12**: Only sociétaires can auto-record bot responses

## Implementation Details

### Files Modified
1. **`NIP-101/relay.writePolicy.plugin/filter/1.sh`**
   - Added `check_memory_slot_access()` function
   - Added `send_memory_access_denied()` function
   - Modified memory recording sections to check access

2. **`Astroport.ONE/IA/UPlanet_IA_Responder.sh`**
   - Added same access control functions
   - Modified all memory operations to check access
   - Added access denied message sending

### User Identification
- **Primary**: Uses KNAME (NOSTR email) as user ID
- **Fallback**: Uses pubkey if KNAME is not available
- **Directory Check**: Verifies `~/.zen/game/players/{user_id}/` exists

## Testing

### Test Scripts
1. **`test_memory_access_control.sh`**
   - Tests access control logic
   - Verifies slot 0 is accessible to all
   - Verifies slots 1-12 require societaire status

2. **`test_memory_access_messages.sh`**
   - Tests access denied message scenarios
   - Verifies correct message content
   - Tests user directory structure

### Running Tests
```bash
chmod +x test_memory_access_control.sh
chmod +x test_memory_access_messages.sh
./test_memory_access_control.sh
./test_memory_access_messages.sh
```

## User Experience

### For Regular Users
- Can use slot 0 for all operations
- Will receive clear messages when trying to access premium slots
- Instructions provided on how to become a sociétaire

### For Sociétaires
- Full access to all 13 memory slots (0-12)
- No restrictions on memory operations
- Premium features available

## Becoming a Sociétaire

To gain access to slots 1-12, users must:
1. Become a CopyLaRadio member
2. Obtain a ZenCard
3. Have their directory created in `~/.zen/game/players/`

The system automatically detects sociétaire status and grants appropriate access.

## Security Considerations

- Access control is enforced at multiple points in the system
- Messages are logged for audit purposes
- No sensitive data is exposed in error messages
- Access denied messages are sent via NOSTR for transparency

## Future Enhancements

- Additional premium features for sociétaires
- More granular access control levels
- Integration with CopyLaRadio membership system
- Enhanced logging and monitoring 