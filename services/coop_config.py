"""
services/coop_config.py — Schéma whitelist unique de configuration coopérative
(NODE ⊕ ARBOR), pont vers cooperative_config.sh (Kind 30800 NOSTR), validation.

Aucune écriture arbitraire : seules les clés déclarées dans COOP_CONFIG_SCHEMA
sont acceptées par les endpoints /api/nostr/admin/node_config* et arbor_* —
contrairement au CLI captain.sh qui accepte un nom de clé en texte libre.
ARBOR n'est qu'une catégorie de plus dans ce même schéma, pas un système
parallèle.
"""

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Optional

from core.config import settings

_COOP_SCRIPT = settings.ZEN_PATH / "Astroport.ONE" / "tools" / "cooperative_config.sh"

_SENSITIVE_TOKENS = ("TOKEN", "SECRET", "KEY", "PASSWORD", "API", "PRIVATE")


def is_sensitive(key: str) -> bool:
    """Même règle EXACTE que cooperative_config.sh::coop_config_set (auto-
    chiffrement à l'écriture) — dérivée du nom de clé, jamais un flag manuel
    qui pourrait diverger de ce qui est réellement chiffré au repos."""
    return any(t in key.upper() for t in _SENSITIVE_TOKENS)


@dataclass(frozen=True)
class CoopKey:
    key: str
    label: str
    help: str = ""
    kind: str = "text"  # text|number|percent|url|email|csv|bool|select
    choices: tuple = ()
    unit: str = ""
    placeholder: str = ""
    readonly: bool = False
    lo: Optional[float] = None
    hi: Optional[float] = None


@dataclass(frozen=True)
class CoopCategory:
    id: str
    icon: str
    title: str
    note: str = ""
    keys: tuple = ()


COOP_CONFIG_SCHEMA: tuple = (
    CoopCategory("economy", "💰", "Économie coopérative", "", (
        CoopKey("PAF", "PAF (Participation Aux Frais)", kind="number", unit="Ẑ", lo=0, placeholder="ex : 10",
                help="Montant mensuel prélevé pour couvrir les frais d'infrastructure de la station."),
        CoopKey("NCARD", "Prix NCARD (MULTIPASS)", kind="number", unit="Ẑ", lo=0, placeholder="ex : 1",
                help="Prix facturé à la création d'un MULTIPASS (identité NOSTR + wallet Ğ1)."),
        CoopKey("ZCARD", "Prix ZCARD (ZenCard)", kind="number", unit="Ẑ", lo=0, placeholder="ex : 4",
                help="Prix facturé à la création d'une ZenCard (compte sociétaire)."),
        CoopKey("ZENCARD_SATELLITE", "ZenCard Satellite", kind="number", unit="Ẑ", lo=0, placeholder="ex : 50",
                help="Tarif ZenCard pour le palier Satellite (sociétaire actif)."),
        CoopKey("ZENCARD_CONSTELLATION", "ZenCard Constellation", kind="number", unit="Ẑ", lo=0, placeholder="ex : 540",
                help="Tarif ZenCard pour le palier Constellation (opérateur de station)."),
    )),
    CoopCategory("fiscal", "🧾", "Fiscalité", "", (
        CoopKey("TVA_RATE", "Taux de TVA", kind="percent", unit="%", placeholder="ex : 20",
                help="Taux de TVA appliqué aux factures émises par la coopérative."),
        CoopKey("IS_RATE_REDUCED", "IS — taux réduit", kind="percent", unit="%", placeholder="ex : 15",
                help="Taux d'impôt sur les sociétés applicable sous le seuil ci-dessous."),
        CoopKey("IS_RATE_NORMAL", "IS — taux normal", kind="percent", unit="%", placeholder="ex : 25",
                help="Taux d'impôt sur les sociétés applicable au-delà du seuil ci-dessous."),
        CoopKey("IS_THRESHOLD", "Seuil IS taux réduit", kind="number", unit="€", lo=0, placeholder="ex : 42500",
                help="Bénéfice annuel en-dessous duquel le taux réduit s'applique."),
    )),
    CoopCategory("shares", "🤝", "Règle 3×1/3", "Doit sommer à 100% — non vérifié automatiquement entre les 3 clés, à contrôler avant sauvegarde.", (
        CoopKey("TREASURY_PERCENT", "Trésorerie", kind="percent", unit="%", placeholder="ex : 33",
                help="Part des excédents affectée à la trésorerie de réserve."),
        CoopKey("RND_PERCENT", "R&D", kind="percent", unit="%", placeholder="ex : 33",
                help="Part des excédents affectée à la recherche & développement."),
        CoopKey("ASSETS_PERCENT", "Actifs", kind="percent", unit="%", placeholder="ex : 34",
                help="Part des excédents affectée à l'acquisition d'actifs (matériel, infrastructure)."),
    )),
    CoopCategory("opencollective", "🌐", "OpenCollective", "", (
        CoopKey("OCSLUG", "Slug OpenCollective", placeholder="ex : monnaie-libre",
                help="Identifiant de la page OpenCollective (fin de l'URL opencollective.com/<slug>)."),
        CoopKey("OPENCOLLECTIVE_SLUG", "Slug OpenCollective (legacy)", placeholder="ex : monnaie-libre",
                help="Alias historique de OCSLUG — conservé pour compatibilité, préférer OCSLUG."),
        CoopKey("OCAPIKEY", "Clé API OpenCollective",
                help="Clé API personnelle OpenCollective (Account Settings → For Developers)."),
        CoopKey("TIER_SLUG_SATELLITE", "Tier slug Satellite", placeholder="ex : satellite",
                help="Identifiant du tier OpenCollective correspondant au palier Satellite."),
        CoopKey("TIER_SLUG_CONSTELLATION", "Tier slug Constellation", placeholder="ex : constellation",
                help="Identifiant du tier OpenCollective correspondant au palier Constellation."),
        CoopKey("TIER_SLUG_LABO", "Tier slug Labo", placeholder="ex : labo",
                help="Identifiant du tier OpenCollective correspondant au palier Labo."),
        CoopKey("TIER_SLUG_CLOUD", "Tier slug Cloud", placeholder="ex : cloud",
                help="Identifiant du tier OpenCollective correspondant au palier Cloud."),
    )),
    CoopCategory("mail", "✉️", "Mailjet", "", (
        CoopKey("MJ_APIKEY_PUBLIC", "Clé publique Mailjet",
                help="Clé API publique Mailjet (mailjet.com → Account Settings → API Keys)."),
        CoopKey("MJ_APIKEY_PRIVATE", "Clé privée Mailjet",
                help="Clé API secrète associée à la clé publique ci-dessus."),
        CoopKey("MJ_SENDER_EMAIL", "Email expéditeur", kind="email", placeholder="ex : contact@votredomaine.tld",
                help="Adresse expéditrice — doit être validée côté Mailjet avant usage."),
    )),
    CoopCategory("dns", "🌍", "DNS OVH", "", (
        CoopKey("OVH_APP_KEY", "Clé application OVH",
                help="Clé d'application OVH API (eu.api.ovh.com/createToken/)."),
        CoopKey("OVH_APP_SECRET", "Secret application OVH",
                help="Secret associé à la clé d'application ci-dessus."),
        CoopKey("OVH_CONSUMER_KEY", "Clé consommateur OVH",
                help="Clé consommateur générée lors de l'autorisation de l'application."),
        CoopKey("OVH_ZONE", "Zone DNS", placeholder="ex : votredomaine.tld",
                help="Nom de la zone DNS gérée par l'API OVH pour cette station."),
    )),
    CoopCategory("git", "🐙", "Forge Git", "", (
        CoopKey("GIT_HOST", "Hôte Git", placeholder="ex : github.com",
                help="Domaine de la forge Git utilisée pour /api/feedback (GitHub ou GitLab)."),
        CoopKey("GIT_OWNER", "Organisation / propriétaire", placeholder="ex : monorganisation",
                help="Compte ou organisation propriétaire du dépôt cible."),
        CoopKey("GIT_TOKEN", "Token d'accès Git",
                help="Token personnel avec droit de création d'issues sur le dépôt cible."),
    )),
    CoopCategory("ia", "🧠", "IA / LLM", "", (
        CoopKey("ANTHROPIC_API_KEY", "Clé API Anthropic (Claude)",
                help="Clé API console.anthropic.com — utilisée par les modules IA de la station."),
        CoopKey("GEMINI_API_KEY", "Clé API Google Gemini",
                help="Clé API Google AI Studio — alternative/complément à Anthropic."),
    )),
    CoopCategory("services", "🔌", "Services externes", "", (
        CoopKey("PLANTNET_API_KEY", "Clé API PlantNet",
                help="Clé API my.plantnet.org — utilisée par plantnet.html pour l'identification végétale."),
    )),
    CoopCategory("arbor", "🧬", "ARBOR — auto-amélioration IA", "Seuils de la boucle d'auto-amélioration BRO. Le déclenchement à distance reste soumis à ARBOR_REMOTE_TRIGGER_ENABLED ; le merge reste TOUJOURS manuel.", (
        CoopKey("ARBOR_MIN_CLUSTER_SIZE", "Taille minimale d'un cluster de besoins", kind="number", lo=2, hi=50,
                placeholder="défaut : 3", help="Nombre de demandes similaires nécessaires pour signaler un pattern récurrent."),
        CoopKey("ARBOR_SIMILARITY_THRESHOLD", "Seuil de similarité (clustering)", kind="number", lo=0.3, hi=0.95,
                placeholder="défaut : 0.68", help="Score cosinus minimal (0.3-0.95) pour regrouper deux demandes ensemble — plus haut = plus strict."),
        CoopKey("ARBOR_MAX_ITERATIONS", "Itérations max par run tool_forge", kind="number", lo=1, hi=6,
                placeholder="défaut : 3", help="Nombre de tentatives de correction avant abandon de la génération d'un outil."),
        CoopKey("ARBOR_CANDIDATE_MODELS", "Modèles Ollama candidats (CSV)", kind="csv",
                placeholder="ex : qwen2.5-coder:14b,qwen3:14b", help="Modèles testés lors d'un run explore/apply — laisser vide pour utiliser la sélection par défaut."),
        CoopKey("ARBOR_REMOTE_TRIGGER_ENABLED", "Autoriser le déclenchement distant", kind="bool",
                help="Doit être 'true' pour que le bouton ▶ du panneau ARBOR fonctionne. Désactivé par défaut."),
        CoopKey("ARBOR_TRIGGER_COOLDOWN_MIN", "Cooldown entre deux déclenchements distants", kind="number", unit="min", lo=1, hi=1440,
                placeholder="défaut : 30", help="Délai minimum entre deux runs déclenchés depuis le web (protection anti-abus)."),
    )),
)

ALL_KEYS: dict = {k.key: k for cat in COOP_CONFIG_SCHEMA for k in cat.keys}


class CoopConfigError(Exception):
    """Erreur de validation ou d'écriture — message directement affichable
    côté endpoint HTTP (400/500), jamais une trace Python brute."""


def validate_value(spec: CoopKey, raw: str) -> str:
    """Valide/normalise une valeur selon le type déclaré. Lève CoopConfigError
    sinon retourne la valeur normalisée à écrire."""
    if raw is None:
        raise CoopConfigError(f"{spec.key} : valeur manquante.")
    v = str(raw)
    if any(c in v for c in ("\n", "\r", "\x00")):
        # coop_config_get::tr -d '\n\r' tronquerait silencieusement une valeur
        # non chiffrée contenant un retour à la ligne — refusé en amont plutôt
        # que corrompu en silence côté lecture.
        raise CoopConfigError(f"{spec.key} : les retours à la ligne ne sont pas autorisés.")
    if len(v) > 4096:
        raise CoopConfigError(f"{spec.key} : valeur trop longue (max 4096 caractères).")
    if v.strip() == "":
        # Une valeur vide + clé sensible = coop_config_set stocke "" en clair,
        # puis coop_publish_config_to_nostr bloque la PUBLICATION DE TOUTE LA
        # CONFIG (elle itère sur toutes les clés et refuse de publier si une
        # clé sensible n'est pas chiffrée) — piège réel, refusé ici. Utiliser
        # la suppression dédiée (coop_delete) pour retirer une clé.
        raise CoopConfigError(f"{spec.key} : valeur vide refusée — utilisez la suppression dédiée.")

    if spec.kind in ("number", "percent"):
        try:
            num = float(v.replace(",", "."))
        except ValueError:
            raise CoopConfigError(f"{spec.key} : nombre attendu.")
        lo = spec.lo if spec.lo is not None else (0.0 if spec.kind == "percent" else None)
        hi = spec.hi if spec.hi is not None else (100.0 if spec.kind == "percent" else None)
        if lo is not None and num < lo:
            raise CoopConfigError(f"{spec.key} : doit être ≥ {lo}.")
        if hi is not None and num > hi:
            raise CoopConfigError(f"{spec.key} : doit être ≤ {hi}.")
        return str(num)
    if spec.kind == "bool":
        if v.strip().lower() not in ("true", "false"):
            raise CoopConfigError(f"{spec.key} : 'true' ou 'false' attendu.")
        return v.strip().lower()
    if spec.kind == "url":
        if not v.startswith(("http://", "https://")):
            raise CoopConfigError(f"{spec.key} : URL http(s) attendue.")
        return v
    if spec.kind == "email":
        if "@" not in v:
            raise CoopConfigError(f"{spec.key} : email attendu.")
        return v
    if spec.kind == "csv":
        if not re.match(r"^[A-Za-z0-9_,.:@ -]+$", v):
            raise CoopConfigError(f"{spec.key} : caractères non autorisés dans la liste CSV.")
        return v
    if spec.kind == "select":
        if spec.choices and v not in spec.choices:
            raise CoopConfigError(f"{spec.key} : valeur attendue parmi {', '.join(spec.choices)}.")
        return v
    return v


# ── Pont bash — mêmes patterns de sécurité que routers/finance.py::_get_coop_config
# (argv positionnels $1/$2, jamais d'interpolation de donnée utilisateur dans le
# texte du script bash lui-même) ────────────────────────────────────────────────

async def coop_get(key: str, timeout: float = 15.0) -> str:
    """Valeur déchiffrée d'une seule clé — réservé à un usage serveur interne
    (jamais renvoyée au client si sensible, cf. node_admin.py)."""
    if not _COOP_SCRIPT.exists():
        return ""
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", 'source "$1" >/dev/null 2>&1 && coop_config_get "$2" 2>/dev/null',
            "--", str(_COOP_SCRIPT), key,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode().strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


async def coop_load_raw(timeout: float = 20.0) -> dict:
    """Config brute complète — les valeurs non sensibles y sont déjà en clair,
    les sensibles au format "hex32:base64" (jamais déchiffrées ici : un seul
    appel suffit pour tout afficher, cf. is_sensitive côté appelant)."""
    if not _COOP_SCRIPT.exists():
        return {}
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", 'source "$1" >/dev/null 2>&1 && coop_load_config 2>/dev/null',
            "--", str(_COOP_SCRIPT),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            return {}
        return json.loads(stdout.decode().strip() or "{}")
    except Exception:
        return {}


async def coop_set_many(entries: list, timeout: float = 120.0) -> None:
    """Écrit plusieurs clés en UN seul cycle load/encrypt/save/publish (cf.
    coop_config_set_batch dans cooperative_config.sh). Valeurs transmises par
    STDIN — jamais argv (invisibles dans `ps aux`, et évite le bug de parsing
    `${_args[@]#_}` du dispatcher CLI de ce script, qui mange un underscore de
    tête). Lève CoopConfigError si le script rapporte une violation de
    sécurité (clé sensible non chiffrée) ou tout autre échec."""
    if not _COOP_SCRIPT.exists():
        raise CoopConfigError("cooperative_config.sh introuvable.")
    payload = "".join(f"{k}\t{v}\n" for k, v in entries)
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", 'source "$1" >/dev/null 2>&1 && coop_config_set_batch',
            "--", str(_COOP_SCRIPT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=payload.encode()), timeout=timeout)
    except asyncio.TimeoutError:
        raise CoopConfigError("Délai dépassé lors de l'écriture de la configuration.")
    except Exception as e:
        raise CoopConfigError(f"Échec d'exécution : {e}")
    err = stderr.decode(errors="replace")
    if "[CRITICAL] Security violation" in err:
        raise CoopConfigError(err.strip().splitlines()[-1])
    if proc.returncode == 75:
        raise CoopConfigError("Configuration en cours de modification par un autre appel — réessayez.")
    if proc.returncode != 0:
        raise CoopConfigError(err.strip() or "Échec d'écriture de la configuration.")


async def coop_delete(key: str, timeout: float = 60.0) -> None:
    if not _COOP_SCRIPT.exists():
        raise CoopConfigError("cooperative_config.sh introuvable.")
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", 'source "$1" >/dev/null 2>&1 && coop_config_delete "$2" 2>&1',
            "--", str(_COOP_SCRIPT), key,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except Exception as e:
        raise CoopConfigError(f"Échec d'exécution : {e}")
    if proc.returncode != 0:
        raise CoopConfigError(stdout.decode(errors="replace").strip() or "Échec de suppression.")
