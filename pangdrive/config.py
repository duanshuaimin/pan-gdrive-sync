"""Configuration and credentials management for Baidu Netdisk & Google Drive."""

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_DIR = Path.home() / ".config" / "pan-gdrive-sync"
CONFIG_FILE = CONFIG_DIR / "config.json"

# Auto-migration sources
BAIDUPAN_CONFIG = Path.home() / ".config" / "baidupan" / "config.json"
BAIDUPCS_GO_CONFIG = Path.home() / ".config" / "BaiduPCS-Go" / "pcs_config.json"

DEFAULT_PAN_UA = (
    "netdisk;P2SP;3.0.0.8;netdisk;11.12.3;ANG-AN00;android-android;10.0;JSbridge4.4.0;jointBridge;1.1.0;"
)
DEFAULT_APP_ID = 266719


class Config:
    def __init__(self):
        self.config_dir = CONFIG_DIR
        self.config_file = CONFIG_FILE
        self.data: Dict[str, Any] = {
            "baidu": {
                "bduss": "",
                "stoken": "",
                "cookies": "",
                "username": "",
                "uid": 0,
                "app_id": DEFAULT_APP_ID,
                "user_agent": DEFAULT_PAN_UA,
            },
            "gdrive": {
                "auth_mode": "oauth2",  # 'service_account', 'oauth2', or 'token'
                "service_account_file": "",
                "access_token": "",
                "refresh_token": "",
                "client_id": "",
                "client_secret": "",
                "token_expiry": 0,
                "root_folder_id": "root",
            },
            "transfer": {
                "stream_mode": True,
                "chunk_size": 8 * 1024 * 1024,  # 8MB
                "temp_dir": str(Path.home() / ".cache" / "pan-gdrive-sync"),
                "max_retries": 3,
                "conflict_policy": "overwrite",  # 'overwrite', 'skip', or 'newer'
            },
        }
        self.load()

    def load(self):
        if self.config_file.exists():
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                    for k, v in saved.items():
                        if isinstance(v, dict) and k in self.data:
                            self.data[k].update(v)
                        else:
                            self.data[k] = v
            except Exception:
                pass
        else:
            self._try_migrate_baidu_credentials()

    def save(self):
        self.config_dir.mkdir(parents=True, exist_ok=True)
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        try:
            os.chmod(self.config_file, 0o600)
        except Exception:
            pass

    def _try_migrate_baidu_credentials(self):
        """Auto-import Baidu credentials from baidupan or BaiduPCS-Go."""
        migrated = False
        # Try baidupan config first
        if BAIDUPAN_CONFIG.exists():
            try:
                with open(BAIDUPAN_CONFIG, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                active = cfg.get("active_user")
                user_info = cfg.get("users", {}).get(active, {})
                if user_info and user_info.get("bduss"):
                    self.data["baidu"]["bduss"] = user_info.get("bduss", "")
                    self.data["baidu"]["stoken"] = user_info.get("stoken", "")
                    self.data["baidu"]["cookies"] = user_info.get("cookies", "")
                    self.data["baidu"]["username"] = user_info.get("username", "")
                    self.data["baidu"]["uid"] = user_info.get("uid", 0)
                    migrated = True
            except Exception:
                pass

        # Try BaiduPCS-Go config if not found
        if not migrated and BAIDUPCS_GO_CONFIG.exists():
            try:
                with open(BAIDUPCS_GO_CONFIG, "r", encoding="utf-8") as f:
                    pcs_cfg = json.load(f)
                users = pcs_cfg.get("baidu_user_list", [])
                active_uid = pcs_cfg.get("baidu_active_uid")
                for u in users:
                    if u.get("uid") == active_uid or not self.data["baidu"]["bduss"]:
                        self.data["baidu"]["bduss"] = u.get("bduss", "")
                        self.data["baidu"]["stoken"] = u.get("stoken", "")
                        self.data["baidu"]["cookies"] = u.get("cookies", "")
                        self.data["baidu"]["username"] = u.get("name", "")
                        self.data["baidu"]["uid"] = u.get("uid", 0)
                        migrated = True
            except Exception:
                pass

        if migrated:
            self.save()

    def set_baidu(self, bduss: str, stoken: str = "", cookies: str = "", username: str = "", uid: int = 0):
        self.data["baidu"]["bduss"] = bduss.strip()
        if stoken:
            self.data["baidu"]["stoken"] = stoken.strip()
        if cookies:
            self.data["baidu"]["cookies"] = cookies.strip()
        if username:
            self.data["baidu"]["username"] = username.strip()
        if uid:
            self.data["baidu"]["uid"] = uid
        self.save()

    def set_gdrive_service_account(self, key_file: str):
        path = Path(key_file).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Service account file not found: {key_file}")
        # Verify JSON
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "client_email" not in data or "private_key" not in data:
            raise ValueError("Invalid Google service account JSON key file (missing client_email or private_key).")

        self.data["gdrive"]["auth_mode"] = "service_account"
        self.data["gdrive"]["service_account_file"] = str(path)
        self.save()

    def set_gdrive_token(self, access_token: str, refresh_token: str = "", client_id: str = "", client_secret: str = ""):
        self.data["gdrive"]["auth_mode"] = "token" if not refresh_token else "oauth2"
        self.data["gdrive"]["access_token"] = access_token.strip()
        if refresh_token:
            self.data["gdrive"]["refresh_token"] = refresh_token.strip()
        if client_id:
            self.data["gdrive"]["client_id"] = client_id.strip()
        if client_secret:
            self.data["gdrive"]["client_secret"] = client_secret.strip()
        self.save()


config = Config()
