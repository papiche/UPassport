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
"""

import base64
import os
import asyncio
import logging
import tempfile
import shutil
import urllib.parse
from pathlib import Path
from typing import Optional

import httpx

from fastapi import APIRouter, Request, BackgroundTasks, Query
from fastapi.responses import Response, JSONResponse, HTMLResponse, RedirectResponse

from core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


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
) -> bytes | None:
    level = level.upper() if level.upper() in ("L", "M", "Q", "H") else "H"
    version = max(1, min(40, int(version)))

    # amzqr — auto-monte la version si les données débordent
    try:
        import amzqr as _amzqr
        for v in range(version, 41):
            tmp = tempfile.mkdtemp()
            out = os.path.join(tmp, "qr.png")
            try:
                _amzqr.run(
                    data,
                    version=v,
                    level=level,
                    picture=picture_path,
                    colorized=colorized and bool(picture_path),
                    contrast=float(contrast),
                    brightness=float(brightness),
                    save_name="qr.png",
                    save_dir=tmp,
                    verbose=False,
                )
                if os.path.isfile(out):
                    result = Path(out).read_bytes()
                    shutil.rmtree(tmp, ignore_errors=True)
                    return result
            except Exception as e:
                shutil.rmtree(tmp, ignore_errors=True)
                if "overflow" in str(e).lower() or "too long" in str(e).lower():
                    continue
                logger.debug("amzqr v%s: %s", v, e)
                break
            shutil.rmtree(tmp, ignore_errors=True)
            break
    except ImportError:
        logger.debug("amzqr not installed")

    # qrencode fallback

    try:
        import subprocess
        tmp2 = tempfile.mktemp(suffix=".png")
        subprocess.run(
            [
                "qrencode", "-s", "8", "-t", "PNG",
                "-l", level,
                "--foreground", color.upper().lstrip("#"),
                "--background", bgcolor.upper().lstrip("#"),
                "-o", tmp2, "--", data,
            ],
            check=True, capture_output=True,
        )
        png = Path(tmp2).read_bytes()
        os.unlink(tmp2)
        return png
    except Exception as e:
        logger.debug("qrencode: %s", e)

    return None

async def _download_picture_url(url: str) -> Optional[str]:
    """Télécharge une image depuis une URL, renvoie le chemin du fichier temporaire."""
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            ct = r.headers.get("content-type", "image/png")
            ext = ".jpg" if "jpeg" in ct else ".png" if "png" in ct else ".gif" if "gif" in ct else ".png"
            tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
            tmp.write(r.content)
            tmp.close()
            return tmp.name
    except Exception as e:
        logger.debug("picture_url download failed: %s", e)
        return None


# ── Interface HTML ─────────────────────────────────────────────────────────────

_QR_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>QR Generator — UPlanet</title>
<style>
:root{--bg:#090a0f;--panel:rgba(12,10,20,.97);--pink:#ff3399;--yellow:#ffd700;--accent:#00ffcc;--text:#e0e2e6}
*{box-sizing:border-box;margin:0;padding:0}
body{background:radial-gradient(circle at 25% 40%,#2a0a18 0%,var(--bg) 70%);color:var(--text);font-family:'Segoe UI',monospace;min-height:100vh;display:flex;flex-direction:column}
header{background:rgba(0,0,0,.7);border-bottom:2px solid var(--pink);padding:14px 28px;display:flex;align-items:center;gap:14px;box-shadow:0 0 20px rgba(255,51,153,.35)}
header h1{color:var(--pink);font-size:1.4rem;text-transform:uppercase;text-shadow:0 0 10px var(--pink);letter-spacing:2px}
header span{color:var(--accent);font-family:monospace;font-size:.82rem;opacity:.8}
.main{display:flex;flex:1}
.cfg{width:370px;min-width:300px;background:var(--panel);border-right:1px solid rgba(255,51,153,.2);padding:20px;overflow-y:auto;display:flex;flex-direction:column;gap:15px}
.prv{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:36px;gap:20px}
.sec{color:var(--pink);font-family:monospace;font-size:.7rem;text-transform:uppercase;letter-spacing:2px;padding-bottom:6px;border-bottom:1px solid rgba(255,51,153,.2)}
.fld{display:flex;flex-direction:column;gap:5px}
label.lbl{color:var(--accent);font-family:monospace;font-size:.72rem;text-transform:uppercase;letter-spacing:1px}
textarea,input[type=text],select{background:rgba(0,0,0,.5);color:var(--text);border:1px solid rgba(255,51,153,.25);border-radius:5px;padding:8px 11px;font-family:monospace;font-size:.9rem;transition:border .2s;width:100%}
textarea{resize:vertical;min-height:68px}
textarea:focus,input[type=text]:focus{outline:none;border-color:var(--pink);box-shadow:0 0 8px rgba(255,51,153,.3)}
.radios{display:flex;gap:5px}
.radios input{display:none}
.radios label{flex:1;text-align:center;padding:7px 2px;border:1px solid rgba(255,51,153,.25);border-radius:5px;cursor:pointer;font-size:.82rem;transition:.2s;color:var(--text)}
.radios input:checked+label{background:rgba(255,51,153,.18);border-color:var(--pink);color:var(--pink);box-shadow:0 0 8px rgba(255,51,153,.25)}
.srow{display:flex;align-items:center;gap:7px}
input[type=range]{flex:1;accent-color:var(--pink);border:none;padding:0;background:none;height:4px}
.sv{color:var(--yellow);font-family:monospace;min-width:38px;text-align:right;font-size:.85rem}
.trow{display:flex;align-items:center;justify-content:space-between}
.tog{position:relative;width:42px;height:22px;flex-shrink:0}
.tog input{opacity:0;width:0;height:0;position:absolute}
.ts{position:absolute;inset:0;background:rgba(0,0,0,.4);border-radius:22px;border:1px solid rgba(255,51,153,.25);cursor:pointer;transition:.3s}
.ts:before{content:'';position:absolute;width:16px;height:16px;left:2px;top:2px;background:#555;border-radius:50%;transition:.3s}
.tog input:checked+.ts{background:rgba(255,51,153,.18);border-color:var(--pink)}
.tog input:checked+.ts:before{transform:translateX(20px);background:var(--pink);box-shadow:0 0 8px var(--pink)}
.drop{border:2px dashed rgba(0,255,204,.25);border-radius:8px;padding:14px;text-align:center;cursor:pointer;transition:.2s;color:#666;font-size:.8rem;position:relative}
.drop:hover,.drop.over{border-color:var(--accent);color:var(--accent);background:rgba(0,255,204,.04)}
.drop input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.drop.has{border-color:var(--yellow);color:var(--yellow)}
.crow{display:flex;align-items:center;gap:8px}
input[type=color]{width:36px;height:32px;border:none;border-radius:4px;cursor:pointer;padding:1px;background:rgba(0,0,0,.3)}
.hint{color:#555;font-size:.68rem;font-family:monospace;margin-top:1px}
.box{background:#fff;padding:14px;border-radius:10px;box-shadow:0 0 40px rgba(255,51,153,.4);transition:opacity .3s;min-width:180px;min-height:180px;display:flex;align-items:center;justify-content:center}
.box.spin{opacity:.45}
.box img{max-width:300px;max-height:300px;display:block}
.ubadge{color:var(--accent);font-family:monospace;font-size:.74rem;background:rgba(0,0,0,.5);padding:7px 14px;border-radius:20px;border:1px solid rgba(0,255,204,.2);max-width:500px;word-break:break-all;text-align:center}
#st{font-family:monospace;font-size:.8rem;min-height:18px;text-align:center}
#st.ok{color:var(--accent)}#st.er{color:var(--pink)}
.btns{display:flex;gap:10px;flex-wrap:wrap;justify-content:center}
.btn{padding:11px 22px;border-radius:7px;font-weight:700;cursor:pointer;text-transform:uppercase;font-size:.85rem;border:none;transition:.2s}
.bdl{background:var(--yellow);color:#000;box-shadow:0 0 14px rgba(255,215,0,.4)}
.bdl:hover{box-shadow:0 0 24px rgba(255,215,0,.7);transform:translateY(-1px)}
.bcp{background:transparent;color:var(--accent);border:2px solid var(--accent)}
.bcp:hover{background:rgba(0,255,204,.1);box-shadow:0 0 12px rgba(0,255,204,.3)}
.api-url{background:rgba(0,0,0,.4);border:1px solid rgba(255,51,153,.15);border-radius:6px;padding:8px 10px;font-family:monospace;font-size:.68rem;color:#888;word-break:break-all;cursor:text;user-select:all;margin-top:4px}
@media(max-width:680px){.main{flex-direction:column}.cfg{width:100%;border-right:none;border-bottom:1px solid rgba(255,51,153,.2)}}
</style>
</head>
<body>
<header>
  <h1>🔲 QR Generator</h1>
  <span>// UPlanet — amzqr + qrencode</span>
</header>
<div class="main">
<div class="cfg">

  <div class="fld">
    <div class="sec">Données</div>
    <label class="lbl">URL / Texte</label>
    <textarea id="data" placeholder="https://qo-op.com">https://qo-op.com</textarea>
  </div>

  <div class="fld">
    <div class="sec">Correction d'erreur</div>
    <div class="radios">
      <input type="radio" name="lvl" id="lL" value="L"><label for="lL">L · 7%</label>
      <input type="radio" name="lvl" id="lM" value="M"><label for="lM">M · 15%</label>
      <input type="radio" name="lvl" id="lQ" value="Q"><label for="lQ">Q · 25%</label>
      <input type="radio" name="lvl" id="lH" value="H" checked><label for="lH">H · 30%</label>
    </div>
    <div class="hint">H = plus robuste aux dommages physiques, QR plus dense</div>
  </div>

  <div class="fld">
    <div class="sec">Version QR (complexité / capacité)</div>
    <label class="lbl">Version min. <span id="vv" style="color:var(--yellow)">1</span> → auto-ajustée si overflow</label>
    <div class="srow">
      <span style="color:#555;font-size:.72rem">1</span>
      <input type="range" id="ver" min="1" max="40" value="1">
      <span style="color:#555;font-size:.72rem">40</span>
      <span class="sv" id="vd">1</span>
    </div>
  </div>

  <div class="fld">
    <div class="sec">Image de fond — QR artistique (amzqr)</div>
    <!-- MULTIPASS connect -->
    <div id="mp-zone" style="border:1px solid rgba(255,51,153,.25);border-radius:7px;padding:10px;margin-bottom:8px">
      <div id="mp-disconnected">
        <button id="btn-mp" onclick="connectMultipass()" style="width:100%;padding:9px;background:rgba(255,51,153,.12);color:var(--pink);border:1px solid var(--pink);border-radius:6px;cursor:pointer;font-weight:700;font-size:.85rem;transition:.2s" onmouseover="this.style.background='rgba(255,51,153,.25)'" onmouseout="this.style.background='rgba(255,51,153,.12)'">
          🪪 Connecter mon MULTIPASS (NIP-07)
        </button>
        <div class="hint" style="margin-top:4px">Utilise l'extension Alby / nos2x pour charger votre avatar</div>
      </div>
      <div id="mp-connected" style="display:none;align-items:center;gap:10px">
        <img id="mp-avatar" src="" style="width:42px;height:42px;border-radius:50%;border:2px solid var(--pink);object-fit:cover">
        <div style="flex:1;min-width:0">
          <div id="mp-name" style="color:var(--pink);font-weight:700;font-size:.85rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">—</div>
          <div id="mp-npub" style="color:#555;font-family:monospace;font-size:.65rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">—</div>
        </div>
        <button onclick="useAvatarAsBg()" style="padding:6px 10px;background:rgba(0,255,204,.12);color:var(--accent);border:1px solid var(--accent);border-radius:5px;cursor:pointer;font-size:.75rem;font-weight:700;white-space:nowrap">
          Utiliser<br>l'avatar
        </button>
      </div>
    </div>
    <div class="drop" id="drop">
      <input type="file" id="pic" accept="image/*">
      <div id="dlbl">📎 Glisser une image ou cliquer<br><span style="opacity:.5;font-size:.7rem">PNG · JPG · GIF · WebP</span></div>
    </div>
    <div class="hint">Overlay artistique : l'image est encodée dans la trame QR</div>
  </div>

  <div class="fld">
    <div class="trow">
      <label style="color:var(--text);font-size:.9rem">🎨 Coloriser depuis l'image</label>
      <label class="tog"><input type="checkbox" id="col"><div class="ts"></div></label>
    </div>
    <div class="hint">Applique la palette de l'image aux modules du QR</div>
  </div>

  <div class="fld">
    <label class="lbl">Contraste</label>
    <div class="srow">
      <span style="color:#555;font-size:.72rem">0.1</span>
      <input type="range" id="ctr" min="0.1" max="3.0" step="0.05" value="1.0">
      <span style="color:#555;font-size:.72rem">3.0</span>
      <span class="sv" id="cd">1.00</span>
    </div>
    <label class="lbl" style="margin-top:6px">Luminosité</label>
    <div class="srow">
      <span style="color:#555;font-size:.72rem">0.1</span>
      <input type="range" id="bri" min="0.1" max="3.0" step="0.05" value="1.0">
      <span style="color:#555;font-size:.72rem">3.0</span>
      <span class="sv" id="bd">1.00</span>
    </div>
  </div>

  <div class="fld">
    <div class="sec">Couleurs modules (qrencode fallback)</div>
    <label class="lbl">Premier plan</label>
    <div class="crow">
      <input type="color" id="fgp" value="#000000">
      <input type="text" id="fg" value="000000" placeholder="000000" style="flex:1">
    </div>
    <label class="lbl" style="margin-top:6px">Fond</label>
    <div class="crow">
      <input type="color" id="bgp" value="#ffffff">
      <input type="text" id="bg" value="ffffff" placeholder="ffffff" style="flex:1">
    </div>
    <div class="hint">Ignoré par amzqr — actif uniquement si amzqr échoue</div>
  </div>

  <div class="fld">
    <div class="sec">URL API</div>
    <div class="api-url" id="apiurl">—</div>
  </div>

</div><!-- cfg -->

<div class="prv">
  <div class="box" id="box">
    <img id="qimg" src="" alt="QR" style="display:none">
    <span id="ph" style="color:#444;font-family:monospace;font-size:.85rem">⏳</span>
  </div>
  <div class="ubadge" id="ubadge">—</div>
  <div id="st"></div>
  <div class="btns">
    <button class="btn bdl" id="bdl">⬇ Télécharger PNG</button>
    <button class="btn bcp" id="bcp">📋 Copier URL API</button>
  </div>
</div>
</div>

<script>
const $=id=>document.getElementById(id);
let _tmr=null,_blob=null,_dataOut='',_file=null;

function sl(rng,disp,decimals){
  rng.addEventListener('input',()=>{
    disp.textContent=parseFloat(rng.value).toFixed(decimals);
    go();
  });
}
sl($('ver'),$('vd'),0); $('ver').addEventListener('input',()=>$('vv').textContent=$('ver').value);
sl($('ctr'),$('cd'),2); sl($('bri'),$('bd'),2);

function colorSync(picker,txt){
  picker.addEventListener('input',e=>{txt.value=e.target.value.slice(1);go()});
  txt.addEventListener('input',e=>{const h=e.target.value.replace(/[^0-9a-fA-F]/g,'').slice(0,6);if(h.length===6)picker.value='#'+h;go()});
}
colorSync($('fgp'),$('fg')); colorSync($('bgp'),$('bg'));

$('data').addEventListener('input',go);
document.querySelectorAll('input[name=lvl]').forEach(e=>e.addEventListener('change',go));
$('col').addEventListener('change',go);

const drop=$('drop'),pic=$('pic');
drop.addEventListener('dragover',e=>{e.preventDefault();drop.classList.add('over')});
drop.addEventListener('dragleave',()=>drop.classList.remove('over'));
drop.addEventListener('drop',e=>{e.preventDefault();drop.classList.remove('over');if(e.dataTransfer.files[0])setFile(e.dataTransfer.files[0])});
pic.addEventListener('change',()=>pic.files[0]&&setFile(pic.files[0]));
function setFile(f){
  _file=f; drop.classList.add('has');
  $('dlbl').innerHTML=`✅ <b>${f.name}</b><br><span style="opacity:.5;font-size:.7rem">cliquer pour changer</span>`;
  go();
}

function go(delay=500){clearTimeout(_tmr);st('…','');_tmr=setTimeout(run,delay)}
function st(msg,cls){const s=$('st');s.textContent=msg;s.className=cls}

function params(){
  return {
    data:$('data').value.trim(),
    version:$('ver').value,
    level:document.querySelector('input[name=lvl]:checked').value,
    colorized:$('col').checked?'1':'0',
    contrast:$('ctr').value,
    brightness:$('bri').value,
    color:$('fg').value.replace(/[^0-9a-fA-F]/g,'').slice(0,6)||'000000',
    bgcolor:$('bg').value.replace(/[^0-9a-fA-F]/g,'').slice(0,6)||'ffffff',
  };
}

async function run(){
  const p=params();
  if(!p.data){st('Entrez une URL ou du texte','er');return}
  $('box').classList.add('spin'); st('Génération…','');

  // mise à jour URL API
  const qs=new URLSearchParams({...p,format:'png'});
  $('apiurl').textContent=location.origin+'/qr?'+qs;

  let imgSrc=null;
  try{
    if(_file){
      const fd=new FormData();
      Object.entries(p).forEach(([k,v])=>fd.append(k,v));
      fd.append('format','json'); fd.append('picture',_file);
      const r=await fetch('/qr',{method:'POST',body:fd,signal:AbortSignal.timeout(12000)});
      if(r.ok){const j=await r.json();imgSrc=j.dataUrl||j.fallback||null;_dataOut=j.data||p.data}
    }else if(_avatarUrl){
      // Avatar MULTIPASS → backend télécharge l'URL (évite les restrictions CORS)
      const r=await fetch('/qr?'+new URLSearchParams({...p,picture_url:_avatarUrl,format:'json'}),{signal:AbortSignal.timeout(15000)});
      if(r.ok){const j=await r.json();imgSrc=j.dataUrl||j.fallback||null;_dataOut=j.data||p.data}
    }else{
      const r=await fetch('/qr?'+new URLSearchParams({...p,format:'json'}),{signal:AbortSignal.timeout(8000)});
      if(r.ok){const j=await r.json();imgSrc=j.dataUrl||j.fallback||null;_dataOut=j.data||p.data}
    }
  }catch(e){st('Erreur: '+e.message,'er');$('box').classList.remove('spin');return}

  $('box').classList.remove('spin');
  if(imgSrc){
    $('qimg').src=imgSrc; $('qimg').style.display='block'; $('ph').style.display='none';
    if(imgSrc.startsWith('data:')){
      const b=atob(imgSrc.split(',')[1]),a=new Uint8Array(b.length);
      for(let i=0;i<b.length;i++)a[i]=b.charCodeAt(i);
      _blob=new Blob([a],{type:'image/png'});
    }
    $('ubadge').textContent=_dataOut||p.data;
    st('✓ Généré','ok');
  }else{st('Échec de génération','er')}
}

$('bdl').addEventListener('click',()=>{
  if(_blob){const a=document.createElement('a');a.href=URL.createObjectURL(_blob);a.download='qr-uplanet.png';a.click();return}
  const qs=new URLSearchParams({...params(),format:'png'});
  window.open('/qr?'+qs,'_blank');
});
$('bcp').addEventListener('click',()=>{
  const qs=new URLSearchParams({...params(),format:'png'});
  navigator.clipboard.writeText(location.origin+'/qr?'+qs).then(()=>st('✓ URL copiée !','ok'));
});

// ── MULTIPASS / NIP-07 ────────────────────────────────────────────────────────
let _avatarUrl = null;

function hexToNpub(hex){
  const CHARS='qpzry9x8gf2tvdw0s3jn54khce6mua7l';
  const data=new Uint8Array(33);data[0]=0;
  for(let i=0;i<32;i++)data[i+1]=parseInt(hex.slice(i*2,i*2+2),16);
  function toWords(d){const r=[];let v=0,b=0;for(const x of d){v=(v<<8)|x;b+=8;while(b>=5){b-=5;r.push((v>>b)&31)}}if(b>0)r.push((v<<(5-b))&31);return r}
  const words=[0,...toWords(data)];
  function cksum(hrp,d){const G=[0x3b6a57b2,0x26508e6d,0x1ea119fa,0x3d4233dd,0x2a1462b3];let c=1;for(const ch of hrp){c=((c>>5)^0)^(ch.charCodeAt(0));for(let i=0;i<5;i++)c=((c>>1)^(G[i]&-(c&1)));}c^=1;for(const x of d){const q=c>>25;c=((c&0x1ffffff)<<5)^x;for(let i=0;i<5;i++)c^=(G[i]&-((q>>i)&1))}return c}
  const ck=cksum('npub',words.concat([0,0,0,0,0,0]))^1;
  const full=words.concat([(ck>>25)&31,(ck>>20)&31,(ck>>15)&31,(ck>>10)&31,(ck>>5)&31,ck&31]);
  return 'npub1'+full.map(x=>CHARS[x]).join('');
}

function getRelayUrl(){
  const h=location.hostname;
  if(h==='localhost'||h==='127.0.0.1')return 'ws://127.0.0.1:7777';
  const domain=h.replace(/^(u|ipfs|astroport|soundspot)\./i,'');
  return `wss://relay.${domain}`;
}

async function fetchKind0(pubkeyHex){
  return new Promise(resolve=>{
    let ws; try{ws=new WebSocket(getRelayUrl())}catch(_){resolve(null);return}
    const t=setTimeout(()=>{try{ws.close()}catch(_){}resolve(null)},5000);
    const sub='k0_'+Date.now();
    ws.onopen=()=>ws.send(JSON.stringify(['REQ',sub,{kinds:[0],authors:[pubkeyHex],limit:1}]));
    ws.onmessage=e=>{
      try{
        const m=JSON.parse(e.data);
        if(m[0]==='EVENT'&&m[2]?.kind===0){
          clearTimeout(t);ws.close();resolve(JSON.parse(m[2].content||'{}'));
        }
      }catch(_){}
    };
    ws.onerror=()=>{clearTimeout(t);resolve(null)};
  });
}

async function connectMultipass(){
  if(typeof window.nostr==='undefined'){
    st('Extension NIP-07 requise (Alby, nos2x…)','er');return;
  }
  st('Connexion MULTIPASS…','');
  try{
    const hex=await window.nostr.getPublicKey();
    const npub=hexToNpub(hex);
    const profile=await fetchKind0(hex)||{};
    const name=profile.display_name||profile.name||npub.slice(0,16)+'…';
    const avatar=profile.picture||'';
    _avatarUrl=avatar||null;

    $('mp-disconnected').style.display='none';
    $('mp-connected').style.display='flex';
    $('mp-name').textContent=name;
    $('mp-npub').textContent=npub.slice(0,20)+'…';
    if(avatar)$('mp-avatar').src=avatar;
    else $('mp-avatar').style.display='none';

    st('✓ MULTIPASS connecté','ok');
  }catch(e){st('Erreur: '+e.message,'er')}
}

function useAvatarAsBg(){
  if(!_avatarUrl){st('Aucun avatar dans le profil','er');return}
  _file=null; // réinitialiser le fichier local
  drop.classList.add('has');
  $('dlbl').innerHTML=`🪪 Avatar MULTIPASS<br><span style="opacity:.5;font-size:.7rem">${_avatarUrl.slice(0,40)}…</span>`;
  go(200);
}

go(200);
</script>
</body>
</html>"""


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
        return HTMLResponse(_QR_HTML)

    picture_path: Optional[str] = None

    if request.method == "POST":
        form = await request.form()
        def _f(k, default=""):
            return form.get(k) or default
        data        = data        or _f("data")
        color       = color       or _f("color",       "000000")
        bgcolor     = bgcolor     or _f("bgcolor",      "ffffff")
        format      = format      or _f("format",       "png")
        picture_url = picture_url or _f("picture_url") or None
        version     = version     if version   is not None else int(_f("version",    "1"))
        level       = level                            or _f("level",      "H")
        colorized   = colorized   if colorized is not None else int(_f("colorized", "0"))
        contrast    = contrast    if contrast  is not None else float(_f("contrast",  "1.0"))
        brightness  = brightness  if brightness is not None else float(_f("brightness","1.0"))

        pic = form.get("picture")
        if pic and hasattr(pic, "read"):
            pic_bytes = await pic.read()
            if pic_bytes:
                suffix = Path(getattr(pic, "filename", "pic.png")).suffix or ".png"
                tmp_pic = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
                tmp_pic.write(pic_bytes)
                tmp_pic.close()
                picture_path = tmp_pic.name

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
        png = await asyncio.to_thread(
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

    if format == "json":
        if png:
            return JSONResponse({
                "dataUrl": "data:image/png;base64," + base64.b64encode(png).decode(),
                "data": data,
            })
        fallback = (
            f"https://api.qrserver.com/v1/create-qr-code/"
            f"?size=180x180&data={urllib.parse.quote_plus(data)}&color={color}"
        )
        return JSONResponse({"fallback": fallback, "data": data})

    if png:
        return Response(content=png, media_type="image/png")

    fallback = (
        f"https://api.qrserver.com/v1/create-qr-code/"
        f"?size=180x180&data={urllib.parse.quote_plus(data)}&color={color}"
    )
    return RedirectResponse(url=fallback)
