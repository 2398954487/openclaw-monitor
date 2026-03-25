"""
OpenClaw Monitor - 监控面板后端
"""
import os, json, time, threading, urllib.request, psutil, subprocess
from flask import Flask, jsonify, render_template, send_file

# 读取配置
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_FILE = os.path.join(_APP_DIR, "config.json")
_CONFIG = {}
if os.path.exists(_CONFIG_FILE):
    with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
        _CONFIG = json.load(f)

_SRV = _CONFIG.get("server", {})
HOST = _SRV.get("host", "127.0.0.1")
PORT = _SRV.get("port", 19100)
TITLE = _SRV.get("title", "OpenClaw Monitor")
REFRESH_INTERVAL = _CONFIG.get("refresh_interval", 60000)

_OC = _CONFIG.get("openclaw", {})
OPENCLAW_DIR = os.path.expanduser(_OC.get("dir", "~/.openclaw"))
GATEWAY_URL = _OC.get("gateway_url", "http://127.0.0.1:18789")
AGENTS_DIR = os.path.join(OPENCLAW_DIR, "agents")
CONFIG_FILE = os.path.join(OPENCLAW_DIR, "openclaw.json")

app = Flask(__name__)
cache = {}
cache_lock = threading.Lock()

def _format_age(mtime, now):
    secs = now - mtime
    if secs < 60:
        return "刚刚"
    elif secs < 3600:
        return f"{int(secs/60)}分钟前"
    elif secs < 86400:
        return f"{int(secs/3600)}小时前"
    else:
        return f"{int(secs/86400)}天前"

def update_cache():
    startup_time = 0
    while True:
        try:
            with cache_lock:
                cache["updated_at"] = time.strftime("%H:%M:%S")

                # === Gateway 状态 ===
                try:
                    t0 = time.time()
                    req = urllib.request.Request(
                        GATEWAY_URL + "/health",
                        headers={"Accept": "application/json"}
                    )
                    r = urllib.request.urlopen(req, timeout=5)
                    data = json.loads(r.read())
                    t1 = time.time()
                    cache["gateway"] = {
                        "ok": data.get("ok", False),
                        "status": data.get("status", "unknown"),
                        "version": "v1.0.0",
                        "uptime": "--",
                        "latency_ms": round((t1 - t0) * 1000, 1),
                    }
                except:
                    cache["gateway"] = {"ok": False}

                # === OpenClaw 配置 ===
                try:
                    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                        config = json.load(f)
                    mcps = list(config.get("mcp", {}).keys())
                    cache["config"] = {
                        "model_count": len(config.get("models", [])),
                        "agent_count": len(config.get("agents", {})),
                        "channel_count": len(config.get("channels", {})),
                        "version": config.get("meta", {}).get("version", "unknown"),
                    }
                    cache["mcp_servers"] = mcps
                except:
                    cache["config"] = {}
                    cache["mcp_servers"] = []

                # === OpenClaw 实时状态 + 活跃会话消息 ===
                try:
                    sessions_file = os.path.join(AGENTS_DIR, "main", "sessions", "sessions.json")
                    if os.path.exists(sessions_file):
                        with open(sessions_file, "r", encoding="utf-8") as f:
                            sessions = json.load(f)
                        latest_sid = max(sessions.keys(), key=lambda k: sessions[k].get("updatedAt", 0))
                        s = sessions[latest_sid]
                        updated = s.get("updatedAt", 0) / 1000
                        status = s.get("status", "")
                        recent_task = ""
                        try:
                            msgs_list = s.get("state", {}).get("messages", [])
                            for m in reversed(msgs_list[-10:]):
                                if m.get("role") == "assistant":
                                    content = m.get("content", "")
                                    if content:
                                        recent_task = content[:60] + ("..." if len(content) > 60 else "")
                                        break
                        except:
                            pass
                        if status == "running":
                            cache["openclaw_status"] = {"icon": "🦞", "text": "工作中", "detail": recent_task or "处理任务中...", "color": "#22c55e"}
                        elif status == "active":
                            cache["openclaw_status"] = {"icon": "⏳", "text": "空闲中", "detail": recent_task or "等待指令...", "color": "#f59e0b"}
                        elif time.time() - updated < 300:
                            cache["openclaw_status"] = {"icon": "💤", "text": "刚结束", "detail": recent_task or f"{int(time.time()-updated)}秒前", "color": "#888"}
                        else:
                            cache["openclaw_status"] = {"icon": "😴", "text": "休息中", "detail": "长时间无活动", "color": "#555"}
                        # 读JSONL获取消息
                        msgs = []
                        sf_rel = s.get("sessionFile", "")
                        if sf_rel:
                            sf_path = os.path.join(os.path.dirname(sessions_file), sf_rel)
                            if os.path.exists(sf_path):
                                with open(sf_path, "r", encoding="utf-8", errors="replace") as f:
                                    for line in f.readlines()[-20:]:
                                        line = line.strip()
                                        if not line:
                                            continue
                                        try:
                                            entry = json.loads(line)
                                            role = entry.get("role") or "assistant"
                                            raw_content = ""
                                            msg = entry.get("message", {})
                                            if isinstance(msg, dict):
                                                blocks = msg.get("content", [])
                                                if isinstance(blocks, list):
                                                    # Extract readable text from blocks
                                                    parts = []
                                                    for b in blocks:
                                                        if isinstance(b, dict):
                                                            t = b.get("type", "")
                                                            if t == "text":
                                                                parts.append(b.get("text", ""))
                                                            elif t == "thinking":
                                                                parts.append("[思考] " + b.get("thinking", "")[:80])
                                                            elif t == "toolCall":
                                                                parts.append("[工具] " + b.get("name", ""))
                                                            elif t == "toolResult":
                                                                parts.append("[结果] " + str(b.get("result", ""))[:80])
                                                    raw_content = " ".join(p for p in parts if p)
                                                else:
                                                    raw_content = str(blocks)
                                            elif isinstance(entry.get("content"), str):
                                                raw_content = entry.get("content", "")
                                            if raw_content:
                                                msgs.append({"role": role, "content": raw_content[:200]})
                                        except:
                                            pass
                        cache["live_messages"] = msgs
                    else:
                        cache["openclaw_status"] = {"icon": "❓", "text": "未知", "detail": "无会话数据", "color": "#888"}
                        cache["live_messages"] = []
                except Exception as e:
                    cache["openclaw_status"] = {"icon": "❓", "text": "未知", "detail": "读取失败", "color": "#888"}
                    cache["live_messages"] = []

                # === 系统信息 ===
                try:
                    mem = psutil.virtual_memory()
                    net = psutil.net_io_counters()
                    cache["system"] = {
                        "cpu_percent": psutil.cpu_percent(interval=0.5),
                        "memory_used_gb": round(mem.used / (1024**3), 1),
                        "memory_total_gb": round(mem.total / (1024**3), 1),
                        "memory_percent": mem.percent,
                        "net_sent_mb": round(net.bytes_sent / (1024**2), 1),
                        "net_recv_mb": round(net.bytes_recv / (1024**2), 1),
                    }
                    try:
                        disk = psutil.disk_usage('C:\\')
                        cache["disk"] = {
                            "total_gb": round(disk.total / (1024**3), 1),
                            "used_gb": round(disk.used / (1024**3), 1),
                            "percent": disk.percent,
                        }
                    except:
                        cache["disk"] = {}
                except:
                    pass

                # === Docker ===
                try:
                    result = subprocess.run(
                        ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
                        capture_output=True, text=True, timeout=5
                    )
                    containers = []
                    for line in result.stdout.strip().split("\n"):
                        if line:
                            parts = line.split("\t")
                            if len(parts) >= 2:
                                containers.append({"name": parts[0], "status": parts[1]})
                    cache["docker"] = {"containers": containers}
                except:
                    cache["docker"] = {"containers": []}

                # === 会话统计 ===
                try:
                    sessions_file = os.path.join(AGENTS_DIR, "main", "sessions", "sessions.json")
                    if os.path.exists(sessions_file):
                        with open(sessions_file, "r", encoding="utf-8") as f:
                            sessions = json.load(f)
                        cache["sessions"] = sessions
                        cache["session_count"] = len(sessions)
                        today_start = time.time() - 86400
                        today_sessions = {
                            k: v for k, v in sessions.items()
                            if v.get("updatedAt", 0) / 1000 > today_start
                        }
                        cache["today_conversations"] = len(today_sessions)
                    else:
                        cache["sessions"] = {}
                        cache["session_count"] = 0
                        cache["today_conversations"] = 0
                except:
                    cache["sessions"] = {}
                    cache["session_count"] = 0
                    cache["today_conversations"] = 0

                # === Token 统计（从 JSONL 文件读取）===
                try:
                    sessions_dir = os.path.join(AGENTS_DIR, "main", "sessions")
                    all_files = []
                    for root, dirs, files in os.walk(sessions_dir):
                        for fname in files:
                            if fname.endswith(".jsonl"):
                                all_files.append(os.path.join(root, fname))
                    total_input = 0
                    total_output = 0
                    total_tokens = 0
                    total_cost = 0.0
                    for fp in all_files:
                        try:
                            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                                for line in f:
                                    line = line.strip()
                                    if not line:
                                        continue
                                    try:
                                        entry = json.loads(line)
                                        usage = entry.get("usage")
                                        if not usage:
                                            msg = entry.get("message", {})
                                            if isinstance(msg, dict):
                                                usage = msg.get("usage")
                                        if usage and isinstance(usage, dict):
                                            total_input += usage.get("input", 0) or 0
                                            total_output += usage.get("output", 0) or 0
                                            total_tokens += usage.get("totalTokens", 0) or 0
                                            cost = usage.get("cost", {})
                                            if isinstance(cost, dict):
                                                total_cost += cost.get("total", 0) or 0
                                            elif isinstance(cost, (int, float)):
                                                total_cost += cost
                                    except:
                                        pass
                        except:
                            pass
                    cache["tokens"] = {
                        "input_tokens": total_input,
                        "output_tokens": total_output,
                        "total_tokens": total_tokens,
                        "cost_usd": round(total_cost, 4),
                    }
                except:
                    cache["tokens"] = {}

                # === Agent 信息 ===
                try:
                    cfg_file = os.path.join(OPENCLAW_DIR, "openclaw.json")
                    if os.path.exists(cfg_file):
                        with open(cfg_file, "r", encoding="utf-8") as f:
                            cfg = json.load(f)
                        agents_cfg = cfg.get("agents", {})
                        defaults = agents_cfg.get("defaults", {})
                        models = defaults.get("models", {})
                        model_list = list(models.keys())
                        primary = defaults.get("model", {}).get("primary", "--")
                        compaction = defaults.get("compaction", {}).get("mode", "--")
                        workspace = defaults.get("workspace", "--")
                        channels = list(cfg.get("channels", {}).keys())
                        current_model = "--"
                        try:
                            sessions_file = os.path.join(AGENTS_DIR, "main", "sessions", "sessions.json")
                            if os.path.exists(sessions_file):
                                with open(sessions_file, "r", encoding="utf-8") as f:
                                    sessions = json.load(f)
                                if sessions:
                                    latest = max(sessions.values(), key=lambda s: s.get("updatedAt", 0))
                                    current_model = latest.get("model", "--")
                        except:
                            pass
                        cache["agent_info"] = {
                            "primary_model": primary,
                            "current_model": current_model,
                            "model_count": len(model_list),
                            "compaction": compaction,
                            "workspace": workspace.replace(os.path.expanduser("~"), "~"),
                            "channels": channels,
                        }
                    else:
                        cache["agent_info"] = {}
                except:
                    cache["agent_info"] = {}

                # === 活动日志 ===
                try:
                    if not hasattr(update_cache, "_prev_sessions"):
                        update_cache._prev_sessions = {}
                    if not hasattr(update_cache, "_activity_log"):
                        update_cache._activity_log = []
                    sessions_file = os.path.join(AGENTS_DIR, "main", "sessions", "sessions.json")
                    if os.path.exists(sessions_file):
                        with open(sessions_file, "r", encoding="utf-8") as f:
                            curr_sessions = json.load(f)
                        now_ts = time.time()
                        for sid, s in curr_sessions.items():
                            updated = s.get("updatedAt", 0) / 1000
                            prev = update_cache._prev_sessions.get(sid, 0)
                            if updated > prev and prev > 0:
                                status = s.get("status", "")
                                if status == "running":
                                    label = "会话执行中"
                                elif status == "active":
                                    label = "新会话激活"
                                else:
                                    label = "会话更新"
                                update_cache._activity_log.append({
                                    "time": time.strftime("%H:%M:%S", time.localtime(now_ts)),
                                    "label": label,
                                    "session": sid[:8],
                                })
                            update_cache._prev_sessions[sid] = updated
                        update_cache._activity_log = update_cache._activity_log[-50:]
                        cache["activity_log"] = update_cache._activity_log
                except:
                    pass

        except Exception as e:
            print(f"[update_cache] error: {e}")

        time.sleep(int(REFRESH_INTERVAL / 1000) or 30)

# ============================================================
# 路由
# ============================================================

@app.route("/")
def index():
    bg_img = _CONFIG.get("theme", {}).get("background_image", "")
    bg_color = _CONFIG.get("theme", {}).get("background_color", "#0f1419")
    footer_text = _CONFIG.get("footer", {}).get("text", "")
    return render_template("index.html", bg_image=bg_img, bg_color=bg_color, footer_text=footer_text)

@app.route("/wallpaper.jpg")
def wallpaper():
    if os.path.exists("wallpaper.jpg"):
        return send_file("wallpaper.jpg")
    return "", 404

@app.route("/camera.jpg")
def camera_img():
    if os.path.exists("camera_captures/latest.jpg"):
        return send_file("camera_captures/latest.jpg")
    return "", 404

@app.route("/api/overview")
def overview():
    with cache_lock:
        msgs = cache.get("live_messages", [])
        oc_status = cache.get("openclaw_status", {})
    return jsonify({
        "gateway": cache.get("gateway", {}),
        "system": cache.get("system", {}),
        "disk": cache.get("disk", {}),
        "docker": cache.get("docker", {}),
        "config": cache.get("config", {}),
        "mcp_servers": cache.get("mcp_servers", []),
        "openclaw_status": oc_status,
        "openapi_status": oc_status,
        "tokens": cache.get("tokens", {}),
        "agent_info": cache.get("agent_info", {}),
        "sessions": cache.get("sessions", {}),
        "session_count": cache.get("session_count", 0),
        "today_conversations": cache.get("today_conversations", 0),
        "messages": msgs,
        "updated_at": cache.get("updated_at", "--"),
    })

@app.route("/api/skills")
def skills():
    with cache_lock:
        cfg = cache.get("config", {})
        mcps = cache.get("mcp_servers", [])
    skills_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")
    skill_list = []
    if os.path.exists(skills_dir):
        for f in os.listdir(skills_dir):
            md = os.path.join(skills_dir, f, "SKILL.md")
            if os.path.exists(md):
                with open(md, "r", encoding="utf-8", errors="replace") as fp:
                    first_line = fp.readline().strip()
                    name = first_line.replace("---", "").replace("name:", "").strip()
                    skill_list.append({"name": name or f})
    return jsonify({
        "skills": skill_list,
        "mcp_servers": mcps,
    })

@app.route("/api/activity")
def activity():
    with cache_lock:
        events = cache.get("activity_log", [])
    return jsonify({"events": events[-50:]})

@app.route("/api/camera/status")
def camera_status():
    with cache_lock:
        pass
    return jsonify({"has_image": False, "last_capture": None})

@app.route("/api/action/reload-config", methods=["POST"])
def action_reload():
    with cache_lock:
        cache.clear()
    return jsonify({"ok": True, "message": "配置已刷新"})

@app.route("/api/action/restart-gateway", methods=["POST"])
def action_restart():
    try:
        subprocess.Popen(["powershell", "-Command", "Restart-Service", "OpenClawGateway"], shell=False)
        return jsonify({"ok": True, "message": "Gateway 重启命令已发送"})
    except:
        return jsonify({"ok": False, "message": "重启失败"}), 500

@app.route("/api/stream")
def stream():
    from flask import Response
    def generate():
        t0 = time.time()
        while time.time() - t0 < 55:
            with cache_lock:
                data = dict(cache)
            yield f"data: {json.dumps(data)}\n\n"
            time.sleep(int(REFRESH_INTERVAL / 1000))
    return Response(generate(), mimetype="text/event-stream")

# ============================================================
# 启动
# ============================================================
if __name__ == "__main__":
    t = threading.Thread(target=update_cache, daemon=True)
    t.start()
    print(f"[OpenClaw Monitor] Starting on {HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
