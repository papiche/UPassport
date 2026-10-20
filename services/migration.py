"""
services/migration.py — Déménagement d'un MULTIPASS entre deux stations, à CLÉ IDENTIQUE.

Même salt/pepper donc même NSEC, même HEX, même wallet Ğ1, même clé IPNS : il n'y a
rien à re-signer, seulement des données à porter. Les stations se parlent par le canal
inter-NODE (Astroport.ONE/tools/nostr_node_intercom.py, NIP-44, comme node_share.py) ;
la PERSONNE reste l'autorité : la demande porte une preuve signée par sa clé.

    NOUVELLE station (cible)                         ANCIENNE station (source)
    ────────────────────────                         ─────────────────────────
    MULTIPASS recréé (même salt/pepper)
    migrate_request {email, hex, preuve} ──────────▶ vérifie la preuve (signature, fraîcheur,
                                                     destinataire, non rejouée) ; GÈLE le compte ;
                                                     paquet chiffré sur IPFS (ucloud_backup)
    importe, contrôle l'empreinte, épingle ◀──────── migrate_bundle {cid, clé, sha, counts}
    migrate_ack {cid, appliqué} ───────────────────▶ libère la clé IPNS (ipns_handover.sh release)
                                                     → séquence + valeur du record
    épingle le contenu, publie l'IPNS ◀───────────── migrate_commit {seq, value}
      avec une séquence SUPÉRIEURE (ipns_handover.sh claim) ;
      mises à jour publiques ciblées (tools/adopt_fixups.sh) ;
      épingle TOUS les documents du compte
      (services/account_pins.py : uDRIVE, ucloud,
      cookies, pièces jointes des événements)
    migrate_pinned {total, ok} ────────────────────▶ rien n'est nettoyé avant ce message ; s'il confirme
                                                     failed == 0 : dossiers du compte (MULTIPASS + ZenCard) DÉPLACÉS
                                                     vers ~/.zen/tmp/migrated/ (purgés à la prochaine 20h12), le
                                                     Capitaine ne suit plus le compte

L'ancienne station reste maître jusqu'à l'accusé : tant qu'il n'est pas arrivé, rien n'est
perdu (clé IPNS gardée, compte intact, simplement gelé). Si l'import échoue (erreur listée,
empreinte fausse, dimension d'embedding différente), AUCUN accusé n'est envoyé.

Authentification des messages : le daemon vérifie déjà que l'expéditeur est un NODE de la
constellation (_is_swarm_node) ; ce module vérifie en plus l'ÉTAT attendu (on n'accepte un
`migrate_bundle` que du NODE à qui l'on a demandé, pour ce compte).

État par compte, des deux côtés : ~/.zen/game/nostr/<email>/.migration.json (0600).
Gel : .migrating (ancienne station) et états target:requested|imported|claiming|error (nouvelle station) =
`account_frozen` (tools/my.sh) : NOSTRCARD.refresh.sh, PLAYER.refresh.sh et ZEN.ECONOMY.sh ignorent le compte
(ni redevance, ni rafraîchissement, ni purge). Le marqueur `.roaming` (visiteur, purgé après 24 h par
NODE.refresh.sh) est retiré dès la demande : un déménagement long ne doit pas voir son dossier effacé.

Ne passe JAMAIS par le serveur UPassport : process courts (multipass_migrate.py), car la
preuve est signée avec le NSEC du compte (même règle que calendar_nostr_bridge.py).
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from services import account_pins, cloud_storage, node_share, pass_guard

logger = logging.getLogger(__name__)

ZEN = Path.home() / ".zen"
IPNS_HANDOVER = ZEN / "Astroport.ONE" / "tools" / "ipns_handover.sh"
ADOPT_FIXUPS = ZEN / "Astroport.ONE" / "tools" / "adopt_fixups.sh"
MAIL_TEMPLATES = ZEN / "Astroport.ONE" / "templates" / "NOSTR"
MAILJET = ZEN / "Astroport.ONE" / "tools" / "mailjet.sh"
MY_SH = ZEN / "Astroport.ONE" / "tools" / "my.sh"
STATE_NAME = ".migration.json"
FROZEN_NAME = ".migrating"
PROOF_MAX_AGE = 600
BUNDLE_REUSE_MAX_AGE = 24 * 3600
MSG_TTL = 3 * 24 * 3600
CHANNELS = ("migrate_request", "migrate_bundle", "migrate_ack", "migrate_commit", "migrate_pinned")


class MigrationError(Exception):
    """Refus ou échec explicite (jamais de repli silencieux)."""


# ── état ────────────────────────────────────────────────────────────────────
def _user_dir(email: str) -> Path:
    return cloud_storage.GAME_NOSTR_PATH / email


def _players_dir(email: str) -> Path:
    """Dossier de la ZenCard : ~/.zen/game/players/<email> (frère de game/nostr)."""
    return cloud_storage.GAME_NOSTR_PATH.parent / "players" / email


def _valid_email(email: str) -> bool:
    return "@" in email and "/" not in email and ".." not in email


def load_state(email: str) -> Dict[str, Any]:
    try:
        return json.loads((_user_dir(email) / STATE_NAME).read_text())
    except (OSError, ValueError):
        return {}


def save_state(email: str, state: Dict[str, Any]) -> None:
    path = _user_dir(email) / STATE_NAME
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, path)


def is_frozen(email: str) -> bool:
    """Compte en cours de départ (ancienne station) : à ne plus rafraîchir ni facturer."""
    return (_user_dir(email) / FROZEN_NAME).exists()


def _account(email: str, hex_pub: str) -> None:
    """Le compte existe ICI avec CETTE clé (jamais un compte homonyme d'une autre clé)."""
    if not _valid_email(email):
        raise MigrationError("email invalide")
    if (cloud_storage.hex_for_email(email) or "") != str(hex_pub or "").lower():
        raise MigrationError(f"{email}: absent de cette station ou clé différente")


def my_node_hex() -> str:
    h = node_share._node_secret().get("HEX", "")
    if not re.fullmatch(r"[0-9a-f]{64}", h):
        raise MigrationError("identité NODE de cette station introuvable (~/.zen/game/secret.nostr)")
    return h


def _send(to_node_hex: str, channel: str, payload: Dict[str, Any]) -> None:
    if not node_share._intercom_send(to_node_hex, channel, payload, MSG_TTL):
        raise MigrationError(f"envoi {channel} vers {to_node_hex[:12]}… impossible (détail dans le journal)")


# ── preuve de la personne ───────────────────────────────────────────────────
def make_proof(email: str, to_node_hex: str) -> Dict[str, Any]:
    """Événement kind 27235 signé par la clé du MULTIPASS : « je demande mon
    déménagement vers CETTE station »."""
    import nostr_sdk
    import ucloud_backup
    keys = ucloud_backup._keys(email)
    tags = [["u", "internode://migrate"], ["method", "MIGRATE"], ["to", to_node_hex],
            ["email", email], ["nonce", os.urandom(8).hex()]]
    ev = nostr_sdk.EventBuilder(nostr_sdk.Kind(27235), "").tags(
        [nostr_sdk.Tag.parse(t) for t in tags]).finalize(keys)
    return json.loads(ev.as_json())


def _seen_path(email: str) -> Path:
    return cloud_storage.ensure_ucloud_dir(email) / "migrate_seen.json"


def check_proof(proof: Dict[str, Any], email: str, hex_pub: str, sender: str) -> None:
    """Lève MigrationError si la preuve n'est pas valable POUR CETTE demande."""
    from utils.crypto import verify_nostr_event
    if not isinstance(proof, dict) or proof.get("kind") != 27235:
        raise MigrationError("preuve absente ou de mauvais type")
    if not verify_nostr_event(proof):
        raise MigrationError("signature de la preuve invalide")
    if str(proof.get("pubkey", "")).lower() != str(hex_pub).lower():
        raise MigrationError("la preuve n'est pas signée par la clé de ce MULTIPASS")
    tags = {t[0]: t[1] for t in proof.get("tags", []) if isinstance(t, list) and len(t) >= 2}
    if tags.get("method") != "MIGRATE" or tags.get("email") != email:
        raise MigrationError("preuve établie pour une autre demande")
    if tags.get("to", "").lower() != sender.lower():
        raise MigrationError("la preuve désigne une autre station que l'expéditeur")
    if abs(time.time() - int(proof.get("created_at", 0))) > PROOF_MAX_AGE:
        raise MigrationError("preuve périmée")
    try:
        seen: List[str] = json.loads(_seen_path(email).read_text())
    except (OSError, ValueError):
        seen = []
    if proof["id"] in seen:
        raise MigrationError("preuve déjà utilisée (rejeu)")
    fd = os.open(str(_seen_path(email)), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump((seen + [proof["id"]])[-50:], fh)


# ── annuaire ────────────────────────────────────────────────────────────────
def find_source_node(email: str, hex_pub: str) -> Optional[str]:
    """NODEHEX de la station qui héberge ce MULTIPASS (annuaire swarm), hors la nôtre."""
    mine = os.environ.get("IPFSNODEID", "")
    for hex_file in (ZEN / "tmp" / "swarm").glob(f"*/TW/{email}/HEX"):
        node = hex_file.parents[2].name
        try:
            same = hex_file.read_text().strip().lower() == hex_pub
        except OSError:
            continue
        if same and node != mine:
            nh = node_share.node_hex_for(node)
            if nh:
                return nh
    return None


# ── 1. NOUVELLE station : demande ───────────────────────────────────────────
def request(email: str, source_node_hex: Optional[str] = None, pass_code: str = "") -> Dict[str, Any]:
    hex_pub = cloud_storage.hex_for_email(email)
    if not hex_pub:
        raise MigrationError(f"{email}: MULTIPASS à recréer d'abord sur cette station (même salt/pepper)")
    source = source_node_hex or find_source_node(email, hex_pub)
    if not source:
        raise MigrationError("station d'origine introuvable dans le swarm — la passer en argument (--from-node NODEHEX)")
    me = my_node_hex()
    if source == me:
        raise MigrationError("la station d'origine est cette station")
    st = load_state(email)
    if st.get("role") == "target" and st.get("state") in ("imported", "claiming", "claimed"):
        raise MigrationError(f"déménagement déjà en état « {st['state']} »")
    # `.roaming` = visiteur, purgé après 24 h par NODE.refresh.sh : un déménagement (gros épinglage) peut durer
    # plus longtemps. Le compte devient un compte hébergé en cours d'arrivée (gelé jusqu'à l'import).
    roaming = _user_dir(email) / ".roaming"
    was_roaming = roaming.exists()
    roaming.unlink(missing_ok=True)
    save_state(email, {"role": "target", "state": "requested", "from_node": source, "at": int(time.time()),
                       "started": int(time.time()), "was_roaming": was_roaming})
    try:
        # Le PASS (saisi avec le QR P1) voyage dans le message NIP-44 : c'est l'ANCIENNE station, qui détient le
        # .pass, qui le vérifie (compteur anti-bruteforce partagé, services/pass_guard.py).
        _send(source, "migrate_request", {"email": email, "hex": hex_pub, "proof": make_proof(email, me),
                                          "pass": str(pass_code or "")[:32]})
    except Exception:
        abort(email)           # rien n'est parti : on remet le compte comme avant, puis l'erreur remonte
        raise
    return {"status": "requested", "from_node": source}


def abort(email: str) -> Dict[str, Any]:
    """Annule une demande NON aboutie (état requested ou error) : efface l'état et, si le compte venait du
    roaming, remet `.roaming`. Refuse après l'import (le compte est alors à terminer, pas à annuler)."""
    st = load_state(email)
    if st.get("role") != "target" or st.get("state") not in ("requested", "error"):
        raise MigrationError(f"{email}: rien à annuler (état « {st.get('role')}:{st.get('state')} »)")
    (_user_dir(email) / STATE_NAME).unlink(missing_ok=True)
    if st.get("was_roaming"):
        (_user_dir(email) / ".roaming").touch()
    return {"status": "aborted", "was_roaming": bool(st.get("was_roaming"))}


def _notify_pass_lock(email: str, attempts: int) -> None:
    """Prévient le Capitaine de CETTE station : PASS verrouillé après des essais de déménagement."""
    cap = _captain_email()
    mailjet = ZEN / "Astroport.ONE" / "tools" / "mailjet.sh"
    if not cap or not mailjet.exists():
        logger.warning("migration: alerte PASS non envoyée pour %s (CAPTAINEMAIL ou mailjet.sh absent)", email)
        return
    body = (f"<h2>⚠️ Déménagement refusé — PASS verrouillé</h2><p><b>Compte :</b> {email}</p>"
            f"<p><b>Échecs :</b> {attempts}</p><p>Le code PASS a été invalidé après des essais de déménagement "
            "vers une autre station. Si c'est bien le propriétaire, le réinitialiser.</p>")
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as fh:
        fh.write(body)
    try:
        subprocess.run([str(mailjet), "--expire", "0s", cap, fh.name, f"⚠️ PASS verrouillé (déménagement) — {email}"],
                       capture_output=True, timeout=60)
    finally:
        Path(fh.name).unlink(missing_ok=True)


# ── 2. ANCIENNE station : sert la demande ───────────────────────────────────
def serve_request(message: Dict[str, Any], sender: str) -> Dict[str, Any]:
    import ucloud_backup
    email, hex_pub = str(message.get("email") or ""), str(message.get("hex") or "").lower()
    _account(email, hex_pub)
    check_proof(message.get("proof") or {}, email, hex_pub, sender)
    # Deuxième facteur : la preuve par la clé ne suffit pas (un nsec volé + un Capitaine complice donnerait le
    # wallet). Même compteur et mêmes seuils que /g1nostr : 3 échecs = PASS invalidé + Capitaine prévenu.
    verdict = pass_guard.check(
        email, str(message.get("pass") or ""), "migrate", sender[:12],
        game_path=cloud_storage.GAME_NOSTR_PATH.parent,
        on_lock=lambda invalidated, count: _notify_pass_lock(email, count))
    if verdict is not None:
        raise MigrationError(f"PASS refusé : {verdict.message}")
    st = load_state(email)
    if st.get("role") == "source" and st.get("state") == "committed":
        raise MigrationError("ce compte a déjà déménagé")
    reuse = (st.get("role") == "source" and st.get("state") == "bundled" and st.get("to_node") == sender
             and time.time() - st.get("at", 0) < BUNDLE_REUSE_MAX_AGE)
    if not reuse:
        # Gel D'ABORD : plus aucun rafraîchissement ne doit modifier le compte pendant l'export.
        (_user_dir(email) / FROZEN_NAME).write_text(f"{sender} {int(time.time())}\n")
        try:
            b = ucloud_backup.export_encrypted(email, for_migration=True)
            if b["counts"].get("errors"):
                raise MigrationError("export incomplet : " + "; ".join(b["counts"]["errors"]))
        except Exception as exc:
            # Un paquet incomplet livré = des données perdues après le nettoyage : on dégèle et on refuse.
            (_user_dir(email) / FROZEN_NAME).unlink(missing_ok=True)
            save_state(email, {"role": "source", "state": "export_error", "errors": [str(exc)], "at": int(time.time())})
            raise exc if isinstance(exc, MigrationError) else MigrationError(f"export impossible : {exc}")
        st = {"role": "source", "state": "bundled", "to_node": sender, "at": int(time.time()),
              "cid": b["cid"], "key": b["key"], "sha": b["sha"], "counts": b["counts"], "size": b["size"]}
        save_state(email, st)
    _send(sender, "migrate_bundle", {"email": email, "hex": hex_pub, "cid": st["cid"], "key": st["key"],
                                     "sha": st["sha"], "counts": st["counts"], "size": st["size"]})
    return {"status": "bundled", "cid": st["cid"], "counts": st["counts"]}


# ── 3. NOUVELLE station : importe et accuse réception ───────────────────────
def receive_bundle(message: Dict[str, Any], sender: str) -> Dict[str, Any]:
    import ucloud_backup
    email, hex_pub = str(message.get("email") or ""), str(message.get("hex") or "").lower()
    _account(email, hex_pub)
    st = load_state(email)
    if st.get("role") != "target" or st.get("state") != "requested" or st.get("from_node") != sender:
        raise MigrationError("paquet non attendu (aucune demande en cours auprès de cette station)")
    cid, key = str(message.get("cid") or ""), str(message.get("key") or "")
    if not cid or len(key) != 64:
        raise MigrationError("paquet incomplet (cid/clé)")
    try:
        result = ucloud_backup.import_encrypted(email, cid, key, str(message.get("sha") or ""),
                                                with_qdrant=True, overwrite_settings=True, for_migration=True)
    except Exception as exc:   # empreinte fausse, IPFS muet, clé fausse… : visible, aucun accusé
        save_state(email, {**st, "state": "error", "errors": [str(exc)], "at": int(time.time())})
        raise MigrationError(f"import impossible, aucun accusé envoyé : {exc}")
    if result.get("errors"):
        save_state(email, {**st, "state": "error", "errors": result["errors"], "at": int(time.time())})
        raise MigrationError("import incomplet, aucun accusé envoyé : " + "; ".join(result["errors"]))
    # Ce qui est annoncé doit être ce qui est appliqué (état du compte, événements) : sinon aucun accusé.
    announced = message.get("counts") or {}
    for sent_key, applied_key in (("state_files", "state_files"), ("nostr_events", "events"),
                                  ("zencard_files", "zencard_files")):
        if int(announced.get(sent_key) or 0) > int(result.get(applied_key) or 0):
            save_state(email, {**st, "state": "error", "errors": [f"{sent_key}: {announced.get(sent_key)} annoncés, "
                                                                  f"{result.get(applied_key)} appliqués"], "at": int(time.time())})
            raise MigrationError(f"import incomplet ({sent_key} : {announced.get(sent_key)} annoncés, "
                                 f"{result.get(applied_key)} appliqués), aucun accusé envoyé")
    try:
        _import_zencard_keys(email)
    except MigrationError as exc:
        save_state(email, {**st, "state": "error", "errors": [str(exc)], "at": int(time.time())})
        raise
    save_state(email, {**st, "state": "imported", "cid": cid, "applied": {k: v for k, v in result.items() if k != "errors"},
                       "at": int(time.time())})
    _spawn(["python3", str(Path(ucloud_backup.__file__).resolve()), "pin", email], f"migrate_pin_{email}")
    _send(sender, "migrate_ack", {"email": email, "hex": hex_pub, "cid": cid,
                                  "applied": {k: v for k, v in result.items() if k != "errors"}})
    return {"status": "imported", **{k: v for k, v in result.items() if k != "errors"}}


def _import_zencard_keys(email: str) -> None:
    """Importe dans Kubo la clé IPNS de la ZenCard restaurée, sous les DEUX noms que VISA.new.sh lui donne
    (`<email>` et `<G1PUB>`) ; l'identifiant obtenu doit être celui de `.playerns` — jamais une clé « proche »."""
    pdir = _players_dir(email)
    if not pdir.is_dir():
        return
    try:
        g1pub = (pdir / ".g1pub").read_text().strip()
        expected = (pdir / ".playerns").read_text().strip().rsplit("/", 1)[-1]
        pem = pdir / "secret.player"
        if not (g1pub and expected and pem.is_file()):
            raise OSError("fichiers de la ZenCard incomplets (.g1pub, .playerns ou secret.player)")
    except OSError as exc:
        raise MigrationError(f"ZenCard restaurée inexploitable : {exc}")
    for name in (email, g1pub):
        subprocess.run(["ipfs", "key", "rm", name], capture_output=True, text=True, timeout=60)
        p = subprocess.run(["ipfs", "key", "import", name, "-f", "pem-pkcs8-cleartext", str(pem)],
                           capture_output=True, text=True, timeout=60)
        got = (p.stdout or "").strip()
        if p.returncode != 0 or got != expected:
            raise MigrationError(f"clé IPNS de la ZenCard : import sous « {name[:12]}… » en échec ou identifiant "
                                 f"différent (attendu {expected[:12]}…, obtenu {got[:12] or '?'}…)")


# ── 4. ANCIENNE station : libère l'IPNS ─────────────────────────────────────
def _run_ipns(args: List[str]) -> Tuple[int, str, str]:
    p = subprocess.run([str(IPNS_HANDOVER), *args], capture_output=True, text=True, timeout=1800)
    return p.returncode, p.stdout, p.stderr


def serve_ack(message: Dict[str, Any], sender: str) -> Dict[str, Any]:
    email, hex_pub = str(message.get("email") or ""), str(message.get("hex") or "").lower()
    _account(email, hex_pub)
    st = load_state(email)
    if st.get("role") != "source" or st.get("state") not in ("bundled", "committed") or st.get("to_node") != sender \
            or st.get("cid") != message.get("cid"):
        raise MigrationError("accusé non attendu (aucun paquet envoyé à cette station pour ce compte)")
    if st["state"] != "committed":
        # Libérations IDEMPOTENTES : chacune est consignée dès qu'elle a eu lieu (un échec plus loin ne doit pas
        # faire oublier la séquence d'une clé déjà retirée du trousseau).
        if not st.get("released_nostr"):
            rc, out, err = _run_ipns(["release", email])
            if rc != 0 and "ERROR:NO_KEY" not in err:
                raise MigrationError(f"libération IPNS impossible, compte NON cédé : {err.strip()[:300]}")
            vals = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
            st.update(released_nostr=True, seq=vals.get("SEQ", ""), value=vals.get("VALUE", ""))
            save_state(email, st)
        if _players_dir(email).is_dir() and not st.get("released_zencard"):
            # La ZenCard a son propre IPNS (TiddlyWiki), sous DEUX noms de clé pour la MÊME clé : les retirer tous
            # les deux, sinon Kubo continuerait de republier l'ancien record et se disputerait la séquence.
            g1pub = ""
            try:
                g1pub = (_players_dir(email) / ".g1pub").read_text().strip()
            except OSError:
                pass
            rc, out, err = _run_ipns(["release", email, "--key", email])
            if rc != 0 and "ERROR:NO_KEY" not in err:
                raise MigrationError(f"libération IPNS de la ZenCard impossible, compte NON cédé : {err.strip()[:300]}")
            zvals = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
            if g1pub:
                rc2, _o, err2 = _run_ipns(["release", email, "--key", g1pub])
                if rc2 != 0 and "ERROR:NO_KEY" not in err2:
                    raise MigrationError(f"libération de la clé {g1pub[:10]}… impossible : {err2.strip()[:300]}")
            st.update(released_zencard=True, zc_seq=zvals.get("SEQ", ""), zc_value=zvals.get("VALUE", ""))
            save_state(email, st)
        st.update(state="committed", at=int(time.time()))
        save_state(email, st)
    commit = {"email": email, "hex": hex_pub, "seq": st.get("seq", ""), "value": st.get("value", "")}
    if st.get("released_zencard"):
        commit["zencard"] = {"seq": st.get("zc_seq", ""), "value": st.get("zc_value", "")}
    _send(sender, "migrate_commit", commit)
    return {"status": "committed", "seq": st.get("seq"), "value": st.get("value")}


# ── 5. NOUVELLE station : publie l'IPNS ─────────────────────────────────────
def receive_commit(message: Dict[str, Any], sender: str) -> Dict[str, Any]:
    email, hex_pub = str(message.get("email") or ""), str(message.get("hex") or "").lower()
    _account(email, hex_pub)
    st = load_state(email)
    if st.get("role") != "target" or st.get("state") not in ("imported", "claiming") or st.get("from_node") != sender:
        raise MigrationError("commit non attendu")
    seq = str(message.get("seq") or "")
    value = str(message.get("value") or "")
    if seq and not seq.isdigit() or value and not re.fullmatch(r"/ipfs/[A-Za-z0-9]+", value):
        raise MigrationError("commit mal formé")
    zc = message.get("zencard") or {}
    zc_seq, zc_value = str(zc.get("seq") or ""), str(zc.get("value") or "")
    if zc_seq and not zc_seq.isdigit() or zc_value and not re.fullmatch(r"/ipfs/[A-Za-z0-9]+", zc_value):
        raise MigrationError("commit mal formé (ZenCard)")
    save_state(email, {**st, "state": "claiming", "seq": seq, "value": value, "zc_seq": zc_seq, "zc_value": zc_value,
                       "at": int(time.time())})
    # Épinglage possiblement long : hors du daemon (jamais sous-shell : processus autonome).
    _spawn([sys.executable, str(Path(__file__).resolve().parent.parent / "multipass_migrate.py"), "--finish", email],
           f"migrate_finish_{email}")
    return {"status": "claiming", "seq": seq, "value": value}


def _run_fixups(email: str) -> Tuple[int, str, str]:
    p = subprocess.run([str(ADOPT_FIXUPS), email], capture_output=True, text=True, timeout=900)
    return p.returncode, p.stdout, p.stderr


def finish(email: str) -> Dict[str, Any]:
    """Épingle la racine de l'ancien record puis publie l'IPNS avec une séquence supérieure ; mises à jour
    publiques ciblées (adopt_fixups.sh) ; puis rapatrie TOUS les documents (pin) et prévient l'ancienne station."""
    st = load_state(email)
    if st.get("role") != "target" or st.get("state") != "claiming":
        raise MigrationError(f"{email}: rien à finaliser (état « {st.get('state')} »)")
    value = st.get("value") or ""
    seq = ""
    if not value:
        note = "aucun record IPNS d'origine : rien à publier"
    else:
        cid = value.rsplit("/", 1)[-1]
        p = subprocess.run(["ipfs", "pin", "add", "--progress=false", cid], capture_output=True, text=True, timeout=3600)
        if p.returncode != 0:
            raise MigrationError(f"épinglage de {cid} impossible : {p.stderr.strip()[:300]}")
        args = ["claim", email, cid] + (["--after", st["seq"]] if st.get("seq") else [])
        rc, out, err = _run_ipns(args)
        if rc != 0:
            raise MigrationError(f"publication IPNS impossible : {err.strip()[:300]}")
        seq = dict(line.split("=", 1) for line in out.splitlines() if "=" in line).get("SEQ", "")
        note = ""
    zc_value, zc_published = st.get("zc_value") or "", ""
    if zc_value and _players_dir(email).is_dir():
        zcid = zc_value.rsplit("/", 1)[-1]
        p = subprocess.run(["ipfs", "pin", "add", "--progress=false", zcid], capture_output=True, text=True, timeout=3600)
        if p.returncode != 0:
            raise MigrationError(f"épinglage de la ZenCard {zcid} impossible : {p.stderr.strip()[:300]}")
        zargs = ["claim", email, zcid] + (["--after", st["zc_seq"]] if st.get("zc_seq") else []) + ["--key", email]
        rc, out, err = _run_ipns(zargs)
        if rc != 0:
            raise MigrationError(f"publication IPNS de la ZenCard impossible : {err.strip()[:300]}")
        zc_published = dict(line.split("=", 1) for line in out.splitlines() if "=" in line).get("SEQ", "")
    rc, fx_out, fx_err = _run_fixups(email)
    warnings = [l for l in fx_err.splitlines() if l.startswith(("WARN:", "ERROR:"))]
    if rc != 0:
        logger.warning("migration: adopt_fixups a signalé des étapes en échec pour %s : %s", email, warnings)
    dns = _dnslink(email) if value else "sans objet"
    save_state(email, {**st, "state": "claimed", "published_seq": seq, "zc_published_seq": zc_published,
                       "dnslink": dns, "note": note,
                       "fixups_rc": rc, "fixups_warnings": warnings, "at": int(time.time())})
    result: Dict[str, Any] = {"status": "claimed", "seq": seq, "dnslink": dns, "fixups_rc": rc}
    if note:
        result["note"] = "no_record"
    result["pin"] = pin(email)
    return result


def pin(email: str) -> Dict[str, Any]:
    """Rapatrie (épingle) tous les documents IPFS du compte, puis envoie `migrate_pinned` à l'ancienne station.
    Reprenable : états claimed / pin_error. L'espace disque est contrôlé avant ; un échec n'est jamais « ok »."""
    st = load_state(email)
    if st.get("role") != "target" or st.get("state") not in ("claimed", "pin_error"):
        raise MigrationError(f"{email}: épinglage impossible dans l'état « {st.get('role')}:{st.get('state')} »")
    roots = [v for v in (st.get("value"), st.get("zc_value")) if v]
    try:
        found = account_pins.collect(email, roots)
        account_pins.check_space(found)
    except account_pins.PinError as exc:
        save_state(email, {**st, "state": "pin_error", "pin_error": str(exc), "at": int(time.time())})
        raise MigrationError(str(exc))

    def progress(p: Dict[str, Any]) -> None:
        cur = load_state(email)
        cur["pin"] = p
        save_state(email, cur)

    res = account_pins.pin_all(found, progress)
    st = load_state(email)
    st.update(state="done" if res["ok"] else "pin_error", pin={k: res[k] for k in ("total", "pinned", "already", "failed")},
              pin_failed_cids=res["failed_cids"], at=int(time.time()))
    st.pop("pin_error", None)
    save_state(email, st)
    msg = {"email": email, "hex": cloud_storage.hex_for_email(email) or "", "total": res["total"],
           "pinned": res["pinned"] + res["already"], "failed": res["failed"], "ok": res["ok"]}
    _send(st["from_node"], "migrate_pinned", msg)
    if res["ok"] and not st.get("notified_arrival"):
        st = load_state(email)
        st["notified_arrival"] = _notify_arrival(email, st, msg)          # une seule fois, même si l'épinglage est repris
        save_state(email, st)
    elif not res["ok"]:
        st = load_state(email)
        st["notified_problem"] = _notify_pin_problem(email, st, msg)
        save_state(email, st)
    return {"status": st["state"], **msg}


# ── Emails : la personne et les Capitaines des deux stations sont prévenus ────────────────────
def _mail(template: str, dest: str, subject: str, ctx: Dict[str, str]) -> str:
    """Envoie templates/NOSTR/<template> (HTML statique lisible partout) par mailjet.sh. Jamais bloquant : un
    envoi qui échoue n'annule PAS un déménagement réussi, mais le résultat est consigné (« ok » ou la raison)."""
    try:
        body = (MAIL_TEMPLATES / template).read_text(encoding="utf-8")
    except OSError as exc:
        return f"échec : template illisible ({exc})"
    for key, val in ctx.items():
        body = body.replace(key, html.escape(str(val)))
    if not dest or not MAILJET.exists():
        return "échec : destinataire ou mailjet.sh absent"
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as fh:
        fh.write(body)
    try:
        p = subprocess.run([str(MAILJET), "--template", str(MAIL_TEMPLATES / template), "--expire", "7d", dest,
                            fh.name, subject], capture_output=True, text=True, timeout=120)
        return "ok" if p.returncode == 0 else f"échec : mailjet.sh code {p.returncode}"
    except Exception as exc:
        logger.warning("migration: email %s → %s : %s", template, dest, exc)
        return f"échec : {exc}"
    finally:
        Path(fh.name).unlink(missing_ok=True)


def _station_label() -> str:
    from core.config import settings
    return str(settings.uSPOT).replace("https://", "").replace("http://", "").rstrip("/")


def _notify_arrival(email: str, st: Dict[str, Any], res: Dict[str, Any]) -> Dict[str, str]:
    """NOUVELLE station, tout épinglé : la personne ET le Capitaine d'ici sont prévenus."""
    from core.config import settings
    cap = _captain_email()
    now = time.strftime("%Y-%m-%d %H:%M")
    old = str(st.get("from_node", ""))[:12] + "…"
    ctx = {"_EMAIL_": email, "_STATION_": _station_label(), "_STATION_URL_": str(settings.uSPOT), "_OLDSTATION_": old,
           "_DATE_": now, "_DOCS_": f"{res['pinned']} / {res['total']}", "_CAPTAIN_": cap or "—"}
    out = {"player": _mail("migration_arrived_player.html", email, "🚚 Votre MULTIPASS a déménagé sur " + _station_label(), ctx)}
    ctx_c = {"_EMAIL_": email, "_OLDSTATION_": old, "_DATE_": now, "_STATUS_": "terminé",
             "_TOTAL_": res["total"], "_PINNED_": res["pinned"], "_FAILED_": res["failed"],
             "_DETAIL_": "Tous les documents sont épinglés sur cette station."}
    out["captain"] = _mail("migration_captain_arrived.html", cap, f"📥 MULTIPASS accueilli — {email}", ctx_c)
    return out


def _notify_pin_problem(email: str, st: Dict[str, Any], res: Dict[str, Any]) -> Dict[str, str]:
    """Rapatriement incomplet : seul le Capitaine d'ICI est prévenu (la personne le sera à la reprise réussie)."""
    cap = _captain_email()
    ctx = {"_EMAIL_": email, "_OLDSTATION_": str(st.get("from_node", ""))[:12] + "…", "_DATE_": time.strftime("%Y-%m-%d %H:%M"),
           "_STATUS_": "INCOMPLET — l'ancienne station garde le compte", "_TOTAL_": res["total"], "_PINNED_": res["pinned"],
           "_FAILED_": res["failed"], "_DETAIL_": "Relancer : multipass_migrate.py --pin " + email}
    return {"captain": _mail("migration_captain_arrived.html", cap, f"⚠️ Déménagement incomplet — {email}", ctx)}


def _notify_departure(email: str, st: Dict[str, Any], dest: Path) -> Dict[str, str]:
    """ANCIENNE station, compte déplacé : son Capitaine est prévenu (où sont les dossiers, quand ils seront purgés)."""
    ctx = {"_EMAIL_": email, "_TONODE_": str(st.get("to_node", ""))[:12] + "…", "_DATE_": time.strftime("%Y-%m-%d %H:%M"),
           "_MOVEDTO_": str(dest)}
    return {"captain": _mail("migration_captain_departed.html", _captain_email(), f"📤 MULTIPASS déménagé — {email}", ctx)}


# ── 6. ANCIENNE station : nettoyage, SEULEMENT après la confirmation d'épinglage ─────────────
def _captain_email() -> str:
    """CAPTAINEMAIL de CETTE station (source de vérité : tools/my.sh)."""
    p = subprocess.run(["bash", "-c", f'. "{MY_SH}" >/dev/null 2>&1; printf %s "$CAPTAINEMAIL"'],
                       capture_output=True, text=True, timeout=120)
    return p.stdout.strip()


def _captain_unfollow(email: str, hex_pub: str) -> str:
    cap = _captain_email()
    if not cap or cap == email:
        return "sans objet"
    try:
        raw = (_user_dir(cap) / ".secret.nostr").read_text()
    except OSError:
        return "clé du Capitaine illisible"
    m = re.search(r"NSEC=([^;\s]+)", raw)
    if not m:
        return "NSEC du Capitaine introuvable"
    env = dict(os.environ, NOSTR_NSEC=m.group(1))
    p = subprocess.run([str(ZEN / "Astroport.ONE" / "tools" / "nostr_unfollow.sh"), "-", hex_pub, "ws://127.0.0.1:7777"],
                       capture_output=True, text=True, timeout=120, env=env)
    return "ok" if p.returncode == 0 else f"échec : {p.stderr.strip()[:120]}"


def serve_pinned(message: Dict[str, Any], sender: str) -> Dict[str, Any]:
    email, hex_pub = str(message.get("email") or ""), str(message.get("hex") or "").lower()
    _account(email, hex_pub)
    st = load_state(email)
    if st.get("role") != "source" or st.get("state") != "committed" or st.get("to_node") != sender:
        raise MigrationError("confirmation d'épinglage non attendue (aucun déménagement cédé à cette station)")
    pinned = {"total": int(message.get("total") or 0), "pinned": int(message.get("pinned") or 0),
              "failed": int(message.get("failed") or 0), "ok": bool(message.get("ok"))}
    st["pinned"] = pinned
    st["at"] = int(time.time())
    save_state(email, st)
    if not pinned["ok"] or pinned["failed"] != 0:
        return {"status": "pin_incomplete", **pinned}        # RIEN n'est nettoyé : les blocs restent ici
    return cleanup_source(email)


def cleanup_source(email: str) -> Dict[str, Any]:
    """Après confirmation `failed == 0` : le Capitaine ne suit plus le compte et les dossiers du compte (MULTIPASS
    et ZenCard) sont DÉPLACÉS vers ~/.zen/tmp/migrated/<email>.<date>/{nostr,players}. Ce dossier est purgé par
    20h12.process.sh à la prochaine échéance (décision du 2026-10-20) : jusque-là, tout est récupérable."""
    st = load_state(email)
    if st.get("role") != "source" or st.get("state") != "committed" or not (st.get("pinned") or {}).get("ok"):
        raise MigrationError(f"{email}: nettoyage refusé tant que l'épinglage n'est pas confirmé sans échec")
    hex_pub = cloud_storage.hex_for_email(email) or ""
    unfollow = _captain_unfollow(email, hex_pub)
    zen = cloud_storage.GAME_NOSTR_PATH.parent.parent
    dest = zen / "tmp" / "migrated" / f"{email}.{time.strftime('%Y%m%d%H%M%S')}"
    dest.mkdir(parents=True, exist_ok=True)
    for d in (dest.parent, dest):
        os.chmod(d, 0o700)
    st["cleanup"] = "done"
    st["moved_to"] = str(dest)
    save_state(email, st)
    moved = {}
    shutil.move(str(_user_dir(email)), str(dest / "nostr"))
    moved["nostr"] = str(dest / "nostr")
    if _players_dir(email).exists():
        shutil.move(str(_players_dir(email)), str(dest / "players"))
        moved["players"] = str(dest / "players")
    logdir = ZEN / "log"
    logdir.mkdir(parents=True, exist_ok=True)
    with open(logdir / "migrations.log", "a") as fh:
        fh.write(f"{time.strftime('%F %T')} {email} déménagé vers {st.get('to_node', '?')[:12]} — "
                 f"déplacé : {dest} (purgé à la prochaine 20h12)\n")
    notified = _notify_departure(email, st, dest)
    try:                                           # l'état a suivi le dossier déplacé : on y consigne le résultat
        moved_state = dest / "nostr" / STATE_NAME
        d = json.loads(moved_state.read_text())
        d["notified_departure"] = notified
        moved_state.write_text(json.dumps(d))
    except (OSError, ValueError):
        pass
    return {"status": "cleaned", "moved_to": str(dest), "moved": moved, "captain_unfollow": unfollow,
            "notified": notified}


def _dnslink(email: str) -> str:
    """DNSLink du sous-domaine du MULTIPASS (même schéma que make_NOSTRCARD.sh). Best-effort,
    jamais silencieux : renvoie « ok » ou la raison."""
    ovh = ZEN / "Astroport.ONE" / "admin" / "system" / "ovh.me.sh"
    clyuser = ZEN / "Astroport.ONE" / "tools" / "clyuseryomail.sh"
    try:
        ns = (_user_dir(email) / "NOSTRNS").read_text().strip()
        if not (ovh.exists() and clyuser.exists()):
            return "ovh.me.sh absent"
        youser = subprocess.run([str(clyuser), email], capture_output=True, text=True, timeout=30).stdout.strip()
        r = subprocess.run([str(ovh), "upsert", youser, ns], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            logger.warning("migration: DNSLink %s : %s", youser, r.stderr.strip()[:200])
            return f"échec : {r.stderr.strip()[:100]}"
        return "ok"
    except OSError as exc:
        return f"illisible : {exc}"


def _spawn(cmd: List[str], logname: str) -> None:
    """Processus AUTONOME (setsid) : ne bloque ni le daemon DM ni son watchdog pgrep."""
    logdir = ZEN / "log"
    logdir.mkdir(parents=True, exist_ok=True)
    with open(logdir / f"{logname}.log", "ab") as fh:
        subprocess.Popen(cmd, stdout=fh, stderr=fh, stdin=subprocess.DEVNULL, start_new_session=True)
