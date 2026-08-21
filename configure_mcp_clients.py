"""configure_mcp_clients.py -- zero-touch external-client MCP registration.

Run by the Anvil CAD installer (and uninstaller with --remove) using the BUNDLED
python, so the user never edits any mcp.json by hand. It registers the anvil-cad
tool server with every known MCP client on this machine, merge-safely:

    Cursor          %USERPROFILE%\.cursor\mcp.json
    Claude Desktop  %APPDATA%\Claude\claude_desktop_config.json
    Claude Code     %USERPROFILE%\.claude.json   (only if it already exists)

Usage:
    python configure_mcp_clients.py <install_dir>            # add/refresh entry
    python configure_mcp_clients.py <install_dir> --remove   # uninstall cleanup

Every path is derived from the given install dir + this user's profile -- nothing
is hardcoded to any machine. Existing config content is preserved: only the
"anvil-cad" server entry is added, replaced, or removed. A corrupt/unreadable
client config is left untouched (never destroy a user's file to add ourselves).
"""

import json
import os
import sys


SERVER_KEY = "anvil-cad"


def _targets():
    home = os.path.expanduser("~")
    appdata = os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming"))
    return [
        # (path, create-if-missing)
        (os.path.join(home, ".cursor", "mcp.json"), True),
        (os.path.join(appdata, "Claude", "claude_desktop_config.json"), True),
        # Claude Code's ~/.claude.json holds much more state than MCP servers:
        # only merge into an existing one, never create it.
        (os.path.join(home, ".claude.json"), False),
    ]


def _load(path):
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def main():
    if len(sys.argv) < 2:
        print("usage: configure_mcp_clients.py <install_dir> [--remove]")
        return 1

    install = sys.argv[1].rstrip("\\/")
    remove = "--remove" in sys.argv[2:]

    entry = {
        "command": os.path.join(install, "bin", "ai", "python", "python.exe"),
        "args": [os.path.join(install, "share", "anvil", "ai",
                              "skidl_mcp_server.py")],
    }

    for path, create in _targets():
        exists = os.path.isfile(path)
        if not exists and (remove or not create):
            continue
        try:
            data = _load(path) if exists else {}
        except Exception:
            print(f"skip (unreadable, left untouched): {path}")
            continue
        if not isinstance(data, dict):
            print(f"skip (not an object, left untouched): {path}")
            continue

        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            if remove:
                continue
            servers = {}
            data["mcpServers"] = servers

        if remove:
            if SERVER_KEY not in servers:
                continue
            del servers[SERVER_KEY]
        else:
            servers[SERVER_KEY] = entry

        try:
            _save(path, data)
            print(("removed from" if remove else "configured"), path)
        except Exception as e:
            print(f"skip (write failed): {path}: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
