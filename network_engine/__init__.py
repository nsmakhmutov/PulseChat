# network_engine/__init__.py — Пакет сетевого клиента InPulse
#
# Совместимость: from network_engine import NetworkClient — работает как раньше.

from .core import NetworkClient
from .sfu_bridge import SfuBridge, get_shared as get_shared_sfu
from .media_engine_bridge import MediaEngineBridge
from .server_discovery import ServerDiscovery, ServerAnnouncer, get_local_radmin_ip

__all__ = [
    'NetworkClient',
    'SfuBridge', 'get_shared_sfu',
    'MediaEngineBridge',
    'ServerDiscovery', 'ServerAnnouncer', 'get_local_radmin_ip',
]
