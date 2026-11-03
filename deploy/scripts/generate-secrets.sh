#!/bin/sh
# Create an env file from its template with freshly generated random secrets.
#
#   deploy/scripts/generate-secrets.sh [OUTPUT_FILE]          # panel host: deploy/.env
#   deploy/scripts/generate-secrets.sh --node [OUTPUT_FILE]   # extra node: deploy/node.env
#
# OUTPUT_FILE defaults to deploy/.env (or deploy/node.env with --node). The script never
# overwrites an existing file (delete or rename it yourself if you really want new
# secrets: changing the secret key or restic password makes existing encrypted data
# unreadable). Secrets are never printed. Only POSIX sh, od and awk are required.
set -eu

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
deploy_dir=$(dirname -- "$script_dir")

die() {
    echo "generate-secrets: $*" >&2
    exit 1
}

mode=panel
if [ "${1:-}" = "--node" ]; then
    mode=node
    shift
fi
[ "$#" -le 1 ] || die "usage: $0 [--node] [OUTPUT_FILE]"

if [ "$mode" = node ]; then
    example="${ENV_EXAMPLE:-$deploy_dir/node.env.example}"
    target="${1:-$deploy_dir/node.env}"
    keys="AGENT_TOKEN AGENT_RESTIC_PASSWORD"
else
    example="${ENV_EXAMPLE:-$deploy_dir/.env.example}"
    target="${1:-$deploy_dir/.env}"
    keys="MCPANEL_SECRET_KEY MCPANEL_AGENT_TOKEN POSTGRES_PASSWORD AGENT_RESTIC_PASSWORD"
fi

[ -f "$example" ] || die "template not found: $example"
if [ -e "$target" ] || [ -L "$target" ]; then
    die "$target already exists; refusing to overwrite it"
fi
[ -r /dev/urandom ] || die "/dev/urandom is not readable"

# N random bytes from the kernel CSPRNG as lowercase hex (2*N characters).
random_hex() {
    od -An -v -tx1 -N "$1" /dev/urandom | tr -d ' \n'
}

for name in $keys; do
    case "$name" in
        MCPANEL_SECRET_KEY) value=$(random_hex 48) ;;
        POSTGRES_PASSWORD) value=$(random_hex 24) ;;
        *) value=$(random_hex 32) ;;
    esac
    eval "$name=\$value"
    # shellcheck disable=SC2163 # exporting the variable whose name is in $name
    export "$name"
    [ "${#value}" -ge 48 ] || die "failed to generate $name"
    count=$(grep -c "^${name}=\$" "$example" || true)
    [ "$count" = 1 ] || die "$example must contain exactly one empty '${name}=' line (found $count)"
done
export SECRET_KEYS="$keys"

umask 077
tmp=$(mktemp "$(dirname -- "$target")/.env.tmp.XXXXXX") || die "cannot create a temporary file next to $target"
trap 'rm -f "$tmp"' EXIT
trap 'exit 130' HUP INT TERM

# Values are read from the environment (ENVIRON), so awk applies no escape processing.
awk '
    BEGIN { split(ENVIRON["SECRET_KEYS"], keys, " ") }
    {
        for (i in keys) {
            if ($0 == keys[i] "=") { print keys[i] "=" ENVIRON[keys[i]]; next }
        }
        print
    }
' "$example" > "$tmp"

# noclobber makes the final create atomic (O_EXCL): a file that appeared in the
# meantime is still never overwritten.
set -C
cat "$tmp" > "$target" || die "$target already exists; refusing to overwrite it"
set +C
chmod 600 "$target"

echo "Created $target with new random secrets (mode 600)."
if [ "$mode" = node ]; then
    echo "Next: review MCPANEL_DATA_DIR, MC_UID/MC_GID, MCPANEL_NODE_BIND and MCPANEL_NODE_HOSTNAMES, then run:"
    echo "  docker compose -f docker-compose.node.yml --env-file node.env up -d --build"
    echo "Add the node in the panel (System -> Nodes) with AGENT_TOKEN from this file and the fingerprint from:"
    echo "  docker compose -f docker-compose.node.yml --env-file node.env exec agent python -m mcagent fingerprint"
else
    echo "Next: review MCPANEL_DATA_DIR, MC_UID/MC_GID, MCPANEL_BIND, MCPANEL_ALLOWED_HOSTS and"
    echo "MCPANEL_CSRF_TRUSTED_ORIGINS in that file, then run: docker compose up -d --build"
fi
echo "Store a copy of AGENT_RESTIC_PASSWORD outside this server - backups cannot be restored without it."
