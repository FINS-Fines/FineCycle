"""configs/disk_record.py — 磁盘操作记录格式配置。"""

from __future__ import annotations
from dataclasses import dataclass, asdict


@dataclass
class DiskRecord:
    """磁盘大小变更记录"""
    vmid: int
    disk_name: str           # 如 virtio0, scsi0
    disk_device: str         # 如 vm-100-disk-0
    original_size_gb: int    # 原始大小(GB) — 用于恢复
    current_size_gb: int     # 记录时的当前大小(GB)
    new_size_gb: int         # 变更后的大小(GB)
    operation_type: str      # "expand" / "shrink" / "initial"
    operated_at: str         # 操作时间戳
    storage: str             # 存储位置，如 local-lvm
    node: str                # 所在 PVE 节点
