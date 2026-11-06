"""
Stand-in X-Road Security Server (REST) for local demos. NOT a real X-Road.
Implements the two endpoints the adapter uses, with the same URL layout and
headers as a real Security Server consumer port:
  GET  /r1/<instance>/listClients
  POST /r1/<instance>/<class>/<code>/<subsystem>/<service>
Run:  python mock_security_server.py     (listens on :8080)
Then set XROAD_SECURITY_SERVER_URL=http://localhost:8080 in .env
Swap for a real Security Server later by changing only that URL.
"""
import json, uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

LOG = []

class H(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Road-Id", self.headers.get("X-Road-Id", str(uuid.uuid4())))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.endswith("/listClients"):
            inst = self.path.split("/")[2]
            print(f"[mock-SS] listClients client={self.headers.get('X-Road-Client')}")
            # "*" = treat every org subsystem as registered
            return self._send(200, {"member": [{"id": {"instance_identifier": inst, "subsystem_code": "*"}}]})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
        LOG.append(body)
        print(f"[mock-SS] {self.path} client={self.headers.get('X-Road-Client')} "
              f"id={self.headers.get('X-Road-Id')} records={body.get('record_count')} hash={str(body.get('payload_hash'))[:12]}")
        self._send(200, {"status": "accepted", "received_hash": body.get("payload_hash")})

    def log_message(self, *a): pass

if __name__ == "__main__":
    print("Mock X-Road Security Server on http://localhost:8080")
    HTTPServer(("0.0.0.0", 8080), H).serve_forever()
