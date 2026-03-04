#!/usr/bin/env python3
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
import subprocess
import sys
import time
import pyautogui

def send_command_to_window(window_title, command, delay=2):
    """
    向指定窗口发送命令
    """
    try:
        # 激活窗口
        window = pyautogui.getWindowsWithTitle(window_title)
        if window:
            window[0].activate()
            time.sleep(0.5)  # 等待窗口激活
            
            # 输入命令并回车
            pyautogui.write(command)
            time.sleep(0.1)
            pyautogui.press('enter')
            print(f"已向窗口 '{window_title}' 发送命令: {command}")
            return True
        else:
            print(f"未找到窗口: {window_title}")
            return False
    except Exception as e:
        print(f"发送命令失败: {e}")
        return False

def main():
    # 获取源端IP
    src_ip = input("请输入源端IP地址 (例如: 192.168.8.180): ").strip()
    
    # 获取目标端IP
    dst_ip = input("请输入目标端IP地址 (例如: 192.168.8.181): ").strip()
    
    # 获取源端磁盘名
    src_disk = input("请输入源端磁盘名 (例如: sda): ").strip()
    
    # 获取目标端磁盘名
    dst_disk = input("请输入目标端磁盘名 (例如: nvme0n1): ").strip()
    
    # 使用用户输入的IP和磁盘名构建命令
    src_ssh = f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL user@{src_ip}'
    src_cmd = f'sudo ocs-onthefly -a -f {src_disk}'
    
    dst_ssh = f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL user@{dst_ip}'
    dst_cmd = f'sudo ocs-onthefly -s {src_ip} -d {dst_disk}'
    
    print(f"\n配置信息:")
    print(f"源端IP: {src_ip}")
    print(f"目标端IP: {dst_ip}")
    print(f"源端磁盘: {src_disk}")
    print(f"目标端磁盘: {dst_disk}")
    print("\n将打开两个终端窗口：")
    print("1. SRC终端：立即SSH连接，5秒后执行sudo命令")
    print("2. DST终端：30秒后SSH连接，再5秒后执行sudo命令")

    try:
        # 启动SRC终端
        print("\n启动SRC终端...")
        src_window_cmd = f'start "SRC Terminal" cmd /k "{src_ssh}"'
        subprocess.Popen(src_window_cmd, shell=True)
        
        # 等待SSH连接建立（可能需要根据网络速度调整）
        print("等待SSH连接建立...")
        time.sleep(8)
        
        # 向SRC终端发送sudo命令
        print("向SRC终端发送sudo命令...")
        send_command_to_window("SRC Terminal", src_cmd)
        
        # 等待一段时间再启动DST终端
        print(f"\n等待22秒后启动DST终端...")
        time.sleep(22)  # 总共30秒延迟，之前已经等了8秒
        
        # 启动DST终端
        print("启动DST终端...")
        dst_window_cmd = f'start "DST Terminal" cmd /k "{dst_ssh}"'
        subprocess.Popen(dst_window_cmd, shell=True)
        
        # 等待SSH连接建立
        print("等待DST SSH连接建立...")
        time.sleep(8)
        
        # 向DST终端发送sudo命令
        print("向DST终端发送sudo命令...")
        send_command_to_window("DST Terminal", dst_cmd)
        
        # 等待20秒
        print("等待20秒...")
        time.sleep(20)
        
        # 向DST终端发送回车
        print("向DST终端发送回车...")
        send_command_to_window("DST Terminal", "")
        
        # 等待5秒
        print("等待5秒...")
        time.sleep(5)
        
        # 向DST终端发送y并回车
        print("向DST终端发送'y'并回车...")
        send_command_to_window("DST Terminal", "y")
        
        # 等待5秒
        print("等待5秒...")
        time.sleep(5)
        
        # 再次向DST终端发送y并回车
        print("向DST终端再次发送'y'并回车...")
        send_command_to_window("DST Terminal", "y")
        
        print("\n所有命令已发送完成！")
        print("请注意：如果终端窗口被其他窗口遮挡，发送命令可能会失败。")
        
    except Exception as e:
        print(f"执行失败: {e}")
        sys.exit(1)
    
    sys.exit(0)

if __name__ == '__main__':
    main()