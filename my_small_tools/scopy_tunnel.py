"""Balance TCP connections across independent SSH port forwards."""

import asyncio
import socket
import subprocess
import time
from collections.abc import Callable

import click


class TunnelRelay:
    """Keep every client connection on one backend, preserving arbitrary protocols."""

    def __init__(self, ports: list[int]):
        self.ports = ports
        self.active = [0] * len(ports)
        self.tasks: set[asyncio.Task] = set()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.current_task()
        self.tasks.add(task)
        backend = min(range(len(self.ports)), key=self.active.__getitem__)
        self.active[backend] += 1
        upstream = None
        pumps = []
        try:
            incoming, upstream = await asyncio.open_connection("127.0.0.1", self.ports[backend])

            async def copy(source, destination):
                while data := await source.read(256 * 1024):
                    destination.write(data)
                    await destination.drain()
                # Preserve half-close: a peer can finish sending and still read a reply.
                if destination.can_write_eof():
                    destination.write_eof()
                    await destination.drain()

            pumps = [asyncio.create_task(copy(reader, upstream)),
                     asyncio.create_task(copy(incoming, writer))]
            await asyncio.gather(*pumps)
        except (OSError, asyncio.IncompleteReadError):
            pass  # Reset/failed connections close; application bytes are never replayed.
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            for connection in (writer, upstream):
                if connection is not None:
                    connection.close()
                    try:
                        await connection.wait_closed()
                    except OSError:
                        pass
            self.active[backend] -= 1
            self.tasks.discard(task)

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def stop_processes(processes):
    """Stop the whole pool, sharing a five-second grace period."""
    for process in processes:
        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 5
    for process in processes:
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


async def run_pool(local_port: int, streams: int, command: Callable[[int], list[str]],
                   ready: Callable[[], None] | None = None):
    """Own a loopback listener and private, foreground SSH processes for its lifetime."""
    relay = TunnelRelay([])
    processes = []
    serving = monitor = None
    # Claim the public port before authenticating any SSH connections.
    server = await asyncio.start_server(relay.handle, "127.0.0.1", local_port,
                                        start_serving=False)
    try:
        for _ in range(streams):
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            # SSH claims this ephemeral port; ExitOnForwardFailure catches races.
            process = subprocess.Popen(command(port))
            processes.append(process)
            deadline = time.monotonic() + 60
            while True:
                if process.poll() is not None:
                    raise click.ClickException(f"SSH pool connection failed (exit status {process.returncode}).")
                try:
                    _, probe = await asyncio.open_connection("127.0.0.1", port)
                    probe.close()
                    await probe.wait_closed()
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise click.ClickException("Timed out waiting for an SSH pool connection.")
                    await asyncio.sleep(0.1)
            relay.ports.append(port)
            relay.active.append(0)

        async def watch():
            while True:
                for process in processes:
                    if process.poll() is not None:
                        raise click.ClickException(
                            f"SSH pool connection closed (exit status {process.returncode})."
                        )
                await asyncio.sleep(0.2)

        click.echo(f"Tunnel ready with {streams} independent SSH connections.")
        if ready is not None:
            ready()
        serving = asyncio.create_task(server.serve_forever())
        monitor = asyncio.create_task(watch())
        done, _ = await asyncio.wait([serving, monitor], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        server.close()
        await server.wait_closed()
        for task in (serving, monitor):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (serving, monitor) if task),
                             return_exceptions=True)
        await relay.close()
        stop_processes(processes)
