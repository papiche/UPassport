"""
services/story_assistant.py — L'assistant « storyboard » du Studio vidéo, côté web.

Même conversation que `Astroport.ONE/IA/generators/generate_storyboard.sh` (en terminal) : l'IA discute, pose les questions
utiles, propose des pistes, puis produit le storyboard JSON de `generate_scene.sh`, validé par `generate_scene.sh --check`
(jusqu'à 3 tentatives de correction). Les CONSIGNES sont les mêmes : `IA/generators/lib/storyboard_prompts.json`, lu par le
script bash ET par ce module (une seule source, sinon les deux copies divergent) ; `web_extra` n'est ajouté qu'ici
(vidéos FUN courtes pour des amis, personnages tirés de vraies personnes).

Le serveur ne fait confiance qu'aux rôles « user » et « assistant » du client : la consigne système est toujours reconstruite ici.
Bibliothèque standard + story_asset : aucun accès à Ollama hors de ce module.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
if not OLLAMA_URL.startswith("http"):
    OLLAMA_URL = "http://" + OLLAMA_URL
# Écriture de scénario en français : gemma3:12b (comparé à minicpm/qwen vision, qui sont des modèles de description d'image).
DEFAULT_MODEL = os.environ.get("FACECLOUD_STORY_MODEL", "gemma3:12b")
FALLBACK_MODELS = ["gemma3:latest", "qwen2.5:latest", "llama3.2:latest"]
MAX_MESSAGES = 40
MAX_CHARS = 4000
MAX_ATTEMPTS = 3
CHAT_TIMEOUT = 180


class AssistantError(RuntimeError):
    """Erreur lisible par l'utilisateur (Ollama injoignable, storyboard invalide après 3 tentatives…)."""

    def __init__(self, message: str, raw: str = ""):
        super().__init__(message)
        self.raw = raw


# ── Le « tournage » : durée maximale, décor, consignes de l'utilisateur ────────────────────────────────────────────
MIN_SECONDS, MAX_SECONDS, DEFAULT_SECONDS = 5, 12, 8     # MiniMax H3 : plage entraînée ~5-15 s par plan → 5 s est la durée minimale, jamais de raccourcissement
DECOR_PATH = "$SCENES_DIR/decor/decor.jpg"
RATIOS = ("9:16", "16:9", "1:1", "4:3", "3:4")
WORDS_PER_SECOND = 2.2                   # débit de parole du modèle (cf. generate_scene.sh::fit_speech_duration)


def clamp_seconds(value: Any, default: int = DEFAULT_SECONDS) -> int:
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        n = default
    return max(MIN_SECONDS, min(MAX_SECONDS, n))


def clean_shoot(shoot: Any) -> Dict[str, Any]:
    """Les réglages de tournage reçus du client, ramenés à des valeurs sûres (jamais de texte libre non borné)."""
    s = shoot if isinstance(shoot, dict) else {}
    ratio = str(s.get("decor_ratio") or "")
    return {"max_seconds": clamp_seconds(s.get("max_seconds")),
            "brief": re.sub(r"\s+", " ", str(s.get("brief") or "")).strip()[:400],
            "decor": bool(s.get("decor")), "decor_ratio": ratio if ratio in RATIOS else "",
            "to": re.sub(r"[\r\n]+", " ", str(s.get("to") or "")).strip()[:60],
            "contact": re.sub(r"[^\w.:@+-]", "", str(s.get("contact") or ""))[:64]}


def shoot_block(shoot: Dict[str, Any]) -> str:
    """Consigne ajoutée à l'assistant : durée maximale, nombre de plans, longueur de réplique, décor imposé, brief."""
    n = shoot["max_seconds"]
    plans = "UN SEUL plan" if n <= 8 else "UN ou DEUX plans"
    words = min(14, max(3, int(n * WORDS_PER_SECOND)))
    lines = ["CONTRAINTES DE TOURNAGE (fixées par l'utilisateur, à respecter strictement) :",
             f"- Durée totale maximale : {n} secondes (la somme des \"duration\" ne doit pas dépasser {n} ; un plan dure au moins {MIN_SECONDS} s). Donc {plans}.",
             f"- Une seule réplique parlée, de {words} mots au plus, et le geste ou la chute doivent tenir dans ces {n} secondes."]
    if shoot["decor"]:
        lines.append("- DÉCOR IMPOSÉ : l'utilisateur fournit une photo qui sert de décor à chaque plan. Ne redéfinis pas le lieu : "
                     "décris l'action, les gestes, la lumière et l'ambiance DANS ce décor. N'écris ni \"decor\" ni \"image\" ni \"image_prompt\" : "
                     "le serveur ajoute le décor.")
    if shoot["brief"]:
        lines.append(f"- Consignes de tournage de l'utilisateur : {shoot['brief']}")
    if shoot["to"]:
        lines.append(f"- La vidéo est destinée à : {shoot['to']}.")
    return "\n".join(lines)


def fit_shots(shots: List[dict], max_seconds: int) -> List[dict]:
    """Garde les plans (dans l'ordre) tant que le total tient dans `max_seconds` ; un plan trop long est ramené à la durée permise
    (jamais moins de MIN_SECONDS : minimum du moteur)."""
    cap = max(max_seconds, MIN_SECONDS)
    kept, total = [], 0.0
    for sh in shots:
        d = float(sh.get("duration") or MIN_SECONDS)
        if kept and total + d > max_seconds + 0.01:
            break
        sh = dict(sh)
        sh["duration"] = int(min(d, cap)) if d == int(d) else min(d, cap)
        kept.append(sh)
        total += sh["duration"]
    return kept


def is_ai_shot(sh: dict) -> bool:
    """Plan calculé par le modèle vidéo (donc doté d'une image finale) : ni capture d'écran, ni carton, ni vidéo importée."""
    return not (sh.get("screen") or sh.get("card") or isinstance(sh.get("video"), str))


def chain_shots(shots: List[dict]) -> List[dict]:
    """Les plans se SUIVENT : chaque plan IA qui en suit un autre repart de l'image finale du précédent (`continue: true`, géré par
    generate_scene.sh : image de départ, ou référence de plus pour les plans avec acteur ou décor). Un plan qui a sa propre image de
    départ (`image`, `image_prompt`) ou qui suit un plan sans image finale (écran, carton) est laissé tel quel."""
    for i, sh in enumerate(shots):
        if i == 0:
            sh.pop("continue", None)                              # le premier plan ne peut pas « continuer »
            continue
        if not is_ai_shot(sh) or not is_ai_shot(shots[i - 1]) or sh.get("image") or sh.get("image_prompt"):
            continue
        sh["continue"] = True
    return shots


def apply_shoot(sb: dict, shoot: Dict[str, Any]) -> dict:
    """Applique les réglages de tournage au storyboard (idempotent) : durée maximale respectée, décor sur chaque plan IA, format
    du décor, bloc `fun` (destinataire, durée, consignes) lu plus tard pour ranger la vidéo dans /VideoFUN/ de l'ami."""
    out = json.loads(json.dumps(sb))
    out["shots"] = fit_shots(out.get("shots") or [], shoot["max_seconds"])
    if shoot["decor"]:
        for sh in out["shots"]:
            if not is_ai_shot(sh):
                continue
            for k in ("image", "image_prompt"):                    # incompatibles avec un décor de référence (ref2va)
                sh.pop(k, None)
            sh["decor"] = DECOR_PATH
        if shoot["decor_ratio"]:
            out["ratio"] = shoot["decor_ratio"]
    chain_shots(out["shots"])
    out["fun"] = {k: v for k, v in {"to": shoot["to"], "contact": shoot["contact"], "max_seconds": shoot["max_seconds"],
                                    "brief": shoot["brief"], "decor": shoot["decor"]}.items() if v not in ("", None)}
    return out


def prompts_path(gen_dir: Path) -> Path:
    return Path(gen_dir) / "lib" / "storyboard_prompts.json"


def load_prompts(gen_dir: Path) -> Dict[str, str]:
    p = prompts_path(gen_dir)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AssistantError(f"consignes de l'assistant introuvables ou illisibles ({p}) : installation incomplète") from exc
    for k in ("system", "final", "web_extra"):
        if not isinstance(data.get(k), str) or not data[k].strip():
            raise AssistantError(f"consignes de l'assistant incomplètes : « {k} » manquant dans {p}")
    return data


def cast_block(cast: List[Dict[str, str]]) -> str:
    """Le bloc « personnages disponibles » : une ligne « - nom : apparence » par personnage (même forme que le script bash)."""
    if not cast:
        return "\n(aucun pour l'instant)"
    out = ""
    for c in cast:
        name = re.sub(r"[\r\n]+", " ", str(c.get("name", ""))).strip()[:60]
        look = re.sub(r"[\r\n]+", " ", str(c.get("look", ""))).strip()[:300]
        if name:
            out += f"\n- {name}" + (f" : {look}" if look else "")
    return out or "\n(aucun pour l'instant)"


def system_prompt(gen_dir: Path, cast: List[Dict[str, str]], shoot: Optional[Dict[str, Any]] = None) -> str:
    p = load_prompts(gen_dir)
    out = p["system"].replace("{{CAST}}", cast_block(cast)) + "\n\n" + p["web_extra"]
    return out + ("\n\n" + shoot_block(shoot) if shoot else "")


def clean_messages(messages: Any) -> List[Dict[str, str]]:
    """Seuls « user » et « assistant » viennent du client (jamais « system » : la consigne est reconstruite côté serveur)."""
    if not isinstance(messages, list):
        raise AssistantError("messages : tableau attendu")
    out = []
    for m in messages[-MAX_MESSAGES:]:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            continue
        content = str(m.get("content") or "").strip()[:MAX_CHARS]
        if content:
            out.append({"role": m["role"], "content": content})
    if not out or out[-1]["role"] != "user":
        raise AssistantError("le dernier message doit être celui de l'utilisateur")
    return out


def _gpu_ensure(model: str) -> None:
    """La 3090 est PARTAGÉE avec ComfyUI : pas de chargement de modèle sans place (`Astroport.ONE/IA/gpu_headroom.py`)."""
    try:
        import sys
        from core.config import settings
        ia = str(settings.ZEN_PATH / "Astroport.ONE" / "IA")
        if ia not in sys.path:
            sys.path.append(ia)          # à la FIN : le venv ~/.astro de l'IA ne doit pas masquer la bibliothèque standard
        import gpu_headroom
    except ImportError as exc:
        raise AssistantError(f"garde-fou GPU introuvable (gpu_headroom.py) : {exc}") from exc
    try:
        gpu_headroom.ensure(model)
    except gpu_headroom.GpuBusy as exc:
        raise AssistantError(f"GPU occupé : {exc}") from exc


def _metric(path, body, t0, resp, error, gpu_call) -> None:
    """Journal des appels au GPU partagé (`Astroport.ONE/IA/ollama_metrics.py`) ; ne casse jamais l'appel mesuré."""
    if not gpu_call:
        return
    try:
        import ollama_metrics            # le chemin de l'IA a été ajouté par _gpu_ensure
        kind = "embed" if path == "/api/embed" else ("generate" if path == "/api/generate" else "chat")
        vision = any(m.get("images") for m in body.get("messages", []) if isinstance(m, dict))
        ollama_metrics.record("vision" if vision else kind, body["model"], t0, resp=resp, ok=error is None, error=error,
                              ctx=(body.get("options") or {}).get("num_ctx"), extra={"origine": "story_assistant"})
    except Exception as exc:
        print(f"[story_assistant] ⚠️ mesure non enregistrée : {exc}", file=sys.stderr)


def _http(path: str, body: Optional[dict] = None, timeout: float = 10.0) -> dict:
    gpu_call = bool(body and path in ("/api/chat", "/api/generate", "/api/embed") and body.get("model"))
    if gpu_call:
        _gpu_ensure(body["model"])
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{OLLAMA_URL}{path}", data, {"Content-Type": "application/json"} if data else {})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.load(r)
    except urllib.error.HTTPError as exc:
        _metric(path, body, t0, None, exc, gpu_call)
        raise AssistantError(f"Ollama HTTP {exc.code} : {exc.read().decode('utf-8', 'replace')[:200]}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _metric(path, body, t0, None, exc, gpu_call)
        raise AssistantError(f"Ollama injoignable ({exc}) : vérifiez le tunnel IA de la station (IA/services/ollama.me.sh)") from exc
    _metric(path, body, t0, out, None, gpu_call)
    return out


def installed_models() -> List[str]:
    """Modèles de TEXTE installés (sans embeddings, modèles de vision/OCR seuls ni modèles de code)."""
    names = [m["name"] for m in _http("/api/tags").get("models", [])]
    return [n for n in names if not re.search(r"embed|bge|paraphrase|nomic|snowflake|arctic|vision|ocr|vl\b|llava|moondream|minicpm|coder", n, re.I)]


def pick_model(wanted: str = "") -> str:
    have = installed_models()
    if wanted and wanted in have:
        return wanted
    for m in [DEFAULT_MODEL] + FALLBACK_MODELS:
        if m in have:
            return m
    if have:
        return have[0]
    raise AssistantError("aucun modèle de texte installé sur Ollama")


def chat(messages: List[Dict[str, str]], model: str, json_mode: bool = False) -> str:
    body: Dict[str, Any] = {"model": model, "messages": messages, "stream": False,
                            "options": {"temperature": 0.3 if json_mode else 0.6}, "keep_alive": "10m"}
    if json_mode:
        body["format"] = "json"
    reply = (_http("/api/chat", body, timeout=CHAT_TIMEOUT).get("message") or {}).get("content", "")
    if not reply.strip():
        raise AssistantError("réponse vide d'Ollama (réessayez)")
    return reply.strip()


def converse(gen_dir: Path, messages: Any, cast: List[Dict[str, str]], model: str = "", shoot: Any = None) -> Dict[str, str]:
    """Un tour de discussion : rend {reply, model}. `shoot` : réglages de tournage (durée max, décor, consignes)."""
    msgs = clean_messages(messages)
    m = pick_model(model)
    sysmsg = system_prompt(gen_dir, cast, clean_shoot(shoot) if shoot is not None else None)
    return {"reply": chat([{"role": "system", "content": sysmsg}] + msgs, m), "model": m}


def extract_json(text: str) -> Optional[dict]:
    """L'objet JSON de la réponse ; repli : le premier bloc {...} si le modèle a ajouté du texte autour."""
    cands = [text]
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        cands.append(m.group(0))
    for cand in cands:
        try:
            data = json.loads(cand)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def check_storyboard(gen_dir: Path, sb: dict) -> str:
    """Message d'erreur de `generate_scene.sh --check` (une seule source de vérité des règles du rendu), « » si valide."""
    script = Path(gen_dir) / "generate_scene.sh"
    if not script.exists():
        return f"{script} introuvable : installation incomplète"
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(sb, f, ensure_ascii=False)
        path = f.name
    try:
        proc = subprocess.run(["bash", str(script), "--check", path], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return "la validation du storyboard a dépassé 60 s"
    finally:
        Path(path).unlink(missing_ok=True)
    if proc.returncode == 0:
        return ""
    err = (proc.stderr or proc.stdout or "storyboard invalide").strip()
    return re.sub(r"^Erreur : ", "", err.splitlines()[-1] if err else err)


def generate(gen_dir: Path, messages: Any, cast: List[Dict[str, str]], model: str = "",
             validator: Optional[Callable[[dict], str]] = None, shoot: Any = None) -> Dict[str, Any]:
    """Storyboard final : l'IA reçoit la discussion + la demande de JSON, avec jusqu'à 3 tentatives de correction guidées
    par la validation (même conversation). Rend {storyboard, model, attempts, cast_names}."""
    msgs = clean_messages(messages)
    m = pick_model(model)
    prompts = load_prompts(gen_dir)
    validate = validator or (lambda sb: check_storyboard(gen_dir, sb))
    sh = clean_shoot(shoot) if shoot is not None else None
    convo = [{"role": "system", "content": system_prompt(gen_dir, cast, sh)}] + msgs + [{"role": "user", "content": prompts["final"]}]
    err, raw = "", ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        raw = chat(convo, m, json_mode=True)
        convo.append({"role": "assistant", "content": raw})
        sb = extract_json(raw)
        if sb is not None and sh:
            sb = apply_shoot(sb, sh)           # durée max, décor et bloc « fun » imposés par le serveur, pas laissés au bon vouloir de l'IA
        err = "le JSON n'a pas pu être analysé" if sb is None else validate(sb)
        if not err:
            return {"storyboard": sb, "model": m, "attempts": attempt, "cast_names": sorted((sb.get("cast") or {}).keys())}
        convo.append({"role": "user", "content": f"Ta réponse n'était pas un storyboard valide : {err}. "
                                                 "Renvoie UNIQUEMENT le JSON complet corrigé, rien d'autre."})
    raise AssistantError(f"l'IA n'a pas produit un storyboard valide après {MAX_ATTEMPTS} tentatives ({err})", raw)
