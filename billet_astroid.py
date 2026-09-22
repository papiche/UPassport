#!/usr/bin/env python3
"""
billet_astroid.py — Génère l'image "AstroID / ZenCard" (QR chiffré GPG du
secret SALT/PEPPER + email en overlay) utilisée par VISA.print.sh pour
l'impression physique sur étiqueteuse Brother QL.

Remplace G1BILLET/MAKE_G1BILLET.sh (projet fermé) sur ce seul chemin — celui
réellement utilisé par VISA.print.sh (le reste de G1BILLET : styles de
billet, CLI G1BILLETS.sh, mode ZenCard+@ lié à un email n'est PAS reproduit).

Format de sortie strictement compatible avec le décodeur existant
`UPassport/upassport.sh` (détection `QRCODE:0:5 == "~~~~~"`, pipeline
`urldecode | tr '_' '+' | tr '-' '\\n' | tr '~' '-'`, payload `/?salt=…&pepper=…`)
— ne pas modifier l'un sans l'autre.

Le PIN (passphrase GPG) n'est JAMAIS passé en argv : il est lu sur stdin
(une ligne), pas dans les arguments du process.

Usage:
  echo "$PIN" | python3 billet_astroid.py --salt "..." --pepper "..." \\
      [--email "user@example.com"] --out /path/to/ZENCARD.png
"""
import argparse
import io
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ASSETS = Path(__file__).parent / "static" / "billet_styles"
LOGO_BG = ASSETS / "astrologo_nb.png"
FONT_PATH = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"


def _gpg_encrypt_armored(payload: bytes, passphrase: str) -> bytes:
    """Chiffrement symétrique GPG (armor). Passphrase via --passphrase-file
    (fichier /dev/shm, jamais en argv), jamais via stdin (déjà utilisé pour
    les données à chiffrer)."""
    shm = "/dev/shm" if os.path.isdir("/dev/shm") else None
    fd, pass_path = tempfile.mkstemp(dir=shm)
    try:
        os.write(fd, passphrase.encode())
        os.close(fd)
        os.chmod(pass_path, 0o600)
        proc = subprocess.run(
            ["gpg", "--batch", "--yes", "--passphrase-file", pass_path,
             "--symmetric", "--armor", "--output", "-"],
            input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30,
        )
        if proc.returncode != 0 or not proc.stdout:
            raise RuntimeError(f"gpg failed: {proc.stderr.decode()[:300]}")
        return proc.stdout
    finally:
        try:
            os.remove(pass_path)
        except OSError:
            pass


def _make_disco_qr(disco: str) -> bytes:
    """QR artistique (amzqr + fond astrologo_nb.png) avec repli qrencode."""
    amzqr_bin = shutil.which("amzqr") or str(Path.home() / ".astro" / "bin" / "amzqr")
    if (os.path.isfile(amzqr_bin) or shutil.which("amzqr")) and LOGO_BG.is_file():
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "qr.png")
            cmd = [amzqr_bin, disco, "-l", "H", "-p", str(LOGO_BG), "-c", "-n", "qr.png", "-d", td]
            try:
                subprocess.run(cmd, capture_output=True, timeout=30)
                if os.path.isfile(out):
                    return Path(out).read_bytes()
            except Exception:
                pass

    tmp2 = tempfile.mktemp(suffix=".png")
    try:
        subprocess.run(["qrencode", "-s", "6", "-o", tmp2, "--", disco], check=True, capture_output=True)
        return Path(tmp2).read_bytes()
    finally:
        try:
            os.unlink(tmp2)
        except OSError:
            pass


def _overlay_email(png_bytes: bytes, email: str) -> bytes:
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    if email:
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype(FONT_PATH, 28)
        except Exception:
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), email, font=font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        margin = 5
        x = max(0, img.width - w - margin)
        y = max(0, img.height - h - margin - 3)
        draw.text((x, y), email, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--salt", required=True)
    ap.add_argument("--pepper", required=True)
    ap.add_argument("--email", default="")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    pin = sys.stdin.readline().strip()
    if not pin:
        print("ERROR: PIN manquant sur stdin", file=sys.stderr)
        return 1

    usalt = urllib.parse.quote(args.salt, safe="")
    upepper = urllib.parse.quote(args.pepper, safe="")
    payload = f"/?salt={usalt}&pepper={upepper}\n".encode()

    try:
        armored = _gpg_encrypt_armored(payload, pin)
    except Exception as e:
        print(f"ERROR: chiffrement échoué: {e}", file=sys.stderr)
        return 1

    disco = urllib.parse.quote(
        armored.decode("ascii").replace("-", "~").replace("\n", "-").replace("+", "_"),
        safe="",
    )

    try:
        qr_png = _make_disco_qr(disco)
        final_png = _overlay_email(qr_png, args.email)
    except Exception as e:
        print(f"ERROR: génération QR échouée: {e}", file=sys.stderr)
        return 1

    Path(args.out).write_bytes(final_png)
    print(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
