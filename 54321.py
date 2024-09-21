from fastapi import FastAPI, Request, Form
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates
import asyncio
import aiofiles

app = FastAPI()
templates = Jinja2Templates(directory="templates")

@app.get("/")
async def get_root(request: Request):
    return templates.TemplateResponse("scan_new.html", {"request": request})

@app.post("/upassport")
async def scan_qr(parametre: str = Form(...)):
    script_path = "./upassport.sh"
    log_file_path = "./tmp/54321.log"

    async def run_script():
        process = await asyncio.create_subprocess_exec(
            script_path, parametre,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )

        last_line = ""
        async with aiofiles.open(log_file_path, "a") as log_file:
            async for line in process.stdout:
                line = line.decode().strip()
                last_line = line
                await log_file.write(line + "\n")
                print(line)

        return_code = await process.wait()
        return return_code, last_line

    return_code, last_line = await run_script()

    if return_code == 0:
        html_file_path = last_line.strip()
        return FileResponse(html_file_path)
    else:
        return {"error": f"Une erreur s'est produite lors de l'exécution du script. Veuillez consulter les logs dans {log_file_path}."}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=54321)
