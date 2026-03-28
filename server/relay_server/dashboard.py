"""HTML dashboard generation for workstream overview."""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape


_STATUS_CONFIG = {
    "active": {"label": "Active", "color": "#4ade80", "bg": "#166534", "stripe_fallback": "#4ade80"},
    "parked": {"label": "Parked", "color": "#fbbf24", "bg": "#713f12", "stripe_fallback": "#fbbf24"},
    "completed": {"label": "Completed", "color": "#9ca3af", "bg": "#374151", "stripe_fallback": "#4b5563"},
}

_STATUS_ORDER = ["active", "parked", "completed"]


def _relative_time(iso_date: str) -> str:
    """Convert ISO date string to relative time like '2h ago', '3d ago'."""
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return "just now"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        if seconds < 86400:
            return f"{seconds // 3600}h ago"
        days = seconds // 86400
        if days < 14:
            return f"{days}d ago"
        return f"{days // 7}w ago"
    except (ValueError, TypeError):
        return iso_date or "\u2014"


def _lighten_color(hex_color: str, amount: float) -> str:
    """Lighten a hex color by a fraction."""
    try:
        h = hex_color.lstrip("#")
        if len(h) != 6:
            return "#3a3a4a"
        r, g, b = int(h[:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        r = min(255, int(r + (255 - r) * amount))
        g = min(255, int(g + (255 - g) * amount))
        b = min(255, int(b + (255 - b) * amount))
        return f"#{r:02x}{g:02x}{b:02x}"
    except (ValueError, TypeError):
        return "#3a3a4a"


def _hex_to_rgb(hex_color: str) -> str:
    """Convert '#4ade80' to '74,222,128' for rgba()."""
    try:
        h = hex_color.lstrip("#")
        return f"{int(h[:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)}"
    except (ValueError, IndexError):
        return "128,128,128"


def _render_row(name: str, ws: dict, status_cfg: dict) -> str:
    """Render a single workstream table row."""
    color = ws.get("color", "") or status_cfg["stripe_fallback"]
    desc = escape(ws.get("description", ""))
    touched = _relative_time(ws.get("last_touched", ""))
    git = ws.get("git", {}) or {}
    branch = escape(git.get("branch", "")) or "\u2014"
    status = ws.get("status", "unknown")
    name_color = "#e0e0e0" if status != "completed" else "#6b7280"
    terminal_bg = ws.get("color", "") or "#1e1e1e"
    terminal_border = _lighten_color(terminal_bg, 0.3)

    return f"""        <tr>
          <td class="stripe" style="background:{escape(color)};"></td>
          <td class="cell">
            <div class="ws-name" style="color:{name_color};">{escape(name)}</div>
            <div class="ws-desc">{desc}</div>
          </td>
          <td class="cell"><span class="badge" style="background:{status_cfg['bg']};color:{status_cfg['color']};">{status_cfg['label']}</span></td>
          <td class="cell meta">{touched}</td>
          <td class="cell branch">{branch}</td>
          <td class="cell">
            <div class="terminal" style="background:{escape(terminal_bg)};border-color:{terminal_border};">
              <span class="prompt">$</span> <span style="color:{name_color};">claude</span>
              <div class="statusline" style="color:{status_cfg['color']};">{escape(name)} &middot; {branch}</div>
            </div>
          </td>
        </tr>"""


def render_dashboard_html(workstreams: dict, ideas: list[dict]) -> str:
    """Generate a self-contained HTML dashboard for workstream overview."""
    total_ws = len(workstreams)
    total_ideas = len(ideas)

    # Group by status
    groups: dict[str, list[tuple[str, dict]]] = {s: [] for s in _STATUS_ORDER}
    for name, ws in sorted(workstreams.items()):
        status = ws.get("status", "active")
        if status in groups:
            groups[status].append((name, ws))
        else:
            groups["active"].append((name, ws))

    # Build table rows
    rows_html = ""
    for status in _STATUS_ORDER:
        items = groups[status]
        if not items:
            continue
        cfg = _STATUS_CONFIG[status]
        rows_html += f"""        <tr class="section-header">
          <td colspan="6" style="color:{cfg['color']};background:rgba({_hex_to_rgb(cfg['color'])},0.05);">{cfg['label']}</td>
        </tr>\n"""
        for name, ws in items:
            rows_html += _render_row(name, ws, cfg) + "\n"

    if not rows_html:
        rows_html = '        <tr><td colspan="6" class="empty">No workstreams found. Use create_workstream to create one.</td></tr>'

    # Ideas
    ideas_html = ""
    if ideas:
        pills = "\n".join(
            f'            <span class="idea-pill">#{escape(str(idea.get("id", "")))} {escape(idea.get("text", ""))}</span>'
            for idea in ideas
        )
        ideas_html = f"""      <div class="ideas-header">Ideas</div>
      <div class="ideas-body">
{pills}
      </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Workstream Dashboard</title>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ background: #0f0f17; color: #e0e0e0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; font-size: 13px; padding: 20px; }}
    .header {{ display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; background: #1a1a2e; border: 1px solid #3a3a4a; border-radius: 8px 8px 0 0; }}
    .header h1 {{ font-size: 14px; font-weight: 600; }}
    .header .meta {{ font-size: 12px; color: #6b7280; }}
    table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
    col.stripe-col {{ width: 4px; }}
    col.name-col {{ width: 28%; }}
    col.status-col {{ width: 10%; }}
    col.touched-col {{ width: 10%; }}
    col.branch-col {{ width: 18%; }}
    col.terminal-col {{ width: auto; }}
    thead th {{ padding: 8px 12px; text-align: left; font-size: 11px; font-weight: 500; color: #6b7280; text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 1px solid #3a3a4a; }}
    thead th:first-child {{ padding: 0; }}
    .section-header td {{ padding: 10px 16px 4px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; border: none; }}
    tr {{ border-bottom: 1px solid #2a2a3a; }}
    .stripe {{ padding: 0 !important; }}
    .cell {{ padding: 10px 12px; }}
    .ws-name {{ font-weight: 600; }}
    .ws-desc {{ font-size: 11px; color: #6b7280; margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .badge {{ font-size: 11px; padding: 2px 8px; border-radius: 10px; white-space: nowrap; }}
    .meta {{ color: #6b7280; font-size: 12px; white-space: nowrap; }}
    .branch {{ font-family: monospace; font-size: 12px; color: #818cf8; }}
    .terminal {{ border-radius: 4px; padding: 6px 10px; font-family: monospace; font-size: 11px; border: 1px solid #3a3a4a; }}
    .prompt {{ color: #6b7280; }}
    .statusline {{ font-size: 10px; margin-top: 2px; }}
    .empty {{ padding: 24px; text-align: center; color: #6b7280; }}
    .table-wrap {{ background: #12121e; border: 1px solid #3a3a4a; border-top: none; border-radius: 0 0 8px 8px; overflow: hidden; }}
    .ideas-header {{ padding: 10px 16px 4px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: #a78bfa; border-top: 1px solid #3a3a4a; }}
    .ideas-body {{ padding: 4px 16px 14px; display: flex; gap: 8px; flex-wrap: wrap; }}
    .idea-pill {{ font-size: 12px; color: #c4b5fd; background: rgba(167,139,250,0.1); padding: 4px 10px; border-radius: 12px; border: 1px solid rgba(167,139,250,0.2); }}
  </style>
</head>
<body>
  <div class="header">
    <h1>Workstream Dashboard</h1>
    <span class="meta">{total_ws} workstream{"s" if total_ws != 1 else ""} &middot; {total_ideas} idea{"s" if total_ideas != 1 else ""}</span>
  </div>
  <div class="table-wrap">
    <table>
      <colgroup>
        <col class="stripe-col">
        <col class="name-col">
        <col class="status-col">
        <col class="touched-col">
        <col class="branch-col">
        <col class="terminal-col">
      </colgroup>
      <thead>
        <tr>
          <th></th>
          <th>Workstream</th>
          <th>Status</th>
          <th>Touched</th>
          <th>Branch</th>
          <th>Terminal</th>
        </tr>
      </thead>
      <tbody>
{rows_html}
      </tbody>
    </table>
{ideas_html}
  </div>
</body>
</html>"""
