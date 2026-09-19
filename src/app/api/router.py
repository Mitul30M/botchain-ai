from fastapi import APIRouter

from app.api.v1 import chats, credits, messages, webhooks

api_router = APIRouter()
api_router.include_router(chats.router, prefix="/chats", tags=["chats"])
api_router.include_router(messages.router, prefix="/chats", tags=["messages"])
api_router.include_router(credits.router, prefix="/credits", tags=["credits"])
api_router.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])