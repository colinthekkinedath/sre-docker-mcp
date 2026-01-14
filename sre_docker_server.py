"""
SRE Docker Management MCP Server
Advanced Docker orchestration and monitoring for SRE operations
"""

import asyncio
import json
import os
import re
import sqlite3
import subprocess
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import docker
import mcp.server.stdio
from docker.errors import APIError, DockerException, NotFound
from mcp.server import Server
from mcp.types import (
    GetPromptResult,
    Prompt,
    PromptMessage,
    Resource,
    TextContent,
    Tool,
)


@dataclass
class ContainerHealth:
    """Container health metrics"""

    container_id: str
    name: str
    status: str
    cpu_percent: float
    memory_usage_mb: float
    memory_limit_mb: float
    network_rx_bytes: int
    network_tx_bytes: int
    block_read_bytes: int
    block_write_bytes: int
    timestamp: str


@dataclass
class Incident:
    """SRE incident tracking"""

    id: str
    severity: str  # critical, high, medium, low
    title: str
    description: str
    affected_containers: List[str]
    status: str  # open, investigating, resolved
    created_at: str
    resolved_at: Optional[str]
    resolution_notes: Optional[str]


class SREDockerManager:
    """Core SRE Docker management system"""

    def __init__(self, db_path: str = None):
        # Use script directory for database if no path provided
        if db_path is None:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            db_path = os.path.join(script_dir, "sre_docker.db")

        self.db_path = db_path

        # Ensure directory exists
        os.makedirs(
            os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True
        )

        self.client = docker.from_env()
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

        # Thresholds for alerting
        self.thresholds = {
            "cpu_percent": 80.0,
            "memory_percent": 85.0,
            "restart_count": 5,
            "disk_io_threshold": 100_000_000,  # 100MB/s
        }

    def _init_db(self):
        """Initialize database schema"""
        schema = """
        CREATE TABLE IF NOT EXISTS health_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            container_id TEXT NOT NULL,
            container_name TEXT NOT NULL,
            cpu_percent REAL,
            memory_usage_mb REAL,
            memory_limit_mb REAL,
            network_rx_bytes INTEGER,
            network_tx_bytes INTEGER,
            block_read_bytes INTEGER,
            block_write_bytes INTEGER,
            timestamp TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS incidents (
            id TEXT PRIMARY KEY,
            severity TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            affected_containers TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            resolution_notes TEXT
        );

        CREATE TABLE IF NOT EXISTS deployment_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            container_name TEXT NOT NULL,
            image TEXT NOT NULL,
            action TEXT NOT NULL,
            status TEXT NOT NULL,
            details TEXT,
            timestamp TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS runbooks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            scenario TEXT NOT NULL,
            steps TEXT NOT NULL,
            tags TEXT,
            created_at TEXT NOT NULL,
            last_used TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_metrics_timestamp ON health_metrics(timestamp);
        CREATE INDEX IF NOT EXISTS idx_metrics_container ON health_metrics(container_id);
        CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);
        """
        self.conn.executescript(schema)
        self.conn.commit()

    # ==================== Container Management ====================

    def list_containers(self, all_containers: bool = False) -> List[Dict]:
        """List all containers with detailed information"""
        try:
            containers = self.client.containers.list(all=all_containers)
            result = []

            for container in containers:
                stats = self._get_container_stats(container)
                info = {
                    "id": container.id[:12],
                    "name": container.name,
                    "image": container.image.tags[0]
                    if container.image.tags
                    else container.image.id[:12],
                    "status": container.status,
                    "created": container.attrs["Created"],
                    "ports": container.ports,
                    "labels": container.labels,
                    "restart_count": container.attrs["RestartCount"],
                    "stats": stats,
                }
                result.append(info)

            return result
        except DockerException as e:
            return [{"error": str(e)}]

    def get_container_details(self, container_name: str) -> Dict:
        """Get detailed information about a specific container"""
        try:
            container = self.client.containers.get(container_name)

            # Get logs
            logs = container.logs(tail=100).decode("utf-8", errors="ignore")

            # Get stats
            stats = self._get_container_stats(container)

            # Get network info
            networks = {}
            for net_name, net_config in container.attrs["NetworkSettings"][
                "Networks"
            ].items():
                networks[net_name] = {
                    "ip_address": net_config.get("IPAddress"),
                    "gateway": net_config.get("Gateway"),
                    "mac_address": net_config.get("MacAddress"),
                }

            return {
                "id": container.id,
                "name": container.name,
                "image": container.image.tags[0]
                if container.image.tags
                else container.image.id,
                "status": container.status,
                "created": container.attrs["Created"],
                "started_at": container.attrs["State"]["StartedAt"],
                "finished_at": container.attrs["State"].get("FinishedAt"),
                "restart_count": container.attrs["RestartCount"],
                "exit_code": container.attrs["State"].get("ExitCode"),
                "pid": container.attrs["State"].get("Pid"),
                "ports": container.ports,
                "mounts": [
                    {
                        "source": m["Source"],
                        "destination": m["Destination"],
                        "mode": m["Mode"],
                        "rw": m["RW"],
                    }
                    for m in container.attrs.get("Mounts", [])
                ],
                "networks": networks,
                "env": container.attrs["Config"]["Env"],
                "labels": container.labels,
                "stats": stats,
                "recent_logs": logs,
            }
        except NotFound:
            return {"error": f"Container '{container_name}' not found"}
        except DockerException as e:
            return {"error": str(e)}

    def _get_container_stats(self, container) -> Dict:
        """Get real-time stats for a container"""
        try:
            stats = container.stats(stream=False)

            # Calculate CPU percentage
            cpu_delta = (
                stats["cpu_stats"]["cpu_usage"]["total_usage"]
                - stats["precpu_stats"]["cpu_usage"]["total_usage"]
            )
            system_delta = (
                stats["cpu_stats"]["system_cpu_usage"]
                - stats["precpu_stats"]["system_cpu_usage"]
            )
            cpu_percent = (
                (cpu_delta / system_delta)
                * len(stats["cpu_stats"]["cpu_usage"]["percpu_usage"])
                * 100.0
                if system_delta > 0
                else 0.0
            )

            # Memory stats
            memory_usage = stats["memory_stats"]["usage"]
            memory_limit = stats["memory_stats"]["limit"]
            memory_percent = (
                (memory_usage / memory_limit) * 100.0 if memory_limit > 0 else 0.0
            )

            # Network stats
            networks = stats.get("networks", {})
            total_rx = sum(net["rx_bytes"] for net in networks.values())
            total_tx = sum(net["tx_bytes"] for net in networks.values())

            # Block I/O
            blkio = stats.get("blkio_stats", {}).get("io_service_bytes_recursive", [])
            total_read = sum(item["value"] for item in blkio if item["op"] == "Read")
            total_write = sum(item["value"] for item in blkio if item["op"] == "Write")

            return {
                "cpu_percent": round(cpu_percent, 2),
                "memory_usage_mb": round(memory_usage / 1024 / 1024, 2),
                "memory_limit_mb": round(memory_limit / 1024 / 1024, 2),
                "memory_percent": round(memory_percent, 2),
                "network_rx_mb": round(total_rx / 1024 / 1024, 2),
                "network_tx_mb": round(total_tx / 1024 / 1024, 2),
                "block_read_mb": round(total_read / 1024 / 1024, 2),
                "block_write_mb": round(total_write / 1024 / 1024, 2),
            }
        except Exception as e:
            return {"error": f"Failed to get stats: {str(e)}"}

    # ==================== Health Monitoring ====================

    def collect_health_metrics(self) -> List[ContainerHealth]:
        """Collect health metrics from all running containers"""
        containers = self.client.containers.list()
        metrics = []
        timestamp = datetime.now().isoformat()

        for container in containers:
            try:
                stats = self._get_container_stats(container)
                if "error" not in stats:
                    metric = ContainerHealth(
                        container_id=container.id[:12],
                        name=container.name,
                        status=container.status,
                        cpu_percent=stats["cpu_percent"],
                        memory_usage_mb=stats["memory_usage_mb"],
                        memory_limit_mb=stats["memory_limit_mb"],
                        network_rx_bytes=int(stats["network_rx_mb"] * 1024 * 1024),
                        network_tx_bytes=int(stats["network_tx_mb"] * 1024 * 1024),
                        block_read_bytes=int(stats["block_read_mb"] * 1024 * 1024),
                        block_write_bytes=int(stats["block_write_mb"] * 1024 * 1024),
                        timestamp=timestamp,
                    )
                    metrics.append(metric)
                    self._store_health_metric(metric)
            except Exception as e:
                print(f"Error collecting metrics for {container.name}: {e}")

        return metrics

    def _store_health_metric(self, metric: ContainerHealth):
        """Store health metric in database"""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO health_metrics
            (container_id, container_name, cpu_percent, memory_usage_mb,
             memory_limit_mb, network_rx_bytes, network_tx_bytes,
             block_read_bytes, block_write_bytes, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                metric.container_id,
                metric.name,
                metric.cpu_percent,
                metric.memory_usage_mb,
                metric.memory_limit_mb,
                metric.network_rx_bytes,
                metric.network_tx_bytes,
                metric.block_read_bytes,
                metric.block_write_bytes,
                metric.timestamp,
            ),
        )
        self.conn.commit()

    def analyze_health(self) -> Dict:
        """Analyze container health and detect issues"""
        metrics = self.collect_health_metrics()
        issues = []

        for metric in metrics:
            container_issues = []

            # Check CPU
            if metric.cpu_percent > self.thresholds["cpu_percent"]:
                container_issues.append(
                    {
                        "type": "high_cpu",
                        "severity": "high" if metric.cpu_percent > 95 else "medium",
                        "message": f"CPU usage at {metric.cpu_percent}%",
                        "threshold": self.thresholds["cpu_percent"],
                    }
                )

            # Check memory
            memory_percent = (metric.memory_usage_mb / metric.memory_limit_mb) * 100
            if memory_percent > self.thresholds["memory_percent"]:
                container_issues.append(
                    {
                        "type": "high_memory",
                        "severity": "high" if memory_percent > 95 else "medium",
                        "message": f"Memory usage at {memory_percent:.1f}%",
                        "threshold": self.thresholds["memory_percent"],
                    }
                )

            # Check restart count
            try:
                container = self.client.containers.get(metric.container_id)
                restart_count = container.attrs["RestartCount"]
                if restart_count >= self.thresholds["restart_count"]:
                    container_issues.append(
                        {
                            "type": "frequent_restarts",
                            "severity": "critical",
                            "message": f"Container has restarted {restart_count} times",
                            "threshold": self.thresholds["restart_count"],
                        }
                    )
            except:
                pass

            if container_issues:
                issues.append(
                    {
                        "container_id": metric.container_id,
                        "container_name": metric.name,
                        "issues": container_issues,
                        "timestamp": metric.timestamp,
                    }
                )

        return {
            "total_containers": len(metrics),
            "containers_with_issues": len(issues),
            "issues": issues,
            "analyzed_at": datetime.now().isoformat(),
        }

    def get_metrics_history(self, container_name: str, hours: int = 24) -> List[Dict]:
        """Get historical metrics for a container"""
        cursor = self.conn.cursor()
        since = (datetime.now() - timedelta(hours=hours)).isoformat()

        cursor.execute(
            """
            SELECT * FROM health_metrics
            WHERE container_name = ? AND timestamp > ?
            ORDER BY timestamp DESC
        """,
            (container_name, since),
        )

        return [dict(row) for row in cursor.fetchall()]

    # ==================== Deployment Operations ====================

    def deploy_container(
        self,
        image: str,
        name: str,
        ports: Dict[str, int] = None,
        environment: Dict[str, str] = None,
        volumes: Dict[str, Dict] = None,
        restart_policy: str = "unless-stopped",
    ) -> Dict:
        """Deploy a new container"""
        try:
            # Check if container with this name exists
            try:
                existing = self.client.containers.get(name)
                return {
                    "error": f"Container '{name}' already exists",
                    "existing_id": existing.id,
                }
            except NotFound:
                pass

            # Pull image if needed
            try:
                self.client.images.get(image)
            except NotFound:
                print(f"Pulling image {image}...")
                self.client.images.pull(image)

            # Create and start container
            container = self.client.containers.run(
                image=image,
                name=name,
                ports=ports,
                environment=environment,
                volumes=volumes,
                restart_policy={"Name": restart_policy},
                detach=True,
            )

            # Log deployment
            self._log_deployment(
                name, image, "deploy", "success", f"Container deployed successfully"
            )

            return {
                "status": "success",
                "container_id": container.id,
                "name": name,
                "image": image,
                "message": "Container deployed successfully",
            }
        except DockerException as e:
            self._log_deployment(name, image, "deploy", "failed", str(e))
            return {"error": str(e)}

    def rolling_update(self, container_name: str, new_image: str) -> Dict:
        """Perform a rolling update of a container"""
        try:
            old_container = self.client.containers.get(container_name)
            old_config = old_container.attrs

            # Get old container config
            ports = old_container.ports
            env = old_config["Config"]["Env"]
            volumes = {
                m["Source"]: {"bind": m["Destination"], "mode": m["Mode"]}
                for m in old_config.get("Mounts", [])
            }
            restart_policy = old_config["HostConfig"]["RestartPolicy"]["Name"]

            # Stop old container
            print(f"Stopping old container {container_name}...")
            old_container.stop(timeout=30)

            # Rename old container
            backup_name = (
                f"{container_name}_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            )
            old_container.rename(backup_name)

            # Deploy new container
            result = self.deploy_container(
                image=new_image,
                name=container_name,
                ports=ports,
                environment={k.split("=")[0]: k.split("=")[1] for k in env if "=" in k},
                volumes=volumes,
                restart_policy=restart_policy,
            )

            if "error" not in result:
                # Remove backup after successful deployment
                old_container.remove()
                self._log_deployment(
                    container_name,
                    new_image,
                    "rolling_update",
                    "success",
                    "Rolling update completed",
                )
                return {
                    "status": "success",
                    "message": "Rolling update completed successfully",
                    "new_container_id": result["container_id"],
                    "old_container_removed": True,
                }
            else:
                # Rollback on failure
                old_container.rename(container_name)
                old_container.start()
                return {
                    "status": "rolled_back",
                    "error": result["error"],
                    "message": "Update failed, rolled back to previous version",
                }
        except Exception as e:
            return {"error": str(e)}

    def _log_deployment(
        self, container_name: str, image: str, action: str, status: str, details: str
    ):
        """Log deployment action"""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO deployment_history
            (container_name, image, action, status, details, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (
                container_name,
                image,
                action,
                status,
                details,
                datetime.now().isoformat(),
            ),
        )
        self.conn.commit()

    def get_deployment_history(
        self, container_name: str = None, limit: int = 50
    ) -> List[Dict]:
        """Get deployment history"""
        cursor = self.conn.cursor()
        if container_name:
            cursor.execute(
                """
                SELECT * FROM deployment_history
                WHERE container_name = ?
                ORDER BY timestamp DESC LIMIT ?
            """,
                (container_name, limit),
            )
        else:
            cursor.execute(
                """
                SELECT * FROM deployment_history
                ORDER BY timestamp DESC LIMIT ?
            """,
                (limit,),
            )

        return [dict(row) for row in cursor.fetchall()]

    # ==================== Incident Management ====================

    def create_incident(
        self,
        severity: str,
        title: str,
        description: str,
        affected_containers: List[str],
    ) -> str:
        """Create a new incident"""
        incident_id = f"INC-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        cursor = self.conn.cursor()

        cursor.execute(
            """
            INSERT INTO incidents
            (id, severity, title, description, affected_containers, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
            (
                incident_id,
                severity,
                title,
                description,
                json.dumps(affected_containers),
                "open",
                datetime.now().isoformat(),
            ),
        )

        self.conn.commit()
        return incident_id

    def update_incident(
        self, incident_id: str, status: str, resolution_notes: str = None
    ) -> Dict:
        """Update incident status"""
        cursor = self.conn.cursor()

        if status == "resolved":
            cursor.execute(
                """
                UPDATE incidents
                SET status = ?, resolved_at = ?, resolution_notes = ?
                WHERE id = ?
            """,
                (status, datetime.now().isoformat(), resolution_notes, incident_id),
            )
        else:
            cursor.execute(
                """
                UPDATE incidents SET status = ? WHERE id = ?
            """,
                (status, incident_id),
            )

        self.conn.commit()
        return {"status": "updated", "incident_id": incident_id}

    def list_incidents(self, status: str = None) -> List[Dict]:
        """List incidents"""
        cursor = self.conn.cursor()

        if status:
            cursor.execute(
                """
                SELECT * FROM incidents WHERE status = ? ORDER BY created_at DESC
            """,
                (status,),
            )
        else:
            cursor.execute("""
                SELECT * FROM incidents ORDER BY created_at DESC
            """)

        incidents = []
        for row in cursor.fetchall():
            incident = dict(row)
            incident["affected_containers"] = json.loads(
                incident["affected_containers"]
            )
            incidents.append(incident)

        return incidents

    # ==================== Docker Compose Operations ====================

    def compose_up(self, compose_file: str, project_name: str = None) -> Dict:
        """Start services defined in docker-compose file"""
        try:
            cmd = ["docker-compose", "-f", compose_file]
            if project_name:
                cmd.extend(["-p", project_name])
            cmd.append("up")
            cmd.extend(["-d", "--remove-orphans"])

            result = subprocess.run(cmd, capture_output=True, text=True)

            return {
                "status": "success" if result.returncode == 0 else "failed",
                "stdout": result.stdout,
                "stderr": result.stderr,
                "return_code": result.returncode,
            }
        except Exception as e:
            return {"error": str(e)}

    def compose_down(
        self, compose_file: str, project_name: str = None, remove_volumes: bool = False
    ) -> Dict:
        """Stop services defined in docker-compose file"""
        try:
            cmd = ["docker-compose", "-f", compose_file]
            if project_name:
                cmd.extend(["-p", project_name])
            cmd.append("down")
            if remove_volumes:
                cmd.append("-v")

            result = subprocess.run(cmd, capture_output=True, text=True)

            return {
                "status": "success" if result.returncode == 0 else "failed",
                "stdout": result.stdout,
                "stderr": result.stderr,
                "return_code": result.returncode,
            }
        except Exception as e:
            return {"error": str(e)}

    # ==================== System-wide Operations ====================

    def system_prune(self, volumes: bool = False) -> Dict:
        """Clean up unused Docker resources"""
        try:
            result = {
                "images_deleted": [],
                "containers_removed": [],
                "space_reclaimed": 0,
            }

            # Prune containers
            prune_result = self.client.containers.prune()
            result["containers_removed"] = prune_result.get("ContainersDeleted", [])
            result["space_reclaimed"] += prune_result.get("SpaceReclaimed", 0)

            # Prune images
            prune_result = self.client.images.prune(filters={"dangling": False})
            result["images_deleted"] = prune_result.get("ImagesDeleted", [])
            result["space_reclaimed"] += prune_result.get("SpaceReclaimed", 0)

            # Prune networks
            self.client.networks.prune()

            # Prune volumes if requested
            if volumes:
                prune_result = self.client.volumes.prune()
                result["space_reclaimed"] += prune_result.get("SpaceReclaimed", 0)

            result["space_reclaimed_mb"] = round(
                result["space_reclaimed"] / 1024 / 1024, 2
            )

            return result
        except DockerException as e:
            return {"error": str(e)}

    def get_system_info(self) -> Dict:
        """Get Docker system information"""
        try:
            info = self.client.info()
            return {
                "containers_total": info["Containers"],
                "containers_running": info["ContainersRunning"],
                "containers_paused": info["ContainersPaused"],
                "containers_stopped": info["ContainersStopped"],
                "images": info["Images"],
                "server_version": info["ServerVersion"],
                "operating_system": info["OperatingSystem"],
                "total_memory_gb": round(info["MemTotal"] / 1024 / 1024 / 1024, 2),
                "cpu_count": info["NCPU"],
                "docker_root_dir": info["DockerRootDir"],
                "driver": info["Driver"],
                "swarm": info.get("Swarm", {}).get("LocalNodeState") != "inactive",
            }
        except DockerException as e:
            return {"error": str(e)}


# ==================== MCP Server Setup ====================

app = Server("sre-docker-server")
manager = SREDockerManager()


@app.list_resources()
async def list_resources() -> list[Resource]:
    """List available Docker resources"""
    return [
        Resource(
            uri="docker://containers",
            name="Running Containers",
            mimeType="application/json",
            description="List of all running Docker containers",
        ),
        Resource(
            uri="docker://system/info",
            name="Docker System Info",
            mimeType="application/json",
            description="Docker system information and statistics",
        ),
        Resource(
            uri="docker://health/analysis",
            name="Health Analysis",
            mimeType="application/json",
            description="Current health analysis of all containers",
        ),
        Resource(
            uri="docker://incidents/open",
            name="Open Incidents",
            mimeType="application/json",
            description="List of open SRE incidents",
        ),
    ]


@app.read_resource()
async def read_resource(uri: str) -> str:
    """Read Docker resource data"""
    if uri == "docker://containers":
        containers = manager.list_containers()
        return json.dumps(containers, indent=2)

    elif uri == "docker://system/info":
        info = manager.get_system_info()
        return json.dumps(info, indent=2)

    elif uri == "docker://health/analysis":
        analysis = manager.analyze_health()
        return json.dumps(analysis, indent=2)

    elif uri == "docker://incidents/open":
        incidents = manager.list_incidents(status="open")
        return json.dumps(incidents, indent=2)

    else:
        return json.dumps({"error": "Unknown resource"})


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available SRE Docker tools"""
    return [
        Tool(
            name="list_containers",
            description="List all Docker containers with their status and stats",
            inputSchema={
                "type": "object",
                "properties": {
                    "all": {
                        "type": "boolean",
                        "description": "Include stopped containers",
                        "default": False,
                    }
                },
            },
        ),
        Tool(
            name="get_container_details",
            description="Get detailed information about a specific container including logs, stats, and configuration",
            inputSchema={
                "type": "object",
                "properties": {
                    "container_name": {
                        "type": "string",
                        "description": "Name or ID of the container",
                    }
                },
                "required": ["container_name"],
            },
        ),
        Tool(
            name="analyze_health",
            description="Analyze health of all containers and detect issues based on thresholds",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_metrics_history",
            description="Get historical health metrics for a container",
            inputSchema={
                "type": "object",
                "properties": {
                    "container_name": {
                        "type": "string",
                        "description": "Name of the container",
                    },
                    "hours": {
                        "type": "integer",
                        "description": "Number of hours of history to retrieve",
                        "default": 24,
                    },
                },
                "required": ["container_name"],
            },
        ),
        Tool(
            name="restart_container",
            description="Restart a Docker container",
            inputSchema={
                "type": "object",
                "properties": {
                    "container_name": {
                        "type": "string",
                        "description": "Name or ID of the container to restart",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds before force kill",
                        "default": 10,
                    },
                },
                "required": ["container_name"],
            },
        ),
        Tool(
            name="stop_container",
            description="Stop a running Docker container",
            inputSchema={
                "type": "object",
                "properties": {
                    "container_name": {
                        "type": "string",
                        "description": "Name or ID of the container",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds",
                        "default": 10,
                    },
                },
                "required": ["container_name"],
            },
        ),
        Tool(
            name="start_container",
            description="Start a stopped Docker container",
            inputSchema={
                "type": "object",
                "properties": {
                    "container_name": {
                        "type": "string",
                        "description": "Name or ID of the container",
                    }
                },
                "required": ["container_name"],
            },
        ),
        Tool(
            name="deploy_container",
            description="Deploy a new Docker container with specified configuration",
            inputSchema={
                "type": "object",
                "properties": {
                    "image": {"type": "string", "description": "Docker image to use"},
                    "name": {"type": "string", "description": "Name for the container"},
                    "ports": {
                        "type": "object",
                        "description": "Port mappings (container_port: host_port)",
                    },
                    "environment": {
                        "type": "object",
                        "description": "Environment variables",
                    },
                    "volumes": {"type": "object", "description": "Volume mappings"},
                    "restart_policy": {
                        "type": "string",
                        "description": "Restart policy (no, always, on-failure, unless-stopped)",
                        "default": "unless-stopped",
                    },
                },
                "required": ["image", "name"],
            },
        ),
        Tool(
            name="rolling_update",
            description="Perform a rolling update of a container to a new image version with automatic rollback on failure",
            inputSchema={
                "type": "object",
                "properties": {
                    "container_name": {
                        "type": "string",
                        "description": "Name of the container to update",
                    },
                    "new_image": {
                        "type": "string",
                        "description": "New Docker image to deploy",
                    },
                },
                "required": ["container_name", "new_image"],
            },
        ),
        Tool(
            name="get_deployment_history",
            description="Get deployment history for containers",
            inputSchema={
                "type": "object",
                "properties": {
                    "container_name": {
                        "type": "string",
                        "description": "Filter by container name (optional)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of records to return",
                        "default": 50,
                    },
                },
            },
        ),
        Tool(
            name="create_incident",
            description="Create a new SRE incident for tracking",
            inputSchema={
                "type": "object",
                "properties": {
                    "severity": {
                        "type": "string",
                        "description": "Incident severity (critical, high, medium, low)",
                        "enum": ["critical", "high", "medium", "low"],
                    },
                    "title": {"type": "string", "description": "Incident title"},
                    "description": {
                        "type": "string",
                        "description": "Detailed description",
                    },
                    "affected_containers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of affected container names",
                    },
                },
                "required": ["severity", "title", "description", "affected_containers"],
            },
        ),
        Tool(
            name="update_incident",
            description="Update an existing incident status",
            inputSchema={
                "type": "object",
                "properties": {
                    "incident_id": {
                        "type": "string",
                        "description": "Incident ID to update",
                    },
                    "status": {
                        "type": "string",
                        "description": "New status",
                        "enum": ["open", "investigating", "resolved"],
                    },
                    "resolution_notes": {
                        "type": "string",
                        "description": "Resolution notes (required if status is resolved)",
                    },
                },
                "required": ["incident_id", "status"],
            },
        ),
        Tool(
            name="list_incidents",
            description="List SRE incidents with optional status filter",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filter by status (optional)",
                        "enum": ["open", "investigating", "resolved"],
                    }
                },
            },
        ),
        Tool(
            name="compose_up",
            description="Start services defined in a docker-compose file",
            inputSchema={
                "type": "object",
                "properties": {
                    "compose_file": {
                        "type": "string",
                        "description": "Path to docker-compose.yml file",
                    },
                    "project_name": {
                        "type": "string",
                        "description": "Project name (optional)",
                    },
                },
                "required": ["compose_file"],
            },
        ),
        Tool(
            name="compose_down",
            description="Stop services defined in a docker-compose file",
            inputSchema={
                "type": "object",
                "properties": {
                    "compose_file": {
                        "type": "string",
                        "description": "Path to docker-compose.yml file",
                    },
                    "project_name": {
                        "type": "string",
                        "description": "Project name (optional)",
                    },
                    "remove_volumes": {
                        "type": "boolean",
                        "description": "Remove volumes",
                        "default": False,
                    },
                },
                "required": ["compose_file"],
            },
        ),
        Tool(
            name="system_prune",
            description="Clean up unused Docker resources (containers, images, networks)",
            inputSchema={
                "type": "object",
                "properties": {
                    "volumes": {
                        "type": "boolean",
                        "description": "Also prune volumes",
                        "default": False,
                    }
                },
            },
        ),
        Tool(
            name="get_system_info",
            description="Get Docker system information and resource usage",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_container_logs",
            description="Get logs from a container",
            inputSchema={
                "type": "object",
                "properties": {
                    "container_name": {
                        "type": "string",
                        "description": "Container name or ID",
                    },
                    "tail": {
                        "type": "integer",
                        "description": "Number of lines to retrieve from end",
                        "default": 100,
                    },
                    "follow": {
                        "type": "boolean",
                        "description": "Stream logs",
                        "default": False,
                    },
                },
                "required": ["container_name"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    """Execute SRE Docker tools"""

    if name == "list_containers":
        result = manager.list_containers(all_containers=arguments.get("all", False))
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "get_container_details":
        result = manager.get_container_details(arguments["container_name"])
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "analyze_health":
        result = manager.analyze_health()
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "get_metrics_history":
        result = manager.get_metrics_history(
            arguments["container_name"], arguments.get("hours", 24)
        )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "restart_container":
        try:
            container = manager.client.containers.get(arguments["container_name"])
            container.restart(timeout=arguments.get("timeout", 10))
            result = {
                "status": "success",
                "message": f"Container {arguments['container_name']} restarted",
            }
        except Exception as e:
            result = {"error": str(e)}
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "stop_container":
        try:
            container = manager.client.containers.get(arguments["container_name"])
            container.stop(timeout=arguments.get("timeout", 10))
            result = {
                "status": "success",
                "message": f"Container {arguments['container_name']} stopped",
            }
        except Exception as e:
            result = {"error": str(e)}
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "start_container":
        try:
            container = manager.client.containers.get(arguments["container_name"])
            container.start()
            result = {
                "status": "success",
                "message": f"Container {arguments['container_name']} started",
            }
        except Exception as e:
            result = {"error": str(e)}
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "deploy_container":
        result = manager.deploy_container(
            image=arguments["image"],
            name=arguments["name"],
            ports=arguments.get("ports"),
            environment=arguments.get("environment"),
            volumes=arguments.get("volumes"),
            restart_policy=arguments.get("restart_policy", "unless-stopped"),
        )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "rolling_update":
        result = manager.rolling_update(
            arguments["container_name"], arguments["new_image"]
        )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "get_deployment_history":
        result = manager.get_deployment_history(
            arguments.get("container_name"), arguments.get("limit", 50)
        )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "create_incident":
        incident_id = manager.create_incident(
            arguments["severity"],
            arguments["title"],
            arguments["description"],
            arguments["affected_containers"],
        )
        result = {"status": "created", "incident_id": incident_id}
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "update_incident":
        result = manager.update_incident(
            arguments["incident_id"],
            arguments["status"],
            arguments.get("resolution_notes"),
        )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "list_incidents":
        result = manager.list_incidents(arguments.get("status"))
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "compose_up":
        result = manager.compose_up(
            arguments["compose_file"], arguments.get("project_name")
        )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "compose_down":
        result = manager.compose_down(
            arguments["compose_file"],
            arguments.get("project_name"),
            arguments.get("remove_volumes", False),
        )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "system_prune":
        result = manager.system_prune(arguments.get("volumes", False))
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "get_system_info":
        result = manager.get_system_info()
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "get_container_logs":
        try:
            container = manager.client.containers.get(arguments["container_name"])
            logs = container.logs(
                tail=arguments.get("tail", 100), follow=arguments.get("follow", False)
            ).decode("utf-8", errors="ignore")
            result = {"logs": logs}
        except Exception as e:
            result = {"error": str(e)}
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    else:
        return [TextContent(type="text", text=json.dumps({"error": "Unknown tool"}))]


async def main():
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
