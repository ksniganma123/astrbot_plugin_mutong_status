# -*- coding: utf-8 -*-
"""AstrBot 插件：木桶面板（Pterodactyl）服务器状态日报。

功能：
1. 每天在指定时间，把服务器状态推送到指定 QQ 群：
   - 当前在线玩家数 / 最大人数
   - CPU 占用率（Pterodactyl 的 cpu_absolute，100% = 1 核）
   - 内存占用（已用 / 上限）
   - 网络状态（上下行实时速率 + 到 MC 服务端的延迟）
   - 附加：硬盘占用、运行时长、服务端版本 / MOTD
2. 手动命令：
   - /mc状态            立即查询并回复当前会话
   - /mc推送 绑定       把当前群设为推送目标（管理员）
   - /mc推送 解绑       取消当前群的推送（管理员）
   - /mc推送 列表       查看推送目标（管理员）
   - /mc推送 测试       立即向所有目标群推送一次（管理员）

数据来源：
- CPU / 内存 / 硬盘 / 网络流量：面板的 Pterodactyl Client API
  GET {面板地址}/api/client/servers/{服务器标识}/resources
- 在线人数 / 延迟 / 版本 / MOTD：直接对 MC 服务端做 Server List Ping（SLP）
- 兜底：SLP 失败时，可选用面板 WebSocket 控制台执行 list 命令解析人数

平台：支持 aiocqhttp（NapCat / Lagrange 等），以及 QQ 官方机器人
（qq_official / qq_official_webhook）的群主动消息。

依赖：aiohttp（AstrBot 自带）；websockets（可选，仅控制台兜底用，AstrBot 自带）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

try:  # websockets 只在“控制台兜底”时用到，缺失时自动降级
    import websockets
except Exception:  # pragma: no cover
    websockets = None


PLUGIN_ID = "astrbot_plugin_mutong_status"
KV_BOUND_UMOS = "bound_umos"
DEFAULT_PANEL = "https://vat-panel.mutong1.com"
DEFAULT_TITLE = "🎮 木桶面板 · 服务器日报"
QQ_OFFICIAL_PLATFORMS = {"qq_official", "qq_official_webhook"}
AT_ALL_PLATFORMS = {"aiocqhttp"}


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


class PanelError(RuntimeError):
    """面板接口调用失败。"""


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt_bytes(num: float | int | None) -> str:
    if num is None:
        return "—"
    value = float(num)
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    index = 0
    while abs(value) >= 1024 and index < len(units) - 1:
        value /= 1024.0
        index += 1
    if index == 0:
        return f"{int(value)} {units[index]}"
    return f"{value:.2f} {units[index]}"


def _fmt_rate(num: float | int | None) -> str:
    if num is None:
        return "—"
    return f"{_fmt_bytes(num)}/s"


def _fmt_duration(seconds: float | int | None) -> str:
    total = int(seconds or 0)
    if total <= 0:
        return "—"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小时")
    if minutes and not days:
        parts.append(f"{minutes} 分钟")
    return " ".join(parts) or "不到 1 分钟"


def _latency_label(ms: float | None) -> str:
    if ms is None:
        return "—"
    if ms < 60:
        return "优秀"
    if ms < 120:
        return "良好"
    if ms < 250:
        return "一般"
    return "较差"


STATE_LABELS = {
    "running": "🟢 运行中",
    "starting": "🟡 启动中",
    "stopping": "🟠 停止中",
    "offline": "🔴 已停止",
    "stopped": "🔴 已停止",
    "suspended": "⛔ 已暂停",
    "installing": "🔵 安装中",
    "install_failed": "❌ 安装失败",
    "restoring_backup": "🟠 恢复备份中",
    "transferring": "🔵 迁移中",
}


def _state_label(state: str | None) -> str:
    if not state:
        return "❔ 未知"
    return STATE_LABELS.get(state, f"❔ {state}")


def parse_push_time(raw: str | None, default: tuple[int, int] = (21, 0)) -> tuple[int, int]:
    """解析 HH:MM，非法时返回默认值。"""
    text = str(raw or "").strip()
    match = re.fullmatch(r"(\d{1,2})[:：](\d{1,2})", text)
    if not match:
        return default
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return default
    return hour, minute


def parse_address(raw: str | None) -> tuple[str, int] | None:
    """解析 host:port，支持 IPv6（[::1]:25565）。"""
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        port_text = rest[1:] if rest.startswith(":") else ""
        if not host:
            return None
        return host, int(port_text) if port_text.isdigit() else 25565
    if text.count(":") == 1:
        host, port_text = text.rsplit(":", 1)
        if host and port_text.isdigit():
            return host, int(port_text)
        return None
    return text, 25565  # 不含端口，或裸 IPv6


# ---------------------------------------------------------------------------
# Minecraft Server List Ping（无第三方依赖实现）
# ---------------------------------------------------------------------------


def encode_varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 32
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def decode_varint_from(buf: bytes, offset: int = 0) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if offset >= len(buf):
            raise ValueError("VarInt 数据不完整")
        byte = buf[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7
        if shift > 35:
            raise ValueError("VarInt 过长")


async def read_varint(reader: asyncio.StreamReader) -> int:
    result = 0
    shift = 0
    while True:
        byte = (await reader.readexactly(1))[0]
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result
        shift += 7
        if shift > 35:
            raise ValueError("VarInt 过长")


def encode_string(text: str) -> bytes:
    data = text.encode("utf-8")
    return encode_varint(len(data)) + data


_MOTD_COLOR_RE = re.compile(r"§[0-9a-fk-or]", re.IGNORECASE)


def _flatten_chat(component: Any) -> str:
    """把聊天组件（str / list / dict）拍平成纯文本。"""
    if component is None:
        return ""
    if isinstance(component, str):
        return component
    if isinstance(component, (int, float, bool)):
        return str(component)
    if isinstance(component, list):
        return "".join(_flatten_chat(item) for item in component)
    if isinstance(component, dict):
        parts: list[str] = []
        if "text" in component:
            parts.append(_flatten_chat(component["text"]))
        if "translate" in component and not parts:
            parts.append(str(component["translate"]))
        if "extra" in component:
            parts.append(_flatten_chat(component["extra"]))
        return "".join(parts)
    return ""


def _clean_motd(description: Any, limit: int = 100) -> str:
    text = _MOTD_COLOR_RE.sub("", _flatten_chat(description))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


async def slp_query(host: str, port: int, timeout: float = 8.0) -> dict[str, Any]:
    """对 Java 版 MC 服务端做 Server List Ping，返回人数、延迟、版本和 MOTD。"""
    start = time.perf_counter()
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, int(port)),
        timeout=timeout,
    )
    connect_ms = (time.perf_counter() - start) * 1000
    try:
        handshake = (
            encode_varint(0x00)
            + encode_varint(47)  # 1.8 协议号，服务端只需要能识别即可
            + encode_string(host)
            + struct.pack(">H", int(port) & 0xFFFF)
            + encode_varint(1)  # next state = status
        )
        writer.write(encode_varint(len(handshake)) + handshake)
        writer.write(encode_varint(1) + encode_varint(0x00))
        await writer.drain()

        request_at = time.perf_counter()
        length = await asyncio.wait_for(read_varint(reader), timeout=timeout)
        data = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
        latency_ms = (time.perf_counter() - request_at) * 1000

        _, offset = decode_varint_from(data, 0)  # packet id
        str_len, offset = decode_varint_from(data, offset)
        info = json.loads(data[offset : offset + str_len].decode("utf-8", "replace"))
        players = info.get("players") or {}
        version = info.get("version") or {}
        return {
            "online": int(_to_float(players.get("online"))),
            "max": int(_to_float(players.get("max"))),
            "version": str(version.get("name") or ""),
            "motd": _clean_motd(info.get("description")),
            "latency_ms": round(latency_ms, 1),
            "connect_ms": round(connect_ms, 1),
        }
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def tcp_ping(host: str, port: int, timeout: float = 5.0) -> float:
    """只做一次 TCP 握手，返回毫秒延迟（服务器关闭状态查询时的兜底）。"""
    start = time.perf_counter()
    _, writer = await asyncio.wait_for(
        asyncio.open_connection(host, int(port)),
        timeout=timeout,
    )
    elapsed = (time.perf_counter() - start) * 1000
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return round(elapsed, 1)


# ---------------------------------------------------------------------------
# 木桶面板 / Pterodactyl Client API
# ---------------------------------------------------------------------------


def extract_primary_address(server_attributes: dict[str, Any]) -> tuple[str, int] | None:
    """从服务器详情里取默认分配地址（优先 ip_alias）。"""
    relationships = server_attributes.get("relationships") or {}
    allocations = (relationships.get("allocations") or {}).get("data") or []
    chosen: dict[str, Any] | None = None
    for item in allocations:
        attributes = item.get("attributes") if isinstance(item, dict) else None
        if not attributes:
            continue
        if attributes.get("is_default"):
            chosen = attributes
            break
        if chosen is None:
            chosen = attributes
    if not chosen:
        return None
    host = str(chosen.get("ip_alias") or chosen.get("ip") or "").strip()
    if not host:
        return None
    port = int(_to_float(chosen.get("port"), 25565)) or 25565
    return host, port


def extract_resources(payload: dict[str, Any]) -> dict[str, Any]:
    """解析 /resources 返回。"""
    attributes = payload.get("attributes") if isinstance(payload, dict) else None
    if not isinstance(attributes, dict):
        attributes = payload if isinstance(payload, dict) else {}
    resources = attributes.get("resources") or {}
    return {
        "state": str(attributes.get("current_state") or ""),
        "suspended": bool(attributes.get("is_suspended")),
        "memory_bytes": _to_float(resources.get("memory_bytes")),
        "cpu_absolute": _to_float(resources.get("cpu_absolute")),
        "disk_bytes": _to_float(resources.get("disk_bytes")),
        "rx_bytes": _to_float(resources.get("network_rx_bytes")),
        "tx_bytes": _to_float(resources.get("network_tx_bytes")),
        "uptime": _to_float(resources.get("uptime")),
    }


class PterodactylClient:
    """Pterodactyl（翼龙）Client API 的最小封装，木桶面板直接适用。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        server_id: str,
        *,
        timeout: float = 10.0,
        verify_ssl: bool = True,
    ) -> None:
        self.base_url = (base_url or DEFAULT_PANEL).strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.server_id = (server_id or "").strip()
        self.timeout = max(3.0, float(timeout))
        self.verify_ssl = bool(verify_ssl)
        self._session: aiohttp.ClientSession | None = None
        self._detail_cache: dict[str, Any] | None = None
        self._detail_cached_at = 0.0

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "AstrBot-MutongStatus/1.0",
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=None if self.verify_ssl else False)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get(self, path: str) -> dict[str, Any]:
        if not self.api_key:
            raise PanelError("未配置面板 API 密钥")
        if not self.server_id:
            raise PanelError("未配置服务器标识符")
        session = await self._get_session()
        url = f"{self.base_url}/api/client{path}"
        async with session.get(url, headers=self.headers) as resp:
            text = await resp.text()
            if resp.status == 401:
                raise PanelError("面板 API 密钥无效或已过期（HTTP 401）")
            if resp.status == 403:
                raise PanelError("API 密钥没有访问该服务器的权限（HTTP 403）")
            if resp.status == 404:
                raise PanelError("面板上找不到该服务器（HTTP 404），请检查服务器标识符")
            if resp.status >= 400:
                raise PanelError(f"面板接口错误 HTTP {resp.status}：{text[:160]}")
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise PanelError("面板返回内容不是 JSON，可能被防火墙或登录页拦截") from exc

    async def get_server(self, *, use_cache: bool = True) -> dict[str, Any]:
        now = time.monotonic()
        if use_cache and self._detail_cache and now - self._detail_cached_at < 300:
            return self._detail_cache
        payload = await self._get(f"/servers/{self.server_id}")
        self._detail_cache = payload
        self._detail_cached_at = now
        return payload

    async def get_resources(self) -> dict[str, Any]:
        return extract_resources(await self._get(f"/servers/{self.server_id}/resources"))

    async def websocket_player_count(self, timeout: float | None = None) -> tuple[int, int] | None:
        """通过面板 WebSocket 控制台执行 list，解析在线人数（兜底方案）。"""
        if websockets is None:
            return None
        timeout = float(timeout or self.timeout)
        payload = (await self._get(f"/servers/{self.server_id}/websocket")).get("data") or {}
        token = payload.get("token")
        socket_url = payload.get("socket")
        if not token or not socket_url:
            return None

        kwargs: dict[str, Any] = {
            "open_timeout": timeout,
            "close_timeout": 3,
            "max_size": 1 << 20,
            "ping_interval": None,
        }
        try:
            connector = websockets.connect(
                socket_url,
                additional_headers={"Origin": self.base_url},
                **kwargs,
            )
        except TypeError:  # 旧版 websockets 的写法
            connector = websockets.connect(socket_url, origin=self.base_url, **kwargs)

        patterns = (
            re.compile(r"There are (\d+)\s+of a max(?:imum)? of (\d+) players online", re.I),
            re.compile(r"There are (\d+)\s+out of a maximum of (\d+) players online", re.I),
            re.compile(r"There are (\d+)\s+out of (?:a )?maximum (?:of )?(\d+) players online", re.I),
        )
        deadline = time.monotonic() + timeout
        async with connector as ws:
            await ws.send(json.dumps({"event": "auth", "args": [token]}))
            while time.monotonic() < deadline:
                message = json.loads(
                    await asyncio.wait_for(
                        ws.recv(),
                        timeout=max(0.2, deadline - time.monotonic()),
                    )
                )
                event = message.get("event")
                if event in ("auth success", "jwt error", "token expired"):
                    break
            await ws.send(json.dumps({"event": "send command", "args": ["list"]}))
            while time.monotonic() < deadline:
                message = json.loads(
                    await asyncio.wait_for(
                        ws.recv(),
                        timeout=max(0.2, deadline - time.monotonic()),
                    )
                )
                if message.get("event") != "console output":
                    continue
                line = "".join(str(part) for part in (message.get("args") or []))
                for pattern in patterns:
                    match = pattern.search(line)
                    if match:
                        return int(match.group(1)), int(match.group(2))
        return None


# ---------------------------------------------------------------------------
# 状态数据与文本报告
# ---------------------------------------------------------------------------


@dataclass
class ServerStatus:
    server_name: str = ""
    state: str = ""
    players: int | None = None
    max_players: int | None = None
    cpu_absolute: float | None = None
    cpu_limit: float | None = None
    memory_bytes: float | None = None
    memory_limit_mb: float | None = None
    disk_bytes: float | None = None
    disk_limit_mb: float | None = None
    rx_rate: float | None = None
    tx_rate: float | None = None
    latency_ms: float | None = None
    version: str = ""
    motd: str = ""
    uptime_s: float | None = None
    address: str = ""
    network_note: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def is_running(self) -> bool:
        return self.state in ("", "running", "starting", "stopping")


def format_report(
    status: ServerStatus,
    *,
    title: str = DEFAULT_TITLE,
    include_disk: bool = True,
    include_uptime: bool = True,
    include_motd: bool = True,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now()
    lines = [title, f"🕐 {now:%Y-%m-%d %H:%M}"]
    if status.server_name:
        lines.append(f"🏷️ 服务器：{status.server_name}")
    lines.append(f"📊 状态：{_state_label(status.state)}")

    if status.players is not None:
        if status.max_players:
            lines.append(f"👥 在线玩家：{status.players} / {status.max_players}")
        else:
            lines.append(f"👥 在线玩家：{status.players}")
    else:
        lines.append("👥 在线玩家：未获取到")

    if status.is_running:
        if status.cpu_absolute is not None:
            cpu_text = f"🖥️ CPU：{status.cpu_absolute:.1f}%"
            if status.cpu_limit and status.cpu_limit > 0:
                cpu_text += (
                    f" / {status.cpu_limit:.0f}%"
                    f"（{status.cpu_absolute / 100:.2f} / {status.cpu_limit / 100:.2f} 核）"
                )
            else:
                cpu_text += f"（{status.cpu_absolute / 100:.2f} 核）"
            lines.append(cpu_text)

        if status.memory_bytes is not None:
            memory_text = f"🧠 内存：{_fmt_bytes(status.memory_bytes)}"
            if status.memory_limit_mb and status.memory_limit_mb > 0:
                limit_bytes = status.memory_limit_mb * 1024 * 1024
                percent = status.memory_bytes / limit_bytes * 100 if limit_bytes else 0
                memory_text += f" / {_fmt_bytes(limit_bytes)}（{percent:.1f}%）"
            lines.append(memory_text)

        if include_disk and status.disk_bytes is not None:
            disk_text = f"💾 硬盘：{_fmt_bytes(status.disk_bytes)}"
            if status.disk_limit_mb and status.disk_limit_mb > 0:
                limit_bytes = status.disk_limit_mb * 1024 * 1024
                percent = status.disk_bytes / limit_bytes * 100 if limit_bytes else 0
                disk_text += f" / {_fmt_bytes(limit_bytes)}（{percent:.1f}%）"
            lines.append(disk_text)
    elif status.state:
        lines.append("🖥️ CPU / 🧠 内存：服务器未运行")

    if status.rx_rate is not None or status.tx_rate is not None:
        lines.append(
            f"🌐 网络：↓ {_fmt_rate(status.rx_rate)}  ↑ {_fmt_rate(status.tx_rate)}"
        )
    if status.latency_ms is not None:
        lines.append(f"📶 延迟：{status.latency_ms:.0f} ms（{_latency_label(status.latency_ms)}）")

    if include_uptime and status.uptime_s and status.uptime_s > 0:
        lines.append(f"⏱️ 运行时长：{_fmt_duration(status.uptime_s)}")

    if include_motd:
        if status.version:
            lines.append(f"🧩 版本：{status.version}")
        if status.motd:
            lines.append(f"📝 MOTD：{status.motd}")

    if status.errors:
        unique_errors = list(dict.fromkeys(str(item) for item in status.errors if item))
        if unique_errors:
            lines.append("⚠️ " + "；".join(unique_errors))
    if status.network_note:
        lines.append(f"ℹ️ {status.network_note}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------


@register(
    PLUGIN_ID,
    "K5NGM",
    "木桶面板（Pterodactyl）服务器状态日报：每日定时在指定群推送在线人数、CPU、内存、网络状态",
    "1.1.0",
    "https://github.com/ksniganma123/astrbot_plugin_mutong_status",
)
class MutongStatusPlugin(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self._client: PterodactylClient | None = None
        self._scheduler_task: asyncio.Task | None = None
        self._query_lock = asyncio.Lock()

    # ---------------------------- 配置读取 ----------------------------

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _cfg_bool(self, key: str, default: bool = False) -> bool:
        value = self._cfg(key, default)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是")
        return bool(value)

    def _cfg_float(self, key: str, default: float = 0.0) -> float:
        return _to_float(self._cfg(key, default), default)

    def _get_tz(self) -> ZoneInfo:
        name = str(self._cfg("timezone", "Asia/Shanghai") or "Asia/Shanghai").strip()
        try:
            return ZoneInfo(name)
        except Exception:
            try:
                return ZoneInfo("Asia/Shanghai")
            except Exception:  # pragma: no cover
                return datetime.now().astimezone().tzinfo  # type: ignore[return-value]

    def _get_client(self) -> PterodactylClient | None:
        api_key = str(self._cfg("api_key", "") or "").strip()
        server_id = str(self._cfg("server_id", "") or "").strip()
        if not api_key or not server_id:
            return None
        if self._client is None:
            self._client = PterodactylClient(
                base_url=str(self._cfg("panel_base_url", DEFAULT_PANEL) or DEFAULT_PANEL),
                api_key=api_key,
                server_id=server_id,
                timeout=self._cfg_float("request_timeout", 10.0),
                verify_ssl=self._cfg_bool("verify_ssl", True),
            )
        return self._client

    # ---------------------------- 生命周期 ----------------------------

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """AstrBot 加载完成后启动每日定时任务。"""
        if not self._cfg_bool("enabled", True):
            self.logger.info("[木桶面板] 每日推送未启用（enabled=false）")
            return
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())
            self.logger.info("[木桶面板] 每日推送任务已启动")

    async def terminate(self):
        """插件卸载 / 重载时清理资源。"""
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scheduler_task
            self._scheduler_task = None
        if self._client is not None:
            await self._client.close()
            self._client = None
        self.logger.info("[木桶面板] 插件已卸载")

    async def _scheduler_loop(self):
        while True:
            try:
                hour, minute = parse_push_time(str(self._cfg("push_time", "21:00")))
                tz = self._get_tz()
                now = datetime.now(tz)
                target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
                delay = (target - now).total_seconds()
                self.logger.info(
                    f"[木桶面板] 下次推送：{target:%Y-%m-%d %H:%M}（{delay / 3600:.2f} 小时后）"
                )
                await asyncio.sleep(delay)
                if self._cfg_bool("enabled", True):
                    await self._push_report()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 网络抖动等异常不应让任务退出
                self.logger.error(f"[木桶面板] 定时推送异常：{exc}")
                await asyncio.sleep(60)

    # ---------------------------- 推送目标 ----------------------------

    def _config_targets(self) -> list[str]:
        raw = self._cfg("target_groups", []) or []
        if isinstance(raw, str):
            raw = [part for part in re.split(r"[,，\s]+", raw) if part.strip()]
        default_platform = (
            str(self._cfg("default_platform", "aiocqhttp") or "aiocqhttp").strip() or "aiocqhttp"
        )
        targets: list[str] = []
        for item in raw:
            text = str(item).strip()
            if not text:
                continue
            if text.count(":") >= 2:
                targets.append(text)
            elif text.count(":") == 1:
                platform, session_id = text.split(":", 1)
                targets.append(f"{platform}:GroupMessage:{session_id}")
            else:
                targets.append(f"{default_platform}:GroupMessage:{text}")
        return targets

    async def _bound_targets(self) -> list[str]:
        data = await self.get_kv_data(KV_BOUND_UMOS, [])
        if not isinstance(data, list):
            return []
        return [str(item) for item in data if str(item).strip()]

    async def _all_targets(self) -> list[str]:
        result: list[str] = []
        for umo in self._config_targets() + await self._bound_targets():
            if umo not in result:
                result.append(umo)
        return result

    async def _push_report(self) -> tuple[int, int]:
        targets = await self._all_targets()
        if not targets:
            self.logger.warning("[木桶面板] 没有配置推送目标群，本次推送已跳过")
            return 0, 0
        report = await self.build_report()
        sent_ok = 0
        for umo in targets:
            try:
                allow_at_all = self._prepare_target(umo)
                chain = MessageChain()
                if self._cfg_bool("mention_all", False) and allow_at_all:
                    chain.at_all().message("\n")
                chain.message(report)
                if await self.context.send_message(umo, chain):
                    sent_ok += 1
                else:
                    self.logger.warning(f"[木桶面板] 未找到平台实例：{umo}")
            except Exception as exc:
                self.logger.error(f"[木桶面板] 推送到 {umo} 失败：{exc}")
        self.logger.info(f"[木桶面板] 推送完成：{sent_ok}/{len(targets)}")
        return sent_ok, len(targets)

    # ------------------------- 平台适配（QQ 官方主动消息） -------------------------

    def _find_platform(self, umo: str):
        """按 UMO 的平台 ID 找到平台适配器实例，返回 (实例, meta)。"""
        try:
            platform_id = umo.split(":", 1)[0]
        except Exception:
            return None
        try:
            platforms = self.context.platform_manager.platform_insts
        except Exception:
            return None
        for platform in platforms:
            try:
                meta = platform.meta()
            except Exception:
                continue
            if str(getattr(meta, "id", "") or "") == platform_id:
                return platform, meta
        return None

    def _prepare_target(self, umo: str) -> bool:
        """发送前的平台适配，返回是否允许使用 @全体成员。

        QQ 官方适配器只有在收到过该群的消息后，才会把会话登记为 group；
        如果 AstrBot 重启后群里一直没人说话，主动推送会被适配器静默跳过。
        这里在发送前主动登记一次群场景，保证每日定时推送能正常发出。
        """
        found = self._find_platform(umo)
        if not found:
            return ":GroupMessage:" in umo
        platform, meta = found
        name = str(getattr(meta, "name", "") or "")
        parts = umo.split(":", 2)

        if name in QQ_OFFICIAL_PLATFORMS and len(parts) == 3 and parts[1] == "GroupMessage":
            remember_scene = getattr(platform, "remember_session_scene", None)
            if callable(remember_scene):
                try:
                    remember_scene(parts[2], "group")
                except Exception as exc:
                    self.logger.warning(f"[木桶面板] 登记 QQ 官方群会话失败：{exc}")
            if not bool(getattr(meta, "support_proactive_message", True)):
                self.logger.warning(
                    f"[木桶面板] 平台 {name} 声明不支持主动消息，推送可能被 QQ 拒绝"
                )

        return name in AT_ALL_PLATFORMS

    # ---------------------------- 数据采集 ----------------------------

    async def _collect_status(self) -> ServerStatus:
        status = ServerStatus()
        client = self._get_client()
        detail_attributes: dict[str, Any] = {}

        if client is not None:
            try:
                detail = await client.get_server()
                detail_attributes = detail.get("attributes") or detail or {}
                status.server_name = str(detail_attributes.get("name") or "")
                status.state = str(detail_attributes.get("status") or "")
                limits = detail_attributes.get("limits") or {}
                status.cpu_limit = _to_float(limits.get("cpu"))
                status.memory_limit_mb = _to_float(limits.get("memory"))
                status.disk_limit_mb = _to_float(limits.get("disk"))
                address = extract_primary_address(detail_attributes)
                if address:
                    status.address = f"{address[0]}:{address[1]}"
            except Exception as exc:
                status.errors.append(str(exc))

        resources_error: Exception | None = None
        if client is not None and status.is_running:
            try:
                first = await client.get_resources()
                interval = min(max(self._cfg_float("network_sample_interval", 2.0), 0.5), 10.0)
                await asyncio.sleep(interval)
                second = await client.get_resources()
                status.state = second.get("state") or status.state
                if second.get("suspended"):
                    status.state = "suspended"
                status.cpu_absolute = second.get("cpu_absolute")
                status.memory_bytes = second.get("memory_bytes")
                status.disk_bytes = second.get("disk_bytes")
                status.uptime_s = second.get("uptime")
                if first.get("rx_bytes", 0) <= second.get("rx_bytes", 0):
                    status.rx_rate = (second["rx_bytes"] - first["rx_bytes"]) / interval
                if first.get("tx_bytes", 0) <= second.get("tx_bytes", 0):
                    status.tx_rate = (second["tx_bytes"] - first["tx_bytes"]) / interval
            except Exception as exc:
                resources_error = exc
                status.errors.append(f"资源接口：{exc}")

        if resources_error is None and client is None:
            status.errors.append(
                "未配置面板 API 密钥或服务器标识符，CPU / 内存 / 网络流量不可用"
            )

        # ---- 在线人数与延迟：优先直连 MC 服务端 ----
        address = parse_address(str(self._cfg("server_address", "") or "")) or parse_address(
            status.address
        )
        timeout = self._cfg_float("request_timeout", 10.0)
        players_known = False
        if address:
            host, port = address
            try:
                ping = await slp_query(host, port, timeout=timeout)
                status.players = ping["online"]
                status.max_players = ping["max"]
                status.version = ping["version"]
                status.motd = ping["motd"]
                status.latency_ms = ping["latency_ms"]
                players_known = True
            except Exception as exc:
                try:
                    status.latency_ms = await tcp_ping(host, port, timeout=min(timeout, 5.0))
                    status.network_note = f"服务器可能关闭了状态查询，延迟为 TCP 握手值（{exc}）"
                except Exception:
                    status.network_note = f"服务器端口无法连接：{exc}"
        else:
            status.network_note = "未配置服务器地址，且面板没有返回分配地址"

        if (
            not players_known
            and client is not None
            and self._cfg_bool("console_fallback", True)
            and status.is_running
        ):
            try:
                result = await client.websocket_player_count(timeout=min(timeout, 10.0))
                if result:
                    status.players, status.max_players = result
                    status.network_note = (status.network_note + "；" if status.network_note else "") + (
                        "人数来自面板控制台 list"
                    )
            except Exception as exc:
                status.errors.append(f"控制台兜底失败：{exc}")

        if not status.is_running and status.players is None:
            status.players = 0 if status.state in ("offline", "stopped", "install_failed", "suspended") else None
            status.max_players = status.max_players or (0 if status.players == 0 else None)

        return status

    async def build_report(self) -> str:
        async with self._query_lock:
            status = await self._collect_status()
            return format_report(
                status,
                title=str(self._cfg("report_title", DEFAULT_TITLE) or DEFAULT_TITLE),
                include_disk=self._cfg_bool("include_disk", True),
                include_uptime=self._cfg_bool("include_uptime", True),
                include_motd=self._cfg_bool("include_motd", True),
                now=datetime.now(self._get_tz()),
            )

    # ---------------------------- 命令 ----------------------------

    @filter.command("mc状态", alias={"mcstat", "服务器状态", "mc查询"})
    async def cmd_status(self, event: AstrMessageEvent):
        """立即查询服务器状态。"""
        yield event.plain_result(await self.build_report())

    @filter.command("mc推送")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_push(self, event: AstrMessageEvent):
        """管理每日推送目标群（仅管理员）。"""
        parts = (event.message_str or "").strip().split()
        sub = parts[-1].lower() if len(parts) > 1 else ""
        umo = event.unified_msg_origin
        bound = await self._bound_targets()

        if sub in ("绑定", "bind", "add"):
            if ":GroupMessage:" not in umo:
                yield event.plain_result("请在需要接收日报的群里发送这条命令。")
                return
            if umo not in bound:
                bound.append(umo)
                await self.put_kv_data(KV_BOUND_UMOS, bound)
            yield event.plain_result(
                f"✅ 已绑定本群\n📮 {umo}\n🕐 每天 {self._cfg('push_time', '21:00')} 推送"
            )
            return

        if sub in ("解绑", "unbind", "remove", "del"):
            if umo in bound:
                bound.remove(umo)
                await self.put_kv_data(KV_BOUND_UMOS, bound)
                yield event.plain_result("✅ 已取消本群的日报推送")
            else:
                yield event.plain_result("本群没有绑定定时推送。")
            return

        if sub in ("列表", "list", "status"):
            lines = ["📮 推送目标列表"]
            config_targets = self._config_targets()
            if config_targets:
                lines.append("配置文件：")
                lines.extend(f"  · {item}" for item in config_targets)
            if bound:
                lines.append("群内绑定：")
                lines.extend(f"  · {item}" for item in bound)
            if not config_targets and not bound:
                lines.append("  （暂无，去目标群发送 /mc推送 绑定）")
            lines.append(f"🕐 推送时间：每天 {self._cfg('push_time', '21:00')}（{self._cfg('timezone', 'Asia/Shanghai')}）")
            yield event.plain_result("\n".join(lines))
            return

        if sub in ("测试", "test", "now", "立即发送", "立即"):
            ok, total = await self._push_report()
            yield event.plain_result(f"📤 已尝试推送：成功 {ok} / 共 {total}")
            return

        yield event.plain_result(
            "用法：\n"
            "/mc状态 —— 立即查询并显示服务器状态\n"
            "/mc推送 绑定 —— 本群加入每日推送\n"
            "/mc推送 解绑 —— 本群取消推送\n"
            "/mc推送 列表 —— 查看推送目标\n"
            "/mc推送 测试 —— 立即推送一次"
        )
