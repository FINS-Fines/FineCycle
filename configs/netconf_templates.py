"""
PVE 宿主机网络配置模板（有线 / 无线）。

本文件是 PVE 宿主机 /etc/network/interfaces 的唯一模板来源，同时服务于：
 - 自动化部署：pve_host.py 的 upload_network_profiles() 使用 .format() 填充后上传
 - 手动参考：可将模板内容作为修改远端 /etc/network/interfaces 的参考

--------------------------------------------------------------
有线模式（Wired Mode）
--------------------------------------------------------------
渲染后的 /etc/network/interfaces 示例：

    auto lo
    iface lo inet loopback

    auto enp86s0
    iface enp86s0 inet manual

    auto vmbr0
    iface vmbr0 inet static
        address 192.168.8.200/21
        gateway 192.168.8.1
        bridge-ports enp86s0
        bridge-stp off
        bridge-fd 0

变量说明：
  {physical_interface} — 物理有线网卡名，如 enp86s0
  {ip}                — CIDR 格式的静态 IP，如 192.168.8.200/21
  {gateway}           — 网关地址，如 192.168.8.1

--------------------------------------------------------------
无线模式（Wireless Mode）
--------------------------------------------------------------
渲染后的 /etc/network/interfaces 示例：

    auto lo
    iface lo inet loopback

    auto wlo1
    iface wlo1 inet static
        address 192.168.8.200/21
        gateway 192.168.8.1
        wpa-ssid MyWiFi
        wpa-psk mypassword

    auto vmbr0
    iface vmbr0 inet manual
        bridge-ports none
        bridge-stp off
        bridge-fd 0

变量说明：
  {wifi_interface} — 无线网卡名，如 wlo1
  {ip}             — CIDR 格式的静态 IP，如 192.168.8.200/21
  {gateway}        — 网关地址，如 192.168.8.1
  {ssid}           — Wi-Fi SSID
  {psk}            — Wi-Fi 密码

注意：PVE 下 Wi-Fi 做桥接较复杂，无线模式下 vmbr0 为空桥（bridge-ports none），
      仅保留以防止 PVE 启动报错。虚拟机需通过 NAT/路由方式上网。
"""

WIRED_TEMPLATE = """\
auto lo
iface lo inet loopback

auto {physical_interface}
iface {physical_interface} inet manual

auto vmbr0
iface vmbr0 inet static
    address {ip}
    gateway {gateway}
    bridge-ports {physical_interface}
    bridge-stp off
    bridge-fd 0
"""

WIRELESS_TEMPLATE = """\
auto lo
iface lo inet loopback

auto {wifi_interface}
iface {wifi_interface} inet static
    address {ip}
    gateway {gateway}
    wpa-ssid {ssid}
    wpa-psk {psk}

auto vmbr0
iface vmbr0 inet manual
    bridge-ports none
    bridge-stp off
    bridge-fd 0
"""
