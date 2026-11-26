import base64
import pathlib
import socketserver
import ssl
import subprocess
import threading

SINK_HOSTNAME = "host.docker.internal"


def write_self_signed_cert(workdir, hostname=SINK_HOSTNAME):
    certfile = pathlib.Path(workdir) / "smtp-sink.crt"
    keyfile = pathlib.Path(workdir) / "smtp-sink.key"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-keyout", str(keyfile), "-out", str(certfile),
            "-subj", f"/CN={hostname}", "-addext", f"subjectAltName=DNS:{hostname}",
        ],
        check=True, capture_output=True, timeout=60,
    )
    return certfile, keyfile


class Session:
    def __init__(self):
        self.tls = False
        self.commands = []
        self.auth = None
        self.mail_from = None
        self.rcpt_to = []
        self.data = None


class SessionHandler(socketserver.BaseRequestHandler):
    def setup(self):
        self.sink = self.server.sink
        self.session = Session()
        self.sink.sessions.append(self.session)
        self.connection = self.request
        self.reader = self.connection.makefile("rb")

    def send(self, line):
        self.connection.sendall(line.encode() + b"\r\n")

    def read_line(self):
        raw = self.reader.readline(65536)
        if not raw:
            return None
        return raw.decode("utf-8", "replace").rstrip("\r\n")

    def read_data(self):
        lines = []
        while True:
            raw = self.reader.readline(1 << 20)
            if not raw or raw == b".\r\n":
                return b"".join(lines)
            lines.append(raw[1:] if raw.startswith(b"..") else raw)

    def ehlo(self):
        extensions = ["sink"]
        if self.sink.starttls and not self.session.tls:
            extensions.append("STARTTLS")
        if self.session.tls:
            extensions.append("AUTH PLAIN")
        for index, extension in enumerate(extensions):
            self.send(("250 " if index == len(extensions) - 1 else "250-") + extension)

    def start_tls(self):
        self.send("220 ready")
        self.connection = self.sink.context.wrap_socket(self.connection, server_side=True)
        self.reader = self.connection.makefile("rb")
        self.session.tls = True

    def authenticate(self, line):
        parts = line.split(" ")
        if not self.session.tls or len(parts) < 2 or parts[1].upper() != "PLAIN":
            self.send("504 unsupported")
            return
        if len(parts) == 2:
            self.send("334 ")
            parts.append(self.read_line() or "")
        self.session.auth = base64.b64decode(parts[2]).split(b"\0")
        self.send("235 ok")

    def handle(self):
        self.send("220 sink ESMTP")
        while True:
            line = self.read_line()
            if line is None:
                return
            verb = line.split(" ", 1)[0].upper()
            self.session.commands.append(verb)
            if verb in ("EHLO", "HELO"):
                self.ehlo()
            elif verb == "STARTTLS" and self.sink.starttls and not self.session.tls:
                self.start_tls()
            elif verb == "AUTH":
                self.authenticate(line)
            elif verb == "MAIL":
                self.session.mail_from = line.split(":", 1)[1].strip().strip("<>")
                self.send("250 ok")
            elif verb == "RCPT":
                self.session.rcpt_to.append(line.split(":", 1)[1].strip().strip("<>"))
                self.send("250 ok")
            elif verb == "DATA":
                self.send("354 end with .")
                self.session.data = self.read_data()
                self.sink.messages.append(self.session)
                self.send("250 queued")
            elif verb in ("RSET", "NOOP"):
                self.send("250 ok")
            elif verb == "QUIT":
                self.send("221 bye")
                return
            else:
                self.send("502 unsupported")


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class SmtpSink:
    def __init__(self, certfile, keyfile):
        self.certfile = pathlib.Path(certfile)
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(certfile, keyfile)
        self.starttls = True
        self.sessions = []
        self.messages = []
        self.server = Server(("127.0.0.1", 0), SessionHandler)
        self.server.sink = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self):
        return self.server.server_address[1]

    def start(self):
        self.thread.start()

    def reset(self, starttls):
        self.starttls = starttls
        self.sessions.clear()
        self.messages.clear()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
