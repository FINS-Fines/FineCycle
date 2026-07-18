# infrastructure/bare_metal_host.py
from __future__ import annotations

from .base_host import BaseHost
from typing import Dict, Any, List
import paramiko
import re
import os
import subprocess
import time

class BareMetalHost(BaseHost):
    """
    裸机机器人主机。
    特点：需要通过网络引导(PXE)拉取镜像，或通过Clonezilla进行裸机克隆。
    """
    def __init__(
        self,
        host_id: str,
        hostname: str,
        target_disk: str,
        ip: str | None = None,
        image_info: dict = None,
        *,
        disk_size: str = "",
        disk_type: str = "HDD",
        disk_model: str = "",
        serial_number: str = "",
        ssh_username: str = 'user',  # 裸机通常不是root用户
        ssh_password: str | None = None,
        ssh_port: int = 22,
        ssh_key_filename: str | None = None,
        ssh_timeout_s: float = 5.0,
    ):
        super().__init__(host_id, ip or "", hostname, image_info)
        self.target_disk = str(target_disk or "").strip()
        
        # --- 硬盘详细信息 ---
        self.hardware_info: Dict[str, str] = {
            'disk_size': str(disk_size or "").strip(),
            'disk_type': str(disk_type or "HDD").strip(),
            'disk_model': str(disk_model or "").strip(),
            'serial_number': str(serial_number or "").strip(),
        }
        
        # --- SSH (lazy connect) ---
        self.ssh_client = paramiko.SSHClient()
        self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._ssh_username = ssh_username
        self._ssh_password = ssh_password
        self._ssh_port = ssh_port
        self._ssh_key_filename = ssh_key_filename
        self._ssh_timeout_s = ssh_timeout_s

    def _ensure_ssh_connected(self):
        """确保SSH连接已建立"""
        try:
            transport = self.ssh_client.get_transport() if self.ssh_client else None
            if transport and transport.is_active():
                return
        except Exception:
            pass

        self.ssh_client.connect(
            hostname=self.ip,
            username=self._ssh_username,
            password=self._ssh_password,
            port=self._ssh_port,
            key_filename=self._ssh_key_filename,
            timeout=self._ssh_timeout_s,
        )

    def execute_ssh(self, cmd: str):
        """通过SSH执行命令"""
        self._ensure_ssh_connected()
        stdin, stdout, stderr = self.ssh_client.exec_command(cmd)
        return stdout.read().decode(), stderr.read().decode()

    def check_health(self) -> bool:
        """检测裸机主机是否在线（Mock：禁止连接 IP/SSH）"""
        return False

    def get_internal_app_status(self) -> Dict[str, Any]:
        """返回主机内部应用状态（裸机可能没有PVE）"""
        return {
            "type": "bare_metal",
            "ip": self.ip,
            "target_disk": self.target_disk,
            "online": self.check_health()
        }

    # -----------------------------------------------------------
    # 必须实现的功能 1: 获取裸机IP
    # -----------------------------------------------------------
    def get_current_ip(self) -> str:
        """获取当前SSH连接的IP地址"""
        return self.ip

    def detect_network_config(self) -> Dict[str, Any]:
        """探测裸机网络配置（类似PVE主机的功能）"""
        config = {}
        
        try:
            # 获取IP地址配置
            if os.name == 'posix':
                # Linux系统
                out, err = self.execute_ssh("ip addr show | grep 'inet ' | grep -v '127.0.0.1'")
                ip_lines = out.strip().split('\n')
                for line in ip_lines:
                    if 'inet' in line:
                        parts = line.strip().split()
                        if len(parts) >= 2:
                            config['ip'] = parts[1]
                            break
                
                # 获取网关
                out, err = self.execute_ssh("ip route | grep 'default via'")
                if out and 'via' in out:
                    config['gateway'] = out.split()[2]
                    
                # 获取接口名称
                out, err = self.execute_ssh("ip link show | grep 'state UP' | awk -F': ' '{print $2}'")
                if out:
                    interfaces = [iface.strip() for iface in out.split('\n') if iface.strip()]
                    if interfaces:
                        config['primary_interface'] = interfaces[0]
                        
            else:
                # Windows系统或其他
                out, err = self.execute_ssh("hostname -I")
                if out:
                    ips = out.strip().split()
                    if ips:
                        config['ip'] = ips[0]
                        
        except Exception as e:
            print(f"[{self.hostname}] 探测网络配置失败: {e}")
            
        return config

    # -----------------------------------------------------------
    # 必须实现的功能 2: 获取裸机的所有硬盘名称
    # -----------------------------------------------------------
    def get_all_disks(self) -> List[Dict[str, Any]]:
        """获取裸机所有硬盘信息"""
        disks = []
        
        try:
            # Linux系统 - 使用lsblk或fdisk
            out, err = self.execute_ssh("lsblk -d -o NAME,SIZE,MODEL,TYPE,ROTA --json 2>/dev/null || sudo fdisk -l 2>/dev/null")
            
            # 尝试解析lsblk的JSON输出
            if out and 'disk' in out.lower():
                # 简化处理，提取磁盘名
                lines = out.strip().split('\n')
                for line in lines:
                    if 'disk' in line.lower() and not 'cdrom' in line.lower():
                        parts = line.split()
                        if parts:
                            disk_name = parts[0].strip()
                            if disk_name.startswith(('sd', 'hd', 'vd', 'nvme')):
                                disk_info = {
                                    'name': disk_name,
                                    'device': f"/dev/{disk_name}",
                                    'size': ' '.join(parts[1:]) if len(parts) > 1 else 'unknown'
                                }
                                disks.append(disk_info)
            
            # 如果lsblk失败，尝试使用fdisk
            if not disks:
                out, err = self.execute_ssh("sudo fdisk -l 2>/dev/null | grep 'Disk /dev/'")
                lines = out.strip().split('\n')
                for line in lines:
                    if 'Disk /dev/' in line and not 'loop' in line:
                        parts = line.split()
                        if len(parts) >= 2:
                            device = parts[1].rstrip(':')
                            disk_name = device.split('/')[-1]
                            size = ' '.join(parts[2:]) if len(parts) > 2 else 'unknown'
                            
                            disk_info = {
                                'name': disk_name,
                                'device': device,
                                'size': size
                            }
                            disks.append(disk_info)
                            
        except Exception as e:
            print(f"[{self.hostname}] 获取磁盘信息失败: {e}")
            
        return disks

    def get_disk_names(self) -> List[str]:
        """获取所有磁盘名称列表（简化版）"""
        disks = self.get_all_disks()
        return [disk['name'] for disk in disks]

    # -----------------------------------------------------------
    # 裸机特定功能
    # -----------------------------------------------------------
    def reboot_system(self) -> bool:
        """重启裸机系统"""
        print(f"[{self.hostname}] 重启裸机系统...")
        try:
            # 尝试使用不同方法重启
            reboot_cmds = [
                "sudo reboot",
                "sudo shutdown -r now",
                "sudo systemctl reboot"
            ]
            
            for cmd in reboot_cmds:
                try:
                    out, err = self.execute_ssh(f"{cmd} &")
                    if not err or 'command not found' not in err:
                        print(f"[{self.hostname}] 重启命令已发送: {cmd}")
                        return True
                except Exception:
                    continue
                    
            print(f"[{self.hostname}] 所有重启命令都失败")
            return False
            
        except Exception as e:
            print(f"[{self.hostname}] 重启失败: {e}")
            return False

    def power_off(self) -> bool:
        """关机裸机系统"""
        print(f"[{self.hostname}] 关闭裸机系统...")
        try:
            out, err = self.execute_ssh("sudo poweroff")
            print(f"[{self.hostname}] 关机命令已发送")
            return True
        except Exception as e:
            print(f"[{self.hostname}] 关机失败: {e}")
            return False

    def wait_for_boot(self, timeout: int = 300) -> bool:
        """等待裸机启动完成"""
        print(f"[{self.hostname}] 等待系统启动 (超时: {timeout}秒)...")
        
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self.check_health():
                print(f"[{self.hostname}] 系统已启动")
                return True
            time.sleep(5)
            
        print(f"[{self.hostname}] 等待系统启动超时")
        return False

    def execute_clonezilla_command(self, command: str) -> tuple[str, str]:
        """在裸机上执行Clonezilla相关命令"""
        print(f"[{self.hostname}] 执行Clonezilla命令: {command}")
        try:
            # 假设裸机已进入Clonezilla环境
            out, err = self.execute_ssh(command)
            return out, err
        except Exception as e:
            print(f"[{self.hostname}] 执行Clonezilla命令失败: {e}")
            raise

    def start_clonezilla_receiver(self, source_ip: str, source_disk: str, target_disk: str) -> bool:
        """启动Clonezilla接收模式"""
        print(f"[{self.hostname}] 启动Clonezilla接收模式...")
        
        try:
            # Clonezilla接收命令
            cmd = f"sudo ocs-onthefly -s {source_ip} -d {target_disk}"
            out, err = self.execute_ssh(cmd)
            
            if "Starting Clonezilla" in out or "clonezilla" in out.lower():
                print(f"[{self.hostname}] Clonezilla接收模式已启动")
                return True
            else:
                print(f"[{self.hostname}] 启动Clonezilla失败: {err}")
                return False
                
        except Exception as e:
            print(f"[{self.hostname}] 启动Clonezilla接收模式失败: {e}")
            return False

    def check_clonezilla_ready(self) -> bool:
        """检查Clonezilla环境是否就绪"""
        try:
            out, err = self.execute_ssh("which ocs-onthefly || echo 'not found'")
            return "not found" not in out
        except Exception:
            return False

    def update_application(self, repository_info: dict) -> str:
        """
        通知裸机机器人主机拉取最新代码并重启应用
        input: repository_info (Git仓库地址, 分支)
        output: str (执行结果日志)
        """
        repo_url = repository_info.get('repo_url')
        branch = repository_info.get('branch', 'main')
        repo_path = repository_info.get('repo_path', '~/app')
        restart_cmd = repository_info.get('restart_cmd', '')

        logs = []
        try:
            # 进入代码目录
            cmd_cd = f"cd {repo_path}"
            out, err = self.execute_ssh(cmd_cd)
            
            # 检查是否有git仓库
            out, err = self.execute_ssh(f"cd {repo_path} && git status 2>&1 || echo 'no git repo'")
            if 'no git repo' in out:
                # 克隆新仓库
                cmd_clone = f"cd {repo_path}/.. && git clone {repo_url} {repo_path}"
                out, err = self.execute_ssh(cmd_clone)
                logs.append(f"Clone: {out}{err}")
            else:
                # 拉取最新代码
                cmd_pull = f"cd {repo_path} && git pull origin {branch}"
                out, err = self.execute_ssh(cmd_pull)
                logs.append(f"Pull: {out}{err}")
            
            # 重启应用
            if restart_cmd:
                out, err = self.execute_ssh(f"cd {repo_path} && {restart_cmd}")
                logs.append(f"Restart: {out}{err}")
                
            return "\n".join(logs)
            
        except Exception as e:
            return f"Update failed: {e}"

    def get_system_info(self) -> Dict[str, Any]:
        """获取裸机系统信息"""
        info = {
            'hostname': self.hostname,
            'ip': self.ip,
            'target_disk': self.target_disk,
            'online': self.check_health()
        }
        
        try:
            # CPU信息
            out, err = self.execute_ssh("lscpu | grep 'Model name' | cut -d':' -f2 | xargs")
            if out:
                info['cpu'] = out.strip()
                
            # 内存信息
            out, err = self.execute_ssh("free -h | grep Mem | awk '{print $2}'")
            if out:
                info['memory'] = out.strip()
                
            # 操作系统信息
            out, err = self.execute_ssh("cat /etc/os-release | grep PRETTY_NAME | cut -d'=' -f2 | tr -d '\"'")
            if out:
                info['os'] = out.strip()
                
        except Exception as e:
            print(f"[{self.hostname}] 获取系统信息失败: {e}")
            
        return info

    def __repr__(self):
        return f"[BareMetal] {self.hostname} (Disk: {self.target_disk})"



