import pytest
from utils.crypto import npub_to_hex, hex_to_npub

def test_npub_to_hex():
    # Valid npub
    npub = "npub180cvv07tjdrrgpa0j7j7tmnyl2yr6yr7l8j4s3evf6u64th6gkwsyjh6w6"
    hex_pubkey = "3bf0c63fcb93463407af97a5e5ee64fa883d107ef9e558472c4eb9aaaefa459d"
    assert npub_to_hex(npub) == hex_pubkey
    
    # Already hex
    assert npub_to_hex(hex_pubkey) == hex_pubkey
    
    # Invalid npub
    assert npub_to_hex("invalid_npub") == None
    assert npub_to_hex("npub1invalid") == None

def test_hex_to_npub():
    hex_pubkey = "3bf0c63fcb93463407af97a5e5ee64fa883d107ef9e558472c4eb9aaaefa459d"
    npub = "npub180cvv07tjdrrgpa0j7j7tmnyl2yr6yr7l8j4s3evf6u64th6gkwsyjh6w6"
    assert hex_to_npub(hex_pubkey) == npub
    
    # Invalid hex
    assert hex_to_npub("invalid_hex") == None
    assert hex_to_npub("123") == None
