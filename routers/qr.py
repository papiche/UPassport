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
    → Planche A4 paysage de 6 Ğ1Billets papier imprimables (2 colonnes ×
      3 lignes) — remplace l'ancien moteur graphique G1BILLET (dépôt
      externe fermé). Chaque billet a sa propre clé G1/duniter JETABLE
      (phrase mnémonique BIP39, cf. `billet_gen.sh` — dérivation identique à
      `keygen.html`), jamais persistée
      sur disque : le secret n'existe que dans la réponse HTTP, à imprimer
      puis oublier. RECTO SEUL : le secret est imprimé en bande verticale
      sur le bord gauche de chaque billet — à replier vers l'arrière et
      scotcher avant distribution. Portefeuilles TOUJOURS vierges à la
      génération (pas de débit automatique — mode `auto` retiré le
      2026-09-24, peu fiable en pratique) : financement manuel après coup,
      par qui veut, vers les G1PUB imprimés.
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
import asyncio
import json
import logging
import tempfile
import shutil
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

from fastapi import APIRouter, Request, BackgroundTasks, Query, HTTPException
from fastapi.responses import Response, JSONResponse, HTMLResponse, RedirectResponse

from core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

_TEMPLATE = Path(__file__).parent.parent / "templates" / "qr.html"
_TEMPLATE_POSTCARD = Path(__file__).parent.parent / "templates" / "qr_postcard.html"
_BILLET_SCRIPT = Path(__file__).parent.parent / "billet_gen.sh"


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
  .warn{max-width:277mm;background:#fff3f3;border:1px solid var(--red);color:var(--red);
        padding:8px 12px;font-family:sans-serif;font-size:.8rem;border-radius:6px}

  .sheet{width:277mm;display:grid;grid-template-columns:repeat(2,136mm);
         grid-auto-rows:60mm;gap:3mm;background:#fff;padding:2mm;
         box-shadow:0 0 16px rgba(0,0,0,.35)}
  .cell{display:flex;height:60mm;border:1px dashed #999;overflow:hidden;
        position:relative;background:#fff}

  /* Bande secrète : bloc horizontal qui wrap normalement (largeur = hauteur
     de la cellule) puis pivoté à -90deg — plus fiable qu'un writing-mode
     vertical (dont le dimensionnement intrinsèque échappe à la grille/flex
     et déborde de la cellule). position:absolute retire le texte du flux :
     sa longueur ne peut plus influencer la taille de .fold ni de la ligne. */
  .cell .fold{width:15mm;flex:0 0 15mm;height:100%;position:relative;overflow:hidden;
              border-right:1px dashed var(--red);
              background:repeating-linear-gradient(45deg,#fffaf0,#fffaf0 2mm,#f2dcdc 2mm,#f2dcdc 4mm)}
  .cell .fold .secret{position:absolute;top:50%;left:50%;width:56mm;
              transform:translate(-50%,-50%) rotate(-90deg);transform-origin:center center;
              font-family:monospace;font-size:6pt;line-height:1.3;letter-spacing:.1px;
              text-align:center;color:#333;word-break:break-word;
              background:#fffaf0;padding:1.5mm 2mm;border-radius:1mm}
  .cell .fold .secret b{color:var(--red);font-weight:700}

  .cell .body{flex:1;min-width:0;position:relative;padding:3mm;display:flex;
              flex-direction:column;gap:1.5mm;background-color:var(--tint,var(--paper));
              background-position:center;background-size:cover;background-repeat:no-repeat}
  .cell .body.has-fond::before{content:'';position:absolute;inset:0;
              background:var(--tint-overlay,rgba(245,240,232,.74))}
  .cell .body>*{position:relative}
  .cell .logo-mark{position:absolute;bottom:2mm;right:2mm;width:16mm;height:16mm;
              border-radius:50%;object-fit:cover;border:1px solid var(--gold);
              box-shadow:0 1px 3px rgba(0,0,0,.35)}
  .cell .brand{font-size:7.5pt;letter-spacing:1.5px;color:var(--green);
              text-transform:uppercase;font-weight:700}

  /* Rangée principale : montant + QR solde à gauche (façon billet de
     banque — gros chiffre sur fond blanc, unité en petites capitales
     dessous, QR de vérification du solde juste en dessous), QR profil
     NOSTR à droite. */
  .cell .main-row{display:flex;align-items:flex-start;justify-content:space-between;
              gap:2.5mm;margin-top:1mm}
  .cell .amount-col{display:flex;flex-direction:column;align-items:center;
              gap:1.5mm;flex-shrink:0}
  .cell .amount-badge{display:inline-flex;flex-direction:column;align-items:center;
              line-height:1;background:#fff;color:var(--red);font-weight:800;
              padding:2mm 4mm;border-radius:2mm;border:1.5px solid var(--gold);
              box-shadow:0 1px 3px rgba(0,0,0,.35)}
  .cell .amount-badge .num{font-size:26pt;min-width:14mm;min-height:1em;
              display:inline-block;text-align:center}
  .cell .amount-badge .num.blank{min-width:20mm;border-bottom:1.5px dashed var(--gold)}
  .cell .amount-badge .unit{font-size:7.5pt;font-weight:700;color:var(--ink);
              letter-spacing:1px;text-transform:uppercase;margin-top:.5mm}
  .cell .qr-col{flex-shrink:0;display:flex}
  .cell .qr-col img,.cell .amount-col img{width:24mm;height:24mm;image-rendering:pixelated;
              background:#fff;padding:1mm;border-radius:1mm;border:1px solid var(--gold)}

  .cell .g1pub{font-family:monospace;font-size:5pt;color:#555;word-break:break-all;
              margin-top:auto}
  .cell .expiry{font-family:sans-serif;font-size:4.6pt;color:#777}
  .cell .status{font-family:sans-serif;font-size:5.5pt;color:var(--green);font-weight:700}
  .cell .status.err{color:var(--red)}

  /* Verso optionnel — RÉPÉTÉ dans une grille identique à la planche recto
     (même largeur de colonne/hauteur de ligne/espacement) : une fois la
     feuille imprimée en duplex et découpée, le même texte se retrouve au
     dos de CHAQUE billet, quel que soit le sens du retournement duplex
     (contenu identique dans les 6 cellules → l'alignement exact importe peu).
     Texte horizontal (la rotation -90° testée était moins lisible), pas de
     fond coloré derrière le texte. */
  .vcell{display:flex;flex-direction:column;align-items:center;justify-content:center;
              height:60mm;border:1px dashed #999;overflow:hidden;position:relative;
              background:#fff;padding:3mm 11mm;text-align:center;gap:1.5mm}
  .vcell .vstamp{font-family:Georgia,serif;font-weight:800;font-size:8pt;
              color:var(--green);letter-spacing:1.5px;text-transform:uppercase}
  .vcell .vtext{font-family:Georgia,serif;font-size:6.3pt;line-height:1.45;
              color:#444;max-width:112mm}
  .vcell .vsupport{display:flex;align-items:flex-start;justify-content:center;gap:7mm;margin-top:.5mm}
  .vcell .vsupport-item{display:flex;flex-direction:column;align-items:center;gap:1mm}
  .vcell .vsupport-item img{width:14mm;height:14mm;image-rendering:pixelated;background:#fff;
              padding:.6mm;border-radius:.8mm;border:1px solid var(--gold)}
  .vcell .vsupport-item span{font-family:monospace;font-size:5.3pt;color:#777;
              max-width:34mm;line-height:1.35;display:inline-block}

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
    @page{size:A4 landscape;margin:8mm}
  }
</style>
</head>
<body>

<div class="warn no-print">
  🔒 Chaque billet n'a qu'un recto. Le secret (phrase mnémonique) est imprimé en <b>vertical sur le
  bord gauche</b> de chaque billet : repliez cette bande vers l'arrière le long du trait pointillé
  et scotchez-la avant de distribuer les billets. Cette page ne pourra pas être régénérée à
  l'identique : imprimez avant de fermer cet onglet.
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
    "Le Ẑen n'est pas une monnaie spéculative : c'est l'unité de compte "
    "d'un bien commun, celui du laboratoire Web3 Made In Zion. Chaque "
    "Astroport installé, chaque ressource inscrite au cadastre commun, y "
    "est mesurée et gouvernée collectivement via les toiles de confiance "
    "UPlanet. Ce billet est un fragment d'un chantier ouvert — rejoignez-le. "
    "Aucune invention n'y est brevetée : code, documentation et recettes "
    "restent ouverts (AGPL-3.0 / CC BY / CC BY-SA) — la Charte Open Source "
    "est le socle de toute la coopérative."
)

_BILLET_VERSO_CELL = """<div class="vcell">
  <div class="vstamp">☀️ Banque Solarpunk</div>
  <div class="vtext">__TEXT__</div>
  <div class="vsupport">
    <div class="vsupport-item"><img src="__OC_QR__" alt="QR OpenCollective"><span>Vos € deviennent vos Ẑen MULTIPASS (OPEX)<br>et vos ẐEN ZenCard (CAPEX) · opencollective.com/monnaie-libre</span></div>
    <div class="vsupport-item"><img src="__ZELKOVA_QR__" alt="QR Zelkova"><span>z.astroport.one</span></div>
  </div>
</div>"""


def _render_billet_verso_html(text: str, qr_url: str, zelkova_qr_url: str) -> str:
    """Grille verso optionnelle — MÊME grille (colonnes/lignes/espacement)
    que la planche recto, texte "contrat" identique répété dans les 6
    cellules : une fois imprimée en duplex et découpée, chaque billet porte
    ce texte au dos, quel que soit le sens du retournement duplex. Texte
    horizontal (une bande tournée -90° a été testée puis abandonnée : moins
    lisible). Deux QR de soutien : OpenCollective (financer le G1FabLab) et
    Zelkova (le wallet Ẑen — z.astroport.one)."""
    esc = html_lib.escape
    text_html = esc(text).replace("\n\n", "<br><br>").replace("\n", "<br>")
    cell = (
        _BILLET_VERSO_CELL
        .replace("__TEXT__", text_html)
        .replace("__OC_QR__", qr_url)
        .replace("__ZELKOVA_QR__", zelkova_qr_url)
    )
    cells = "\n".join([cell] * _BILLET_A4_COUNT)
    return f'<div class="sheet verso-sheet">\n{cells}\n</div>'

_BILLET_A4_CELL = """<div class="cell">
  <div class="fold"><div class="secret">__SECRET__</div></div>
  <div class="body __FOND_CLASS__" style="background-image:__FOND_CSS__;--tint:__TINT_SOLID__;--tint-overlay:__TINT_OVERLAY__">
    __LOGO_IMG__
    <div class="brand">Ğ1BILLET</div>
    <div class="main-row">
      <div class="amount-col">
        <div class="amount-badge">
          <span class="num __AMOUNT_CLASS__">__AMOUNT_TEXT__</span><span class="unit">__UNIT_LABEL__</span>
        </div>
        <img src="__PUB_QR__" alt="QR solde">
      </div>
      <div class="qr-col">
        <img src="__PROFILE_QR__" alt="QR profil">
      </div>
    </div>
    <div class="g1pub">__G1PUB__</div>
    <div class="expiry">__EXPIRES__</div>
    <div class="status __STATUS_CLASS__">__STATUS__</div>
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


_BILLET_FONT_BOLD = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"


def _billet_kind0_images(amount: float, unit_label: str) -> tuple[str, str]:
    """Génère l'icône (picture) et la bannière (banner) du profil NOSTR kind 0
    du billet — même teinte par coupure que la planche recto (_BILLET_TINTS),
    montant mis en évidence. Deux data: URI PNG, rien n'est jamais écrit sur
    disque (même discipline que le reste de la génération du billet)."""
    hexcolor = _BILLET_TINTS.get(amount, "#c8a83c")
    bg = tuple(int(hexcolor[i:i + 2], 16) for i in (1, 3, 5))
    is_blank = amount <= 0
    amount_text = "VIERGE" if is_blank else f"{amount:g}"

    def _font(size: int) -> ImageFont.FreeTypeFont:
        try:
            return ImageFont.truetype(_BILLET_FONT_BOLD, size)
        except Exception:
            return ImageFont.load_default()

    def _centered(draw: ImageDraw.ImageDraw, text: str, font, width: int, y: int, fill: str) -> None:
        bbox = draw.textbbox((0, 0), text, font=font)
        x = (width - (bbox[2] - bbox[0])) / 2 - bbox[0]
        draw.text((x, y), text, fill=fill, font=font)

    def _png_data_uri(img: Image.Image) -> str:
        buf = BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    # Icône (picture) — carrée, montant en gros, façon badge du recto.
    icon = Image.new("RGB", (256, 256), bg)
    idraw = ImageDraw.Draw(icon)
    _centered(idraw, amount_text, _font(30 if is_blank else 90), 256, 84, "#8b1a1a")
    if not is_blank:
        _centered(idraw, unit_label, _font(24), 256, 168, "#1a1a1a")

    # Bannière (banner) — large, brand + montant.
    banner = Image.new("RGB", (600, 200), bg)
    bdraw = ImageDraw.Draw(banner)
    bdraw.text((24, 24), "Ğ1BILLET", fill="#2d5a1b", font=_font(34))
    amount_banner = amount_text if is_blank else f"{amount_text} {unit_label}"
    bbox = bdraw.textbbox((0, 0), amount_banner, font=_font(58))
    bdraw.text((600 - (bbox[2] - bbox[0]) - 24 - bbox[0], 200 - (bbox[3] - bbox[1]) - 30 - bbox[1]),
               amount_banner, fill="#8b1a1a", font=_font(58))

    return _png_data_uri(icon), _png_data_uri(banner)


def _render_billet_a4_html(
    amount: float,
    cells: list[dict],
    unit_label: str = "Ẑen",
    fond_url: Optional[str] = None,
    logo_url: Optional[str] = None,
    verso_html: str = "",
) -> str:
    """Compose la planche A4 paysage — 6 billets recto seul (2 colonnes × 3
    lignes), secret en vertical sur le bord gauche (à replier/scotcher)."""
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
        cell_blocks.append(
            _BILLET_A4_CELL
            .replace("__SECRET__", f"<b>MNEMONIC</b><br>{esc(c['mnemonic'])}")
            .replace("__FOND_CSS__", fond_css)
            .replace("__FOND_CLASS__", fond_class)
            .replace("__TINT_SOLID__", tint_solid)
            .replace("__TINT_OVERLAY__", tint_overlay)
            .replace("__LOGO_IMG__", logo_img)
            .replace("__AMOUNT_CLASS__", amount_class)
            .replace("__AMOUNT_TEXT__", amount_text)
            .replace("__UNIT_LABEL__", esc(unit_label))
            .replace("__PUB_QR__", c["pub_qr_url"])
            .replace("__PROFILE_QR__", c.get("profile_qr_url", ""))
            .replace("__G1PUB__", esc(c["g1pub"]))
            .replace("__EXPIRES__", (f"Valide jusqu'au {esc(c['expires_str'])} · {esc(c['npub'][:16])}…"
                                      if c.get("expires_str") else ""))
            .replace("__STATUS__", status)
            .replace("__STATUS_CLASS__", status_class)
        )

    verso_button = '<button onclick="printSide(\'verso\')">🖨️ Imprimer le Verso</button>' if verso_html else ""
    return (
        _BILLET_A4_PAGE
        .replace("__COUNT__", str(len(cells)))
        .replace("__CELLS__", "\n".join(cell_blocks))
        .replace("__VERSO__", verso_html)
        .replace("__VERSO_BUTTON__", verso_button)
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
    journalisé — cf. billet_gen.sh). Retourne {"g1pub","mnemonic"} — mnemonic
    est une phrase BIP39 standard (12 mots), dérivation identique à
    UPlanet/earth/keygen.html (onglet "Mnemonic (v2)")."""
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



_BILLET_EXPIRY_DAYS = 90


async def _publish_billet_emission(
    nsec: str, npub: str, g1pub: str, amount: float, unit_label: str,
) -> Optional[int]:
    """Publie un profil NOSTR (kind 0) AU NOM DU BILLET LUI-MÊME — signé par
    le compte NOSTR jumeau dérivé de la MÊME phrase mnémonique que le
    portefeuille G1 (billet_gen.sh). Quiconque connaît la phrase peut
    reproduire cette signature ; personne d'autre ne le peut — même garantie
    que pour les fonds, sans tiers de confiance additionnel.

    `picture` (icône) et `banner` sont générées à la volée (_billet_kind0_images,
    Pillow, même teinte par coupure que le recto — _BILLET_TINTS) et embarquées
    en `data:image/png;base64,...` directement dans le contenu de l'event —
    jamais écrites sur disque ni hébergées ailleurs.

    Attestation PUBLIQUE et INFORMATIVE uniquement : `["expiration", …]`
    (NIP-40) donne une date de fin vérifiable par n'importe quel client
    NOSTR/relay, mais ne verrouille aucune dépense — les fonds restent
    entièrement sur la blockchain Ğ1. Best-effort : une panne du relay ne
    doit jamais faire échouer la génération du billet.

    Retourne le timestamp Unix d'expiration si la publication a réussi,
    sinon None (le billet reste valide, juste sans attestation publique).
    """
    expires_ts = int(time.time()) + _BILLET_EXPIRY_DAYS * 86400
    expires_str = datetime.fromtimestamp(expires_ts).strftime("%d/%m/%Y")
    picture_uri, banner_uri = _billet_kind0_images(amount, unit_label)
    content = json.dumps({
        "name": f"Ğ1Billet · {amount:g} {unit_label}",
        "about": f"Ğ1Billet papier — G1PUB {g1pub} — valable jusqu'au {expires_str}",
        "picture": picture_uri,
        "banner": banner_uri,
    })
    tags = json.dumps([["expiration", str(expires_ts)], ["t", "g1billet"]])

    shm = "/dev/shm" if os.path.isdir("/dev/shm") else None
    fd, keyfile_path = tempfile.mkstemp(dir=shm)
    try:
        os.write(fd, f"NSEC={nsec}; NPUB={npub};".encode())
        os.close(fd)
        os.chmod(keyfile_path, 0o600)
        proc = await asyncio.create_subprocess_exec(
            "python3", str(settings.TOOLS_PATH / "nostr_send_note.py"),
            "--keyfile", keyfile_path, "--content", content,
            "--kind", "0", "--tags", tags, "--relays", settings.myRELAY,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            logger.warning("billet emission NOSTR — échec pour %s: %s", npub[:16], stderr.decode()[:200])
            return None
        return expires_ts
    except Exception as e:
        logger.warning("billet emission NOSTR — erreur: %s", e)
        return None
    finally:
        try:
            os.remove(keyfile_path)
        except OSError:
            pass


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
):
    """Planche A4 paysage de 6 Ğ1Billets papier imprimables — remplace le
    moteur graphique G1BILLET. RECTO SEUL : 6 clés jetables indépendantes,
    secret en vertical sur le bord gauche de chaque billet (à replier vers
    l'arrière et scotcher). Portefeuilles TOUJOURS vierges à la génération —
    pas de débit automatique depuis un MULTIPASS (mode `auto` retiré :
    peu fiable en pratique, cf. échecs constatés en production le 2026-09-24).
    Financement manuel après coup, par qui veut, comme pour n'importe quel
    portefeuille Ğ1.

    unit : zen|g1 (défaut zen) — unité du montant annoncé (affichage seul,
    aucun transfert n'est déclenché par cet endpoint).
    1 Ẑen = 0.1 Ğ1 (convention UPlanet ORIGIN).

    verso : "1" pour ajouter une page verso à l'impression, RÉPÉTÉE dans une
    grille identique à la planche recto (même 2×3, mêmes dimensions de
    cellule) — après découpe, chaque billet porte ce texte au dos. Texte
    "contrat" court (~550 car. max, imprimé petit, horizontal) + QR de soutien
    OpenCollective. `verso_text` (optionnel) remplace le texte par défaut.
    """
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
        mnemonic = keydata["mnemonic"]
        nsec, npub = keydata["nsec"], keydata["npub"]
        keydata = None

        expires_ts = await _publish_billet_emission(nsec, npub, g1pub, amount, unit_label)
        nsec = None
        expires_str = datetime.fromtimestamp(expires_ts).strftime("%d/%m/%Y") if expires_ts else ""

        pub_png, _ = await asyncio.to_thread(_generate_qr_png, g1pub, 3, "M")
        pub_qr_url = ("data:image/png;base64," + base64.b64encode(pub_png).decode()) if pub_png else ""

        # QR "profil" = le npub jumeau brut (PAS un lien web) : ce QR est lu
        # par des wallets NOSTR (Zelkova...) qui attendent une clé, pas une URL.
        profile_png, _ = await asyncio.to_thread(_generate_qr_png, npub, 4, "M")
        profile_qr_url = ("data:image/png;base64," + base64.b64encode(profile_png).decode()) if profile_png else ""

        cells.append({
            "g1pub": g1pub, "mnemonic": mnemonic, "npub": npub, "expires_str": expires_str,
            "pub_qr_url": pub_qr_url, "profile_qr_url": profile_qr_url,
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
        verso_html = _render_billet_verso_html(text=text, qr_url=oc_qr_url, zelkova_qr_url=zelkova_qr_url)

    page = _render_billet_a4_html(
        amount=amount, cells=cells, unit_label=unit_label, fond_url=fond_url, logo_url=logo_url,
        verso_html=verso_html,
    )
    cells = None
    return HTMLResponse(page)
