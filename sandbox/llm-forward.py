"""Forward 127.0.0.1:8080 inside the sandbox to the watchdog's llm proxy.

The sandbox has no network; the only way out is the unix socket the
watchdog mounts at /run/llm/llm.sock. The proxy behind it only passes
chat-completion requests to llama-server and adds the API key itself.
"""
import asyncio

SOCK = "/run/llm/llm.sock"


async def pump(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def handle(client_r, client_w):
    try:
        up_r, up_w = await asyncio.open_unix_connection(SOCK)
    except OSError:
        client_w.close()
        return
    await asyncio.gather(pump(client_r, up_w), pump(up_r, client_w))


async def main():
    server = await asyncio.start_server(handle, "127.0.0.1", 8080)
    async with server:
        await server.serve_forever()


asyncio.run(main())
