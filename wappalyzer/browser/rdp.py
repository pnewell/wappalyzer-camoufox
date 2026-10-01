import asyncio
import json
import socket
import uuid


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class RemoteDebugger:
    """evaluates code in an extension's background page over firefox's remote debugging protocol"""

    def __init__(self, port):
        self.port = port
        self.reader = None
        self.writer = None

    async def connect(self, timeout=10):
        deadline = asyncio.get_running_loop().time() + timeout

        while True:
            try:
                self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
                await self._read()
                return
            except OSError:
                if asyncio.get_running_loop().time() > deadline:
                    raise RuntimeError(f"Firefox debugger server did not start on port {self.port}")
                await asyncio.sleep(0.25)

    async def close(self):
        if self.writer:
            self.writer.close()

    async def _read(self):
        length = int((await self.reader.readuntil(b":"))[:-1])
        return json.loads(await self.reader.readexactly(length))

    async def _send(self, to, type_, **kwargs):
        body = json.dumps({"to": to, "type": type_, **kwargs}).encode()
        self.writer.write(str(len(body)).encode() + b":" + body)
        await self.writer.drain()

    async def _request(self, to, type_, **kwargs):
        await self._send(to, type_, **kwargs)

        while True:
            message = await self._read()

            if message.get("from") == to:
                return message

    async def background_console(self, addon_id, timeout=10):
        deadline = asyncio.get_running_loop().time() + timeout
        addon = None

        while not addon:
            addons = await self._request("root", "listAddons")
            addon = next((a for a in addons.get("addons", []) if a.get("id") == addon_id), None)

            if not addon:
                if asyncio.get_running_loop().time() > deadline:
                    raise RuntimeError(f"Extension {addon_id} is not installed")
                await asyncio.sleep(0.25)

        watcher = (await self._request(addon["actor"], "getWatcher"))["actor"]
        await self._send(watcher, "watchTargets", targetType="frame")
        targets = []

        while True:
            message = await self._read()

            if message.get("from") != watcher:
                continue

            if message.get("type") == "target-available-form":
                targets.append(message["target"])
            elif "type" not in message:
                break

        for target in targets:
            if (target.get("url") or "").endswith("/_generated_background_page.html"):
                return target["consoleActor"]

        raise RuntimeError(f"Background page for {addon_id} not found")

    async def _evaluate(self, console, text):
        ack = await self._request(console, "evaluateJSAsync", text=text)

        if ack.get("error"):
            raise RuntimeError(ack.get("message") or ack["error"])

        while True:
            message = await self._read()

            if message.get("type") == "evaluationResult" and message.get("resultID") == ack.get("resultID"):
                break

        if message.get("exceptionMessage"):
            raise RuntimeError(message["exceptionMessage"])

        result = message.get("result")

        if isinstance(result, dict) and result.get("type") == "longString":
            result = (await self._request(result["actor"], "substring", start=0, end=result["length"]))["substring"]

        return result

    async def call(self, console, function, arg, timeout=10):
        key = f"__wappalyzer_{uuid.uuid4().hex}"
        await self._evaluate(
            console,
            f"globalThis.{key} = undefined; "
            f"Promise.resolve(({function})({json.dumps(arg)}))"
            f".then((value) => {{ globalThis.{key} = JSON.stringify({{ value }}) }},"
            f" (error) => {{ globalThis.{key} = JSON.stringify({{ error: String(error) }}) }})",
        )
        deadline = asyncio.get_running_loop().time() + timeout

        while True:
            result = await self._evaluate(console, f"globalThis.{key}")

            if isinstance(result, str):
                await self._evaluate(console, f"delete globalThis.{key}")
                result = json.loads(result)

                if "error" in result:
                    raise RuntimeError(result["error"])

                return result.get("value")

            if asyncio.get_running_loop().time() > deadline:
                raise RuntimeError("Timed out waiting for the extension")

            await asyncio.sleep(0.05)
