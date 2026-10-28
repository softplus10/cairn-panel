# cairnd local protocol v1

Transport: AF_UNIX / SOCK_STREAM, `/var/run/cairn/control.sock`.
One request and one response per connection. Socket directory: root:cairn 2750;
socket: root:cairn 0660. Access is granted through filesystem group permissions.
The directory must not be group-writable or world-accessible. Group users may
invoke every operation below. The protocol itself has no separate authentication.

A request is a UTF-8 operation name, followed by tab-separated hex-encoded UTF-8
fields and LF. Fields cannot contain NUL, CR or LF. Hex protects field boundaries,
not confidentiality. Maximum request: 4095 bytes including LF; individual encoded
fields: 2048 bytes. SSIDs are restricted to 1–32 UTF-8 bytes by the connect operation.
Non-UTF-8 SSIDs are not supported in v1.

Responses: `OK<TAB>hex(output)<LF>` or `ERR<TAB>hex(message)<LF>`.
Arguments and command stderr are never included in error messages.

| Operation | Decoded fields | Action |
| --- | --- | --- |
| ping | none | Empty success; service readiness |
| brightness-get | none | wsconsctl -n display.brightness |
| brightness | 1..100 | Set display.brightness |
| wifi | interface, up/down | Set interface link state |
| scan | interface | ifconfig interface scan |
| connect | interface, SSID, WPA key or empty | Set network, enable inet autoconf, bring up |
| power | auto/low/high | apm -A/-L/-H |
| suspend | none | zzz |

Example, set brightness to 72:

```text
brightness\t3732\n
```

The example uses visible `\t` and `\n` escapes for documentation; send actual tab
and newline bytes. The Python client implements framing in `cwm_panel/system.py`.

The daemon is sequential and processes a single request at a time. Each socket
read/write has a 3 second timeout; each system child has a 10 second timeout.
A connect sequence may run three commands. The client uses a 35 second timeout.
Command stdout is capped at 64 KiB in the response. Scan results are not cached.
Failures can leave partially applied configuration; there is no transactional
rollback. DHCP completion is asynchronous. Credentials are never persisted.

Only hardcoded absolute command paths can be executed. Interfaces are limited
to 15 ASCII alphanumeric characters, with an initial letter and final digit.
WPA passphrases must be 8–63 bytes, or a 64-digit hex PSK. The service does not
execute a shell. Each system child runs with a cleared environment and fixed PATH.
The daemon holds an advisory lock so restarting cannot replace a live socket.
