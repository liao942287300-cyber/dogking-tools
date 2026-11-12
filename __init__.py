"""dogking-tools —— ComfyUI 自定义节点插件入口。

这个文件只做两件事：汇总各模块的节点映射、注册后端接口。

为什么节点模块用 try/except ImportError 分别导入：
  节点被拆成了 `pv_nodes`（提示词/图片/词库）和 `pv_asset_nodes`（资产图）两个文件，
  它们的落地时间不同（T03 / T04）。**任何一个文件暂时缺失或自身报错，
  都不应该让整个插件挂掉** —— 否则用户会看到「dogking-tools 加载失败」，
  连文件选择框那些无关节点也一起消失。

  注意这里 catch 的是 Exception 而不是只 catch ImportError：节点模块内部
  依赖缺失（比如某台机器没装 PIL）时抛的是别的异常，同样不能连坐整个插件。
"""

import traceback

from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
from .ripple_upscale import NODE_CLASS_MAPPINGS as _UPSCALE_CLASSES
from .ripple_upscale import NODE_DISPLAY_NAME_MAPPINGS as _UPSCALE_NAMES

# Ripple 二采放大节点也挂在这个插件下
NODE_CLASS_MAPPINGS.update(_UPSCALE_CLASSES)
NODE_DISPLAY_NAME_MAPPINGS.update(_UPSCALE_NAMES)

_LOADED_GROUPS = ["提示词批量加载", "图片批量加载", "Ripple 二采放大"]


def _register_node_module(module_name, label):
    """
    导入一个节点子模块并合并它的映射表。

    @param {str} module_name 形如 "pv_nodes"
    @param {str} label 打印用的中文名
    @returns {bool} 是否加载成功
    """
    try:
        module = __import__(f"{__name__}.{module_name}", fromlist=["NODE_CLASS_MAPPINGS"])
        classes = getattr(module, "NODE_CLASS_MAPPINGS", None)
        names = getattr(module, "NODE_DISPLAY_NAME_MAPPINGS", None)
        if not isinstance(classes, dict):
            print(f"[dogking-tools] {module_name} 没有 NODE_CLASS_MAPPINGS，已跳过")
            return False
        NODE_CLASS_MAPPINGS.update(classes)
        if isinstance(names, dict):
            NODE_DISPLAY_NAME_MAPPINGS.update(names)
        _LOADED_GROUPS.append(label)
        return True
    except ImportError as e:
        # 文件还没创建（T04 的 pv_asset_nodes.py 在 T03 阶段不存在）——正常情况，只提示
        print(f"[dogking-tools] 跳过 {module_name}（{e}），其余节点照常可用")
        return False
    except Exception:
        # 模块存在但自身报错：打印完整堆栈，方便用户/开发者定位，但不影响其他节点
        print(f"[dogking-tools] {module_name} 加载失败，其余节点照常可用：")
        traceback.print_exc()
        return False


# PromptVault 数据源节点（v2：图片+提示词 / 词库）
_register_node_module("pv_nodes", "PromptVault 数据源")
# 资产图节点（v2：单张 / 多重）
_register_node_module("pv_asset_nodes", "PromptVault 资产图")
# 提示词元数据嵌入 PNG（出图后反向喂回 PromptVault）
_register_node_module("pv_meta_nodes", "提示词元数据嵌入")

WEB_DIRECTORY = "./web"

# 注册后端接口（文件选择 / 行数预览 / 重置计数 / PromptVault 代转）
try:
    from . import server
    if not server.register_routes():
        # PromptServer 还没就绪，后台等它起来再挂
        server.register_routes_async()
except Exception as e:  # 接口挂了不影响节点本身可用
    print(f"[dogking-tools] 后端接口注册失败：{e}")

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

print(f"[dogking-tools] 已加载：{' / '.join(_LOADED_GROUPS)}")
