"""
tests/test_identity_keys.py — norme des clés (Astroport.ONE/tools/my.sh) côté UPassport.

Les identifiants (salt, pepper) peuvent désigner un compte récent (crédentiel ISOLÉ par UPlanet) ou un
compte historique (pepper brut) : la détection de conflit et la récupération silencieuse doivent connaître
les DEUX, et la formule isolée doit coïncider avec celle de make_NOSTRCARD.sh / keygen.

    pytest tests/test_identity_keys.py -v
"""
import asyncio
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from core.config import settings
from routers import identity

SALT, PEPPER, UPLANETNAME = "salt-test-42", "pepper-test-42", "uplanet-test-A"

# keygen est lancé par son shebang (`env python3`) : y mettre le python de CE processus (celui qui a
# duniterpy), sinon le système en choisit un autre et keygen répond une erreur d'import, pas un npub.
os.environ["PATH"] = os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")


def keygen_npub(salt: str, pepper: str) -> str:
    with tempfile.NamedTemporaryFile("w", delete=False) as fh:
        fh.write(f"{salt}\n{pepper}\n")
    try:
        res = subprocess.run([sys.executable, str(settings.TOOLS_PATH / "keygen"), "-t", "nostr", "-i", fh.name],
                             capture_output=True, text=True, timeout=30)
        assert res.stdout.strip().startswith("npub1"), f"keygen a échoué : {res.stderr[-200:]}"
        return res.stdout.strip()
    finally:
        Path(fh.name).unlink(missing_ok=True)


class TestDeriveNpubs(unittest.TestCase):
    def setUp(self):
        self._orig = identity.get_env_from_mysh

        async def fake_env(name):
            return UPLANETNAME if name == "UPLANETNAME" else ""
        identity.get_env_from_mysh = fake_env

    def tearDown(self):
        identity.get_env_from_mysh = self._orig

    def test_isolated_first_then_legacy(self):
        sel = hashlib.sha256(UPLANETNAME.encode()).hexdigest()[:16]
        got = asyncio.run(identity._derive_npubs_from_credentials(SALT, PEPPER))
        self.assertEqual(got, [keygen_npub(SALT, f"{PEPPER}_{sel}"), keygen_npub(SALT, PEPPER)])
        self.assertNotEqual(got[0], got[1])

    def test_two_uplanets_never_share_the_isolated_npub(self):
        a = asyncio.run(identity._derive_npubs_from_credentials(SALT, PEPPER))[0]

        async def other(name):
            return "uplanet-test-B" if name == "UPLANETNAME" else ""
        identity.get_env_from_mysh = other
        b = asyncio.run(identity._derive_npubs_from_credentials(SALT, PEPPER))[0]
        self.assertNotEqual(a, b)

    def test_without_uplanetname_never_guesses_an_isolated_npub(self):
        async def none(name):
            return ""
        identity.get_env_from_mysh = none
        got = asyncio.run(identity._derive_npubs_from_credentials(SALT, PEPPER))
        self.assertEqual(got, [keygen_npub(SALT, PEPPER)])


class TestPepperNostr(unittest.TestCase):
    """`pepper_nostr` du .multipass.json : suit la formule consignée dans .keyformula, jamais devinée."""

    def setUp(self):
        self._orig = identity.get_env_from_mysh

        async def fake_env(name):
            return UPLANETNAME if name == "UPLANETNAME" else ""
        identity.get_env_from_mysh = fake_env
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        identity.get_env_from_mysh = self._orig

    def run_for(self, keyformula):
        if keyformula is not None:
            (self.dir / ".keyformula").write_text(keyformula)
        data = {"pepper": PEPPER}
        asyncio.run(identity._add_pepper_nostr(data, self.dir))
        return data

    def test_legacy_account_keeps_raw_pepper(self):
        self.assertEqual(self.run_for("nostr=legacy g1=isolated ipns=isolated")["pepper_nostr"], PEPPER)

    def test_isolated_account_uses_isolated_pepper(self):
        sel = hashlib.sha256(UPLANETNAME.encode()).hexdigest()[:16]
        self.assertEqual(self.run_for("nostr=isolated g1=isolated ipns=isolated")["pepper_nostr"], f"{PEPPER}_{sel}")

    def test_unknown_formula_adds_nothing(self):
        self.assertNotIn("pepper_nostr", self.run_for(None))
        self.assertNotIn("pepper_nostr", self.run_for("nostr=bizarre"))


if __name__ == "__main__":
    unittest.main()
