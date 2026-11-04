from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

WEB_DIRECTORY = "./web"

# 注册后端接口（文件选择 / 行数预览 / 重置计数）
try:
    from . import server
    if not server.register_routes():
        # PromptServer 还没就绪，后台等它起来再挂
        server.register_routes_async()
except Exception as e:  # 接口挂了不影响节点本身可用
    print(f"[dogking-tools] 后端接口注册失败：{e}")

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

print("[dogking-tools] 提示词批量加载节点已加载")
