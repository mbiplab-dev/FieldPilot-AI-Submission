"""Web GUI: live annotated feed + analysis dashboard.

Runs the safety pipeline inside the server's event loop, writing each annotated frame and a stats
snapshot into a shared LiveState. The browser shows the feed as an MJPEG stream and polls `/stats`
for the analysis panel and hazard event feed.

    uv run python -m fieldpilot.display.server         # or: python -m fieldpilot.run --gui
    open http://localhost:8000
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from fieldpilot.core.config import Config, load_config
from fieldpilot.core.pipeline import Pipeline
from fieldpilot.core.video_source import VideoSource
from fieldpilot.display.state import LiveState
from fieldpilot.logging_.logger import get_logger, setup_logging

log = get_logger("fieldpilot.gui")


def _build_source(cfg: Config, kind: str | None, file_path: str | None) -> VideoSource:
    v = cfg.section("video")
    return VideoSource(
        kind=kind or v.get("source", "webcam"),
        webcam_index=int(v.get("webcam_index", 0)),
        file_path=file_path or v.get("file_path"),
        target_fps=int(v.get("target_fps", 30)),
        queue_maxsize=int(v.get("queue_maxsize", 4)),
        pace=True,
    )


def create_app(cfg: Config, source_kind: str | None = None, file_path: str | None = None) -> FastAPI:
    state = LiveState()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        source = _build_source(cfg, source_kind, file_path)
        pipeline = Pipeline(cfg, sink=state)
        task = asyncio.create_task(pipeline.run(source))
        log.info("GUI pipeline started — open http://localhost:8000")
        try:
            yield
        finally:
            state.running = False
            source.stop()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    app = FastAPI(title="FieldPilot AI", lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _PAGE

    @app.get("/stats")
    async def stats() -> JSONResponse:
        return JSONResponse(state.snapshot())

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def gen():
            boundary = b"--frame\r\n"
            while state.running:
                jpeg = state.get_jpeg()
                if jpeg:
                    yield boundary + b"Content-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
                await asyncio.sleep(1 / 25)

        return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")

    return app


def run_gui(config_path: str = "config.yaml", source_kind: str | None = None,
            file_path: str | None = None, host: str = "0.0.0.0", port: int = 8000) -> int:
    import uvicorn

    cfg = load_config(config_path)
    setup_logging(cfg.get("logging.level", "INFO"))
    app = create_app(cfg, source_kind, file_path)
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FieldPilot AI — Live</title>
<style>
  :root{--bg:#0d1017;--panel:#161b26;--line:#232b3a;--txt:#e6e9ef;--dim:#8b94a7;
        --hi:#ff5252;--med:#ffab40;--low:#ffd54f;--ok:#4ade80;--accent:#5b9dff}
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.4 ui-sans-serif,system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--txt)}
  header{display:flex;align-items:center;gap:12px;padding:12px 18px;border-bottom:1px solid var(--line);background:var(--panel)}
  header h1{font-size:16px;margin:0;letter-spacing:.3px;font-weight:600}
  header .tag{font-size:11px;color:var(--dim);border:1px solid var(--line);padding:2px 8px;border-radius:99px}
  .dot{width:9px;height:9px;border-radius:50%;background:var(--ok);box-shadow:0 0 8px var(--ok)}
  .wrap{display:grid;grid-template-columns:1fr 340px;gap:16px;padding:16px;max-width:1400px;margin:0 auto}
  @media(max-width:900px){.wrap{grid-template-columns:1fr}}
  .feed{background:#000;border:1px solid var(--line);border-radius:12px;overflow:hidden;position:relative}
  .feed img{display:block;width:100%;height:auto}
  .banner{position:absolute;top:0;left:0;right:0;padding:8px 12px;background:rgba(255,82,82,.92);
          color:#fff;font-weight:600;font-size:13px;display:none}
  .side{display:flex;flex-direction:column;gap:14px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
  .card h2{margin:0 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--dim)}
  .tiles{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .tile{background:#0f131c;border:1px solid var(--line);border-radius:10px;padding:10px 12px}
  .tile .v{font-size:22px;font-weight:650;font-variant-numeric:tabular-nums}
  .tile .k{font-size:11px;color:var(--dim);margin-top:2px}
  .feedlist{display:flex;flex-direction:column;gap:8px;max-height:340px;overflow:auto}
  .ev{border-left:3px solid var(--dim);padding:7px 10px;background:#0f131c;border-radius:6px}
  .ev.high{border-color:var(--hi)} .ev.medium{border-color:var(--med)} .ev.low{border-color:var(--low)}
  .ev .t{font-size:11px;color:var(--dim);display:flex;justify-content:space-between}
  .ev .m{margin-top:2px}
  .empty{color:var(--dim);font-size:13px;padding:6px 0}
</style></head>
<body>
<header>
  <span class="dot" id="dot"></span>
  <h1>FieldPilot AI</h1><span class="tag">Live Safety Monitor</span>
  <span class="tag" id="persp">—</span>
  <span style="flex:1"></span>
  <span class="tag" id="uptime">up 0s</span>
</header>
<div class="wrap">
  <div class="feed">
    <div class="banner" id="banner"></div>
    <img src="/stream" alt="live feed"/>
  </div>
  <div class="side">
    <div class="card">
      <h2>Live analysis</h2>
      <div class="tiles">
        <div class="tile"><div class="v" id="fps">–</div><div class="k">FPS</div></div>
        <div class="tile"><div class="v" id="infer">–</div><div class="k">inference (ms)</div></div>
        <div class="tile"><div class="v" id="persons">–</div><div class="k">persons in view</div></div>
        <div class="tile"><div class="v" id="tracks">–</div><div class="k">unique tracks</div></div>
        <div class="tile"><div class="v" id="hazards">–</div><div class="k">hazards</div></div>
        <div class="tile"><div class="v" id="alerts">–</div><div class="k">alerts fired</div></div>
      </div>
    </div>
    <div class="card">
      <h2>Hazard event feed</h2>
      <div class="feedlist" id="events"><div class="empty">No events yet.</div></div>
    </div>
  </div>
</div>
<script>
const $=id=>document.getElementById(id);
function fmt(ts){try{return new Date(ts*1000).toLocaleTimeString()}catch(e){return ''}}
async function tick(){
  try{
    const r=await fetch('/stats'); const d=await r.json(); const s=d.stats||{};
    $('fps').textContent=s.fps??'–'; $('infer').textContent=s.infer_ms??'–';
    $('persons').textContent=s.persons??'–'; $('tracks').textContent=s.unique_tracks??'–';
    $('hazards').textContent=s.hazards??'–'; $('alerts').textContent=s.alerts??'–';
    $('persp').textContent=s.perspective||'—'; $('uptime').textContent='up '+(d.uptime_s??0)+'s';
    $('dot').style.background=d.running?'var(--ok)':'var(--dim)';
    const act=s.active_hazards||[]; const b=$('banner');
    if(act.length){b.style.display='block';b.textContent='⚠ HAZARD ACTIVE — '+act.map(a=>a.type+' (id'+a.track_id+')').join(', ');}
    else b.style.display='none';
    const ev=d.events||[]; const box=$('events');
    if(!ev.length){box.innerHTML='<div class="empty">No events yet.</div>';}
    else box.innerHTML=ev.map(e=>`<div class="ev ${e.severity}"><div class="t"><span>${e.type}${e.track_id!=null?' · id'+e.track_id:''}</span><span>${fmt(e.ts_wall)}${e.latency_ms!=null?' · '+e.latency_ms+'ms':''}</span></div><div class="m">${e.message||''}</div></div>`).join('');
  }catch(e){$('dot').style.background='var(--hi)';}
}
setInterval(tick,500); tick();
</script>
</body></html>"""


if __name__ == "__main__":
    raise SystemExit(run_gui())
