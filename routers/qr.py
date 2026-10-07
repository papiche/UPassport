"""
QR code router — génération amzqr complète + interface web.

GET  /qr?html=1                          → Interface de configuration
GET  /qr?data=URL[&version=1][&level=H][&colorized=0]
         [&contrast=1.0][&brightness=1.0][&color=000000][&bgcolor=ffffff]
         [&picture_url=URL][&format=json|png]
POST /qr  (multipart : data, version, level, colorized, contrast,
           brightness, color, bgcolor, format, picture, picture_url)

Options amzqr :
  version     int  1-40       version QR (auto-incrémentée si overflow)
  level       str  L|M|Q|H   correction d'erreur (L=7% M=15% Q=25% H=30%)
  colorized   int  0|1        coloriser depuis l'image de fond
  contrast    float 0.1-3.0  contraste image de fond
  brightness  float 0.1-3.0  luminosité image de fond
  picture     file            image de fond (POST multipart)
  picture_url str             URL d'une image à télécharger (GET ou POST)
  color       str  RRGGBB    couleur modules (qrencode fallback)
  bgcolor     str  RRGGBB    couleur fond   (qrencode fallback)

GET  /qr/postcard?html=1                 → Générateur de carte postale (config + preview)
GET  /qr/postcard?data=URL[&title=][&image_url=][&back_title=][&message=][&footer=]
         [&level=H][&version=1]
POST /qr/postcard  (multipart, mêmes champs)
    → Page imprimable 10x15cm (recto QR+image, verso message) — réutilisable
      pour n'importe quel projet, pas seulement UPlanet.
  data        str  requis     URL/texte encodé dans le QR (recto)
  title       str             Titre recto
  image_url   str             Illustration recto (optionnelle)
  back_title  str             Titre verso
  message     str             Corps du message verso (les doubles retours à la
                               ligne \\n\\n séparent les paragraphes)
  footer      str             Signature / ligne de pied verso

GET  /qr/billet?amount=5[&unit=zen|g1][&fond_url=][&logo_url=][&verso=1][&verso_text=]
POST /qr/billet  (multipart, mêmes champs)
    Auth NIP-98 OBLIGATOIRE (Authorization: Nostr <event>, kind 27235) — seul
    un MULTIPASS connecté peut créer un Ğ1Billet (cf. UPlanet/earth/billet.html).
    → Planche A4 paysage de 6 Ğ1Billets papier imprimables (2 colonnes ×
      3 lignes) — remplace l'ancien moteur graphique G1BILLET (dépôt
      externe fermé). Chaque billet a sa propre clé G1/duniter JETABLE
      (cf. `billet_gen.sh`), jamais persistée sur disque : le secret n'existe
      que dans la réponse HTTP, à imprimer puis oublier. Son seed est scindé
      en 3 parts Shamir (2-sur-3, GF(256), même procédé que TrocZen) : P1
      (QR visible), P2 (bande repliable/scellée), P3 (jamais imprimé —
      témoin chiffré publié sur NOSTR, réserve de recours/support). Aucune
      part seule ne permet de dépenser — la reconstruction (2 parts
      minimum) se fait hors-ligne, côté client, via
      `UPlanet/earth/billet_redeem.html`. RECTO SEUL : P2 est imprimé en
      bande verticale sur le bord gauche de chaque billet — à replier vers
      l'avant et scotcher avant distribution (P2 se retrouve ainsi caché
      derrière P1). Portefeuilles TOUJOURS
      vierges à la génération (pas de débit automatique — mode `auto`
      retiré le 2026-09-24, peu fiable en pratique) : financement manuel
      après coup, par qui veut, vers les G1PUB imprimés.
  amount    float  requis   Montant annoncé par billet, dans l'unité `unit`
                              (× 6 au total, affichage seul). 0 → billets
                              "vierges" : la zone montant reste blanche à
                              l'impression, inscrite à la main
  unit      str    zen|g1 (défaut zen) — unité de `amount`. 1 Ẑen = 0.1 Ğ1
                              (convention UPlanet ORIGIN)
  fond_url  str    optionnel  Image de fond des billets (sinon fond neutre).
                              Par défaut coté client : bannière du profil
                              NOSTR connecté (kind 0 `banner`)
  logo_url  str    optionnel  Petit logo rond en coin. Par défaut coté
                              client : avatar du profil NOSTR connecté
                              (kind 0 `picture`)
"""

import base64
import html as html_lib
import os
import re
import asyncio
import json
import logging
import tempfile
import shutil
import urllib.parse
import uuid
from pathlib import Path
from typing import Optional

import httpx
import websockets

from fastapi import APIRouter, Request, BackgroundTasks, Query, HTTPException, Depends
from fastapi.responses import Response, JSONResponse, HTMLResponse, RedirectResponse

from core.config import settings
from services.nostr import verify_nip98_auth, get_nostr_relay_url
from services.g1_squid import get_g1_balance_native, get_g1_primal_source, g1pub_to_ss58, get_network_status
from utils.security import is_safe_g1pub, find_user_directory_by_hex, is_multipass_user
from utils.helpers import get_env_from_mysh

logger = logging.getLogger(__name__)
router = APIRouter()

_TEMPLATE = Path(__file__).parent.parent / "templates" / "qr.html"
_TEMPLATE_POSTCARD = Path(__file__).parent.parent / "templates" / "qr_postcard.html"
_BILLET_SCRIPT = Path(__file__).parent.parent / "billet_gen.sh"
# Logo embarqué (amzqr -p) dans le seul QR "Vérifier" (lien /scan générique,
# identique sur tous les billets) — PAS dans P1/P2 (hex brut, Shamir : un
# logo dégraderait la fiabilité de lecture d'une donnée qui doit décoder
# EXACTEMENT, cf. billet_astroid.py qui utilise le même visuel).
_BILLET_VERIFY_LOGO = Path(__file__).parent.parent / "static" / "billet_styles" / "astrologo_nb.png"


def _qr_html() -> str:
    try:
        return _TEMPLATE.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.error("Template manquant : %s", _TEMPLATE)
        return "<h1>Template manquant : templates/qr.html</h1>"


def _postcard_config_html() -> str:
    try:
        return _TEMPLATE_POSTCARD.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.error("Template manquant : %s", _TEMPLATE_POSTCARD)
        return "<h1>Template manquant : templates/qr_postcard.html</h1>"


_POSTCARD_PAGE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__FRONT_TITLE__ — Carte Postale</title>
<style>
  :root{--ink:#222;--gold:#c8a83c;--green:#2d5a1b;--paper:#f5f0e8;--brown:#8b4513}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#ccc;font-family:Georgia,serif;color:var(--ink);
       display:flex;flex-direction:column;align-items:center;gap:22px;padding:22px 0}
  .bar{display:flex;gap:10px}
  .bar button{padding:9px 18px;border:none;border-radius:6px;background:var(--gold);
              color:#1a1209;font-weight:700;cursor:pointer;font-size:.9rem}
  .bar button:hover{box-shadow:0 0 10px var(--gold)}
  .card{width:150mm;height:100mm;background:var(--paper);
        box-shadow:0 0 16px rgba(0,0,0,.35);position:relative;overflow:hidden}

  /* ---- RECTO ---- */
  #recto{display:flex;flex-direction:column;align-items:center;justify-content:center;
         text-align:center;padding:8mm;gap:3mm}
  #recto .front-title{font-size:15pt;color:var(--green);text-transform:uppercase;
                       letter-spacing:1.5px}
  #recto .front-img{max-width:70mm;max-height:45mm;object-fit:contain}
  #recto .front-placeholder{font-size:40pt;line-height:1}
  #recto .qr-row{display:flex;align-items:center;gap:6mm;margin-top:2mm}
  #recto .qr-row img{width:24mm;height:24mm;image-rendering:pixelated}
  #recto .qr-cap{font-family:monospace;font-size:8pt;color:#555;max-width:60mm;
                 word-break:break-all;text-align:left}

  /* ---- VERSO ---- */
  #verso{display:flex}
  #verso .msg{flex:1 1 60%;padding:9mm;display:flex;flex-direction:column;gap:2.5mm;
              overflow:hidden}
  #verso .msg h2{font-size:11pt;color:var(--brown);letter-spacing:.5px;
                 border-bottom:1px solid var(--gold);padding-bottom:2mm;margin-bottom:1mm}
  #verso .msg p{font-size:8.5pt;line-height:1.4}
  #verso .msg .footer{margin-top:auto;font-size:8pt;color:var(--green);font-weight:700}
  #verso .side{flex:0 0 40%;border-left:1px dashed var(--brown);padding:9mm 7mm;
               display:flex;flex-direction:column;align-items:center;gap:4mm}
  #verso .stamp{width:22mm;height:22mm;border:1px dashed #999;color:#999;
                font-family:monospace;font-size:6.5pt;display:flex;align-items:center;
                justify-content:center;text-align:center;align-self:flex-end}
  #verso .side img{width:26mm;height:26mm;image-rendering:pixelated}
  #verso .side .cap{font-family:monospace;font-size:7pt;color:#555;text-align:center;
                    word-break:break-all}

  @media print{
    .no-print{display:none!important}
    html,body{background:#fff!important;width:100%!important;height:100%!important;
              margin:0!important;padding:0!important}
    .card{box-shadow:none!important}
    body.p-recto #verso{display:none!important}
    body.p-verso #recto{display:none!important}
  }
</style>
</head>
<body>

<div class="bar no-print">
  <button onclick="printSide('recto')">🖨️ Imprimer le Recto</button>
  <button onclick="printSide('verso')">🖨️ Imprimer le Verso</button>
</div>

<main class="card" id="recto">
  <div class="front-title">__FRONT_TITLE__</div>
  __IMAGE_BLOCK__
  <div class="qr-row">
    <img src="__QR_DATA_URL__" alt="QR">
    <div class="qr-cap">__QR_TARGET__</div>
  </div>
</main>

<div class="card" id="verso">
  <div class="msg">
    __BACK_TITLE_BLOCK__
    __MESSAGE_PARAGRAPHS__
    __FOOTER_BLOCK__
  </div>
  <div class="side">
    <div class="stamp">TIMBRE</div>
    <img src="__QR_DATA_URL__" alt="QR">
    <div class="cap">__QR_TARGET__</div>
  </div>
</div>

<script>
  var pageStyle = document.createElement('style');
  document.head.appendChild(pageStyle);
  function printSide(side){
    pageStyle.innerHTML = '@page { size: 150mm 100mm; margin: 0; }';
    document.body.classList.remove('p-recto','p-verso');
    document.body.classList.add(side === 'recto' ? 'p-recto' : 'p-verso');
    setTimeout(function(){ window.print(); }, 100);
  }
</script>
</body>
</html>
"""


def _sanitize_image_url(url: Optional[str]) -> Optional[str]:
    """N'autorise que http(s)/data:image — jamais javascript:/vbscript: etc.
    dans un attribut src/background-image construit par concaténation."""
    if not url:
        return None
    u = url.strip()
    low = u.lower()
    if low.startswith("http://") or low.startswith("https://") or low.startswith("data:image/"):
        return u
    return None


def _render_postcard_html(
    data: str,
    qr_data_url: str,
    front_title: str,
    front_image_url: Optional[str],
    back_title: str,
    message: str,
    footer: str,
) -> str:
    """Compose la page HTML imprimable (recto QR+image / verso message)."""
    esc = html_lib.escape

    image_block = (
        f'<img class="front-img" src="{esc(front_image_url)}" alt="">'
        if front_image_url else
        '<div class="front-placeholder">🔲</div>'
    )
    back_title_block = f"<h2>{esc(back_title)}</h2>" if back_title else ""
    paragraphs = "".join(
        f"<p>{esc(p.strip())}</p>" for p in message.split("\n\n") if p.strip()
    )
    footer_block = f'<div class="footer">{esc(footer)}</div>' if footer else ""

    return (
        _POSTCARD_PAGE
        .replace("__FRONT_TITLE__", esc(front_title))
        .replace("__IMAGE_BLOCK__", image_block)
        .replace("__QR_DATA_URL__", qr_data_url)
        .replace("__QR_TARGET__", esc(data))
        .replace("__BACK_TITLE_BLOCK__", back_title_block)
        .replace("__MESSAGE_PARAGRAPHS__", paragraphs)
        .replace("__FOOTER_BLOCK__", footer_block)
    )


_BILLET_A4_PAGE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ğ1Billet — planche A4 (__COUNT__ billets)</title>
<style>
  :root{--ink:#222;--gold:#c8a83c;--green:#2d5a1b;--paper:#f5f0e8;--red:#8b1a1a}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#ccc;font-family:Georgia,serif;color:var(--ink);
       display:flex;flex-direction:column;align-items:center;gap:16px;padding:16px 0}
  .bar{display:flex;gap:10px}
  .bar button{padding:9px 18px;border:none;border-radius:6px;background:var(--gold);
              color:#1a1209;font-weight:700;cursor:pointer;font-size:.9rem}
  .bar button:hover{box-shadow:0 0 10px var(--gold)}
  .warn{max-width:297mm;background:#fff3f3;border:1px solid var(--red);color:var(--red);
        padding:8px 12px;font-family:sans-serif;font-size:.8rem;border-radius:6px}

  /* 297×210mm = A4 paysage EXACT (cf. @page ci-dessous, margin:0) : la
     feuille remplit toute la page physique, sans marge ni gap entre
     cellules — chaque frontière interne (entre 2 colonnes, entre 2 lignes)
     ne porte alors plus qu'UNE seule ligne de coupe (les pointillés
     .cell/.vcell adjacents se superposent exactement) au lieu d'une bande
     blanche à découper des deux côtés. Pour 6 billets en 2×3, c'est déjà la
     disposition qui minimise le nombre de coupes (1 verticale + 2
     horizontales = 3 coupes au total sur la pile recto+verso empilée,
     contre 5 pour un 1×6/6×1) — gap:0/padding:0 ne fait que supprimer les
     bandes blanches superflues entre ces 3 coupes, il ne change pas leur
     nombre. */
  .sheet{width:297mm;height:210mm;display:grid;grid-template-columns:repeat(2,148.5mm);
         grid-auto-rows:70mm;gap:0;background:#fff;padding:0;
         box-shadow:0 0 16px rgba(0,0,0,.35)}
  .cell{display:flex;height:70mm;border:1px dashed #999;overflow:hidden;
        position:relative;background:#fff}

  /* Bande secrète : bloc horizontal qui wrap normalement (largeur = hauteur
     de la cellule) puis pivoté à -90deg — plus fiable qu'un writing-mode
     vertical (dont le dimensionnement intrinsèque échappe à la grille/flex
     et déborde de la cellule). position:absolute retire le texte du flux :
     sa longueur ne peut plus influencer la taille de .fold ni de la ligne. */
  .cell .fold{width:24mm;flex:0 0 24mm;height:100%;position:relative;overflow:hidden;
              border-right:1px dashed var(--red);
              background:repeating-linear-gradient(45deg,#fffaf0,#fffaf0 2mm,#f2dcdc 2mm,#f2dcdc 4mm)}
  /* 66mm = largeur AVANT rotation = hauteur visible une fois pivoté (calée
     sur les 70mm de la cellule, marge de 4mm comme avant — seule la
     cellule a grandi avec le passage à une planche A4 plein format, pas
     cette marge de sécurité). La contrainte réelle est inverse : la
     HAUTEUR avant rotation devient la LARGEUR visible une fois pivoté —
     qui doit tenir dans les 24mm du .fold, sans quoi le contenu se retrouve
     rogné par overflow:hidden (constaté à l'impression). D'où l'hexa forcé
     sur EXACTEMENT 2 lignes (cf. _render_billet_a4_html) plutôt que laissé
     au retour à la ligne naturel. Fold élargi (15mm→21mm→24mm au fil des
     révisions) pour un QR P2 plus gros et lisible, au prix d'un peu de
     largeur reprise sur .body (qui a de la marge depuis le passage à la
     disposition horizontale, puis à la planche A4 plein format).

     Avec rotate(-90deg), empiler verticalement avant rotation (QR puis
     texte) les sépare, une fois pivoté, HORIZONTALEMENT (QR vers le bord
     externe, texte vers le corps du billet) — PAS verticalement : une
     rotation -90° envoie l'axe Y (vertical, empilement) vers l'axe X final
     (horizontal), et l'axe X (largeur) vers l'axe Y final (vertical).
     Pour que le QR apparaisse bien AU-DESSUS du texte sur la page imprimée
     (demande explicite), il faut donc les juxtaposer HORIZONTALEMENT avant
     rotation (flex row), QR en dernier (axe X original croissant → axe Y
     final décroissant = vers le haut une fois pivoté) — vérifié par rendu
     réel, pas seulement par le calcul. */
  .cell .fold .secret{position:absolute;top:50%;left:50%;width:66mm;
              transform:translate(-50%,-50%) rotate(-90deg);transform-origin:center center;
              display:flex;flex-direction:row;align-items:center;justify-content:center;gap:1.5mm;
              font-family:monospace;font-size:5pt;line-height:1.25;letter-spacing:0;
              color:#333;background:#fffaf0;padding:1mm 2mm;border-radius:1mm}
  .cell .fold .secret .p2-text{text-align:center}
  .cell .fold .secret b{display:block;color:var(--red);font-weight:700;
              font-family:sans-serif;font-size:5.5pt;margin-bottom:.4mm}
  .cell .fold .secret img{width:20mm;height:20mm;image-rendering:pixelated;
              display:block;flex-shrink:0;background:#fff;padding:.5mm;
              border-radius:.6mm;border:1px solid var(--gold)}

  .cell .body{flex:1;min-width:0;position:relative;padding:3mm;display:flex;
              flex-direction:column;gap:1.5mm;background-color:var(--tint,var(--paper));
              background-position:center;background-size:cover;background-repeat:no-repeat}
  .cell .body.has-fond::before{content:'';position:absolute;inset:0;
              background:var(--tint-overlay,rgba(245,240,232,.74))}
  .cell .body>*{position:relative}

  /* Coin haut-droit : logo du profil (si connecté) PUIS, juste dessous, le
     QR de vérification du solde (anciennement dans .main-row — regroupé
     ici avec le logo, qui sert de même repère "identité de ce billet"). */
  .cell .corner-stack{position:absolute;top:2mm;right:2mm;display:flex;
              flex-direction:column;align-items:center;gap:1mm;width:23mm}
  .cell .corner-stack .logo-mark{width:23mm;height:23mm;border-radius:50%;
              object-fit:cover;border:1px solid var(--gold);
              box-shadow:0 1px 3px rgba(0,0,0,.35)}
  /* QR de vérification agrandi (23mm, contre 20mm pour P1/P2) — c'est le QR
     destiné à n'importe quel inconnu scannant le billet au hasard (cf.
     /scan?g1pub=...) : priorité à la lisibilité/robustesse du scan plutôt
     qu'à la compacité. Agrandi de 20→23mm avec le passage à la planche A4
     plein format (cellule 136×60mm → 148.5×70mm) : .corner-stack n'est
     contraint ni par la largeur fixe du pli (24mm, cf. .fold) ni par
     l'appariement visuel P1/P2 (cf. commentaire .p1-corner) — ce coin a
     toute la marge de manœuvre libérée par l'agrandissement de la cellule.
     Même largeur que logo-mark (23mm) pour que les deux éléments du coin
     soient bien alignés (bords à bords, colonne nette). */
  .cell .corner-stack .qr-item img{width:23mm;height:23mm}

  /* P1 en position ABSOLUE (et non plus dans le flux de .main-row) : laisse
     .main-row entièrement libre pour centrer le montant, SANS perdre le
     positionnement voulu pour P1 — juste à côté du pli (.fold, bord gauche,
     replié vers l'AVANT le long de son bord droit). ⚠️ CONFIRMÉ PAR TEST
     PHYSIQUE : une fois le pli (24mm) rabattu à 180° sur cet axe vertical,
     il recouvre EXACTEMENT la bande de .body large de 24mm adjacente à la
     charnière — donc P1 (positionné à seulement 2mm dans .body) se
     retrouve, comme P2, caché sous le pli replié. C'est voulu, pas un bug :
     un billet replié/scotché en circulation ne montre NI P1 NI P2 à qui le
     regarde (sécurité), et déplier révèle les deux parts D'UN COUP, bien
     alignées, pour un encaissement rapide — P1 n'est un "repère visuel"
     qu'AVANT pliage, pour qui assemble le billet (savoir où aligner le
     pli/la bande adhésive), jamais après. Précision au mm près non requise
     — le pliage reste manuel — seule la PROXIMITÉ immédiate avec le pli
     compte. Même taille que P2 (20mm, cf. .fold .secret img) pour renforcer
     visuellement que les deux QR forment la même paire. */
  .cell .p1-corner{position:absolute;top:3.5mm;left:2mm}

  /* Montant SEUL, centré dans tout l'espace restant du corps (entre le
     pli/P1 à gauche, le logo/QR solde à droite, et le bloc g1pub en bas) —
     flex:1 sur .main-row absorbe l'espace vertical libéré par la
     suppression du bandeau "Ğ1BILLET" et du QR solde (déplacé dans le
     coin). "Encaisser" est toujours au verso (cf.
     _render_billet_verso_html). */
  .cell .main-row{flex:1;display:flex;align-items:center;justify-content:center}
  .cell .amount-badge{display:inline-flex;flex-direction:column;align-items:center;
              line-height:1;background:#fff;color:var(--red);font-weight:800;
              padding:2mm 5mm;border-radius:3mm;border:1.5px solid var(--gold);
              box-shadow:0 1px 4px rgba(0,0,0,.35);flex-shrink:0}
  .cell .amount-badge .num{font-size:28pt;min-width:16mm;min-height:1em;
              display:inline-block;text-align:center}
  .cell .amount-badge .num.blank{min-width:22mm;border-bottom:2px dashed var(--gold)}
  .cell .amount-badge .unit{font-size:8pt;font-weight:700;color:var(--ink);
              letter-spacing:1px;text-transform:uppercase;margin-top:.5mm}
  .cell .qr-item{display:flex;flex-direction:column;align-items:center;gap:.5mm;flex-shrink:0}
  .cell .qr-item img{width:20mm;height:20mm;image-rendering:pixelated;
              background:#fff;padding:1mm;border-radius:1mm;border:1px solid var(--gold)}
  /* Fond photo possible derrière (profil NOSTR connecté) : les légendes et le
     bloc g1pub/statut ont besoin de leur PROPRE contraste, pas seulement du
     voile --tint-overlay — une image chargée (ex. photo de profil) peut
     rendre un simple texte gris illisible dessus. */
  .cell .qr-item span{font-family:sans-serif;font-size:4.3pt;color:#444;
              text-transform:uppercase;letter-spacing:.3px;
              background:rgba(255,255,255,.88);padding:.3mm 1.2mm;border-radius:.8mm}

  .cell .info-block{background:rgba(255,255,255,.82);border-radius:1.2mm;
              padding:1mm 1.5mm;margin-top:auto;display:flex;flex-direction:column;
              gap:.3mm;align-items:flex-end;text-align:right}
  .cell .g1pub{font-family:monospace;font-size:5pt;color:#444;word-break:break-all}
  .cell .expiry{font-family:sans-serif;font-size:4.6pt;color:#666}
  .cell .status{font-family:sans-serif;font-size:5.5pt;color:var(--green);font-weight:700}
  .cell .status.err{color:var(--red)}

  /* Verso optionnel — RÉPÉTÉ dans une grille identique à la planche recto
     (même largeur de colonne/hauteur de ligne/espacement) : une fois la
     feuille imprimée en duplex et découpée, le même texte se retrouve au
     dos de CHAQUE billet, quel que soit le sens du retournement duplex
     (contenu identique dans les 6 cellules → quelle paire recto/verso
     exacte n'importe pas). Texte horizontal (la rotation -90° testée
     était moins lisible), pas de fond coloré derrière le texte.

     MAIS la correspondance LOCALE (à l'intérieur d'un même billet découpé)
     entre recto et verso, elle, est parfaitement fixe : le verso est
     imprimé au dos de CE papier, donc directement derrière le recto, à la
     même position locale. Et une fois le pli (bord gauche, 24mm) rabattu
     vers l'avant et scotché, P2 vient se plaquer, en MIROIR par rapport
     à la charnière (x=24mm), sur le verso entre 24 et 48mm — recouvrant
     tout ce qui y est imprimé là (cf. .p1-corner plus haut : c'est
     d'ailleurs tout l'intérêt, puisque ça place P2 pile derrière P1 —
     confirmé par test physique : les deux finissent cachés ensemble).

     Zone 0-24mm (.vflap) : c'est le dos du pli lui-même — AUTANT L'UTILISER
     (texte + QR, même trick de rotation -90° que .fold .secret au recto)
     plutôt que la laisser vide, puisqu'elle n'est jamais recouverte par
     quoi que ce soit (c'est elle qui bouge, pas l'inverse). QR "Encaisser"
     déplacé ici depuis .vsupport : c'est le repère naturel pour qui
     manipule déjà cette languette.
     Zone 24-48mm (.vgap) : VIDE, volontairement — c'est l'endroit précis
     où P2 vient se plaquer une fois le pli refermé ; tout texte/QR laissé
     là serait recouvert.
     Zone 48mm+ (.vmain) : contenu principal, à l'abri du pli — texte
     contrat + QR OpenCollective/Zelkova, qui restent ENTIÈREMENT visibles
     quel que soit l'état du pli. */
  .vcell{display:flex;align-items:stretch;height:70mm;border:1px dashed #999;
              overflow:hidden;position:relative;background:#fff}
  .vcell .vflap{width:24mm;flex:0 0 24mm;position:relative;overflow:hidden;
              border-right:1px dashed var(--red);
              background:repeating-linear-gradient(45deg,#fffaf0,#fffaf0 2mm,#f2dcdc 2mm,#f2dcdc 4mm)}
  .vcell .vflap-inner{position:absolute;top:50%;left:50%;width:66mm;
              transform:translate(-50%,-50%) rotate(-90deg);transform-origin:center center;
              display:flex;flex-direction:row;align-items:center;justify-content:center;gap:1.5mm;
              font-family:sans-serif;font-size:5pt;line-height:1.25;color:#333;
              background:#fffaf0;padding:1mm 2mm;border-radius:1mm}
  .vcell .vflap-inner b{display:block;color:var(--red);font-weight:700;font-size:5.5pt;margin-bottom:.4mm}
  .vcell .vflap-inner img{width:20mm;height:20mm;image-rendering:pixelated;
              background:#fff;padding:.5mm;border-radius:.6mm;border:1px solid var(--gold);flex-shrink:0}
  .vcell .vgap{width:24mm;flex:0 0 24mm}
  .vcell .vmain{flex:1;min-width:0;display:flex;flex-direction:column;align-items:center;
              justify-content:center;padding:3mm 8mm;text-align:center;gap:1.5mm}
  .vcell .vstamp{font-family:Georgia,serif;font-weight:800;font-size:8pt;
              color:var(--green);letter-spacing:1.5px;text-transform:uppercase}
  .vcell .vtext{font-family:Georgia,serif;font-size:6pt;line-height:1.4;
              color:#444;max-width:86mm}
  .vcell .vsupport{display:flex;align-items:flex-start;justify-content:center;gap:4mm;margin-top:.5mm;width:100%}
  .vcell .vsupport-item{display:flex;flex-direction:column;align-items:center;gap:.6mm}
  .vcell .vsupport-item img{width:20mm;height:20mm;image-rendering:pixelated;background:#fff;
              padding:.6mm;border-radius:.8mm;border:1px solid var(--gold)}
  .vcell .vsupport-item span{font-family:monospace;font-size:4.3pt;color:#777;
              max-width:26mm;line-height:1.25;display:inline-block}

  @media print{
    .no-print{display:none!important}
    html,body{background:#fff!important;margin:0!important;padding:0!important}
    .sheet,.verso-sheet{box-shadow:none!important}
    .verso-sheet{page-break-before:always}
    /* Impression d'un seul côté à la fois (duplex manuel : imprimer le recto,
       retourner la pile de papier, réimprimer le verso par-dessus) — même
       principe que printSide() sur /qr/postcard. */
    body.p-recto .verso-sheet{display:none!important}
    body.p-verso .sheet:not(.verso-sheet){display:none!important}
    /* margin:0 — la feuille (.sheet) occupe toute la page physique : chaque
       billet découpé au pointillé a son propre bord de coupe (cf. .cell{border})
       jusqu'au bord du papier, sans marge blanche supplémentaire à gérer en
       plus des pointillés pour aligner les 6 découpes. */
    @page{size:A4 landscape;margin:0}
  }
</style>
</head>
<body>
<script>window.__G1BILLETS__ = __G1BILLETS_JSON__;</script>

<div class="warn no-print">
  🔒 Pour chaque billet, son secret est scindé en 3 parts (Shamir, 2 sur 3) : P1 (QR
  visible, à côté du montant) et P2 (<b>bande verticale sur le bord gauche</b>). Repliez cette bande
  vers l'avant le long du pointillé et scotchez-la avant de distribuer les billets : P2 se
  retrouve ainsi caché derrière P1. Aucune des deux parts ne suffit seule à dépenser le billet — la
  reconstruction se fait sur <code>SCAN</code>. Cette page ne se régénère pas à
  l'identique : imprimez avant de fermer l'onglet.
</div>

<div class="bar no-print">
  <button onclick="printSide('recto')">🖨️ Imprimer le Recto (__COUNT__ billets)</button>
  __VERSO_BUTTON__
</div>

<div class="sheet">
__CELLS__
</div>

__VERSO__

<script>
  function printSide(side){
    document.body.classList.remove('p-recto','p-verso');
    document.body.classList.add(side === 'recto' ? 'p-recto' : 'p-verso');
    setTimeout(function(){ window.print(); }, 50);
  }
</script>
</body>
</html>
"""

# Texte "contrat" par défaut — imprimé petit, identique dans les 6 cellules
# du verso. Ancré sur le vocabulaire réel du projet Made In Zion (MIZ,
# chantier-école coopératif intégré à UPlanet : chaque dôme/Astroport
# installé devient une ambassade Web3 rémunérée en Ẑen — cf. miz.html/
# mouvement.html) plutôt qu'inventé. Se termine par un rappel de la Charte
# Open Source (opensource.html) : le choix de licence est le socle légal de
# toute la coopérative, pas seulement de Made In Zion.
_BILLET_VERSO_DEFAULT_TEXT = (
    "Financez un Internet qui nous appartient, pas un cloud qui nous facture. "
    "Le Ẑen est la monnaie d'usage des serveurs et stations gérés en commun par le "
    "collectif G1FabLab/Monnaie Libre (opencollective.com/monnaie-libre). Ce billet protège qui "
    "le porte : son secret est scindé en 3 parts — une seule ne permet rien, et sa valeur "
    "se vérifie d'un simple scan. Zéro compte bancaire, zéro spéculation : la monnaie "
    "d'un bien commun citoyen (AGPL-3.0 / CC BY-SA). Rejoignez-nous sur astroport.one"
)

_BILLET_VERSO_CELL = """<div class="vcell">
  <div class="vflap"><div class="vflap-inner"><b>Encaisser</b><img src="__REDEEM_QR__" alt="QR reconstruction"></div></div>
  <div class="vgap"></div>
  <div class="vmain">
    <div class="vstamp">☀️ G1FabLab</div>
    <div class="vtext">__TEXT__</div>
    <div class="vsupport">
      <div class="vsupport-item"><img src="__OC_QR__" alt="QR OpenCollective"><span>opencollective.com/monnaie-libre</span></div>
      <div class="vsupport-item"><img src="__ZELKOVA_QR__" alt="QR Zelkova"><span>z.astroport.one</span></div>
    </div>
  </div>
</div>"""


def _render_billet_verso_html(text: str, qr_url: str, zelkova_qr_url: str, redeem_qr_url: str) -> str:
    """Grille verso optionnelle — MÊME grille (colonnes/lignes/espacement)
    que la planche recto, texte "contrat" identique répété dans les 6
    cellules : une fois imprimée en duplex et découpée, chaque billet porte
    ce texte au dos, quel que soit le sens du retournement duplex. Trois QR
    de soutien : OpenCollective (financer le G1FabLab) et Zelkova (le wallet
    Ẑen — z.astroport.one) dans .vmain, à l'abri du pli ; reconstruction
    (billet_redeem.html / SCAN) dans .vflap — au dos du pli lui-même (cf.
    commentaire CSS .vflap ci-dessus).

    `redeem_qr_url` est volontairement le même lien GÉNÉRIQUE
    (SANS ?g1pub=...) sur les 6 cellules : un lien par billet serait faux
    une fois sur deux après impression duplex + découpe, puisque la
    correspondance recto/verso par position n'est pas garantie (cf.
    commentaire ci-dessus) — contrairement au QR "reconstruction"
    spécifique à chaque billet qui existait au recto avant cette révision."""
    esc = html_lib.escape
    text_html = esc(text).replace("\n\n", "<br><br>").replace("\n", "<br>")
    cell = (
        _BILLET_VERSO_CELL
        .replace("__TEXT__", text_html)
        .replace("__OC_QR__", qr_url)
        .replace("__ZELKOVA_QR__", zelkova_qr_url)
        .replace("__REDEEM_QR__", redeem_qr_url)
    )
    cells = "\n".join([cell] * _BILLET_A4_COUNT)
    return f'<div class="sheet verso-sheet">\n{cells}\n</div>'

_BILLET_A4_CELL = """<div class="cell">
  <div class="fold"><div class="secret">__SECRET__</div></div>
  <div class="body __FOND_CLASS__" style="background-image:__FOND_CSS__;--tint:__TINT_SOLID__;--tint-overlay:__TINT_OVERLAY__">
    <div class="corner-stack">
      __LOGO_IMG__
      <div class="qr-item">
        <img src="__PUB_QR__" alt="QR solde">
        <span>🔍 Vérifier</span>
      </div>
    </div>
    <div class="p1-corner qr-item">
      <img src="__PROFILE_QR__" alt="QR P1">
      <span>P1</span>
    </div>
    <div class="main-row">
      <div class="amount-badge">
        <span class="num __AMOUNT_CLASS__">__AMOUNT_TEXT__</span><span class="unit">__UNIT_LABEL__</span>
      </div>
    </div>
    <div class="info-block">
      <div class="g1pub">__G1PUB__</div>
      <div class="expiry">__EXPIRES__</div>
      <div class="status __STATUS_CLASS__">__STATUS__</div>
    </div>
  </div>
</div>"""


# Teintes façon billets de banque — une couleur par coupure, pour
# différencier les planches au premier coup d'œil (pastel : le texte
# brand/ink reste lisible dessus). Pas d'entrée pour "vierge" (montant=0) :
# le billet garde alors la couleur papier neutre, montant à définir à la main.
_BILLET_TINTS: dict[float, str] = {
    5: "#e3e3e3", 10: "#f7dede", 20: "#dde8f7", 50: "#fbe6d0",
    100: "#dff0da", 200: "#faf3c9", 500: "#ecdcf2",
}


def _billet_tint_vars(amount: float) -> tuple[str, str]:
    """(couleur pleine, couleur translucide) pour la coupure donnée —
    variables CSS --tint/--tint-overlay. Retombe sur --paper si la coupure
    n'a pas de teinte dédiée (montant vierge ou libre)."""
    hexcolor = _BILLET_TINTS.get(amount)
    if not hexcolor:
        return "var(--paper)", "rgba(245,240,232,.74)"
    r, g, b = int(hexcolor[1:3], 16), int(hexcolor[3:5], 16), int(hexcolor[5:7], 16)
    return hexcolor, f"rgba({r},{g},{b},.62)"


def _render_billet_a4_html(
    amount: float,
    cells: list[dict],
    unit: str = "zen",
    unit_label: str = "Ẑen",
    fond_url: Optional[str] = None,
    logo_url: Optional[str] = None,
    verso_html: str = "",
) -> str:
    """Compose la planche A4 paysage — 6 billets recto seul (2 colonnes × 3
    lignes), part P2 (Shamir 2-sur-3) en vertical sur le bord gauche
    (à replier/scotcher). Embarque aussi `window.__G1BILLETS__` (g1pub +
    montant/unité par billet) : billet.html (même origine — onglet ouvert
    via window.open puis document.write, cf. generate.addEventListener dans
    UPlanet/earth/billet.html) lit ce global juste après l'écriture de la
    page pour construire son panneau de financement (6 contrôles), sans
    avoir à reparser le HTML imprimable ni à régénérer les clés."""
    esc = html_lib.escape
    fond = _sanitize_image_url(fond_url)
    logo = _sanitize_image_url(logo_url)
    fond_css = f"url('{esc(fond)}')" if fond else "none"
    fond_class = "has-fond" if fond else ""
    logo_img = f'<img class="logo-mark" src="{esc(logo)}" alt="">' if logo else ""
    tint_solid, tint_overlay = _billet_tint_vars(amount)

    is_blank = amount <= 0
    amount_class = "blank" if is_blank else ""
    amount_text = "&nbsp;" if is_blank else esc(f"{amount:g}")

    cell_blocks = []
    for c in cells:
        status = esc(c.get("status", "")) or ("Portefeuille vierge — montant à inscrire à la main" if is_blank else "")
        status_class = "err" if c.get("status_error") else ""
        # P2 sur EXACTEMENT 2 lignes (32+32 car.) — jamais laissé au retour à
        # la ligne naturel du navigateur (cf. commentaire CSS .fold .secret) :
        # un wrap imprévisible a déjà tronqué/rogné l'affichage à l'impression.
        p2_hex = c["p2"]
        p2_line1, p2_line2 = p2_hex[:32], p2_hex[32:]
        cell_blocks.append(
            _BILLET_A4_CELL
            .replace("__SECRET__", f'<div class="p2-text"><b>P2</b>{esc(p2_line1)}<br>{esc(p2_line2)}</div>'
                     f'<img src="{c.get("p2_qr_url", "")}" alt="QR P2">')
            .replace("__FOND_CSS__", fond_css)
            .replace("__FOND_CLASS__", fond_class)
            .replace("__TINT_SOLID__", tint_solid)
            .replace("__TINT_OVERLAY__", tint_overlay)
            .replace("__LOGO_IMG__", logo_img)
            .replace("__AMOUNT_CLASS__", amount_class)
            .replace("__AMOUNT_TEXT__", amount_text)
            .replace("__UNIT_LABEL__", esc(unit_label))
            .replace("__PUB_QR__", c["pub_qr_url"])
            .replace("__PROFILE_QR__", c.get("p1_qr_url", ""))
            .replace("__G1PUB__", esc(c["g1pub"]))
            .replace("__EXPIRES__", "Scan P1 &amp; P2 pour dépenser")
            .replace("__STATUS__", status)
            .replace("__STATUS_CLASS__", status_class)
        )

    verso_button = '<button onclick="printSide(\'verso\')">🖨️ Imprimer le Verso</button>' if verso_html else ""
    billets_json = json.dumps([
        {"g1pub": c["g1pub"], "amount": amount, "unit": unit} for c in cells
    ])
    return (
        _BILLET_A4_PAGE
        .replace("__COUNT__", str(len(cells)))
        .replace("__CELLS__", "\n".join(cell_blocks))
        .replace("__VERSO__", verso_html)
        .replace("__VERSO_BUTTON__", verso_button)
        .replace("__G1BILLETS_JSON__", billets_json)
    )


# ── Mailjet ───────────────────────────────────────────────────────────────────

async def _notify_captain(data: str, visitor_ip: str) -> None:
    captain = settings.CAPTAINEMAIL
    mailjet_sh = settings.TOOLS_PATH / "mailjet.sh"
    if not (captain and mailjet_sh.exists()):
        return
    body = (
        "<h2>🎟️ Nouveau MULTIPASS demandé</h2>"
        f"<p><b>URL :</b> {data}</p>"
        f"<p><b>Station :</b> {settings.uSPOT}</p>"
        f"<p><b>IP visiteur :</b> {visitor_ip}</p>"
    )
    tmp_msg = tempfile.NamedTemporaryFile(suffix=".html", mode="w", delete=False, encoding="utf-8")
    try:
        tmp_msg.write(body)
        tmp_msg.close()
        proc = await asyncio.create_subprocess_exec(
            str(mailjet_sh), "--expire", "0s", captain, tmp_msg.name,
            f"🐷 MULTIPASS demandé — {settings.uSPOT}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=30)
    except Exception as exc:
        logger.warning("Mailjet notification failed: %s", exc)
    finally:
        try:
            os.unlink(tmp_msg.name)
        except OSError:
            pass


# ── Génération QR ─────────────────────────────────────────────────────────────

def _generate_qr_png(
    data: str,
    version: int = 1,
    level: str = "H",
    colorized: bool = False,
    contrast: float = 1.0,
    brightness: float = 1.0,
    picture_path: Optional[str] = None,
    color: str = "000000",
    bgcolor: str = "ffffff",
) -> tuple[bytes | None, str]:
    """Génère un PNG QR. Retourne (png_bytes | None, moteur_utilisé)."""
    level = level.upper() if level.upper() in ("L", "M", "Q", "H") else "H"
    version = max(1, min(40, int(version)))
    use_picture = picture_path and os.path.isfile(picture_path)

    logger.info(
        "QR gen — data=%.60r version=%s level=%s colorized=%s picture=%s",
        data, version, level, colorized, use_picture,
    )

    # ── amzqr — auto-monte la version si overflow ─────────────────
    import subprocess as _sp
    _astro_amzqr = os.path.expanduser("~/.astro/bin/amzqr")
    _amzqr_bin = shutil.which("amzqr") or (_astro_amzqr if os.path.isfile(_astro_amzqr) else None)
    if _amzqr_bin:
        for v in range(version, 41):
            tmp = tempfile.mkdtemp()
            out = os.path.join(tmp, "qr.png")
            try:
                cmd = [_amzqr_bin, data, "-v", str(v), "-l", level, "-n", "qr.png", "-d", tmp]
                if use_picture:
                    cmd += ["-p", picture_path]
                    if colorized:
                        cmd += ["-c"]
                if contrast != 1.0:
                    cmd += ["-con", str(contrast)]
                if brightness != 1.0:
                    cmd += ["-bri", str(brightness)]
                logger.debug("amzqr v%s picture=%s colorized=%s", v, use_picture, colorized and use_picture)
                result_proc = _sp.run(cmd, capture_output=True, text=True)
                if os.path.isfile(out):
                    result = Path(out).read_bytes()
                    shutil.rmtree(tmp, ignore_errors=True)
                    logger.info("amzqr OK v%s → %d bytes", v, len(result))
                    return result, "amzqr"
                stderr = result_proc.stderr.lower()
                if "overflow" in stderr or "too long" in stderr or "capacity" in stderr:
                    logger.debug("amzqr v%s overflow → essai v%s", v, v + 1)
                    shutil.rmtree(tmp, ignore_errors=True)
                    continue
                logger.warning("amzqr v%s échec: %s", v, result_proc.stderr.strip())
                shutil.rmtree(tmp, ignore_errors=True)
                break
            except Exception as e:
                shutil.rmtree(tmp, ignore_errors=True)
                logger.warning("amzqr v%s erreur inattendue: %s", v, e)
                break
    else:
        logger.warning("amzqr non trouvé dans PATH — fallback qrencode")

    # ── qrencode fallback ─────────────────────────────────────────
    try:
        import subprocess
        tmp2 = tempfile.mktemp(suffix=".png")
        cmd = [
            "qrencode", "-s", "8", "-t", "PNG",
            "-l", level,
            "--foreground", color.upper().lstrip("#"),
            "--background", bgcolor.upper().lstrip("#"),
            "-o", tmp2, "--", data,
        ]
        logger.debug("qrencode: %s", " ".join(cmd[:6]))
        subprocess.run(cmd, check=True, capture_output=True)
        png = Path(tmp2).read_bytes()
        os.unlink(tmp2)
        logger.info("qrencode OK → %d bytes (image de fond ignorée)", len(png))
        if use_picture:
            logger.warning("L'image de fond a été ignorée : amzqr requis pour les QR artistiques")
        return png, "qrencode"
    except Exception as e:
        logger.error("qrencode échec: %s", e)

    return None, "none"


async def _download_picture_url(url: str) -> Optional[str]:
    """Télécharge une image depuis une URL, renvoie le chemin du fichier temporaire."""
    logger.info("Téléchargement image: %s", url[:80])
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            ct = r.headers.get("content-type", "image/png")
            ext = ".jpg" if "jpeg" in ct else ".gif" if "gif" in ct else ".png"
            tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
            tmp.write(r.content)
            tmp.close()
            logger.info("Image téléchargée → %s (%d bytes)", tmp.name, len(r.content))
            return tmp.name
    except Exception as e:
        logger.warning("Échec téléchargement image %s : %s", url[:60], e)
        return None


# ── Ğ1Billet — clé jetable + paiement ────────────────────────────────────────

async def _gen_billet_key() -> dict:
    """Génère une clé G1 jetable via billet_gen.sh (tout en /dev/shm, jamais
    journalisé — cf. billet_gen.sh). Retourne {"g1pub","p1","p2","p3"} — le
    seed a été scindé en 3 parts Shamir (2-sur-3, GF(256)), p1/p2/p3 en hex
    (32 octets chacune)."""
    proc = await asyncio.create_subprocess_exec(
        str(_BILLET_SCRIPT),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    lines = [l for l in stdout.decode().splitlines() if l.strip()]
    try:
        keydata = json.loads(lines[-1]) if lines else {}
    except Exception:
        raise RuntimeError(f"sortie JSON invalide (stderr={stderr.decode()[:200]})")
    if not keydata or "error" in keydata or not keydata.get("g1pub"):
        raise RuntimeError(keydata.get("error") if keydata else "sortie vide")
    return keydata



_BILLET_WITNESS_KEYFILE = Path.home() / ".zen" / "game" / "uplanet.G1.nostr"
_BILLET_WITNESS_TTL_S = 180 * 86400  # NIP-40 expiration — cf. _publish_billet_witness


async def _encrypt_with_uplanetname(plaintext_hex: str) -> Optional[str]:
    """Chiffre une valeur via coop_encrypt() (cooperative_config.sh —
    AES-256-CBC, clé = sha256($UPLANETNAME)) : même convention que la config
    coopérative (kind 30800). Retourne "iv:base64", ou None si échec."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c",
            f'source "{settings.TOOLS_PATH}/cooperative_config.sh" >/dev/null 2>&1 && coop_encrypt "$1"',
            "--", plaintext_hex,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        out = stdout.decode().strip()
        if proc.returncode != 0 or not out:
            logger.warning("billet witness — coop_encrypt échec: %s", stderr.decode()[:200])
            return None
        return out
    except Exception as e:
        logger.warning("billet witness — coop_encrypt erreur: %s", e)
        return None


async def _publish_billet_witness(
    p3_hex: str, g1pub: str, amount: float, unit_label: str, creator_hex: str,
) -> bool:
    """Publie la part P3 (Shamir 2-sur-3, cf. billet_gen.sh) CHIFFRÉE sur
    NOSTR, comme témoin d'émission du billet — réserve de recours/support
    UNIQUEMENT, jamais utilisée dans le flux normal de dépense (cf.
    UPlanet/earth/billet_redeem.html, qui ne réunit que P1+P2, imprimées
    toutes deux sur le billet). Il n'existe plus aucune identité NOSTR
    propre au billet : cet event est signé par l'identité coopérative de la
    station (~/.zen/game/uplanet.G1.nostr — même identité que la config
    coopérative kind 30800), avec un tag `["p", creator_hex]` reliant le
    billet au MULTIPASS de son créateur (authentifié via NIP-98, cf.
    generate_billet) — traçabilité de l'émission, PAS une protection
    anti-purge (ce témoin reste chiffré/inerte sans P1+P2).

    P3 est chiffrée avec $UPLANETNAME (_encrypt_with_uplanetname) : seule
    une station de la même constellation peut la déchiffrer. Kind 30078
    (NIP-78, générique) : PAS de protection anti-purge dédiée — contrairement
    à l'ancien profil kind 0, ce témoin est inerte sans P1+P2 ; sa purge
    éventuelle ne dégrade que le recours en dernier ressort, jamais le
    portefeuille Ğ1 lui-même.

    Best-effort : un échec ne doit jamais faire échouer la génération du
    billet. Retourne True si la publication a réussi.

    Tag NIP-40 `expiration` à _BILLET_WITNESS_TTL_S (180 jours) : la P3
    n'existe qu'une fois, au moment de CET appel (jamais régénérable), donc
    ce témoin doit être publié à CHAQUE génération, financée ou non — sans
    ça, un MULTIPASS réel qui génère des planches "pour essayer" sans les
    financer laisserait des témoins orphelins s'accumuler indéfiniment sur
    le relay (is_multipass_user, cf. generate_billet, ne bloque que les
    comptes jamais inscrits, pas les essais d'un compte réel). Un relay qui
    honore NIP-40 purge automatiquement les témoins expirés : la fenêtre de
    180 jours couvre largement le temps normal entre génération et dépense
    d'un billet physique, sans faire grossir le relay indéfiniment pour les
    planches jamais financées. N'affecte QUE le recours P3 (sauvegarde en
    cas de P1 ou P2 perdue/abîmée) : la reconstruction normale (P1+P2 via
    /scan) ne dépend jamais de ce témoin ni de sa durée de vie.
    """
    encrypted = await _encrypt_with_uplanetname(p3_hex)
    if not encrypted:
        return False
    content = json.dumps({"p3": encrypted})
    tags = json.dumps([
        ["d", f"g1billet:{g1pub}"], ["t", "g1billet"], ["g1pub", g1pub],
        ["amount", f"{amount:g}"], ["unit", unit_label], ["p", creator_hex],
    ])
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", str(settings.TOOLS_PATH / "nostr_send_note.py"),
            "--keyfile", str(_BILLET_WITNESS_KEYFILE), "--content", content,
            "--kind", "30078", "--tags", tags, "--relays", settings.myRELAY,
            "--ephemeral", str(_BILLET_WITNESS_TTL_S),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            logger.warning("billet witness NOSTR — échec pour %s: %s", g1pub[:16], stderr.decode()[:200])
            return False
        return True
    except Exception as e:
        logger.warning("billet witness NOSTR — erreur: %s", e)
        return False


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/qr")
@router.post("/qr")
async def generate_qr(
    request:     Request,
    background_tasks: BackgroundTasks,
    data:        Optional[str]   = Query(None),
    color:       Optional[str]   = Query(None),
    bgcolor:     Optional[str]   = Query(None),
    format:      Optional[str]   = Query(None),
    html:        Optional[int]   = Query(None),
    version:     Optional[int]   = Query(None),
    level:       Optional[str]   = Query(None),
    colorized:   Optional[int]   = Query(None),
    contrast:    Optional[float] = Query(None),
    brightness:  Optional[float] = Query(None),
    picture_url: Optional[str]   = Query(None),
):
    if html:
        return HTMLResponse(_qr_html())

    picture_path: Optional[str] = None

    if request.method == "POST":
        form = await request.form()

        def _f(k: str, default: str = "") -> str:
            return str(form.get(k) or default)

        data        = data        or _f("data")
        color       = color       or _f("color",       "000000")
        bgcolor     = bgcolor     or _f("bgcolor",      "ffffff")
        format      = format      or _f("format",       "png")
        picture_url = picture_url or (_f("picture_url") or None)
        version     = version     if version   is not None else int(_f("version",    "1") or "1")
        level       = level                            or _f("level",      "H")
        colorized   = colorized   if colorized is not None else int(_f("colorized", "0") or "0")
        contrast    = contrast    if contrast  is not None else float(_f("contrast",  "1.0") or "1.0")
        brightness  = brightness  if brightness is not None else float(_f("brightness","1.0") or "1.0")

        pic = form.get("picture")
        if pic and hasattr(pic, "read"):
            pic_bytes = await pic.read()
            if pic_bytes:
                suffix = Path(getattr(pic, "filename", "pic.png")).suffix or ".png"
                tmp_pic = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
                tmp_pic.write(pic_bytes)
                tmp_pic.close()
                picture_path = tmp_pic.name
                logger.info("Fichier uploadé: %s (%d bytes) → %s", getattr(pic, "filename", "?"), len(pic_bytes), picture_path)

    # Télécharger picture_url si fournie et aucun fichier uploadé
    if picture_url and not picture_path:
        picture_path = await _download_picture_url(picture_url)

    # Valeurs par défaut
    data       = (data or "").strip()
    color      = (color   or "000000").lstrip("#")
    bgcolor    = (bgcolor  or "ffffff").lstrip("#")
    format     = format     or "png"
    version    = max(1, min(40, int(version or 1)))
    level      = (level    or "H").upper()
    if level not in ("L", "M", "Q", "H"):
        level = "H"
    colorized  = bool(int(colorized  or 0))
    contrast   = float(contrast   or 1.0)
    brightness = float(brightness or 1.0)

    if not data:
        return JSONResponse({"error": "missing data parameter"}, status_code=400)

    if data in ("/", ""):
        data = str(settings.uSPOT).rstrip("/") + "/g1nostr"

    visitor_ip = request.client.host if request.client else "?"
    background_tasks.add_task(_notify_captain, data, visitor_ip)

    try:
        png, engine = await asyncio.to_thread(
            _generate_qr_png,
            data, version, level, colorized,
            contrast, brightness, picture_path, color, bgcolor,
        )
    finally:
        if picture_path:
            try:
                os.unlink(picture_path)
            except OSError:
                pass

    logger.info("Réponse: format=%s engine=%s png=%s", format, engine, bool(png))

    if format == "json":
        if png:
            return JSONResponse({
                "dataUrl": "data:image/png;base64," + base64.b64encode(png).decode(),
                "data":    data,
                "engine":  engine,
            })
        fallback = (
            f"https://api.qrserver.com/v1/create-qr-code/"
            f"?size=180x180&data={urllib.parse.quote_plus(data)}&color={color}"
        )
        return JSONResponse({"fallback": fallback, "data": data, "engine": "fallback"})

    if png:
        return Response(content=png, media_type="image/png")

    fallback = (
        f"https://api.qrserver.com/v1/create-qr-code/"
        f"?size=180x180&data={urllib.parse.quote_plus(data)}&color={color}"
    )
    return RedirectResponse(url=fallback)


@router.get("/qr/postcard")
@router.post("/qr/postcard")
async def generate_postcard(
    request:    Request,
    data:       Optional[str] = Query(None),
    title:      Optional[str] = Query(None),
    image_url:  Optional[str] = Query(None),
    back_title: Optional[str] = Query(None),
    message:    Optional[str] = Query(None),
    footer:     Optional[str] = Query(None),
    level:      Optional[str] = Query(None),
    version:    Optional[int] = Query(None),
    html:       Optional[int] = Query(None),
):
    """Carte postale imprimable 10x15cm — recto QR+image, verso message.

    Générique : réutilisable pour n'importe quel projet (`data` est la seule
    valeur obligatoire), pas seulement pour un manuel UPlanet donné.
    """
    if html:
        return HTMLResponse(_postcard_config_html())

    if request.method == "POST":
        form = await request.form()

        def _f(k: str, default: str = "") -> str:
            return str(form.get(k) or default)

        data       = data       or _f("data")
        title      = title      or _f("title")
        image_url  = image_url  or (_f("image_url") or None)
        back_title = back_title or _f("back_title")
        message    = message    or _f("message")
        footer     = footer     or _f("footer")
        level      = level      or _f("level", "H")
        version    = version    if version is not None else int(_f("version", "1") or "1")

    data = (data or "").strip()
    if not data:
        return JSONResponse({"error": "missing data parameter"}, status_code=400)

    title      = (title or "Carte Postale").strip()
    image_url  = (image_url or "").strip() or None
    back_title = (back_title or "").strip()
    message    = (message or "").strip()
    footer     = (footer or "").strip()
    level      = (level or "H").upper()
    if level not in ("L", "M", "Q", "H"):
        level = "H"
    version = max(1, min(40, int(version or 1)))

    png, engine = await asyncio.to_thread(_generate_qr_png, data, version, level)
    qr_data_url = ("data:image/png;base64," + base64.b64encode(png).decode()) if png else ""
    logger.info("Postcard — data=%.60r engine=%s png=%s", data, engine, bool(png))

    page = _render_postcard_html(
        data=data,
        qr_data_url=qr_data_url,
        front_title=title,
        front_image_url=image_url,
        back_title=back_title,
        message=message,
        footer=footer,
    )
    return HTMLResponse(page)


_BILLET_A4_COUNT = 6  # 2 colonnes × 3 lignes — grille validée sur planche de référence


@router.get("/qr/billet")
@router.post("/qr/billet")
async def generate_billet(
    request: Request,
    background_tasks: BackgroundTasks,
    amount: Optional[float] = Query(None),
    unit: Optional[str] = Query(None),
    fond_url: Optional[str] = Query(None),
    logo_url: Optional[str] = Query(None),
    verso: Optional[str] = Query(None),
    verso_text: Optional[str] = Query(None),
    creator_hex: str = Depends(verify_nip98_auth),
):
    """Planche A4 paysage de 6 Ğ1Billets papier imprimables — remplace le
    moteur graphique G1BILLET. RECTO SEUL : 6 clés jetables indépendantes,
    secret en vertical sur le bord gauche de chaque billet (à replier vers
    l'avant et scotcher). Portefeuilles TOUJOURS vierges à la génération —
    pas de débit automatique depuis un MULTIPASS (mode `auto` retiré :
    peu fiable en pratique, cf. échecs constatés en production le 2026-09-24).
    Financement manuel après coup, par qui veut, comme pour n'importe quel
    portefeuille Ğ1.

    Auth NIP-98 OBLIGATOIRE (Authorization: Nostr <event>, kind 27235) — même
    mécanisme que /api/cloud/enroll, /api/fileupload, /api/calendar/email.
    Mais une signature NIP-98 valide prouve seulement la possession d'UNE
    clé NOSTR — pas que ce soit un MULTIPASS réellement enregistré (compte
    créé via UPassport /g1nostr, dossier ~/.zen/game/nostr/{email}/). Sans
    second contrôle, n'importe qui avec une extension NOSTR fraîchement
    installée (gratuit, 10 secondes, jamais inscrit) pourrait générer des
    planches à volonté — 6 témoins NOSTR publiés (cf. _publish_billet_witness)
    par appel, quasi jamais financés : pollution du relay sans aucun coût.
    `is_multipass_user(creator_hex)` ferme ce trou : seul un compte RÉELLEMENT
    enregistré peut générer. Le pubkey authentifié (`creator_hex`) est aussi
    associé au témoin NOSTR de chaque billet (tag `p`, cf.
    _publish_billet_witness) — traçabilité de l'émission, pas une
    restriction d'usage du billet lui-même (toujours un bearer Ğ1 classique).

    unit : zen|g1 (défaut zen) — unité du montant annoncé (affichage seul,
    aucun transfert n'est déclenché par cet endpoint).
    1 Ẑen = 0.1 Ğ1 (convention UPlanet ORIGIN).

    verso : "1" pour ajouter une page verso à l'impression, RÉPÉTÉE dans une
    grille identique à la planche recto (même 2×3, mêmes dimensions de
    cellule) — après découpe, chaque billet porte ce texte au dos. Texte
    "contrat" court (~550 car. max, imprimé petit, horizontal) + QR de soutien
    OpenCollective. `verso_text` (optionnel) remplace le texte par défaut.
    """
    if not is_multipass_user(creator_hex):
        raise HTTPException(
            status_code=403,
            detail="Seul un MULTIPASS enregistré peut générer un Ğ1Billet (pas une simple clé NOSTR connectée)",
        )

    if request.method == "POST":
        form = await request.form()

        def _f(k: str, default: str = "") -> str:
            return str(form.get(k) or default)

        amount     = amount     if amount is not None else float(_f("amount", "0") or "0")
        unit       = unit       or _f("unit", "zen")
        fond_url   = fond_url   or (_f("fond_url") or None)
        logo_url   = logo_url   or (_f("logo_url") or None)
        verso      = verso      or (_f("verso") or None)
        verso_text = verso_text or (_f("verso_text") or None)

    verso_enabled = str(verso or "").strip().lower() in ("1", "true", "yes", "on")

    unit = (unit or "zen").strip().lower()
    if unit not in ("zen", "g1"):
        unit = "zen"
    unit_label = "Ğ1" if unit == "g1" else "Ẑen"

    try:
        amount = float(amount or 0)
    except (TypeError, ValueError):
        amount = 0.0
    if amount < 0:
        return JSONResponse({"error": "Montant invalide"}, status_code=400)

    visitor_ip = request.client.host if request.client else "?"

    cells: list[dict] = []
    for _ in range(_BILLET_A4_COUNT):
        try:
            keydata = await _gen_billet_key()
        except RuntimeError as e:
            logger.error("billet_gen.sh (a4x6): %s", e)
            return JSONResponse({"error": "génération de clé impossible"}, status_code=500)

        g1pub = keydata["g1pub"]
        p1, p2, p3 = keydata["p1"], keydata["p2"], keydata["p3"]
        keydata = None

        await _publish_billet_witness(p3, g1pub, amount, unit_label, creator_hex)
        p3 = None

        # QR "Solde" = lien de vérification (PAS le g1pub brut) : un g1pub seul,
        # scanné par l'appareil photo d'un inconnu, n'affiche qu'une chaîne
        # inexploitable. Un lien /scan?g1pub=... ouvre directement la page de
        # validation de valeur — "entête" qui facilite la découverte du billet
        # par quiconque le trouve, sans application dédiée.
        solde_url = settings.uSPOT.rstrip("/") + "/scan?g1pub=" + urllib.parse.quote(g1pub)
        # Logo UPlanet embarqué (amzqr -p, cf. _BILLET_VERIFY_LOGO) : lien
        # GÉNÉRIQUE (le domaine, pas le g1pub) — même logo sur tous les
        # billets, donc même visuel reconnaissable au premier coup d'œil.
        # Niveau "H" obligatoire avec une image embarquée (amzqr/billet_astroid.py) :
        # sans la correction d'erreur maximale, le logo recouvre trop de
        # modules pour rester scannable. _generate_qr_png retombe sur
        # qrencode (sans logo) si amzqr ou l'image sont absents.
        pub_png, _ = await asyncio.to_thread(
            _generate_qr_png, solde_url, 3, "H", False, 1.0, 1.0,
            str(_BILLET_VERIFY_LOGO) if _BILLET_VERIFY_LOGO.is_file() else None,
        )
        pub_qr_url = ("data:image/png;base64," + base64.b64encode(pub_png).decode()) if pub_png else ""

        # QR "P1" = part Shamir visible (hex, PAS un lien web) — seule, elle
        # ne permet pas de reconstruire le secret (il faut au moins 2 des 3
        # parts P1/P2/P3, cf. UPlanet/earth/billet_redeem.html).
        p1_png, _ = await asyncio.to_thread(_generate_qr_png, p1, 4, "M")
        p1_qr_url = ("data:image/png;base64," + base64.b64encode(p1_png).decode()) if p1_png else ""

        # QR "P2" — même nature que P1 (hex, inerte seule), imprimé sous la
        # bande repliable/scellée.
        p2_png, _ = await asyncio.to_thread(_generate_qr_png, p2, 4, "M")
        p2_qr_url = ("data:image/png;base64," + base64.b64encode(p2_png).decode()) if p2_png else ""

        cells.append({
            "g1pub": g1pub, "p1": p1, "p2": p2,
            "pub_qr_url": pub_qr_url, "p1_qr_url": p1_qr_url, "p2_qr_url": p2_qr_url,
        })

    background_tasks.add_task(
        _notify_captain,
        f"Planche Ğ1Billet générée — {len(cells)}×{amount:g} {unit_label}",
        visitor_ip,
    )

    verso_html = ""
    if verso_enabled:
        text = (verso_text or "").strip()[:550] or _BILLET_VERSO_DEFAULT_TEXT
        oc_png, _ = await asyncio.to_thread(_generate_qr_png, "https://opencollective.com/monnaie-libre", 3, "M")
        oc_qr_url = ("data:image/png;base64," + base64.b64encode(oc_png).decode()) if oc_png else ""
        zelkova_png, _ = await asyncio.to_thread(_generate_qr_png, "https://z.astroport.one", 3, "M")
        zelkova_qr_url = ("data:image/png;base64," + base64.b64encode(zelkova_png).decode()) if zelkova_png else ""
        # Lien GÉNÉRIQUE (sans ?g1pub=...) — jamais de lien par-billet ici :
        # la correspondance recto/verso par position n'est pas garantie après
        # impression duplex + découpe (cf. _render_billet_verso_html).
        # settings.uSPOT (PAS request.base_url) : ce dernier reflète l'hôte
        # de la requête EN COURS — s'il s'agit d'un test en local
        # (http://127.0.0.1:54321), cette adresse inutilisable depuis
        # l'extérieur se retrouverait figée dans le QR imprimé. uSPOT est
        # l'URL publique canonique de la station (https://u.copylaradio.com
        # par défaut), valable pour quiconque scanne le billet plus tard.
        redeem_url = settings.uSPOT.rstrip("/") + "/scan"
        redeem_png, _ = await asyncio.to_thread(_generate_qr_png, redeem_url, 3, "M")
        redeem_qr_url = ("data:image/png;base64," + base64.b64encode(redeem_png).decode()) if redeem_png else ""
        verso_html = _render_billet_verso_html(
            text=text, qr_url=oc_qr_url, zelkova_qr_url=zelkova_qr_url, redeem_qr_url=redeem_qr_url,
        )

    page = _render_billet_a4_html(
        amount=amount, cells=cells, unit=unit, unit_label=unit_label, fond_url=fond_url, logo_url=logo_url,
        verso_html=verso_html,
    )
    cells = None
    return HTMLResponse(page)


# ── Portefeuille coopératif UPLANETNAME_G1 ───────────────────────────────────
# Règle primale unique à TOUS les portefeuilles UPlanet (cf. Astroport.ONE/
# tools/primal_wallet_control.sh::control_primal_transactions) : la source
# primale d'un wallet = l'émetteur de sa toute première transaction reçue ;
# seule une source dont le primal est UPLANETNAME_G1 (ou UPLANETNAME_G1
# lui-même) est reconnue comme légitime — toute autre source entrante est
# traitée comme une INTRUSION et redirigée/vidée. Le financement ET
# l'encaissement d'un Ğ1Billet doivent donc, l'un comme l'autre, transiter
# par ce portefeuille pour rester cohérents avec cette règle si elle est un
# jour étendue aux billets (actuellement non surveillés : pas de player_email
# associé, cf. ci-dessous).
_BILLET_PRIMO_G1 = 1.0    # dépôt d'activation (existential deposit Duniter)
_BILLET_FUND_EPS = 0.01   # tolérance flottante sur les comparaisons de solde G1


async def _run_payforsure(key_or_vault: str, amount: str, dest_g1pub: str, comment: str, timeout: int = 90) -> tuple[bool, str]:
    """Exécute PAYforSURE.sh, retourne (succès, log stdout+stderr tronqué)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            str(settings.TOOLS_PATH / "PAYforSURE.sh"), key_or_vault, amount, dest_g1pub, comment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        return False, "délai dépassé"
    ok = proc.returncode == 0
    return ok, (stdout.decode(errors="replace") + stderr.decode(errors="replace"))[-2000:]


def _read_dunikey_pubkey(dunikey_path: str) -> str:
    """Lit la ligne `pub:` d'un fichier dunikey (format pubsec) — même
    convention que billet_gen.sh. Pas de conversion SS58 : get_g1_balance_native
    essaie déjà les deux formats (cf. services/g1_squid.py)."""
    try:
        with open(dunikey_path) as f:
            for line in f:
                if line.startswith("pub:"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


async def _resolve_uplanet_g1() -> tuple[Path, str]:
    """Résout le portefeuille coopératif UPLANETNAME_G1 (dunikey + pubkey).
    Source `my.sh` via get_env_from_mysh : crée le fichier dunikey au passage
    s'il n'existe pas encore (cf. tools/my.sh::init_and_cache_wallet)."""
    dunikey = Path.home() / ".zen" / "game" / "uplanet.G1.dunikey"
    pub = await get_env_from_mysh("UPLANETNAME_G1")
    if not pub or not dunikey.is_file():
        raise HTTPException(status_code=500, detail="Portefeuille coopératif UPLANETNAME_G1 indisponible")
    return dunikey, pub


async def _destination_primal_ok(dest_g1pub: str, uplanet_g1_pub: str) -> bool:
    """Règle primale unique à tous les portefeuilles UPlanet (cf.
    Astroport.ONE/tools/primal_wallet_control.sh::control_primal_transactions) :
    le "primal" d'un wallet = l'émetteur de sa TOUTE PREMIÈRE transaction
    reçue. Une destination n'est légitime que si elle EST UPLANETNAME_G1, ou
    si son primal L'EST — sinon les Ẑen forwardés par /qr/billet/redeem
    sortiraient définitivement du circuit coopératif vers une adresse sans
    aucun lien avec UPlanet. Un wallet jamais crédité (aucune transaction
    reçue, primal introuvable) est refusé par prudence : il n'a par
    définition pas pu recevoir sa primo transaction de UPLANETNAME_G1."""
    dest_ss58 = g1pub_to_ss58(dest_g1pub)
    uplanet_ss58 = g1pub_to_ss58(uplanet_g1_pub)
    if dest_ss58 == uplanet_ss58:
        return True
    primal = await get_g1_primal_source(dest_g1pub)
    return bool(primal) and g1pub_to_ss58(primal) == uplanet_ss58


def _read_nostr_hex_pubkey(keyfile: Path) -> str:
    """Lit le HEX d'un fichier .secret.nostr (format `NSEC=...; NPUB=...; HEX=...;`,
    cf. nostr_send_note.py)."""
    try:
        content = keyfile.read_text()
    except OSError:
        return ""
    m = re.search(r"HEX=([0-9a-f]{64})", content)
    return m.group(1) if m else ""


async def _fetch_billet_witness(g1pub: str) -> Optional[dict]:
    """Interroge le relay local pour retrouver le témoin NOSTR (kind 30078,
    `d=g1billet:{g1pub}`, cf. _publish_billet_witness) d'un Ğ1Billet.

    GARDE-FOU DE SÉCURITÉ pour fund_billet : sans cette vérification,
    n'IMPORTE QUEL MULTIPASS authentifié pourrait appeler /qr/billet/fund
    avec un g1pub de son choix (une adresse qu'il contrôle, jamais générée
    par /qr/billet) et se faire créditer 1 Ğ1 GRATUIT depuis le portefeuille
    coopératif UPLANETNAME_G1 (la "primo transaction" n'est déclenchée que
    si le solde est < 1 Ğ1, ce qui est vrai de n'importe quelle adresse
    neuve) — un puits sans fond pour vider la trésorerie commune, 1 Ğ1 à la
    fois. Le témoin est signé par l'identité coopérative de la STATION
    elle-même (_BILLET_WITNESS_KEYFILE, jamais accessible à un MULTIPASS) :
    un attaquant ne peut pas le forger. fund_billet exige en plus que son
    tag `p` (creator_hex) corresponde à l'appelant authentifié — on ne peut
    financer que les billets qu'on a soi-même générés.

    Retourne les tags sous forme de dict (`{"p": "...", "amount": "...", ...}`)
    ou None si introuvable (relay injoignable, ou billet inconnu)."""
    witness_hex = _read_nostr_hex_pubkey(_BILLET_WITNESS_KEYFILE)
    if not witness_hex:
        return None
    d_tag = f"g1billet:{g1pub}"
    found = None
    try:
        async with websockets.connect(get_nostr_relay_url(), open_timeout=5) as ws:
            sub_id = "billetwit" + uuid.uuid4().hex[:10]
            await ws.send(json.dumps(["REQ", sub_id, {
                "kinds": [30078], "authors": [witness_hex], "#d": [d_tag], "limit": 1,
            }]))
            try:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    msg = json.loads(raw)
                    if msg[0] == "EVENT" and len(msg) >= 3:
                        found = msg[2]
                    elif msg[0] == "EOSE":
                        break
            except asyncio.TimeoutError:
                pass
            try:
                await ws.send(json.dumps(["CLOSE", sub_id]))
            except Exception:
                pass
    except Exception as e:
        logger.warning("fetch_billet_witness: relay injoignable — %s", e)
        return None
    if not found:
        return None
    return {t[0]: t[1] for t in found.get("tags", []) if len(t) >= 2}


@router.post("/qr/billet/fund")
async def fund_billet(request: Request, creator_hex: str = Depends(verify_nip98_auth)):
    """Financement MANUEL, billet par billet, d'une planche déjà imprimée —
    panneau "6 contrôles" de billet.html (cf. `window.__G1BILLETS__` embarqué
    dans la planche par generate_billet). Remplace l'ancien mode `auto`
    (retiré le 2026-09-24, cf. git log : 6 paiements enchaînés en aveugle
    dans UNE SEULE requête HTTP, aucun retry isolé possible, peu fiable en
    pratique). Ici chaque billet se finance via son propre appel HTTP,
    indépendant, avec son propre statut — un échec n'affecte pas les 5 autres.

    Deux virements successifs, pour respecter la règle primale unique à tous
    les portefeuilles UPlanet (cf. commentaire au-dessus de _BILLET_PRIMO_G1) :
      1. Si le billet n'a encore rien reçu (solde < 1 Ğ1), on lui envoie
         D'ABORD 1 Ğ1 depuis UPLANETNAME_G1 — "primo transaction" : établit
         UPLANETNAME_G1 comme primal du billet (dépôt d'activation Duniter).
      2. On envoie ENSUITE le montant annoncé (converti en Ğ1) depuis le
         MULTIPASS connecté (`creator_hex`) — son propre primal étant déjà
         UPLANETNAME_G1 (onboarding standard UPlanet), cette 2ᵉ transaction
         reste valide pour un éventuel contrôle primal ultérieur.
    Idempotent : si le billet a déjà le solde attendu (1 Ğ1 + montant), ne
    renvoie rien — sûr à rappeler plusieurs fois (retry manuel depuis le
    panneau de financement)."""
    data = await request.json()
    g1pub = str(data.get("g1pub", "")).strip()
    try:
        amount = float(data.get("amount", 0))
    except (TypeError, ValueError):
        amount = 0.0
    unit = str(data.get("unit", "zen")).strip().lower()
    if unit not in ("zen", "g1"):
        unit = "zen"

    if not is_safe_g1pub(g1pub):
        raise HTTPException(status_code=400, detail="g1pub invalide")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Montant invalide (0 = billet vierge, rien à financer)")

    # Garde-fou de sécurité : seul un g1pub RÉELLEMENT émis par /qr/billet,
    # par CE créateur précis, peut déclencher la primo transaction gratuite
    # (1 Ğ1 depuis la trésorerie commune UPLANETNAME_G1) — cf. docstring de
    # _fetch_billet_witness. Sans ce contrôle, n'importe quel MULTIPASS
    # authentifié pourrait vider la trésorerie 1 Ğ1 à la fois vers une
    # adresse de son choix.
    witness = await _fetch_billet_witness(g1pub)
    if not witness or witness.get("p") != creator_hex:
        raise HTTPException(
            status_code=403,
            detail="Ce G1PUB ne correspond à aucun Ğ1Billet émis par vous",
        )

    user_dir = find_user_directory_by_hex(creator_hex)  # lève déjà HTTPException 404
    captain_keyfile = user_dir / ".secret.dunikey"
    if not captain_keyfile.is_file():
        raise HTTPException(status_code=404, detail="Clé du MULTIPASS introuvable")

    uplanet_g1_dunikey, _uplanet_g1_pub = await _resolve_uplanet_g1()

    g1_amount = amount if unit == "g1" else amount / 10
    target_g1 = _BILLET_PRIMO_G1 + g1_amount

    async def _balance() -> float:
        bal = await get_g1_balance_native(g1pub)
        return (bal.get("balances", {}).get("total", 0) or 0) / 100

    balance = await _balance()
    if balance >= target_g1 - _BILLET_FUND_EPS:
        return JSONResponse({"ok": True, "already": True, "balance": balance})

    logs = []
    if balance < _BILLET_PRIMO_G1 - _BILLET_FUND_EPS:
        ok, log = await _run_payforsure(
            str(uplanet_g1_dunikey), f"{_BILLET_PRIMO_G1:.2f}", g1pub, "UPLANET:BILLET:PRIMO",
        )
        logs.append("primo: " + log)
        if not ok:
            logger.error("fund_billet: primo échoué pour %s… — %s", g1pub[:12], log)
            return JSONResponse(
                {"ok": False, "detail": "Échec du dépôt d'activation (1 Ğ1)", "log": "\n".join(logs)},
                status_code=502,
            )

    ok, log = await _run_payforsure(
        str(captain_keyfile), f"{g1_amount:.4f}", g1pub, f"UPLANET:BILLET:{amount:g}{unit.upper()}",
    )
    logs.append("financement: " + log)
    if not ok:
        logger.error("fund_billet: financement échoué pour %s… — %s", g1pub[:12], log)
        return JSONResponse(
            {"ok": False, "detail": "Échec du financement", "log": "\n".join(logs)}, status_code=502,
        )

    return JSONResponse({"ok": True, "balance": await _balance(), "log": "\n".join(logs)})


@router.get("/qr/billet/network_status")
async def billet_network_status():
    """Statut du réseau Duniter/Squid pour affichage UI (cf. /scan) : quel
    nœud sert actuellement, latence, fraîcheur du cache — PAS un ping en
    direct, lecture du cache déjà entretenu par duniter_getnode.sh (même
    source que PAYforSURE.sh et get_g1_balance_native). Public, lecture
    seule, aucune donnée sensible."""
    return JSONResponse(get_network_status())


@router.get("/qr/billet/check_primal")
async def check_billet_primal(g1pub: str = Query(...)):
    """Contrôle public, en lecture seule, de la règle primale unique (cf.
    _destination_primal_ok) — PAS d'authentification : interrogé par /scan
    dès que le portefeuille de destination est saisi/scanné (étape ③), pour
    refuser immédiatement un portefeuille non reconnu par UPlanet AVANT la
    tentative d'encaissement elle-même (qui revérifie de toute façon la
    même règle côté serveur, cf. /qr/billet/redeem — ce endpoint n'est qu'un
    confort d'UX pour échouer tôt plutôt qu'à la toute dernière étape)."""
    if not is_safe_g1pub(g1pub):
        raise HTTPException(status_code=400, detail="g1pub invalide")
    _, uplanet_g1_pub = await _resolve_uplanet_g1()
    ok = await _destination_primal_ok(g1pub, uplanet_g1_pub)
    return JSONResponse({"ok": ok})


@router.post("/qr/billet/redeem")
async def redeem_billet(request: Request):
    """Encaisse un Ğ1Billet : reçoit le seed reconstruit côté client (2 parts
    Shamir sur 3 réunies — cf. billet_redeem.html / le nouveau /scan) et un
    G1PUB de destination. Pas d'authentification requise : le billet est un
    instrument au porteur — qui réunit 2 des 3 parts a déjà, de fait, le
    pouvoir de le dépenser (même modèle de confiance qu'un billet de banque
    physique). Le seed ne transite qu'une fois, en HTTPS, jamais journalisé
    ni persisté — même discipline /dev/shm que billet_gen.sh.

    DEUX virements chaînés — jamais un DRAIN direct vers la destination —
    pour respecter la même règle primale que le financement (cf.
    /qr/billet/fund et le commentaire au-dessus de _BILLET_PRIMO_G1) :
      1. DRAIN total (PAYforSURE.sh, vide même le dépôt existentiel) du
         billet vers UPLANETNAME_G1 — ferme la boucle : UPLANETNAME_G1
         récupère à la fois son dépôt d'activation (1 Ğ1) ET la valeur Ẑen
         déposée par le MULTIPASS émetteur lors du financement.
      2. UPLANETNAME_G1 renvoie (solde drainé − 1 Ğ1) vers la destination
         fournie. Ce "− 1 Ğ1" n'est PAS une commission prélevée au porteur :
         c'est exactement le dépôt d'activation que UPLANETNAME_G1 avait
         lui-même avancé à l'étape 1 du financement — il le récupère ici,
         bouclant son propre bilan à zéro sur le cycle complet du billet
         (financement → encaissement). Le porteur reçoit l'intégralité du
         montant annoncé sur le billet, ni plus ni moins.
    Si l'étape 2 échoue après que l'étape 1 a réussi, les fonds ne sont PAS
    perdus : ils restent sur UPLANETNAME_G1 (portefeuille coopératif connu
    et persistant), pas sur la clé jetable du billet qui vient d'être vidée —
    un nouvel essai ou un envoi manuel reste possible.

    La destination est vérifiée AVANT tout virement (règle primale unique,
    cf. _destination_primal_ok) : si elle échoue, le billet n'est PAS touché
    (P1+P2 restent valides, un nouvel essai avec une autre destination reste
    possible) — plutôt que de drainer d'abord et découvrir le problème
    ensuite (ce qui bloquerait les fonds sur UPLANETNAME_G1 sans recours
    pour le porteur)."""
    data = await request.json()
    seed_hex = str(data.get("seed_hex", "")).strip().lower()
    dest_g1pub = str(data.get("dest_g1pub", "")).strip()

    if not re.fullmatch(r"[0-9a-f]{64}", seed_hex):
        raise HTTPException(status_code=400, detail="seed_hex invalide")
    if not is_safe_g1pub(dest_g1pub):
        raise HTTPException(status_code=400, detail="dest_g1pub invalide")

    uplanet_g1_dunikey, uplanet_g1_pub = await _resolve_uplanet_g1()

    if not await _destination_primal_ok(dest_g1pub, uplanet_g1_pub):
        raise HTTPException(
            status_code=403,
            detail="Ce portefeuille de destination n'est pas reconnu par UPlanet (primal incorrect) — le billet n'a pas été touché.",
        )

    shm_dir = "/dev/shm" if os.path.isdir("/dev/shm") else None
    cred_path = tempfile.mktemp(dir=shm_dir)
    dunikey_path = tempfile.mktemp(dir=shm_dir)
    try:
        with open(cred_path, "w") as f:
            f.write(seed_hex + "\n")
        os.chmod(cred_path, 0o600)

        proc = await asyncio.create_subprocess_exec(
            str(settings.TOOLS_PATH / "keygen"), "-t", "duniter", "-o", dunikey_path, "-i", cred_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0 or not os.path.isfile(dunikey_path):
            logger.error("redeem_billet: keygen échoué — %s", stderr.decode(errors="replace")[:500])
            raise HTTPException(status_code=500, detail="Dérivation de clé échouée")

        billet_g1pub = _read_dunikey_pubkey(dunikey_path)
        if not billet_g1pub:
            raise HTTPException(status_code=500, detail="Lecture du G1PUB du billet impossible")

        bal = await get_g1_balance_native(billet_g1pub)
        total_g1 = (bal.get("balances", {}).get("total", 0) or 0) / 100
        amount_to_forward = total_g1 - _BILLET_PRIMO_G1
        if amount_to_forward <= _BILLET_FUND_EPS:
            raise HTTPException(
                status_code=400,
                detail=f"Billet insuffisant pour être encaissé (solde {total_g1:.2f} Ğ1 ≤ dépôt d'activation)",
            )

        ok1, log1 = await _run_payforsure(dunikey_path, "DRAIN", uplanet_g1_pub, "UPLANET:BILLET:REDEEM:DRAIN")
        if not ok1:
            logger.error("redeem_billet: DRAIN échoué — %s", log1)
            return JSONResponse({"ok": False, "detail": "Le vidage du billet a échoué.", "log": log1}, status_code=502)

        ok2, log2 = await _run_payforsure(
            str(uplanet_g1_dunikey), f"{amount_to_forward:.4f}", dest_g1pub, "UPLANET:BILLET:REDEEM:FORWARD",
        )
        if not ok2:
            logger.error("redeem_billet: FORWARD échoué — %s", log2)
            return JSONResponse({
                "ok": False,
                "detail": "Le billet a été vidé mais le transfert final a échoué — les fonds sont saufs sur le portefeuille coopératif, contactez le support.",
                "log": log1 + "\n" + log2,
            }, status_code=502)

        return JSONResponse({"ok": True, "amount": round(amount_to_forward, 4), "log": log1 + "\n" + log2})
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Délai dépassé lors du virement")
    finally:
        seed_hex = None
        for p in (cred_path, dunikey_path):
            try:
                os.remove(p)
            except OSError:
                pass
