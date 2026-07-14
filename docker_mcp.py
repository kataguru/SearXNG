import json
from typing import Optional

from fastmcp import FastMCP
from pydantic import Field

_docker_client = None


def _get_docker():
    global _docker_client
    if _docker_client is None:
        import docker
        _docker_client = docker.from_env()
    return _docker_client


def _format_size(size_bytes):
    if size_bytes is None:
        return "N/A"
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


mcp = FastMCP(
    "docker-mcp",
    instructions="Docker MCP server for managing containers and images."
)


@mcp.tool()
async def docker_mcp_list_containers(all_: bool = False):
    """List Docker containers. Set all_=True to include stopped containers."""
    import docker
    try:
        d = _get_docker()
        containers = d.containers.list(all=all_)
        results = []
        for c in containers:
            ports = {}
            for k, v in c.ports.items():
                if v:
                    for entry in v:
                        host = f"{entry.get('HostPort', '')}"
                        ports[k] = f"{host}->{entry.get('ContainerPort', '')}"

            results.append({
                "id": c.short_id,
                "name": c.name,
                "image": c.image.tags[0] if c.image.tags else c.image.id[:12],
                "status": c.status,
                "ports": ports,
            })
        return json.dumps(results, indent=2)
    except docker.errors.DockerException as e:
        return f"Error: Docker daemon error: {e}"


@mcp.tool()
async def docker_mcp_list_images():
    """List Docker images."""
    import docker
    try:
        d = _get_docker()
        images = d.images.list()
        results = []
        for i in images:
            tags = i.tags if i.tags else [i.id[:12]]
            size = i.attrs.get("Size", 0)
            results.append({
                "id": i.id[:12],
                "tags": tags,
                "size": _format_size(size),
            })
        return json.dumps(results, indent=2)
    except docker.errors.DockerException as e:
        return f"Error: Docker daemon error: {e}"


@mcp.tool()
async def docker_mcp_logs(
    container_id: str = Field(..., description="Container ID or name"),
    tail: int = Field(default=100, description="Number of lines from end of logs"),
):
    """Get logs from a Docker container."""
    import docker
    try:
        d = _get_docker()
        c = d.containers.get(container_id)
        return c.logs(tail=tail).decode("utf-8", errors="replace")
    except docker.errors.NotFound:
        return f"Container '{container_id}' not found."
    except docker.errors.DockerException as e:
        return f"Error: Docker daemon error: {e}"


@mcp.tool()
async def docker_mcp_start_container(
    container_id: str = Field(..., description="Container ID or name"),
):
    """Start a Docker container."""
    import docker
    try:
        d = _get_docker()
        c = d.containers.get(container_id)
        c.start()
        return f"Container '{c.short_id}' started successfully."
    except docker.errors.NotFound:
        return f"Container '{container_id}' not found."
    except docker.errors.DockerException as e:
        return f"Error: Docker daemon error: {e}"

@mcp.tool()
async def docker_mcp_stop_container(
    container_id: str = Field(..., description="Container ID or name"),
):
    """Stop a running Docker container."""
    import docker
    try:
        d = _get_docker()
        c = d.containers.get(container_id)
        c.stop()
        return f"Container '{c.short_id}' stopped successfully."
    except docker.errors.NotFound:
        return f"Container '{container_id}' not found."
    except docker.errors.DockerException as e:
        return f"Error: Docker daemon error: {e}"

@mcp.tool()
async def docker_mcp_restart_container(
    container_id: str = Field(..., description="Container ID or name"),
):
    """Restart a Docker container."""
    import docker
    try:
        d = _get_docker()
        c = d.containers.get(container_id)
        c.restart()
        return f"Container '{c.short_id}' restarted successfully."
    except docker.errors.NotFound:
        return f"Container '{container_id}' not found."
    except docker.errors.DockerException as e:
        return f"Error: Docker daemon error: {e}"


@mcp.tool()
async def docker_mcp_stop_container(
    container_id: str = Field(..., description="Container ID or name"),
):
    """Stop a running Docker container."""
    d = _get_docker()
    import docker
    try:
        c = d.containers.get(container_id)
        c.stop()
        return f"Container '{c.short_id}' stopped successfully."
    except docker.errors.NotFound:
        return f"Container '{container_id}' not found."


@mcp.tool()
async def docker_mcp_restart_container(
    container_id: str = Field(..., description="Container ID or name"),
):
    """Restart a Docker container."""
    d = _get_docker()
    import docker
    try:
        c = d.containers.get(container_id)
        c.restart()
        return f"Container '{c.short_id}' restarted successfully."
    except docker.errors.NotFound:
        return f"Container '{container_id}' not found."


if __name__ == "__main__":
    mcp.run()
