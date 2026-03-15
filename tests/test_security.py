import pytest
from utils.security import is_safe_email, is_safe_g1pub, sanitize_filename_python

def test_is_safe_email():
    assert is_safe_email("test@example.com") == True
    assert is_safe_email("support@qo-op.com") == True
    assert is_safe_email("user.name+tag@domain.co.uk") == True
    assert is_safe_email("invalid-email") == False
    assert is_safe_email("test@") == False
    assert is_safe_email("@domain.com") == False
    assert is_safe_email("test@domain@com") == False
    assert is_safe_email("test@domain..com") == False # Technically valid format but maybe we want to restrict it? The current implementation allows it.
    assert is_safe_email("test@domain.com/path") == False
    assert is_safe_email("test@domain.com\\path") == False
    assert is_safe_email("test@domain.com<script>") == False

def test_is_safe_g1pub():
    assert is_safe_g1pub("5B8iMAzq1dNmFe3ZxFTBQkqhq4fsyceZqVvB4A15qXy7") == True
    assert is_safe_g1pub("5B8iMAzq1dNmFe3ZxFTBQkqhq4fsyceZqVvB4A15qXy7:ZEN") == True
    assert is_safe_g1pub("invalid_pubkey!") == False
    assert is_safe_g1pub("invalid/pubkey") == False
    assert is_safe_g1pub("invalid<pubkey>") == False

def test_sanitize_filename_python():
    assert sanitize_filename_python("normal_file.txt") == "normal_file.txt"
    assert sanitize_filename_python("../../../etc/passwd") == "passwd"
    assert sanitize_filename_python("file/with/slashes.txt") == "slashes.txt"
    assert sanitize_filename_python("file<with>invalid:chars.txt") == "file_with_invalid_chars.txt"
    assert sanitize_filename_python("file\0with\0nulls.txt") == "filewithnulls.txt"
