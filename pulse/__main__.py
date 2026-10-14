"""`uv run pulse` — start AI Pulse on http://127.0.0.1:8765 and open it in your browser."""

from __future__ import annotations

import os
import threading
import urllib.request
import webbrowser


def main() -> None:
    port = int(os.environ.get("PULSE_PORT", "8765"))
    url = f"http://127.0.0.1:{port}/"
    try:  # already running (another terminal, or you double-clicked twice)? just open it
        with urllib.request.urlopen(url + "api/state", timeout=1):
            print(f"AI Pulse is already running: {url}")
            if not os.environ.get("PULSE_NO_BROWSER"):
                webbrowser.open(url)
            return
    except OSError:
        pass

    import uvicorn  # imported late so the "already running" path stays instant

    print(f"AI Pulse: {url}   (Ctrl+C to stop)")
    if not os.environ.get("PULSE_NO_BROWSER"):
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    # The live-progress stream stays open for a whole pull. Without a short graceful timeout,
    # Ctrl+C would wait for that pull to finish (still spending), with nothing on screen.
    uvicorn.run("pulse.server:app", host="127.0.0.1", port=port, log_level="warning", timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
