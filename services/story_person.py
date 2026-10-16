"""
services/story_person.py — Transformer une PERSONNE de FaceCloud (visages rangés dans l'ucloud) en PERSONNAGE du Studio vidéo.

Le Studio (`generate_scene.sh`) joue ses personnages à partir d'un portrait (`portrait.png`, repris comme image de
référence par le générateur vidéo) et d'une voix. Ici le portrait vient des photos de la personne : on retient son plus beau
visage (grand, net), recadré « tête et épaules » en carré 1024 px. L'apparence (`look`, en anglais, pour le prompteur vidéo)
est décrite par le modèle de vision d'Ollama, sans jamais deviner d'identité.

Respect des personnes : l'appelant exige un consentement explicite, le personnage reste PRIVÉ (jamais publié à la coopérative
par ce chemin) et son fichier porte la trace du consentement.

Bibliothèque standard + Pillow.
"""

from __future__ import annotations

import base64
import io
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from services import story_assistant

PORTRAIT_SIDE = 1024
HEAD_SHOULDERS = 3.0          # côté du carré = 3 × la hauteur du visage : tête, cou et épaules
MAX_CANDIDATES = 6
MAX_PORTRAITS = 4              # photo principale + 3 autres vues : MiniMax H3 accepte 9 images de référence, il en reste pour décor et continuité
KEEP_RATIO = 0.4               # une photo supplémentaire doit avoir au moins 40 % de la netteté de la meilleure
NAME_OK = re.compile(r"[^\w \-'’]", re.UNICODE)


def clean_name(raw: str) -> str:
    """Un nom accepté par POST /api/story/assets : lettres, chiffres, espace, - _ ' ; 60 caractères ; premier mot si c'est un nom long."""
    n = NAME_OK.sub("", str(raw or "")).replace("’", "'").strip()
    n = re.sub(r"\s+", " ", n)
    if len(n) > 60:
        n = n.split(" ")[0][:60]
    return n


def bbox_area(payload: Dict[str, Any]) -> float:
    b = payload.get("bbox") or {}
    try:
        return max(0.0, (float(b["x2"]) - float(b["x1"])) * (float(b["y2"]) - float(b["y1"])))
    except (KeyError, TypeError, ValueError):
        return 0.0


def is_named(name: str) -> bool:
    n = (name or "").strip()
    return bool(n) and not re.match(r"(?i)^(inconnu|unknown|\?)", n)


def group_people(points: List[Dict[str, Any]], min_faces: int = 2, limit: int = 120, me_hex: str = "") -> List[Dict[str, Any]]:
    """Les personnes NOMMÉES du catalogue de visages (points Qdrant), les plus photographiées d'abord :
    [{key, name, pubkey, contact, faces, photos, sample_ids}] — `sample_ids` = ses 8 plus grands visages (le premier fait la vignette ; le portrait
    du personnage est choisi parmi eux). `me_hex` = ma clé NOSTR : ma propre personne (`me: true`) est acceptée dès 1 visage et passe en premier —
    on peut être la star de sa propre vidéo FUN."""
    people: Dict[str, Dict[str, Any]] = {}
    for pt in points:
        pl = pt.get("payload") or {}
        name = (pl.get("name") or "").strip()
        if pl.get("ref") == "contact" or not is_named(name):
            continue
        key = (pl.get("pubkey") or pl.get("contact") or name.lower()).strip()
        p = people.setdefault(key, {"key": key, "name": name, "pubkey": pl.get("pubkey") or "", "contact": "", "faces": 0,
                                    "photos": set(), "ids": []})
        p["contact"] = p["contact"] or str(pl.get("contact") or "")            # UID de la fiche du carnet d'adresses
        p["faces"] += 1
        if pl.get("source_path"):
            p["photos"].add(pl["source_path"])
            p["ids"].append((bbox_area(pl), str(pt.get("id"))))
    out = []
    for p in people.values():
        me = bool(me_hex) and p["pubkey"].lower() == me_hex.lower()
        if p["faces"] < (1 if me else min_faces) or not p["ids"]:
            continue
        ids = [i for _a, i in sorted(p["ids"], reverse=True)[:8]]        # les 8 plus grands visages : 1er = vignette, tous = portrait
        out.append({"key": p["key"], "name": p["name"], "pubkey": p["pubkey"], "contact": p["contact"], "faces": p["faces"],
                    "photos": len(p["photos"]), "sample_ids": ids, "me": me})
    out.sort(key=lambda x: (not x["me"], -x["faces"], x["name"].lower()))
    return out[:limit]


def candidate_faces(payloads: Dict[str, Dict[str, Any]], k: int = MAX_CANDIDATES) -> List[Tuple[str, Dict[str, Any]]]:
    """Les k plus grands visages, une seule fois par photo source (pas k visages de la même photo)."""
    seen, out = set(), []
    for pid, pl in sorted(payloads.items(), key=lambda kv: -bbox_area(kv[1])):
        sp = pl.get("source_path")
        if not sp or sp in seen or bbox_area(pl) <= 0:
            continue
        seen.add(sp)
        out.append((pid, pl))
        if len(out) >= k:
            break
    return out


def crop_portrait(plaintext: bytes, bbox: Dict[str, Any], side: int = PORTRAIT_SIDE) -> Tuple[bytes, int]:
    """Carré « tête et épaules » centré sur le visage, PNG `side`×`side`. Rend (png, côté source en pixels : la netteté possible)."""
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(plaintext))).convert("RGB")
    w, h = img.size
    x1, y1, x2, y2 = (float(bbox.get(k, d)) for k, d in (("x1", 0), ("y1", 0), ("x2", w), ("y2", h)))
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2 + (y2 - y1) * 0.25          # un peu plus bas : plus d'épaules que de front
    s = max(max(x2 - x1, y2 - y1) * HEAD_SHOULDERS, 64.0)
    s = min(s, float(min(w, h)))                                       # jamais plus grand que l'image
    left = min(max(0.0, cx - s / 2), w - s)
    top = min(max(0.0, cy - s / 2), h - s)
    crop = img.crop((int(left), int(top), int(left + s), int(top + s)))
    src_side = crop.size[0]
    crop = crop.resize((side, side), Image.LANCZOS)
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return buf.getvalue(), src_side


def sharpness(png: bytes) -> float:
    """Variance des contours d'une version 256 px : plus c'est net, plus c'est grand (un visage flou donne ≈ 0)."""
    from PIL import Image, ImageFilter, ImageStat
    img = Image.open(io.BytesIO(png)).convert("L")
    img.thumbnail((256, 256))
    return float(ImageStat.Stat(img.filter(ImageFilter.FIND_EDGES)).var[0])


def rank_portraits(crops: List[Tuple[Any, bytes, int]]) -> List[Tuple[Any, bytes, float]]:
    """(identifiant, png, côté source) → (identifiant, png, score) du plus net au moins net, même pondération que `best_portrait`."""
    scored = [(pid, png, sharpness(png) * min(1.0, side / 512.0)) for pid, png, side in crops]
    return sorted(scored, key=lambda t: -t[2])


def auto_pick(ranked: List[Tuple[Any, bytes, float]], n: int = 3) -> List[Tuple[Any, bytes, float]]:
    """Sélection automatique : la meilleure, puis les suivantes tant qu'elles restent assez nettes (jamais une photo floue de plus)."""
    if not ranked:
        return []
    top = ranked[0][2]
    return [r for r in ranked[:min(n, MAX_PORTRAITS)] if r is ranked[0] or r[2] >= KEEP_RATIO * top]


def best_portrait(crops: List[Tuple[bytes, int]]) -> Optional[Tuple[bytes, float]]:
    """Parmi (png, côté source) : le plus net, pénalisé s'il vient d'une très petite zone de l'image."""
    best, best_score = None, -1.0
    for png, src_side in crops:
        score = sharpness(png) * min(1.0, src_side / 512.0)
        if score > best_score:
            best, best_score = png, score
    return (best, best_score) if best is not None else None


LOOK_PROMPT = (
    "You prepare a reference for an AI video generator. Look at this portrait photo. Answer with EXACTLY two lines, in English, "
    "no other text.\nLOOK: one sentence (max 30 words) describing the person's apparent age range, gender expression, hair, "
    "glasses/beard if any, clothing and expression.\nVOICE: a natural voice description (max 12 words: age, gender, pitch, tone) that fits this person.\n"
    "Never guess a name or identity."
)


def parse_look(text: str) -> Tuple[str, str]:
    """(look, voix) lus dans la réponse « LOOK: … / VOICE: … » du modèle (gras markdown toléré)."""
    found = {"LOOK": "", "VOICE": ""}
    for line in text.splitlines():
        m = re.match(r"(?i)\s*\**(LOOK|VOICE)\**\s*[:：]\s*(.+)", line)
        if m:
            found[m.group(1).upper()] = m.group(2).strip().strip("*").strip()
    return found["LOOK"][:300], found["VOICE"][:120]


def describe_look(png: bytes, model: str) -> Tuple[str, str]:
    """(look, voice_design) décrits par le modèle de vision ; ("", "") si Ollama ne répond pas (non fatal : on peut les saisir)."""
    body = {"model": model, "stream": False, "keep_alive": "10m", "options": {"temperature": 0.2, "num_predict": 220},
            "messages": [{"role": "user", "content": LOOK_PROMPT, "images": [base64.b64encode(png).decode()]}]}
    try:
        reply = (story_assistant._http("/api/chat", body, timeout=90).get("message") or {}).get("content", "")
    except story_assistant.AssistantError:
        return "", ""
    return parse_look(reply)


def portrait_files(name: str, portraits: List[bytes]) -> Dict[str, bytes]:
    """`portrait.png` (la photo principale) puis `portrait_2.png`… (autres vues, 4 au plus) : generate_scene.sh les passe toutes
    au modèle vidéo comme images de référence du même visage."""
    out = {f"cast/{name}/portrait.png": portraits[0]}
    for i, png in enumerate(portraits[1:MAX_PORTRAITS], start=2):
        out[f"cast/{name}/portrait_{i}.png"] = png
    return out


def character_files(name: str, look: str, voice_design: str, portrait_png: Any) -> Dict[str, bytes]:
    """Fichiers du paquet personnage : fiche + portrait(s) (`portrait_png` = un PNG ou une liste, le premier est le principal).
    La voix (voice.wav) est produite ensuite par le rendu du personnage."""
    portraits = portrait_png if isinstance(portrait_png, list) else [portrait_png]
    import json
    # generate_character.sh exige un « look » non vide même quand le portrait existe déjà : repli générique si la vision est indisponible.
    meta = {"name": name, "look": look or "a person, head and shoulders portrait", "voice_design": voice_design or "friendly natural voice, warm and cheerful",
            "voice_line": f"Bonjour, je m'appelle {name}, et aujourd'hui j'ai une petite surprise pour vous !",
            "source": "facecloud", "private_only": True,
            "consent": {"given": True, "at": int(time.time()),
                        "note": "Portrait tiré des photos de la personne, avec son accord ; vidéos privées, envoyées à elle ou à ses proches."}}
    return {f"cast/{name}/character.json": json.dumps(meta, ensure_ascii=False, indent=1).encode("utf-8"), **portrait_files(name, portraits)}
