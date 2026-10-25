"""Direct local Playwright protocol transport, without a Node relay per worker.

Compatibility adapter for Playwright 1.63.0's Python Connection/Transport API.
The normal public sync API remains the interface presented to strategies.
Local driver utilities (device presets, HAR and trace archives) aren't used by
extraction; explicitly reject them rather than silently changing their behavior.
"""
import asyncio
from importlib.metadata import version

from greenlet import getcurrent
from playwright._impl._connection import Connection, from_channel
from playwright._impl._greenlets import MainGreenlet
from playwright._impl._errors import TargetClosedError
from playwright._impl._object_factory import create_remote_object
from playwright._impl._transport import Transport
from playwright.sync_api._context_manager import PlaywrightContextManager
from playwright.sync_api._generated import Browser
from websockets.asyncio.client import connect


class DriverUtilities:
    devices = {}

    def __getattr__(self, name):
        raise NotImplementedError(f'Local Playwright utility {name} requires a standalone driver')


class WebSocketTransport(Transport):
    def __init__(self, loop, endpoint):
        super().__init__(loop)
        self.endpoint = endpoint
        self.socket = None
        self.stopping = False
        self.stopped = loop.create_future()
        self.stop_requested = asyncio.Event()
        self.writes = set()

    async def connect(self):
        try:
            self.socket = await connect(self.endpoint, additional_headers={'x-playwright-browser':'chromium'},
                                        proxy=None, open_timeout=15, close_timeout=2,
                                        max_size=None, max_queue=4, compression=None,
                                        ping_interval=None)
        except Exception as exc:
            self.on_error_future.set_exception(TargetClosedError('Shared browser connection: '+str(exc)))

    def request_stop(self):
        self.stopping = True
        self.stop_requested.set()
        if self.socket:
            self._loop.create_task(self.socket.close())
        elif not self.stopped.done():
            self.stopped.set_result(None)

    async def wait_until_stopped(self):
        await self.stopped

    async def run(self):
        try:
            if self.socket:
                async for data in self.socket:
                    self.on_message(self.deserialize_message(data))
                    await asyncio.sleep(0)
                if not self.stopping:
                    raise ConnectionError('Shared browser disconnected')
        except Exception as exc:
            if not self.stopping and not self.on_error_future.done():
                self.on_error_future.set_exception(TargetClosedError('Shared browser connection: '+str(exc)))
        finally:
            # Keep the sync dispatcher alive until error callbacks have returned
            # control to the caller, which then closes the context manager.
            await self.stop_requested.wait()
            if not self.stopped.done():
                self.stopped.set_result(None)

    def send(self, message):
        async def write():
            try:
                await self.socket.send(self.serialize_message(message).decode())
            except Exception as exc:
                if not self.stopping and not self.on_error_future.done():
                    self.on_error_future.set_exception(TargetClosedError('Shared browser connection: '+str(exc)))
        task = self._loop.create_task(write())
        self.writes.add(task)
        task.add_done_callback(self.writes.discard)


class SharedBrowserConnection(PlaywrightContextManager):
    """Use one private protocol connection/context owner per worker process."""
    def __init__(self, endpoint):
        super().__init__()
        self.endpoint = endpoint
        self._browser = None

    def __enter__(self):
        if version('playwright') != '1.63.0':
            raise RuntimeError('Shared transport requires Playwright 1.63.0; verify adapter before upgrading')
        self._loop = asyncio.new_event_loop()
        self._own_loop = True
        transport = WebSocketTransport(self._loop, self.endpoint)
        dispatcher = MainGreenlet(lambda: self._loop.run_until_complete(self._connection.run_as_sync()))
        self._connection = Connection(dispatcher, create_remote_object, transport, self._loop,
                                      local_utils=DriverUtilities())
        self._connection.mark_as_remote()
        caller = getcurrent()
        def failure(future):
            if future.cancelled():return
            error = future.exception()
            if self._browser:
                for context in self._browser.contexts:
                    for page in context.pages:page._on_close()
                    context._on_close()
                self._loop.call_soon(self._browser._on_close)
            # A transport error and an outstanding RPC may fail together.
            # Retrieve both failures even when the API awaits only one of them.
            for callback in self._connection._callbacks.values():
                callback.future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
            self._connection.cleanup(str(error))
            if not self._connection.playwright_future.done():
                self._connection.playwright_future.set_exception(error)
        transport.on_error_future.add_done_callback(failure)
        self._connection.playwright_future.add_done_callback(lambda _: caller.switch())
        dispatcher.switch()
        try:
            playwright = self._connection.playwright_future.result()
            browser = from_channel(playwright._initializer['preLaunchedBrowser'])
            self._browser = browser
            browser._connect_to_browser_type(playwright.chromium, None)
            browser._should_close_connection_on_close = False
            return Browser(browser)
        except BaseException:
            self.__exit__()
            raise
