#!/bin/sh
# Create deploy/.env from deploy/.env.example with freshly generated random secrets.
#
#   deploy/scripts/generate-secrets.sh [OUTPUT_FILE]
#
# OUTPUT_FILE defaults to deploy/.env. The script never overwrites an existing file
# (delete or rename it yourself if you really want new secrets: changing the secret
# key or restic password makes existing encrypted data unreadable). Secrets are
# never printed. Only POSIX sh, od and awk are required.
set -eu

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
deploy_dir=$(dirname -- "$script_dir")
example="${ENV_EXAMPLE:-$deploy_dir/.env.example}"
target="${1:-$deploy_dir/.env}"

die() {
    echo "generate-secrets: $*" >&2
    exit 1
}

[ "$#" -le 1 ] || die "usage: $0 [OUTPUT_FILE]"
[ -f "$example" ] || die "template not found: $example"
if [ -e "$target" ] || [ -L "$target" ]; then
    die "$target already exists; refusing to overwrite it"
fi
[ -r /dev/urandom ] || die "/dev/urandom is not readable"

# N random bytes from the kernel CSPRNG as lowercase hex (2*N characters).
random_hex() {
    od -An -v -tx1 -N "$1" /dev/urandom | tr -d ' \n'
}

MCPANEL_SECRET_KEY=$(random_hex 48)
MCPANEL_AGENT_TOKEN=$(random_hex 32)
POSTGRES_PASSWORD=$(random_hex 24)
AGENT_RESTIC_PASSWORD=$(random_hex 32)
export MCPANEL_SECRET_KEY MCPANEL_AGENT_TOKEN POSTGRES_PASSWORD AGENT_RESTIC_PASSWORD

for name in MCPANEL_SECRET_KEY MCPANEL_AGENT_TOKEN POSTGRES_PASSWORD AGENT_RESTIC_PASSWORD; do
    eval "value=\${$name}"
    # shellcheck disable=SC2154 # assigned by eval above
    [ "${#value}" -ge 48 ] || die "failed to generate $name"
    count=$(grep -c "^${name}=\$" "$example" || true)
    [ "$count" = 1 ] || die "$example must contain exactly one empty '${name}=' line (found $count)"
done

umask 077
tmp=$(mktemp "$(dirname -- "$target")/.env.tmp.XXXXXX") || die "cannot create a temporary file next to $target"
trap 'rm -f "$tmp"' EXIT
trap 'exit 130' HUP INT TERM

# Values are read from the environment (ENVIRON), so awk applies no escape processing.
awk '
    BEGIN { split("MCPANEL_SECRET_KEY MCPANEL_AGENT_TOKEN POSTGRES_PASSWORD AGENT_RESTIC_PASSWORD", keys, " ") }
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
echo "Next: review MCPANEL_DATA_DIR, MC_UID/MC_GID, MCPANEL_BIND, MCPANEL_ALLOWED_HOSTS and"
echo "MCPANEL_CSRF_TRUSTED_ORIGINS in that file, then run: docker compose up -d --build"
echo "Store a copy of AGENT_RESTIC_PASSWORD outside this server - backups cannot be restored without it."
