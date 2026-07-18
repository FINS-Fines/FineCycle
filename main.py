# main.py
from domain.image import RobotAppImage
from infrastructure.pve_host import PveHost
from infrastructure.bare_metal_host import BareMetalHost
from core.datacenter import DataCenter

def main():
    # 1. 初始化数据中心
    dc = DataCenter("Robot Central Command")

    # 2. 设置核心镜像服务器 (PVE架构)
    repo_server = PveHost("srv-01", "192.168.1.10", "Master-Repo", "pve-master")
    dc.set_master_server(repo_server)

    # 3. 注册不同的机器人主机
    # 主机A：高端机器人，自带PVE
    robot_pve = PveHost("bot-01", "192.168.1.101", "Robot-Dog-Pro", "pve-node-05")

    # 主机B：低成本机器人，裸机
    robot_bare = BareMetalHost("bot-02", "Robot-Cart-Lite", target_disk="nvme0n1", ip=None)

    dc.register_host(robot_pve)
    dc.register_host(robot_bare)

    # 4. 定义一个机器人应用镜像
    # 这是一个包含OS和ROS环境的完整虚拟机模板
    nav_app = RobotAppImage("img-nav-v2", "Navigation Stack", "2.0.1", source_vm_id=1001)

    # 5. 执行监控 (获取主机和应用状态)
    print("\n=== Monitoring Status ===")
    status = dc.get_host_status()
    import json
    print(json.dumps(status, indent=2))

    # 6. 场景一：部署到 PVE 机器人 (触发迁移)
    dc.software_deployment(robot_pve, nav_app)

    # 7. 场景二：部署到 裸机 机器人 (触发克隆)
    dc.deploy_app(nav_app, "bot-02")

if __name__ == "__main__":
    main()