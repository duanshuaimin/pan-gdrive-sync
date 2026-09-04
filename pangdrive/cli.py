"""Command Line Interface for pan-gdrive-sync."""

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .baidu_client import BaiduClient
from .config import config
from .gdrive_client import GoogleDriveClient
from .transfer import TransferEngine
from .utils import format_size, split_storage_uri

console = Console()


@click.group()
@click.version_option("1.0.0", prog_name="pan-gdrive-sync")
def cli():
    """Baidu Netdisk ⇄ Google Drive Bidirectional File Sync & Transfer Tool."""
    pass


@cli.group("auth")
def auth_group():
    """Authenticate and configure storage providers."""
    pass


@auth_group.command("baidu")
@click.option("--bduss", "-b", help="Baidu BDUSS cookie value")
@click.option("--stoken", "-s", default="", help="Baidu STOKEN cookie value (optional)")
@click.option("--cookies", "-c", help="Full cookie string copied from browser")
def auth_baidu_cmd(bduss, stoken, cookies):
    """Configure Baidu Netdisk credentials."""
    if cookies:
        cookie_dict = {}
        for item in cookies.split(";"):
            if "=" in item:
                k, v = item.strip().split("=", 1)
                cookie_dict[k.strip()] = v.strip()
        bduss = cookie_dict.get("BDUSS")
        stoken = cookie_dict.get("STOKEN", "")
        if not bduss:
            console.print("[bold red]Failed to find BDUSS in the provided cookie string![/bold red]")
            sys.exit(1)

    if not bduss:
        bduss = click.prompt("Enter Baidu BDUSS", hide_input=True)

    config.set_baidu(bduss=bduss, stoken=stoken, cookies=cookies or "")
    baidu = BaiduClient(config)

    with console.status("[bold cyan]Verifying Baidu Netdisk credentials..."):
        try:
            info = baidu.get_user_info()
            uname = info.get("uname", "User")
            uk = info.get("uk", 0)
            config.set_baidu(bduss=bduss, username=uname, uid=uk, stoken=stoken, cookies=cookies or "")
            console.print(f"[bold green]✓ Baidu Netdisk authenticated successfully![/bold green] User: [bold cyan]{uname}[/bold cyan] (UK: {uk})")
        except Exception as e:
            console.print(f"[bold red]Baidu Netdisk authentication error:[/bold red] {e}")


@auth_group.command("gdrive")
@click.option("--service-account", "-s", help="Path to Google Cloud service account JSON key file")
@click.option("--token", "-t", help="Direct OAuth2 Bearer Access Token")
@click.option("--refresh-token", "-r", help="OAuth2 Refresh Token")
@click.option("--client-id", help="OAuth2 Client ID")
@click.option("--client-secret", help="OAuth2 Client Secret")
def auth_gdrive_cmd(service_account, token, refresh_token, client_id, client_secret):
    """Configure Google Drive credentials."""
    if service_account:
        try:
            config.set_gdrive_service_account(service_account)
            console.print(f"[bold green]✓ Configured Google Drive Service Account key:[/bold green] {service_account}")
        except Exception as e:
            console.print(f"[bold red]Failed to configure Service Account:[/bold red] {e}")
            sys.exit(1)
    elif token or refresh_token:
        config.set_gdrive_token(token or "", refresh_token=refresh_token or "", client_id=client_id or "", client_secret=client_secret or "")
        console.print("[bold green]✓ Configured Google Drive OAuth credentials.[/bold green]")
    else:
        console.print("[yellow]Please provide either --service-account <key.json> or --token <access_token>[/yellow]")
        sys.exit(1)

    gdrive = GoogleDriveClient(config)
    with console.status("[bold cyan]Verifying Google Drive credentials..."):
        try:
            about = gdrive.get_about()
            u_info = about.get("user", {})
            u_name = u_info.get("displayName", "Google User")
            u_email = u_info.get("emailAddress", "N/A")
            console.print(f"[bold green]✓ Google Drive authenticated successfully![/bold green] User: [bold cyan]{u_name}[/bold cyan] ({u_email})")
        except Exception as e:
            console.print(f"[bold yellow]Google Drive verification notice:[/bold yellow] {e}")


@cli.command("status")
def status_cmd():
    """Display connection status and quota for both Baidu Netdisk and Google Drive."""
    baidu = BaiduClient()
    gdrive = GoogleDriveClient()

    # 1. Baidu Status
    b_auth = baidu.is_authenticated()
    if b_auth:
        try:
            b_user = baidu.get_user_info()
            b_quota = baidu.get_quota()
            uname = b_user.get("uname", "N/A")
            uk = b_user.get("uk", "N/A")
            vip = "SVIP" if b_user.get("vip_type") == 2 else ("VIP" if b_user.get("vip_type") == 1 else "Normal")
            b_desc = (
                f"[bold green]Connected[/bold green]\n"
                f"User: {uname} (UK: {uk}) | Tier: {vip}\n"
                f"Storage: {format_size(b_quota['used'])} / {format_size(b_quota['total'])} ({b_quota['percent']}%)"
            )
        except Exception as e:
            b_desc = f"[yellow]Connected (Offline check failed: {e})[/yellow]"
    else:
        b_desc = "[red]Not configured. Run 'pan-gdrive-sync auth baidu'[/red]"

    # 2. GDrive Status
    g_auth = gdrive.is_authenticated()
    if g_auth:
        try:
            g_about = gdrive.get_about()
            u_info = g_about.get("user", {})
            u_name = u_info.get("displayName", "Google User")
            u_email = u_info.get("emailAddress", "N/A")
            g_desc = (
                f"[bold green]Connected[/bold green]\n"
                f"Account: {u_name} ({u_email})\n"
                f"Storage: {format_size(g_about['used'])} / {format_size(g_about['total'])} ({g_about['percent']}%)"
            )
        except Exception as e:
            g_desc = f"[yellow]Configured (Verification: {e})[/yellow]"
    else:
        g_desc = "[red]Not configured. Run 'pan-gdrive-sync auth gdrive'[/red]"

    table = Table(title="Pan-GDrive-Sync Cloud Storage Providers", title_style="bold blue")
    table.add_column("Provider", style="bold cyan")
    table.add_column("Status & Details")
    table.add_row("Baidu Netdisk (百度网盘)", b_desc)
    table.add_row("Google Drive", g_desc)
    console.print(table)


@cli.command("quota")
def quota_cmd():
    """Compare storage quotas of both cloud providers."""
    baidu = BaiduClient()
    gdrive = GoogleDriveClient()

    table = Table(title="Storage Quota Comparison", title_style="bold blue")
    table.add_column("Provider", style="bold cyan")
    table.add_column("Total Space", justify="right")
    table.add_column("Used Space", justify="right")
    table.add_column("Free Space", justify="right")
    table.add_column("Usage (%)", justify="center")

    if baidu.is_authenticated():
        try:
            bq = baidu.get_quota()
            table.add_row("Baidu Netdisk", format_size(bq["total"]), format_size(bq["used"]), format_size(bq["free"]), f"{bq['percent']}%")
        except Exception:
            table.add_row("Baidu Netdisk", "Error", "Error", "Error", "-")
    else:
        table.add_row("Baidu Netdisk", "Not Configured", "-", "-", "-")

    if gdrive.is_authenticated():
        try:
            gq = gdrive.get_about()
            table.add_row("Google Drive", format_size(gq["total"]), format_size(gq["used"]), format_size(gq["free"]), f"{gq['percent']}%")
        except Exception:
            table.add_row("Google Drive", "Error", "Error", "Error", "-")
    else:
        table.add_row("Google Drive", "Not Configured", "-", "-", "-")

    console.print(table)


@cli.command("ls")
@click.argument("uri")
def ls_cmd(uri):
    """List files in cloud storage. URI format: 'baidu:/path' or 'gdrive:/folder'."""
    try:
        provider, path = split_storage_uri(uri)
    except Exception as e:
        console.print(f"[bold red]Invalid URI:[/bold red] {e}")
        sys.exit(1)

    with console.status(f"[cyan]Listing {provider.upper()}:{path}..."):
        try:
            if provider == "baidu":
                baidu = BaiduClient()
                items = baidu.list_dir(path)
            else:
                gdrive = GoogleDriveClient()
                items = gdrive.list_dir(path)
        except Exception as e:
            console.print(f"[bold red]List failed:[/bold red] {e}")
            sys.exit(1)

    if not items:
        console.print(f"[dim]Directory {path} is empty.[/dim]")
        return

    table = Table(title=f"{provider.upper()}: {path} ({len(items)} items)", title_style="bold blue")
    table.add_column("Type", justify="center", style="cyan")
    table.add_column("Name", style="bold")
    table.add_column("Size", justify="right", style="green")
    table.add_column("Modified Time", style="yellow")

    for it in items:
        t = "📁 DIR" if it["isdir"] else "📄 FILE"
        sz = "-" if it["isdir"] else format_size(it["size"])
        table.add_row(t, it["name"], sz, str(it.get("mtime", "-")))

    console.print(table)


@cli.command("copy")
@click.argument("src_uri")
@click.argument("dst_uri")
@click.option("--overwrite/--skip", default=True, help="Overwrite or skip if destination file exists")
@click.option("--disk-cache", is_flag=True, help="Use local temporary disk buffer instead of direct streaming pipe")
def copy_cmd(src_uri, dst_uri, overwrite, disk_cache):
    """Transfer a single file between Baidu Netdisk and Google Drive.

    Example: pan-gdrive-sync copy baidu:/test.docx gdrive:/backup/
    """
    try:
        src_p, src_path = split_storage_uri(src_uri)
        dst_p, dst_path = split_storage_uri(dst_uri)
    except Exception as e:
        console.print(f"[bold red]URI Error:[/bold red] {e}")
        sys.exit(1)

    engine = TransferEngine()
    ondup = "overwrite" if overwrite else "skip"
    console.print(f"[bold cyan]Initiating cross-cloud transfer:[/bold cyan] {src_uri} -> {dst_uri}")

    try:
        res = engine.transfer_file(
            src_p,
            src_path,
            dst_p,
            dst_path,
            ondup=ondup,
            use_disk_cache=disk_cache,
        )
        console.print(f"[bold green]✓ File transfer completed successfully![/bold green]")
    except Exception as e:
        console.print(f"[bold red]Transfer failed:[/bold red] {e}")
        sys.exit(1)


@cli.command("sync")
@click.argument("src_uri")
@click.argument("dst_uri")
@click.option("--overwrite/--skip", default=True, help="Overwrite or skip existing files")
@click.option("--no-recursive", is_flag=True, help="Do not recurse into subdirectories")
def sync_cmd(src_uri, dst_uri, overwrite, no_recursive):
    """Synchronize an entire folder between Baidu Netdisk and Google Drive.

    Example: pan-gdrive-sync sync baidu:/folder gdrive:/backup/
    """
    try:
        src_p, src_path = split_storage_uri(src_uri)
        dst_p, dst_path = split_storage_uri(dst_uri)
    except Exception as e:
        console.print(f"[bold red]URI Error:[/bold red] {e}")
        sys.exit(1)

    engine = TransferEngine()
    ondup = "overwrite" if overwrite else "skip"
    try:
        engine.sync_directory(
            src_p,
            src_path,
            dst_p,
            dst_path,
            ondup=ondup,
            recursive=not no_recursive,
        )
    except Exception as e:
        console.print(f"[bold red]Sync failed:[/bold red] {e}")
        sys.exit(1)


def main():
    cli()


if __name__ == "__main__":
    main()
