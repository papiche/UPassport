"""
services/trace.py — Journal de suivi horodaté des traitements de fond (descriptions, vectorisation…).

Niveau = variable d'environnement `FACECLOUD_VERBOSE` (0 silencieux · 1 une ligne par étape/photo · 2 tout :
commandes lancées, réponses brutes, durées, SQL, avant/après nettoyage). Les scripts (`ucloud_describe.py -v`,
`-vv`) la positionnent : les sous-processus (`vision_describe.py`, `describe_image.py`) l'héritent et écrivent
sur stderr au même format, donc UNE seule chronologie. Tout va sur stderr : le stdout (JSON, résumés) reste intact.
"""
from __future__ import annotations

import os
import sys
import time


def level() -> int:
    try:
        return int(os.environ.get("FACECLOUD_VERBOSE", "0") or 0)
    except ValueError:
        return 0


def set_level(n: int) -> None:
    os.environ["FACECLOUD_VERBOSE"] = str(max(0, int(n)))


def log(component: str, msg: str, lvl: int = 1) -> None:
    if level() >= lvl:
        t = time.time()
        print(f"[{time.strftime('%H:%M:%S', time.localtime(t))}.{int((t % 1) * 1000):03d}] {component}: {msg}",
              file=sys.stderr, flush=True)


class Timer:
    """with Timer() as t: … ; t.s = secondes écoulées."""

    def __enter__(self):
        self._t = time.time()
        self.s = 0.0
        return self

    def __exit__(self, *exc):
        self.s = time.time() - self._t
        return False
