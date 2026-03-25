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
HOST = _SRV.get("host", "0.0.0.0")
PORT = _SRV.get("port", 19100)
TITLE = _SRV.get("title", "OpenClaw Monitor")
REFRESH_INTERVAL = _CONFIG.get("refresh_interval", 60000) / 1000  # 秒转毫秒

_OC = _CONFIG.get("openclaw", {})
OPENCLAW_DIR = os.path.expanduser(_OC.get("dir", "~/.openclaw"))
GATEWAY_URL = _OC.get("gateway_url", "http://127.0.0.1:18789")
AGENTS_DIR = os.path.join(OPENCLAW_DIR, "agents")
CONFIG_FILE = os.path.join(OPENCLAW_DIR, "openclaw.json")

app = Flask(__name__)
cache = {}
cache_lock = threading.Lock()
def update_cache():
    while True:
        with cache_lock:
            try:
                # Gateway + 响应时间
                try:
                    t0 = time.time()
                    req = urllib.request.Request(f"{GATEWAY_URL}/health")
                    resp = urllib.request.urlopen(req, timeout=3)
                    t1 = time.time()
                    data = json.loads(resp.read())
                    cache["gateway"] = {
                        "ok": data.get("ok", False),
                        "status": data.get("status", "unknown"),
                        "version": "v1.0.0",
                        "uptime": data.get("uptime", "--"),
                        "latency_ms": round((t1 - t0) * 1000, 1),
                    }
                except:
                    cache["gateway"] = {"ok": False, "status": "down", "latency_ms": None}

                # Sessions
                try:
                    total = 0
                    by_agent = {}
                    if os.path.exists(AGENTS_DIR):
                        for agent_dir in os.listdir(AGENTS_DIR):
                            sf = os.path.join(AGENTS_DIR, agent_dir, "sessions", "sessions.json")
                            if os.path.exists(sf):
                                with open(sf, "r", encoding="utf-8") as f:
                                    sessions = json.load(f)
                                by_agent[agent_dir] = len(sessions)
                                total += len(sessions)
                    cache["sessions"] = {"total": total, "by_agent": by_agent}
                except:
                    cache["sessions"] = {"total": 0}

                # Config
                try:
                    if os.path.exists(CONFIG_FILE):
                        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                            config = json.load(f)
                        mcps = []
                        for name, plugin in config.get("plugins", {}).items():
                            for srv in plugin.get("mcp", {}).keys():
                                mcps.append(srv)
                        cache["config"] = {
                            "model_count": len(config.get("models", [])),
                            "agent_count": len(config.get("agents", {})),
                            "channel_count": len(config.get("channels", {})),
                            "version": config.get("meta", {}).get("version", "unknown"),
                        }
                        cache["mcp_servers"] = mcps

                        # 最近修改的文件
                        recent_files = []
                        now = time.time()
                        try:
                            for base in RECENT_SCAN_PATHS:
                                if not os.path.exists(base):
                                    continue
                                for root, dirs, files in os.walk(base):
                                    # 跳过 node_modules 和 .git
                                    dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "__pycache__")]
                                    for fname in files:
                                        if fname.startswith("."):
                                            continue
                                        fpath = os.path.join(root, fname)
                                        try:
                                            mtime = os.path.getmtime(fpath)
                                            age_hours = (now - mtime) / 3600
                                            if age_hours < 24:  # 24小时内的
                                                recent_files.append({
                                                    "name": fname,
                                                    "path": root.replace(os.path.expanduser("~"), "~"),
                                                    "mtime": time.strftime("%H:%M", time.localtime(mtime)),
                                                    "age": _format_age(mtime, now),
                                                })
                                        except:
                                            pass
                            # 按时间排序，取最新的20个
                            recent_files.sort(key=lambda x: x["mtime"], reverse=True)
                            cache["recent_files"] = recent_files[:20]
                        except:
                            cache["recent_files"] = []

                        # Token 统计（从 transcript JSONL 文件读取）
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

                        # Agent 信息
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
                                # 当前使用中的模型（从最新 session 读取）
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

                        # 活动日志
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

                        # 计划任务
                        try:
                            result = subprocess.run([
                                "powershell", "-ExecutionPolicy", "Bypass", "-File",
                                r"C:\Users\Administrator\get_tasks.ps1"
                            ], capture_output=True, text=True, timeout=15)
                            raw = result.stdout.strip()
                            if raw and raw != "null":
                                data = json.loads(raw)
                                items = data if isinstance(data, list) else [data]
                                tasks = []
                                for t in items[:15]:
                                    state_map = {"Ready":"Ready","Running":"Running","Disabled":"Disabled","Unknown":"Unknown"}
                                    state = t.get("State","--")
                                    tasks.append({
                                        "name": t.get("Name","--"),
                                        "state": state,
                                        "last_run": t.get("LastRun","--"),
                                        "next_run": t.get("NextRun","--"),
                                    })
                                cache["scheduled_tasks"] = tasks
                            else:
                                cache["scheduled_tasks"] = []
                        except:
                            cache["scheduled_tasks"] = []
                    else:
                        cache["config"] = {}
                        cache["mcp_servers"] = []
                except:
                    cache["config"] = {}
                    cache["mcp_servers"] = []

                # System
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
                    # 磁盘
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

                # Docker
                try:
                    result = subprocess.run(
                        ["docker", "ps", "--format", "{{.Names}}|{{.Status}}|{{.Ports}}"],
                        capture_output=True, text=True, timeout=5
                    )
                    containers = []
                    for line in result.stdout.strip().split("\n"):
                        if line:
                            parts = line.split("|")
                            containers.append({"name": parts[0], "status": parts[1], "ports": parts[2] if len(parts) > 2 else ""})
                    cache["docker"] = {"containers": containers, "count": len(containers)}
                except:
                    cache["docker"] = {"containers": [], "count": 0}

                # 所有会话的消息总数
                try:
                    msg_count = 0
                    session_count = 0
                    if os.path.exists(AGENTS_DIR):
                        for agent_dir in os.listdir(AGENTS_DIR):
                            sessions_file = os.path.join(AGENTS_DIR, agent_dir, "sessions", "sessions.json")
                            if os.path.exists(sessions_file):
                                with open(sessions_file, "r", encoding="utf-8") as f:
                                    sessions = json.load(f)
                                session_count += len(sessions)
                                for sid, sess in sessions.items():
                                    sf = sess.get("sessionFile", "")
                                    if sf:
                                        if not os.path.isabs(sf):
                                            sf = os.path.join(AGENTS_DIR, agent_dir, "sessions", sf)
                                        if os.path.exists(sf):
                                            try:
                                                with open(sf, "r", encoding="utf-8") as f:
                                                    msg_count += len(f.readlines())
                                            except:
                                                pass
                    cache["today_conversations"] = msg_count
                    cache["session_count"] = session_count
                except:
                    cache["today_conversations"] = 0
                    cache["session_count"] = 0

                # 当前对话状态
                try:
                    sessions_file = os.path.join(AGENTS_DIR, "main", "sessions", "sessions.json")
                    if os.path.exists(sessions_file):
                        with open(sessions_file, "r", encoding="utf-8") as f:
                            sessions = json.load(f)
                        # 找最新的会话
                        latest_sid = max(sessions.keys(), key=lambda k: sessions[k].get("updatedAt", 0))
                        s = sessions[latest_sid]
                        status = s.get("status", "")
                        state = s.get("state", {})
                        msgs = state.get("messages", [])
                        session_file = s.get("sessionFile", "")

                        # 优先从 .jsonl 读取（包含所有历史消息）
                        messages = []
                        if session_file:
                            if not os.path.isabs(session_file):
                                session_file = os.path.join(AGENTS_DIR, "main", "sessions", session_file)
                            if os.path.exists(session_file):
                                try:
                                    with open(session_file, "r", encoding="utf-8") as sf:
                                        lines = sf.readlines()
                                    for line in lines[-30:]:
                                        try:
                                            msg = json.loads(line.strip())
                                            inner = msg.get("message", {})
                                            role = inner.get("role", msg.get("type", ""))
                                            content_blocks = inner.get("content", [])
                                            texts = []
                                            if isinstance(content_blocks, list):
                                                for block in content_blocks:
                                                    if isinstance(block, dict):
                                                        if block.get("type") == "text":
                                                            texts.append(block.get("text", ""))
                                                        elif block.get("type") == "output":
                                                            texts.append(block.get("output", ""))
                                                        elif block.get("type") == "thinking":
                                                            t = block.get("thinking", "")
                                                            if t:
                                                                texts.append(f"[思考] {t[:100]}")
                                            text = "".join(texts)
                                            if text:
                                                if len(text) > 500:
                                                    text = text[:500] + "..."
                                                messages.append({"role": role, "content": text})
                                        except:
                                            pass
                                except:
                                    pass

                        # 活跃会话（内存消息补充）
                        if status in ("active", "running") and msgs:
                            for m in msgs[-50:]:
                                role = m.get("role", m.get("type", "user"))
                                content = m.get("content", "")
                                if content and isinstance(content, str):
                                    if len(content) > 500:
                                        content = content[:500] + "..."
                                    messages.append({"role": role, "content": content})
                            cache["messages"] = messages[-30:] if len(messages) > 30 else messages
                            cache["active_conversation"] = ""
                        elif status in ("active", "running"):
                            cache["messages"] = messages[-30:] if len(messages) > 30 else messages
                            cache["active_conversation"] = ""
                        else:
                            # 已结束（done），读取 .jsonl 文件
                            session_file = s.get("sessionFile", "")
                            messages = []
                            if session_file:
                                if not os.path.isabs(session_file):
                                    session_file = os.path.join(AGENTS_DIR, "main", "sessions", session_file)
                                if os.path.exists(session_file):
                                    try:
                                        with open(session_file, "r", encoding="utf-8") as sf:
                                            lines = sf.readlines()
                                        for line in lines[-50:]:
                                            try:
                                                msg = json.loads(line.strip())
                                                inner = msg.get("message", {})
                                                role = inner.get("role", msg.get("type", ""))
                                                content_blocks = inner.get("content", [])
                                                texts = []
                                                if isinstance(content_blocks, list):
                                                    for block in content_blocks:
                                                        if isinstance(block, dict):
                                                            if block.get("type") == "text":
                                                                texts.append(block.get("text", ""))
                                                            elif block.get("type") == "output":
                                                                texts.append(block.get("output", ""))
                                                            elif block.get("type") == "thinking":
                                                                t = block.get("thinking", "")
                                                                if t:
                                                                    texts.append(f"[思考] {t[:100]}")
                                                text = "".join(texts)
                                                if text:
                                                    if len(text) > 500:
                                                        text = text[:500] + "..."
                                                    messages.append({"role": role, "content": text})
                                            except:
                                                pass
                                    except:
                                        pass
                            cache["messages"] = messages[-50:] if len(messages) > 50 else messages
                            cache["active_conversation"] = ""
                    else:
                        cache["messages"] = []
                        cache["active_conversation"] = "无会话数据"
                except Exception as ex:
                    cache["messages"] = []
                    cache["active_conversation"] = f"读取失败: {ex}"

                cache["updated_at"] = time.strftime("%H:%M:%S")

                # OpenClaw 状态
                try:
                    sessions_file = os.path.join(AGENTS_DIR, "main", "sessions", "sessions.json")
                    if os.path.exists(sessions_file):
                        with open(sessions_file, "r", encoding="utf-8") as f:
                            sessions = json.load(f)
                        latest_sid = max(sessions.keys(), key=lambda k: sessions[k].get("updatedAt", 0))
                        latest = sessions[latest_sid]
                        status = latest.get("status", "")
                        updated = latest.get("updatedAt", 0)
                        time_diff = (time.time() * 1000 - updated) / 1000 if updated else 9999
                        # 取最后一条助手消息作为最近任务
                        recent_task = ""
                        try:
                            msgs = latest.get("state", {}).get("messages", [])
                            for m in reversed(msgs[-10:]):
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
                        elif time_diff < 300:
                            cache["openclaw_status"] = {"icon": "💤", "text": "刚结束", "detail": f"{int(time_diff)}秒前: {recent_task}" if recent_task else f"{int(time_diff)}秒前", "color": "#888"}
                        else:
                            cache["openclaw_status"] = {"icon": "😴", "text": "休息中", "detail": recent_task or "长时间无活动", "color": "#555"}
                    else:
                        cache["openclaw_status"] = {"icon": "❓", "text": "未知", "detail": "无会话数据", "color": "#888"}
                except:
                    cache["openclaw_status"] = {"icon": "❓", "text": "未知", "detail": "读取失败", "color": "#888"}
            except:
                pass
        time.sleep(int(REFRESH_INTERVAL / 1000 * 0.5) or 30)  # 间隔取配置值的一半

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/overview")
def overview():
    with cache_lock:
        return jsonify({
            "gateway": cache.get("gateway", {}),
            "sessions": cache.get("sessions", {}),
            "session_count": cache.get("session_count", 0),
            "config": cache.get("config", {}),
            "tokens": cache.get("tokens", {}),
            "agent_info": cache.get("agent_info", {}),
            "system": cache.get("system", {}),
            "disk": cache.get("disk", {}),
            "docker": cache.get("docker", {}),
            "today_conversations": cache.get("today_conversations", 0),
            "mcp_servers": cache.get("mcp_servers", []),
            "active_conversation": cache.get("active_conversation", ""),
            "messages": cache.get("messages", []),
            "openclaw_status": cache.get("openclaw_status", {}),
            "updated_at": cache.get("updated_at", ""),
        })

@app.route("/api/stream")
def stream():
    """SSE长连接：状态变化时推送，无变化时每30秒心跳"""
    def event_generator():
        last_status = None
        last_msg_count = None
        last_system = None
        while True:
            with cache_lock:
                oc = cache.get("openclaw_status", {})
                msgs = cache.get("messages", [])
                sys_data = cache.get("system", {})
                gw = cache.get("gateway", {})
            cur_status = oc.get("text", "")
            cur_msg_count = len(msgs)
            cur_system = (sys_data.get("cpu_percent"), sys_data.get("memory_percent"))
            if cur_status != last_status or cur_msg_count != last_msg_count or cur_system != last_system:
                yield f"data: {json.dumps({
                    "openclaw_status": oc,
                    "messages": msgs[-30:] if msgs else [],
                    "system": sys_data,
                    "gateway": gw,
                    "updated_at": cache.get("updated_at", ""),
                }, ensure_ascii=False)}\n\n"
                last_status = cur_status
                last_msg_count = cur_msg_count
                last_system = cur_system
            time.sleep(2)
    return app.response_class(
        event_generator(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )

@app.route("/api/debug_conv")
def debug_conv():
    """临时调试：返回当前对话状态"""
    sessions_file = os.path.join(AGENTS_DIR, "main", "sessions", "sessions.json")
    if os.path.exists(sessions_file):
        with open(sessions_file, "r", encoding="utf-8") as f:
            sessions = json.load(f)
        latest_sid = max(sessions.keys(), key=lambda k: sessions[k].get("updatedAt", 0))
        s = sessions[latest_sid]
        return jsonify({
            "latest_sid": latest_sid[:8],
            "status": s.get("status"),
            "updatedAt": s.get("updatedAt"),
            "state_msgs": len(s.get("state", {}).get("messages", [])),
        })
    return jsonify({"error": "no sessions file"})

@app.route("/api/skills")
def skills():
    skills_dir = r"C:\Users\Administrator\.openclaw\workspace\skills"
    result = []
    try:
        for d in os.listdir(skills_dir):
            if os.path.isdir(os.path.join(skills_dir, d)):
                result.append({"name": d})
    except:
        pass
    return jsonify({"skills": result, "count": len(result)})

# Camera
CAMERA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "camera_captures")
CAMERA_INDEX = 0
os.makedirs(CAMERA_DIR, exist_ok=True)
_last_capture_time = 0
_capture_interval = 7200  # 2小时
_current_image = os.path.join(CAMERA_DIR, "latest.jpg")

def capture_camera():
    """后台线程：每30分钟拍照一次"""
    global _last_capture_time
    try:
        import cv2
        cap = cv2.VideoCapture(CAMERA_INDEX)
        ret, frame = cap.read()
        cap.release()
        if ret:
            cv2.imwrite(_current_image, frame)
            _last_capture_time = time.time()
            # 清理1小时前的旧图
            try:
                for f in os.listdir(CAMERA_DIR):
                    fp = os.path.join(CAMERA_DIR, f)
                    if f != "latest.jpg" and os.path.isfile(fp) and time.time() - os.path.getmtime(fp) > 3600:
                        os.remove(fp)
            except:
                pass
    except Exception as ex:
        sys.stderr.write(f"[Camera] capture error: {ex}\n")
        sys.stderr.flush()

# 启动时先拍一张
capture_camera()

def camera_updater():
    while True:
        time.sleep(_capture_interval)
        capture_camera()

@app.route("/camera.jpg")
def camera_feed():
    if os.path.exists(_current_image):
        response = send_file(_current_image, mimetype="image/jpeg")
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    return "no image", 404

@app.route("/wallpaper.jpg")
def wallpaper():
    wp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wallpaper.jpg")
    if os.path.exists(wp):
        return send_file(wp, mimetype="image/jpeg")
    return "no image", 404

@app.route("/api/camera/status")
def camera_status():
    return jsonify({
        "has_image": os.path.exists(_current_image),
        "last_capture": time.strftime("%H:%M:%S", time.localtime(_last_capture_time)) if _last_capture_time else None,
        "interval_minutes": _capture_interval // 60,
        "next_capture_in": max(0, _capture_interval - (time.time() - _last_capture_time)) if _last_capture_time else 0,
    })

@app.route("/api/action/restart-gateway", methods=["POST"])
def action_restart():
    try:
        subprocess.Popen(["powershell", "-Command", "Restart-Service", "OpenClawGateway"], shell=False)
        return jsonify({"ok": True, "message": "Gateway 重启命令已发送"})
    except:
        return jsonify({"ok": False, "message": "重启失败"}), 500

@app.route("/api/action/reload-config", methods=["POST"])
def action_reload():
    # 清除缓存，下次会重新读取配置
    with cache_lock:
        cache.clear()
    return jsonify({"ok": True, "message": "配置已刷新"})@app.route("/api/scheduled-tasks")
def scheduled_tasks():
    tasks = cache.get("scheduled_tasks", [])
    return jsonify({"tasks": tasks})

@app.route("/api/activity")
def activity():
    events = cache.get("activity_log", [])
    return jsonify({"events": events[-50:]})

@app.route("/api/agent-info")
def agent_info():
    with cache_lock:
        return jsonify(cache.get("agent_info", {}))

if __name__ == "__main__":
    t = threading.Thread(target=update_cache, daemon=True)
    t.start()
    print(f"[OpenClaw Monitor] Starting server on {HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
