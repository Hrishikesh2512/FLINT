"""Venom web console — manage and prompt the wearable from any browser.

A tiny stdlib HTTP server (no new dependencies) on the Pi serving one
page: live status, conversation transcript, a text prompt box that
feeds straight into the Gemini session, and music/volume controls.
The voice loop stays the owner of all state; this thread only reads
snapshots and posts messages onto thread-safe queues.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("venom.web")

CONTROL_REQUEST = Path("/run/venom/control.request")
OVERRIDE_PATH = Path("/var/lib/venom/override.toml")
VOICE_KEYS = ("wake_word", "wake_threshold", "voice_name", "user_name",
              "inactivity_timeout")


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))


def write_override(section: str, values: dict,
                   path: Path = OVERRIDE_PATH) -> None:
    """Merge `values` into [section] of the override TOML the daemon owns."""
    data: dict = {}
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        pass
    data.setdefault(section, {}).update(values)
    lines = []
    for sect, vals in data.items():
        lines.append(f"[{sect}]")
        lines += [f"{k} = {_toml_value(v)}" for k, v in vals.items()]
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))

PAGE = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Venom Console</title><style>
body{font:15px/1.5 system-ui;background:#0d1117;color:#e6edf3;max-width:640px;
margin:0 auto;padding:16px}h1{font-size:18px}.row{display:flex;gap:8px;
flex-wrap:wrap;margin:8px 0}.pill{background:#161b22;border:1px solid #30363d;
border-radius:20px;padding:4px 12px;font-size:13px}.ok{border-color:#2ea043}
.bad{border-color:#f85149}#log{background:#161b22;border:1px solid #30363d;
border-radius:8px;padding:10px;height:280px;overflow-y:auto;font-size:14px}
#log .you{color:#79c0ff}#log .venom{color:#e6edf3}#log .sys{color:#8b949e}
input,button{font:inherit;border-radius:8px;border:1px solid #30363d;
background:#161b22;color:#e6edf3;padding:8px 12px}input{flex:1}
button{cursor:pointer}button:hover{background:#21262d}</style>
<h1>&#128225; Venom Console</h1>
<div class=row id=status></div>
<div id=log></div>
<form class=row id=say><input id=text placeholder="Talk to Venom..." autocomplete=off>
<button>Send</button></form>
<div class=row>
<input id=song placeholder="Song..." style=max-width:180px>
<button onclick="music('play',song.value)">&#9654; Play</button>
<button onclick="music('pause')">&#9208;</button>
<button onclick="music('resume')">&#9654;</button>
<button onclick="music('stop')">&#9632; Stop</button>
<button onclick="vol(-10)">Vol-</button><button onclick="vol(10)">Vol+</button>
</div>
<details><summary>&#128266; Bluetooth</summary><div class=row>
<button onclick="bt(0)">Paired devices</button>
<button onclick="bt(1)">Scan nearby (8s)</button></div>
<div id=btlist></div></details>
<details><summary>&#9881;&#65039; Settings</summary><div id=settings></div>
<div class=row><button onclick="saveSettings()">Save &amp; restart</button></div></details>
<details><summary>&#128203; Logs</summary>
<div class=row><button onclick="loadLogs()">Refresh</button></div>
<pre id=logs style="font-size:11px;overflow-x:auto;background:#161b22;
border:1px solid #30363d;border-radius:8px;padding:8px"></pre></details>
<details><summary>&#128295; System</summary><div class=row>
<button onclick="sys('update')">&#11015; Update from GitHub</button>
<button onclick="sys('restart')">&#8635; Restart Venom</button>
<button onclick="sys('reboot')">&#9888; Reboot Pi</button></div></details>
<script>
const $=id=>document.getElementById(id);let n=0;
async function api(p,b){return fetch(p,b?{method:'POST',body:JSON.stringify(b)}:{})}
async function tick(){try{const s=await(await api('/api/state')).json();
$('status').innerHTML=[['voice',s.voice,1],['internet',s.internet?'up':'down',s.internet],
['headset',s.headset||'none',!!s.headset],['brain',s.brain||'-',!!s.brain],
['music',s.now_playing||'idle',1]].map(([k,v,ok])=>
`<span class="pill ${ok?'ok':'bad'}">${k}: ${v}</span>`).join('');
if(s.transcript.length!=n){n=s.transcript.length;
$('log').innerHTML=s.transcript.map(([w,t])=>{
const c=w.startsWith('you')?'you':w=='venom'?'venom':'sys';
return `<div class=${c}><b>${w}</b>: ${t}</div>`}).join('');
$('log').scrollTop=1e9}}catch(e){}}
$('say').onsubmit=e=>{e.preventDefault();if($('text').value.trim())
api('/api/prompt',{text:$('text').value.trim()});$('text').value=''};
function music(a,q){api('/api/music',{action:a,query:q||''})}
function vol(d){api('/api/volume',{delta:d})}
function sys(a){if(a=='reboot'&&!confirm('Reboot the Pi?'))return;
if(a=='update'&&!confirm('Pull latest code from GitHub and reinstall?'))return;
api('/api/system',{action:a})}
async function bt(scan){$('btlist').innerHTML='<i>looking...</i>';
const d=await(await api('/api/bluetooth'+(scan?'/scan':''))).json();
$('btlist').innerHTML=d.map(x=>`<div class=row><span class="pill ${
x.connected?'ok':''}">${x.name} ${x.connected?'&#10003;':''}</span>
<button onclick="btUse('${x.mac}','${x.name.replace(/'/g,'')}')">Use</button>
</div>`).join('')||'<i>none found</i>'}
function btUse(m,n){if(confirm('Switch headset to '+n+'? Venom restarts.'))
api('/api/bluetooth',{mac:m,name:n})}
async function loadSettings(){const s=await(await api('/api/settings')).json();
$('settings').innerHTML=Object.entries(s).map(([k,v])=>
`<div class=row><label style=min-width:140px>${k}</label>
<input data-k=${k} value="${v}"></div>`).join('')}
async function saveSettings(){const b={};document.querySelectorAll('#settings input')
.forEach(i=>b[i.dataset.k]=i.value);
if(!confirm('Save settings and restart Venom?'))return;
const r=await(await api('/api/settings',b)).json();alert(r.result)}
async function loadLogs(){const l=await(await api('/api/logs')).json();
$('logs').textContent=l.text}
loadSettings();
setInterval(tick,1500);tick();
</script>"""


class WebConsole:
    """Owns the HTTP thread; the voice loop attaches itself on each start."""

    def __init__(self, port: int = 8787):
        self.port = port
        self.orchestrator = None  # set by attach(); may be replaced on restart
        self.loop = None

    def attach(self, orchestrator, loop) -> None:
        self.orchestrator = orchestrator
        self.loop = loop

    # ── actions called from HTTP threads ─────────────────────────────────────
    def state(self) -> dict:
        orch = self.orchestrator
        base = {"voice": "starting", "transcript": [], "now_playing": "",
                "internet": True, "headset": None, "brain": None}
        try:
            from venom.config import load_config

            status = json.loads(load_config().status_path.read_text())
            base.update({k: status.get(k) for k in ("internet", "headset", "brain")})
        except Exception:
            pass
        if orch is not None:
            base["voice"] = orch.state
            base["transcript"] = list(orch.transcript)
            base["now_playing"] = orch.music.now_playing
        return base

    def prompt(self, text: str) -> bool:
        orch, loop = self.orchestrator, self.loop
        if not text or orch is None or loop is None:
            return False
        orch.transcript.append(("you (console)", text))
        loop.call_soon_threadsafe(orch.inbox.put_nowait, text)
        return True

    def music(self, action: str, query: str) -> str:
        orch = self.orchestrator
        if orch is None:
            return "not ready"
        acts = {"play": lambda: orch.music.play(query), "stop": orch.music.stop,
                "pause": lambda: orch.music.set_paused(True),
                "resume": lambda: orch.music.set_paused(False)}
        result = acts.get(action, lambda: "unknown action")()
        orch.transcript.append(("system", result))
        return result

    def system(self, action: str) -> str:
        """Privileged actions via the root control unit watching /run/venom."""
        if action not in ("update", "restart", "reboot"):
            return "unknown action"
        try:
            CONTROL_REQUEST.write_text(action)
        except OSError as exc:
            return f"control channel unavailable: {exc}"
        notes = {"update": "Updating from GitHub — takes a few minutes, "
                           "then Venom restarts.",
                 "restart": "Restarting Venom...",
                 "reboot": "Rebooting the Pi..."}
        if self.orchestrator is not None:
            self.orchestrator.transcript.append(("system", notes[action]))
        return notes[action]

    @staticmethod
    def bluetooth_list(scan_seconds: int = 0) -> list[dict]:
        from venom.btaudio import parse_devices

        def run(args, timeout=10):
            out = subprocess.run(["bluetoothctl", *args], capture_output=True,
                                 text=True, timeout=timeout)
            return (out.stdout or "") + (out.stderr or "")

        if scan_seconds:
            run(["--timeout", str(scan_seconds), "scan", "on"],
                timeout=scan_seconds + 10)
        devices = []
        for mac, name in parse_devices(run(["devices"])).items():
            connected = "Connected: yes" in run(["info", mac])
            devices.append({"mac": mac, "name": name, "connected": connected})
        return devices

    def bluetooth_use(self, mac: str, name: str) -> str:
        """Persist a new preferred headset and restart to adopt it."""
        write_override("audio", {"output": "bluetooth",
                                 "bluetooth_mac": mac, "bluetooth_name": name})
        self.system("restart")
        return f"Switching to {name or mac} — restarting Venom."

    def settings_get(self) -> dict:
        from venom.config import load_config

        voice = load_config().voice
        return {k: getattr(voice, k) for k in VOICE_KEYS}

    def settings_set(self, values: dict) -> str:
        clean = {k: values[k] for k in VOICE_KEYS if k in values}
        for key in ("wake_threshold", "inactivity_timeout"):
            if key in clean:
                clean[key] = float(clean[key])
        # Only openWakeWord's pretrained models exist on the device; anything
        # else crash-loops the voice stack (seen live with "hey_venom").
        allowed = ("hey_jarvis", "alexa", "hey_mycroft")
        if clean.get("wake_word") and clean["wake_word"] not in allowed:
            return (f"wake_word must be one of {', '.join(allowed)} — "
                    "not saved")
        if not clean:
            return "nothing to change"
        write_override("voice", clean)
        self.system("restart")
        return "Saved — restarting Venom to apply."

    @staticmethod
    def logs(lines: int = 60) -> str:
        out = subprocess.run(
            ["journalctl", "-u", "venom", "-n", str(lines), "--no-pager",
             "-o", "short-precise"], capture_output=True, text=True, timeout=10)
        return out.stdout or out.stderr or "(journal not readable)"

    @staticmethod
    def volume(delta: int) -> None:
        sign = "+" if delta >= 0 else "-"
        subprocess.run(["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@",
                        f"{abs(delta)}%{sign}"], capture_output=True, timeout=5)

    # ── server ───────────────────────────────────────────────────────────────
    def start(self) -> None:
        console = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # journald stays quiet
                pass

            def _send(self, body: bytes, ctype: str = "application/json"):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/state":
                    self._send(json.dumps(console.state()).encode())
                elif self.path.startswith("/api/bluetooth"):
                    scan = 8 if "scan" in self.path else 0
                    self._send(json.dumps(console.bluetooth_list(scan)).encode())
                elif self.path == "/api/settings":
                    self._send(json.dumps(console.settings_get()).encode())
                elif self.path == "/api/logs":
                    self._send(json.dumps({"text": console.logs()}).encode())
                else:
                    self._send(PAGE.encode(), "text/html; charset=utf-8")

            def do_POST(self):
                try:
                    size = int(self.headers.get("Content-Length", 0))
                    data = json.loads(self.rfile.read(size) or b"{}")
                except (ValueError, json.JSONDecodeError):
                    data = {}
                if self.path == "/api/prompt":
                    ok = console.prompt(str(data.get("text", "")).strip())
                    self._send(json.dumps({"ok": ok}).encode())
                elif self.path == "/api/music":
                    result = console.music(str(data.get("action", "")),
                                           str(data.get("query", "")))
                    self._send(json.dumps({"result": result}).encode())
                elif self.path == "/api/volume":
                    try:
                        console.volume(int(data.get("delta", 0)))
                    except Exception as exc:
                        log.debug("volume failed: %s", exc)
                    self._send(b"{}")
                elif self.path == "/api/system":
                    result = console.system(str(data.get("action", "")))
                    self._send(json.dumps({"result": result}).encode())
                elif self.path == "/api/bluetooth":
                    result = console.bluetooth_use(str(data.get("mac", "")),
                                                   str(data.get("name", "")))
                    self._send(json.dumps({"result": result}).encode())
                elif self.path == "/api/settings":
                    result = console.settings_set(data)
                    self._send(json.dumps({"result": result}).encode())
                else:
                    self._send(b"{}")

        server = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True,
                         name="venom-web").start()
        log.info("web console on http://0.0.0.0:%d", self.port)
