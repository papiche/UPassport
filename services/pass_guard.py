"""
services/pass_guard.py — Vérification UNIQUE d'un code PASS, avec anti-bruteforce SERVEUR.

Le PASS n'a que 10 000 valeurs : le compteur vit dans le dossier du compte (`.pass.fails`, pas ~/.zen/tmp, purgé
chaque nuit), une erreur isolée s'oublie après WINDOW, un succès remet à zéro, et au MAX_FAILS-ième échec le PASS est
INVALIDÉ (toutes copies) et le Capitaine prévenu. Même fichiers, mêmes seuils pour TOUS les canaux :
  * routers/identity.py::_check_pass (/g1nostr, /g1/onboard, /atom4love/*) — enveloppe JSON ;
  * services/migration.py (déménagement : l'ANCIENNE station vérifie le PASS avant de geler le compte).
Sans FastAPI ni asyncio : appelable d'un process court. L'appelant fournit les journaux (`on_fail`, `on_lock`) et
envoie l'alerte au Capitaine quand `Verdict.alert_attempts > 0`.
"""
from __future__ import annotations

import fcntl
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

MAX_FAILS = 3
WINDOW = 24 * 3600


@dataclass
class Verdict:
    status: int
    error: str
    message: str
    attempts_left: Optional[int] = None
    locked: bool = False
    alert_attempts: int = 0          # > 0 : verrouillage à l'instant, nombre d'échecs à signaler au Capitaine

    def content(self) -> dict:
        out = {"error": self.error, "message": self.message}
        if self.attempts_left is not None:
            out["attempts_left"] = self.attempts_left
        if self.locked:
            out["locked"] = True
        return out


def _nostr(game: Path, email: str) -> Path:
    return game / "nostr" / email


def resolve_pass_file(game: Path, email: str) -> Optional[Path]:
    """nostr/<email>/.pass (copie autoritaire, jour 0) ; players/<email>/.pass en repli (anciens comptes)."""
    for base in ("nostr", "players"):
        p = game / base / email / ".pass"
        if p.exists():
            return p
    return None


def invalidate_pass_files(game: Path, email: str) -> bool:
    """Supprime TOUTES les copies du PASS : en laisser une rendrait le verrouillage inopérant."""
    done = False
    for base in ("nostr", "players"):
        p = game / base / email / ".pass"
        if p.exists():
            try:
                p.unlink()
                done = True
            except OSError as exc:
                logger.error("Impossible d'invalider %s : %s", p, exc)
    return done


def check(email: str, pass_code: str, context: str, ip: str = "?", game_path: Optional[Path] = None,
          on_fail: Optional[Callable[[int], None]] = None,
          on_lock: Optional[Callable[[bool, int], None]] = None) -> Optional[Verdict]:
    """None si le PASS est bon, sinon le Verdict d'erreur."""
    game = Path(game_path) if game_path else Path.home() / ".zen" / "game"
    if (_nostr(game, email) / ".pass.disabled").is_file():
        logger.warning("%s: PASS restore disabled by user for %s", context, email)
        return Verdict(403, "PASS_DISABLED",
                       "La récupération par code PASS a été désactivée pour ce compte. Contactez le support.")
    pass_file = resolve_pass_file(game, email)
    if not pass_file:
        logger.warning("%s: PASS file missing for %s", context, email)
        return Verdict(503, "PASS_UNAVAILABLE",
                       "Code PASS indisponible (bloqué après erreurs répétées ou jamais créé). "
                       "Contactez le Capitaine de votre station.")

    fails_path = _nostr(game, email) / ".pass.fails"
    with open(fails_path, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        try:
            state = json.loads(fh.read() or "{}")
        except json.JSONDecodeError:
            state = {}
        now = time.time()
        count = int(state.get("count", 0)) if now - float(state.get("last", 0)) < WINDOW else 0

        if hmac.compare_digest(str(pass_code).encode(), pass_file.read_text().strip().encode()):
            fh.seek(0)
            fh.truncate()
            return None

        count += 1
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps({"count": count, "last": now}))
    os.chmod(fails_path, 0o600)
    if on_fail:
        on_fail(count)
    if count < MAX_FAILS:
        left = MAX_FAILS - count
        logger.warning("%s: wrong PASS for %s (%d/%d) from %s", context, email, count, MAX_FAILS, ip)
        return Verdict(401, "INVALID_PASS",
                       f"Code PASS incorrect ({left} essai{'s' if left > 1 else ''} restant{'s' if left > 1 else ''}).",
                       attempts_left=left)

    invalidated = invalidate_pass_files(game, email)
    fails_path.unlink(missing_ok=True)
    logger.warning("%s: PASS invalidé pour %s après %d échecs depuis %s", context, email, count, ip)
    if on_lock:
        on_lock(invalidated, count)
    return Verdict(401, "INVALID_PASS",
                   "Code PASS incorrect. Récupération bloquée après 3 erreurs : votre Capitaine a été prévenu.",
                   attempts_left=0, locked=True, alert_attempts=count)
