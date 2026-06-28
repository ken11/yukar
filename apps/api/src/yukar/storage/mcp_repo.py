"""MCP config storage — project-level mcp.yaml (L3).

Stores the raw (unexpanded) MCP server config as YAML.
${VAR} expansion is NOT performed here — it happens at runtime in McpClientManager
so that secrets are never persisted to disk.

Layout:
  <yukar_dir>/mcp.yaml
"""

from __future__ import annotations

from yukar.config import paths as p
from yukar.models.mcp import McpConfig
from yukar.storage.yaml_io import load_model, save_model


def get_mcp_config(root: str, project_id: str) -> McpConfig:
    """Read mcp.yaml and return McpConfig.

    Returns an empty McpConfig (no servers) if the file does not exist.
    """
    yaml_path = p.project_mcp_yaml(root, project_id)
    return load_model(yaml_path, McpConfig, default=McpConfig())


async def save_mcp_config(root: str, project_id: str, config: McpConfig) -> None:
    """Persist *config* to mcp.yaml atomically."""
    yaml_path = p.project_mcp_yaml(root, project_id)
    await save_model(yaml_path, config)
