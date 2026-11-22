"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import field_handover, resource

# 交接路由含固定前缀，必须排在通配 /api/{module} 之前，否则会被动态路径抢占
ROUTERS = [field_handover, resource]
