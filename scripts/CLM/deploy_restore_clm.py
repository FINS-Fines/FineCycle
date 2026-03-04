#!/usr/bin/env python3
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from __future__ import annotations

import argparse
import re
import sys
from typing import List, Tuple

import paramiko


def ssh_execute(ip: str, user: str, password: str, cmd: str, port: int = 22, timeout: float = 15.0) -> Tuple[str, str, int]:
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


def parse_usb_keys(conf_text: str) -> List[str]:
    return sorted(set(re.findall(r"^usb\d+(?=:)", conf_text, flags=re.MULTILINE)))


def parse_non_shared_disk_keys(conf_text: str, shared_storage: str) -> List[str]:
    """找出不在共享存储上的业务磁盘（scsi/sata/ide/virtio）。"""
    keys: List[str] = []
    for line in conf_text.splitlines():
        if ":" not in line:
            continue
        key = line.split(":", 1)[0].strip()
        if not key.startswith(("scsi", "sata", "ide", "virtio")):
            continue
        if "media=cdrom" in line or "cloudinit" in line:
            continue
        if shared_storage not in line:
            keys.append(key)
    return keys


def prepare_usb_for_migration(ip: str, user: str, password: str, vmid: int, port: int) -> None:
    """CLM 迁移前准备：清理 usbX 并设置 usb0=spice。"""
    conf = get_vm_config(ip, user, password, vmid, port)
    usb_keys = parse_usb_keys(conf)
    for key in usb_keys:
        run_cmd(ip, user, password, f"qm set {vmid} --delete {key}", port=port, allow_fail=True)
    run_cmd(ip, user, password, f"qm set {vmid} --usb0 spice", port=port)


def ensure_disks_on_shared_storage(
    ip: str,
    user: str,
    password: str,
    vmid: int,
    shared_storage: str,
    port: int,
) -> None:
    """确保磁盘位于共享存储（论文 CLM 前提）。"""
    conf = get_vm_config(ip, user, password, vmid, port)
    disk_keys = parse_non_shared_disk_keys(conf, shared_storage)
    if not disk_keys:
        print(f"所有业务磁盘已在共享存储 {shared_storage}，无需 move-disk。")
        return

    for key in disk_keys:
        # 按需求：如果不在共享存储，则移动到共享存储
        cmd = f"qm move-disk {vmid} {key} {shared_storage} --delete 1 --online 1"
        text = run_cmd(ip, user, password, cmd, port=port, allow_fail=True)
        # 与现有逻辑一致：有 snapshot 时，--delete 1 失败则回退 --delete 0
        if "snapshot" in text.lower() and "delete" in text.lower():
            retry_cmd = f"qm move-disk {vmid} {key} {shared_storage} --delete 0 --online 1"
            run_cmd(ip, user, password, retry_cmd, port=port, allow_fail=True)


def run_clm_migration(
    src_ip: str,
    src_user: str,
    password: str,
    vmid: int,
    target_node: str,
    src_port: int,
) -> None:
    """论文 CLM：仅迁移状态。"""
    cmd = f"qm migrate {vmid} {target_node} --online 1"
    run_cmd(src_ip, src_user, password, cmd, port=src_port)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ICRA V1.0: CLM 纯状态热迁移脚本")
    parser.add_argument("--vmid", required=True, type=int, help="虚拟机 ID")
    parser.add_argument("--src-ip", required=True, help="源 PVE 节点 IP（在此执行迁移命令）")
    parser.add_argument("--target-node", required=True, help="目标 PVE 节点名（qm migrate 第二参数）")
    parser.add_argument("--password", required=True, help="SSH 密码")
    parser.add_argument("--src-user", default="root", help="源节点 SSH 用户名，默认 root")
    parser.add_argument("--src-port", type=int, default=22, help="源节点 SSH 端口，默认 22")
    parser.add_argument("--shared-storage", default="VMs", help="共享存储名，默认 VMs")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        print("=== [1/3] 迁移前准备（USB 归一）===")
        prepare_usb_for_migration(args.src_ip, args.src_user, args.password, int(args.vmid), int(args.src_port))

        print("\n=== [2/3] 确保磁盘位于共享存储 ===")
        ensure_disks_on_shared_storage(
            ip=args.src_ip,
            user=args.src_user,
            password=args.password,
            vmid=int(args.vmid),
            shared_storage=str(args.shared_storage),
            port=int(args.src_port),
        )

        print("\n=== [3/3] 执行 CLM 迁移 ===")
        run_clm_migration(
            src_ip=args.src_ip,
            src_user=args.src_user,
            password=args.password,
            vmid=int(args.vmid),
            target_node=str(args.target_node),
            src_port=int(args.src_port),
        )
        print("\nCLM 完成：仅状态迁移，磁盘保持在共享存储。")
    except Exception as exc:
        print(f"\n执行失败：{exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
