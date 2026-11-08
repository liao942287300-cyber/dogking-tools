from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
from .ripple_upscale import NODE_CLASS_MAPPINGS as _UPSCALE_CLASSES
from .ripple_upscale import NODE_DISPLAY_NAME_MAPPINGS as _UPSCALE_NAMES
from .pv_nodes import NODE_CLASS_MAPPINGS as _PV_CLASSES
from .pv_nodes import NODE_DISPLAY_NAME_MAPPINGS as _PV_NAMES

# Ripple 二采放大节点也挂在这个插件下
NODE_CLASS_MAPPINGS.update(_UPSCALE_CLASSES)
NODE_DISPLAY_NAME_MAPPINGS.update(_UPSCALE_NAMES)

# PromptVault 数据源节点（提示词 / 图片 / 图片+提示词 / 词库）
NODE_CLASS_MAPPINGS.update(_PV_CLASSES)
NODE_DISPLAY_NAME_MAPPINGS.update(_PV_NAMES)

WEB_DIRECTORY = "./web"

# 注册后端接口（文件选择 / 行数预览 / 重置计数 / PromptVault 分类代转）
try:
    from . import server
    if not server.register_routes():
        # PromptServer 还没就绪，后台等它起来再挂
        server.register_routes_async()
except Exception as e:  # 接口挂了不影响节点本身可用
    print(f"[dogking-tools] 后端接口注册失败：{e}")

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

print("[dogking-tools] 已加载：提示词批量加载 / 图片批量加载 / Ripple 二采放大 / PromptVault 数据源 ×4")
