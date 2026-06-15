# magewell-capture

Monitored HDMI capture with browser preview and record control, using the
Magewell USB Capture HDMI Gen2 and NVENC hardware encoding.

## Test tiers

Tests are split by hardware requirements:

| Marker | What it needs | Runs on CI |
|---|---|---|
| _(none)_ | Nothing | Yes |
| `virtual_device` | v4l2loopback + snd-aloop kernel modules | No |
| `device` | Magewell hardware + NVENC + ffmpeg | No |

## Before committing

When the conversation turns to wrapping up work, discussing a commit message,
or summarising changes, proactively suggest running the non-hardware tests
before the user commits:

```
uv run pytest -m "not virtual_device and not device" -v
```

All tests in this tier must pass before a commit is finalised. Do not commit
if any non-hardware test is failing.
