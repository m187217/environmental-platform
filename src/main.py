"""FastAPI application entry point."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.user.routes import router as auth_router

app = FastAPI(
    title="Environmental Reports Platform",
    description="中国企业环境评估报告共享与匹配平台",
    version="0.1.0",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routes
app.include_router(auth_router)


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}
