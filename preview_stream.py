"""Loopback-only camera preview for aim/focus checks.

MJPEG over an SSH tunnel reliably freezes after the first frame (kernel
send queue fills faster than Tailscale/SSH drains it). This serves a
small latest-JPEG snapshot instead, with the page polling for updates.
"""
import io
import logging
import socketserver
from http import server
from threading import Condition

from picamera2 import Picamera2
from picamera2.encoders import MJPEGEncoder
from picamera2.outputs import FileOutput

# Full camera main stream; snap-poll keeps SSH tunnels usable at this size.
WIDTH, HEIGHT = 1456, 1088
BITRATE = 8_000_000
FPS = 5.0
SNAP_POLL_MS = 250

PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>serv preview</title>
<style>html,body{margin:0;background:#111;color:#ddd;font:14px sans-serif}
img{width:100%;height:auto;display:block;background:#000}</style></head>
<body>
<img id="f" alt="preview">
<script>
const img = document.getElementById('f');
function tick() {
  img.src = '/snap.jpg?' + Date.now();
}
img.onload = img.onerror = () => setTimeout(tick, 250);
tick();
</script>
</body></html>
"""


class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.condition = Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()


class StreamingHandler(server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logging.info("%s - " + fmt, self.address_string(), *args)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            content = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)
            return

        if path == "/snap.jpg":
            with output.condition:
                if output.frame is None:
                    output.condition.wait(timeout=2.0)
                frame = output.frame
            if not frame:
                self.send_error(503, "No frame yet")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(frame)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            self.wfile.write(frame)
            return

        # Keep the old path working for tools that expect MJPEG, but throttle
        # hard so tunnels cannot back up.
        if path == "/stream.mjpg":
            self.send_response(200)
            self.send_header("Age", "0")
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.send_header(
                "Content-Type", "multipart/x-mixed-replace; boundary=FRAME"
            )
            self.end_headers()
            try:
                last = None
                while True:
                    with output.condition:
                        output.condition.wait(timeout=1.0)
                        frame = output.frame
                    if not frame or frame is last:
                        continue
                    last = frame
                    self.wfile.write(b"--FRAME\r\n")
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(frame)))
                    self.end_headers()
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
            except Exception as e:
                logging.warning("Client disconnected: %s", e)
            return

        self.send_error(404)


class StreamingServer(socketserver.ThreadingMixIn, server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


picam2 = Picamera2()
picam2.configure(
    picam2.create_video_configuration(
        main={"size": (WIDTH, HEIGHT)},
        controls={"FrameRate": FPS},
    )
)
output = StreamingOutput()
picam2.start_recording(MJPEGEncoder(bitrate=BITRATE), FileOutput(output))

try:
    # Always loopback-only: this maintenance server has no authentication and
    # camera imagery must not be exposed to Ethernet/LTE/Tailscale peers. Use
    # an SSH tunnel (ssh -L 8000:127.0.0.1:8000 serv@PI) to view it remotely.
    host = "127.0.0.1"
    address = (host, 8000)
    httpd = StreamingServer(address, StreamingHandler)
    print(
        f"Streaming at http://{host}:8000  "
        f"({WIDTH}x{HEIGHT} @ {FPS:.0f}fps snap, bitrate={BITRATE // 1_000_000}Mbps)"
    )
    httpd.serve_forever()
finally:
    picam2.stop_recording()
    picam2.close()
