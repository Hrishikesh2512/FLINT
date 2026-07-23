"""Venom web console — manage and prompt the wearable from any browser.

A tiny stdlib HTTP server (no new dependencies) on the Pi serving one
page: live status, conversation transcript, a text prompt box that
feeds straight into the Gemini session, and music/volume controls.
The voice loop stays the owner of all state; this thread only reads
snapshots and posts messages onto thread-safe queues.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import shlex
import socket
import subprocess
import threading
import time
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

# Brute-force protection for the console PIN. The terminal here is a root
# shell, so the PIN is the only thing between the LAN and full device control:
# lock an IP out after a burst of wrong guesses instead of letting it grind
# through the keyspace.
LOCKOUT_THRESHOLD = 8      # consecutive wrong PINs from one IP before a lock
LOCKOUT_SECONDS = 300     # how long that IP stays locked out


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=theme-color content="#03110a">
<title>VENOM // CONSOLE</title><style>
:root{
--grn:#22e07d;--grn2:#8dffc4;--grn-d:#0f4a2e;--grn-x:#06170f;--grn-h:rgba(34,224,125,.30);
--amber:#f0a824;--tan:#c9b892;--red:#ff4242;--red-d:#4a0f14;
--bg:#020705;--pnl:rgba(5,17,11,.87);--pnl2:rgba(8,28,18,.90);--ink:#d6f6e6;--dim:#5c8f77;
--tab:#08201512;--r:3px}
*{box-sizing:border-box}
::selection{background:var(--grn);color:#001208}
html{-webkit-text-size-adjust:100%}
body{margin:0;padding:0 0 40px;background:var(--bg);color:var(--ink);
font:13px/1.55 ui-monospace,'Courier New',monospace;letter-spacing:.2px;
text-shadow:0 0 5px rgba(34,224,125,.20);overflow-x:hidden}
.page{max-width:1180px;margin:0 auto;padding:10px 12px;position:relative;z-index:2}
/* ── the world tree: crowned at the centre, timelines out of both hands ── */
#tree{position:fixed;inset:0;z-index:0;pointer-events:none;opacity:.72;
animation:breathe 9s ease-in-out infinite}
#tree path{fill:none;stroke:url(#strand);stroke-linecap:round;
animation:grow 2.6s cubic-bezier(.25,.9,.3,1) backwards}
#tree .neb{filter:blur(18px)}
#tree .heart{animation:pyre 4.5s ease-in-out infinite;transform-origin:600px 300px}
@keyframes pyre{50%{opacity:.72;transform:scale(1.08)}}
#tree circle.tip{fill:#bfffe0;animation:spark 4.5s ease-in-out infinite}
@keyframes grow{from{stroke-dashoffset:var(--len)}to{stroke-dashoffset:0}}
@keyframes rise{from{opacity:0}to{opacity:1}}
@keyframes breathe{50%{opacity:.74}}
@keyframes spark{0%,100%{opacity:.12}50%{opacity:.75}}
/* ── HUD targeting frame ── */
.hudf{position:fixed;inset:7px;z-index:7;pointer-events:none}
.hudf span{position:absolute;width:22px;height:22px;border:1px solid var(--grn);opacity:.45}
.hudf span:nth-child(1){top:0;left:0;border-right:0;border-bottom:0}
.hudf span:nth-child(2){top:0;right:0;border-left:0;border-bottom:0}
.hudf span:nth-child(3){bottom:0;left:0;border-right:0;border-top:0}
.hudf span:nth-child(4){bottom:0;right:0;border-left:0;border-top:0}
/* ── CRT dressing ── */
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:9;
background:repeating-linear-gradient(0deg,rgba(0,0,0,.20) 0 1px,transparent 1px 3px);
animation:flick .12s infinite}
body::after{content:'';position:fixed;inset:0;pointer-events:none;z-index:8;
background:radial-gradient(ellipse at 50% 40%,transparent 52%,rgba(0,0,0,.72))}
@keyframes flick{50%{opacity:.88}}
/* ── header: case plate ── */
.hdr{display:flex;align-items:center;gap:13px;padding:10px 12px 12px;margin:0 -12px;background:linear-gradient(180deg,rgba(2,7,5,.94),rgba(2,7,5,.62));backdrop-filter:blur(2px);
border-bottom:1px solid var(--grn-d);position:relative}
.hdr::after{content:'';position:absolute;left:0;right:0;bottom:-4px;height:2px;
background:repeating-linear-gradient(90deg,var(--grn-d) 0 14px,transparent 14px 22px)}
.seal{flex:0 0 auto;width:70px;height:70px;filter:drop-shadow(0 0 9px var(--grn-h))}
.seal .ring{fill:none;stroke:var(--grn);stroke-width:1.1;opacity:.8}
.seal .spin{fill:none;stroke:var(--grn2);stroke-width:1.6;opacity:.6;stroke-dasharray:2 7;
transform-origin:50% 50%;animation:spin 18s linear infinite}
.seal .coils path{fill:var(--grn);opacity:.5}
.seal .glow{fill:url(#core);transform-origin:50% 50%;animation:reactor 3.2s ease-in-out infinite}
.seal .hub{fill:#eaffef;filter:drop-shadow(0 0 7px var(--grn2))}
.seal text{fill:var(--grn2);font:600 4.9px ui-monospace,monospace;letter-spacing:1.05px}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes reactor{50%{opacity:.75;transform:scale(1.1)}}
body.live .seal{animation:aura 1.9s ease-in-out infinite}
@keyframes aura{50%{filter:drop-shadow(0 0 17px var(--grn))}}
.id{flex:1;min-width:0}
.org{color:var(--tan);font-size:8.5px;letter-spacing:3.4px;text-transform:uppercase;opacity:.82}
h1{margin:1px 0 2px;font-size:clamp(21px,6.4vw,32px);line-height:1;letter-spacing:9px;
color:var(--grn2);text-shadow:0 0 16px var(--grn-h),0 0 3px var(--grn);font-weight:700}
.sub{color:var(--dim);font-size:9.5px;letter-spacing:1.4px;white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}
.clockbox{text-align:right;flex:0 0 auto}
#clock{color:var(--amber);font-size:clamp(13px,3.6vw,17px);letter-spacing:2.4px;
text-shadow:0 0 10px rgba(240,168,36,.5)}
.tzl{color:var(--dim);font-size:8px;letter-spacing:2.2px;text-transform:uppercase}
/* ── panels ── */
.bar{border:1px solid var(--grn-d);background:
linear-gradient(180deg,rgba(34,224,125,.045),rgba(0,0,0,0) 60%),var(--pnl);
padding:11px 12px 12px;margin:10px 0;position:relative;border-radius:var(--r)}
.bar::before,.bar::after{content:'';position:absolute;width:7px;height:7px;
border:1px solid var(--grn);opacity:.55}
.bar::before{top:-1px;left:-1px;border-right:0;border-bottom:0}
.bar::after{bottom:-1px;right:-1px;border-left:0;border-top:0}
.tab{margin:-20px 0 10px -1px;display:inline-block;font-size:9px;letter-spacing:3.2px;
text-transform:uppercase;color:#03150c;background:var(--grn);padding:2px 11px 2px 9px;
font-weight:700;text-shadow:none;clip-path:polygon(0 0,100% 0,calc(100% - 7px) 100%,0 100%)}
.tab b{color:#03150c;opacity:.55;font-weight:700;margin-right:5px}
.lbl{color:var(--dim);font-size:9px;letter-spacing:2.2px;text-transform:uppercase}
.note{color:var(--dim);font-size:9.5px;letter-spacing:.6px;line-height:1.5;margin-top:7px}
.note b{color:var(--tan);font-weight:400}
/* ── layout ── */
.top{display:grid;gap:10px;grid-template-columns:1fr}
.cols{display:grid;gap:10px;grid-template-columns:1fr}
@media(min-width:880px){
.top{grid-template-columns:minmax(300px,.85fr) 1.15fr;align-items:start}
.cols{grid-template-columns:1fr 1fr}}
@media(min-width:1180px){.cols{grid-template-columns:1fr 1fr 1fr}}
.top>.bar,.cols>details{margin:0}
/* ── status chips: rubber-stamped clearances ── */
.chips{display:flex;flex-wrap:wrap;gap:7px}
.chip{border:1px solid var(--grn-d);background:var(--grn-x);padding:4px 10px;
font-size:10px;letter-spacing:1.6px;text-transform:uppercase;color:var(--dim);
border-radius:2px;display:flex;gap:7px;align-items:center;flex:1 1 auto;justify-content:center}
.chip i{width:6px;height:6px;border-radius:50%;background:var(--dim);flex:0 0 auto}
.chip b{font-weight:400;color:var(--ink)}
.chip.on{border-color:var(--grn);color:var(--grn2);box-shadow:0 0 11px rgba(34,224,125,.20) inset}
.chip.on i{background:var(--grn);box-shadow:0 0 8px var(--grn);animation:pulse 2.4s infinite}
.chip.off{border-color:var(--red);color:var(--red)}
.chip.off i{background:var(--red);box-shadow:0 0 8px var(--red)}
@keyframes pulse{50%{opacity:.35}}
/* ── HUD dials ── */
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(64px,1fr));gap:7px}
.dial{text-align:center;position:relative}
.dial svg{width:100%;max-width:86px;aspect-ratio:1;display:block;margin:0 auto}
.dial .tk{fill:none;stroke:var(--grn);stroke-width:9;opacity:.12;stroke-dasharray:1 4}
.dial .trk{fill:none;stroke:var(--grn-d);stroke-width:4}
.dial .arcv{fill:none;stroke:var(--grn);stroke-width:4;stroke-linecap:round;
transform:rotate(-90deg);transform-origin:50% 50%;
transition:stroke-dasharray .6s cubic-bezier(.4,0,.2,1);
filter:drop-shadow(0 0 5px var(--grn-h))}
.dial.hot .arcv{stroke:var(--amber);filter:drop-shadow(0 0 5px rgba(240,168,36,.5))}
.dial.crit .arcv{stroke:var(--red);filter:drop-shadow(0 0 6px rgba(255,66,66,.5))}
.dial .dv{position:absolute;top:44%;left:0;right:0;transform:translateY(-50%);
font-size:12.5px;color:var(--grn2)}
.dial.hot .dv{color:var(--amber)}.dial.crit .dv{color:var(--red)}
.dial .dl{font-size:8.5px;letter-spacing:2.4px;color:var(--dim);text-transform:uppercase;
margin-top:-3px}
.readout{display:flex;justify-content:space-between;gap:8px;padding:6px 10px;margin-top:9px;
border:1px solid var(--grn-d);background:var(--grn-x);border-radius:2px;font-size:11px}
.readout b{font-weight:400;color:var(--amber)}
.v{display:flex;align-items:center;gap:9px}
.v{display:flex;align-items:center;gap:9px}
.v>span:first-child{min-width:44px;flex:0 0 auto}
.meter{position:relative;height:11px;flex:1;background:#02120b;
border:1px solid var(--grn-d);overflow:hidden;border-radius:1px}
.meter::after{content:'';position:absolute;inset:0;pointer-events:none;
background:repeating-linear-gradient(90deg,transparent 0 9px,rgba(34,224,125,.22) 9px 10px)}
.meter i{display:block;height:100%;width:0;background:
linear-gradient(90deg,var(--grn-d),var(--grn));box-shadow:0 0 10px var(--grn);
transition:width .55s cubic-bezier(.4,0,.2,1)}
.hot i{background:linear-gradient(90deg,#5a3c00,var(--amber));box-shadow:0 0 10px var(--amber)}
.crit i{background:linear-gradient(90deg,#4a0f14,var(--red));box-shadow:0 0 10px var(--red)}
.val{min-width:52px;text-align:right;color:var(--amber);font-size:11.5px;flex:0 0 auto}
/* ── uplink log ── */
#log{background:rgba(1,14,8,.93);border:1px solid var(--grn-d);height:clamp(210px,34vh,330px);
overflow-y:auto;padding:9px 10px;font-size:12.5px;border-radius:2px;
background-image:linear-gradient(180deg,rgba(34,224,125,.05),transparent 90px)}
#log div{white-space:pre-wrap;word-break:break-word;margin:2px 0}
#log b{font-weight:400;opacity:.62;letter-spacing:1.4px;font-size:10px}
.you{color:var(--tan)}.jarvis{color:var(--grn2)}.sys{color:var(--amber)}
#log::-webkit-scrollbar,pre::-webkit-scrollbar{width:8px;height:8px}
#log::-webkit-scrollbar-thumb,pre::-webkit-scrollbar-thumb{background:var(--grn-d)}
/* ── controls ── */
.row{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:8px 0 0}
input,button,select{font:inherit;background:#02120b;color:var(--ink);
border:1px solid var(--grn-d);padding:8px 11px;text-shadow:inherit;outline:none;
border-radius:2px;min-height:38px}
input::placeholder{color:#3f6b58}
input:focus{border-color:var(--grn);box-shadow:0 0 0 1px rgba(34,224,125,.28)}
button{cursor:pointer;color:var(--grn2);letter-spacing:1.5px;text-transform:uppercase;
font-size:10.5px;background:linear-gradient(180deg,var(--grn-x),#020d08);
border-color:var(--grn-d);white-space:nowrap}
button:hover{border-color:var(--grn);box-shadow:0 0 12px rgba(34,224,125,.25);color:#fff}
button:active{transform:translateY(1px)}
button.warn{color:var(--amber);border-color:#5a4212}
button.warn:hover{border-color:var(--amber);box-shadow:0 0 12px rgba(240,168,36,.25)}
button.danger{color:var(--red);border-color:#5a1c1c}
button.danger:hover{border-color:var(--red);box-shadow:0 0 12px rgba(255,66,66,.3)}
#say{display:flex;gap:6px;margin-top:9px}
#say input{flex:1;min-width:100px}
#say span{color:var(--grn);align-self:center;letter-spacing:2px;font-size:11px}
/* ── audio ── */
.np{color:var(--grn2);font-size:12px;letter-spacing:1px;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap;padding:5px 9px;border:1px dashed var(--grn-d);
background:#02120b;border-radius:2px}
/* ── nexus event / SOS ── */
.nexus{border-color:#5a1c1c}
.nexus[open]{border-color:var(--red);background:
linear-gradient(180deg,rgba(255,66,66,.07),transparent 130px),var(--pnl2)}
.nexus summary{color:#ff9a9a}
.nexus summary::before{border-color:var(--red)}
.nexus[open] summary{color:var(--red);border-bottom-color:#5a1c1c}
.nexus[open] summary::before{background:var(--red);box-shadow:0 0 9px var(--red)}
.badge{margin-left:auto;font-size:8.5px;letter-spacing:2px;color:var(--dim);
border:1px solid var(--grn-d);padding:2px 8px;border-radius:2px;white-space:nowrap}
.badge.armed{color:#fff;background:var(--red);border-color:var(--red);animation:pulse 1s infinite}
.tape{height:7px;background:repeating-linear-gradient(45deg,var(--amber) 0 9px,#0b0703 9px 18px);
opacity:.5}
#sosstate{font-size:10.5px;letter-spacing:1.8px;text-transform:uppercase;color:var(--dim);
display:flex;flex-wrap:wrap;gap:8px;align-items:center}
#sosstate .ok{color:var(--grn2)}
#sosstate .alarm{color:var(--red);font-weight:700;animation:pulse 1s infinite}
.case{display:flex;gap:7px;align-items:center;margin:7px 0;padding:6px 9px;
border:1px solid var(--grn-d);border-left:3px solid var(--grn);background:var(--grn-x);
border-radius:2px;flex-wrap:wrap}
.case.pausedc{border-left-color:var(--dim);opacity:.62}
.case .who{flex:1;min-width:120px;font-size:12px;color:var(--ink)}
.case .who small{color:var(--tan);opacity:.75;letter-spacing:1px;font-size:9.5px}
.case .who em{color:var(--dim);font-style:normal;font-size:10.5px}
.case button{padding:5px 9px;min-height:30px;font-size:9.5px}
.sosbtn{flex:1;min-width:150px;color:#fff;border-color:var(--red);
background:linear-gradient(180deg,#3a0d10,#180405);font-weight:700;letter-spacing:2.6px}
.sosbtn:hover{box-shadow:0 0 20px rgba(255,66,66,.5);border-color:#ff8a8a}
body.alert{animation:redshift 1.4s ease-in-out infinite}
@keyframes redshift{50%{box-shadow:inset 0 0 130px rgba(255,66,66,.16)}}
/* ── folders ── */
details{border:1px solid var(--grn-d);background:var(--pnl);border-radius:var(--r);
overflow:hidden}
details[open]{background:var(--pnl2);border-color:#1c6b45}
summary{cursor:pointer;user-select:none;list-style:none;padding:10px 12px;
font-size:10px;letter-spacing:3px;text-transform:uppercase;color:var(--tan);
display:flex;align-items:center;gap:9px;min-height:40px}
summary::-webkit-details-marker{display:none}
summary::before{content:'';width:9px;height:9px;border:1px solid var(--grn);
flex:0 0 auto;transition:.2s}
details[open] summary::before{background:var(--grn);box-shadow:0 0 9px var(--grn)}
details[open] summary{color:var(--grn2);border-bottom:1px dashed var(--grn-d)}
.body{padding:10px 12px 13px}
pre{white-space:pre-wrap;font-size:11px;background:rgba(1,14,8,.93);border:1px solid var(--grn-d);
padding:9px;overflow-x:auto;color:var(--dim);border-radius:2px;margin:0}
#term{height:250px;overflow-y:auto;color:var(--grn2)}
#cwd{color:var(--amber);align-self:center;font-size:11px}
#termform{display:flex;gap:6px;margin-top:7px}
#termin{flex:1;min-width:100px}
/* ── footer stamp ── */
.foot{display:flex;justify-content:space-between;align-items:center;gap:12px;
margin-top:18px;padding-top:12px;border-top:1px dashed var(--grn-d);flex-wrap:wrap}
.stamp{border:2px solid var(--amber);color:var(--amber);opacity:.62;
padding:5px 12px;transform:rotate(-3.2deg);font-size:9.5px;letter-spacing:3px;
text-transform:uppercase;box-shadow:0 0 0 3px rgba(240,168,36,.09) inset;border-radius:2px}
.foot .lbl{font-size:8.5px}
@media(prefers-reduced-motion:reduce){*{animation:none!important}}
</style>
<svg id="tree" viewBox="0 0 1200 640" preserveAspectRatio="xMidYMid meet">
<defs>
<radialGradient id="strand" cx="600" cy="300" r="620" gradientUnits="userSpaceOnUse">
<stop offset="0%" stop-color="#dcffee"/><stop offset="14%" stop-color="#5bffb0"/>
<stop offset="34%" stop-color="#22e07d"/><stop offset="54%" stop-color="#19c6b4"/>
<stop offset="74%" stop-color="#3d7fe4"/><stop offset="100%" stop-color="#9b6bff"/>
</radialGradient>
<radialGradient id="halo"><stop offset="0%" stop-color="#8dffc4" stop-opacity=".24"/>
<stop offset="45%" stop-color="#22e07d" stop-opacity=".07"/>
<stop offset="100%" stop-color="#22e07d" stop-opacity="0"/></radialGradient>
<radialGradient id="heart"><stop offset="0%" stop-color="#eaffef" stop-opacity=".85"/>
<stop offset="18%" stop-color="#8dffc4" stop-opacity=".45"/>
<stop offset="52%" stop-color="#22e07d" stop-opacity=".16"/>
<stop offset="100%" stop-color="#22e07d" stop-opacity="0"/></radialGradient>
<radialGradient id="neb1"><stop offset="0%" stop-color="#7b4ddb" stop-opacity=".26"/>
<stop offset="100%" stop-color="#7b4ddb" stop-opacity="0"/></radialGradient>
<radialGradient id="neb2"><stop offset="0%" stop-color="#2f6bd8" stop-opacity=".22"/>
<stop offset="100%" stop-color="#2f6bd8" stop-opacity="0"/></radialGradient>
</defs>
<ellipse class="neb" cx="300" cy="270" rx="430" ry="250" fill="url(#neb1)"></ellipse>
<ellipse class="neb" cx="900" cy="330" rx="430" ry="250" fill="url(#neb2)"></ellipse>
<ellipse class="neb" cx="600" cy="300" rx="600" ry="200" fill="url(#neb1)" opacity=".55"></ellipse>
<circle class="core" cx="600" cy="300" r="300"></circle>
<circle class="heart" cx="600" cy="300" r="150" fill="url(#heart)"></circle>
<g id="canopy"></g>
</svg>
<div class=hudf><span></span><span></span><span></span><span></span></div>
<div class=page>
<header class=hdr>
<svg class=seal viewBox="0 0 100 100">
<defs><path id=arc d="M50 50 m-38 0 a38 38 0 1 1 76 0 a38 38 0 1 1 -76 0"/>
<radialGradient id="core"><stop offset="0%" stop-color="#eaffef" stop-opacity="1"/>
<stop offset="45%" stop-color="#8dffc4" stop-opacity=".85"/>
<stop offset="100%" stop-color="#22e07d" stop-opacity="0"/></radialGradient></defs>
<circle class="ring" cx="50" cy="50" r="47"></circle>
<circle class="spin" cx="50" cy="50" r="41"></circle>
<circle class="ring" cx="50" cy="50" r="30" opacity=".55"></circle>
<g class="coils">
<path d="M50 22 L57 33 L43 33 Z"></path><path d="M78 50 L67 57 L67 43 Z"></path>
<path d="M50 78 L43 67 L57 67 Z"></path><path d="M22 50 L33 43 L33 57 Z"></path>
<path d="M70 30 L67 42 L58 33 Z"></path><path d="M70 70 L58 67 L67 58 Z"></path>
<path d="M30 70 L33 58 L42 67 Z"></path><path d="M30 30 L42 33 L33 42 Z"></path></g>
<circle class="glow" cx="50" cy="50" r="19"></circle>
<circle class="hub" cx="50" cy="50" r="7"></circle>
<text><textPath href=#arc startOffset=1%>VIRTUAL ENHANCED NEURAL OPTIMIZATION MODEL ·</textPath></text>
</svg>
<div class=id><div class=org>Virtual Enhanced Neural Optimization Model</div><h1>VENOM</h1>
<div class=sub>j.a.r.v.i.s. core &middot; stark industries &middot; case <span id=case>VEN-0000</span>
&middot; build <span id=ver>?</span></div></div>
<div class=clockbox><div id=clock>--:--:--</div><div class=tzl>sacred timeline</div></div>
</header>
<section class="bar chips" id=status></section>
<div class=top>
<section class=bar><h2 class=tab><b>01</b>vitals</h2><div class=grid id=vitals></div><div class=readout id=uptime><span class=lbl>uptime</span><b>&mdash;</b></div>
<div class=row style=margin-top:11px><span class=lbl>output</span>
<div class=meter style=flex:1><i id=volbar></i></div>
<button onclick="vol(-10)">&minus;</button><button onclick="vol(10)">+</button></div>
<div class=np id=np style=margin-top:9px>&mdash; idle &mdash;</div>
<div class=row><input id=song placeholder="track / artist" style=flex:1;min-width:110px>
<button onclick="music('play',song.value)">&#9654;</button>
<button onclick="music('pause')">&#10074;&#10074;</button>
<button onclick="music('resume')">&#9654;&#9654;</button>
<button onclick="music('next')">next</button>
<button onclick="music('stop')" class=warn>stop</button></div></section>
<section class=bar><h2 class=tab><b>02</b>j.a.r.v.i.s. uplink</h2><div id=log></div>
<form id=say><span>&gt;</span>
<input id=text placeholder="transmit to venom..." autocomplete=off>
<button>send</button></form></section>
</div>
<div class=cols>
<details class=nexus id=sospanel ontoggle="if(this.open)loadSos()" style=grid-column:1/-1>
<summary>nexus event &mdash; emergency sos <span id=sosbadge class=badge>standby</span></summary>
<div class=tape></div><div class=body>
<div id=sosstate>&mdash;</div>
<div id=soslist style=margin-top:9px></div>
<div class=row><input id=sname placeholder=name style=flex:1;min-width:110px>
<input id=sto placeholder="+91 number / whatsapp name" style=flex:2;min-width:150px>
<input id=slabel placeholder="who (father...)" style=flex:1;min-width:110px>
<button onclick="sosAdd()">add / update</button></div>
<div class=row><input id=snote placeholder="what's happening (optional)"
style=flex:1;min-width:140px>
<button class=sosbtn onclick="sosAct('trigger')">&#9888; send sos</button>
<button onclick="sosAct('stop')">all clear</button>
<button class=warn onclick="sosAct('test')">test</button></div>
<div id=sosmsg class=note style=color:var(--amber)></div>
<div class=note>everyone here gets a WhatsApp with your approximate location and the time,
re-sent every few minutes until you call it off &mdash; <b>a list of its own</b>, so the SOS
can reach different people than you usually message. by voice: <b>&ldquo;add my father to my
emergency contacts&rdquo;</b> &middot; <b>&ldquo;SOS&rdquo;</b> &middot;
<b>&ldquo;I&rsquo;m safe&rdquo;</b>.</div></div></details>

<details><summary>bluetooth</summary><div class=body>
<div class=row style=margin-top:0><button onclick="bt(0)">paired</button>
<button onclick="bt(1)">scan 8s</button></div><div id=btlist></div></div></details>
<details ontoggle="if(this.open)loadWifi()"><summary>network</summary><div class=body>
<div id=wifinow class=lbl>&mdash;</div><div id=wifisaved></div>
<div class=row><input id=wssid placeholder=SSID autocomplete=off style=flex:1;min-width:110px>
<input id=wpass placeholder=password type=password autocomplete=off
style=flex:1;min-width:110px>
<input id=wprio placeholder=prio autocomplete=off style=max-width:70px>
<button onclick="wifiAdd()">add</button><button onclick="wifiScan()">scan</button></div>
<div id=wifimsg class=note style=color:var(--amber)></div><div id=wifiscan class=lbl></div>
<div class=note><b>base</b> = the network the Pi always prefers (your phone hotspot). It
auto-connects to the highest-priority network in range and falls back on its own &mdash;
manage this over Tailscale so a change can&rsquo;t lock you out.</div></div></details>
<details><summary>timers</summary><div class=body><div id=timers class=lbl>&mdash;</div>
</div></details>
<details ontoggle="if(this.open)loadSettings()"><summary>settings</summary><div class=body>
<div id=settings></div>
<div class=row><button class=warn onclick="saveSettings()">save &amp; restart</button></div>
</div></details>
<details ontoggle="if(this.open)loadMem()"><summary>memory</summary><div class=body>
<pre id=mem>...</pre><div class=note>edit in the terminal:
<b>memory set &lt;category&gt; &lt;key&gt; &lt;value&gt;</b> &middot;
<b>memory del &lt;category&gt; &lt;key&gt;</b> &middot; <b>memory categories</b></div>
</div></details>
<details ontoggle="if(this.open)loadConn()"><summary>connections</summary><div class=body>
<pre id=conn>...</pre><div class=note>edit in the terminal:
<b>connections add &lt;name&gt; phone=.. nick=.. insta=..</b> &middot;
<b>connections del &lt;name&gt;</b> &middot; <b>connections show &lt;name&gt;</b></div>
</div></details>
<details ontoggle="if(this.open)loadLogs()"><summary>logs</summary><div class=body>
<div class=row style=margin-top:0><button onclick="loadLogs()">refresh</button></div>
<pre id=logs></pre></div></details>
<details ontoggle="if(this.open)termInit()"><summary>terminal</summary><div class=body>
<pre id=term></pre><form id=termform><span id=cwd>~</span>
<input id=termin autocomplete=off spellcheck=false autocapitalize=off></form></div></details>
<details><summary>system</summary><div class=body>
<div class=row style=margin-top:0><button onclick="sys('update')">&#11015; update</button>
<button onclick="sys('restart')">&#8635; restart</button>
<button class=warn onclick="sys('reboot')">&#9888; reboot</button>
<button class=danger onclick="sys('poweroff')">&#9211; power off</button>
<button onclick="localStorage.removeItem('vtok');location.reload()">lock</button></div>
<div class=note>always power off here (or ssh + <b>sudo poweroff</b>) and wait for the green
LED to stop before unplugging &mdash; never pull power live.</div></div></details>
</div>
<div class=foot><div class=stamp>approved for all time</div>
<div class=lbl>stark industries &middot; j.a.r.v.i.s. core &middot; for all time. always.</div></div>
</div>
<script>
const $=id=>document.getElementById(id),H=s=>(s+'').replace(/[<&]/g,c=>c=='<'?'&lt;':'&amp;');
let n=0;
function tok(){return localStorage.getItem('vtok')||''}
async function api(p,b){
const o=b?{method:'POST',body:JSON.stringify(b)}:{};
o.headers={'Authorization':'Bearer '+tok()};
const r=await fetch(p,o);
if(r.status==401){const t=prompt('VENOM ACCESS PIN:');
if(t){localStorage.setItem('vtok',t.trim());return api(p,b)}}
return r}
function chip(k,v,ok){return `<span class="chip ${ok?'on':'off'}"><i></i>${k}<b>${H(v)}</b></span>`}
async function tick(){try{const s=await(await api('/api/state')).json();
$('ver').textContent=s.version||'?';
document.body.classList.toggle('live',s.voice=='conversation');
$('status').innerHTML=chip('vox',s.voice,s.voice!='reconnecting')
+chip('net',s.internet?'online':'offline',s.internet)
+chip('mic',s.headset?'linked':'none',!!s.headset)
+chip('core',s.brain||'none',!!s.brain);
$('np').textContent=s.now_playing?'\\u266a '+s.now_playing:'\\u2014 idle \\u2014';
if(s.volume!=null)$('volbar').style.width=Math.round(s.volume*100)+'%';
$('timers').innerHTML=(s.timers&&s.timers.length)?s.timers.map(t=>
`&#9202; ${H(t.label)} &mdash; ${t.mins}m`).join('<br>'):'no active timers';
if(s.transcript.length!=n){n=s.transcript.length;
$('log').innerHTML=s.transcript.map(([w,t])=>{
const c=w.startsWith('you')?'you':w=='jarvis'?'jarvis':'sys';
const p=c=='you'?'variant':c=='jarvis'?'jarvis':'node';
return `<div class=${c}><b>${p} &#9656;</b> ${H(t)}</div>`}).join('');
$('log').scrollTop=1e9}}catch(e){}}
function dial(lbl,pct,txt,warn,crit){
const C=2*Math.PI*30,cls=pct>=crit?'crit':pct>=warn?'hot':'';
const p=Math.max(0,Math.min(100,pct||0));
return `<div class="dial ${cls}"><svg viewBox="0 0 80 80">`+
`<circle class=tk cx=40 cy=40 r=34></circle>`+
`<circle class=trk cx=40 cy=40 r=30></circle>`+
`<circle class=arcv cx=40 cy=40 r=30 stroke-dasharray="${C*p/100} ${C}"></circle>`+
`</svg><div class=dv>${H(txt)}</div><div class=dl>${lbl}</div></div>`}
async function vtick(){try{const v=await(await api('/api/vitals')).json();
const w=v.wifi||{},sig=w.dbm!=null?Math.max(0,Math.min(100,(w.dbm+90)*2.5)):null;
$('vitals').innerHTML=[
dial('cpu',v.cpu_pct,(v.cpu_pct??'?')+'%',70,90),
dial('temp',v.temp_c,(v.temp_c??'?')+'\u00b0',65,80),
dial('ram',v.mem_pct,(v.mem_pct??'?')+'%',75,90),
dial('disk',v.disk_pct,(v.disk_pct??'?')+'%',80,92),
dial('wifi',sig,w.dbm!=null?w.dbm:'n/a',101,101)
].join('');
$('uptime').innerHTML=`<span class=lbl>uptime</span><b>${H(v.uptime||'?')}</b>`;
}catch(e){}}
$('say').onsubmit=e=>{e.preventDefault();const t=$('text').value.trim();
if(t)api('/api/prompt',{text:t});$('text').value=''};
function music(a,q){api('/api/music',{action:a,query:q||''})}
function vol(d){api('/api/volume',{delta:d}).then(()=>setTimeout(tick,400))}
function sys(a){if(a=='reboot'&&!confirm('REBOOT the Pi?'))return;
if(a=='poweroff'&&!confirm('POWER OFF the Pi? Wait for the green LED to stop, then unplug.'))return;
if(a=='update'&&!confirm('Pull latest from GitHub and reinstall?'))return;
api('/api/system',{action:a}).then(async r=>{if(a=='poweroff')alert((await r.json()).result)})}
const jsx=s=>(s+'').replace(/'/g,"\\\\'");
async function bt(scan){$('btlist').innerHTML='<div class=lbl>scanning...</div>';
const d=await(await api('/api/bluetooth'+(scan?'/scan':''))).json();
$('btlist').innerHTML=d.map(x=>`<div class=case><span class=who>${H(x.name)} `+
`${x.connected?'<em>&#10003; linked</em>':''}</span>`+
`<button onclick="btUse('${x.mac}','${H(x.name).replace(/'/g,'')}')">use</button></div>`
).join('')||'<div class=lbl>none found</div>'}
function btUse(m,n){if(confirm('Switch headset to '+n+'? Venom restarts.'))
api('/api/bluetooth',{mac:m,name:n})}
async function loadWifi(){let d;try{d=await(await api('/api/wifi')).json()}catch(e){return}
if(!d.nm){$('wifinow').textContent='NetworkManager not available on this box.';
$('wifisaved').innerHTML='';return}
$('wifinow').innerHTML='&#9679; on: '+(d.current.name?H(d.current.name)+
(d.current.signal!=null?' ('+d.current.signal+'%)':''):'&mdash;');
$('wifisaved').innerHTML=d.saved.map(n=>`<div class="case ${n.active?'':'pausedc'}">`+
`<span class=who>${H(n.name)} ${n.active?'<em>&#10003; active</em>':''} `+
`<small>p${n.priority}</small></span>`+
`<button onclick="wifiAct('connect','${jsx(n.name)}')">use</button>`+
`<button onclick="wifiAct('base','${jsx(n.name)}')">base</button>`+
`<button class=danger onclick="wifiAct('remove','${jsx(n.name)}')">del</button>`+
`</div>`).join('')||'<div class=lbl>no saved networks yet</div>'}
async function wifiAct(action,name){if(action=='remove'&&!confirm('Remove '+name+'?'))return;
$('wifimsg').textContent='working...';
const r=await(await api('/api/wifi',{action,name})).json();
$('wifimsg').textContent=r.result||'';loadWifi()}
async function wifiAdd(){const ssid=$('wssid').value.trim();if(!ssid)return;
const b={action:'add',ssid,password:$('wpass').value};
const p=$('wprio').value.trim();if(p)b.priority=p;
$('wifimsg').textContent='working...';
const r=await(await api('/api/wifi',b)).json();
$('wifimsg').textContent=r.result||'';$('wpass').value='';loadWifi()}
async function wifiScan(){$('wifiscan').textContent='scanning...';
let d;try{d=await(await api('/api/wifi')).json()}catch(e){return}
$('wifiscan').innerHTML=(d.available||[]).map(a=>
`<span class=chip style=cursor:pointer onclick="$('wssid').value='${jsx(a.ssid)}';`+
`$('wssid').focus()">${H(a.ssid)} ${a.signal}%${a.known?' &#10003;':''}</span>`
).join(' ')||'<span class=lbl>none found</span>'}
async function loadSos(){let d;try{d=await(await api('/api/sos')).json()}catch(e){return}
document.body.classList.toggle('alert',!!d.active);
$('sosbadge').className='badge'+(d.active?' armed':'');
$('sosbadge').textContent=d.active?'⚠ live':((d.contacts||[]).length
?(d.contacts.filter(c=>c.enabled).length+' on call'):'no contacts');
if(d.active)$('sospanel').open=true;
$('sosstate').innerHTML=d.active
?`<span class=alarm>&#9888; nexus event &mdash; emergency mode live</span>`+
`<span class=lbl>${H(d.summary||'')}</span>`
:`<span class=ok>&#9679; sacred timeline nominal</span>`+
`<span class=lbl>${d.offline?'not live &mdash; contacts editable, nothing can be sent'
:'standby &middot; resend every '+(d.repeat_minutes||10)+' min once armed'}</span>`;
$('soslist').innerHTML=(d.contacts||[]).map(c=>
`<div class="case ${c.enabled?'':'pausedc'}"><span class=who>${H(c.name)} `+
`${c.label?'<small>'+H(c.label)+'</small> ':''}`+
`${c.to?'<em>&rarr; '+H(c.to)+'</em>':'<em>&rarr; by name</em>'}`+
`${c.enabled?'':' <em>[paused]</em>'}</span>`+
`<button onclick="sosAct('${c.enabled?'disable':'enable'}','${jsx(c.name)}')">`+
`${c.enabled?'pause':'resume'}</button>`+
`<button class=danger onclick="sosAct('remove','${jsx(c.name)}')">del</button></div>`
).join('')||'<div class=lbl>no emergency contacts yet</div>'}
async function sosAct(action,name){
if(action=='trigger'&&!confirm('Send the EMERGENCY SOS to every contact right now?'))return;
if(action=='remove'&&!confirm('Remove '+name+' from your emergency contacts?'))return;
$('sosmsg').textContent='working...';
const b={action};if(name)b.name=name;if(action=='trigger')b.note=$('snote').value.trim();
const r=await(await api('/api/sos',b)).json();
$('sosmsg').textContent=r.result||'';loadSos()}
async function sosAdd(){const name=$('sname').value.trim();if(!name)return;
$('sosmsg').textContent='working...';
const r=await(await api('/api/sos',{action:'add',name,to:$('sto').value.trim(),
label:$('slabel').value.trim()})).json();
$('sosmsg').textContent=r.result||'';
$('sname').value=$('sto').value=$('slabel').value='';loadSos()}
async function loadSettings(){const s=await(await api('/api/settings')).json();
$('settings').innerHTML=Object.entries(s).map(([k,v])=>
`<div class=row style=margin-top:5px><span class=lbl style=min-width:130px>${k}</span>`+
`<input data-k=${k} value="${H(v)}" style=flex:1;min-width:110px></div>`).join('')}
async function saveSettings(){const b={};document.querySelectorAll('#settings input')
.forEach(i=>b[i.dataset.k]=i.value);
if(!confirm('Save and restart Venom?'))return;
alert((await(await api('/api/settings',b)).json()).result)}
async function loadLogs(){$('logs').textContent='loading...';
$('logs').textContent=(await(await api('/api/logs')).json()).text}
async function loadMem(){$('mem').textContent='loading...';
$('mem').textContent=(await(await api('/api/memory')).json()).text}
async function loadConn(){$('conn').textContent='loading...';
$('conn').textContent=(await(await api('/api/connections')).json()).text}
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
function growTree(){
const g=document.getElementById('canopy');if(!g)return;
const NS='http://www.w3.org/2000/svg';let seed=20231026,made=0;
const rnd=()=>(seed=(seed*1103515245+12345)&0x7fffffff)/0x7fffffff;
const frag=document.createDocumentFragment();
const CAP=1900;                       // a phone still has to paint this
// angle 0 points straight up; +PI/2 is right, -PI/2 is left
function limb(x,y,ang,len,w,depth){
if(made++>CAP)return;
const bow=(rnd()-.5)*.38;
const mx=x+Math.sin(ang+bow)*len*.55,my=y-Math.cos(ang+bow)*len*.55;
const ex=x+Math.sin(ang)*len,ey=y-Math.cos(ang)*len;
const el=document.createElementNS(NS,'path');
el.setAttribute('d',`M${x.toFixed(1)} ${y.toFixed(1)} Q${mx.toFixed(1)} ${my.toFixed(1)} `+
`${ex.toFixed(1)} ${ey.toFixed(1)}`);
el.setAttribute('stroke-width',w.toFixed(2));
el.setAttribute('stroke-opacity',(.16+depth*.055).toFixed(2));
if(depth<=1&&rnd()>.93)el.setAttribute('stroke','#f0c674');
const L=Math.round(len*1.3);
el.style.setProperty('--len',L);el.style.strokeDasharray=L;
el.style.animationDelay=(1.5-depth*.2+rnd()*.25).toFixed(2)+'s';
frag.appendChild(el);
if(depth<=0){
if(rnd()>(Math.abs(ex-600)>300?.42:.8)){const c=document.createElementNS(NS,'circle');
c.setAttribute('class','tip');c.setAttribute('cx',ex.toFixed(1));
c.setAttribute('cy',ey.toFixed(1));c.setAttribute('r',(.8+rnd()*1.3).toFixed(1));
if(Math.abs(ex-600)>340)c.setAttribute('fill','#9b6bff');
c.style.animationDelay=(rnd()*4).toFixed(1)+'s';frag.appendChild(c)}
return}
const kids=rnd()>.5?3:2;
for(let i=0;i<kids;i++){
const a=ang+(i-(kids-1)/2)*(.44-depth*.02)+(rnd()-.5)*.22;
limb(ex,ey,a,len*(.7+rnd()*.14),Math.max(.32,w*.6),depth-1)}}
// every strand leaves the same knot at the centre, on an ellipse that keeps
// the spread horizontal — the shape only reads as the tree from the side
const N=96,CX=600,CY=300;
for(let i=0;i<N;i++){
// walk the ellipse in mirrored pairs, so any budget cut stays symmetric
const j=(i%2?N-1-(i>>1):(i>>1));
const tau=(j+.5)/N*Math.PI*2+rnd()*.03;
const dx=Math.cos(tau),dy=Math.sin(tau)*.34;
const ang=Math.atan2(dx,-dy);
const flat=Math.abs(dx);
const r0=4+rnd()*16;                 // start just off the knot, not on it
const sx=CX+Math.sin(ang)*r0,sy=CY-Math.cos(ang)*r0;
const dep=flat>.82?3:(flat>.5?2:1);
limb(sx,sy,ang,110+flat*140+rnd()*34,1.4+flat*2,dep)}
// a brighter armful either side, where the hands actually hold them
[[466,300,-Math.PI/2],[734,300,Math.PI/2]].forEach(([hx,hy,base])=>{
for(let i=0;i<5;i++){
const a=base+(i-2)*.26*(base<0?1:-1)+(rnd()-.5)*.08;
limb(hx,hy,a,130+rnd()*45,2.6,3)}});
g.appendChild(frag)}
growTree();
// fill the screen on a laptop, keep both hands in frame on a phone
function fitTree(){const s=document.getElementById('tree');if(!s)return;
s.setAttribute('preserveAspectRatio',
innerWidth/innerHeight>1.25?'xMidYMid slice':'xMidYMid meet')}
fitTree();addEventListener('resize',fitTree);
function faceClock(){const d=new Date();
$('clock').textContent=d.toLocaleTimeString();
}
$('case').textContent='VEN-'+(new Date().getFullYear()+'').slice(2)+
('0'+(new Date().getMonth()+1)).slice(-2)+('0'+new Date().getDate()).slice(-2);
setInterval(faceClock,1000);faceClock();
setInterval(tick,1500);setInterval(vtick,3000);tick();vtick();
setInterval(()=>{if(document.body.classList.contains('alert'))loadSos()},5000);loadSos();
</script>"""


class WebConsole:
    """Owns the HTTP thread; the voice loop attaches itself on each start."""

    def __init__(self, port: int = 8787, token: str = "",
                 bind: str = "127.0.0.1"):
        self.port = port
        self.token = token
        # Bind address. Default loopback: the console (a root shell) is reached
        # over an SSH tunnel or Tailscale, never raw on whatever Wi-Fi the Pi
        # roams onto. Set web.bind = "0.0.0.0" in venom.toml to expose it on the
        # LAN (only sensible on a network you fully trust).
        self.bind = bind or "127.0.0.1"
        self.orchestrator = None  # set by attach(); may be replaced on restart
        self.loop = None
        self._prev_cpu = None  # (idle, total) for %-usage deltas
        self._cwd = None       # terminal working dir, persisted across calls
        self._prev_cwd = None  # for `cd -`
        self._auth_fails: dict[str, tuple[int, float]] = {}  # ip -> (count, until)
        self._auth_lock = threading.Lock()

    def authorized(self, headers, ip: str = "") -> str:
        """Authorise a request: returns 'ok', 'bad', or 'locked'.

        The PIN is compared in constant time (no timing oracle), and an IP that
        keeps guessing wrong is locked out for a while so the 32-bit-ish PIN
        can't be ground through over the network."""
        if not self.token:
            return "ok"
        now = time.monotonic()
        with self._auth_lock:
            fails, until = self._auth_fails.get(ip, (0, 0.0))
            if until and now < until:
                return "locked"
        supplied = (headers.get("Authorization", "") or "").removeprefix("Bearer ")
        if hmac.compare_digest(supplied, self.token):
            with self._auth_lock:
                self._auth_fails.pop(ip, None)  # clean slate on success
            return "ok"
        # An absent PIN is just an un-authenticated poll (the page fetches
        # /api/state before the user types anything) — answer 401 so the UI
        # prompts, but don't let it burn the lockout budget. Only a supplied,
        # wrong PIN counts as a guess.
        if not supplied:
            return "bad"
        with self._auth_lock:
            fails += 1
            until = now + LOCKOUT_SECONDS if fails >= LOCKOUT_THRESHOLD else 0.0
            self._auth_fails[ip] = (fails, until)
        if until:
            log.warning("console: %s locked out after %d bad PINs", ip, fails)
            return "locked"
        return "bad"

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

    def connections_dump(self) -> str:
        try:
            from venom.config import load_config
            from venom.stores import ConnectionStore

            store = ConnectionStore(load_config().memory_path.parent
                                    / "connections.json")
            return self._connections_cmd(store, ["list"])
        except Exception as exc:
            return f"(connections unavailable: {exc})"

    # ── emergency SOS ────────────────────────────────────────────────────────
    # Contacts can be edited with the voice loop down (they're just a file);
    # firing or calling off an alert needs the live orchestrator, which owns
    # WhatsApp and the location provider.
    def _sos_store(self):
        from venom.config import load_config
        from venom.sos import SosStore

        return SosStore(load_config().memory_path.parent / "sos.json")

    def sos_snapshot(self) -> dict:
        try:
            orch = self.orchestrator
            if orch is not None and getattr(orch, "sos", None) is not None:
                return orch.sos.snapshot()
            config = self._sos_store().config()
            return {"active": False, "started_at": 0, "last_sent": 0,
                    "summary": "", "contacts": config["contacts"],
                    "repeat_minutes": config["repeat_minutes"],
                    "include_location": config["include_location"],
                    "offline": True}
        except Exception as exc:
            return {"active": False, "contacts": [], "error": str(exc)}

    def sos_action(self, data: dict) -> str:
        action = str(data.get("action", "")).strip().lower()
        orch = self.orchestrator
        live = getattr(orch, "sos", None) if orch is not None else None
        try:
            if action in ("trigger", "stop", "test", "status"):
                if live is None:
                    # Either the voice service is down or WhatsApp is off; both
                    # mean the same thing here — nothing can leave the device.
                    return ("SOS isn't live right now (voice service down, or "
                            "WhatsApp disabled), so nothing can be sent — "
                            "contacts can still be edited.")
                if action == "trigger":
                    return live.start(str(data.get("note", "")).strip())
                if action == "stop":
                    return live.stop(str(data.get("note", "")).strip())
                if action == "test":
                    return live.test()
                return live.status()

            from venom.sos import describe_contact

            store = live.store if live is not None else self._sos_store()
            name = str(data.get("name", "")).strip()
            if action == "add":
                if not name:
                    return "a name is required"
                entry = store.add(name, to=str(data.get("to", "")).strip(),
                                  label=str(data.get("label", "")).strip(),
                                  message=str(data.get("message", "")))
                return "saved: " + describe_contact(entry)
            if action == "remove":
                return (f"removed {name}" if store.remove(name)
                        else f"no SOS contact called '{name}'")
            if action in ("enable", "disable"):
                on = action == "enable"
                if not store.set_enabled(name, on):
                    return f"no SOS contact called '{name}'"
                return f"{name} {'will' if on else 'will not'} be alerted"
            if action == "settings":
                repeat = data.get("repeat_minutes")
                store.set_settings(
                    repeat_minutes=None if repeat in (None, "") else float(repeat),
                    include_location=data.get("include_location"))
                return "settings saved"
            return f"unknown action '{action}'"
        except Exception as exc:
            log.warning("sos action failed: %s", exc)
            return f"error: {exc}"

    # ── console built-ins: edit Connections & Memory from the terminal ────────
    # Intercepted before the shell so neither needs hand-edited JSON. Return a
    # {out, cwd} dict when the command is ours, else None to fall through.
    def _data_command(self, raw: str) -> dict | None:
        try:
            parts = shlex.split(raw)
        except ValueError:
            return None
        if not parts or parts[0].lower() not in (
                "connections", "conn", "memory", "mem"):
            return None
        cwd = self._cwd or "/"
        try:
            from venom.config import load_config

            cfg = load_config()
            if parts[0].lower() in ("connections", "conn"):
                from venom.stores import ConnectionStore

                store = ConnectionStore(cfg.memory_path.parent
                                        / "connections.json")
                return {"out": self._connections_cmd(store, parts[1:]),
                        "cwd": cwd}
            from flint_core.memory import MemoryStore

            return {"out": self._memory_cmd(MemoryStore(cfg.memory_path),
                                            parts[1:]), "cwd": cwd}
        except Exception as exc:
            return {"out": f"[error: {exc}]", "cwd": cwd}

    _CONN_KEYS = {"phone", "nick", "nickname", "insta", "instagram",
                  "interest", "note"}

    @classmethod
    def _connections_cmd(cls, store, args: list[str]) -> str:
        if not args or args[0].lower() in ("list", "ls"):
            data = store._load({})
            if not data:
                return "(no connections saved)"
            out: list[str] = []
            for rec in data.values():
                head = rec.get("name", "?")
                if rec.get("aliases"):
                    head += " (" + ", ".join(rec["aliases"]) + ")"
                out.append(head)
                if rec.get("phones"):
                    out.append("    ph: " + ", ".join(rec["phones"]))
                if rec.get("instagram"):
                    out.append("    ig: " + rec["instagram"])
                if rec.get("interests"):
                    out.append("    likes: " + ", ".join(rec["interests"]))
                if rec.get("notes"):
                    out.append("    note: " + "; ".join(rec["notes"]))
            return "\n".join(out)
        sub, rest = args[0].lower(), args[1:]
        if sub == "show":
            return store.describe(" ".join(rest)) if rest \
                else "usage: connections show <name>"
        if sub in ("del", "delete", "rm", "forget"):
            name = " ".join(rest)
            if not name:
                return "usage: connections del <name>"
            return f"removed {name}" if store.forget(name) \
                else f"no match for '{name}'"
        if sub in ("add", "set", "save", "edit"):
            kv: dict[str, str] = {}
            name_parts: list[str] = []
            for a in rest:
                key = a.split("=", 1)[0].lower() if "=" in a else ""
                if key in cls._CONN_KEYS:
                    kv[key] = a.split("=", 1)[1]
                else:
                    name_parts.append(a)
            name = " ".join(name_parts)
            if not name:
                return ("usage: connections add <name> [phone=..] [nick=..] "
                        "[insta=..] [interest=..] [note=..]")
            store.save(name, phone=kv.get("phone", ""),
                       nickname=kv.get("nick", kv.get("nickname", "")),
                       instagram=kv.get("insta", kv.get("instagram", "")),
                       interest=kv.get("interest", ""), note=kv.get("note", ""))
            return "saved:\n" + store.describe(name)
        return ("connections commands:\n"
                "  connections [list]\n"
                "  connections show <name>\n"
                "  connections add <name> [phone=..] [nick=..] [insta=..] "
                "[interest=..] [note=..]\n"
                "  connections del <name>")

    @staticmethod
    def _memory_cmd(mem, args: list[str]) -> str:
        if not args or args[0].lower() in ("list", "ls", "show"):
            out: list[str] = []
            for cat, entries in mem.load().items():
                if not entries:
                    continue
                out.append(f"[{cat}]")
                for key, entry in entries.items():
                    val = entry.get("value") if isinstance(entry, dict) else entry
                    out.append(f"  {key} = {val}")
            return "\n".join(out) if out else "(nothing remembered yet)"
        sub = args[0].lower()
        if sub == "set":
            if len(args) < 4:
                return "usage: memory set <category> <key> <value>"
            return mem.remember(args[1], args[2], " ".join(args[3:]))
        if sub in ("del", "delete", "rm", "forget"):
            if len(args) < 3:
                return "usage: memory del <category> <key>"
            return mem.forget(args[1], args[2])
        if sub in ("cats", "categories"):
            from flint_core.memory.store import CATEGORIES

            return "categories: " + ", ".join(CATEGORIES)
        return ("memory commands:\n"
                "  memory [list]\n"
                "  memory set <category> <key> <value>\n"
                "  memory del <category> <key>\n"
                "  memory categories")

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
                "resume": lambda: orch.music.set_paused(False),
                "next": orch.music.skip}
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
        # Console built-ins (connections/memory editing) win over the shell so
        # they work identically on the root daemon and the sandboxed fallback.
        builtin = self._data_command((cmd or "").strip())
        if builtin is not None:
            return builtin
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

    # ── Wi-Fi networks (via NetworkManager, as root) ─────────────────────────
    def _nm(self, argv: list[str]) -> tuple[int, str]:
        """Run one nmcli command as root and return (rc, output). Goes through
        the root shell daemon (same channel as the terminal); on a dev box
        without the daemon it falls back to a direct call, which usually can't
        modify connections — that's fine, it just reports the failure."""
        cmd = " ".join(shlex.quote(a) for a in argv)
        root = self._root_shell(cmd + '; printf "\\n__rc:%s" "$?"')
        if root is not None:
            out = root.get("out", "")
            rc = 0
            m = re.search(r"__rc:(\d+)\s*$", out)
            if m:
                rc = int(m.group(1))
                out = out[:m.start()]
            return rc, out.rstrip()
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, str(exc)

    def wifi_overview(self) -> dict:
        from venom import netman

        try:
            return netman.overview(self._nm)
        except Exception as exc:  # never let a parse hiccup 500 the panel
            log.warning("wifi overview failed: %s", exc)
            return {"nm": False, "current": {}, "saved": [], "available": []}

    def wifi_action(self, data: dict) -> str:
        from venom import netman

        action = str(data.get("action", "")).strip()
        name = str(data.get("name", data.get("ssid", ""))).strip()
        try:
            if action == "add":
                return netman.add_or_update(
                    self._nm, name, str(data.get("password", "")),
                    _as_int(data.get("priority")))
            if action == "remove":
                return netman.remove(self._nm, name)
            if action == "connect":
                return netman.connect(self._nm, name)
            if action == "base":
                return netman.set_base(self._nm, name)
            if action == "priority":
                return netman.set_priority(self._nm, name,
                                           _as_int(data.get("priority")) or 0)
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the thread
            log.warning("wifi action %s failed: %s", action, exc)
            return f"That didn't work: {exc}"
        return "Unknown Wi-Fi action."

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
                """True when allowed; else serve 401 (bad PIN) or 429 (locked)."""
                verdict = console.authorized(
                    self.headers, self.client_address[0])
                if verdict == "ok":
                    return True
                if verdict == "locked":
                    self._send(b'{"error":"too many attempts - locked"}',
                               code=429)
                else:
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
                elif self.path == "/api/connections":
                    self._send(json.dumps(
                        {"text": console.connections_dump()}).encode())
                elif self.path == "/api/wifi":
                    self._send(json.dumps(console.wifi_overview()).encode())
                elif self.path == "/api/sos":
                    self._send(json.dumps(console.sos_snapshot()).encode())
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
                elif self.path == "/api/wifi":
                    self._send(json.dumps(
                        {"result": console.wifi_action(data)}).encode())
                elif self.path == "/api/sos":
                    self._send(json.dumps(
                        {"result": console.sos_action(data)}).encode())
                else:
                    self._send(b"{}")

        server = ThreadingHTTPServer((self.bind, self.port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True,
                         name="venom-web").start()
        log.info("web console on http://%s:%d", self.bind, self.port)
