#!/usr/bin/env bash
# One-time (and safe to re-run) setup of the Doshin EC2 host — Ubuntu 24.04, run as root:
#
#   sudo bash setup_server.sh <domain> <letsencrypt-email>
#
# Installs Docker + compose, nginx, certbot and the AWS CLI; adds 4 GB swap; creates the data
# folders; creates the "deploy" user that GitHub Actions logs in as (SSH key added by you);
# gets the TLS certificate and installs the nginx site. Before running it, the domain's DNS
# A record must point at this server's Elastic IP and ports 80/443 must be open.
set -euo pipefail

DOMAIN="${1:?usage: setup_server.sh <domain> <letsencrypt-email>}"
EMAIL="${2:?usage: setup_server.sh <domain> <letsencrypt-email>}"
APP_DIR=/opt/salesai
HERE="$(cd "$(dirname "$0")" && pwd)"

if [[ $EUID -ne 0 ]]; then echo "Run as root (sudo)." >&2; exit 1; fi
if [[ ! "$DOMAIN" =~ ^[a-z0-9.-]+\.[a-z]{2,}$ ]]; then echo "Domain looks wrong: $DOMAIN" >&2; exit 1; fi

echo "== Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ca-certificates curl unzip nginx certbot python3-certbot-nginx docker.io docker-compose-v2
systemctl enable --now docker nginx

if ! command -v aws >/dev/null; then
  echo "== AWS CLI v2 (ECR login uses the instance role)"
  tmp="$(mktemp -d)"
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "$tmp/awscliv2.zip"
  unzip -q "$tmp/awscliv2.zip" -d "$tmp" && "$tmp/aws/install"
  rm -rf "$tmp"
fi

echo "== Swap (4 GB)"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
sysctl -q vm.swappiness=10
grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf

echo "== Folders"
mkdir -p "$APP_DIR/data/output/chroma" /var/www/certbot
chown -R 1000:1000 "$APP_DIR/data/output"          # the container runs as uid 1000
chmod 750 "$APP_DIR"

echo "== Deploy user (GitHub Actions logs in as 'deploy')"
if ! id deploy >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash deploy
fi
usermod -aG docker deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
touch /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys && chmod 600 /home/deploy/.ssh/authorized_keys
chown -R deploy:deploy "$APP_DIR"
chown -R 1000:1000 "$APP_DIR/data/output"

echo "== nginx + TLS certificate for $DOMAIN"
rm -f /etc/nginx/sites-enabled/default
if [[ ! -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]]; then
  # Bootstrap: HTTP only, just enough to answer the ACME challenge
  cat > /etc/nginx/sites-available/salesai <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 503; }
}
EOF
  ln -sf /etc/nginx/sites-available/salesai /etc/nginx/sites-enabled/salesai
  nginx -t && systemctl reload nginx
  certbot certonly --webroot -w /var/www/certbot -d "$DOMAIN" --email "$EMAIL" --agree-tos --non-interactive
fi
# certbot's shared TLS settings (normally written by the nginx plugin)
[[ -f /etc/letsencrypt/options-ssl-nginx.conf ]] || \
  curl -fsSL https://raw.githubusercontent.com/certbot/certbot/main/certbot-nginx/src/certbot_nginx/_internal/tls_configs/options-ssl-nginx.conf \
    -o /etc/letsencrypt/options-ssl-nginx.conf
[[ -f /etc/letsencrypt/ssl-dhparams.pem ]] || openssl dhparam -out /etc/letsencrypt/ssl-dhparams.pem 2048

sed "s/__DOMAIN__/$DOMAIN/g" "$HERE/nginx/salesai.conf" > /etc/nginx/sites-available/salesai
ln -sf /etc/nginx/sites-available/salesai /etc/nginx/sites-enabled/salesai
nginx -t && systemctl reload nginx

# Renewals: certbot's systemd timer renews; reload nginx afterwards
mkdir -p /etc/letsencrypt/renewal-hooks/deploy
printf '#!/bin/sh\nsystemctl reload nginx\n' > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh

cat <<EOF

Done. Next:
  1. Add the GitHub deploy public key to /home/deploy/.ssh/authorized_keys
  2. Run the GitHub "Deploy" workflow — it writes $APP_DIR/.env and starts the app
  3. Check: curl -s https://$DOMAIN/api/health
EOF
