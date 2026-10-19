#!/usr/bin/env python3
"""EffectCraft MCP CI Gauntlet - Tests 1-7 over stdio JSON-RPC 2.0.

Spawns the headless MCP server (effectcraft-cli mcp --autosave --demo) and runs
the mandated scenario battery. Every pass/fail is decided by observed replies
only. Writes results.json + gauntlet.log + server_stderr.log for the workflow
to upload as artifacts.
"""
import json
import os
import subprocess
import sys
import time
import traceback

CLI = os.environ.get("GAUNTLET_CLI", "./target/release/effectcraft-cli")
FONT_DIRS = os.environ.get("EFFECTCRAFT_FONT_DIRS", "")
WORKDIR = os.environ.get("GAUNTLET_WORKDIR", os.getcwd())

RESULTS = []
LOG_LINES = []


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    LOG_LINES.append(line)
    print(line, flush=True)


class McpServer:
    """Minimal MCP stdio client: one JSON message per line."""

    def __init__(self, cmd, stderr_path):
        self.stderr_f = open(stderr_path, "wb")
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr_f,
            cwd=WORKDIR,
            env={**os.environ},
            bufsize=0,
        )
        self._id = 0

    def send(self, obj):
        self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def recv(self, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(f"server closed stdout (exit={self.proc.poll()}); see server_stderr.log")
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                log(f"NON-JSON line from server: {line[:200]}")
                continue
        raise TimeoutError("no reply within timeout")

    def request(self, method, params=None, timeout=30.0):
        self._id += 1
        rid = self._id
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self.recv(timeout=max(0.1, deadline - time.time()))
            if msg.get("id") == rid:
                return msg
            log(f"(skipped notification while waiting for id={rid})")
        raise TimeoutError(f"no matching reply for id={rid}")

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=20)
        except Exception:
            self.proc.kill()
        self.stderr_f.flush()
        return self.proc.returncode


def record(name, status, seconds, evidence):
    RESULTS.append({"test": name, "status": status, "seconds": round(seconds, 2), "evidence": evidence})
    log(f"RESULT {name}: {status} ({seconds:.2f}s) - {evidence}")


def call_tool(srv, name, arguments, timeout=30.0):
    """MCP tools/call. Returns (ok, parsed_or_text, raw_result)."""
    msg = srv.request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
    if "error" in msg:
        return False, msg["error"], msg
    res = msg.get("result", {})
    is_error = res.get("isError", False)
    texts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
    blob = "\n".join(texts)
    try:
        parsed = json.loads(blob)
    except Exception:
        parsed = blob
    return (not is_error), parsed, res


def engine(srv, command, params=None, timeout=60.0):
    """execute_command with one describe_command-guided retry on unknown-key rejection."""
    ok, res, raw = call_tool(srv, "execute_command", {"command": command, "params": params or {}}, timeout)
    if ok:
        return True, res, raw
    blob = res if isinstance(res, str) else json.dumps(res)
    low = blob.lower()
    if ("unknown" in low and "key" in low) or "accepted" in low or "unknown" in low:
        log(f"engine {command}: params rejected ({blob[:250]}) - asking describe_command for accepted keys")
        dok, dres, _ = call_tool(srv, "describe_command", {"command": command}, timeout=15.0)
        log(f"describe_command {command}: "
            f"{json.dumps(dres)[:700] if not isinstance(dres, str) else dres[:700]}")
        if isinstance(dres, dict):
            schema = dres.get("schema") or {}
            props = schema.get("properties", {}) if isinstance(schema, dict) else {}
            keep = {k: v for k, v in (params or {}).items() if k in props}
            if keep:
                log(f"engine {command}: retrying with documented keys {sorted(keep)}")
                return call_tool(srv, "execute_command", {"command": command, "params": keep}, timeout)
    return False, res, raw


def get_comp_by_name(srv, name):
    ok, gcomp, _ = call_tool(srv, "get_comp", {"comp": name}, timeout=20.0)
    return gcomp if isinstance(gcomp, dict) else {}


def layer_id_of(comp, name):
    for l in comp.get("layers", []):
        if str(l.get("name", "")) == name:
            return l.get("id")
    return None


# ---------------------------------------------------------------- tests

def t1_startup_state(srv):
    t0 = time.time()
    init = srv.request("initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "gauntlet", "version": "1.0"},
    }, timeout=10.0)
    srv.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    if "error" in init:
        record("T1_startup_state", "FAIL", time.time() - t0, f"initialize error: {init['error']}")
        return
    info = init.get("result", {}).get("serverInfo", {})
    ok, state, _ = call_tool(srv, "get_state", {}, timeout=10.0)
    dt = time.time() - t0
    snippet = json.dumps(state)[:350] if not isinstance(state, str) else state[:350]
    if ok and dt <= 5.0 and isinstance(state, dict):
        record("T1_startup_state", "PASS", dt,
               f"server={info.get('name')}/{info.get('version')} proto={init['result'].get('protocolVersion')}; state: {snippet}")
    else:
        record("T1_startup_state", "FAIL", dt, f"ok={ok} dt={dt:.2f}s state: {snippet}")


def t2_composition_layers(srv):
    t0 = time.time()
    ok, comp, _ = engine(srv, "comp.new", {"name": "Gauntlet", "width": 1920, "height": 1080,
                                           "frameRate": 30, "duration": 5})
    if not ok:
        record("T2_composition_layers", "FAIL", time.time() - t0, f"comp.new: {str(comp)[:350]}")
        return {}
    ok, solid, _ = engine(srv, "layer.newSolid", {"name": "BG", "color": "#101820"})
    if not ok:
        record("T2_composition_layers", "FAIL", time.time() - t0, f"layer.newSolid: {str(solid)[:350]}")
        return {}
    ok, text, _ = engine(srv, "layer.newText", {"text": "GAUNTLET", "size": 160, "name": "Title",
                                                "font": "Inter"})
    if not ok:
        record("T2_composition_layers", "FAIL", time.time() - t0, f"layer.newText: {str(text)[:350]}")
        return {}
    gcomp = get_comp_by_name(srv, "Gauntlet")
    dt = time.time() - t0
    layers = gcomp.get("layers", [])
    names = [str(l.get("name", "")) for l in layers]
    ids = [l.get("id") for l in layers]
    w = gcomp.get("width"); h = gcomp.get("height"); fr = gcomp.get("frameRate"); dur = gcomp.get("duration")
    if ("BG" in names) and ("Title" in names) and all(i is not None for i in ids):
        record("T2_composition_layers", "PASS", dt,
               f"comp={w}x{h}@{fr}/{dur}s layers={names} ids={ids}; comp.new reply: {json.dumps(comp)[:120]}")
    else:
        record("T2_composition_layers", "FAIL", dt, f"comp={w}x{h}@{fr}/{dur}s layers={names} ids={ids}")
    return {"gcomp": gcomp, "ids": ids}


def t3_keyframes(srv, ctx):
    t0 = time.time()
    gcomp = ctx.get("gcomp", {})
    text_id = layer_id_of(gcomp, "Title")
    if text_id is None:
        record("T3_keyframes", "FAIL", time.time() - t0, "no Title layer id from T2 context")
        return
    ok, r1, _ = call_tool(srv, "add_keyframe", {
        "layer": text_id, "path": "transform/position",
        "keys": [{"time": 0, "value": [-300, 540]}, {"time": 2.0, "value": [960, 540]}],
        "interpolation": "easyEase"}, timeout=20.0)
    log(f"add_keyframe position: {json.dumps(r1)[:250] if not isinstance(r1, str) else r1[:250]}")
    ok2, r2, _ = call_tool(srv, "add_keyframe", {
        "layer": text_id, "path": "transform/opacity",
        "keys": [{"time": 0, "value": 0}, {"time": 2.0, "value": 100}],
        "interpolation": "easyEase"}, timeout=20.0)
    ok3, layer, _ = call_tool(srv, "get_layer", {"layer": text_id, "flat": True}, timeout=20.0)
    ok4, gpos, _ = call_tool(srv, "get_property", {"layer": text_id, "path": "transform/position"}, timeout=20.0)
    dt = time.time() - t0
    blob = json.dumps(layer) if not isinstance(layer, str) else layer

    def key_count(props, path):
        n = 0
        items = props if isinstance(props, list) else (props.get("properties", props.get("nodes", [])) if isinstance(props, dict) else [])
        for item in items:
            if isinstance(item, dict) and item.get("path") == path:
                n = item.get("keyframes", item.get("keyCount", item.get("keys", 0)))
                n = len(n) if isinstance(n, list) else n
        return n

    kp = key_count(layer, "transform/position")
    ko = key_count(layer, "transform/opacity")
    gpos_keys = ""
    if isinstance(gpos, dict):
        gpos_keys = json.dumps(gpos.get("keyframes", gpos.get("keys", "")))[:300]
    log(f"T3 get_property transform/position: {json.dumps(gpos)[:400] if not isinstance(gpos, str) else gpos[:400]}")
    if ok and ok2 and ok3 and kp >= 2 and ko >= 2:
        record("T3_keyframes", "PASS", dt,
               f"position keys={kp} opacity keys={ko} easyEase accepted; get_property keys={gpos_keys or 'n/a'}")
    else:
        record("T3_keyframes", "FAIL", dt,
               f"add_key ok={ok},{ok2}; get_layer ok={ok3}; pos={kp} op={ko}; blob={blob[:400]}")


def t4_effects(srv, ctx):
    t0 = time.time()
    gcomp = ctx.get("gcomp", {})
    text_id = layer_id_of(gcomp, "Title")
    ok, fx, _ = call_tool(srv, "list_effects", {"filter": "Gaussian"}, timeout=20.0)
    names = []
    if isinstance(fx, dict):
        for e in fx.get("effects", fx.get("items", [])):
            names.append(e.get("name", "") if isinstance(e, dict) else str(e))
    elif isinstance(fx, list):
        names = [e.get("name", "") if isinstance(e, dict) else str(e) for e in fx]
    blur = next((n for n in names if "gaussian" in n.lower()), None)
    if not ok or blur is None:
        record("T4_effects", "FAIL", time.time() - t0, f"list_effects filter=Gaussian -> {str(fx)[:350]}")
        return
    ok2, added, _ = call_tool(srv, "add_effect", {"layer": text_id, "effect": blur}, timeout=20.0)
    ok3, sp, _ = call_tool(srv, "set_property",
                           {"layer": text_id, "path": "effects/#1/blurriness", "value": 12}, timeout=20.0)
    ok4, layer, _ = call_tool(srv, "get_layer", {"layer": text_id, "flat": True}, timeout=20.0)
    dt = time.time() - t0
    lblob = json.dumps(layer) if not isinstance(layer, str) else layer
    attached = "blurriness" in lblob
    if ok2 and ok3 and attached:
        record("T4_effects", "PASS", dt,
               f"applied '{blur}' (reply {json.dumps(added)[:140]}), blurriness=12, confirmed in get_layer")
    else:
        record("T4_effects", "FAIL", dt,
               f"add_effect ok={ok2} ({str(added)[:150]}), set_property ok={ok3}, attached={attached}: {lblob[:300]}")


def t5_render(srv):
    t0 = time.time()
    out = os.path.join(WORKDIR, "gauntlet_render.mp4")
    fmt = "h264"
    ok, add, _ = engine(srv, "renderQueue.add", {"comp": "Gauntlet", "format": fmt, "output": out}, timeout=30.0)
    if not ok:
        out = os.path.join(WORKDIR, "gauntlet_render.mov")
        fmt = "prores"
        ok, add, _ = engine(srv, "renderQueue.add", {"comp": "Gauntlet", "format": fmt, "output": out}, timeout=30.0)
    if not ok:
        record("T5_render_queue", "FAIL", time.time() - t0, f"renderQueue.add failed (h264+prores): {str(add)[:400]}")
        return None
    log(f"renderQueue.add ok: {json.dumps(add)[:300]}")
    ok, rend, _ = engine(srv, "renderQueue.render", {"wait": True}, timeout=900.0)
    dt = time.time() - t0
    exists = os.path.exists(out)
    size = os.path.getsize(out) if exists else 0
    magic = ""
    if exists:
        try:
            magic = subprocess.run(["file", out], capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception as ex:
            magic = f"(file probe failed: {ex})"
    log(f"render reply: {json.dumps(rend)[:400] if not isinstance(rend, str) else rend[:400]}")
    log(f"file probe: {magic}")
    if ok and exists and size > 10000:
        record("T5_render_queue", "PASS", dt,
               f"{fmt} written: {out} {size} bytes ({size/1e6:.2f} MB), wall {dt:.1f}s; {magic}")
        return out
    blob = json.dumps(rend) if not isinstance(rend, str) else rend
    record("T5_render_queue", "FAIL", dt,
           f"ok={ok} exists={exists} size={size}; reply={blob[:400]}; {magic}")
    return None


def font_families(srv, query):
    ok, fonts, _ = call_tool(srv, "list_fonts", {"query": query} if query else {}, timeout=25.0)
    fams = []
    if isinstance(fonts, dict):
        for f in fonts.get("families", fonts.get("fonts", [])):
            if isinstance(f, dict):
                fams.append({"family": f.get("family", ""), "origin": f.get("origin", "?"),
                             "styles": f.get("styles", [])})
            else:
                fams.append({"family": str(f), "origin": "?"})
    elif isinstance(fonts, list):
        fams = [{"family": f.get("family", "?"), "origin": f.get("origin", "?")} if isinstance(f, dict)
                else {"family": str(f), "origin": "?"} for f in fonts]
    return fams


def t6_fonts(srv, ctx):
    """The real CI trap: Inter alone proves nothing (it is origin='bundled', so it would
    be visible even if EFFECTCRAFT_FONT_DIRS were ignored). Grenze is not bundled,
    not shipped on GitHub runner images (workflow fc-list pre-check proves absence),
    and only becomes visible when the env var is read. We also create a text layer
    with font='Grenze' and render it, so the artifact shows real glyph rasterization."""
    t0 = time.time()
    log(f"font env EFFECTCRAFT_FONT_DIRS='{FONT_DIRS}'; dir listing: "
        f"{sorted(os.listdir(FONT_DIRS))[:12] if os.path.isdir(FONT_DIRS) else 'MISSING'}")
    inter = font_families(srv, "Inter")
    grenze = font_families(srv, "Grenze")
    has_inter = any("inter" in str(f["family"]).lower() for f in inter)
    grenze_hit = next((f for f in grenze if "grenze" in str(f["family"]).lower()), None)
    log(f"list_fonts Inter -> {inter}")
    log(f"list_fonts Grenze -> {grenze}")
    gcomp = ctx.get("gcomp", {})
    layer_note = "n/a (no Gauntlet comp from T2)"
    grenze_layer_ok = None
    if gcomp:
        okg, gl, _ = engine(srv, "layer.newText",
                            {"text": "FONT CONTROL: Grenze", "size": 72, "name": "FontControl",
                             "font": "Grenze", "fill": "#ffcc00"}, timeout=20.0)
        grenze_layer_ok = okg
        layer_note = f"layer.newText font='Grenze' ok={okg}: {str(gl)[:200]}"
        log(f"Grenze text layer: {layer_note}")
    ok2, frame, _ = call_tool(srv, "render_frame",
                              {"comp": "Gauntlet", "time": 1.0, "max_side": 640,
                               "path": os.path.join(WORKDIR, "font_probe.png")}, timeout=120.0)
    dt = time.time() - t0
    png = os.path.join(WORKDIR, "font_probe.png")
    png_ok = os.path.exists(png) and os.path.getsize(png) > 3000
    if has_inter and grenze_hit and grenze_layer_ok and ok2 and png_ok:
        record("T6_fonts", "PASS", dt,
               f"Inter listed (origin={inter[0]['origin'] if inter else '?'}; bundled => not probative); "
               f"Grenze listed via EFFECTCRAFT_FONT_DIRS (origin={grenze_hit['origin']}); "
               f"Grenze text layer created; font_probe.png {os.path.getsize(png)} bytes")
    else:
        record("T6_fonts", "FAIL", dt,
               f"inter_in_list={has_inter} ({[f['origin'] for f in inter]}); grenze_hit={grenze_hit}; "
               f"grenze_layer_ok={grenze_layer_ok} ({layer_note}); render_ok={ok2} png_ok={png_ok}")


def t7_error_recovery(srv):
    t0 = time.time()
    gcomp = get_comp_by_name(srv, "Gauntlet")
    layer_id = 2
    if gcomp.get("layers"):
        layer_id = gcomp["layers"][0].get("id", 2)
    ok1, err1, raw1 = call_tool(srv, "set_property",
                                {"layer": layer_id, "path": "transform/fake_property", "value": 1},
                                timeout=20.0)
    alive, state2 = True, {}
    try:
        ok2, state2, _ = call_tool(srv, "get_state", {}, timeout=10.0)
        alive = ok2
    except Exception as ex:
        alive, state2 = False, str(ex)
    dt = time.time() - t0
    blob = err1 if isinstance(err1, str) else json.dumps(err1)
    mentions = ("fake_property" in blob) or ("accepted" in blob.lower()) or ("unknown" in blob.lower())
    if (not ok1) and alive and mentions:
        record("T7_error_recovery", "PASS", dt,
               f"structured rejection, server alive after; error: {blob[:280]}")
    elif (not ok1) and alive:
        record("T7_error_recovery", "PASS", dt,
               f"rejected without crash (message not self-describing): {blob[:280]}")
    else:
        record("T7_error_recovery", "FAIL", dt, f"ok1={ok1} alive={alive} err={blob[:280]}")


# ---------------------------------------------------------------- main

def main():
    log(f"gauntlet starting; CLI={CLI}; EFFECTCRAFT_FONT_DIRS='{FONT_DIRS}'; cwd={WORKDIR}")
    if not os.path.exists(CLI):
        RESULTS.append({"test": "server_spawn", "status": "FAIL", "seconds": 0, "evidence": f"no binary at {CLI}"})
        finish(1)
    cmd = [CLI, "mcp", "--autosave", "--demo"]
    log(f"spawning: {' '.join(cmd)}")
    srv = McpServer(cmd, os.path.join(WORKDIR, "server_stderr.log"))
    try:
        t1_startup_state(srv)
        ctx = t2_composition_layers(srv) or {}
        t3_keyframes(srv, ctx)
        t4_effects(srv, ctx)
        t5_render(srv)
        t6_fonts(srv, ctx)
        t7_error_recovery(srv)
        try:
            ok, st, _ = call_tool(srv, "get_state", {}, timeout=10.0)
            log(f"final liveness get_state ok={ok}")
        except Exception as ex:
            log(f"final liveness get_state raised: {ex}")
        rc = srv.close()
        log(f"server exit code on stdin EOF: {rc}")
    except Exception as ex:
        log("UNCAUGHT: " + traceback.format_exc())
        RESULTS.append({"test": "harness", "status": "CRASH", "seconds": 0, "evidence": str(ex)[:400]})
        try:
            srv.close()
        except Exception:
            pass
    finish(0)


def finish(code):
    with open(os.path.join(WORKDIR, "results.json"), "w") as f:
        json.dump(RESULTS, f, indent=2)
    with open(os.path.join(WORKDIR, "gauntlet.log"), "w") as f:
        f.write("\n".join(LOG_LINES) + "\n")
    n_pass = sum(1 for r in RESULTS if r["status"] == "PASS")
    log(f"SUMMARY: {n_pass}/{len(RESULTS)} PASS")
    for r in RESULTS:
        log(f"  {r['status']:5} {r['test']} ({r['seconds']}s)")
    sys.exit(code)


if __name__ == "__main__":
    main()
