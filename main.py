import asyncio
import json
import os
import subprocess
import time
from typing import Dict, List, Optional, Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import psutil

app = FastAPI(title="Ubuntu Monitoring Service")

# Dossier de base pour localiser index.html
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")

def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"notifications_enabled": True}

def save_settings(data: dict):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[ERREUR] Échec de la sauvegarde des paramètres: {e}")

settings = load_settings()

# Configuration des seuils d'alerte par défaut
THRESHOLDS = {
    "cpu_usage": 90.0,       # en %
    "ram_usage": 95.0,       # en %
    "cpu_temp": 80.0,        # en °C
}

# Cooldown des notifications (en secondes) pour éviter le spam (5 minutes)
NOTIFICATION_COOLDOWN = 300
last_notified: Dict[str, float] = {
    "cpu_usage": 0.0,
    "ram_usage": 0.0,
    "cpu_temp": 0.0,
}

# Variables globales pour calculer les débits I/O (Stockage & Réseau)
prev_disk_io: Optional[tuple] = None
prev_net_io: Optional[tuple] = None
prev_io_time: float = time.time()

# Débits actuels calculés (octets/s)
speeds = {
    "disk_read": 0.0,
    "disk_write": 0.0,
    "net_sent": 0.0,
    "net_recv": 0.0,
}

# Stockage de l'état actuel des métriques
current_metrics: dict = {}

# Liste des clients WebSocket actifs
active_connections: Set[WebSocket] = set()

def get_cpu_temp() -> Optional[float]:
    """Récupère la température CPU actuelle."""
    temps = psutil.sensors_temperatures()
    if not temps:
        return None
    
    # Recherche dans 'coretemp' (Intel) ou 'k10temp' / 'tctl' (AMD)
    for name in ["coretemp", "k10temp", "zenpower"]:
        if name in temps:
            # Essayer de trouver la température globale ("Package id 0" ou "Tctl")
            for entry in temps[name]:
                if entry.label in ["Package id 0", "Tctl"]:
                    return entry.current
            # Repli sur le max des cœurs si non trouvé
            if temps[name]:
                return max(entry.current for entry in temps[name])
                
    # Repli sur le premier capteur de température disponible si spécifique introuvable
    for name, entries in temps.items():
        if entries:
            return max(entry.current for entry in entries)
            
    return None

def get_gpu_temp() -> Optional[float]:
    """Récupère la température GPU actuelle (AMD ou Nvidia)."""
    temps = psutil.sensors_temperatures()
    if not temps:
        return None
    
    for name in ["amdgpu", "nouveau"]:
        if name in temps:
            for entry in temps[name]:
                if entry.label == "edge" or not entry.label:
                    return entry.current
                    
    # Nvidia fallback via nvidia-smi si dispo
    try:
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

def send_desktop_notification(title: str, message: str, urgency: str = "normal"):
    """
    Envoie une notification de bureau en ciblant la session DBus de l'utilisateur actif.
    Ceci est nécessaire car le service s'exécute en arrière-plan.
    """
    env = os.environ.copy()
    user_uid = os.getenv("USER_UID", "1000")
    
    # Définition des variables graphiques pour notify-send
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
    """Vérifie si les seuils de métriques sont dépassés et gère l'envoi d'alertes."""
    if not settings.get("notifications_enabled", True):
        return
        
    now = time.time()
    
    # 1. Alerte CPU
    cpu_percent = metrics["cpu"]["usage"]
    if cpu_percent > THRESHOLDS["cpu_usage"]:
        if now - last_notified["cpu_usage"] > NOTIFICATION_COOLDOWN:
            send_desktop_notification(
                "Alerte CPU Critique", 
                f"L'utilisation du CPU est de {cpu_percent}% (Seuil: {THRESHOLDS['cpu_usage']}%).",
                "critical"
            )
            last_notified["cpu_usage"] = now
            
    # 2. Alerte RAM
    ram_percent = metrics["ram"]["percent"]
    if ram_percent > THRESHOLDS["ram_usage"]:
        if now - last_notified["ram_usage"] > NOTIFICATION_COOLDOWN:
            send_desktop_notification(
                "Alerte RAM Critique", 
                f"L'utilisation de la mémoire RAM est de {ram_percent}% (Seuil: {THRESHOLDS['ram_usage']}%).",
                "critical"
            )
            last_notified["ram_usage"] = now
            
    # 3. Alerte Température CPU
    cpu_temp = metrics["temperatures"]["cpu"]
    if cpu_temp and cpu_temp > THRESHOLDS["cpu_temp"]:
        if now - last_notified["cpu_temp"] > NOTIFICATION_COOLDOWN:
            send_desktop_notification(
                "Alerte Température Critique", 
                f"La température du CPU atteint {cpu_temp}°C (Seuil: {THRESHOLDS['cpu_temp']}°C).",
                "critical"
            )
            last_notified["cpu_temp"] = now

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
    
    # Disques
    disks = []
    for part in psutil.disk_partitions(all=False):
        # Filtrer uniquement les disques physiques réels (ignorer squashfs de snap, tmpfs, etc.)
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
        except PermissionError:
            # Certaines partitions peuvent lever une exception de permission
            continue
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
    
    # Températures
    cpu_temp = get_cpu_temp()
    gpu_temp = get_gpu_temp()
    
    # Compilation
    metrics = {
        "timestamp": now,
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
        "thresholds": THRESHOLDS,
        "notifications_enabled": settings.get("notifications_enabled", True)
    }
    
    return metrics

async def metrics_collector_loop():
    """Tâche de fond qui collecte les métriques toutes les secondes et notifie les clients."""
    global current_metrics
    # Premier appel pour initialiser psutil.cpu_percent (qui nécessite un appel initial)
    psutil.cpu_percent(interval=None)
    psutil.cpu_percent(interval=None, percpu=True)
    await asyncio.sleep(0.5)
    
    while True:
        try:
            current_metrics = collect_metrics_data()
            check_alerts(current_metrics)
            
            # Diffuser aux WebSockets connectés
            if active_connections:
                payload = json.dumps(current_metrics)
                # Utiliser asyncio.gather pour diffuser à tous les clients en parallèle
                await asyncio.gather(
                    *[client.send_text(payload) for client in active_connections],
                    return_exceptions=True
                )
        except Exception as e:
            print(f"[ERREUR] Tâche de collecte des métriques: {e}")
            
        await asyncio.sleep(1.0)

@app.on_event("startup")
async def startup_event():
    # Démarre la boucle de collecte de métriques en arrière-plan
    asyncio.create_task(metrics_collector_loop())

@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    """Sert le tableau de bord HTML principal."""
    try:
        html_path = os.path.join(BASE_DIR, "index.html")
        if os.path.exists(html_path):
            with open(html_path, "r", encoding="utf-8") as f:
                return HTMLResponse(content=f.read())
        return HTMLResponse(
            content="<h1>Fichier index.html introuvable</h1><p>Veuillez exécuter l'installation correctement.</p>",
            status_code=404
        )
    except Exception as e:
        return HTMLResponse(content=f"<h1>Erreur serveur</h1><p>{str(e)}</p>", status_code=500)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Gère la connexion WebSocket en temps réel."""
    await websocket.accept()
    active_connections.add(websocket)
    
    # Envoyer immédiatement les dernières métriques dispo dès la connexion
    if current_metrics:
        try:
            await websocket.send_text(json.dumps(current_metrics))
        except Exception:
            pass
            
    try:
        while True:
            # Garder la connexion ouverte, écouter d'éventuels messages du client
            data_str = await websocket.receive_text()
            try:
                data = json.loads(data_str)
                if data.get("type") == "toggle_notifications":
                    enabled = bool(data.get("enabled", True))
                    settings["notifications_enabled"] = enabled
                    save_settings(settings)
            except Exception as e:
                print(f"[ERREUR] Message WS invalide: {e}")
    except WebSocketDisconnect:
        active_connections.remove(websocket)
    except Exception:
        if websocket in active_connections:
            active_connections.remove(websocket)

if __name__ == "__main__":
    import uvicorn
    # Sécurité : localhost par défaut
    uvicorn.run(app, host="127.0.0.1", port=8000)
