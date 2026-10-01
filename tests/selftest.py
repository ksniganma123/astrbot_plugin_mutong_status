# -*- coding: utf-8 -*-
r"""离线自测：不需要真实面板和服务器，用本地 mock 验证采集链路。

运行方式（Windows PowerShell，使用 AstrBot 自带 Python）：
    $env:PYTHONPATH = "D:\QQBOT\astrbot\backend\app"
    & "D:\QQBOT\astrbot\backend\python\python.exe" tests\selftest.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web

PLUGIN_DIR = Path(__file__).resolve().parents[1]

# AstrBot 的部分模块会按当前工作目录创建 data/ 目录，切到临时目录避免污染插件文件夹。
os.chdir(tempfile.gettempdir())
sys.dont_write_bytecode = True

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_plugin_module():
    spec = importlib.util.spec_from_file_location("mutong_status_main", PLUGIN_DIR / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = load_plugin_module()


class FakeMeta:
    def __init__(self, name: str, platform_id: str, proactive: bool = True) -> None:
        self.name = name
        self.id = platform_id
        self.support_proactive_message = proactive


class FakePlatform:
    def __init__(self, name: str, platform_id: str, proactive: bool = True) -> None:
        self._meta = FakeMeta(name, platform_id, proactive)
        self.scene_calls: list[tuple[str, str]] = []

    def meta(self) -> FakeMeta:
        return self._meta

    def remember_session_scene(self, session_id: str, scene: str) -> None:
        self.scene_calls.append((session_id, scene))


async def start_mock_minecraft() -> tuple[asyncio.AbstractServer, int]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            length = await plugin.read_varint(reader)
            await reader.readexactly(length)  # handshake
            length = await plugin.read_varint(reader)
            await reader.readexactly(length)  # status request
            info = {
                "version": {"name": "Paper 1.20.6", "protocol": 766},
                "players": {"online": 3, "max": 20},
                "description": {"text": "木桶测试服"},
            }
            body = json.dumps(info, ensure_ascii=False).encode("utf-8")
            payload = plugin.encode_varint(0) + plugin.encode_varint(len(body)) + body
            writer.write(plugin.encode_varint(len(payload)) + payload)
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def start_mock_panel() -> tuple[web.AppRunner, str]:
    calls = {"count": 0}

    async def server_detail(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "object": "server",
                "attributes": {
                    "identifier": "abc12345",
                    "name": "木桶测试服",
                    "status": "running",
                    "limits": {"memory": 4096, "disk": 10240, "cpu": 200},
                    "relationships": {
                        "allocations": {
                            "object": "list",
                            "data": [
                                {
                                    "object": "allocation",
                                    "attributes": {
                                        "ip": "127.0.0.1",
                                        "ip_alias": None,
                                        "port": 25565,
                                        "is_default": True,
                                    },
                                }
                            ],
                        }
                    },
                },
            }
        )

    async def resources(_request: web.Request) -> web.Response:
        calls["count"] += 1
        step = calls["count"] - 1
        return web.json_response(
            {
                "object": "stats",
                "attributes": {
                    "current_state": "running",
                    "is_suspended": False,
                    "resources": {
                        "memory_bytes": 1024 ** 3,
                        "cpu_absolute": 25.5,
                        "disk_bytes": 2 * 1024 ** 3,
                        "network_rx_bytes": 1000 + step * 2000,
                        "network_tx_bytes": 500 + step * 1000,
                        "uptime": 90061,
                    },
                },
            }
        )

    app = web.Application()
    app.router.add_get("/api/client/servers/abc12345", server_detail)
    app.router.add_get("/api/client/servers/abc12345/resources", resources)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    return runner, f"http://127.0.0.1:{port}"


async def main() -> None:
    # 1. VarInt 编解码
    for value in (0, 1, 127, 128, 255, 2147483647):
        encoded = plugin.encode_varint(value)
        decoded, offset = plugin.decode_varint_from(encoded, 0)
        assert decoded == value and offset == len(encoded), value

    # 2. 地址解析
    assert plugin.parse_address("play.example.com:25566") == ("play.example.com", 25566)
    assert plugin.parse_address("play.example.com") == ("play.example.com", 25565)
    assert plugin.parse_address("[::1]:25565") == ("::1", 25565)

    mc_server, mc_port = await start_mock_minecraft()
    panel_runner, panel_base = await start_mock_panel()

    try:
        # 3. SLP 查询
        ping = await plugin.slp_query("127.0.0.1", mc_port, timeout=3)
        assert ping["online"] == 3 and ping["max"] == 20, ping
        assert ping["version"] == "Paper 1.20.6", ping
        assert ping["motd"] == "木桶测试服", ping

        # 4. 插件采集链路（mock 面板 + mock MC）
        config = {
            "enabled": False,
            "panel_base_url": panel_base,
            "api_key": "ptlc_test",
            "server_id": "abc12345",
            "server_address": f"127.0.0.1:{mc_port}",
            "network_sample_interval": 0.5,
            "request_timeout": 5,
            "verify_ssl": False,
            "console_fallback": False,
        }
        context = SimpleNamespace(get_config=lambda: {})
        instance = plugin.MutongStatusPlugin(context, config)

        # 5. QQ 官方主动消息：登记群场景；@全体只对 aiocqhttp 生效
        qq_official = FakePlatform("qq_official", "qq_official")
        qq_webhook = FakePlatform("qq_official_webhook", "qq_webhook")
        aiocqhttp = FakePlatform("aiocqhttp", "aiocqhttp")
        instance.context = SimpleNamespace(
            get_config=lambda: {},
            platform_manager=SimpleNamespace(
                platform_insts=[qq_official, qq_webhook, aiocqhttp]
            ),
        )
        assert instance._prepare_target("qq_official:GroupMessage:ABC123") is False
        assert qq_official.scene_calls == [("ABC123", "group")]
        assert instance._prepare_target("qq_webhook:GroupMessage:XYZ789") is False
        assert qq_webhook.scene_calls == [("XYZ789", "group")]
        assert instance._prepare_target("aiocqhttp:GroupMessage:123456") is True
        assert aiocqhttp.scene_calls == []

        status = await instance._collect_status()
        assert status.players == 3 and status.max_players == 20, status
        assert abs(status.cpu_absolute - 25.5) < 0.001, status
        assert abs(status.memory_bytes - 1024 ** 3) < 1, status
        assert abs(status.rx_rate - 4000) < 1, status
        assert abs(status.tx_rate - 2000) < 1, status
        assert status.uptime_s == 90061, status

        report = await instance.build_report()
        print(report)
        assert "在线玩家：3 / 20" in report
        assert "CPU：25.5%" in report
        assert "内存：1.00 GiB / 4.00 GiB（25.0%）" in report
        assert "网络：↓ 3.91 KiB/s" in report
        assert "延迟：" in report

        await instance.terminate()
    finally:
        mc_server.close()
        await mc_server.wait_closed()
        await panel_runner.cleanup()

    print("\n✅ 全部自测通过")


if __name__ == "__main__":
    asyncio.run(main())
