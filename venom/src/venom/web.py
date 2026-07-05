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
import os
import socket
import subprocess
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("venom.web")

CONTROL_REQUEST = Path("/run/venom/control.request")
# Root shell daemon (venom-shell.service). When its socket is present the
# console terminal proxies commands there for a full-privilege shell; when
# it's absent (dev boxes) we fall back to the in-process sandboxed shell.
SHELL_SOCK = "/run/venom-shell/shell.sock"
CMD_TIMEOUT = 300  # must match venom.shell_server; console waits this long
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

BANNER = (
    " ██╗   ██╗███████╗███╗   ██╗ ██████╗ ███╗   ███╗\n"
    " ██║   ██║██╔════╝████╗  ██║██╔═══██╗████╗ ████║\n"
    " ██║   ██║█████╗  ██╔██╗ ██║██║   ██║██╔████╔██║\n"
    " ╚██╗ ██╔╝██╔══╝  ██║╚██╗██║██║   ██║██║╚██╔╝██║\n"
    "  ╚████╔╝ ███████╗██║ ╚████║╚██████╔╝██║ ╚═╝ ██║\n"
    "   ╚═══╝  ╚══════╝╚═╝  ╚═══╝ ╚═════╝ ╚═╝     ╚═╝"
)

PAGE = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>VENOM // console</title><style>
:root{--g:#00ff9c;--dim:#0b4433;--amber:#ffb000;--red:#ff2e4d;--bg:#020604}
*{box-sizing:border-box}
::selection{background:var(--g);color:#000}
body{margin:0 auto;max-width:780px;padding:14px;background:var(--bg);color:var(--g);
font:13px/1.5 'Courier New',ui-monospace,monospace;text-shadow:0 0 4px currentColor}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:9;
background:repeating-linear-gradient(0deg,rgba(0,0,0,.16) 0 1px,transparent 1px 3px);
animation:flick .12s infinite}
body::after{content:'';position:fixed;inset:0;pointer-events:none;z-index:8;
background:radial-gradient(ellipse at center,transparent 60%,rgba(0,0,0,.5))}
@keyframes flick{50%{opacity:.9}}
.wrap{overflow-x:auto}
.banner{white-space:pre;margin:0;font-size:9px;line-height:1.05;color:var(--g);
filter:drop-shadow(0 0 6px var(--g))}
.lbl{color:#3fae86;font-size:10px;letter-spacing:2px;text-transform:uppercase}
.bar{border:1px solid var(--dim);padding:9px 11px;margin:9px 0;
background:linear-gradient(180deg,rgba(0,255,156,.03),transparent)}
.led{display:inline-block;padding:2px 8px;border:1px solid var(--dim);margin:3px 5px 3px 0}
.on{color:var(--g);border-color:var(--g);box-shadow:0 0 9px rgba(0,255,156,.4)}
.off{color:var(--red);border-color:var(--red);text-shadow:0 0 4px var(--red)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:7px 14px;margin-top:6px}
.v{display:flex;align-items:center;gap:8px}.v span:first-child{min-width:52px}
.meter{height:9px;flex:1;background:#04160f;border:1px solid var(--dim);overflow:hidden}
.meter i{display:block;height:100%;width:0;background:var(--g);box-shadow:0 0 8px var(--g);
transition:width .5s}.hot i{background:var(--amber)}.crit i{background:var(--red)}
#log{background:#03100b;border:1px solid var(--dim);height:240px;overflow-y:auto;padding:8px;
font-size:12.5px}#log div{white-space:pre-wrap;word-break:break-word;margin:1px 0}
.you{color:#7fffd4}.jarvis{color:var(--g)}.sys{color:var(--amber);opacity:.9}
input,button,select{font:inherit;background:#03100b;color:var(--g);border:1px solid var(--dim);
padding:7px 10px;text-shadow:inherit;outline:none}
button{cursor:pointer}button:hover{background:var(--dim);box-shadow:0 0 8px rgba(0,255,156,.3)}
button:active{transform:translateY(1px)}
.row{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:6px 0}
input:focus{border-color:var(--g);box-shadow:0 0 8px rgba(0,255,156,.3)}
#say input{flex:1;min-width:120px}#say span{color:var(--g)}
details{border:1px solid var(--dim);margin:7px 0;padding:5px 10px}
summary{cursor:pointer;user-select:none;letter-spacing:1px}
pre{white-space:pre-wrap;font-size:11px;background:#03100b;border:1px solid var(--dim);
padding:8px;overflow-x:auto}
.np{color:var(--g);max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
</style>
<div class=wrap><pre class=banner>__BANNER__</pre></div>
<div class=lbl>personal voice node // <span id=clock>--:--:--</span> //
build <span id=ver>?</span></div>
<div class=bar id=status></div>
<div class=bar><div class=lbl>system vitals</div><div class=grid id=vitals></div></div>
<div class=bar><div class=lbl>uplink log</div><div id=log></div>
<form class=row id=say><span>&gt;</span>
<input id=text placeholder="transmit to venom..." autocomplete=off>
<button>SEND</button></form></div>
<div class=bar><div class=lbl>audio</div>
<div class=row><span class=np id=np>— idle —</span></div>
<div class=row><input id=song placeholder="track / artist" style=max-width:220px>
<button onclick="music('play',song.value)">&#9654; PLAY</button>
<button onclick="music('pause')">&#10074;&#10074;</button>
<button onclick="music('resume')">&#9654;</button>
<button onclick="music('stop')">&#9632; STOP</button></div>
<div class=v><span class=lbl>vol</span><div class=meter><i id=volbar></i></div>
<button onclick="vol(-10)">&minus;</button><button onclick="vol(10)">+</button></div></div>
<details><summary>[+] BLUETOOTH</summary><div class=row>
<button onclick="bt(0)">paired</button><button onclick="bt(1)">scan 8s</button></div>
<div id=btlist></div></details>
<details><summary>[+] TIMERS</summary><div id=timers class=lbl>—</div></details>
<details ontoggle="if(this.open)loadSettings()"><summary>[+] SETTINGS</summary>
<div id=settings></div>
<div class=row><button onclick="saveSettings()">save &amp; restart</button></div></details>
<details ontoggle="if(this.open)loadMem()"><summary>[+] MEMORY</summary>
<pre id=mem>...</pre></details>
<details ontoggle="if(this.open)loadLogs()"><summary>[+] LOGS</summary>
<div class=row><button onclick="loadLogs()">refresh</button></div><pre id=logs></pre></details>
<details ontoggle="if(this.open)termInit()"><summary>[+] TERMINAL</summary>
<pre id=term style=height:230px;overflow-y:auto;margin:6px 0></pre>
<form class=row id=termform><span id=cwd style=color:var(--amber)>~</span>
<input id=termin autocomplete=off spellcheck=false autocapitalize=off
style="flex:1;min-width:120px"></form></details>
<details><summary>[+] SYSTEM</summary><div class=row>
<button onclick="sys('update')">&#11015; update</button>
<button onclick="sys('restart')">&#8635; restart</button>
<button onclick="sys('reboot')">&#9888; reboot</button>
<button onclick="sys('poweroff')" style=border-color:var(--red)>&#9211; power off</button>
<button onclick="localStorage.removeItem('vtok');location.reload()">lock</button></div>
<div class=lbl style=margin-top:6px>always power off here (or ssh + `sudo poweroff`) and wait
for the green LED to stop before unplugging &mdash; never pull power live</div></details>
<script>
const $=id=>document.getElementById(id),H=s=>(s+'').replace(/[<&]/g,c=>c=='<'?'&lt;':'&amp;');
let n=0;
function tok(){return localStorage.getItem('vtok')||''}
async function api(p,b){
const o=b?{method:'POST',body:JSON.stringify(b)}:{};
o.headers={'Authorization':'Bearer '+tok()};
const r=await fetch(p,o);
if(r.status==401){const t=prompt('ACCESS PIN:');
if(t){localStorage.setItem('vtok',t.trim());return api(p,b)}}
return r}
function led(k,v,ok){return `<span class="led ${ok?'on':'off'}">${k}:${H(v)}</span>`}
async function tick(){try{const s=await(await api('/api/state')).json();
$('ver').textContent=s.version||'?';
$('status').innerHTML=led('VOX',s.voice,s.voice!='reconnecting')
+led('NET',s.internet?'online':'offline',s.internet)
+led('MIC',s.headset?'linked':'none',!!s.headset)
+led('CPU',s.brain||'none',!!s.brain);
$('np').textContent=s.now_playing?'♪ '+s.now_playing:'— idle —';
if(s.volume!=null){const i=$('volbar');i.style.width=Math.round(s.volume*100)+'%';
i.parentNode.parentNode.className='v'}
$('timers').innerHTML=(s.timers&&s.timers.length)?s.timers.map(t=>
`&#9202; ${H(t.label)} &mdash; ${t.mins}m`).join('<br>'):'no active timers';
if(s.transcript.length!=n){n=s.transcript.length;
$('log').innerHTML=s.transcript.map(([w,t])=>{
const c=w.startsWith('you')?'you':w=='jarvis'?'jarvis':'sys';
const p=c=='you'?'&gt; ':c=='jarvis'?'jarvis&#9002; ':'&#9679; ';
return `<div class=${c}>${p}${H(t)}</div>`}).join('');
$('log').scrollTop=1e9}}catch(e){}}
function meter(lbl,pct,txt,warn,crit){
let cls=pct>=crit?'crit':pct>=warn?'hot':'';
return `<div class="v ${cls}"><span class=lbl>${lbl}</span>`+
(pct==null?`<span>${H(txt)}</span>`:
`<div class=meter><i style=width:${Math.max(0,Math.min(100,pct))}%></i></div>`+
`<span style=min-width:44px>${H(txt)}</span>`)+`</div>`}
async function vtick(){try{const v=await(await api('/api/vitals')).json();
const w=v.wifi||{},sig=w.dbm!=null?Math.max(0,Math.min(100,(w.dbm+90)*2.5)):null;
$('vitals').innerHTML=[
meter('cpu',v.cpu_pct,(v.cpu_pct??'?')+'%',70,90),
meter('temp',v.temp_c,(v.temp_c??'?')+'°C',65,80),
meter('ram',v.mem_pct,(v.mem_pct??'?')+'%',75,90),
meter('disk',v.disk_pct,(v.disk_pct??'?')+'%',80,92),
meter('wifi',sig,w.dbm!=null?w.dbm+'dBm':'n/a',-1,-1),
meter('up',null,v.uptime||'?')
].join('')}catch(e){}}
$('say').onsubmit=e=>{e.preventDefault();const t=$('text').value.trim();
if(t)api('/api/prompt',{text:t});$('text').value=''};
function music(a,q){api('/api/music',{action:a,query:q||''})}
function vol(d){api('/api/volume',{delta:d}).then(()=>setTimeout(tick,400))}
function sys(a){if(a=='reboot'&&!confirm('REBOOT the Pi?'))return;
if(a=='poweroff'&&!confirm('POWER OFF the Pi? Wait for the green LED to stop, then unplug.'))return;
if(a=='update'&&!confirm('Pull latest from GitHub and reinstall?'))return;
api('/api/system',{action:a}).then(async r=>{if(a=='poweroff')alert((await r.json()).result)})}
async function bt(scan){$('btlist').innerHTML='<div class=lbl>scanning...</div>';
const d=await(await api('/api/bluetooth'+(scan?'/scan':''))).json();
$('btlist').innerHTML=d.map(x=>`<div class=row><span class="led ${x.connected?'on':''}">`+
`${H(x.name)} ${x.connected?'&#10003;':''}</span>`+
`<button onclick="btUse('${x.mac}','${H(x.name).replace(/'/g,'')}')">use</button></div>`
).join('')||'<div class=lbl>none found</div>'}
function btUse(m,n){if(confirm('Switch headset to '+n+'? Venom restarts.'))
api('/api/bluetooth',{mac:m,name:n})}
async function loadSettings(){const s=await(await api('/api/settings')).json();
$('settings').innerHTML=Object.entries(s).map(([k,v])=>
`<div class=row><span class=lbl style=min-width:150px>${k}</span>`+
`<input data-k=${k} value="${H(v)}"></div>`).join('')}
async function saveSettings(){const b={};document.querySelectorAll('#settings input')
.forEach(i=>b[i.dataset.k]=i.value);
if(!confirm('Save and restart Venom?'))return;
alert((await(await api('/api/settings',b)).json()).result)}
async function loadLogs(){$('logs').textContent='loading...';
$('logs').textContent=(await(await api('/api/logs')).json()).text}
async function loadMem(){$('mem').textContent='loading...';
$('mem').textContent=(await(await api('/api/memory')).json()).text}
let hist=[],hp=0;
async function runTerm(c){const r=await(await api('/api/term',{cmd:c})).json();
$('term').textContent+=$('cwd').textContent+'$ '+c+'\\n'+(r.out||'')+'\\n';
$('cwd').textContent=r.cwd;$('term').scrollTop=1e9}
function termInit(){setTimeout(()=>$('termin').focus(),60);
if(!$('term').textContent){$('term').textContent=
'venom root shell // full privileges \\u2014 mkdir/apt/sudo all work. \\u2191/\\u2193 = history\\n';
runTerm('whoami; pwd')}}
$('termform').onsubmit=e=>{e.preventDefault();const c=$('termin').value;
if(c.trim()){hist.push(c);hp=hist.length;runTerm(c)}$('termin').value=''};
$('termin').onkeydown=e=>{if(e.key=='ArrowUp'&&hp>0){$('termin').value=hist[--hp];
e.preventDefault()}else if(e.key=='ArrowDown'){hp=Math.min(hist.length,hp+1);
$('termin').value=hist[hp]||''}};
setInterval(()=>$('clock').textContent=new Date().toLocaleTimeString(),1000);
setInterval(tick,1500);setInterval(vtick,3000);tick();vtick();
</script>""".replace("__BANNER__", BANNER)


class WebConsole:
    """Owns the HTTP thread; the voice loop attaches itself on each start."""

    def __init__(self, port: int = 8787, token: str = ""):
        self.port = port
        self.token = token
        self.orchestrator = None  # set by attach(); may be replaced on restart
        self.loop = None
        self._prev_cpu = None  # (idle, total) for %-usage deltas
        self._cwd = None       # terminal working dir, persisted across calls
        self._prev_cwd = None  # for `cd -`

    def authorized(self, headers) -> bool:
        """A request is allowed when no token is set, or it presents it."""
        if not self.token:
            return True
        supplied = (headers.get("Authorization", "") or "").removeprefix("Bearer ")
        return supplied == self.token

    def attach(self, orchestrator, loop) -> None:
        self.orchestrator = orchestrator
        self.loop = loop

    # ── actions called from HTTP threads ─────────────────────────────────────
    def state(self) -> dict:
        orch = self.orchestrator
        base = {"voice": "starting", "transcript": [], "now_playing": "",
                "internet": True, "headset": None, "brain": None,
                "version": "", "timers": [], "volume": None}
        try:
            from venom.config import load_config

            status = json.loads(load_config().status_path.read_text())
            base.update({k: status.get(k)
                         for k in ("internet", "headset", "brain", "version")})
        except Exception:
            pass
        if orch is not None:
            base["voice"] = orch.state
            base["transcript"] = list(orch.transcript)
            base["now_playing"] = orch.music.now_playing
            base["timers"] = [
                {"label": label, "mins": round(mins, 1)}
                for label, mins in orch.timers.pending()
            ]
        base["volume"] = self._volume_level()
        return base

    # ── telemetry ────────────────────────────────────────────────────────────
    @staticmethod
    def _read(path: str, default: str = "") -> str:
        try:
            return Path(path).read_text()
        except OSError:
            return default

    @staticmethod
    def _volume_level() -> float | None:
        try:
            out = subprocess.run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
                                 capture_output=True, text=True, timeout=3).stdout
            return float(out.split()[1])  # "Volume: 0.70 [MUTED]"
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            return None

    def vitals(self) -> dict:
        """Live system health for the dashboard — all from /proc and /sys."""
        v: dict = {}

        temp = self._read("/sys/class/thermal/thermal_zone0/temp").strip()
        v["temp_c"] = round(int(temp) / 1000, 1) if temp.isdigit() else None

        # CPU %: delta of idle vs total jiffies between polls.
        fields = self._read("/proc/stat").split("\n")[0].split()[1:]
        if fields:
            nums = [int(x) for x in fields]
            idle, total = nums[3] + nums[4], sum(nums)
            if self._prev_cpu:
                di, dt = idle - self._prev_cpu[0], total - self._prev_cpu[1]
                v["cpu_pct"] = round(100 * (1 - di / dt), 1) if dt > 0 else None
            self._prev_cpu = (idle, total)

        mem = {k.split(":")[0]: int(k.split()[1])
               for k in self._read("/proc/meminfo").splitlines()[:5] if ":" in k}
        if "MemTotal" in mem and "MemAvailable" in mem:
            v["mem_pct"] = round(
                100 * (1 - mem["MemAvailable"] / mem["MemTotal"]), 1)
            v["mem_total_mb"] = round(mem["MemTotal"] / 1024)

        up = self._read("/proc/uptime").split()
        if up:
            secs = int(float(up[0]))
            v["uptime"] = f"{secs // 3600}h {secs % 3600 // 60}m"

        try:
            import os

            st = os.statvfs("/")  # Linux-only; absent on dev boxes
            v["disk_pct"] = round(100 * (1 - st.f_bavail / st.f_blocks), 1)
        except (OSError, AttributeError):
            pass

        v["wifi"] = self._wifi()
        return v

    @staticmethod
    def _wifi() -> dict:
        """SSID + signal dBm from iw, else /proc/net/wireless."""
        for iw in ("/usr/sbin/iw", "iw"):
            try:
                out = subprocess.run([iw, "dev", "wlan0", "link"],
                                     capture_output=True, text=True, timeout=4).stdout
            except (OSError, subprocess.SubprocessError):
                continue
            info: dict = {}
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("SSID:"):
                    info["ssid"] = line[5:].strip()
                elif line.startswith("signal:"):
                    try:
                        info["dbm"] = int(line.split()[1])
                    except (ValueError, IndexError):
                        pass
            if info:
                return info
        return {}

    def memory_dump(self) -> str:
        try:
            from flint_core.memory import MemoryStore
            from venom.config import load_config

            text = MemoryStore(load_config().memory_path).render_for_prompt()
            return text or "(nothing remembered yet)"
        except Exception as exc:
            return f"(memory unavailable: {exc})"

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
        if action not in ("update", "restart", "reboot", "poweroff"):
            return "unknown action"
        try:
            CONTROL_REQUEST.write_text(action)
        except OSError as exc:
            return f"control channel unavailable: {exc}"
        notes = {"update": "Updating from GitHub — takes a few minutes, "
                           "then Venom restarts.",
                 "restart": "Restarting Venom...",
                 "reboot": "Rebooting the Pi...",
                 "poweroff": "Shutting down cleanly — wait for the green LED "
                             "to stop blinking, then it's safe to unplug."}
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

    def _root_shell(self, cmd: str) -> dict | None:
        """Proxy the command to the root shell daemon (venom-shell.service)
        over its Unix socket. Returns None when the daemon isn't reachable so
        the caller can fall back to the in-process sandboxed shell."""
        if not os.path.exists(SHELL_SOCK):
            return None
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(CMD_TIMEOUT + 10)
            s.connect(SHELL_SOCK)
        except OSError:
            return None  # daemon down / stale socket → fall back
        try:
            with s:
                s.sendall((json.dumps({"cmd": cmd}) + "\n").encode())
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
            return json.loads(buf)
        except (OSError, ValueError) as exc:
            return {"out": f"[root shell error: {exc}]", "cwd": "?"}

    def terminal(self, cmd: str) -> dict:
        """Run a shell command on the Pi and return combined output, tracking
        the working directory across calls so `cd` behaves. Prefers the root
        shell daemon (full privileges); falls back to the in-process shell,
        which runs as the unprivileged, ProtectSystem=strict venom user."""
        root = self._root_shell((cmd or "").strip())
        if root is not None:
            return root

        if self._cwd is None:
            self._cwd = "/opt/venom/app" if os.path.isdir("/opt/venom/app") else "/"
        cmd = (cmd or "").strip()
        if not cmd:
            return {"out": "", "cwd": self._cwd}

        # cd is a shell builtin — subprocess can't persist it, so handle it.
        if cmd == "cd" or cmd.startswith("cd "):
            target = cmd[2:].strip() or "/"
            if target == "-":
                target = self._prev_cwd or self._cwd
            new = os.path.normpath(
                os.path.join(self._cwd, os.path.expanduser(target)))
            if os.path.isdir(new):
                self._prev_cwd, self._cwd = self._cwd, new
                return {"out": "", "cwd": self._cwd}
            return {"out": f"cd: {target}: not a directory", "cwd": self._cwd}

        try:
            # A real bash login shell: pipes, globs, redirection, $VARS,
            # command substitution, aliases in /etc/profile — the full set.
            r = subprocess.run(["/bin/bash", "-lc", cmd], cwd=self._cwd,
                               capture_output=True, text=True, timeout=30,
                               env={**os.environ, "TERM": "xterm-256color",
                                    "HOME": "/var/lib/venom"})
            out = (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            out = "[timed out after 30s]"
        except Exception as exc:
            out = f"[error: {exc}]"
        return {"out": out[-20000:], "cwd": self._cwd}

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

            def _send(self, body: bytes, ctype: str = "application/json",
                      code: int = 200):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _guard(self) -> bool:
                """Serve 401 for unauthorized API calls; True when allowed."""
                if console.authorized(self.headers):
                    return True
                self._send(b'{"error":"unauthorized"}', code=401)
                return False

            def do_GET(self):
                if self.path.startswith("/api/") and not self._guard():
                    return
                if self.path == "/api/state":
                    self._send(json.dumps(console.state()).encode())
                elif self.path.startswith("/api/bluetooth"):
                    scan = 8 if "scan" in self.path else 0
                    self._send(json.dumps(console.bluetooth_list(scan)).encode())
                elif self.path == "/api/settings":
                    self._send(json.dumps(console.settings_get()).encode())
                elif self.path == "/api/logs":
                    self._send(json.dumps({"text": console.logs()}).encode())
                elif self.path == "/api/vitals":
                    self._send(json.dumps(console.vitals()).encode())
                elif self.path == "/api/memory":
                    self._send(json.dumps({"text": console.memory_dump()}).encode())
                else:
                    self._send(PAGE.encode(), "text/html; charset=utf-8")

            def do_POST(self):
                if not self._guard():
                    return
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
                elif self.path == "/api/term":
                    self._send(json.dumps(
                        console.terminal(str(data.get("cmd", "")))).encode())
                else:
                    self._send(b"{}")

        server = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True,
                         name="venom-web").start()
        log.info("web console on http://0.0.0.0:%d", self.port)
