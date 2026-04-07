import asyncio
from datetime import datetime, timezone
import logging
import os
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from kubernetes import client, config
from kubernetes.client import ApiException

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("discord-kube-bot")


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
DISCORD_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0"))
ALLOWED_ROLE_IDS = {
    int(role_id.strip())
    for role_id in os.getenv("DISCORD_ALLOWED_ROLE_IDS", "").split(",")
    if role_id.strip().isdigit()
}
ALLOWED_NAMESPACES = {
    ns.strip()
    for ns in os.getenv("KUBE_ALLOWED_NAMESPACES", "discord-bot").split(",")
    if ns.strip()
}
AUDIT_WEBHOOK_URL = os.getenv("DISCORD_AUDIT_WEBHOOK_URL", "")


def ensure_kube_config() -> None:
    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes config")
    except config.config_exception.ConfigException:
        config.load_kube_config()
        logger.info("Loaded local kubeconfig")


def is_namespace_allowed(namespace: str) -> bool:
    return namespace in ALLOWED_NAMESPACES


def member_has_allowed_role(member: discord.Member) -> bool:
    if not ALLOWED_ROLE_IDS:
        return True
    member_role_ids = {role.id for role in member.roles}
    return bool(member_role_ids & ALLOWED_ROLE_IDS)


async def send_audit(message: str) -> None:
    if not AUDIT_WEBHOOK_URL:
        return
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            await session.post(AUDIT_WEBHOOK_URL, json={"content": message})
    except Exception:
        logger.exception("Failed to send audit webhook")


class KubeBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        guild = discord.Object(id=DISCORD_GUILD_ID) if DISCORD_GUILD_ID else None
        if guild:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Synced commands to guild %s", DISCORD_GUILD_ID)
        else:
            await self.tree.sync()
            logger.info("Synced global commands")


bot = KubeBot()


async def run_blocking(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


def format_pods(namespace: str) -> str:
    api = client.CoreV1Api()
    pods = api.list_namespaced_pod(namespace=namespace).items
    if not pods:
        return f"No pods found in `{namespace}`"

    lines = []
    for pod in pods:
        phase = pod.status.phase
        name = pod.metadata.name
        node = pod.spec.node_name or "n/a"
        lines.append(f"- {name} | {phase} | node={node}")
    return "\n".join(lines)


def read_logs(namespace: str, pod: str, tail_lines: int = 200, container: Optional[str] = None) -> str:
    api = client.CoreV1Api()
    return api.read_namespaced_pod_log(
        name=pod,
        namespace=namespace,
        tail_lines=tail_lines,
        container=container,
    )


def restart_deployment(namespace: str, deployment: str) -> str:
    apps = client.AppsV1Api()
    patch = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": datetime.now(timezone.utc).isoformat()
                    }
                }
            }
        }
    }
    apps.patch_namespaced_deployment(name=deployment, namespace=namespace, body=patch)
    return f"Restart triggered for deployment `{deployment}` in `{namespace}`"


def scale_deployment(namespace: str, deployment: str, replicas: int) -> str:
    apps = client.AppsV1Api()
    body = {"spec": {"replicas": replicas}}
    apps.patch_namespaced_deployment_scale(name=deployment, namespace=namespace, body=body)
    return f"Scaled deployment `{deployment}` in `{namespace}` to replicas={replicas}"


async def validate_access(interaction: discord.Interaction, namespace: str, mutate: bool = False) -> bool:
    if not is_namespace_allowed(namespace):
        await interaction.response.send_message(
            f"Namespace `{namespace}` is not allowed. Allowed: {', '.join(sorted(ALLOWED_NAMESPACES))}",
            ephemeral=True,
        )
        return False

    if mutate:
        member = interaction.user
        if not isinstance(member, discord.Member) or not member_has_allowed_role(member):
            await interaction.response.send_message(
                "You do not have permission for mutating commands.",
                ephemeral=True,
            )
            return False
    return True


@bot.tree.command(name="k8s_get", description="List pods in namespace")
@app_commands.describe(namespace="Kubernetes namespace")
async def k8s_get(interaction: discord.Interaction, namespace: str = "discord-bot") -> None:
    if not await validate_access(interaction, namespace, mutate=False):
        return

    await interaction.response.defer(ephemeral=True)
    try:
        output = await run_blocking(format_pods, namespace)
        msg = output[:1900]
        await interaction.followup.send(f"```\n{msg}\n```", ephemeral=True)
    except ApiException as e:
        await interaction.followup.send(f"Kubernetes API error: `{e.reason}`", ephemeral=True)


@bot.tree.command(name="k8s_logs", description="Read pod logs")
@app_commands.describe(namespace="Namespace", pod="Pod name", lines="Tail lines (max 500)")
async def k8s_logs(
    interaction: discord.Interaction,
    namespace: str,
    pod: str,
    lines: app_commands.Range[int, 1, 500] = 200,
) -> None:
    if not await validate_access(interaction, namespace, mutate=False):
        return

    await interaction.response.defer(ephemeral=True)
    try:
        logs = await run_blocking(read_logs, namespace, pod, lines)
        msg = logs[-1900:] if len(logs) > 1900 else logs
        await interaction.followup.send(f"```\n{msg}\n```", ephemeral=True)
    except ApiException as e:
        await interaction.followup.send(f"Kubernetes API error: `{e.reason}`", ephemeral=True)


@bot.tree.command(name="k8s_restart", description="Restart a deployment")
@app_commands.describe(namespace="Namespace", deployment="Deployment name")
async def k8s_restart(interaction: discord.Interaction, namespace: str, deployment: str) -> None:
    if not await validate_access(interaction, namespace, mutate=True):
        return

    await interaction.response.defer(ephemeral=True)
    try:
        result = await run_blocking(restart_deployment, namespace, deployment)
        await interaction.followup.send(result, ephemeral=True)
        await send_audit(f"{interaction.user} restarted {namespace}/{deployment}")
    except ApiException as e:
        await interaction.followup.send(f"Kubernetes API error: `{e.reason}`", ephemeral=True)


@bot.tree.command(name="k8s_scale", description="Scale a deployment")
@app_commands.describe(namespace="Namespace", deployment="Deployment", replicas="Desired replicas")
async def k8s_scale(
    interaction: discord.Interaction,
    namespace: str,
    deployment: str,
    replicas: app_commands.Range[int, 0, 100],
) -> None:
    if not await validate_access(interaction, namespace, mutate=True):
        return

    await interaction.response.defer(ephemeral=True)
    try:
        result = await run_blocking(scale_deployment, namespace, deployment, replicas)
        await interaction.followup.send(result, ephemeral=True)
        await send_audit(f"{interaction.user} scaled {namespace}/{deployment} to {replicas}")
    except ApiException as e:
        await interaction.followup.send(f"Kubernetes API error: `{e.reason}`", ephemeral=True)


@bot.tree.command(name="k8s_health", description="Bot and Kubernetes health check")
async def k8s_health(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        version_api = client.VersionApi()
        version = await run_blocking(version_api.get_code)
        await interaction.followup.send(
            f"Bot OK. Connected to Kubernetes `{version.git_version}`",
            ephemeral=True,
        )
    except Exception as e:
        await interaction.followup.send(f"Health check failed: `{type(e).__name__}` - {e}", ephemeral=True)


@bot.event
async def on_ready() -> None:
    logger.info("Bot connected as %s", bot.user)


def main() -> None:
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is required")
    ensure_kube_config()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
