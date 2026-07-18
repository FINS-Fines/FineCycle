"""core/utils.py — 跨模块工具函数。"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path


def setup_task_logger(task_prefix: str) -> tuple[logging.Logger, str]:
    """为手动任务创建独立 logger，输出到 logs/ 目录。"""
    base_dir = Path(__file__).resolve().parent.parent
    logs_dir = base_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = logs_dir / f"manual-{task_prefix}-{timestamp}.log"

    logger_name = f"manual.{task_prefix}.{timestamp}"
    task_logger = logging.getLogger(logger_name)
    task_logger.setLevel(logging.INFO)
    task_logger.propagate = False
    task_logger.handlers = []

    file_handler = logging.FileHandler(str(log_path), encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    task_logger.addHandler(file_handler)

    return task_logger, str(log_path)
