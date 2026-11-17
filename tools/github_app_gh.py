#!/usr/bin/python3 -I
"""Run a PR/issue gh command with an ephemeral, repository-scoped App token."""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PERMISSIONS = {"contents": "write", "issues": "write", "pull_requests": "write"}
BODY = [("--body", "-b"), ("--body-file", "-F")]
CREATE = [*BODY, ("--title", "-t"), ("--assignee", "-a"), ("--label", "-l"),
          ("--milestone", "-m")]
EDIT = [*BODY, ("--title", "-t"), ("--milestone", "-m"), ("--add-assignee",),
        ("--remove-assignee",), ("--add-label",), ("--remove-label",)]
LIST = [("--assignee", "-a"), ("--author", "-A"), ("--label", "-l"), ("--limit", "-L"),
        ("--search", "-S"), ("--state", "-s"), ("--app",), ("--json",)]
# Each operation explicitly lists value options and boolean switches. Unknown syntax fails closed.
COMMANDS = {
    "pr": {
        "create": (CREATE + [("--base", "-B"), ("--head", "-H"), ("--reviewer", "-r")],
                   [("--draft", "-d"), ("--fill", "-f"), ("--no-maintainer-edit",)]),
        "edit": (EDIT + [("--base", "-B"), ("--add-reviewer",), ("--remove-reviewer",)],
                 [("--remove-milestone",)]),
        "view": ([("--json",)], [("--comments", "-c")]),
        "list": (LIST + [("--base", "-B"), ("--head", "-H")], [("--draft", "-d")]),
        "comment": (BODY, [("--edit-last",), ("--create-if-none",)]),
        "diff": ([("--color",), ("--exclude",)], [("--name-only",), ("--patch",)]),
        "checks": ([("--json",), ("--interval", "-i")],
                   [("--required",), ("--watch",), ("--fail-fast",)]),
    },
    "issue": {
        "create": (CREATE, []),
        "edit": (EDIT, [("--remove-milestone",)]),
        "view": ([("--json",)], [("--comments", "-c")]),
        "list": (LIST + [("--mention",), ("--milestone", "-m")], []),
        "comment": (BODY, [("--edit-last",), ("--create-if-none",)]),
    },
}


class CommandParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("unsupported PR/issue option syntax")


def positive_integer(value):
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise ValueError("expected a positive integer")
    return int(value)


def validate_command(command):
    if (len(command) < 2 or command[1] not in COMMANDS.get(command[0], {})
            or any(not isinstance(arg, str) or "\0" in arg or arg == "--" for arg in command)):
        raise ValueError("only scoped PR/issue commands are supported")
    family, operation = command[:2]
    values, switches = COMMANDS[family][operation]
    parser = CommandParser(add_help=False, allow_abbrev=False)
    for flags in values:
        kwargs = {}
        if flags[0] in {"--limit", "--interval"}:
            kwargs["type"] = positive_integer
        elif flags[0] == "--state":
            kwargs["choices"] = ["open", "closed", "all"] + (["merged"] if family == "pr" else [])
        elif flags[0] == "--color":
            kwargs["choices"] = ["always", "never", "auto"]
        parser.add_argument(*flags, **kwargs)
    for flags in switches:
        parser.add_argument(*flags, action="store_true")
    if operation not in {"create", "list"}:
        parser.add_argument("number", type=positive_integer, nargs="?" if family == "pr" else None)
    options = vars(parser.parse_args(command[2:]))
    body = options.get("body") is not None or options.get("body_file") is not None
    if options.get("body") is not None and options.get("body_file") is not None:
        raise ValueError("choose body or body-file")
    if operation == "create" and not options.get("fill") and not (options.get("title") and body):
        raise ValueError("create requires title and body, or PR fill")
    if operation == "comment" and not body:
        raise ValueError("comment requires a body")
    if operation == "edit" and not any(
        value for name, value in options.items() if name != "number"
    ):
        raise ValueError("edit requires an explicit change")
    if options.get("create_if_none") and not options.get("edit_last"):
        raise ValueError("create-if-none requires edit-last")


def encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def jwt(config):
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 540, "iss": config["app_id"]}
    unsigned = b".".join(encode(json.dumps(value).encode()) for value in
                         ({"alg": "RS256", "typ": "JWT"}, payload))
    signature = subprocess.run(
        ["/usr/bin/openssl", "dgst", "-sha256", "-sign", config["private_key"]],
        input=unsigned, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        check=True, timeout=15, env={"PATH": "/usr/bin:/bin"},
    ).stdout
    return (unsigned + b"." + encode(signature)).decode()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("authentication redirects are forbidden")


def installation_token(config, signed):
    request = urllib.request.Request(
        f'https://api.github.com/app/installations/{config["installation_id"]}/access_tokens',
        data=json.dumps({"repositories": [config["repository"].split("/")[1]],
                         "permissions": PERMISSIONS}).encode(),
        headers={"Authorization": "Bearer " + signed,
                 "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10",
                 "User-Agent": "pokegent-owner", "Content-Type": "application/json"},
        method="POST",
    )
    # Do not forward authentication to an inherited proxy or a redirect destination.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=15) as response:
        result = json.load(response)
    if (not isinstance(result, dict) or not isinstance(result.get("permissions"), dict)
            or not isinstance(result.get("repositories"), list)):
        raise ValueError("invalid installation response")
    token = result["token"]
    if not isinstance(token, str) or not token or any(char.isspace() for char in token):
        raise ValueError("invalid installation token")
    # Never accept broader permissions than requested, even from an unexpected response.
    allowed = {**PERMISSIONS, "metadata": "read"}
    if any(name not in allowed or level != allowed[name]
           for name, level in result["permissions"].items()):
        raise ValueError("unexpected installation permissions")
    repositories = result.get("repositories", [])
    if (len(repositories) != 1 or not isinstance(repositories[0], dict)
            or not isinstance(repositories[0].get("full_name"), str)
            or repositories[0]["full_name"].lower() != config["repository"].lower()):
        raise ValueError("installation token must cover exactly the configured repository")
    return token


def run(config, command):
    if (not isinstance(config, dict)
            or set(config) != {"app_id", "installation_id", "repository", "private_key", "gh"}):
        raise ValueError("missing or unknown App configuration fields")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", config["repository"]):
        raise ValueError("repository must be owner/name")
    if not str(config["installation_id"]).isdigit() or not str(config["app_id"]):
        raise ValueError("invalid App identity")
    for name in ("private_key", "gh"):
        if not Path(config[name]).is_absolute() or not Path(config[name]).is_file():
            raise ValueError("private_key and gh must name absolute files")
    key_stat = Path(config["private_key"]).stat()
    if key_stat.st_uid != os.getuid() or key_stat.st_mode & 0o077:
        raise ValueError("private key must be accessible only to its owner")
    validate_command(command)
    signed = jwt(config)
    token = installation_token(config, signed)
    # Empty gh config prevents aliases, saved credentials, debug settings and pager execution.
    with tempfile.TemporaryDirectory(prefix="pokegent-gh-") as directory:
        env = {"PATH": "/usr/bin:/bin", "HOME": directory, "GH_CONFIG_DIR": directory,
               "GH_TOKEN": token, "GH_HOST": "github.com", "GH_PROMPT_DISABLED": "1",
               "GH_PAGER": "cat", "PAGER": "cat", "GH_BROWSER": "/usr/bin/false",
               "LANG": "C.UTF-8", "NO_COLOR": "1"}
        child = subprocess.run([config["gh"], *command, "--repo", config["repository"]],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # Buffer before forwarding: even a child error that echoes credentials is redacted.
    for stream, output in ((sys.stdout.buffer, child.stdout), (sys.stderr.buffer, child.stderr)):
        for secret in (token, signed):
            output = output.replace(secret.encode(), b"[REDACTED]")
        stream.write(output)
        stream.flush()
    return child.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        os.umask(0o077)
        return run(json.loads(args.config.read_text()), command)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        # Exceptions/HTTP bodies can include credentials; never log their text or traceback.
        print("GitHub App command failed; check private configuration, permissions "
              "and connectivity",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
