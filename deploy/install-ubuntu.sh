#!/usr/bin/env bash
# Run on a fresh Ubuntu server after placing the source at /opt/OptionsDashboard.
set -euo pipefail
if [[ $EUID != 0 ]]; then echo 'Run with sudo.'; exit 1; fi
SERVER_IP=${1:?Supply the server IP}
PROJECT=/opt/OptionsDashboard
DATA=/var/lib/optionsdashboard
python3 - "$SERVER_IP" <<'PY'
import ipaddress,sys
ipaddress.ip_address(sys.argv[1])
PY
id optionsdashboard >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin optionsdashboard
install -d -o optionsdashboard -g optionsdashboard -m 700 "$DATA"
install -d -m 755 /var/www/optionsdashboard-acme
python3 -m venv /opt/optionsdashboard-tools
/opt/optionsdashboard-tools/bin/pip install --disable-pip-version-check uv 'certbot>=5.4'
export UV_PYTHON_INSTALL_DIR=/opt/optionsdashboard-python
/opt/optionsdashboard-tools/bin/uv python install 3.12
/opt/optionsdashboard-tools/bin/uv venv --python 3.12 "$PROJECT/.venv"
/opt/optionsdashboard-tools/bin/uv pip install --python "$PROJECT/.venv/bin/python" -r "$PROJECT/requirements-linux.txt"
mkdir -p /etc/optionsdashboard
chmod 700 /etc/optionsdashboard
python3 - "$SERVER_IP" <<'PY'
import json,secrets,sys
from pathlib import Path
folder=Path('/etc/optionsdashboard')
access={'username':'terminal','password':secrets.token_urlsafe(24),'proxy_token':secrets.token_urlsafe(48)}
receipt=folder/'initial-access.json'
if receipt.exists():
    raise SystemExit('Existing deployment found; refusing to replace credentials.')
receipt.write_text(json.dumps(access))
receipt.chmod(0o600)
env=folder/'server.env'
env.write_text('TERMINAL_DATA_DIR=/var/lib/optionsdashboard\nTERMINAL_REMOTE_ORIGIN=https://'+sys.argv[1]+'\nTERMINAL_PROXY_TOKEN='+access['proxy_token']+'\n')
env.chmod(0o600)
PY
python3 - <<'PY' | htpasswd -ci /etc/optionsdashboard/htpasswd terminal
import json
print(json.load(open('/etc/optionsdashboard/initial-access.json'))['password'])
PY
chown root:www-data /etc/optionsdashboard/htpasswd
chmod 640 /etc/optionsdashboard/htpasswd
# Nginx workers need traversal for the password file, but no access to secrets.
chmod 711 /etc/optionsdashboard
cat > /etc/nginx/sites-available/optionsdashboard <<EOF
server {
    listen 80;
    server_name $SERVER_IP;
    location /.well-known/acme-challenge/ { root /var/www/optionsdashboard-acme; }
    location / { return 404; }
}
EOF
ln -s /etc/nginx/sites-available/optionsdashboard /etc/nginx/sites-enabled/optionsdashboard
# Fresh server only; preserve the original default configuration for recovery.
if [[ -L /etc/nginx/sites-enabled/default ]]; then mv /etc/nginx/sites-enabled/default /etc/nginx/default-site.disabled; fi
nginx -t
systemctl reload nginx
/opt/optionsdashboard-tools/bin/certbot certonly --non-interactive --agree-tos --register-unsafely-without-email --preferred-profile shortlived --webroot --webroot-path /var/www/optionsdashboard-acme --ip-address "$SERVER_IP"
python3 - "$SERVER_IP" <<'PY'
import json,sys
from pathlib import Path
ip=sys.argv[1]
token=json.loads(Path('/etc/optionsdashboard/initial-access.json').read_text())['proxy_token']
config='''server {
    listen 80;
    server_name IP;
    location /.well-known/acme-challenge/ { root /var/www/optionsdashboard-acme; }
    location / { return 301 https://IP$request_uri; }
}
server {
    listen 443 ssl;
    server_name IP;
    ssl_certificate /etc/letsencrypt/live/IP/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/IP/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    auth_basic "OptionsDashboard";
    auth_basic_user_file /etc/optionsdashboard/htpasswd;
    client_max_body_size 32k;
    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Terminal-Proxy-Token "TOKEN";
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600;
        proxy_buffering off;
    }
}
'''.replace('IP',ip).replace('TOKEN',token)
path=Path('/etc/nginx/sites-available/optionsdashboard')
path.write_text(config)
path.chmod(0o600)
PY
cat > /etc/systemd/system/optionsdashboard.service <<EOF
[Unit]
Description=OptionsDashboard trading terminal
After=network-online.target
Wants=network-online.target
[Service]
User=optionsdashboard
Group=optionsdashboard
WorkingDirectory=$PROJECT
EnvironmentFile=/etc/optionsdashboard/server.env
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$PROJECT/.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8765 --workers 1 --no-access-log --no-proxy-headers
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DATA
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/optionsdashboard-cert-renew.service <<'EOF'
[Unit]
Description=Renew OptionsDashboard HTTPS certificate
[Service]
Type=oneshot
ExecStart=/opt/optionsdashboard-tools/bin/certbot renew --quiet --deploy-hook "systemctl reload nginx"
EOF
cat > /etc/systemd/system/optionsdashboard-cert-renew.timer <<'EOF'
[Unit]
Description=Daily OptionsDashboard certificate renewal
[Timer]
OnCalendar=*-*-* 03:00:00
RandomizedDelaySec=3600
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now optionsdashboard optionsdashboard-cert-renew.timer
# Use the tested virtual-environment Certbot, not Ubuntu's older IP-incompatible version.
systemctl disable --now certbot.timer 2>/dev/null || true
nginx -t
systemctl reload nginx
echo 'Dashboard deployed in dry mode. Initial login is in /etc/optionsdashboard/initial-access.json (root only).'
