import asyncio
import base64
import hashlib
import os
import ssl
import struct
from typing import Any
from urllib.parse import urlsplit


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class NativeWebSocketConnection:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, mask_outgoing: bool) -> None:
        self.reader = reader
        self.writer = writer
        self.mask_outgoing = mask_outgoing

    @classmethod
    async def accept(
        cls,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        access_token: str | None = None,
    ) -> "NativeWebSocketConnection":
        request = await reader.readuntil(b"\r\n\r\n")
        lines = request.decode("iso-8859-1").split("\r\n")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()

        if not lines or not lines[0].startswith("GET "):
            raise ConnectionError("Invalid WebSocket request")
        if headers.get("upgrade", "").lower() != "websocket":
            raise ConnectionError("Missing WebSocket upgrade header")
        if access_token and headers.get("authorization") != f"Bearer {access_token}":
            raise PermissionError("Invalid reverse WebSocket access token")

        key = headers.get("sec-websocket-key")
        if not key:
            raise ConnectionError("Missing Sec-WebSocket-Key header")

        accept = base64.b64encode(hashlib.sha1(f"{key}{WS_GUID}".encode("ascii")).digest()).decode("ascii")
        response = "\r\n".join(
            [
                "HTTP/1.1 101 Switching Protocols",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Accept: {accept}",
                "",
                "",
            ]
        )
        writer.write(response.encode("ascii"))
        await writer.drain()
        return cls(reader, writer, mask_outgoing=False)

    async def recv(self) -> str | bytes:
        while True:
            opcode, payload = await self._read_frame()
            if opcode == 0x1:
                return payload.decode("utf-8")
            if opcode == 0x2:
                return payload
            if opcode == 0x8:
                raise ConnectionError("WebSocket closed by peer")
            if opcode == 0x9:
                await self._send_frame(0xA, payload)
            elif opcode == 0xA:
                continue
            else:
                raise ConnectionError(f"Unsupported WebSocket opcode: {opcode}")

    async def send(self, text: str) -> None:
        await self._send_frame(0x1, text.encode("utf-8"))

    async def close(self) -> None:
        try:
            await self._send_frame(0x8, b"")
        except Exception:
            pass
        self.writer.close()
        await self.writer.wait_closed()

    async def _read_frame(self) -> tuple[int, bytes]:
        first, second = await self.reader.readexactly(2)
        opcode = first & 0x0F
        masked = second & 0x80
        length = second & 0x7F

        if length == 126:
            length = struct.unpack("!H", await self.reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await self.reader.readexactly(8))[0]

        mask = await self.reader.readexactly(4) if masked else b""
        payload = await self.reader.readexactly(length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        first = 0x80 | opcode
        length = len(payload)
        mask_bit = 0x80 if self.mask_outgoing else 0
        if length < 126:
            header = struct.pack("!BB", first, mask_bit | length)
        elif length < 65536:
            header = struct.pack("!BBH", first, mask_bit | 126, length)
        else:
            header = struct.pack("!BBQ", first, mask_bit | 127, length)

        if self.mask_outgoing:
            mask = os.urandom(4)
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            self.writer.write(header + mask + payload)
        else:
            self.writer.write(header + payload)
        await self.writer.drain()


class NativeWebSocketClient:
    def __init__(self, url: str, headers: dict[str, str] | None = None) -> None:
        self.url = url
        self.headers = headers or {}
        self.connection: NativeWebSocketConnection | None = None

    async def __aenter__(self) -> "NativeWebSocketClient":
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def connect(self) -> None:
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"ws", "wss"}:
            raise ValueError("NAPCAT_WS_URL must start with ws:// or wss://")

        host = parsed.hostname
        if not host:
            raise ValueError("NAPCAT_WS_URL must include a host")

        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        ssl_context = ssl.create_default_context() if parsed.scheme == "wss" else None
        reader, writer = await asyncio.open_connection(host, port, ssl=ssl_context)

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        host_header = host if parsed.port is None else f"{host}:{port}"
        request_headers = {
            "Host": host_header,
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Key": key,
            "Sec-WebSocket-Version": "13",
            **self.headers,
        }
        request = [f"GET {path} HTTP/1.1", *[f"{name}: {value}" for name, value in request_headers.items()], "", ""]

        writer.write("\r\n".join(request).encode("ascii"))
        await writer.drain()
        response = await reader.readuntil(b"\r\n\r\n")
        self._validate_handshake(response, key)
        self.connection = NativeWebSocketConnection(reader, writer, mask_outgoing=True)

    async def recv(self) -> str | bytes:
        if not self.connection:
            raise ConnectionError("WebSocket is not connected")
        return await self.connection.recv()

    async def send(self, text: str) -> None:
        if not self.connection:
            raise ConnectionError("WebSocket is not connected")
        await self.connection.send(text)

    async def close(self) -> None:
        if self.connection:
            await self.connection.close()
            self.connection = None

    def _validate_handshake(self, response: bytes, key: str) -> None:
        lines = response.decode("iso-8859-1").split("\r\n")
        if not lines or " 101 " not in lines[0]:
            raise ConnectionError(f"WebSocket handshake failed: {lines[0] if lines else '<empty>'}")

        headers = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()

        expected = base64.b64encode(hashlib.sha1(f"{key}{WS_GUID}".encode("ascii")).digest()).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise ConnectionError("WebSocket handshake accept header mismatch")
