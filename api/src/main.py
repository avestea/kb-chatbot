import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from src.lib.log import log
from src.lib.errors import ApiError
from src.db.base import engine

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("api starting")
    
    try:
        yield
    finally:
        log.info("api shutting down")
        await engine.dispose()


app = FastAPI(lifespan=lifespan, title="KBChat API", version="0.1.0")


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    """Handle ApiError exceptions."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}}
    )


@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception):
    log.error("unhandled error", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": "An unexpected error occurred"}}
    )


@app.get("/health")
async def health():
    async def check_db():
        try:
            from sqlalchemy import text
            
            async with engine.begin() as conn:
                await conn.execute(text("SELECT 1"))
            return "connected"
        except Exception as e:
            log.error("db health check failed", error=str(e))
            raise
    
    async def check_redis():
        try:
            from src.lib.redis import get_redis_client
            client = await get_redis_client()
            await client.ping()
            return "connected"
        except Exception as e:
            log.error("redis health check failed", error=str(e))
            raise
    
    results = await asyncio.gather(
        check_db(), check_redis(), return_exceptions=True
    )
    
    db_ok = not isinstance(results[0], Exception)
    redis_ok = not isinstance(results[1], Exception)
    status = "ok" if (db_ok and redis_ok) else "degraded"
    code = 200 if status == "ok" else 503
    
    return JSONResponse(
        status_code=code,
        content={
            "status": status,
            "db": "connected" if db_ok else "error",
            "redis": "connected" if redis_ok else "error",
        }
    )
