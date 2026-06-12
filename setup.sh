#!/bin/bash

# Codes couleurs pour l'affichage
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;36m'
NC='\033[0m' # No Color

echo -e "${BLUE}=== Ubuntu Monitoring Setup (User Service) ===${NC}\n"

# 1. Vérification de l'utilisateur courant (ne pas exécuter ce script en root)
if [ "$EUID" -eq 0 ]; then
    echo -e "${RED}[ERREUR] N'exécutez pas ce script en tant que root ou via sudo. Lancez-le en tant qu'utilisateur normal : ./setup.sh${NC}"
    exit 1
fi

CURRENT_USER=$(whoami)
CURRENT_DIR=$(pwd)

echo -e "${BLUE}[1/5] Vérification des prérequis système...${NC}"

# Vérification de notify-send (libnotify-bin)
if ! command -v notify-send &> /dev/null; then
    echo -e "${YELLOW}[INFO] notify-send n'est pas installé. Installation de libnotify-bin requise.${NC}"
    echo -e "Exécution de : sudo apt update && sudo apt install -y libnotify-bin"
    sudo apt update && sudo apt install -y libnotify-bin
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}[OK] libnotify-bin installé avec succès.${NC}"
    else
        echo -e "${RED}[ERREUR] Échec de l'installation de libnotify-bin. Les notifications bureau seront indisponibles.${NC}"
    fi
else
    echo -e "${GREEN}[OK] notify-send est déjà installé.${NC}"
fi

# Vérification des capteurs de température
echo -e "${BLUE}[2/5] Détection des capteurs de température matérielle...${NC}"
TEMP_SENSORS=$(python3 -c "
try:
    import psutil
    print(list(psutil.sensors_temperatures().keys()))
except Exception:
    import os
    print(os.listdir('/sys/class/hwmon/') if os.path.exists('/sys/class/hwmon/') else 'Aucun')
" 2>/dev/null)

echo -e "Capteurs détectés : ${YELLOW}${TEMP_SENSORS}${NC}"
if [[ "$TEMP_SENSORS" == *"Aucun"* || "$TEMP_SENSORS" == "[]" ]]; then
    echo -e "${YELLOW}[ATTENTION] Aucun capteur de température détecté.${NC}"
    echo -e "Pour les processeurs Intel, assurez-vous que le module 'coretemp' est chargé (sudo modprobe coretemp)."
    echo -e "Pour les processeurs AMD, chargez 'k10temp' ou 'zenpower'."
else
    echo -e "${GREEN}[OK] Capteurs thermiques actifs.${NC}"
fi

# 2. Création de l'environnement virtuel avec UV
echo -e "\n${BLUE}[3/5] Création de l'environnement virtuel Python...${NC}"
if ! command -v uv &> /dev/null; then
    echo -e "${RED}[ERREUR] L'outil de packaging 'uv' n'est pas détecté. Veuillez l'installer ou l'ajouter à votre PATH.${NC}"
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo -e "Création du dossier .venv..."
    uv venv .venv
    if [ $? -ne 0 ]; then
        echo -e "${RED}[ERREUR] Échec de la création de la venv avec uv.${NC}"
        exit 1
    fi
else
    echo -e "L'environnement .venv existe déjà."
fi

# 3. Installation des dépendances
echo -e "\n${BLUE}[4/5] Installation des dépendances (FastAPI, Uvicorn, psutil)...${NC}"
uv pip install --python .venv/bin/python fastapi "uvicorn[standard]" psutil
if [ $? -ne 0 ]; then
    echo -e "${RED}[ERREUR] Échec de l'installation des dépendances.${NC}"
    exit 1
fi
echo -e "${GREEN}[OK] Dépendances installées.${NC}"

# 4. Configuration du service systemd utilisateur (sans sudo)
echo -e "\n${BLUE}[5/5] Installation et activation du service systemd (utilisateur)...${NC}"
SERVICE_FILE="monitoring.service"

if [ ! -f "$SERVICE_FILE" ]; then
    echo -e "${RED}[ERREUR] Fichier $SERVICE_FILE introuvable dans le répertoire courant.${NC}"
    exit 1
fi

# Création du dossier systemd utilisateur s'il n'existe pas
mkdir -p "$HOME/.config/systemd/user"

echo -e "Copie du fichier de service dans ~/.config/systemd/user/..."
cp "$SERVICE_FILE" "$HOME/.config/systemd/user/monitoring.service"

echo -e "Rechargement du démon systemd utilisateur..."
systemctl --user daemon-reload

echo -e "Activation du service utilisateur..."
systemctl --user enable monitoring.service

echo -e "Activation du lingering pour que le service tourne au démarrage sans connexion requise..."
loginctl enable-linger

echo -e "Démarrage du service de monitoring utilisateur..."
systemctl --user restart monitoring.service

if [ $? -eq 0 ]; then
    echo -e "\n${GREEN}==================================================${NC}"
    echo -e "${GREEN}  INSTALLATION RÉUSSIE ET SERVICE ACTIF !        ${NC}"
    echo -e "${GREEN}==================================================${NC}"
    echo -e "Le serveur de monitoring est démarré sur : ${BLUE}http://localhost:8000${NC}"
    echo -e "Vous pouvez vérifier son statut avec : ${YELLOW}systemctl --user status monitoring.service${NC}"
    echo -e "Les logs sont consultables avec : ${YELLOW}journalctl --user -u monitoring.service -f${NC}"
else
    echo -e "${RED}[ERREUR] Le service n'a pas pu démarrer correctement. Vérifiez les logs avec journalctl --user -u monitoring.service${NC}"
fi
