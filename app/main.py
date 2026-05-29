from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from config import BASE_DIR, SESSION_SECRET
from database import init_db, seed_initial_admin
from routers.admin_categories import router as admin_categories_router
from routers.admin_series import router as admin_series_router
from routers.admin_users import router as admin_users_router
from routers.admin_videos import router as admin_videos_router
from routers.auth import router as auth_router
from routers.profile import router as profile_router
from routers.top import router as top_router

app = FastAPI()
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    max_age=60 * 60 * 24 * 7,
    https_only=False,
    same_site="lax",
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
def startup() -> None:
    init_db()
    seed_initial_admin()


app.include_router(top_router)
app.include_router(auth_router)
app.include_router(profile_router)
app.include_router(admin_users_router)
app.include_router(admin_videos_router)
app.include_router(admin_categories_router)
app.include_router(admin_series_router)
