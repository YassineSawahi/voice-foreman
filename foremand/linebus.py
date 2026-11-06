"""The line's control channel: baresip's ctrl_dbus module on your D-Bus session bus.

Commands go to baresip's `invoke` method (the long form, without the slash: "dial sip:...",
"hangup"), and every call event comes back as an `event` signal carrying the same JSON that
ctrl_tcp sends. Only processes running as you can reach your session bus, so unlike a TCP
control port on 127.0.0.1, other accounts on this machine can't drive the line.

Also a small command line, for the tools in bin/:
  linebus.py text [MESSAGE...]   text the phone (reads the message from stdin when none is given)
  linebus.py invoke COMMAND...   run any baresip command and print its reply
"""
import json
import sys

from jeepney import DBusAddress, MatchRule, MessageType, message_bus, new_method_call

LINE = DBusAddress("/baresip", bus_name="com.github.Baresip", interface="com.github.Baresip")
EVENTS = MatchRule(type="signal", interface="com.github.Baresip", member="event", path="/baresip")


class LineError(Exception):
    """The line didn't take the command, usually because it isn't running."""


def _call(command, params=""):
    return new_method_call(LINE, "invoke", "s", (f"{command} {params}".strip(),))


def _reply_text(reply):
    if reply.header.message_type == MessageType.error:
        raise LineError(" ".join(str(part) for part in reply.body) or "the line refused the command")
    return reply.body[0]


def invoke(command, params="", timeout=5):
    """Run one baresip command and return what it printed. Blocking: for tools and the probe."""
    from jeepney.io.blocking import open_dbus_connection
    with open_dbus_connection() as conn:
        return _reply_text(conn.send_and_get_reply(_call(command, params), timeout=timeout))


class Line:
    """foremand's async side: send() commands, iterate events() for the call events."""

    async def connect(self):
        import asyncio
        from contextlib import AsyncExitStack
        from jeepney.io.asyncio import Proxy, open_dbus_router
        self._stack = AsyncExitStack()
        self.router = await self._stack.enter_async_context(open_dbus_router())
        self.queue = asyncio.Queue()  # unbounded: no event is ever dropped
        self._stack.enter_context(self.router.filter(EVENTS, queue=self.queue))
        await Proxy(message_bus, self.router).AddMatch(EVENTS)

    async def send(self, command, params=""):
        return _reply_text(await self.router.send_and_get_reply(_call(command, params)))

    async def events(self):
        while True:
            msg = await self.queue.get()
            try:
                yield json.loads(msg.body[2])
            except (IndexError, ValueError):
                continue


def main(args):
    if args[:1] == ["text"]:
        message = " ".join(args[1:]) or sys.stdin.read()
        message = " ".join(message.split())
        if not message:
            print("usage: vf-text MESSAGE (or the message on stdin)", file=sys.stderr)
            return 2
        print(invoke("message", message).strip() or "Texted.")
        return 0
    if args[:1] == ["invoke"] and len(args) > 1:
        print(invoke(args[1], " ".join(args[2:])), end="")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except LineError as e:
        sys.exit(f"the line didn't take it: {e}")
