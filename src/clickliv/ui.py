"""Minimal concurrency dashboard. One line chart, one platform filter, nothing more."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from .answers import marts
from .ch import ClickHouse

GRAIN_MINUTES = 60

PLATFORMS_SQL = "SELECT DISTINCT platform FROM minute_occupancy ORDER BY platform"

CONCURRENCY_SQL = """
SELECT bucket_minute, peak_concurrency, average_concurrency, minutes_in_bucket
FROM {marts}.v_concurrency(
    grain_minutes = {grain:UInt32}, country = '', platform = {platform:String},
    video_type = '', content_id = 0, minute_from = 0, minute_to = 4294967295)
"""

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>clickliv concurrency</title>
<style>
:root {
  color-scheme: light;
  --surface: #fcfcfb;
  --page: #f9f9f7;
  --ink: #0b0b0b;
  --ink-secondary: #52514e;
  --ink-muted: #898781;
  --grid: #e1e0d9;
  --axis: #c3c2b7;
  --peak: #2a78d6;
  --average: #eb6834;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --surface: #1a1a19;
    --page: #0d0d0d;
    --ink: #ffffff;
    --ink-secondary: #c3c2b7;
    --ink-muted: #898781;
    --grid: #2c2c2a;
    --axis: #383835;
    --peak: #3987e5;
    --average: #d95926;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--page);
  color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
}
main {
  max-width: 960px;
  margin: 0 auto;
  padding: 24px 16px 48px;
}
h1 {
  font-size: 18px;
  font-weight: 600;
  margin: 0 0 4px;
}
p.sub {
  color: var(--ink-secondary);
  font-size: 13px;
  margin: 0 0 20px;
}
.controls {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 16px;
}
label {
  font-size: 13px;
  color: var(--ink-secondary);
}
select {
  font: inherit;
  font-size: 13px;
  padding: 4px 8px;
  border-radius: 6px;
  border: 1px solid var(--axis);
  background: var(--surface);
  color: var(--ink);
}
.chart-card {
  background: var(--surface);
  border: 1px solid var(--grid);
  border-radius: 8px;
  padding: 16px;
}
.legend {
  display: flex;
  gap: 16px;
  font-size: 12px;
  color: var(--ink-secondary);
  margin-bottom: 8px;
}
.legend span {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.swatch {
  width: 10px;
  height: 10px;
  border-radius: 2px;
  display: inline-block;
}
svg { display: block; width: 100%; height: auto; }
.gridline { stroke: var(--grid); stroke-width: 1; }
.axis-line { stroke: var(--axis); stroke-width: 1; }
.axis-label { fill: var(--ink-muted); font-size: 11px; }
.line-peak { fill: none; stroke: var(--peak); stroke-width: 2; }
.line-average { fill: none; stroke: var(--average); stroke-width: 2; }
.hover-line { stroke: var(--axis); stroke-width: 1; opacity: 0; }
.tooltip {
  position: absolute;
  pointer-events: none;
  background: var(--surface);
  border: 1px solid var(--grid);
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 12px;
  color: var(--ink);
  opacity: 0;
  white-space: nowrap;
}
.empty {
  color: var(--ink-muted);
  font-size: 13px;
  padding: 40px 0;
  text-align: center;
}
</style>
</head>
<body>
<main>
<h1>Concurrency</h1>
<p class="sub">Peak and average concurrency per hour, from marts.v_concurrency.</p>
<div class="controls">
<label for="platform">Platform</label>
<select id="platform"><option value="">All platforms</option></select>
</div>
<div class="chart-card" style="position: relative;">
<div class="legend">
<span><i class="swatch" style="background: var(--peak);"></i>Peak</span>
<span><i class="swatch" style="background: var(--average);"></i>Average</span>
</div>
<div id="chart-wrap" style="position: relative;">
<svg id="chart" viewBox="0 0 900 320" preserveAspectRatio="none"></svg>
<div id="tooltip" class="tooltip"></div>
</div>
</div>
</main>
<script>
const svgNS = "http://www.w3.org/2000/svg";
const chart = document.getElementById("chart");
const tooltip = document.getElementById("tooltip");
const platformSelect = document.getElementById("platform");
const width = 900, height = 320, padLeft = 48, padRight = 12, padTop = 12, padBottom = 28;

function el(tag, attrs) {
  const node = document.createElementNS(svgNS, tag);
  for (const k in attrs) node.setAttribute(k, attrs[k]);
  return node;
}

async function loadPlatforms() {
  const res = await fetch("/api/platforms");
  const data = await res.json();
  for (const p of data.platforms) {
    const opt = document.createElement("option");
    opt.value = p;
    opt.textContent = p;
    platformSelect.appendChild(opt);
  }
}

async function loadConcurrency(platform) {
  const res = await fetch("/api/concurrency?platform=" + encodeURIComponent(platform) + "&grain=60");
  return await res.json();
}

function draw(payload) {
  chart.innerHTML = "";
  const rows = payload.rows;
  if (!rows.length) {
    const wrap = document.getElementById("chart-wrap");
    wrap.querySelector(".empty")?.remove();
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "No data for this filter";
    wrap.appendChild(empty);
    return;
  }
  document.getElementById("chart-wrap").querySelector(".empty")?.remove();

  const xs = rows.map(r => r.bucket_minute);
  const peaks = rows.map(r => r.peak_concurrency);
  const avgs = rows.map(r => r.average_concurrency);
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  const yMax = Math.max(...peaks, ...avgs) * 1.08 || 1;

  const xScale = x => padLeft + (xMax === xMin ? 0 : (x - xMin) / (xMax - xMin)) * (width - padLeft - padRight);
  const yScale = y => height - padBottom - (y / yMax) * (height - padTop - padBottom);

  const gridCount = 4;
  for (let i = 0; i <= gridCount; i++) {
    const y = padTop + (i / gridCount) * (height - padTop - padBottom);
    chart.appendChild(el("line", { x1: padLeft, x2: width - padRight, y1: y, y2: y, class: "gridline" }));
    const value = Math.round(yMax * (1 - i / gridCount));
    const label = el("text", { x: padLeft - 8, y: y + 4, class: "axis-label", "text-anchor": "end" });
    label.textContent = value;
    chart.appendChild(label);
  }
  chart.appendChild(el("line", { x1: padLeft, x2: padLeft, y1: padTop, y2: height - padBottom, class: "axis-line" }));
  chart.appendChild(el("line", { x1: padLeft, x2: width - padRight, y1: height - padBottom, y2: height - padBottom, class: "axis-line" }));

  const tickCount = Math.min(6, xs.length);
  for (let i = 0; i < tickCount; i++) {
    const idx = Math.round(i * (xs.length - 1) / Math.max(1, tickCount - 1));
    const x = xScale(xs[idx]);
    const date = new Date(xs[idx] * 60 * 1000);
    const label = el("text", { x: x, y: height - padBottom + 16, class: "axis-label", "text-anchor": "middle" });
    label.textContent = date.toISOString().slice(5, 16).replace("T", " ");
    chart.appendChild(label);
  }

  function path(values) {
    return values.map((v, i) => (i === 0 ? "M" : "L") + xScale(xs[i]) + " " + yScale(v)).join(" ");
  }
  chart.appendChild(el("path", { d: path(peaks), class: "line-peak" }));
  chart.appendChild(el("path", { d: path(avgs), class: "line-average" }));

  const hoverLine = el("line", { x1: 0, x2: 0, y1: padTop, y2: height - padBottom, class: "hover-line" });
  chart.appendChild(hoverLine);
  const hitArea = el("rect", {
    x: padLeft, y: padTop, width: width - padLeft - padRight, height: height - padTop - padBottom,
    fill: "transparent",
  });
  chart.appendChild(hitArea);

  hitArea.addEventListener("mousemove", ev => {
    const rect = chart.getBoundingClientRect();
    const scaleX = width / rect.width;
    const px = (ev.clientX - rect.left) * scaleX;
    const frac = Math.max(0, Math.min(1, (px - padLeft) / (width - padLeft - padRight)));
    const idx = Math.round(frac * (xs.length - 1));
    const x = xScale(xs[idx]);
    hoverLine.setAttribute("x1", x);
    hoverLine.setAttribute("x2", x);
    hoverLine.setAttribute("opacity", 1);
    const date = new Date(xs[idx] * 60 * 1000);
    tooltip.innerHTML = date.toISOString().slice(0, 16).replace("T", " ") +
      "<br>peak " + peaks[idx] + "<br>average " + avgs[idx].toFixed(1);
    tooltip.style.opacity = 1;
    tooltip.style.left = Math.min(rect.width - 140, (x / scaleX)) + "px";
    tooltip.style.top = "0px";
  });
  hitArea.addEventListener("mouseleave", () => {
    hoverLine.setAttribute("opacity", 0);
    tooltip.style.opacity = 0;
  });
}

async function refresh() {
  const payload = await loadConcurrency(platformSelect.value);
  draw(payload);
}

platformSelect.addEventListener("change", refresh);
loadPlatforms().then(refresh);
</script>
</body>
</html>
"""


def concurrency_rows(ch: ClickHouse, platform: str, grain: int) -> list[dict]:
    result = ch.query(CONCURRENCY_SQL.replace("{marts}", marts()),
                      settings={"param_grain": grain, "param_platform": platform})
    return result.dicts()


def platforms(ch: ClickHouse) -> list[str]:
    return ch.query(PLATFORMS_SQL).column("platform")


def make_handler(ch: ClickHouse):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def send_json(self, payload, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path == "/":
                self.send_html(INDEX_HTML)
            elif parsed.path == "/api/platforms":
                self.send_json({"platforms": platforms(ch)})
            elif parsed.path == "/api/concurrency":
                platform = query.get("platform", [""])[0]
                grain = int(query.get("grain", [str(GRAIN_MINUTES)])[0])
                rows = concurrency_rows(ch, platform, grain)
                self.send_json({"platform": platform, "grain_minutes": grain, "rows": rows})
            else:
                self.send_json({"error": "not found"}, status=404)

    return Handler


def run(ch: ClickHouse, port: int = 8090) -> None:
    server = HTTPServer(("0.0.0.0", port), make_handler(ch))
    print(f"clickliv dashboard at http://localhost:{port}")
    server.serve_forever()


if __name__ == "__main__":
    from .cli import load_dotenv

    load_dotenv()
    run(ClickHouse())
