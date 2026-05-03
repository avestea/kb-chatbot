from fastapi import FastAPI

app = FastAPI(title="KB Chatbot API")


@app.get("/health")
async def health():
    return {"status": "ok"}
