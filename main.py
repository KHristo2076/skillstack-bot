import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.bot import lifespan
from app.routes import router

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="SkillStack Bot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
