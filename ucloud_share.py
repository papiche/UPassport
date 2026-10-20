#!/usr/bin/env python3
"""
ucloud_share.py — Partage de photos FaceCloud entre MULTIPASS (script CLI).

Point d'entrée des processus HORS UPassport (satellite_face_matcher.py,
bro_dm_daemon.sh), pour qu'ils n'importent pas la configuration UPassport.

Modes :
  ucloud_share.py <owner_email> <recipient_email> <from_hex> <from_name> <path>…
      Copie locale (destinataire sur CETTE station) → cloud_storage.share_copy.
      Une ligne JSON par chemin. Exit 0 si au moins une copie a réussi.

  ucloud_share.py --remote <owner_email> <friend_hex> <from_name> <path>…
      Destinataire sur une AUTRE station : résout son MULTIPASS et sa station
      (services/identity_links.py) puis envoie par le canal inter-NODE
      (services/node_share.py). Exit 0 envoyé, 3 station inconnue (l'appelant
      se rabat alors sur un message NIP-17), 1 erreur.

  ucloud_share.py --receive        (message JSON sur stdin)
      Station du DESTINATAIRE, appelé par bro_dm_daemon.sh à réception du
      canal `ucloud_share` : range les photos, envoie l'accusé de réception,
      un email récapitulatif au plus par expéditeur et par jour.

  ucloud_share.py --ack            (message JSON sur stdin)
      Station de l'EXPÉDITEUR, canal `ucloud_share_ack` : désépingle les
      copies temporaires reçues.
"""

import sys as _sys
import os as _os

_venv_python = _os.path.expanduser("~/.astro/bin/python3")
if _os.path.exists(_venv_python) and _sys.executable != _venv_python:
    _os.execv(_venv_python, [_venv_python] + _sys.argv)

_UPASSPORT_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _UPASSPORT_DIR not in _sys.path:
    _sys.path.insert(0, _UPASSPORT_DIR)
del _sys, _os

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from services import cloud_storage, identity_links, node_share  # noqa: E402


def _local(argv) -> int:
    owner_email, recipient_email, from_hex, from_name = argv[:4]
    ok = 0
    for path in argv[4:]:
        try:
            res = cloud_storage.share_copy(owner_email, path, recipient_email, from_hex, from_name)
            print(json.dumps({"path": path, "status": res["status"], "dest": res["path"]}))
            ok += 1
        except Exception as exc:
            print(json.dumps({"path": path, "status": "error", "error": str(exc)}))
            print(f"ERROR:ucloud_share:{path}: {exc}", file=sys.stderr)
    return 0 if ok else 1


def _remote(argv) -> int:
    owner_email, friend_hex, from_name = argv[:3]
    account = identity_links.email_for_hex(friend_hex)
    if not account or account.get("where") != "swarm":
        print(json.dumps({"status": "no_node"}))
        return 3
    res = node_share.send(owner_email, from_name, account, argv[3:])
    print(json.dumps(res))
    if res["status"] == "no_node":
        return 3
    return 0 if res["status"] == "sent" else 1


def _notify(email: str, from_name: str, n: int) -> None:
    """Un email récapitulatif au plus par (destinataire, expéditeur, jour)."""
    flag = cloud_storage.ensure_ucloud_dir(email) / f".received_{from_name[:40]}_{time.strftime('%Y%m%d')}"
    if flag.exists():
        return
    mailjet = Path.home() / ".zen" / "Astroport.ONE" / "tools" / "mailjet.sh"
    if not mailjet.exists():
        return
    who = from_name or "Un ami"
    from html import escape
    from services import degafam
    base = degafam.base_url()
    body = degafam.layout(
        f"{n} photo{'s' if n > 1 else ''} de vous, chez vous",
        f"<b>{escape(who)}</b> vous a reconnu·e sur des photos et vous en a envoyé une copie, rangée dans votre cloud "
        f"chiffré, dossier <code>/Reçues/{escape(who)}/</code>.",
        ["<b>Ouvrez FaceCloud</b> (bouton ci-dessous).", "<b>Retrouvez-les</b> dans l'onglet Photos → « Reçues ».",
         f"<b>Rendez la pareille</b> : FaceCloud peut retrouver {escape(who)} dans vos propres photos."],
        "Voir mes photos", f"{base}/earth/ucloud.html#photos",
        f"Et vos autres photos, contacts et agenda ? <a href=\"{base}/earth/degafam.html\">Le guide pas à pas</a> les met à l'abri.",
        preheader=f"{who} vous a reconnu·e sur des photos.", unsubscribe=False)
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as fh:
        fh.write(body)
    try:
        subprocess.run([str(mailjet), "--channel", "facecloud-invite", "--no-ipfs-link", email, fh.name,
                        f"📸 {who} vous a envoyé des photos"], capture_output=True, timeout=60)
        flag.touch()
    finally:
        Path(fh.name).unlink(missing_ok=True)


def _receive() -> int:
    message = json.load(sys.stdin)
    res = node_share.receive(message)
    print(json.dumps({k: v for k, v in res.items() if k != "received"}))
    if res.get("status") != "ok":
        return 1
    node_share.acknowledge(message, res)
    if res.get("copied"):
        _notify(res["email"], str(message.get("from_name") or ""), res["copied"])
    return 0


def _ack() -> int:
    print(json.dumps({"unpinned": node_share.on_ack(json.load(sys.stdin))}))
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["--remote"] and len(argv) >= 5:
        return _remote(argv[1:])
    if argv[:1] == ["--receive"]:
        return _receive()
    if argv[:1] == ["--ack"]:
        return _ack()
    if len(argv) >= 5 and not argv[0].startswith("--"):
        return _local(argv)
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
