import asyncio
from collections import deque
import json
import os
import platform
import subprocess
import time
from typing import Dict, List, Optional, Set
import urllib.request
import urllib.error

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse
import psutil

app = FastAPI(title="Ubuntu Monitoring Service")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")

DEFAULT_SETTINGS = {
    "notifications_enabled": True,
    "sound_enabled": True,
    "threshold_cpu": 90.0,
    "threshold_ram": 95.0,
    "threshold_temp": 80.0,
    "webhook_url": ""
}

def load_settings() -> dict:
    res = DEFAULT_SETTINGS.copy()
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                res.update(data)
        except Exception as e:
            print(f"[ATTENTION] Impossible de lire settings.json: {e}")
    return res

def save_settings(data: dict):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[ERREUR] Échec de la sauvegarde des paramètres: {e}")

settings = load_settings()

# Notification Cooldown (5 minutes)
NOTIFICATION_COOLDOWN = 300
last_notified: Dict[str, float] = {
    "cpu_usage": 0.0,
    "ram_usage": 0.0,
    "cpu_temp": 0.0,
}

# Historical memory buffer (up to 3600 points = 1 hour at 1 point/sec)
history_buffer: deque = deque(maxlen=3600)

# Disk & Net IO speed calculation variables
prev_disk_io: Optional[tuple] = None
prev_net_io: Optional[tuple] = None
prev_io_time: float = time.time()

speeds = {
    "disk_read": 0.0,
    "disk_write": 0.0,
    "net_sent": 0.0,
    "net_recv": 0.0,
}

current_metrics: dict = {}
active_connections: Set[WebSocket] = set()

def get_cpu_model() -> str:
    """Récupère le nom du modèle de processeur."""
    try:
        if os.path.exists("/proc/cpuinfo"):
            with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
                for line in f:
                    if "model name" in line:
                        return line.split(":")[1].strip()
    except Exception:
        pass
    return platform.processor() or "Processeur x86_64"

SYSTEM_INFO = {
    "hostname": platform.node(),
    "os_name": "Ubuntu Linux",
    "kernel": platform.release(),
    "architecture": platform.machine(),
    "cpu_model": get_cpu_model()
}

def get_cpu_temp() -> Optional[float]:
    """Récupère la température CPU actuelle."""
    try:
        temps = psutil.sensors_temperatures()
        if not temps:
            return None
        for name in ["coretemp", "k10temp", "zenpower"]:
            if name in temps:
                for entry in temps[name]:
                    if entry.label in ["Package id 0", "Tctl"]:
                        return entry.current
                if temps[name]:
                    return max(entry.current for entry in temps[name])
        for name, entries in temps.items():
            if entries:
                return max(entry.current for entry in entries)
    except Exception:
        pass
    return None

def get_gpu_temp() -> Optional[float]:
    """Récupère la température GPU actuelle."""
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for name in ["amdgpu", "nouveau"]:
                if name in temps:
                    for entry in temps[name]:
                        if entry.label == "edge" or not entry.label:
                            return entry.current
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=1
        )
        if res.returncode == 0:
            return float(res.stdout.strip())
    except Exception:
        pass
    return None

def send_webhook_alert(title: str, message: str):
    """Envoie une notification Webhook asynchrone (Discord / Slack / Telegram)."""
    url = settings.get("webhook_url", "").strip()
    if not url:
        return
    
    payload = json.dumps({"content": f"🚨 **{title}**\n{message}"}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Ubuntu-Monitoring"}
    )
    
    def _do_send():
        try:
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception as e:
            print(f"[ERREUR] Échec de l'envoi du Webhook: {e}")

    asyncio.get_event_loop().run_in_executor(None, _do_send)

def send_desktop_notification(title: str, message: str, urgency: str = "normal"):
    """Envoie une notification de bureau via DBus et notify-send."""
    env = os.environ.copy()
    user_uid = os.getenv("USER_UID", str(os.getuid()))
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{user_uid}/bus"
    env["DISPLAY"] = os.getenv("DISPLAY", ":0")
    
    try:
        subprocess.run(
            ["notify-send", "-u", urgency, "-a", "Monitoring", title, message],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    except Exception as e:
        print(f"[ERREUR] Échec de la notification desktop: {e}")

def check_alerts(metrics: dict):
    """Vérifie les seuils d'alerte et envoie des notifications si nécessaire."""
    if not settings.get("notifications_enabled", True):
        return
        
    now = time.time()
    th_cpu = float(settings.get("threshold_cpu", 90.0))
    th_ram = float(settings.get("threshold_ram", 95.0))
    th_temp = float(settings.get("threshold_temp", 80.0))
    
    # 1. Alerte CPU
    cpu_percent = metrics["cpu"]["usage"]
    if cpu_percent > th_cpu:
        if now - last_notified["cpu_usage"] > NOTIFICATION_COOLDOWN:
            msg = f"L'utilisation CPU a atteint {cpu_percent:.1f}% (Seuil: {th_cpu}%)."
            send_desktop_notification("Alerte CPU Critique", msg, "critical")
            send_webhook_alert("Alerte CPU Critique", msg)
            last_notified["cpu_usage"] = now
            
    # 2. Alerte RAM
    ram_percent = metrics["ram"]["percent"]
    if ram_percent > th_ram:
        if now - last_notified["ram_usage"] > NOTIFICATION_COOLDOWN:
            msg = f"L'utilisation de la mémoire RAM est de {ram_percent:.1f}% (Seuil: {th_ram}%)."
            send_desktop_notification("Alerte RAM Critique", msg, "critical")
            send_webhook_alert("Alerte RAM Critique", msg)
            last_notified["ram_usage"] = now
            
    # 3. Alerte Température CPU
    cpu_temp = metrics["temperatures"]["cpu"]
    if cpu_temp and cpu_temp > th_temp:
        if now - last_notified["cpu_temp"] > NOTIFICATION_COOLDOWN:
            msg = f"La température du CPU atteint {cpu_temp:.1f}°C (Seuil: {th_temp}°C)."
            send_desktop_notification("Alerte Température Critique", msg, "critical")
            send_webhook_alert("Alerte Température Critique", msg)
            last_notified["cpu_temp"] = now

def get_top_processes() -> dict:
    """Récupère les 5 processus consommant le plus de CPU et les 5 consommant le plus de RAM."""
    procs = []
    for p in psutil.process_iter(['pid', 'name', 'username', 'cpu_percent', 'memory_percent', 'memory_info']):
        try:
            p_info = p.info
            p_info['memory_bytes'] = p_info['memory_info'].rss if p_info['memory_info'] else 0
            procs.append(p_info)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
            
    top_cpu = sorted(procs, key=lambda x: x.get('cpu_percent') or 0.0, reverse=True)[:5]
    top_ram = sorted(procs, key=lambda x: x.get('memory_bytes') or 0, reverse=True)[:5]
    
    format_proc = lambda p: {
        "pid": p['pid'],
        "name": p['name'] or "Inconnu",
        "username": p['username'] or "root",
        "cpu_percent": round(p['cpu_percent'] or 0.0, 1),
        "ram_percent": round(p['memory_percent'] or 0.0, 1),
        "ram_bytes": p['memory_bytes']
    }
    
    return {
        "top_cpu": [format_proc(p) for p in top_cpu],
        "top_ram": [format_proc(p) for p in top_ram]
    }

def get_battery_info() -> Optional[dict]:
    """Récupère l'état de la batterie si disponible."""
    try:
        batt = psutil.sensors_battery()
        if batt:
            return {
                "percent": round(batt.percent, 1),
                "power_plugged": batt.power_plugged,
                "secs_left": batt.secsleft if batt.secsleft != psutil.POWER_TIME_UNLIMITED and batt.secsleft != psutil.POWER_TIME_UNKNOWN else None
            }
    except Exception:
        pass
    return None

def collect_metrics_data() -> dict:
    """Collecte l'ensemble des données système en temps réel."""
    global prev_disk_io, prev_net_io, prev_io_time, speeds
    
    now = time.time()
    dt = now - prev_io_time
    if dt <= 0:
        dt = 1.0
        
    # CPU
    cpu_usage = psutil.cpu_percent(interval=None)
    cpu_cores = psutil.cpu_percent(interval=None, percpu=True)
    cpu_count = len(cpu_cores)
    
    # RAM
    ram = psutil.virtual_memory()
    
    # Load Avg
    try:
        load_avg = list(os.getloadavg())
    except Exception:
        load_avg = [0.0, 0.0, 0.0]
        
    # Disques
    disks = []
    for part in psutil.disk_partitions(all=False):
        if "squashfs" in part.fstype or "tmpfs" in part.fstype or "devtmpfs" in part.fstype:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append({
                "device": part.device,
                "mountpoint": part.mountpoint,
                "fstype": part.fstype,
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "percent": usage.percent
            })
        except Exception:
            continue
            
    # Débits Disk I/O
    current_disk_io = psutil.disk_io_counters()
    if prev_disk_io and current_disk_io:
        speeds["disk_read"] = max(0.0, (current_disk_io.read_bytes - prev_disk_io.read_bytes) / dt)
        speeds["disk_write"] = max(0.0, (current_disk_io.write_bytes - prev_disk_io.write_bytes) / dt)
    prev_disk_io = current_disk_io
    
    # Débits Réseau I/O
    current_net_io = psutil.net_io_counters()
    if prev_net_io and current_net_io:
        speeds["net_sent"] = max(0.0, (current_net_io.bytes_sent - prev_net_io.bytes_sent) / dt)
        speeds["net_recv"] = max(0.0, (current_net_io.bytes_recv - prev_net_io.bytes_recv) / dt)
    prev_net_io = current_net_io
    
    prev_io_time = now
    
    # Températures & Batterie
    cpu_temp = get_cpu_temp()
    gpu_temp = get_gpu_temp()
    battery = get_battery_info()
    top_processes = get_top_processes()
    
    metrics = {
        "timestamp": now,
        "system": SYSTEM_INFO,
        "load_avg": load_avg,
        "battery": battery,
        "cpu": {
            "usage": cpu_usage,
            "cores": cpu_cores,
            "count": cpu_count
        },
        "ram": {
            "total": ram.total,
            "used": ram.used,
            "free": ram.available,
            "percent": ram.percent
        },
        "disks": disks,
        "disk_io": {
            "read_speed": speeds["disk_read"],
            "write_speed": speeds["disk_write"]
        },
        "net_io": {
            "bytes_sent_speed": speeds["net_sent"],
            "bytes_recv_speed": speeds["net_recv"]
        },
        "temperatures": {
            "cpu": cpu_temp,
            "gpu": gpu_temp
        },
        "processes": top_processes,
        "settings": settings
    }
    
    # Store point in 1-hour history buffer
    history_buffer.append({
        "timestamp": now,
        "cpu": cpu_usage,
        "ram": ram.percent,
        "temp": cpu_temp if cpu_temp is not None else 0.0
    })
    
    return metrics

async def metrics_collector_loop():
    """Collecte les métriques toutes les secondes et les distribue en temps réel."""
    global current_metrics
    psutil.cpu_percent(interval=None)
    psutil.cpu_percent(interval=None, percpu=True)
    await asyncio.sleep(0.5)
    
    while True:
        try:
            current_metrics = collect_metrics_data()
            check_alerts(current_metrics)
            
            if active_connections:
                payload = json.dumps(current_metrics)
                await asyncio.gather(
                    *[client.send_text(payload) for client in active_connections],
                    return_exceptions=True
                )
        except Exception as e:
            print(f"[ERREUR] Boucle de collecte: {e}")
            
        await asyncio.sleep(1.0)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(metrics_collector_loop())

@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    """Sert le tableau de bord HTML principal."""
    try:
        html_path = os.path.join(BASE_DIR, "index.html")
        if os.path.exists(html_path):
            with open(html_path, "r", encoding="utf-8") as f:
                return HTMLResponse(content=f.read())
        return HTMLResponse(content="<h1>Fichier index.html introuvable</h1>", status_code=404)
    except Exception as e:
        return HTMLResponse(content=f"<h1>Erreur serveur</h1><p>{str(e)}</p>", status_code=500)

@app.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics():
    """Endpoint au format standard Prometheus."""
    if not current_metrics:
        return PlainTextResponse("# System metrics starting up...\n", status_code=503)
        
    lines = [
        "# HELP node_cpu_usage_percent Global CPU utilization percentage.",
        "# TYPE node_cpu_usage_percent gauge",
        f"node_cpu_usage_percent {current_metrics['cpu']['usage']:.2f}",
        
        "# HELP node_memory_usage_percent RAM utilization percentage.",
        "# TYPE node_memory_usage_percent gauge",
        f"node_memory_usage_percent {current_metrics['ram']['percent']:.2f}",
        f"node_memory_total_bytes {current_metrics['ram']['total']}",
        f"node_memory_used_bytes {current_metrics['ram']['used']}",
        
        "# HELP node_load_1m 1 minute load average.",
        "# TYPE node_load_1m gauge",
        f"node_load_1m {current_metrics['load_avg'][0]:.2f}",
        f"node_load_5m {current_metrics['load_avg'][1]:.2f}",
        f"node_load_15m {current_metrics['load_avg'][2]:.2f}",
    ]
    
    if current_metrics['temperatures']['cpu'] is not None:
        lines.extend([
            "# HELP node_cpu_temperature_celsius CPU Core Temperature in Celsius.",
            "# TYPE node_cpu_temperature_celsius gauge",
            f"node_cpu_temperature_celsius {current_metrics['temperatures']['cpu']:.2f}"
        ])
        
    lines.extend([
        "# HELP node_disk_read_bytes_per_sec Disk read speed in bytes per sec.",
        "# TYPE node_disk_read_bytes_per_sec gauge",
        f"node_disk_read_bytes_per_sec {current_metrics['disk_io']['read_speed']:.2f}",
        f"node_disk_write_bytes_per_sec {current_metrics['disk_io']['write_speed']:.2f}",
        
        "# HELP node_network_receive_bytes_per_sec Network download speed in bytes per sec.",
        "# TYPE node_network_receive_bytes_per_sec gauge",
        f"node_network_receive_bytes_per_sec {current_metrics['net_io']['bytes_recv_speed']:.2f}",
        f"node_network_transmit_bytes_per_sec {current_metrics['net_io']['bytes_sent_speed']:.2f}",
    ])
    
    return PlainTextResponse("\n".join(lines) + "\n")

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Gère la connexion WebSocket en temps réel et les commandes interactives."""
    await websocket.accept()
    active_connections.add(websocket)
    
    if current_metrics:
        try:
            await websocket.send_text(json.dumps(current_metrics))
        except Exception:
            pass
            
    try:
        while True:
            data_str = await websocket.receive_text()
            try:
                data = json.loads(data_str)
                action_type = data.get("type")
                
                if action_type == "toggle_notifications":
                    settings["notifications_enabled"] = bool(data.get("enabled", True))
                    save_settings(settings)
                    
                elif action_type == "update_settings":
                    new_settings = data.get("settings", {})
                    settings.update(new_settings)
                    save_settings(settings)
                    # Broadcaster les nouveaux paramètres à tous les clients
                    if current_metrics:
                        current_metrics["settings"] = settings
                        
                elif action_type == "kill_process":
                    pid = int(data.get("pid", 0))
                    result = {"type": "kill_result", "pid": pid, "success": False, "message": ""}
                    try:
                        p = psutil.Process(pid)
                        p.terminate()
                        result["success"] = True
                        result["message"] = f"Le processus {p.name()} (PID {pid}) a été arrêté avec succès."
                    except psutil.NoSuchProcess:
                        result["message"] = f"Le processus PID {pid} n'existe plus."
                    except psutil.AccessDenied:
                        result["message"] = f"Permission refusée pour arrêter le processus PID {pid}."
                    except Exception as ex:
                        result["message"] = f"Erreur lors de l'arrêt du processus: {ex}"
                    
                    await websocket.send_text(json.dumps(result))
                    
                elif action_type == "get_history":
                    # Envoyer l'historique complet disponible au client
                    hist_data = list(history_buffer)
                    await websocket.send_text(json.dumps({
                        "type": "history_response",
                        "history": hist_data
                    }))
                    
            except Exception as e:
                print(f"[ERREUR] Message WS invalide: {e}")
                
    except WebSocketDisconnect:
        active_connections.remove(websocket)
    except Exception:
        if websocket in active_connections:
            active_connections.remove(websocket)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
