"""Real loopback relay checks, without requiring an SSH server."""

import asyncio
import sys
import unittest
from unittest.mock import patch

import click

from my_small_tools import scopy_tunnel


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers = []
        self.relays = []

    async def asyncTearDown(self):
        for server in self.servers:
            server.close()
            await server.wait_closed()
        for relay in self.relays:
            await relay.close()

    async def listen(self, handler):
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.servers.append(server)
        return server.sockets[0].getsockname()[1]

    async def test_binary_bytes_half_close_and_balancing(self):
        hits = [0, 0, 0, 0]
        arrived = asyncio.Event()
        finished = asyncio.Event()
        count = 0

        def backend(index):
            async def handle(reader, writer):
                nonlocal count
                try:
                    hits[index] += 1
                    count += 1
                    if count == 4:
                        arrived.set()
                    # Respond only after all clients are open and have half-closed.
                    await arrived.wait()
                    payload = await reader.read()
                    writer.write(payload)
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()
                    count -= 1
                    if count == 0:
                        finished.set()
            return handle

        ports = [await self.listen(backend(index)) for index in range(4)]
        relay = scopy_tunnel.TunnelRelay(ports)
        self.relays.append(relay)
        port = await self.listen(relay.handle)
        payload = bytes(range(256)) * 4097

        async def client():
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                writer.write(payload)
                await writer.drain()
                writer.write_eof()
                self.assertEqual(await reader.read(), payload)
            finally:
                writer.close()
                await writer.wait_closed()

        await asyncio.wait_for(asyncio.gather(*(client() for _ in range(4))), timeout=10)
        await asyncio.wait_for(finished.wait(), timeout=2)
        self.assertEqual(hits, [1, 1, 1, 1])

    async def test_failed_backend_closes_client(self):
        server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        dead_port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()
        relay = scopy_tunnel.TunnelRelay([dead_port])
        self.relays.append(relay)
        port = await self.listen(relay.handle)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        self.assertEqual(await asyncio.wait_for(reader.read(), timeout=2), b"")
        writer.close()
        await writer.wait_closed()

    async def test_pool_cancellation_stops_all_processes(self):
        # Stand-in SSH forwards: actual processes bind the requested private ports.
        program = '''
import asyncio, sys
async def echo(reader, writer):
    try:
        data = await reader.read()
        writer.write(data)
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()
async def main():
    server = await asyncio.start_server(echo, '127.0.0.1', int(sys.argv[1]))
    async with server:
        await server.serve_forever()
asyncio.run(main())
'''
        # Get a free frontend port; run_pool itself must claim it.
        temporary = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = temporary.sockets[0].getsockname()[1]
        temporary.close()
        await temporary.wait_closed()
        processes = []
        real_popen = scopy_tunnel.subprocess.Popen

        def start(arguments):
            process = real_popen(arguments)
            processes.append(process)
            return process

        with patch.object(scopy_tunnel.subprocess, "Popen", side_effect=start):
            pool = asyncio.create_task(scopy_tunnel.run_pool(
                port, 2, lambda private: [sys.executable, "-c", program, str(private)]))
            try:
                for _ in range(100):
                    if pool.done():
                        pool.result()
                    try:
                        reader, writer = await asyncio.open_connection("127.0.0.1", port)
                        break
                    except OSError:
                        await asyncio.sleep(0.05)
                else:
                    self.fail("Pool did not start")
                writer.write(b"round trip")
                await writer.drain()
                writer.write_eof()
                self.assertEqual(await asyncio.wait_for(reader.read(), timeout=5), b"round trip")
                writer.close()
                await writer.wait_closed()
            finally:
                pool.cancel()
                await asyncio.gather(pool, return_exceptions=True)
        self.assertEqual(len(processes), 2)
        self.assertTrue(all(process.poll() is not None for process in processes))

    async def test_failed_ssh_startup_aborts_pool(self):
        with self.assertRaisesRegex(click.ClickException, "exit status 7"):
            await scopy_tunnel.run_pool(
                0, 2, lambda _: [sys.executable, "-c", "raise SystemExit(7)"])

    async def test_cancellation_during_authentication_stops_process(self):
        processes = []
        real_popen = scopy_tunnel.subprocess.Popen

        def start(arguments):
            process = real_popen(arguments)
            processes.append(process)
            return process

        with patch.object(scopy_tunnel.subprocess, "Popen", side_effect=start):
            pool = asyncio.create_task(scopy_tunnel.run_pool(
                0, 2, lambda _: [sys.executable, "-c", "import time; time.sleep(120)"]))
            try:
                for _ in range(100):
                    if processes:
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(processes)
            finally:
                pool.cancel()
                await asyncio.gather(pool, return_exceptions=True)
        self.assertTrue(all(process.poll() is not None for process in processes))


if __name__ == "__main__":
    unittest.main()
