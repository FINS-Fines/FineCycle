#!/usr/bin/env python3
"""
ICRA V1.0 脚本：deploy_cdc.py

功能对应论文 CDC（Cluster Disk Cloning）：
前提：VM 已通过 CLM 到达目标节点并运行。
动作：在目标节点将磁盘从共享存储（默认 VMs）移动到本地存储（默认 local-lvm）。
结果：VM 在目标主机获得本地盘高 I/O 能力。

依赖：pip install paramiko
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from typing import List, Tuple

import paramiko


def ssh_execute(ip: str, user: str, password: str, cmd: str, port: int = 22, timeout: float = 10.0) -> Tuple[str, str, int]:
    """最小 SSH 执行函数：返回 stdout、stderr、exit_code。"""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(hostname=ip, username=user, password=password, port=port, timeout=timeout)
        _stdin, stdout, stderr = client.exec_command(cmd)
        code = stdout.channel.recv_exit_status()
        out_text = stdout.read().decode(errors="replace")
        err_text = stderr.read().decode(errors="replace")
        return out_text, err_text, code
    finally:
        client.close()


def run_cmd(ip: str, user: str, password: str, cmd: str, port: int = 22, allow_fail: bool = False) -> str:
    print(f"\n[SSH {ip}]$ {cmd}")
    out, err, code = ssh_execute(ip, user, password, cmd, port=port)
    if out.strip():
        print(out.strip())
    if err.strip():
        print(err.strip(), file=sys.stderr)
    if code != 0 and not allow_fail:
        raise RuntimeError(f"命令执行失败（exit={code}）：{cmd}")
    return (out or "") + (err or "")


def get_vm_config(ip: str, user: str, password: str, vmid: int, port: int) -> str:
    return run_cmd(ip, user, password, f"qm config {vmid}", port=port)


def find_disks_on_storage(conf_text: str, storage_name: str) -> List[str]:
    """找出位于指定存储上的业务磁盘（scsi/sata/ide/virtio）。"""
    keys: List[str] = []
    for line in conf_text.splitlines():
        if ":" not in line:
            continue
        key = line.split(":", 1)[0].strip()
        if not key.startswith(("scsi", "sata", "ide", "virtio")):
            continue
        if "media=cdrom" in line or "cloudinit" in line:
            continue
        if storage_name in line:
            keys.append(key)
    return keys


def move_shared_disks_to_local(
    target_ip: str,
    target_user: str,
    password: str,
    vmid: int,
    shared_storage: str,
    target_local_storage: str,
    target_port: int,
) -> None:
    """CDC 核心：在目标节点将共享存储磁盘落地到本地存储。"""
    conf = get_vm_config(target_ip, target_user, password, vmid, target_port)
    disk_keys = find_disks_on_storage(conf, shared_storage)
    if not disk_keys:
        print(f"未发现位于共享存储 {shared_storage} 的业务磁盘，跳过 CDC move-disk。")
        return

    print(f"将在目标节点执行 CDC 落盘：{disk_keys}")
    for disk in disk_keys:
        cmd = f"qm move-disk {vmid} {disk} {target_local_storage} --delete 1 "
        text = run_cmd(target_ip, target_user, password, cmd, port=target_port, allow_fail=True)

        # 如果 snapshot 导致 delete 1 失败，回退到 --delete 0
        if "snapshot" in text.lower() and "delete" in text.lower():
            retry_cmd = f"qm move-disk {vmid} {disk} {target_local_storage} --delete 0 "
            run_cmd(target_ip, target_user, password, retry_cmd, port=target_port, allow_fail=True)


def switch_network(ip: str, user: str, password: str, mode: str, port: int = 22) -> None:
    """
    切换目标主机网络配置：
    /etc/network/interfaces.<mode> -> /etc/network/interfaces
    mode 仅允许 wired 或 wireless。
    """
    mode_value = str(mode).strip().lower()
    if mode_value not in {"wired", "wireless"}:
        raise ValueError("mode 仅支持 wired 或 wireless")

    template_file = f"/etc/network/interfaces.{mode_value}"
    run_cmd(ip, user, password, f"test -f {template_file}", port=port)
    run_cmd(ip, user, password, f"cp {template_file} /etc/network/interfaces", port=port)
    print(f"网络配置已切换为 {mode_value}。")


def reboot_and_wait(
    old_ip: str,
    new_ip: str,
    user: str,
    password: str,
    port: int = 22,
    timeout: int = 300,
) -> None:
    """
    触发重启并等待新 IP 上线。
    - 向 old_ip 发送 reboot（允许并忽略连接中断异常）
    - 循环探测 new_ip，直到 SSH 可执行 `echo SSH_OK` 或超时
    """
    print(f"准备重启节点：old_ip={old_ip} -> new_ip={new_ip}")
    try:
        # 使用较短 timeout 触发 reboot，连接中断属于预期行为
        ssh_execute(old_ip, user, password, "reboot", port=port, timeout=3.0)
    except Exception as reboot_exc:
        print(f"reboot 触发连接中断（预期）：{reboot_exc}")

    # 关键防竞态步骤：等待 SSH 服务确实下线，避免误判“已重启完成”
    print("等待 10 秒确保节点彻底断开连接...")
    time.sleep(10)

    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        try:
            out, err, code = ssh_execute(new_ip, user, password, "echo SSH_OK", port=port, timeout=8.0)
            text = ((out or "") + (err or "")).strip()
            if code == 0 and "SSH_OK" in text:
                print(f"节点已上线：{new_ip}")
                return
        except Exception as conn_exc:
            print(f"等待节点上线中：{new_ip} ({conn_exc})")

        time.sleep(5)

    raise TimeoutError(f"等待节点 SSH 上线超时：{new_ip} (timeout={timeout}s)")


def select_boot_disk_from_local(conf_text: str, target_local_storage: str) -> str | None:
    local_disks = find_disks_on_storage(conf_text, target_local_storage)
    prefer = ["scsi0", "virtio0", "sata0", "ide0", "scsi1", "virtio1", "sata1", "ide1"]
    for disk in prefer:
        if disk in local_disks:
            return disk
    return local_disks[0] if local_disks else None


def set_boot_on_target(
    target_ip: str,
    target_user: str,
    password: str,
    vmid: int,
    target_local_storage: str,
    target_port: int,
) -> None:
    """CDC 收尾：设置启动盘和启动顺序。"""
    conf_after = get_vm_config(target_ip, target_user, password, vmid, target_port)
    boot_disk = select_boot_disk_from_local(conf_after, target_local_storage)
    if not boot_disk:
        print("未找到位于本地存储的可用系统盘，跳过 bootdisk 设置。")
        return

    run_cmd(target_ip, target_user, password, f"qm set {vmid} --bootdisk {boot_disk}", port=target_port, allow_fail=True)
    run_cmd(target_ip, target_user, password, f"qm set {vmid} --boot order={boot_disk}", port=target_port, allow_fail=True)
    print(f"已设置启动盘：{boot_disk}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ICRA V1.0: CDC 磁盘落地脚本（仅目标节点操作）")
    parser.add_argument("--vmid", required=True, type=int, help="虚拟机 ID")
    parser.add_argument("--target-ip", required=True, help="目标 PVE 节点 IP")
    parser.add_argument("--wired-ip", default=None, help="切换为有线网络后的 IP（默认等于 --target-ip）")
    parser.add_argument("--wireless-ip", default=None, help="切换回无线网络后的 IP（默认等于 --target-ip）")
    parser.add_argument("--password", required=True, help="SSH 密码")
    parser.add_argument("--target-user", default="root", help="目标节点 SSH 用户名，默认 root")
    parser.add_argument("--target-port", type=int, default=22, help="目标节点 SSH 端口，默认 22")
    parser.add_argument("--shared-storage", default="VMs", help="共享存储名，默认 VMs")
    parser.add_argument("--target-local-storage", default="local-lvm", help="目标本地存储名，默认 local-lvm")
    parser.add_argument("--reboot-timeout", type=int, default=300, help="每次重启等待上线超时秒数，默认 300")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    wired_ip = args.wired_ip if args.wired_ip else args.target_ip
    wireless_ip = args.wireless_ip if args.wireless_ip else args.target_ip

    try:
        print("=== [阶段 1/4] 准备阶段：切换有线网络并等待重启上线 ===")
        switch_network(args.target_ip, args.target_user, args.password, mode="wired", port=int(args.target_port))
        reboot_and_wait(
            old_ip=args.target_ip,
            new_ip=wired_ip,
            user=args.target_user,
            password=args.password,
            port=int(args.target_port),
            timeout=int(args.reboot_timeout),
        )

        print("\n=== [阶段 2/4] CDC 磁盘克隆（共享存储 -> 本地存储）===")
        move_shared_disks_to_local(
            target_ip=wired_ip,
            target_user=args.target_user,
            password=args.password,
            vmid=int(args.vmid),
            shared_storage=str(args.shared_storage),
            target_local_storage=str(args.target_local_storage),
            target_port=int(args.target_port),
        )

        print("\n=== [阶段 3/4] 收尾配置（设置 bootdisk / boot order）===")
        set_boot_on_target(
            target_ip=wired_ip,
            target_user=args.target_user,
            password=args.password,
            vmid=int(args.vmid),
            target_local_storage=str(args.target_local_storage),
            target_port=int(args.target_port),
        )

        print("\n=== [阶段 4/4] 恢复阶段：切回无线网络并等待重启上线 ===")
        switch_network(wired_ip, args.target_user, args.password, mode="wireless", port=int(args.target_port))
        reboot_and_wait(
            old_ip=wired_ip,
            new_ip=wireless_ip,
            user=args.target_user,
            password=args.password,
            port=int(args.target_port),
            timeout=int(args.reboot_timeout),
        )

        print("\nCDC 全流程完成：已完成有线克隆并恢复无线网络。")
    except Exception as exc:
        print(f"\n执行失败：{exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
