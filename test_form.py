from fastapi import FastAPI, Form
from pydantic import BaseModel
from fastapi.testclient import TestClient

app = FastAPI()

class MyForm(BaseModel):
    name: str
    age: int

@app.post("/test")
async def test_route(form_data: MyForm = Form(...)):
    return form_data

client = TestClient(app)
response = client.post("/test", data={"name": "John", "age": 30})
print(response.json())
