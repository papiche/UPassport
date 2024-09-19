from fastapi import FastAPI, Request, Form
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates
import subprocess

app = FastAPI()
templates = Jinja2Templates(directory="templates")

@app.get("/")
async def get_root(request: Request):
    return templates.TemplateResponse("scan_qr.html", {"request": request})

@app.post("/upassport")
async def scan_qr(parametre: str = Form(...)):
    script_path = "./upassport.sh"  # Remplacez par le chemin réel du script
    log_file_path = "./tmp/54321.log"  # Chemin vers le fichier de log

    with open(log_file_path, "a") as log_file:
        process = subprocess.Popen([script_path, parametre], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        last_line = ""
        for line in process.stdout:
            last_line = line  # Stockez la dernière ligne de sortie
            log_file.write(line)  # Écrivez chaque ligne dans le fichier de log
            print(line, end="")  # Affichez également la sortie dans la console pour un suivi en temps réel
        return_code = process.wait()

    if return_code == 0:
        html_file_path = last_line.strip()  # Obtenez le chemin du fichier HTML de la dernière ligne des journaux
        return FileResponse(html_file_path)  # Renvoie directement le contenu du fichier HTML en tant que réponse HTTP
    else:
        return {"error": f"Une erreur s'est produite lors de l'exécution du script. Veuillez consulter les logs dans {log_file_path}."}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=54321)
