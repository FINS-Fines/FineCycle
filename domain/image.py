# domain/image.py

class RobotAppImage:
    """
    机器人应用镜像实体
    
    代表一个包含了OS、运行环境、开发工具和应用本体的虚拟机镜像。
    """
    def __init__(
        self,
        image_id: str,
        name: str,
        version: str,
        source_vm_id: int,
        *,
        is_template: bool = False,
    ):
        self.image_id = image_id
        self.name = name
        self.version = version
        self.source_vm_id = source_vm_id  # 在PVE源服务器上的VM ID模板
        self.status = "stopped"           # running, stopped, deploying
        self.is_template = bool(is_template)

    def __repr__(self):
        return f"<RobotApp: {self.name} (v{self.version})>"
