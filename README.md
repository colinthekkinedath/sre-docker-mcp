# SRE Docker MCP Server

A comprehensive Model Context Protocol (MCP) server for Docker management with SRE capabilities.

## Features

- 🐳 Full Docker container lifecycle management
- 📊 Real-time health monitoring and metrics
- 🚀 Rolling deployments with automatic rollback
- 🚨 Incident management and tracking
- 📈 Historical metrics and deployment history
- 🔧 Docker Compose orchestration
- 🧹 System cleanup and optimization

## Prerequisites

- Python 3.10+
- Docker installed and running
- Claude Desktop (for MCP integration)

## Installation

1. Clone the repository:
```bash
git clone https://github.com/colinthekkinedath/sre-docker-mcp.git
cd sre-docker-mcp
```

2. Create and activate virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

## Configuration

### Claude Desktop Setup

Add to your `claude_desktop_config.json`:

**macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
**Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
**Linux:** `~/.config/Claude/claude_desktop_config.json`
```json
{
  "mcpServers": {
    "sre-docker": {
      "command": "/absolute/path/to/sre-docker-mcp/venv/bin/python3",
      "args": ["/absolute/path/to/sre-docker-mcp/sre_docker_server.py"]
    }
  }
}
```

## Usage

### Standalone Testing
```bash
python sre_docker_server.py
```

### Example Commands (via Claude)

- "List all running Docker containers"
- "Deploy nginx:latest as web-server on port 8080"
- "Analyze container health"
- "Show deployment history"
- "Create an incident for high CPU usage"
- "Perform rolling update of my-app to version 2.0"

## Available Tools

- `list_containers` - List all containers with stats
- `get_container_details` - Detailed container information
- `analyze_health` - Health analysis with threshold checks
- `deploy_container` - Deploy new containers
- `rolling_update` - Zero-downtime updates
- `create_incident` - SRE incident tracking
- `compose_up/down` - Docker Compose operations
- `system_prune` - Cleanup unused resources
- And many more...

## Architecture
```
sre-docker-mcp/
├── sre_docker_server.py    # Main MCP server
├── requirements.txt        # Python dependencies
├── README.md              # This file
└── .gitignore            # Git ignore rules
```

## Database

The server uses SQLite to store:
- Health metrics history
- Incident tracking
- Deployment history
- Runbook templates

Database file (`sre_docker.db`) is created automatically and stored locally.

## Development

### Running Tests
```bash
# TODO: Add tests
pytest
```

### Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

## Security Notes

- Never commit the `.db` files (contains local data)
- Keep Docker daemon secure
- Review container permissions before deployment

## License

MIT License - see LICENSE file for details

## Troubleshooting

### Docker Connection Issues
```bash
# Check Docker is running
docker ps

# Check permissions (Linux)
sudo usermod -aG docker $USER
```

### MCP Not Connecting
- Verify absolute paths in config
- Check Claude Desktop logs
- Restart Claude Desktop completely

## Roadmap

- [ ] Add alerting webhooks (Slack, PagerDuty)
- [ ] Log analysis and anomaly detection
- [ ] Custom runbook automation
- [ ] Kubernetes support
- [ ] Prometheus metrics export
- [ ] Web dashboard

## Support

For issues and questions, please open a GitHub issue.
