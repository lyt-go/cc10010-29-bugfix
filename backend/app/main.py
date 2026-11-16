"""数据仓库ETL任务编排平台 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8000
健康检查：GET /api/health
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import ROUTERS
from app.services import slafulfillment
from app.store import store

app = FastAPI(title="数据仓库ETL任务编排平台", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in ROUTERS:
    app.include_router(module.router)


@app.on_event("startup")
def bootstrap_sla_fulfillment() -> None:
    """启动时按「规则 + 运行水位」重算一次，确保已生效规则立刻有履约事件。

    只处理 ``已生效`` 规则，因此已关闭规则即使水位变化也不会恢复升级。
    """
    slafulfillment.recalc_all()


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪。"""
    return {"ok": True, "app": settings.app_name, "modules": len(store.module_names())}


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()
