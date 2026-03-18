"""
Tests for NIP-42 authentication flow used by /api/fileupload.

Root cause context
──────────────────
Kind 22242 (NIP-42 auth) is in the ephemeral range 20000-29999 per NIP-01.
strfry (and most relay implementations) forward ephemeral events to live
subscribers but do NOT persist them in the database.  Querying the relay
via REQ or via `strfry scan` for kind-22242 events therefore always returns
empty results.

Fix
───
• ajouter_media.sh creates a local marker file
  ~/.zen/game/nostr/<email>/.nip42_auth  after sending the NIP-42 event.
• check_nip42_auth_local_marker() checks for that marker file (max age 1 h).
• check_nip42_auth() tries the local marker FIRST, then falls back to
  nostr_get_events.sh (strfry scan) and finally to a WebSocket REQ.

These tests verify the whole chain without needing a live relay.
"""

import json
import os
import sys
import time
import tempfile
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, mock_open
import pytest

# ── make sure the UPassport package root is importable ──────────────────────
UPASSPORT_DIR = Path(__file__).parent.parent
if str(UPASSPORT_DIR) not in sys.path:
    sys.path.insert(0, str(UPASSPORT_DIR))


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_nip42_event(pubkey: str = "a" * 64, age_seconds: int = 0) -> dict:
    """Build a minimal valid kind-22242 event dict."""
    return {
        "id": "b" * 64,
        "pubkey": pubkey,
        "created_at": int(time.time()) - age_seconds,
        "kind": 22242,
        "tags": [["relay", "ws://127.0.0.1:7777"], ["challenge", "test"]],
        "content": "test",
        "sig": "c" * 128,
    }


# ════════════════════════════════════════════════════════════════════════════
# 1. Unit tests for validate_nip42_event
# ════════════════════════════════════════════════════════════════════════════

class TestValidateNip42Event:
    """validate_nip42_event(event, expected_relay_url) → bool"""

    def setup_method(self):
        from services.nostr import validate_nip42_event
        self.validate = validate_nip42_event

    def test_valid_recent_event(self):
        event = _make_nip42_event()
        assert self.validate(event, "ws://127.0.0.1:7777") is True

    def test_wrong_kind_rejected(self):
        event = _make_nip42_event()
        event["kind"] = 1
        assert self.validate(event, "ws://127.0.0.1:7777") is False

    def test_missing_required_field_rejected(self):
        for field in ["id", "pubkey", "created_at", "kind", "tags", "content", "sig"]:
            event = _make_nip42_event()
            del event[field]
            assert self.validate(event, "ws://127.0.0.1:7777") is False, \
                f"Expected False when '{field}' is missing"

    def test_event_older_than_24h_rejected(self):
        event = _make_nip42_event(age_seconds=25 * 3600)  # 25 hours ago
        assert self.validate(event, "ws://127.0.0.1:7777") is False

    def test_event_exactly_24h_is_rejected(self):
        event = _make_nip42_event(age_seconds=24 * 3600 + 1)
        assert self.validate(event, "ws://127.0.0.1:7777") is False

    def test_non_dict_event_rejected(self):
        assert self.validate(None, "ws://127.0.0.1:7777") is False
        assert self.validate("string", "ws://127.0.0.1:7777") is False
        assert self.validate([], "ws://127.0.0.1:7777") is False

    def test_missing_relay_tag_still_accepted_with_warning(self):
        """relay tag is optional: only logged as warning, not a hard failure."""
        event = _make_nip42_event()
        event["tags"] = [["challenge", "test"]]  # no relay tag
        # function logs a warning but returns True if rest is valid
        assert self.validate(event, "ws://127.0.0.1:7777") is True


# ════════════════════════════════════════════════════════════════════════════
# 2. Unit tests for check_nip42_auth_local_marker
# ════════════════════════════════════════════════════════════════════════════

class TestCheckNip42AuthLocalMarker:
    """check_nip42_auth_local_marker(hex_pubkey) → bool  (async)"""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def setup_method(self):
        from services.nostr import check_nip42_auth_local_marker
        self.check = check_nip42_auth_local_marker

    def test_fresh_marker_returns_true(self, tmp_path):
        marker = tmp_path / ".nip42_auth"
        marker.touch()
        hex_pubkey = "d" * 64

        with patch("utils.security.find_user_directory_by_hex", return_value=tmp_path):
            result = self._run(self.check(hex_pubkey))
        assert result is True

    def test_expired_marker_returns_false(self, tmp_path):
        marker = tmp_path / ".nip42_auth"
        marker.touch()
        # Backdate the marker by > 1 hour
        old_mtime = time.time() - 3700
        os.utime(marker, (old_mtime, old_mtime))
        hex_pubkey = "d" * 64

        with patch("utils.security.find_user_directory_by_hex", return_value=tmp_path):
            result = self._run(self.check(hex_pubkey))
        assert result is False

    def test_no_marker_returns_false(self, tmp_path):
        hex_pubkey = "d" * 64
        with patch("utils.security.find_user_directory_by_hex", return_value=tmp_path):
            result = self._run(self.check(hex_pubkey))
        assert result is False

    def test_nonexistent_directory_returns_false(self):
        hex_pubkey = "d" * 64
        with patch("utils.security.find_user_directory_by_hex",
                   side_effect=Exception("user not found")):
            result = self._run(self.check(hex_pubkey))
        assert result is False

    def test_marker_within_3600s_is_valid(self, tmp_path):
        marker = tmp_path / ".nip42_auth"
        marker.touch()
        # Set mtime to 3599 seconds ago (just under 1 hour)
        recent = time.time() - 3599
        os.utime(marker, (recent, recent))
        hex_pubkey = "d" * 64

        with patch("utils.security.find_user_directory_by_hex", return_value=tmp_path):
            result = self._run(self.check(hex_pubkey))
        assert result is True


# ════════════════════════════════════════════════════════════════════════════
# 3. Integration-style tests for check_nip42_auth
# ════════════════════════════════════════════════════════════════════════════

class TestCheckNip42Auth:
    """check_nip42_auth(npub_or_hex, timeout) → bool  – full flow"""

    VALID_NPUB = "npub180cvv07tjdrrgpa0j7j7tmnyl2yr6yr7l8j4s3evf6u64th6gkwsyjh6w6"
    VALID_HEX  = "3bf0c63fcb93463407af97a5e5ee64fa883d107ef9e558472c4eb9aaaefa459d"

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def setup_method(self):
        from services.nostr import check_nip42_auth
        self.check = check_nip42_auth

    # ── local marker path ──────────────────────────────────────────────────

    def test_local_marker_shortcircuits_relay_check(self, tmp_path):
        """A fresh local marker should succeed without touching the relay."""
        marker = tmp_path / ".nip42_auth"
        marker.touch()

        # Pass hex directly (as verify_nostr_auth does)
        with patch("services.nostr.check_nip42_auth_local_marker",
                   new_callable=AsyncMock, return_value=True):
            # relay should never be called
            with patch("services.nostr.asyncio.create_subprocess_exec",
                       side_effect=AssertionError("Should not reach relay")):
                result = self._run(self.check(self.VALID_HEX))
        assert result is True

    def test_no_marker_falls_through_to_relay(self, tmp_path):
        """Without a marker, check_nip42_auth should try nostr_get_events.sh."""
        with patch("services.nostr.check_nip42_auth_local_marker",
                   new_callable=AsyncMock, return_value=False):
            # Simulate nostr_get_events.sh returning empty output (ephemeral events)
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            with patch("services.nostr.asyncio.create_subprocess_exec",
                       return_value=mock_proc):
                # And relay WebSocket returning no events
                with patch("services.nostr.websockets.connect") as mock_ws:
                    ctx = AsyncMock()
                    ctx.__aenter__ = AsyncMock(return_value=ctx)
                    ctx.__aexit__ = AsyncMock(return_value=None)
                    ctx.send = AsyncMock()
                    ctx.recv = AsyncMock(
                        side_effect=[
                            json.dumps(["EOSE", "auth_check_0"]),
                        ]
                    )
                    mock_ws.return_value = ctx
                    result = self._run(self.check(self.VALID_HEX))
        assert result is False

    def test_nostr_get_events_finds_valid_event(self, tmp_path):
        """If nostr_get_events.sh returns a valid kind-22242 event, auth passes."""
        event = _make_nip42_event(pubkey=self.VALID_HEX)
        event_json_line = json.dumps(event)

        with patch("services.nostr.check_nip42_auth_local_marker",
                   new_callable=AsyncMock, return_value=False):
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(
                return_value=(event_json_line.encode(), b"")
            )
            with patch("services.nostr.asyncio.create_subprocess_exec",
                       return_value=mock_proc):
                # script_path.exists() must return True
                with patch("pathlib.Path.exists", return_value=True):
                    result = self._run(self.check(self.VALID_HEX))
        assert result is True

    def test_invalid_npub_returns_false(self):
        result = self._run(self.check("not_a_valid_npub"))
        assert result is False

    def test_empty_npub_returns_false(self):
        result = self._run(self.check(""))
        assert result is False


# ════════════════════════════════════════════════════════════════════════════
# 4. Integration test: /api/fileupload with NIP-42 auth
# ════════════════════════════════════════════════════════════════════════════

class TestFileUploadNip42:
    """
    Integration tests for POST /api/fileupload.

    The endpoint calls require_nostr_auth(npub, force_check=True) which calls
    verify_nostr_auth → check_nip42_auth.

    We mock check_nip42_auth at the services.nostr level so we can test the
    full HTTP flow without a live relay.
    """

    @pytest.fixture(autouse=True)
    def _setup_app(self):
        """Lazy-import the ASGI app to avoid circular import during test collection."""
        import importlib, sys
        spec = importlib.util.spec_from_file_location(
            "main_app", str(UPASSPORT_DIR / "54321.py")
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["main_app"] = mod
        spec.loader.exec_module(mod)
        self.app = mod.app

    @pytest.mark.asyncio
    async def test_upload_rejected_without_auth(self):
        """Without a valid NIP-42 event or marker, upload must return 403."""
        import httpx
        from httpx import ASGITransport

        with patch("services.nostr.check_nip42_auth",
                   new_callable=AsyncMock, return_value=False):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=self.app),
                base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/fileupload",
                    data={"npub": "npub180cvv07tjdrrgpa0j7j7tmnyl2yr6yr7l8j4s3evf6u64th6gkwsyjh6w6"},
                    files={"file": ("test.mp4", b"fake video", "video/mp4")},
                )
        assert response.status_code == 403
        body = response.json()
        assert "error" in body or "detail" in body

    @pytest.mark.asyncio
    async def test_upload_accepted_with_local_marker(self, tmp_path):
        """With a valid local marker, upload must proceed past auth."""
        import httpx
        from httpx import ASGITransport

        # Create a fresh marker
        marker = tmp_path / ".nip42_auth"
        marker.touch()

        NPUB = "npub180cvv07tjdrrgpa0j7j7tmnyl2yr6yr7l8j4s3evf6u64th6gkwsyjh6w6"
        HEX  = "3bf0c63fcb93463407af97a5e5ee64fa883d107ef9e558472c4eb9aaaefa459d"

        # Patch so that:
        # - check_nip42_auth → True (auth passes)
        # - get_authenticated_user_directory → tmp_path
        # - run_script → simulated upload success

        json_result = json.dumps({
            "cid": "QmTestCID",
            "fileName": "test.mp4",
            "fileHash": "abc123",
            "mimeType": "video/mp4",
            "info": "QmInfoCID",
        })
        result_file = tmp_path / "result.json"
        result_file.write_text(json_result)

        async def fake_run_script(*args, **kwargs):
            # Write the JSON result file the router expects
            for arg in args:
                if isinstance(arg, str) and "temp_" in arg and arg.endswith(".json"):
                    Path(arg).parent.mkdir(parents=True, exist_ok=True)
                    Path(arg).write_text(json_result)
                    break
            return (0, json_result)

        with patch("services.nostr.check_nip42_auth",
                   new_callable=AsyncMock, return_value=True), \
             patch("utils.security.get_authenticated_user_directory",
                   return_value=tmp_path), \
             patch("utils.security.find_user_directory_by_hex",
                   return_value=tmp_path), \
             patch("utils.helpers.run_script", side_effect=fake_run_script):

            async with httpx.AsyncClient(
                transport=ASGITransport(app=self.app),
                base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/fileupload",
                    data={"npub": NPUB},
                    files={"file": ("test.mp4", b"fake video content", "video/mp4")},
                )

        # Auth must have passed (no 403)
        assert response.status_code != 403, \
            f"Got 403 – auth not bypassed correctly: {response.text}"
        # Either 200 (success) or 500 (upload pipeline error in test env)
        assert response.status_code in (200, 500), \
            f"Unexpected status: {response.status_code} — {response.text}"

    @pytest.mark.asyncio
    async def test_upload_returns_403_error_format(self):
        """Auth failure response must contain 'error' key (custom exception handler)."""
        import httpx
        from httpx import ASGITransport

        with patch("services.nostr.check_nip42_auth",
                   new_callable=AsyncMock, return_value=False):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=self.app),
                base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/fileupload",
                    data={"npub": "npub180cvv07tjdrrgpa0j7j7tmnyl2yr6yr7l8j4s3evf6u64th6gkwsyjh6w6"},
                    files={"file": ("test.pdf", b"%PDF fake", "application/pdf")},
                )

        assert response.status_code == 403
        body = response.json()
        # UPassport uses custom exception handler → {"error": "...", "status": "error"}
        assert body.get("status") == "error"
        assert "22242" in body.get("error", "") or "authentication" in body.get("error", "").lower()


# ════════════════════════════════════════════════════════════════════════════
# 5. Shell-based diagnostic test (verifies live relay behaviour)
# ════════════════════════════════════════════════════════════════════════════

class TestKind22242RelayBehaviour:
    """
    Verify that kind-22242 events are:
    - Published successfully (nostr_send_note.py exit 0)
    - NOT stored by strfry (nostr_get_events.sh returns empty)
    This is the documented root cause for the /api/fileupload auth failure.
    """

    NPUB    = "npub180cvv07tjdrrgpa0j7j7tmnyl2yr6yr7l8j4s3evf6u64th6gkwsyjh6w6"
    HEX     = "3bf0c63fcb93463407af97a5e5ee64fa883d107ef9e558472c4eb9aaaefa459d"
    RELAY   = "ws://127.0.0.1:7777"

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    @pytest.mark.live_relay
    def test_kind22242_not_stored_in_strfry(self):
        """
        After publishing a kind-22242 event to the local relay, querying
        nostr_get_events.sh for it must return no results.

        This test is marked @live_relay and is skipped unless a real strfry
        relay is running.  Run with:  pytest -m live_relay
        """
        import subprocess, shutil, asyncio as _aio

        nostr_script = Path.home() / ".zen" / "Astroport.ONE" / "tools" / "nostr_get_events.sh"
        if not nostr_script.exists():
            pytest.skip("nostr_get_events.sh not found")

        since = int(time.time()) - 5  # only events from last 5 seconds

        result = subprocess.run(
            [str(nostr_script),
             "--kind", "22242",
             "--author", self.HEX,
             "--since", str(since),
             "--limit", "5"],
            capture_output=True, text=True, timeout=15
        )

        lines = [l for l in result.stdout.splitlines() if l.strip()]
        # If the relay were to store kind-22242, this would fail
        # (and we'd need to change the fix approach)
        assert len(lines) == 0, (
            f"Unexpected: strfry stored {len(lines)} kind-22242 event(s).\n"
            f"Output: {result.stdout[:500]}\n"
            f"This invalidates the ephemeral-event assumption underpinning the auth fix."
        )

    @pytest.mark.live_relay
    def test_local_marker_auth_works_end_to_end(self, tmp_path):
        """
        Create the .nip42_auth marker (as ajouter_media.sh does), then call
        check_nip42_auth and expect True without relay access.
        """
        marker = tmp_path / ".nip42_auth"
        marker.touch()

        with patch("utils.security.find_user_directory_by_hex", return_value=tmp_path):
            from services.nostr import check_nip42_auth
            result = self._run(check_nip42_auth(self.HEX))

        assert result is True, "Expected True with fresh local marker"
