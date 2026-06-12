# Ubuntu System Monitoring Dashboard

Un outil de monitoring autonome et léger pour votre PC sous Ubuntu. Il collecte les métriques matérielles critiques (CPU, RAM, Disques, Débits, Températures) et les affiche en temps réel sur un tableau de bord web local à l'aide de WebSockets et de Chart.js.

## Fonctionnalités

- **Extraction de métriques matérielles** : CPU globale et par cœur, RAM, espace disque et partitions réelles, débits disque (lecture/écriture), débits réseau (entrant/sortant) et températures physiques (CPU & GPU).
- **Tableau de bord temps réel** : Interface moderne "Glassmorphism" avec mode sombre et historique glissant de 60 secondes.
- **Alertes de bureau** : Notifications desktop via `notify-send` lorsque les seuils critiques sont dépassés (CPU > 90%, RAM > 95%, Température > 80°C).
- **Service persistant** : S'exécute de façon autonome au démarrage de la session à l'aide d'un service systemd utilisateur.
- **Sécurisé** : L'accès au serveur web est restreint à `localhost` (127.0.0.1:8000) par défaut.

## Prérequis

- **Ubuntu** (testé sur LTS).
- **Python 3.12+**.
- **uv** (gestionnaire de paquets ultra-rapide).
- **libnotify-bin** (pour l'affichage des notifications de bureau).

## Installation

Exécutez simplement le script d'installation automatique depuis le dossier racine du projet (sans utiliser `sudo` pour l'installation) :

```bash
chmod +x setup.sh
./setup.sh
```

Le script va créer l'environnement virtuel, installer les dépendances et activer le service systemd utilisateur.

## Administration du service

Puisqu'il s'agit d'un service systemd utilisateur, vous n'avez pas besoin des privilèges `sudo` pour le piloter :

### Voir le statut
```bash
systemctl --user status monitoring.service
```

### Consulter les logs en temps réel
```bash
journalctl --user -u monitoring.service -f
```

### Arrêter ou Redémarrer
```bash
systemctl --user stop monitoring.service
systemctl --user restart monitoring.service
```

## Licence

Ce projet est sous licence MIT.
